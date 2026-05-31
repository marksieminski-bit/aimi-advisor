"""
Stability gate.

Adaptive systems like AIMI learn slowly. Changing settings faster than they can
settle fights the learner and makes control worse. This module infers when the
user last changed each setting (by diffing imported-settings snapshots over time)
and produces:

  - per-setting "settling" status (changed within the last SETTLE_DAYS days)
  - an over-tweaking warning when a new change is made while another is still settling
  - a plain-language change log

Design choices (from the user):
  - settle window = 7 days
  - soft gate: recommendations are still shown, just flagged "still settling"
  - over-tweaking = any change made while another setting is still settling

Nothing here changes AIMI. It only annotates the advisor's own recommendations.
"""
from datetime import datetime, timezone

SETTLE_DAYS = 7

# Friendly names for settings keys (for the change log / warnings)
SETTING_LABELS = {
    "lgs_threshold": "LGS threshold",
    "max_iob": "Max IOB",
    "max_basal": "Max basal",
    "dynisf_factor": "DynamicISF factor",
    "tdd7": "TDD7",
    "smb_interval": "SMB interval",
    "pkpd_initial_dia": "Initial DIA",
    "pkpd_initial_peak": "Initial peak",
    "isf_fusion_min": "ISF fusion min",
    "isf_fusion_max": "ISF fusion max",
    "smb_tail_damping": "SMB tail damping",
    "learning_pace": "Learning pace",
    "insulin_type": "Insulin preset",
}

# Map recommendation IDs (from engine) to the setting key they concern, so we can
# tell which recommendations touch a setting that is still settling.
REC_TO_SETTING = {
    "lgs": "lgs_threshold",
    "max_iob": "max_iob",
    "init_dia": "pkpd_initial_dia",
    "isf_fusion": "isf_fusion_max",
    "tail_damping": "smb_tail_damping",
    "learning_pace": "learning_pace",
    # 'overnight' and 'evening' are glucose-pattern recs, not tied to one setting,
    # so they are intentionally not gated by settling.
}


def _parse(ts):
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00")).replace(tzinfo=timezone.utc) \
            if "+" not in ts and "Z" not in ts else datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except Exception:
        try:
            return datetime.fromisoformat(ts)
        except Exception:
            return None


def _days_ago(ts):
    dt = _parse(ts)
    if not dt:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    now = datetime.now(timezone.utc)
    return (now - dt).total_seconds() / 86400.0


def compute_change_history(history):
    """
    history: list of {created_at, settings, source}, most-recent-first (from store).
    Returns:
      last_changed: {setting_key: days_ago_changed}   (when each setting last changed)
      change_log: list of {date, days_ago, changes:[{setting,label,from,to}]}  newest first
    """
    last_changed = {}
    change_log = []
    # Walk oldest→newest to compute diffs in order
    ordered = list(reversed(history))
    for i in range(1, len(ordered)):
        prev = ordered[i - 1]["settings"] or {}
        cur = ordered[i]["settings"] or {}
        ts = ordered[i]["created_at"]
        changes = []
        for k, v in cur.items():
            if k in prev and prev[k] != v:
                changes.append({
                    "setting": k,
                    "label": SETTING_LABELS.get(k, k),
                    "from": prev[k],
                    "to": v,
                })
                last_changed[k] = ts
        if changes:
            change_log.append({
                "date": ts,
                "days_ago": _days_ago(ts),
                "changes": changes,
            })
    change_log.reverse()  # newest first
    return last_changed, change_log


def settling_status(last_changed):
    """
    Returns {setting_key: {days_ago, days_left}} for settings still within the
    settle window (i.e. changed less than SETTLE_DAYS days ago).
    """
    settling = {}
    for k, ts in last_changed.items():
        d = _days_ago(ts)
        if d is not None and d < SETTLE_DAYS:
            settling[k] = {"label": SETTING_LABELS.get(k, k),
                           "days_ago": round(d, 1), "days_left": round(SETTLE_DAYS - d, 1)}
    return settling


def annotate_recommendations(recommendations, settling):
    """
    Add a 'settling' annotation to any recommendation whose setting was changed
    within the settle window. Soft gate — the rec is kept, just flagged.
    """
    out = []
    for r in recommendations:
        rid = r.get("id", "")
        setting = REC_TO_SETTING.get(rid)
        r = dict(r)
        if setting and setting in settling:
            st = settling[setting]
            r["settling"] = {
                "label": SETTING_LABELS.get(setting, setting),
                "days_ago": st["days_ago"],
                "days_left": st["days_left"],
                "note": (f"You changed {SETTING_LABELS.get(setting, setting)} "
                         f"{st['days_ago']:.0f} day(s) ago. Give it about {st['days_left']:.0f} "
                         f"more day(s) to settle before changing it again — the learner needs "
                         f"stable settings to adapt."),
            }
        out.append(r)
    return out


def overtweaking_warning(change_log, settling):
    """
    Over-tweaking = a change was made while another setting was still settling.
    Detect by checking, for each logged change, whether a DIFFERENT setting had
    been changed within SETTLE_DAYS before it.
    """
    # Flatten changes to (timestamp, setting) and sort by time
    events = []
    for entry in change_log:
        for c in entry["changes"]:
            events.append((_parse(entry["date"]), c["setting"], c["label"]))
    events = [e for e in events if e[0] is not None]
    events.sort(key=lambda x: x[0])

    overlaps = []
    for i in range(len(events)):
        ti, si, li = events[i]
        for j in range(i):
            tj, sj, lj = events[j]
            if sj != si and (ti - tj).total_seconds() / 86400.0 < SETTLE_DAYS:
                overlaps.append((lj, li))
    # Also: currently >1 setting settling at once is itself an over-tweak signal
    active = len(settling)
    if not overlaps and active <= 1:
        return None

    msg = None
    if active > 1:
        names = ", ".join(s["label"] for s in settling.values())
        msg = (f"{active} settings are still settling at once ({names}). Changing several "
               "things together means you can't tell which one helped or hurt — and the "
               "learner is adapting to a moving target. Consider holding steady and changing "
               "one thing at a time.")
    elif overlaps:
        a, b = overlaps[-1]
        msg = (f"You changed {b} while {a} was still settling. Stacking changes makes it hard "
               "to know what's working and disrupts AIMI's learning. Try to let one change "
               "settle (~7 days) before making the next.")
    return msg


def build_stability(history, recommendations):
    """
    Top-level helper. Returns everything the UI needs:
      {settling, change_log, overtweaking, recommendations(annotated)}
    """
    if not history:
        return {
            "settling": {},
            "change_log": [],
            "overtweaking": None,
            "recommendations": recommendations,
            "has_history": False,
        }
    last_changed, change_log = compute_change_history(history)
    settling = settling_status(last_changed)
    annotated = annotate_recommendations(recommendations, settling)
    warning = overtweaking_warning(change_log, settling)
    return {
        "settling": settling,
        "change_log": change_log,
        "overtweaking": warning,
        "recommendations": annotated,
        "has_history": len(history) > 1,
    }
