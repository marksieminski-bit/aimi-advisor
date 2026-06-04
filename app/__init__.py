"""AIMI Settings Advisor — Flask app. Self-hostable, multi-user with login."""
import os
from datetime import timedelta

from flask import (Flask, jsonify, render_template, request, abort,
                   session, redirect, url_for)

from . import store
from . import auth
from .auth import (login_required, profile_guard, current_account)
from .nightscout import NightscoutClient
from .analytics import analyze_entries, extract_profile, actual_tdd
from . import engine
from .pkpd_reference import get_reference

app = Flask(__name__, template_folder="../templates", static_folder="../static")

# Session secret: read from env (persist across restarts), else generate one.
# In production set AIMI_SECRET so sessions survive restarts.
app.secret_key = os.environ.get("AIMI_SECRET") or os.urandom(32)
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    # Secure cookie when served over HTTPS (it is, via the Cloudflare tunnel).
    SESSION_COOKIE_SECURE=os.environ.get("AIMI_HTTPS", "1") == "1",
    PERMANENT_SESSION_LIFETIME=timedelta(days=14),
)

store.init_db()
auth.init_auth_db()

MIN_DAYS = 15
MAX_DAYS = 90


# ---------------------------------------------------------------------------
# AUTH ROUTES
# ---------------------------------------------------------------------------
@app.route("/login", methods=["GET", "POST"])
def login_page():
    # First-run: if no accounts exist, send to registration to create the first.
    if auth.account_count() == 0:
        return redirect(url_for("register_page"))

    if request.method == "POST":
        if auth.is_locked_out():
            return render_template("login.html",
                                   error="Too many attempts. Try again in a few minutes.",
                                   mode="login"), 429
        username = request.form.get("username", "")
        password = request.form.get("password", "")
        acct = auth.verify_login(username, password)
        if acct:
            auth.clear_fails()
            auth.login_session(acct)
            nxt = request.args.get("next") or url_for("index")
            # Only allow local redirects
            if not nxt.startswith("/"):
                nxt = url_for("index")
            return redirect(nxt)
        auth.record_fail()
        return render_template("login.html", error="Incorrect username or password.",
                               mode="login", self_reg=auth.self_registration_enabled()), 401
    return render_template("login.html", mode="login",
                           self_reg=auth.self_registration_enabled())


@app.route("/register", methods=["GET", "POST"])
def register_page():
    first_run = auth.account_count() == 0
    acct = current_account()
    is_admin = bool(acct and acct.get("is_admin"))
    self_reg = auth.self_registration_enabled()

    # Who's allowed to see/use this page?
    #   - first run (creating the admin), OR
    #   - an admin adding an account, OR
    #   - self-registration is enabled (invite code set)
    if not first_run and not is_admin and not self_reg:
        return redirect(url_for("login_page"))

    if request.method == "POST":
        username = request.form.get("username", "")
        password = request.form.get("password", "")
        confirm = request.form.get("confirm", "")
        invite = request.form.get("invite", "")

        # If this is a self-registration (not first run, not an admin creating it),
        # require a valid invite code.
        needs_invite = (not first_run) and (not is_admin)
        if needs_invite and not auth.check_invite(invite):
            return render_template("login.html", error="Invalid or missing invite code.",
                                   mode="register", first_run=first_run,
                                   self_reg=self_reg, needs_invite=True), 403

        if password != confirm:
            return render_template("login.html", error="Passwords don't match.",
                                   mode="register", first_run=first_run,
                                   self_reg=self_reg, needs_invite=needs_invite), 400
        # The very first account is the admin.
        aid, err = auth.create_account(username, password, is_admin=first_run)
        if err:
            return render_template("login.html", error=err,
                                   mode="register", first_run=first_run,
                                   self_reg=self_reg, needs_invite=needs_invite), 400
        if first_run:
            auth.login_session({"id": aid, "username": username.strip().lower(),
                                "is_admin": True})
            return redirect(url_for("index"))
        # Self-registered users get logged straight in; admin-created ones don't.
        if needs_invite:
            auth.login_session({"id": aid, "username": username.strip().lower(),
                                "is_admin": False})
        return redirect(url_for("index"))

    needs_invite = (not first_run) and (not is_admin)
    return render_template("login.html", mode="register", first_run=first_run,
                           self_reg=self_reg, needs_invite=needs_invite)


@app.route("/logout")
def logout():
    auth.logout_session()
    return redirect(url_for("login_page"))


@app.route("/account/password", methods=["POST"])
@login_required
def change_password():
    acct = current_account()
    ok, err = auth.change_password(
        acct["id"], request.form.get("old_password", ""),
        request.form.get("new_password", ""))
    if ok:
        return jsonify({"ok": True})
    return jsonify({"ok": False, "error": err}), 400


@app.route("/")
@login_required
def index():
    acct = current_account()
    return render_template("index.html",
                           users=store.list_users(owner_account_id=acct["id"],
                                                   is_admin=acct["is_admin"]),
                           account=acct)


@app.route("/api/users", methods=["GET"])
@login_required
def api_list_users():
    acct = current_account()
    return jsonify(store.list_users(owner_account_id=acct["id"],
                                    is_admin=acct["is_admin"]))


@app.route("/api/users", methods=["POST"])
@login_required
def api_create_user():
    acct = current_account()
    d = request.get_json(force=True)
    if not d.get("name") or not d.get("ns_url"):
        return jsonify({"error": "name and ns_url required"}), 400
    uid = store.create_user(
        name=d["name"], ns_url=d["ns_url"],
        ns_token=d.get("ns_token"), ns_secret=d.get("ns_secret"),
        tz_offset_min=int(d.get("tz_offset_min", 0)),
        settings=d.get("settings", {}),
        owner_account_id=acct["id"],
    )
    return jsonify({"id": uid})


@app.route("/api/users/<int:uid>", methods=["GET"])
@profile_guard
def api_get_user(uid):
    u = store.get_user(uid)
    if not u:
        abort(404)
    # Never return raw credentials to the client
    u_safe = {k: v for k, v in u.items() if k not in ("ns_token", "ns_secret")}
    u_safe["has_token"] = bool(u["ns_token"])
    u_safe["has_secret"] = bool(u["ns_secret"])
    return jsonify(u_safe)


@app.route("/api/users/<int:uid>", methods=["PUT"])
@profile_guard
def api_update_user(uid):
    if not store.get_user(uid):
        abort(404)
    d = request.get_json(force=True)
    allowed = {}
    for k in ("name", "ns_url", "tz_offset_min", "settings", "ns_token", "ns_secret"):
        if k in d and d[k] not in (None, ""):
            allowed[k] = d[k]
    store.update_user(uid, **allowed)
    return jsonify({"ok": True})


@app.route("/api/users/<int:uid>", methods=["DELETE"])
@profile_guard
def api_delete_user(uid):
    store.delete_user(uid)
    return jsonify({"ok": True})


@app.route("/api/users/<int:uid>/test", methods=["POST"])
@profile_guard
def api_test_connection(uid):
    u = store.get_user(uid)
    if not u:
        abort(404)
    client = NightscoutClient(u["ns_url"], u["ns_token"], u["ns_secret"])
    try:
        status = client.test_connection()
        return jsonify({"ok": True, "status": {
            "name": status.get("name"), "version": status.get("version"),
        }})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 502


@app.route("/api/users/<int:uid>/analyze", methods=["POST"])
@profile_guard
def api_analyze(uid):
    u = store.get_user(uid)
    if not u:
        abort(404)
    # User-selectable analysis window. Short windows are allowed but flagged as
    # statistically weak — see data_confidence below.
    ALLOWED_DAYS = [1, 2, 3, 5, 7, 10, 14, 15, 21, 30, 60, 90]
    try:
        days = int(request.args.get("days", 15))
    except (TypeError, ValueError):
        days = 15
    # clamp to the allowed range
    days = max(1, min(days, 90))

    client = NightscoutClient(u["ns_url"], u["ns_token"], u["ns_secret"])
    try:
        entries = client.get_entries(days=days)
    except Exception as e:
        return jsonify({"error": f"nightscout_fetch_failed: {e}"}), 502

    analysis = analyze_entries(entries, tz_offset_min=u["tz_offset_min"])
    if analysis.get("error"):
        return jsonify({"error": analysis["error"], "detail": analysis}), 422

    actual_days = analysis.get("days", 0)
    # Only hard-fail if there's essentially nothing to analyze.
    if actual_days < 1:
        return jsonify({
            "error": "insufficient_days",
            "message": "Not enough glucose data found to analyze.",
            "analysis": analysis,
        }), 422

    # Data-confidence rating — be honest about how much weight to put on the result.
    # Recommendations off 1-2 days are reacting to noise, not patterns.
    if actual_days < 3:
        confidence = {
            "level": "very_low",
            "label": "Very low confidence",
            "note": (f"Only {actual_days} day(s) of data. This is a snapshot, not a pattern — "
                     "a single bad day, meal, or activity can dominate it. Do NOT change "
                     "settings based on this; use it to spot something to watch, then look "
                     "at a longer window."),
        }
    elif actual_days < 7:
        confidence = {
            "level": "low",
            "label": "Low confidence",
            "note": (f"{actual_days} days of data. Enough to spot a trend forming, but short "
                     "windows are heavily affected by day-to-day variation. Treat patterns "
                     "as tentative and confirm over a longer window before acting."),
        }
    elif actual_days < 14:
        confidence = {
            "level": "moderate",
            "label": "Moderate confidence",
            "note": (f"{actual_days} days of data. Reasonable for spotting consistent patterns. "
                     "14+ days is preferred for settings decisions."),
        }
    else:
        confidence = {
            "level": "good",
            "label": "Good confidence",
            "note": f"{actual_days} days of data — a solid window for identifying stable patterns.",
        }

    treatments = client.get_treatments(days=days)
    ns_profile = client.get_profile()
    profile = extract_profile(ns_profile)

    # estimate actual TDD = profile basal TDD + bolus/day from treatments
    bolus_per_day = actual_tdd(treatments, analysis["days"]) or 0
    tdd_est = None
    if profile.get("basal_tdd"):
        tdd_est = round(profile["basal_tdd"] + bolus_per_day, 1)

    result = engine.generate(analysis, profile, u["settings"], actual_tdd_est=tdd_est)

    # Bolus / meal correlation analysis
    from .bolus_analysis import analyze_boluses
    bolus = analyze_boluses(entries, treatments, tz_offset_min=u["tz_offset_min"])

    # Stability gate: infer setting changes from snapshot history, flag settling
    # recommendations and over-tweaking. Soft gate — recs are annotated, not hidden.
    from .stability import build_stability
    history = store.get_settings_history(uid, limit=30)
    stability = build_stability(history, result.get("recommendations", []))
    # Replace recommendations with the annotated versions (carry the 'settling' flag)
    result["recommendations"] = stability["recommendations"]

    store.save_report(uid, analysis, result)
    # Cache the latest analysis context so the feedback endpoint can reuse it
    _LATEST[uid] = {"analysis": analysis, "bolus": bolus, "profile": profile,
                    "settings": u["settings"]}

    # Units: prefer the Nightscout profile's units, fall back to mmol
    units = (profile.get("units") or "mmol").lower()
    units = "mgdl" if "mg" in units else "mmol"

    return jsonify({
        "user": {"id": u["id"], "name": u["name"]},
        "analysis": analysis,
        "profile": profile,
        "units": units,
        "tdd_estimate": tdd_est,
        "bolus": bolus,
        "result": result,
        "days_requested": days,
        "days_actual": actual_days,
        "data_confidence": confidence,
        "stability": {
            "settling": stability["settling"],
            "change_log": stability["change_log"],
            "overtweaking": stability["overtweaking"],
            "has_history": stability["has_history"],
        },
    })


# In-memory cache of the most recent analysis per user (for the feedback generator).
_LATEST: dict = {}


@app.route("/api/users/<int:uid>/feedback", methods=["POST"])
@profile_guard
def api_feedback(uid):
    u = store.get_user(uid)
    if not u:
        abort(404)
    d = request.get_json(force=True) or {}
    ids = d.get("feedback", [])
    ctx = _LATEST.get(uid)
    if not ctx:
        return jsonify({"error": "run_analysis_first",
                        "message": "Run an analysis first so feedback can be checked against your data."}), 422
    from .feedback_engine import generate_from_feedback, FEEDBACK_OPTIONS
    suggestions = generate_from_feedback(ids, ctx["analysis"], ctx["bolus"],
                                         ctx["settings"], ctx["profile"])
    return jsonify({"suggestions": suggestions, "options": FEEDBACK_OPTIONS})


@app.route("/api/users/<int:uid>/import_settings", methods=["POST"])
@profile_guard
def api_import_settings(uid):
    u = store.get_user(uid)
    if not u:
        abort(404)
    from .aaps_import import parse_aaps_export
    # Accept raw text body or {"text": ..., "password": ...} JSON
    raw = None
    password = None
    if request.is_json:
        body = request.get_json(silent=True) or {}
        raw = body.get("text")
        password = body.get("password")
    if raw is None:
        raw = request.get_data(as_text=True)
    result = parse_aaps_export(raw, password=password)
    if not result.get("ok"):
        return jsonify(result), 422
    # Merge imported settings into existing (imported values win where present)
    merged = dict(u["settings"] or {})
    merged.update(result["settings"])
    store.update_user(uid, settings=merged)
    # Record a timestamped snapshot so the app can infer when settings change.
    changed_keys = store.record_settings_snapshot(uid, merged, source="import")
    return jsonify({
        "ok": True,
        "matched_count": result["matched_count"],
        "total_keys": result["total_keys"],
        "matched": result["matched"],
        "units": result["units"],
        "settings": merged,
        "changed_keys": changed_keys,
    })


@app.route("/api/feedback_options")
def api_feedback_options():
    from .feedback_engine import FEEDBACK_OPTIONS
    return jsonify(FEEDBACK_OPTIONS)


@app.route("/dashboard/<int:uid>")
@profile_guard
def dashboard(uid):
    u = store.get_user(uid)
    if not u:
        abort(404)
    return render_template("dashboard.html", user={"id": u["id"], "name": u["name"]},
                           min_days=MIN_DAYS)


@app.route("/api/reference")
def api_reference():
    return jsonify(get_reference())


@app.route("/health")
def health():
    return jsonify({"status": "ok"})


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8080)))
