"""Glucose analytics. Works in mg/dL internally; converts for display."""
from datetime import datetime, timezone
from statistics import mean, stdev

MGDL_PER_MMOL = 18.018

# Thresholds in mg/dL
T_VERY_LOW = 54
T_LOW = 70
T_HIGH = 180
T_VERY_HIGH = 250


def to_mmol(mgdl: float) -> float:
    return round(mgdl / MGDL_PER_MMOL, 1)


def analyze_entries(entries: list[dict], tz_offset_min: int = 0) -> dict:
    """entries: list of NS sgv dicts with 'sgv' (mg/dL) and 'date' (ms epoch)."""
    if not entries:
        return {"error": "no_data"}

    sgvs = [e["sgv"] for e in entries if e.get("sgv")]
    n = len(sgvs)
    if n < 100:
        return {"error": "insufficient_data", "count": n}

    first = min(e["date"] for e in entries)
    last = max(e["date"] for e in entries)
    days = max(1, (last - first) / 86400000)

    avg = mean(sgvs)
    sd = stdev(sgvs) if n > 1 else 0
    cv = (sd / avg * 100) if avg else 0

    def pct(cond):
        return round(sum(1 for v in sgvs if cond(v)) / n * 100, 1)

    tir = {
        "very_low": pct(lambda v: v < T_VERY_LOW),
        "low": pct(lambda v: T_VERY_LOW <= v < T_LOW),
        "in_range": pct(lambda v: T_LOW <= v <= T_HIGH),
        "high": pct(lambda v: T_HIGH < v <= T_VERY_HIGH),
        "very_high": pct(lambda v: v > T_VERY_HIGH),
    }

    # Hourly buckets (local time via offset)
    hourly = {h: [] for h in range(24)}
    for e in entries:
        if not e.get("sgv"):
            continue
        dt = datetime.fromtimestamp(e["date"] / 1000, tz=timezone.utc)
        local_h = (dt.hour + tz_offset_min // 60) % 24
        hourly[local_h].append(e["sgv"])

    hourly_stats = []
    for h in range(24):
        vals = hourly[h]
        if vals:
            hourly_stats.append({
                "hour": h,
                "mean_mmol": to_mmol(mean(vals)),
                "mean_mgdl": round(mean(vals)),
                "std_mmol": to_mmol(stdev(vals)) if len(vals) > 1 else 0,
                "min_mmol": to_mmol(min(vals)),
                "max_mmol": to_mmol(max(vals)),
                "n": len(vals),
            })
        else:
            hourly_stats.append({"hour": h, "mean_mmol": None, "n": 0})

    # Low episode counting. A new episode starts when BG drops below T_LOW after
    # having been at/above T_LOW for at least RECOVERY_MIN minutes (so brief
    # sensor wiggles around the threshold don't each count as a fresh event).
    RECOVERY_MIN = 15
    low_events = 0
    in_low = False
    above_since = None  # ms timestamp BG first went >= T_LOW after a low
    sorted_e = sorted(entries, key=lambda x: x.get("date", 0))
    for e in sorted_e:
        v = e.get("sgv")
        if v is None:
            continue
        ts = e.get("date", 0)
        if v < T_LOW:
            if not in_low:
                low_events += 1
                in_low = True
            above_since = None
        else:
            if in_low:
                # require sustained recovery before allowing a new episode
                if above_since is None:
                    above_since = ts
                elif ts - above_since >= RECOVERY_MIN * 60000:
                    in_low = False
                    above_since = None

    # GMI / eA1c estimate (ADAG)
    gmi = round(3.31 + 0.02392 * avg, 1)

    return {
        "n": n,
        "days": round(days, 1),
        "mean_mgdl": round(avg),
        "mean_mmol": to_mmol(avg),
        "sd_mgdl": round(sd),
        "cv": round(cv, 1),
        "gmi": gmi,
        "tir": tir,
        "hourly": hourly_stats,
        "low_events": low_events,
        "low_events_per_day": round(low_events / days, 1),
        "first": first,
        "last": last,
    }


def extract_profile(ns_profile: dict | None) -> dict:
    """Pull basal/ISF/CR/target from a Nightscout profile document."""
    out = {"basal_tdd": None, "isf": None, "cr": None, "target_low": None,
            "target_high": None, "units": "mmol", "dia": None}
    if not ns_profile:
        return out
    store = ns_profile.get("store", {})
    default_name = ns_profile.get("defaultProfile")
    prof = store.get(default_name) if default_name else None
    if not prof and store:
        prof = next(iter(store.values()))
    if not prof:
        return out

    out["units"] = prof.get("units", "mmol")
    out["dia"] = prof.get("dia")

    basals = prof.get("basal", [])
    if basals:
        # TDD = sum over the day
        tdd = 0.0
        for i, b in enumerate(basals):
            start = b.get("timeAsSeconds", 0)
            end = basals[i + 1]["timeAsSeconds"] if i + 1 < len(basals) else 86400
            hours = (end - start) / 3600
            tdd += b.get("value", 0) * hours
        out["basal_tdd"] = round(tdd, 2)

    def first_val(key):
        arr = prof.get(key, [])
        return arr[0].get("value") if arr else None

    out["isf"] = first_val("sens")
    out["cr"] = first_val("carbratio")
    tgt_low = prof.get("target_low", [])
    tgt_high = prof.get("target_high", [])
    out["target_low"] = tgt_low[0].get("value") if tgt_low else None
    out["target_high"] = tgt_high[0].get("value") if tgt_high else None
    return out


def actual_tdd(treatments: list[dict], days: float) -> float | None:
    """Estimate actual TDD from bolus + basal treatments."""
    if not treatments or days <= 0:
        return None
    total = 0.0
    for t in treatments:
        ins = t.get("insulin")
        if ins:
            total += ins
    # This captures boluses/SMBs; basal handled separately via profile
    return round(total / days, 1) if total else None
