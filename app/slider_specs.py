"""
Slider/track specifications for the visual settings UI.
Each entry mirrors the range AIMI's own sliders use (from the code's coerceIn bounds
and the settings screens) so the dashboard can position current/recommended markers
on a track grouped like AIMI's settings screens.

type:
  "range"  -> numeric slider with min/max
  "toggle" -> on/off pill
  "choice" -> discrete options (e.g. learning pace, insulin preset)
group: the AIMI screen section it belongs to (display order preserved via GROUP_ORDER)
"""

GROUP_ORDER = [
    "Profile",
    "Main AIMI",
    "Insulin Model",
    "Starting Values",
    "Learning Limits",
    "Peak Governor",
    "ISF Fusion",
    "DynISF Trajectory",
    "SMB Tail",
    "Caps & Guards",
]

# name -> spec
SLIDER_SPECS = {
    # ---- Profile ----
    "ISF (profile)":        {"group": "Profile", "type": "range", "min": 1.0, "max": 10.0, "step": 0.1, "unit": "mmol/U"},
    "Carb ratio":           {"group": "Profile", "type": "range", "min": 3.0, "max": 20.0, "step": 0.1, "unit": "g/U"},
    "BG target":            {"group": "Profile", "type": "range", "min": 4.0, "max": 8.0, "step": 0.1, "unit": "mmol/L"},

    # ---- Main AIMI ----
    "LGS threshold":        {"group": "Main AIMI", "type": "range", "min": 3.3, "max": 5.5, "step": 0.1, "unit": "mmol/L", "lower_better": True},
    "Max IOB":              {"group": "Main AIMI", "type": "range", "min": 0.0, "max": 15.0, "step": 0.5, "unit": "U", "lower_better": True},
    "Max basal":            {"group": "Main AIMI", "type": "range", "min": 0.0, "max": 15.0, "step": 0.1, "unit": "U/h"},
    "DynamicISF factor":    {"group": "Main AIMI", "type": "range", "min": 50, "max": 200, "step": 5, "unit": "%"},
    "TDD7":                 {"group": "Main AIMI", "type": "range", "min": 20, "max": 80, "step": 1, "unit": "U"},
    "SMB interval":         {"group": "Main AIMI", "type": "range", "min": 1, "max": 10, "step": 1, "unit": "min"},
    # (Sensitivity raises target / Resistance lowers target removed from advisor UI)

    # ---- Insulin Model ----
    "Insulin preset":       {"group": "Insulin Model", "type": "choice",
                              "options": ["Ultra-fast (Fiasp / Lyumjev)", "Rapid (Humalog / Novorapid)", "Standard (Actrapid)"]},

    # ---- Starting Values ----
    "Initial DIA":          {"group": "Starting Values", "type": "range", "min": 3.0, "max": 10.0, "step": 0.1, "unit": "h"},
    "Initial Peak":         {"group": "Starting Values", "type": "range", "min": 35, "max": 180, "step": 1, "unit": "min"},

    # ---- Learning Limits ----
    "DIA bounds":           {"group": "Learning Limits", "type": "rangepair", "min": 3.0, "max": 10.0, "step": 0.1, "unit": "h"},
    "Peak bounds":          {"group": "Learning Limits", "type": "rangepair", "min": 35, "max": 240, "step": 1, "unit": "min"},
    "Learning pace":        {"group": "Learning Limits", "type": "choice", "options": ["SLOW", "NORMAL", "FAST"]},

    # ---- Peak Governor ----
    "TAP-G blend weight":   {"group": "Peak Governor", "type": "range", "min": 0.0, "max": 1.0, "step": 0.01, "unit": ""},

    # ---- ISF Fusion ----
    "ISF fusion min factor": {"group": "ISF Fusion", "type": "range", "min": 0.3, "max": 1.0, "step": 0.01, "unit": ""},
    "ISF fusion max factor": {"group": "ISF Fusion", "type": "range", "min": 1.0, "max": 2.0, "step": 0.01, "unit": ""},
    "Max ISF change/tick":   {"group": "ISF Fusion", "type": "range", "min": 0.0, "max": 0.84, "step": 0.01, "unit": ""},

    # ---- DynISF Trajectory ----
    "DynISF trajectory tuning": {"group": "DynISF Trajectory", "type": "toggle"},
    "DynISF shadow only":       {"group": "DynISF Trajectory", "type": "toggle"},
    "DynISF max ISF change/tick": {"group": "DynISF Trajectory", "type": "range", "min": 0.0, "max": 0.3, "step": 0.001, "unit": ""},

    # ---- SMB Tail ----
    "SMB tail damping":     {"group": "SMB Tail", "type": "range", "min": 0.70, "max": 0.92, "step": 0.01, "unit": ""},
    "Tail fraction threshold": {"group": "SMB Tail", "type": "range", "min": 0.0, "max": 0.5, "step": 0.01, "unit": ""},
    "Exercise damping factor": {"group": "SMB Tail", "type": "range", "min": 0.0, "max": 1.0, "step": 0.05, "unit": ""},
    "Late meal/fat damping factor": {"group": "SMB Tail", "type": "range", "min": 0.0, "max": 1.0, "step": 0.05, "unit": ""},

    # ---- Caps & Guards ----
    "Pragmatic relief":     {"group": "Caps & Guards", "type": "toggle"},
    "PKPD relief minimum factor": {"group": "Caps & Guards", "type": "range", "min": 0.5, "max": 1.0, "step": 0.01, "unit": ""},
    "Red Carpet restore threshold": {"group": "Caps & Guards", "type": "range", "min": 0.0, "max": 1.0, "step": 0.05, "unit": ""},
    "IOB surveillance guard": {"group": "Caps & Guards", "type": "toggle"},
    "Priority MaxIOB factor": {"group": "Caps & Guards", "type": "range", "min": 1.0, "max": 1.6, "step": 0.05, "unit": "×"},
}


def get_specs():
    return SLIDER_SPECS


def group_order():
    return GROUP_ORDER
