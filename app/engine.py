"""
Rule-based recommendations engine with AIMI code logic baked in.

Encodes the safety/threshold logic observed in the OpenAPS AIMI source:
  - HypoThresholdMath: LGS is a hard floor; must be set sensibly
  - PkpdPresetProfiles: Ultra-fast preset bounds (DIA 5-8h, peak 35-95, init DIA 6.0)
  - PkpdSmbTailDamping: cautious=0.92, neutral=0.85, permissive=0.70
  - PkpdCorrectionPrudence: ISF fusion neutral=(0.75,1.25), prudent=(0.85,1.10)
  - InsulinStackingStance: Max IOB ceiling; priority factor 1.20 + 2.0U extra
  - PkpdLearningPace: slow=0.2h/2min, normal=0.5h/5min, fast=1.0h/10min

All glucose values handled in mmol/L for the user (units in profile).
Each recommendation includes: id, severity, title, detail, current, recommended,
and the code reference that justifies it.
"""

MMOL = 18.018

SEV_CRITICAL = "critical"
SEV_WARN = "warn"
SEV_OK = "ok"
SEV_INFO = "info"

# Ultra-fast preset values from PkpdPresetProfiles.kt
ULTRAFAST = {
    "dia_min": 5.0, "dia_max": 8.0, "peak_min": 35.0, "peak_max": 95.0,
    "init_dia": 6.0, "init_peak": 55.0, "anchor_dia": 4.0, "anchor_peak": 55.0,
    "isf_min": 0.75, "isf_max": 1.25, "tail_damping": 0.85,
    "max_dia_change": 0.5, "max_peak_change": 5.0,
}


def _rec(rid, sev, title, detail, current=None, recommended=None, code_ref=None):
    return {
        "id": rid, "severity": sev, "title": title, "detail": detail,
        "current": current, "recommended": recommended, "code_ref": code_ref,
    }


def generate(analysis: dict, profile: dict, settings: dict, actual_tdd_est=None) -> dict:
    """
    analysis: output of analytics.analyze_entries
    profile: output of analytics.extract_profile (units, isf, cr, basal_tdd, ...)
    settings: dict of AIMI settings the user entered (may be partial)
    Returns dict with recommendations[], score, summary.
    """
    recs = []
    tir = analysis.get("tir", {})
    in_range = tir.get("in_range", 0)
    low = tir.get("low", 0) + tir.get("very_low", 0)
    very_low = tir.get("very_low", 0)
    high = tir.get("high", 0) + tir.get("very_high", 0)
    cv = analysis.get("cv", 0)
    hourly = analysis.get("hourly", [])
    low_per_day = analysis.get("low_events_per_day", 0)

    s = settings or {}

    # ---- 1. LGS THRESHOLD (HypoThresholdMath.kt: hard floor override) ----
    lgs = s.get("lgs_threshold")
    if lgs is not None:
        lgs = float(lgs)
        if lgs > 5.0:
            recs.append(_rec(
                "lgs", SEV_CRITICAL,
                "Lower LGS Threshold — Immediate Safety Fix",
                f"At {lgs} mmol/L, insulin suspends while BG is still normal. "
                "HypoThresholdMath.computeHypoThreshold uses this as a hard floor that "
                "overrides the formula-based threshold, driving suspend→rise→overcorrect→low cycles.",
                f"{lgs} mmol/L", "4.0–4.2 mmol/L",
                "HypoThresholdMath.kt → computeHypoThreshold()",
            ))
        elif lgs > 4.4:
            recs.append(_rec(
                "lgs", SEV_WARN, "Consider lowering LGS slightly",
                f"LGS at {lgs} is on the higher side; 4.0–4.2 gives protection without premature suspends.",
                f"{lgs} mmol/L", "4.0–4.2 mmol/L",
                "HypoThresholdMath.kt",
            ))

    # ---- 2. MAX IOB (InsulinStackingStance + priority factor) ----
    max_iob = s.get("max_iob")
    tdd = actual_tdd_est or profile.get("basal_tdd")
    if max_iob is not None:
        max_iob = float(max_iob)
        # effective ceiling = max_iob * 1.20 + 2.0 (priority factors)
        eff = max_iob * 1.20 + 2.0
        # sensible ceiling ~ 0.15 * TDD if known, else heuristic
        target = round(0.15 * tdd, 1) if tdd else 6.5
        target = max(4.0, min(target, 8.0))
        if max_iob > target * 1.4:
            recs.append(_rec(
                "max_iob", SEV_CRITICAL,
                "Reduce Max IOB — Prevent Insulin Stacking",
                f"Max IOB {max_iob}U with priority factors gives an effective ceiling of "
                f"{eff:.1f}U (1.20× + 2.0U extra). "
                f"{'With TDD ~' + str(round(tdd)) + 'U/day, ' if tdd else ''}"
                "this allows excessive stacking. InsulinStackingStance.evaluate() is the brake.",
                f"{max_iob} U", f"{target} U",
                "InsulinStackingStance.kt + OApsAIMIPriorityMaxIobFactor",
            ))
        elif max_iob > target * 1.15:
            recs.append(_rec(
                "max_iob", SEV_WARN, "Max IOB slightly high",
                f"Consider lowering toward {target}U to tighten the stacking brake.",
                f"{max_iob} U", f"{target} U",
                "InsulinStackingStance.kt",
            ))

    # ---- 3. PK/PD INITIAL DIA (PkpdPresetProfiles ULTRA_FAST) ----
    init_dia = s.get("pkpd_initial_dia")
    insulin = (s.get("insulin_type") or "ultrafast").lower()
    if init_dia is not None and "ultra" in insulin:
        init_dia = float(init_dia)
        if abs(init_dia - ULTRAFAST["init_dia"]) > 0.3:
            recs.append(_rec(
                "init_dia", SEV_WARN,
                "Align PK/PD Initial DIA with Ultra-fast preset",
                f"Initial DIA {init_dia}h doesn't match the Ultra-fast preset (6.0h). "
                "Mismatch resets the learner's starting point on each restart. "
                "Use 'Run setup guide again' on the Simple tab to fix in one step.",
                f"{init_dia} h", "6.0 h",
                "PkpdPresetProfiles.kt → applyPkpdInsulinPreset(ULTRA_FAST)",
            ))

    # ---- 4. TDD7 calibration ----
    tdd7 = s.get("tdd7")
    if tdd7 is not None and tdd:
        tdd7 = float(tdd7)
        if tdd7 < tdd * 0.85:
            recs.append(_rec(
                "tdd7", SEV_WARN,
                "Raise TDD7 to better calibrate ISF",
                f"TDD7 is {tdd7}U but actual usage is ~{round(tdd)}U/day. "
                "ci = 450/TDD7 drives the carb limit and ISF anchor; underestimating TDD "
                "produces a looser ISF than reality requires.",
                f"{tdd7} U", f"{round(tdd)} U",
                "DetermineBasalAIMI2.kt line ~2794 (ci = 450/tdd7Days)",
            ))

    # ---- 5. ISF FUSION (PkpdCorrectionPrudence neutral = 0.75/1.25) ----
    isf_min = s.get("isf_fusion_min")
    isf_max = s.get("isf_fusion_max")
    if isf_max is not None:
        isf_max = float(isf_max)
        if isf_max < 1.23 and high > 15:
            recs.append(_rec(
                "isf_fusion", SEV_WARN,
                "Restore ISF Fusion toward neutral",
                f"ISF fusion max factor {isf_max} caps how aggressively AIMI tightens ISF "
                f"during rises. With {high:.0f}% time high, the ceiling is likely being hit. "
                "IsfFusion.fused() uses maxSafeIsf = tdd × maxFactor × 1.5.",
                f"min {isf_min} / max {isf_max}", "0.75 / 1.25",
                "PkpdCorrectionPrudence.kt (neutral = 0.75/1.25)",
            ))

    # ---- 6. SENSITIVITY RAISES TARGET (asymmetry check) ----
    srt = s.get("sensitivity_raises_target")
    rlt = s.get("resistance_lowers_target")
    if srt is False and rlt is True and low > 3:
        recs.append(_rec(
            "sens_target", SEV_WARN,
            "Enable 'Sensitivity raises target'",
            "You have resistance-lowers-target ON but sensitivity-raises-target OFF — "
            "a one-way ratchet. During sensitive periods the target never rises to protect you. "
            "Both flags are checked independently in the code.",
            "OFF", "ON",
            "DetermineBasalAIMI2.kt line ~2571",
        ))

    # ---- 7. LEARNING PACE (PkpdLearningPace) ----
    pace = s.get("learning_pace")
    if pace and pace.lower() == "slow":
        recs.append(_rec(
            "learning_pace", SEV_OK,
            "Consider switching learning speed to Normal",
            "Slow = 0.2h DIA / 2min peak change per day. Once the learner has converged, "
            "Normal (0.5h / 5min) adapts faster to site changes and daily variation.",
            "SLOW", "NORMAL",
            "PkpdSettingsSupport.kt → PkpdLearningPace",
        ))

    # ---- 8. SMB TAIL DAMPING (PkpdSmbTailDamping) ----
    tail = s.get("smb_tail_damping")
    if tail is not None:
        tail = float(tail)
        if low_per_day > 2 and tail < 0.90:
            recs.append(_rec(
                "tail_damping", SEV_OK,
                "Slightly increase SMB tail damping while lows are frequent",
                f"Tail damping {tail} sits between neutral (0.85) and cautious (0.92). "
                f"With {low_per_day} low episodes/day, nudging toward 0.90 dampens late-tail SMBs.",
                f"{tail}", "0.90–0.92",
                "PkpdSmbTailDamping.kt (cautious=0.92)",
            ))

    # ---- 9. OVERNIGHT PATTERN (data-driven) ----
    night = [h for h in hourly if h.get("hour") in (2, 3, 4, 5, 6) and h.get("mean_mmol")]
    if night:
        night_avg = sum(h["mean_mmol"] for h in night) / len(night)
        night_min = min(h.get("min_mmol", 99) for h in night)
        if night_min < 3.0 or night_avg < 5.5:
            recs.append(_rec(
                "overnight", SEV_WARN,
                "Overnight lows detected (2–6am)",
                f"Overnight average is {night_avg:.1f} mmol/L with lows down to {night_min} mmol/L. "
                "Combined with the LGS fix, review whether overnight basal carries too much, "
                "or evening IOB stacks into the night.",
                f"avg {night_avg:.1f} / min {night_min}", "target avg ~6.0",
                "data-driven (hourly pattern)",
            ))

    # ---- 10. EVENING HIGHS (data-driven) ----
    evening = [h for h in hourly if h.get("hour") in (17, 18, 19, 20) and h.get("mean_mmol")]
    if evening:
        eve_avg = sum(h["mean_mmol"] for h in evening) / len(evening)
        if eve_avg > 8.0:
            recs.append(_rec(
                "evening", SEV_INFO,
                "Evening highs detected (5–8pm)",
                f"Evening average is {eve_avg:.1f} mmol/L. This may reflect dinner coverage "
                "or an ISF fusion ceiling limiting corrections. Review CR/meal handling.",
                f"avg {eve_avg:.1f}", "target avg ~7.0",
                "data-driven (hourly pattern)",
            ))

    # ---- PK/PD targeted recommendations (always produced) ----
    pkpd = pkpd_recommendations(analysis, profile, s, tdd)

    # ---- Grouped slider view (everything, AIMI-style) ----
    slider_groups = build_slider_groups(analysis, profile, s, pkpd, tdd)

    # ---- Copy-paste checklist + optional prefs file ----
    checklist = build_checklist(slider_groups)
    prefs_kv = build_prefs_kv(slider_groups)

    score = _score(in_range, low, very_low, cv, low_per_day, recs)
    summary = _summary(in_range, low, cv, recs)

    return {
        "recommendations": recs,
        "pkpd": pkpd,
        "slider_groups": slider_groups,
        "checklist": checklist,
        "prefs_kv": prefs_kv,
        "score": score,
        "summary": summary,
        "counts": {
            "critical": sum(1 for r in recs if r["severity"] == SEV_CRITICAL),
            "warn": sum(1 for r in recs if r["severity"] == SEV_WARN),
            "ok": sum(1 for r in recs if r["severity"] in (SEV_OK, SEV_INFO)),
        },
    }


def pkpd_recommendations(analysis, profile, s, tdd):
    """
    Produce a complete recommended PK/PD configuration based on the glucose data
    and the Ultra-fast / Rapid / Standard preset logic in PkpdPresetProfiles.kt.
    Always returns concrete target values plus reasoning — even when the user
    hasn't entered their current settings.
    """
    insulin = (s.get("insulin_type") or "ultrafast").lower()
    tir = analysis.get("tir", {})
    in_range = tir.get("in_range", 0)
    low = tir.get("low", 0) + tir.get("very_low", 0)
    high = tir.get("high", 0) + tir.get("very_high", 0)
    cv = analysis.get("cv", 0)
    low_per_day = analysis.get("low_events_per_day", 0)
    hourly = analysis.get("hourly", [])

    # Preset bounds (PkpdPresetProfiles.kt)
    presets = {
        "ultrafast": {"label": "Ultra-fast (Fiasp / Lyumjev)",
                      "dia_min": 5.0, "dia_max": 8.0, "peak_min": 35, "peak_max": 95,
                      "init_dia": 6.0, "init_peak": 55, "anchor_dia": 4.0, "anchor_peak": 55},
        "rapid": {"label": "Rapid (Humalog / Novorapid)",
                  "dia_min": 5.0, "dia_max": 9.0, "peak_min": 55, "peak_max": 120,
                  "init_dia": 6.5, "init_peak": 75, "anchor_dia": 5.0, "anchor_peak": 75},
        "standard": {"label": "Standard (Actrapid)",
                     "dia_min": 6.0, "dia_max": 10.0, "peak_min": 120, "peak_max": 240,
                     "init_dia": 8.0, "init_peak": 180, "anchor_dia": 6.0, "anchor_peak": 180},
    }
    p = presets.get(insulin, presets["ultrafast"])

    # ISF fusion target. Neutral = (0.75, 1.25) from PkpdCorrectionPrudence.
    # Lean prudent if lows are frequent; lean wider if highs dominate & lows are low.
    if low > 5 or low_per_day > 2.5:
        isf_min, isf_max, prudence = 0.80, 1.15, "prudent (frequent lows → tighter correction range)"
    elif high > 20 and low < 3:
        isf_min, isf_max, prudence = 0.72, 1.35, "wider (high time dominant, lows rare → more correction room)"
    else:
        isf_min, isf_max, prudence = 0.75, 1.25, "neutral (balanced)"

    # SMB tail damping. cautious=0.92, neutral=0.85, permissive=0.70 (PkpdSmbTailDamping)
    if low_per_day > 2:
        tail, tail_desc = 0.92, "cautious (frequent lows)"
    elif low_per_day > 1:
        tail, tail_desc = 0.88, "between neutral and cautious"
    else:
        tail, tail_desc = 0.85, "neutral"

    # Learning pace. Recommend slow only if data is very erratic, else normal.
    if cv > 40:
        pace, pace_desc = "slow", "high variability — keep learning stable"
    else:
        pace, pace_desc = "normal", "converged data — normal adaptation is safe"

    # Build the target config list
    items = []

    def item(name, target, reason, current=None, code_ref=None):
        items.append({"name": name, "target": target, "current": current,
                      "reason": reason, "code_ref": code_ref})

    item("Insulin preset", p["label"],
         "Pick the insulin you actually use; sets the learning bounds automatically.",
         s.get("insulin_type"), "PkpdPresetProfiles.applyPkpdInsulinPreset()")
    item("Initial DIA", f"{p['init_dia']} h",
         "Starting duration before learning. Matching the preset avoids a reset gap each restart.",
         _fmt(s.get("pkpd_initial_dia"), "h"), "PkpdPresetProfiles.kt")
    item("Initial Peak", f"{p['init_peak']} min",
         "Starting peak time for the insulin model.",
         None, "PkpdPresetProfiles.kt")
    item("DIA bounds", f"{p['dia_min']}–{p['dia_max']} h",
         "Hard limits the learner cannot exceed.", None, "PkpdLearningBounds.kt")
    item("Peak bounds", f"{p['peak_min']}–{p['peak_max']} min",
         "Hard limits for the learned peak.", None, "PkpdLearningBounds.kt")
    item("TAP-G blend weight", "0.55",
         "Default blend of insulin anchor vs learned peak. Leave at default unless advised.",
         None, "TapPeakGovernor.kt")
    item("ISF fusion min factor", f"{isf_min}",
         f"Lower bound on fused ISF — {prudence}.",
         _fmt(s.get("isf_fusion_min")), "PkpdCorrectionPrudence.kt")
    item("ISF fusion max factor", f"{isf_max}",
         f"Upper bound on fused ISF — {prudence}.",
         _fmt(s.get("isf_fusion_max")), "IsfFusion.fused()")
    item("Max ISF change/tick", "0.40",
         "Cap on how fast ISF can move per 5-min loop. Default is fine.",
         None, "IsfFusion.kt")
    item("SMB tail damping", f"{tail}",
         f"Late-action SMB reduction — {tail_desc}.",
         _fmt(s.get("smb_tail_damping")), "PkpdSmbTailDamping.kt")
    item("Learning pace", pace.upper(),
         f"{pace_desc}.", (s.get("learning_pace") or "").upper() or None,
         "PkpdSettingsSupport.PkpdLearningPace")

    # Pragmatic relief & guards — sensible defaults
    item("Pragmatic relief", "ON (min 0.75)",
         "Keeps SMB intent from being over-reduced in meal/high-rise contexts.",
         None, "AimiPkpdPragmaticRelief")
    item("IOB surveillance guard", "ON",
         "Anti-stacking protection when BG is high with meaningful IOB. Keep on.",
         None, "InsulinStackingStance.kt")

    # Headline guidance based on glucose pattern
    notes = []
    evening = [h for h in hourly if h.get("hour") in (17, 18, 19, 20) and h.get("mean_mmol")]
    if evening and sum(h["mean_mmol"] for h in evening) / len(evening) > 8.0:
        notes.append("Evening highs suggest the ISF fusion ceiling may be limiting corrections — "
                     "the wider max factor above gives AIMI more room after dinner.")
    night = [h for h in hourly if h.get("hour") in (2, 3, 4, 5, 6) and h.get("mean_mmol")]
    if night and min(h.get("min_mmol", 99) for h in night) < 3.0:
        notes.append("Overnight lows mean the cautious tail damping and tighter ISF floor are the "
                     "safer choice until the lows resolve.")

    # Attach reference descriptions (short tooltip + long + direction) to each item
    try:
        from .pkpd_reference import get_reference
        ref = get_reference()
        for it in items:
            r = ref.get(it["name"])
            if r:
                it["info_short"] = r.get("short")
                it["info_long"] = r.get("long")
                it["effect_up"] = r.get("effect_up")
                it["effect_down"] = r.get("effect_down")
    except Exception:
        pass

    return {
        "preset": p["label"],
        "items": items,
        "notes": notes,
        "prudence": prudence,
    }


def _fmt(v, unit=""):
    if v is None or v == "":
        return None
    return f"{v}{(' ' + unit) if unit else ''}"


def _num(val):
    """Pull the first number out of a value like '5.6 mmol/L', '0.78', 11, None."""
    if val is None:
        return None
    if isinstance(val, (int, float)):
        return float(val)
    import re
    m = re.search(r"-?\d+(?:\.\d+)?", str(val))
    return float(m.group()) if m else None


def build_slider_groups(analysis, profile, settings, pkpd, tdd):
    """
    Assemble a grouped list of every setting with current + recommended values
    positioned for a visual slider track. Combines:
      - profile values (ISF/CR/target)
      - main AIMI settings (LGS, Max IOB, TDD7, ...)
      - PK/PD items already computed in `pkpd`
    Returns: [{group, items:[{name, type, min, max, step, unit, current, recommended,
                              lower_better, status, ref...}]}]
    """
    from .slider_specs import get_specs, group_order
    from .pkpd_reference import get_reference
    specs = get_specs()
    ref = get_reference()
    s = settings or {}

    # Collect recommended values keyed by name.
    rec = {}

    # Profile recommendations (light touch — recommend keeping unless clearly off)
    if profile.get("isf"):
        rec["ISF (profile)"] = (profile["isf"], "Profile fallback ISF — AIMI blends around this.")
    if profile.get("cr"):
        cr = profile["cr"]
        cr_target = cr if cr >= 6 else 6.0
        rec["Carb ratio"] = (cr_target, "Very low CR over-boluses meals; 6–10 g/U is typical for adults.")
    if profile.get("target_low"):
        rec["BG target"] = (profile["target_low"], "Keep target as set unless lows/highs cluster.")

    # Main AIMI recommendations
    lgs = _num(s.get("lgs_threshold"))
    rec["LGS threshold"] = (4.1 if (lgs and lgs > 4.4) else (lgs or 4.1),
                            "Suspends insulin below this. 4.0–4.2 protects without premature suspends.")
    miob = _num(s.get("max_iob"))
    miob_target = round(0.15 * tdd, 1) if tdd else 6.5
    miob_target = max(4.0, min(miob_target, 8.0))
    rec["Max IOB"] = (miob_target, "Stacking ceiling. ~15% of TDD is a sensible cap.")
    if s.get("max_basal") is not None:
        rec["Max basal"] = (_num(s.get("max_basal")), "Outer basal ceiling; rarely reached.")
    tdd7 = _num(s.get("tdd7"))
    if tdd:
        rec["TDD7"] = (round(tdd), "Match actual total daily dose so ISF calc is accurate.")
    if s.get("dynisf_factor") is not None:
        rec["DynamicISF factor"] = (100, "AIMI builds ISF itself; 100% avoids double-aggression.")
    if s.get("smb_interval") is not None:
        rec["SMB interval"] = (_num(s.get("smb_interval")) or 6, "6 min is standard.")
    rec["Sensitivity raises target"] = (True, "Enable for symmetric protection during sensitive periods.")
    rec["Resistance lowers target"] = (s.get("resistance_lowers_target", True), "Fine as set.")

    # PK/PD items -> recommended values from the pkpd block
    for it in (pkpd.get("items") or []):
        rec[it["name"]] = (it["target"], it.get("reason"))

    # Current values keyed by name
    cur = {
        "ISF (profile)": profile.get("isf"),
        "Carb ratio": profile.get("cr"),
        "BG target": profile.get("target_low"),
        "LGS threshold": s.get("lgs_threshold"),
        "Max IOB": s.get("max_iob"),
        "Max basal": s.get("max_basal"),
        "DynamicISF factor": s.get("dynisf_factor"),
        "TDD7": s.get("tdd7"),
        "SMB interval": s.get("smb_interval"),
        "Sensitivity raises target": s.get("sensitivity_raises_target"),
        "Resistance lowers target": s.get("resistance_lowers_target"),
        "Insulin preset": s.get("insulin_type"),
        "Initial DIA": s.get("pkpd_initial_dia"),
        "ISF fusion min factor": s.get("isf_fusion_min"),
        "ISF fusion max factor": s.get("isf_fusion_max"),
        "SMB tail damping": s.get("smb_tail_damping"),
        "Learning pace": (s.get("learning_pace") or "").upper() or None,
    }

    # Build grouped structure
    groups = {g: [] for g in group_order()}
    for name, spec in specs.items():
        recommended = rec.get(name, (None, None))
        rv = recommended[0] if isinstance(recommended, tuple) else recommended
        reason = recommended[1] if isinstance(recommended, tuple) else None
        cv = cur.get(name)
        r = ref.get(name, {})

        rec_num = _num(rv)
        cur_num = _num(cv)
        status = "unknown"
        if spec.get("type") == "toggle":
            # interpret booleans
            cb = _truthy(cv)
            rb = _truthy(rv)
            status = "match" if cb is not None and cb == rb else ("diff" if cb is not None else "unknown")
        elif rec_num is not None and cur_num is not None:
            tol = (spec.get("step", 0.01) or 0.01)
            status = "match" if abs(rec_num - cur_num) <= tol else "diff"

        item = {
            "name": name,
            "type": spec.get("type", "range"),
            "min": spec.get("min"), "max": spec.get("max"), "step": spec.get("step"),
            "unit": spec.get("unit", ""),
            "options": spec.get("options"),
            "lower_better": spec.get("lower_better", False),
            "current": cv, "current_num": cur_num,
            "recommended": rv if not isinstance(rv, float) else round(rv, 3),
            "recommended_num": rec_num,
            "reason": reason or r.get("short"),
            "info_short": r.get("short"), "info_long": r.get("long"),
            "effect_up": r.get("effect_up"), "effect_down": r.get("effect_down"),
            "code": r.get("code"),
            "status": status,
        }
        grp = spec.get("group", "Other")
        groups.setdefault(grp, []).append(item)

    # Return as ordered list, dropping empty groups
    out = []
    for g in group_order():
        if groups.get(g):
            out.append({"group": g, "items": groups[g]})
    return out


def _truthy(v):
    if v is None:
        return None
    if isinstance(v, bool):
        return v
    sv = str(v).strip().lower()
    if sv in ("on", "true", "1", "yes", "enabled"):
        return True
    if sv in ("off", "false", "0", "no", "disabled"):
        return False
    return None


# Map display names -> AAPS/AIMI preference keys (best-effort, for the prefs file export).
# Profile values (ISF/CR/target/basal) are NOT included — those go via profile, not prefs.
AAPS_PREF_KEYS = {
    "LGS threshold": "OApsAIMIlgsThreshold",
    "Max IOB": "OApsAIMIMaxIOB",
    "Max basal": "OApsAIMIMaxBasal",
    "DynamicISF factor": "DynISFAdjust",
    "TDD7": "OApsAIMITDD7",
    "SMB interval": "OApsAIMISMBInterval",
    "Sensitivity raises target": "sensitivity_raises_target",
    "Resistance lowers target": "resistance_lowers_target",
    "Initial DIA": "OApsAIMIPkpdInitialDiaH",
    "Initial Peak": "OApsAIMIPkpdInitialPeakMin",
    "TAP-G blend weight": "OApsAIMITapGLearnedPeakBlendWeight",
    "ISF fusion min factor": "OApsAIMIIsfFusionMinFactor",
    "ISF fusion max factor": "OApsAIMIIsfFusionMaxFactor",
    "Max ISF change/tick": "OApsAIMIIsfFusionMaxChangePerTick",
    "SMB tail damping": "OApsAIMISmbTailDamping",
    "Tail fraction threshold": "OApsAIMISmbTailThreshold",
    "Exercise damping factor": "OApsAIMISmbExerciseDamping",
    "Late meal/fat damping factor": "OApsAIMISmbLateFatDamping",
    "Pragmatic relief": "OApsAIMIPkpdPragmaticReliefEnabled",
    "PKPD relief minimum factor": "OApsAIMIPkpdPragmaticReliefMinFactor",
    "Red Carpet restore threshold": "OApsAIMIRedCarpetRestoreThreshold",
    "IOB surveillance guard": "OApsAIMIIobSurveillanceGuard",
    "Priority MaxIOB factor": "OApsAIMIPriorityMaxIobFactor",
    "DynISF trajectory tuning": "OApsAIMIDynIsfTrajectoryTuning",
    "DynISF shadow only": "OApsAIMIDynIsfTrajectoryShadow",
    "DynISF max ISF change/tick": "OApsAIMIDynIsfTrajectoryMaxChange",
    "Learning pace": "OApsAIMIPkpdLearningPace",
    "Insulin preset": "OApsAIMIPkpdInsulinPreset",
}


def build_checklist(slider_groups):
    """
    Produce a plain-text, AIMI-worded checklist of settings to change, grouped by
    the AIMI screen. Only includes settings that differ from the recommendation.
    Returns {"text": <str>, "change_count": <int>, "groups": [...]}.
    """
    lines = ["AIMI SETTINGS — RECOMMENDED CHANGES", "=" * 38, ""]
    change_count = 0
    groups_out = []
    for g in slider_groups:
        changed = [it for it in g["items"] if it.get("status") == "diff"]
        if not changed:
            continue
        lines.append(f"▸ {g['group']}")
        items_out = []
        for it in changed:
            change_count += 1
            unit = (" " + it["unit"]) if it.get("unit") else ""
            if it["type"] == "toggle":
                rv = _truthy(it.get("recommended"))
                target = "ON" if rv else "OFF"
                cur = it.get("current")
                cur_s = "ON" if _truthy(cur) else ("OFF" if cur is not None else "unset")
                lines.append(f"   [ ] {it['name']}: set to {target}  (currently {cur_s})")
            else:
                rec = it.get("recommended_num")
                cur = it.get("current_num")
                rec_s = f"{rec}{unit}" if rec is not None else str(it.get("recommended"))
                cur_s = f"{cur}{unit}" if cur is not None else "not set"
                lines.append(f"   [ ] {it['name']}: set to {rec_s}  (currently {cur_s})")
            items_out.append({"name": it["name"], "recommended": it.get("recommended"),
                              "current": it.get("current"), "type": it["type"]})
        lines.append("")
        groups_out.append({"group": g["group"], "items": items_out})
    if change_count == 0:
        lines = ["No changes needed — your settings already match the recommendations."]
    else:
        lines.append(f"Total changes: {change_count}")
        lines.append("")
        lines.append("Set these manually in AIMI. Review each with your care team first.")
    return {"text": "\n".join(lines), "change_count": change_count, "groups": groups_out}


def build_prefs_kv(slider_groups):
    """
    Build a flat key->value map of recommended AIMI preference keys, suitable for
    merging into an AAPS preferences export. Only includes settings we have a
    confident key mapping for AND that differ from current.
    NOTE: this is provided as a convenience; importing carries risk (see UI warning).
    """
    kv = {}
    for g in slider_groups:
        for it in g["items"]:
            if it.get("status") != "diff":
                continue
            key = AAPS_PREF_KEYS.get(it["name"])
            if not key:
                continue
            if it["type"] == "toggle":
                kv[key] = bool(_truthy(it.get("recommended")))
            elif it["type"] == "choice":
                kv[key] = it.get("recommended")
            else:
                v = it.get("recommended_num")
                if v is not None:
                    kv[key] = v
    return kv


def _score(in_range, low, very_low, cv, low_per_day, recs):
    score = 100
    # TIR component
    if in_range < 70:
        score -= (70 - in_range) * 0.8
    # Lows
    if low > 4:
        score -= (low - 4) * 3
    if very_low > 1:
        score -= (very_low - 1) * 6
    # Variability
    if cv > 36:
        score -= (cv - 36) * 1.5
    # Episodes
    if low_per_day > 1:
        score -= (low_per_day - 1) * 3
    # Critical recs
    score -= sum(4 for r in recs if r["severity"] == SEV_CRITICAL)
    return max(0, min(100, round(score)))


def _summary(in_range, low, cv, recs):
    crit = sum(1 for r in recs if r["severity"] == SEV_CRITICAL)
    parts = []
    if in_range >= 70:
        parts.append(f"Time-in-range of {in_range}% is above the 70% clinical target.")
    else:
        parts.append(f"Time-in-range of {in_range}% is below the 70% target.")
    if low > 4:
        parts.append(f"Time below range ({low}%) exceeds the 4% safety limit.")
    if cv > 36:
        parts.append(f"Variability (CV {cv}%) is above the recommended 36%.")
    if crit:
        parts.append(f"{crit} critical safety issue(s) need attention.")
    if not crit and in_range >= 70 and low <= 4:
        parts.append("Control is solid with no critical safety gaps.")
    return " ".join(parts)
