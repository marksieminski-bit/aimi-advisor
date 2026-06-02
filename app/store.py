"""SQLite-backed multi-user profile store. Credentials encrypted at rest with Fernet."""
import json
import os
import sqlite3
from datetime import datetime

from cryptography.fernet import Fernet

DB_PATH = os.environ.get("AIMI_DB", "/data/aimi.db")
KEY_PATH = os.environ.get("AIMI_KEY", "/data/secret.key")


def _get_fernet() -> Fernet:
    if os.path.exists(KEY_PATH):
        with open(KEY_PATH, "rb") as f:
            key = f.read()
    else:
        key = Fernet.generate_key()
        os.makedirs(os.path.dirname(KEY_PATH), exist_ok=True)
        with open(KEY_PATH, "wb") as f:
            f.write(key)
        os.chmod(KEY_PATH, 0o600)
    return Fernet(key)


_fernet = None


def fernet():
    global _fernet
    if _fernet is None:
        _fernet = _get_fernet()
    return _fernet


def init_db():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    con = sqlite3.connect(DB_PATH)
    con.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            ns_url TEXT NOT NULL,
            ns_token_enc BLOB,
            ns_secret_enc BLOB,
            tz_offset_min INTEGER DEFAULT 0,
            settings_json TEXT DEFAULT '{}',
            created_at TEXT,
            updated_at TEXT
        )
    """)
    con.execute("""
        CREATE TABLE IF NOT EXISTS reports (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            created_at TEXT,
            analysis_json TEXT,
            recommendations_json TEXT,
            FOREIGN KEY(user_id) REFERENCES users(id)
        )
    """)
    con.execute("""
        CREATE TABLE IF NOT EXISTS settings_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            created_at TEXT,
            settings_json TEXT,
            source TEXT,
            FOREIGN KEY(user_id) REFERENCES users(id)
        )
    """)
    con.commit()
    con.close()


def _enc(val: str | None):
    if not val:
        return None
    return fernet().encrypt(val.encode())


def _dec(blob):
    if not blob:
        return None
    return fernet().decrypt(blob).decode()


def create_user(name, ns_url, ns_token=None, ns_secret=None, tz_offset_min=0,
                settings=None, owner_account_id=None):
    con = sqlite3.connect(DB_PATH)
    now = datetime.utcnow().isoformat()
    cur = con.execute(
        """INSERT INTO users (name, ns_url, ns_token_enc, ns_secret_enc, tz_offset_min,
           settings_json, created_at, updated_at, owner_account_id) VALUES (?,?,?,?,?,?,?,?,?)""",
        (name, ns_url, _enc(ns_token), _enc(ns_secret), tz_offset_min,
         json.dumps(settings or {}), now, now, owner_account_id),
    )
    con.commit()
    uid = cur.lastrowid
    con.close()
    return uid


def update_user(uid, **fields):
    con = sqlite3.connect(DB_PATH)
    sets, vals = [], []
    for k, v in fields.items():
        if k == "ns_token":
            sets.append("ns_token_enc=?"); vals.append(_enc(v))
        elif k == "ns_secret":
            sets.append("ns_secret_enc=?"); vals.append(_enc(v))
        elif k == "settings":
            sets.append("settings_json=?"); vals.append(json.dumps(v))
        elif k in ("name", "ns_url", "tz_offset_min"):
            sets.append(f"{k}=?"); vals.append(v)
    sets.append("updated_at=?"); vals.append(datetime.utcnow().isoformat())
    vals.append(uid)
    con.execute(f"UPDATE users SET {', '.join(sets)} WHERE id=?", vals)
    con.commit()
    con.close()


def get_user(uid):
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    row = con.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()
    con.close()
    if not row:
        return None
    return _row_to_user(row)


def _row_to_user(row):
    return {
        "id": row["id"], "name": row["name"], "ns_url": row["ns_url"],
        "ns_token": _dec(row["ns_token_enc"]), "ns_secret": _dec(row["ns_secret_enc"]),
        "tz_offset_min": row["tz_offset_min"],
        "settings": json.loads(row["settings_json"] or "{}"),
        "created_at": row["created_at"], "updated_at": row["updated_at"],
    }


def list_users(owner_account_id=None, is_admin=False):
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    if is_admin or owner_account_id is None:
        rows = con.execute(
            "SELECT id, name, ns_url, updated_at FROM users ORDER BY name").fetchall()
    else:
        rows = con.execute(
            "SELECT id, name, ns_url, updated_at FROM users WHERE owner_account_id=? ORDER BY name",
            (owner_account_id,)).fetchall()
    con.close()
    return [{"id": r["id"], "name": r["name"], "ns_url": r["ns_url"],
             "updated_at": r["updated_at"]} for r in rows]


def delete_user(uid):
    con = sqlite3.connect(DB_PATH)
    con.execute("DELETE FROM reports WHERE user_id=?", (uid,))
    con.execute("DELETE FROM users WHERE id=?", (uid,))
    con.commit()
    con.close()


def save_report(uid, analysis, recommendations):
    con = sqlite3.connect(DB_PATH)
    con.execute(
        "INSERT INTO reports (user_id, created_at, analysis_json, recommendations_json) VALUES (?,?,?,?)",
        (uid, datetime.utcnow().isoformat(), json.dumps(analysis), json.dumps(recommendations)),
    )
    con.commit()
    con.close()


def record_settings_snapshot(uid, settings, source="import"):
    """
    Save a timestamped snapshot of settings, but only if it DIFFERS from the most
    recent snapshot (so re-importing identical settings doesn't create noise).
    Returns the list of changed setting keys vs the previous snapshot (or [] if first).
    """
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    prev = con.execute(
        "SELECT settings_json FROM settings_history WHERE user_id=? ORDER BY created_at DESC LIMIT 1",
        (uid,),
    ).fetchone()
    prev_settings = json.loads(prev["settings_json"]) if prev else {}

    changed = []
    for k, v in (settings or {}).items():
        if k in prev_settings and prev_settings[k] != v:
            changed.append(k)
    # also catch keys that existed before and are now different/removed handled above

    # Only insert if it's the first snapshot or something actually changed
    if not prev or changed:
        con.execute(
            "INSERT INTO settings_history (user_id, created_at, settings_json, source) VALUES (?,?,?,?)",
            (uid, datetime.utcnow().isoformat(), json.dumps(settings or {}), source),
        )
        con.commit()
    con.close()
    return changed


def get_settings_history(uid, limit=20):
    """Most-recent-first list of {created_at, settings, source}."""
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    rows = con.execute(
        "SELECT created_at, settings_json, source FROM settings_history "
        "WHERE user_id=? ORDER BY created_at DESC LIMIT ?",
        (uid, limit),
    ).fetchall()
    con.close()
    return [{"created_at": r["created_at"], "settings": json.loads(r["settings_json"]),
             "source": r["source"]} for r in rows]
