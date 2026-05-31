"""
Feedback-driven setting suggestion generator.

The user answers a few structured questions about what they're experiencing
(e.g. "I go low overnight", "meals spike then crash"). For each piece of feedback
we produce:
  - the specific setting(s) to consider and an exact target value
  - the LOGIC (why this setting, in plain language)
  - the EVIDENCE: the actual numbers from their Nightscout analysis that do or
    do NOT support the feedback, so they can see whether the data agrees

CRITICAL: every suggestion is a candidate to discuss, never an instruction.
If the data does NOT back up the feedback, we say so plainly rather than
generating a change anyway. Nothing here computes a dose.
"""

MMOL = 18.018


def _mmol(mgdl):
    return round(mgdl / MMOL, 1) if mgdl is not None else None


# Feedback options the UI offers. Each maps to a handler below.
FEEDBACK_OPTIONS = [
    {"id": "overnight_lows", "label": "I go low overnight"},
    {"id": "overnight_highs", "label": "I run high overnight"},
    {"id": "meal_spikes", "label": "Meals spike then I crash"},
    {"id": "meals_stay_high", "label": "Meals stay high for hours"},
    {"id": "lows_after_correction", "label": "I go low after corrections"},
    {"id": "stubborn_highs", "label": "Highs are hard to bring down"},
    {"id": "too_much_variability", "label": "My BG swings a lot"},
]


def generate_from_feedback(feedback_ids, analysis, bolus, settings, profile):
    """
    feedback_ids: list of ids the user selected
    analysis: analytics.analyze_entries output
    bolus: bolus_analysis.analyze_boluses output (may be {})
    settings: user's current AIMI settings dict
    Returns: list of suggestion dicts {feedback, setting, current, suggested,
             logic, evidence:[{label,value,supports}], data_supports: bool}
    """
    hourly = {h["hour"]: h for h in analysis.get("hourly", []) if h.get("mean_mmol") is not None}
    tir = analysis.get("tir", {})
    cv = analysis.get("cv", 0)
    low_pct = tir.get("low", 0) + tir.get("very_low", 0)
    high_pct = tir.get("high", 0) + tir.get("very_high", 0)
    s = settings or {}
    out = []

    def night_stats():
        hrs = [hourly[h] for h in (0, 1, 2, 3, 4, 5, 6) if h in hourly]
        if not hrs:
            return None, None
        avg = sum(h["mean_mmol"] for h in hrs) / len(hrs)
        mn = min(h.get("min_mmol", 99) for h in hrs)
        return round(avg, 1), mn

    def evening_stats():
        hrs = [hourly[h] for h in (17, 18, 19, 20) if h in hourly]
        if not hrs:
            return None
        return round(sum(h["mean_mmol"] for h in hrs) / len(hrs), 1)

    for fid in feedback_ids:
        if fid == "overnight_lows":
            avg, mn = night_stats()
            supports = (mn is not None and mn < 4.0) or low_pct > 4
            ev = []
            if avg is not None:
                ev.append({"label": "Overnight avg (00–07h)", "value": f"{avg} mmol/L",
                           "supports": avg < 6.0})
                ev.append({"label": "Overnight lowest reading", "value": f"{mn} mmol/L",
                           "supports": mn < 4.0})
            ev.append({"label": "Time below 3.9 (whole window)", "value": f"{low_pct:.1f}%",
                       "supports": low_pct > 4})
            out.append({
                "feedback": "I go low overnight",
                "setting": "Overnight basal / LGS threshold",
                "current": f"LGS {s.get('lgs_threshold','?')} mmol/L",
                "suggested": "Raise LGS toward 4.0–4.2 and review overnight basal with your team",
                "logic": ("Overnight lows usually come from too much basal carrying through the night, "
                          "or evening IOB stacking into it. The LGS threshold is the safety floor that "
                          "suspends insulin; if it's set high it suspends late. Lowering it lets the loop "
                          "act sooner, but the root fix for true lows is often the overnight basal rate."),
                "evidence": ev,
                "data_supports": supports,
            })

        elif fid == "overnight_highs":
            avg, mn = night_stats()
            supports = avg is not None and avg > 8.0
            ev = [{"label": "Overnight avg (00–07h)", "value": f"{avg} mmol/L" if avg else "n/a",
                   "supports": bool(avg and avg > 8.0)}]
            out.append({
                "feedback": "I run high overnight",
                "setting": "Overnight basal / ISF fusion max",
                "current": f"ISF fusion max {s.get('isf_fusion_max','?')}",
                "suggested": "Review overnight basal upward with your team; allow ISF fusion max ~1.25",
                "logic": ("Steady overnight highs point to not enough background insulin overnight, or an "
                          "ISF ceiling that stops the loop correcting hard enough. The loop can only "
                          "correct within its ISF bounds — a too-low max factor caps it."),
                "evidence": ev,
                "data_supports": supports,
            })

        elif fid in ("meal_spikes", "meals_stay_high"):
            eve = evening_stats()
            unc = (bolus or {}).get("summary", {}).get("uncorrected_meals", 0)
            worst_hr = (bolus or {}).get("worst_meal_hour")
            supports = high_pct > 12 or unc > 0
            ev = [{"label": "Time high (>10)", "value": f"{high_pct:.1f}%", "supports": high_pct > 12}]
            if unc:
                ev.append({"label": "Meals still high at 4h", "value": str(unc), "supports": True})
            if worst_hr is not None:
                ev.append({"label": "Worst meal hour", "value": f"{worst_hr:02d}:00", "supports": True})
            if eve:
                ev.append({"label": "Evening avg (17–20h)", "value": f"{eve} mmol/L", "supports": eve > 8})
            if fid == "meal_spikes":
                out.append({
                    "feedback": "Meals spike then I crash",
                    "setting": "Meal-bolus timing / carb ratio",
                    "current": f"Carb ratio {profile.get('cr','?')} g/U",
                    "suggested": "Pre-bolus earlier; review carb ratio with your team (spike+crash often = right dose, wrong timing)",
                    "logic": ("A spike followed by a crash usually means the insulin was the right amount "
                              "but arrived too late — it catches up after the carbs have already peaked, "
                              "then overshoots. This is a timing problem the loop can't fully fix after the "
                              "fact; pre-bolusing is the usual lever."),
                    "evidence": ev,
                    "data_supports": supports,
                })
            else:
                out.append({
                    "feedback": "Meals stay high for hours",
                    "setting": "Carb ratio / meal bolus size",
                    "current": f"Carb ratio {profile.get('cr','?')} g/U",
                    "suggested": "Review whether meal insulin is too little for those carbs (carb ratio) with your team",
                    "logic": ("Meals that never come back down suggest the meal insulin was too small for "
                              "the carbs — the loop's later micro-corrections can't catch a big under-dose. "
                              "This points at the carb ratio or the meal announcement rather than a loop knob."),
                    "evidence": ev,
                    "data_supports": supports,
                })

        elif fid == "lows_after_correction":
            la = (bolus or {}).get("summary", {}).get("lows_after_manual", 0)
            supports = la > 0 or low_pct > 4
            ev = [{"label": "Lows within 3h of a manual bolus", "value": str(la), "supports": la > 0},
                  {"label": "Max IOB setting", "value": f"{s.get('max_iob','?')} U", "supports": True}]
            out.append({
                "feedback": "I go low after corrections",
                "setting": "Max IOB / correction aggressiveness",
                "current": f"Max IOB {s.get('max_iob','?')} U",
                "suggested": "Lower Max IOB toward ~15% of TDD; consider more cautious ISF fusion (0.80/1.15)",
                "logic": ("Lows after correcting usually mean corrections stack — insulin from one "
                          "correction is still active when the next is given. A lower Max IOB caps how "
                          "much can pile up, and a tighter ISF range makes each correction gentler."),
                "evidence": ev,
                "data_supports": supports,
            })

        elif fid == "stubborn_highs":
            supports = high_pct > 15
            ev = [{"label": "Time high (>10)", "value": f"{high_pct:.1f}%", "supports": high_pct > 15},
                  {"label": "ISF fusion max", "value": f"{s.get('isf_fusion_max','?')}", "supports": True}]
            out.append({
                "feedback": "Highs are hard to bring down",
                "setting": "ISF fusion max / DynamicISF",
                "current": f"ISF fusion max {s.get('isf_fusion_max','?')}",
                "suggested": "Allow ISF fusion max ~1.25 so the loop can correct harder when resistant",
                "logic": ("If highs linger, the loop may be hitting its ISF ceiling — it isn't allowed to "
                          "make ISF strong enough during resistance. Widening the max factor gives it room. "
                          "Persistent resistance can also mean a basal or site issue worth checking."),
                "evidence": ev,
                "data_supports": supports,
            })

        elif fid == "too_much_variability":
            supports = cv > 36
            ev = [{"label": "Coefficient of variation", "value": f"{cv}%", "supports": cv > 36}]
            out.append({
                "feedback": "My BG swings a lot",
                "setting": "Meal timing / learning pace",
                "current": f"Learning pace {(s.get('learning_pace') or '?').upper()}",
                "suggested": "Stabilise inputs (consistent pre-bolus, logging carbs) before changing loop knobs",
                "logic": ("High variability (CV over 36%) is usually driven by inconsistent meal timing and "
                          "un-logged carbs more than by loop settings. Chasing it with aggressive settings can "
                          "make swings worse. Steadier inputs first, then a slower learning pace to avoid "
                          "the loop chasing noise."),
                "evidence": ev,
                "data_supports": supports,
            })

    return out
