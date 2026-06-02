"""
Authentication for the AIMI advisor.

Per-user accounts: each account logs in and sees ONLY the profiles it owns.
Passwords are hashed with Werkzeug's scrypt (never stored in plaintext).
Includes simple brute-force throttling since the login page is internet-exposed.

This is app-level auth. For an internet-facing app holding glucose data and AAPS
master passwords, running Cloudflare Access in front of this as well is strongly
recommended — that blocks unauthenticated traffic before it ever reaches here.
"""
import os
import sqlite3
import time
from datetime import datetime
from functools import wraps

from flask import session, redirect, url_for, request, jsonify
from werkzeug.security import generate_password_hash, check_password_hash

from .store import DB_PATH


# Self-registration is gated by an invite code, set via env. If unset, the app
# falls back to admin-only account creation (safe default — no open door).
def invite_code():
    return os.environ.get("AIMI_INVITE_CODE", "").strip()


def self_registration_enabled():
    return bool(invite_code())


def check_invite(code):
    expected = invite_code()
    if not expected:
        return False
    # constant-time compare to avoid leaking the code via timing
    import hmac
    return hmac.compare_digest((code or "").strip(), expected)


def init_auth_db():
    con = sqlite3.connect(DB_PATH)
    con.execute("""
        CREATE TABLE IF NOT EXISTS accounts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL,
            is_admin INTEGER DEFAULT 0,
            created_at TEXT
        )
    """)
    # Add owner column to users (profiles) if missing
    cols = [r[1] for r in con.execute("PRAGMA table_info(users)").fetchall()]
    if "owner_account_id" not in cols:
        con.execute("ALTER TABLE users ADD COLUMN owner_account_id INTEGER")
    con.commit()
    con.close()


# ---- brute-force throttle (in-memory; resets on restart) -------------------
_FAILS = {}          # key -> [count, first_attempt_ts]
_MAX_FAILS = 5
_WINDOW = 300        # 5 minutes
_LOCKOUT = 900       # 15 minutes after too many fails


def _throttle_key():
    # Throttle per source IP + username attempted
    ip = request.headers.get("CF-Connecting-IP") or request.remote_addr or "?"
    return ip


def is_locked_out():
    k = _throttle_key()
    rec = _FAILS.get(k)
    if not rec:
        return False
    count, first = rec
    if count >= _MAX_FAILS and (time.time() - first) < _LOCKOUT:
        return True
    # window expired -> reset
    if (time.time() - first) > _WINDOW:
        _FAILS.pop(k, None)
    return False


def record_fail():
    k = _throttle_key()
    rec = _FAILS.get(k)
    if not rec or (time.time() - rec[1]) > _WINDOW:
        _FAILS[k] = [1, time.time()]
    else:
        rec[0] += 1


def clear_fails():
    _FAILS.pop(_throttle_key(), None)


# ---- account CRUD ----------------------------------------------------------
def create_account(username, password, is_admin=False):
    username = (username or "").strip().lower()
    if not username or not password:
        return None, "Username and password required."
    if len(password) < 8:
        return None, "Password must be at least 8 characters."
    con = sqlite3.connect(DB_PATH)
    try:
        cur = con.execute(
            "INSERT INTO accounts (username, password_hash, is_admin, created_at) VALUES (?,?,?,?)",
            (username, generate_password_hash(password), 1 if is_admin else 0,
             datetime.utcnow().isoformat()),
        )
        con.commit()
        return cur.lastrowid, None
    except sqlite3.IntegrityError:
        return None, "That username is already taken."
    finally:
        con.close()


def verify_login(username, password):
    username = (username or "").strip().lower()
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    row = con.execute("SELECT * FROM accounts WHERE username=?", (username,)).fetchone()
    con.close()
    if not row:
        # Run a dummy check to keep timing similar (avoid username enumeration)
        check_password_hash("scrypt:32768:8:1$dummy$dummy", password or "x")
        return None
    if check_password_hash(row["password_hash"], password or ""):
        return {"id": row["id"], "username": row["username"], "is_admin": bool(row["is_admin"])}
    return None


def account_count():
    con = sqlite3.connect(DB_PATH)
    n = con.execute("SELECT COUNT(*) FROM accounts").fetchone()[0]
    con.close()
    return n


def change_password(account_id, old_password, new_password):
    if len(new_password or "") < 8:
        return False, "New password must be at least 8 characters."
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    row = con.execute("SELECT * FROM accounts WHERE id=?", (account_id,)).fetchone()
    if not row or not check_password_hash(row["password_hash"], old_password or ""):
        con.close()
        return False, "Current password is incorrect."
    con.execute("UPDATE accounts SET password_hash=? WHERE id=?",
                (generate_password_hash(new_password), account_id))
    con.commit()
    con.close()
    return True, None


# ---- session helpers -------------------------------------------------------
def current_account():
    aid = session.get("account_id")
    if not aid:
        return None
    return {"id": aid, "username": session.get("username"),
            "is_admin": session.get("is_admin", False)}


def login_session(account):
    session["account_id"] = account["id"]
    session["username"] = account["username"]
    session["is_admin"] = account["is_admin"]
    session.permanent = True


def logout_session():
    session.clear()


# ---- decorators ------------------------------------------------------------
def login_required(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        if not current_account():
            # API routes get 401 JSON; page routes redirect to login
            if request.path.startswith("/api/"):
                return jsonify({"error": "auth_required"}), 401
            return redirect(url_for("login_page", next=request.path))
        return f(*args, **kwargs)
    return wrapper


def owns_profile(account_id, profile_id):
    """True if this account owns the given profile (user) id."""
    con = sqlite3.connect(DB_PATH)
    row = con.execute("SELECT owner_account_id FROM users WHERE id=?", (profile_id,)).fetchone()
    con.close()
    if not row:
        return False
    return row[0] == account_id


def profile_guard(f):
    """For routes with <uid> = profile id: 403 unless the logged-in account owns it
    (admins may access any)."""
    @wraps(f)
    def wrapper(*args, **kwargs):
        acct = current_account()
        if not acct:
            if request.path.startswith("/api/"):
                return jsonify({"error": "auth_required"}), 401
            return redirect(url_for("login_page", next=request.path))
        uid = kwargs.get("uid") or kwargs.get("user_id")
        if uid is not None and not acct["is_admin"] and not owns_profile(acct["id"], int(uid)):
            return jsonify({"error": "forbidden"}), 403
        return f(*args, **kwargs)
    return wrapper
