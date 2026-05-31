"""
Bolus / meal / manual-intervention analysis.

Pulls Nightscout treatments (boluses, SMBs, carbs, temp basals) and correlates
them with glucose outcomes. CRITICAL FRAMING: this surfaces CO-OCCURRENCE only,
never causation. Glucose is driven by food, activity, hormones, sensor error and
timing simultaneously. Every output is phrased as "co-occurred with" / "preceded
by", and the UI must keep that framing. Nothing here is a dosing instruction.

All glucose handled in mg/dL internally (NS native), converted for display.
"""
from datetime import datetime, timezone

MGDL_PER_MMOL = 18.018
T_LOW = 70          # mg/dL
T_HIGH = 180

def _ts(t):
    """Treatment timestamp -> ms epoch. NS uses 'date' (ms) or 'created_at' (iso)."""
    if t.get("date"):
        return int(t["date"])
    ca = t.get("created_at")
    if ca:
        try:
            dt = datetime.fromisoformat(ca.replace("Z", "+00:00"))
            return int(dt.timestamp() * 1000)
        except Exception:
            return None
    return None


def _bg_at(entries_sorted, ts, window_ms=600000):
    """Nearest sgv within window of ts (default ±10min). entries_sorted by date asc."""
    if ts is None:
        return None
    best, bestd = None, window_ms + 1
    # linear scan is fine for these sizes; could bisect if needed
    for e in entries_sorted:
        d = abs(e["date"] - ts)
        if d < bestd:
            bestd, best = d, e
        elif e["date"] > ts + window_ms:
            break
    return best["sgv"] if best and bestd <= window_ms else None


def _bg_extreme_after(entries_sorted, ts, hours, kind="min"):
    """min or max sgv in the (ts, ts+hours] window."""
    if ts is None:
        return None
    end = ts + hours * 3600000
    vals = [e["sgv"] for e in entries_sorted if ts < e["date"] <= end]
    if not vals:
        return None
    return min(vals) if kind == "min" else max(vals)


def _is_smb(t):
    """
    Decide if a treatment is an automatic SMB (delivered by AIMI) vs a manual bolus.
    AAPS/AIMI tags SMBs several ways depending on version; we check all of them:
      1. Explicit boolean flag: isSMB / isSmb
      2. type field == "SMB"
      3. eventType "Correction Bolus" with NO carbs  (AAPS records SMBs as Correction Bolus)
      4. "smb" mentioned in notes
    Manual boluses are typically "Meal Bolus"/"Bolus", carry carbs, or are larger.
    Returns (is_smb: bool, reason: str).
    """
    # 1. explicit flag (definitive)
    for flag in ("isSMB", "isSmb", "is_smb"):
        if flag in t:
            return (bool(t[flag]), f"flag {flag}={t[flag]}")
    # 2. explicit type
    typ = (t.get("type") or "").upper()
    if typ == "SMB":
        return (True, "type=SMB")
    # 3. notes
    notes = (t.get("notes") or "").lower()
    if "smb" in notes:
        return (True, "notes mention SMB")
    # 4. eventType heuristics
    et = (t.get("eventType") or "").lower()
    carb = t.get("carbs") or 0
    ins = t.get("insulin") or 0
    if et in ("meal bolus", "snack bolus") or carb > 0:
        return (False, "meal/carb bolus → manual")
    if et == "correction bolus":
        # In AAPS, automatic SMBs are written as Correction Bolus.
        # A genuinely manual correction is possible but far less common and usually larger.
        if ins <= 1.0:
            return (True, "small Correction Bolus → SMB")
        return (False, "large Correction Bolus → likely manual")
    if et in ("bolus", "") :
        # bare 'Bolus' with no carbs: small = SMB, large = manual
        return (ins <= 0.6, "size-based (no clear type)")
    return (False, "default → manual")


def classify_treatments(treatments):
    """Split NS treatments into manual boluses, SMBs, carbs, others."""
    manual_bolus, smb, carbs, temp_basal, other = [], [], [], [], []
    for t in treatments:
        et = (t.get("eventType") or "").lower()
        ins = t.get("insulin")
        carb = t.get("carbs")
        if ins and ins > 0:
            is_smb, reason = _is_smb(t)
            t["_smb_reason"] = reason
            (smb if is_smb else manual_bolus).append(t)
        if carb and carb > 0:
            carbs.append(t)
        if et in ("temp basal", "temporary basal"):
            temp_basal.append(t)
        if not ins and not carb and et not in ("temp basal", "temporary basal"):
            other.append(t)
    return {"manual_bolus": manual_bolus, "smb": smb, "carbs": carbs,
            "temp_basal": temp_basal, "other": other}


def analyze_boluses(entries, treatments, tz_offset_min=0):
    """
    Returns a dict of correlation findings + an events summary. Correlation only.
    """
    if not entries:
        return {"error": "no_entries"}
    entries_sorted = sorted([e for e in entries if e.get("sgv")], key=lambda x: x["date"])
    cls = classify_treatments(treatments or [])

    manual = cls["manual_bolus"]
    smbs = cls["smb"]
    carbs = cls["carbs"]

    # How were SMBs detected? (transparency for the user)
    detect_reasons = {}
    for t in smbs:
        r = t.get("_smb_reason", "?")
        detect_reasons[r] = detect_reasons.get(r, 0) + 1
    smb_detect = max(detect_reasons, key=detect_reasons.get) if detect_reasons else "none"

    days_span = max(1, (entries_sorted[-1]["date"] - entries_sorted[0]["date"]) / 86400000)

    # --- Manual bolus summary + low co-occurrence ---
    manual_total_u = round(sum(t.get("insulin", 0) for t in manual), 1)
    smb_total_u = round(sum(t.get("insulin", 0) for t in smbs), 1)
    manual_count = len(manual)
    # lows within 3h after a manual bolus (co-occurrence)
    lows_after_manual = 0
    big_manual_uncovered = 0   # manual bolus with no carbs logged within 30min
    for t in manual:
        ts = _ts(t)
        lo = _bg_extreme_after(entries_sorted, ts, 3, "min")
        if lo is not None and lo < T_LOW:
            lows_after_manual += 1
        # uncovered: a correction-sized bolus with no carbs near it
        near_carb = any(abs((_ts(c) or 0) - (ts or 0)) <= 1800000 for c in carbs)
        if not near_carb and t.get("insulin", 0) >= 1.0:
            big_manual_uncovered += 1

    # --- Manual vs SMB share (a high manual share can fight the loop) ---
    total_bolus_u = manual_total_u + smb_total_u
    manual_share = round(manual_total_u / total_bolus_u * 100, 0) if total_bolus_u else 0

    # --- Meals that couldn't be corrected (hanging highs after carbs) ---
    uncorrected_meals = []
    for c in carbs:
        ts = _ts(c)
        carb_g = c.get("carbs", 0)
        start_bg = _bg_at(entries_sorted, ts)
        peak = _bg_extreme_after(entries_sorted, ts, 4, "max")
        end_bg = _bg_at(entries_sorted, (ts or 0) + 4 * 3600000) if ts else None
        # "uncorrected" = still high 4h later (didn't come back near range)
        if end_bg is not None and end_bg > T_HIGH + 20 and peak and peak > T_HIGH:
            uncorrected_meals.append({
                "ts": ts, "carbs": carb_g,
                "start_mmol": round(start_bg / MGDL_PER_MMOL, 1) if start_bg else None,
                "peak_mmol": round(peak / MGDL_PER_MMOL, 1),
                "end_mmol": round(end_bg / MGDL_PER_MMOL, 1),
            })

    # hour-of-day clustering for uncorrected meals
    meal_hours = {}
    for m in uncorrected_meals:
        if m["ts"]:
            dt = datetime.fromtimestamp(m["ts"] / 1000, tz=timezone.utc)
            h = (dt.hour + tz_offset_min // 60) % 24
            meal_hours[h] = meal_hours.get(h, 0) + 1
    worst_meal_hour = max(meal_hours, key=meal_hours.get) if meal_hours else None

    findings = []

    if manual_count:
        findings.append({
            "id": "manual_bolus_lows",
            "level": "warn" if lows_after_manual >= max(2, manual_count * 0.25) else "info",
            "title": "Manual boluses and following lows",
            "text": (f"{manual_count} manual bolus(es) over {round(days_span)} days "
                     f"({manual_total_u} U total). A low (<3.9) co-occurred within 3h in "
                     f"{lows_after_manual} case(s). Co-occurrence only — not proof the bolus "
                     "caused the low (food timing, activity and basal all contribute)."),
        })

    if manual_share >= 40 and total_bolus_u > 0:
        findings.append({
            "id": "manual_share",
            "level": "warn",
            "title": "Large share of insulin is manual",
            "text": (f"Manual boluses are {manual_share:.0f}% of total bolus insulin. A high manual "
                     "share can work against the loop: AIMI sees the IOB but didn't plan it, so it "
                     "may hold back its own corrections. Worth discussing whether some manual doses "
                     "could be announced as carbs/meals instead."),
        })

    if big_manual_uncovered:
        findings.append({
            "id": "uncovered_manual",
            "level": "info",
            "title": "Manual corrections without logged carbs",
            "text": (f"{big_manual_uncovered} manual bolus(es) ≥1 U had no carbs logged nearby. "
                     "If these were corrections, that's expected; if they covered unlogged food, "
                     "logging the carbs would let AIMI model them and reduce surprise lows later."),
        })

    if uncorrected_meals:
        hr = f" most often around {worst_meal_hour:02d}:00" if worst_meal_hour is not None else ""
        findings.append({
            "id": "uncorrected_meals",
            "level": "warn",
            "title": "Meals that stayed high after 4 hours",
            "text": (f"{len(uncorrected_meals)} logged meal(s) were still above ~10.9 mmol/L four "
                     f"hours later{hr}. This pattern often points to meal insulin being too little "
                     "or too late for those carbs, rather than anything the loop can fix afterward — "
                     "a candidate to review meal-bolus timing or carb ratio with your team."),
        })

    return {
        "summary": {
            "days": round(days_span, 1),
            "manual_bolus_count": manual_count,
            "manual_bolus_u": manual_total_u,
            "smb_count": len(smbs),
            "smb_u": smb_total_u,
            "smb_detect": smb_detect,
            "manual_share_pct": manual_share,
            "carb_entries": len(carbs),
            "lows_after_manual": lows_after_manual,
            "uncorrected_meals": len(uncorrected_meals),
        },
        "findings": findings,
        "uncorrected_meal_examples": sorted(
            uncorrected_meals, key=lambda m: m["peak_mmol"], reverse=True
        )[:5],
        "worst_meal_hour": worst_meal_hour,
    }
