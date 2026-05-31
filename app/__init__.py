"""AIMI Settings Advisor — Flask app. Self-hostable, multi-user."""
import os

from flask import Flask, jsonify, render_template, request, abort

from . import store
from .nightscout import NightscoutClient
from .analytics import analyze_entries, extract_profile, actual_tdd
from . import engine
from .pkpd_reference import get_reference

app = Flask(__name__, template_folder="../templates", static_folder="../static")
store.init_db()

MIN_DAYS = 15
MAX_DAYS = 90


@app.route("/")
def index():
    return render_template("index.html", users=store.list_users())


@app.route("/api/users", methods=["GET"])
def api_list_users():
    return jsonify(store.list_users())


@app.route("/api/users", methods=["POST"])
def api_create_user():
    d = request.get_json(force=True)
    if not d.get("name") or not d.get("ns_url"):
        return jsonify({"error": "name and ns_url required"}), 400
    uid = store.create_user(
        name=d["name"], ns_url=d["ns_url"],
        ns_token=d.get("ns_token"), ns_secret=d.get("ns_secret"),
        tz_offset_min=int(d.get("tz_offset_min", 0)),
        settings=d.get("settings", {}),
    )
    return jsonify({"id": uid})


@app.route("/api/users/<int:uid>", methods=["GET"])
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
def api_delete_user(uid):
    store.delete_user(uid)
    return jsonify({"ok": True})


@app.route("/api/users/<int:uid>/test", methods=["POST"])
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
def api_analyze(uid):
    u = store.get_user(uid)
    if not u:
        abort(404)
    days = int(request.args.get("days", MIN_DAYS))
    if days < MIN_DAYS:
        days = MIN_DAYS
    if days > MAX_DAYS:
        days = MAX_DAYS

    client = NightscoutClient(u["ns_url"], u["ns_token"], u["ns_secret"])
    try:
        entries = client.get_entries(days=days)
    except Exception as e:
        return jsonify({"error": f"nightscout_fetch_failed: {e}"}), 502

    analysis = analyze_entries(entries, tz_offset_min=u["tz_offset_min"])
    if analysis.get("error"):
        return jsonify({"error": analysis["error"], "detail": analysis}), 422
    if analysis["days"] < MIN_DAYS - 1:
        return jsonify({
            "error": "insufficient_days",
            "message": f"Only {analysis['days']} days of data found; need at least {MIN_DAYS}.",
            "analysis": analysis,
        }), 422

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
