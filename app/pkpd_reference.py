"""
Human-readable reference for every PK/PD setting, grounded in the AIMI source.
Keyed by the same `name` strings the engine emits in pkpd_recommendations(),
plus extra entries for settings that aren't in the recommendation table but
appear in the app's glossary.

Each entry: short (one-line tooltip) + long (full explanation) + effect_up/effect_down
where a numeric direction makes sense.
"""

PKPD_REFERENCE = {
    "Insulin preset": {
        "group": "Insulin Model",
        "location": "Preferences → OpenAPS AIMI → PK/PD insulin model → Insulin preset",
        "short": "Master switch — sets all DIA/peak bounds at once for your insulin.",
        "long": ("Selecting an insulin calls applyPkpdInsulinPreset(), which overwrites every "
                 "bound and starting value to match that drug. Ultra-fast = DIA 5–8h, peak 35–95min, "
                 "init DIA 6h, init peak 55min. Change this FIRST, then fine-tune — picking the wrong "
                 "insulin means the learner works inside limits designed for a different drug."),
        "code": "PkpdPresetProfiles.applyPkpdInsulinPreset()",
    },
    "Initial DIA": {
        "group": "Starting Values",
        "location": "Preferences → OpenAPS AIMI → PK/PD insulin model → Starting DIA before learning (h)",
        "short": "The DIA the learner begins from before it adapts.",
        "long": ("The estimator walks away from this starting value toward your true learned DIA. "
                 "Set too high (e.g. 7h on ultra-fast), AIMI initially thinks insulin lasts longer "
                 "than it does, sees more 'active IOB', and under-doses corrections until learning "
                 "catches up. Every restart reintroduces that gap, so match your preset (6.0h ultra-fast)."),
        "effect_up": "AIMI assumes insulin lasts longer → less aggressive corrections, more IOB on board.",
        "effect_down": "AIMI assumes insulin clears faster → more willing to add insulin.",
        "code": "PkPdParams initial / PkpdPresetProfiles.kt",
    },
    "Initial Peak": {
        "group": "Starting Values",
        "location": "Preferences → OpenAPS AIMI → PK/PD insulin model → Starting peak time before learning (min)",
        "short": "The peak-action time the learner begins from.",
        "long": ("Where AIMI initially expects insulin to be working hardest. Ultra-fast peaks ~55min, "
                 "rapid ~75min. A wrong starting peak shifts SMB timing until the learner converges."),
        "effect_up": "Expects peak later → delays when it leans on insulin already given.",
        "effect_down": "Expects peak earlier → reacts to dosed insulin sooner.",
        "code": "PkPdParams initial",
    },
    "DIA bounds": {
        "group": "Learning Limits",
        "location": "Preferences → OpenAPS AIMI → PK/PD insulin model → Learning anchor DIA / DIA limits",
        "short": "Hard limits the learned DIA can never exceed.",
        "long": ("The estimator clamps every update with coerceIn(diaMinH, diaMaxH). No matter what the "
                 "data suggests, learned DIA stays inside these. Tighter bounds = safer but less freedom "
                 "to adapt to unusual physiology."),
        "code": "PkpdLearningBounds.kt / AdaptivePkPdEstimator.update()",
    },
    "Peak bounds": {
        "group": "Learning Limits",
        "location": "Preferences → OpenAPS AIMI → PK/PD insulin model → Learning anchor peak / peak limits",
        "short": "Hard limits for the learned peak time.",
        "long": ("Same idea as DIA bounds but for peak. coerceIn(peakMinMin, peakMinMax) is applied to "
                 "every learned peak update."),
        "code": "PkpdLearningBounds.kt",
    },
    "TAP-G blend weight": {
        "group": "Peak Governor",
        "location": "Preferences → OpenAPS AIMI → PK/PD insulin model → Peak governor blend",
        "short": "How much the loop trusts the learned peak vs your profile peak.",
        "long": ("From TapPeakGovernor.resolve(): blended = anchor×(1−w) + learned×w. At 0.55 the "
                 "effective peak leans toward what's been learned. Example: anchor 55min, learned 69min, "
                 "w=0.55 → effective ≈63min. Toward 0 trusts your profile; toward 1 trusts learning."),
        "effect_up": "Loop uses the learned peak more heavily.",
        "effect_down": "Loop sticks closer to your profile peak.",
        "code": "TapPeakGovernor.resolve()",
    },
    "ISF fusion min factor": {
        "group": "ISF Fusion",
        "location": "Preferences → OpenAPS AIMI → PK/PD insulin model → ISF fusion minimum factor",
        "short": "Floor on fused ISF — how STRONG corrections can get.",
        "long": ("minSafeIsf = min(profile, tdd×minFactor), tightened ×0.8 during a rise. A lower number "
                 "allows a more sensitive ISF (smaller ISF = stronger correction = bigger SMBs on rises). "
                 "Clamped to 0.3–1.0 in code."),
        "effect_up": "Limits correction strength → gentler, safer, but highs persist longer.",
        "effect_down": "Allows stronger corrections → bigger SMBs, more low risk.",
        "code": "IsfFusion.fused()",
    },
    "ISF fusion max factor": {
        "group": "ISF Fusion",
        "location": "Preferences → OpenAPS AIMI → PK/PD insulin model → ISF fusion maximum factor",
        "short": "Ceiling on fused ISF — how WEAK ISF can get when resistant.",
        "long": ("maxSafeIsf = tdd × maxFactor × 1.5. Limits how much ISF can loosen during genuine "
                 "resistance (illness, big meals). Higher gives AIMI more room to back off when resistant; "
                 "lower keeps it correcting harder. Clamped to 1.0–2.0 in code."),
        "effect_up": "More room to loosen ISF when resistant → helps persistent post-meal/evening highs.",
        "effect_down": "Keeps ISF tighter → corrects harder even when resistant (use if lows dominate).",
        "code": "IsfFusion.fused()",
    },
    "Max ISF change/tick": {
        "group": "ISF Fusion",
        "location": "Preferences → OpenAPS AIMI → Dynamic ISF → Max ISF change per cycle",
        "short": "How fast fused ISF can move per 5-min loop.",
        "long": ("maxUp = prev×(1+tick), maxDown = prev×(0.85−tick). Lower = smooth, stable ISF; higher = "
                 "responsive but jumpy. Clamped to 0–0.84."),
        "effect_up": "ISF can swing faster loop-to-loop — responsive but twitchy.",
        "effect_down": "ISF changes slowly and smoothly — stable.",
        "code": "IsfFusion.normalized()",
    },
    "SMB tail damping": {
        "group": "SMB Tail",
        "location": "Preferences → OpenAPS AIMI → SMB → Late insulin action (SMB)",
        "short": "Reduces SMBs when most insulin has already acted (the tail).",
        "long": ("Presets: cautious 0.92, neutral 0.85, permissive 0.70. computeTailMultiplier() = "
                 "base + (1−base)×relief, modulated by how fresh/active insulin is. The cautious setting "
                 "is paired with stronger tail restraint → fewer late-tail SMBs → fewer late lows."),
        "effect_up": "More cautious — preserves restraint in the tail, fewer late drops.",
        "effect_down": "More permissive — keeps dosing in the tail, better coverage, more late-low risk.",
        "code": "PkpdSmbTailDamping.kt / SmbDamping.computeTailMultiplier()",
    },
    "Learning pace": {
        "group": "Learning Limits",
        "location": "Preferences → OpenAPS AIMI → PK/PD insulin model → Learning speed",
        "short": "How fast DIA/peak are allowed to change per day.",
        "long": ("Normal = 0.5h DIA / 5min peak per day (PkpdLearningBounds). Slow is below that "
                 "(~0.2h/day), Fast above. coerceMaxDiaChangePerDayH() force-caps anything above 1.2h/day "
                 "back to 0.5 as a safety clamp. Slow = stable, good while dialing in; Normal = adapts in "
                 "a few days; Fast = reacts quickly but can chase noise."),
        "code": "PkpdLearningBounds.kt / PkpdSettingsSupport.PkpdLearningPace",
    },
    "Pragmatic relief": {
        "group": "Caps & Guards",
        "location": "Preferences → OpenAPS AIMI → SMB → Pragmatic relief",
        "short": "Stops SMB intent being over-reduced in clear meal/high-rise contexts.",
        "long": ("Keeps the safety layers from making AIMI too timid when you clearly need insulin. "
                 "Min factor 0.75 means relief can't scale an SMB below 75% in a relief context. Keep ON "
                 "unless you see recurrent over-corrections despite the hard caps."),
        "code": "AimiPkpdPragmaticRelief",
    },
    "IOB surveillance guard": {
        "group": "Caps & Guards",
        "location": "Preferences → OpenAPS AIMI → Safety → IOB surveillance guard",
        "short": "Anti-stacking: caps SMB when BG is high but insulin is already working.",
        "long": ("From InsulinStackingStance: when BG is above target WITH meaningful IOB, the trend has "
                 "flattened, and predictions point down, SMB is capped and temp basal is favoured so onboard "
                 "insulin can act. A meal-rise bypass (Δ≥2.0 or short-avg≥2.5 mg/dL/5m) keeps it from blocking "
                 "real meal spikes. Key protection — keep ON, especially with a high Max IOB."),
        "code": "InsulinStackingStance.kt",
    },
    # Extra glossary-only entries (not in the rec table but in the settings screens)
    "Enable adaptive PK/PD": {
        "group": "Insulin Model",
        "location": "Preferences → OpenAPS AIMI → PK/PD insulin model → Enable adaptive PK/PD",
        "short": "Turns the whole learning system on.",
        "long": ("When on, AdaptivePkPdEstimator.update() runs each loop, comparing predicted vs observed "
                 "insulin action and nudging DIA/peak toward reality within your bounds. Off freezes the "
                 "model at your profile values — only do that if advised or to isolate a problem."),
        "code": "AdaptivePkPdEstimator.update()",
    },
    "Correction aggressiveness": {
        "group": "Insulin Model",
        "location": "Preferences → OpenAPS AIMI → SMB → Correction aggressiveness",
        "short": "Simple-tab slider that moves the ISF fusion min/max factors together.",
        "long": ("Maps via PkpdCorrectionPrudence: cautious ≈0.85/1.10, neutral 0.75/1.25, aggressive "
                 "≈0.65/1.40. More aggressive lets AIMI tighten ISF more on rises (bigger corrections, more "
                 "low risk); more cautious means gentler corrections (highs linger)."),
        "code": "PkpdCorrectionPrudence.kt",
    },
    "Late insulin action (SMB)": {
        "group": "Insulin Model",
        "location": "Preferences → OpenAPS AIMI → SMB → Late insulin action (SMB)",
        "short": "Simple-tab slider behind SMB tail damping.",
        "long": ("More cautious (→0.92) strongly reduces SMBs once insulin is mostly finished, protecting "
                 "against late drops. Allow more (→0.70) keeps delivering in the tail — better coverage but "
                 "more late-low risk."),
        "code": "PkpdSmbTailDamping.kt",
    },
    "DynISF trajectory tuning": {
        "group": "DynISF Trajectory",
        "location": "Preferences → OpenAPS AIMI → Dynamic ISF → Trajectory tuning",
        "short": "Uses short-horizon CGM geometry to nudge ISF (needs Dynamic Sensitivity).",
        "long": ("Uses the same deltas/parabolic-projection/acceleration signals as AutoISF, only when "
                 "quality gates pass. ISF tightens slightly on strong rises, loosens on strong falls, within "
                 "the per-tick cap. Off by default."),
        "code": "DetermineBasalAIMI2 (trajectory tuning)",
    },
    "DynISF shadow only": {
        "group": "DynISF Trajectory",
        "location": "Preferences → OpenAPS AIMI → Dynamic ISF → Shadow mode only",
        "short": "Logs what trajectory tuning WOULD do without applying it.",
        "long": ("Lets you gather evidence in the logs that the signal is trustworthy before letting it act. "
                 "Run shadow ON for a week, review logs, then turn shadow OFF to let it act."),
        "code": "DetermineBasalAIMI2 (shadow mode)",
    },
    "DynISF max ISF change/tick": {
        "group": "DynISF Trajectory",
        "location": "Preferences → OpenAPS AIMI → Dynamic ISF → Max ISF change per cycle",
        "short": "Caps trajectory's relative ISF adjustment (≈10% at 0.098).",
        "long": ("0.098 ≈ at most ~10% tighter ISF on a strong rise or ~10% looser on a strong fall. Lower = "
                 "safer/smaller benefit; higher = bigger trajectory influence and more risk."),
        "code": "DetermineBasalAIMI2",
    },
    "Tail fraction threshold": {
        "group": "SMB Tail",
        "location": "Preferences → OpenAPS AIMI → SMB → Tail fraction threshold",
        "short": "What fraction of remaining activity counts as the 'tail'.",
        "long": ("Default 0.25. Lower (0.15) means only the very end is treated as tail (damping applies "
                 "later); higher (0.35) means more of the curve is tail (damping applies earlier, cutting "
                 "SMBs sooner). Used in PkpdAbsorptionGuard via tailFraction."),
        "code": "PkpdAbsorptionGuard.kt",
    },
    "Exercise damping factor": {
        "group": "SMB Tail",
        "location": "Preferences → OpenAPS AIMI → Activity → Exercise damping factor",
        "short": "Multiplies SMBs when exercise is detected (0.60 = cut to 60%).",
        "long": ("out *= policy.postExerciseDamping. Lower = stronger exercise protection (fewer SMBs when "
                 "active); higher = less protection."),
        "code": "SmbDamping.damp()",
    },
    "Late meal/fat damping factor": {
        "group": "SMB Tail",
        "location": "Preferences → OpenAPS AIMI → Meals → Late meal/fat damping",
        "short": "Damps SMBs in the late post-meal (fat/protein) window.",
        "long": ("Default 0.70. Several hours post-meal, SMBs are damped to this fraction to avoid over-dosing "
                 "a slow tail. Lower = more restraint on late SMBs; higher = more willing to dose the late rise."),
        "code": "SmbDamping.dampWithAudit()",
    },
    "Red Carpet restore threshold": {
        "group": "Caps & Guards",
        "location": "Preferences → OpenAPS AIMI → Advanced → Red Carpet restore threshold",
        "short": "When AIMI re-engages aggressive SMB after a low.",
        "long": ("Default 0.75. BG must recover to 75% of the restore condition before normal dosing resumes — "
                 "prevents slamming insulin right after a hypo."),
        "code": "AimiRedCarpetRestoreThreshold",
    },
    "Priority MaxIOB factor": {
        "group": "Caps & Guards",
        "location": "Preferences → OpenAPS AIMI → Safety → Priority Max IOB factor",
        "short": "How far AIMI may exceed Max IOB in priority meal contexts.",
        "long": ("Effective ceiling = MaxIOB × factor + extra. With factor 1.20 + 2.0U extra and MaxIOB 11, "
                 "the real ceiling is 15.2U; with MaxIOB 6 it's 9.2U. This is why lowering Max IOB matters so "
                 "much — these multipliers compound on top of it."),
        "code": "OApsAIMIPriorityMaxIobFactor / OApsAIMIPriorityMaxIobExtraU",
    },
}


def get_reference():
    return PKPD_REFERENCE
