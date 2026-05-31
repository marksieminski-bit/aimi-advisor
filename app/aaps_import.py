"""
Parse an AndroidAPS (AIMI) settings export (UNENCRYPTED JSON) and map the
relevant preference keys into the app's internal settings dict.

AAPS exports look like:
  {
    "version": 2,
    "content": { "<pref_key>": "<value>", ... },        # newer format
    ...
  }
or a flat dict of key->value (older / partial). We handle both, and we also
handle the encrypted format by detecting it and returning a clear error.

Key matching is PATTERN-BASED (case-insensitive substrings) rather than exact,
because preference key names vary between AIMI versions. This keeps the importer
working across builds. Anything we can't confidently map is reported back so the
user can see what was and wasn't imported.
"""
import json
import re
import base64
import hashlib


def _decrypt_aaps(content_b64, salt_hex, password, content_hash=None):
    """
    Replicate AAPS CryptoUtil.decrypt:
      - PBKDF2withHmacSHA1, 50000 iterations, 256-bit key
      - AES-256-GCM, 128-bit tag, 12-byte IV
      - blob layout (base64): [iv_len:1][iv:iv_len][ciphertext+tag]
    Returns plaintext str or raises ValueError with a friendly message.
    """
    from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    try:
        salt = bytes.fromhex(salt_hex)
    except ValueError:
        raise ValueError("bad_salt")

    kdf = PBKDF2HMAC(algorithm=hashes.SHA1(), length=32, salt=salt, iterations=50000)
    key = kdf.derive(password.encode("utf-8"))

    try:
        blob = base64.b64decode(content_b64)
    except Exception:
        raise ValueError("bad_content_b64")

    if len(blob) < 1:
        raise ValueError("empty_content")
    iv_len = blob[0]
    iv = blob[1:1 + iv_len]
    ciphertext = blob[1 + iv_len:]
    if len(iv) != iv_len or not ciphertext:
        raise ValueError("malformed_blob")

    aesgcm = AESGCM(key)
    try:
        plaintext = aesgcm.decrypt(iv, ciphertext, None)  # GCM tag is appended to ciphertext
    except Exception:
        # Almost always a wrong password (GCM auth tag mismatch)
        raise ValueError("wrong_password")

    text = plaintext.decode("utf-8", errors="replace")
    # Optional integrity check against stored content hash
    if content_hash:
        if hashlib.sha256(text.encode("utf-8")).hexdigest() != content_hash:
            raise ValueError("hash_mismatch")
    return text


# Each internal setting maps to:
#   "keys": exact preference key names to try first, in priority order
#           (these are the real AIMI/AAPS keys; matching is case-insensitive but EXACT)
#   "contains": fallback loose substrings (only used if no exact key matched)
#   "min"/"max": sanity bounds — a matched value outside this range is REJECTED
#               (this is what stops timestamps/percentages being read as settings)
SETTING_MAP = {
    "lgs_threshold": {
        "keys": ["OApsAIMILgsThreshold", "lgsThreshold", "hypoGuard"],
        "contains": [["lgs", "threshold"]],
        "min": 3.0, "max": 8.0,      # mmol/L; rejects 99 (a percentage)
    },
    "max_iob": {
        "keys": ["ApsSmbMaxIob", "OApsAIMIMaxIOB", "max_iob_u", "MAX_IOB"],
        "contains": [["max", "iob"]],
        "min": 0.0, "max": 40.0,      # Units; rejects millisecond timestamps
    },
    "max_basal": {
        "keys": ["ApsMaxBasal", "OApsAIMIMaxBasal"],
        "contains": [["max", "basal"]],
        "min": 0.0, "max": 35.0,
    },
    "dynisf_factor": {
        "keys": ["OApsAIMIDynISFAdjust", "DynISF_Adjust", "OApsAIMIDynISFAdjusthyper"],
        "contains": [["dynisf", "adjust"]],
        "min": 50.0, "max": 250.0,    # percent
    },
    "tdd7": {
        "keys": ["OApsAIMITDD7", "key_tdd7", "tdd7"],
        "contains": [["tdd7"]],
        "min": 5.0, "max": 200.0,     # Units/day
    },
    "smb_interval": {
        "keys": ["OApsAIMISMBInterval", "ApsSmbInterval"],
        "contains": [["smb", "interval"]],
        "min": 1.0, "max": 30.0,      # minutes
    },
    "pkpd_initial_dia": {
        "keys": ["OApsAIMIPkpdStateDiaH", "OApsAIMIPkpdInitialDiaH"],
        "contains": [["pkpd", "dia"]],
        "min": 3.0, "max": 12.0,      # hours
    },
    "pkpd_initial_peak": {
        "keys": ["OApsAIMIPkpdStatePeakMin", "OApsAIMIPkpdInitialPeak"],
        "contains": [["pkpd", "peak"]],
        "min": 30.0, "max": 120.0,    # minutes
    },
    "isf_fusion_min": {
        "keys": ["OApsAIMIIsfFusionMinFactor", "PkPd_Fusion_Min"],
        "contains": [["fusion", "min"]],
        "min": 0.3, "max": 1.5,
    },
    "isf_fusion_max": {
        "keys": ["OApsAIMIIsfFusionMaxFactor", "PkPd_Fusion_Max"],
        "contains": [["fusion", "max"]],
        "min": 0.8, "max": 2.5,
    },
    "smb_tail_damping": {
        "keys": ["OApsAIMISmbTailDamping", "OApsAIMIPkpdTailDamping"],
        "contains": [["tail", "damping"]],
        "min": 0.3, "max": 1.0,
    },
    "learning_pace": {
        "keys": ["OApsAIMIPkpdLearningPace", "OApsAIMILearningPace"],
        "contains": [["learning", "pace"]],
    },
    "insulin_type": {
        "keys": ["OApsAIMIPkpdInsulinPreset", "OApsAIMIInsulinPreset"],
        "contains": [["insulin", "preset"]],
    },
    "sensitivity_raises_target": {
        "keys": ["OApsAIMISensitivityRaisesTarget"],
        "contains": [["sensitivity", "raises"]],
    },
    "resistance_lowers_target": {
        "keys": ["OApsAIMIResistanceLowersTarget"],
        "contains": [["resistance", "lowers"]],
    },
}

NUMERIC = {"lgs_threshold", "max_iob", "max_basal", "dynisf_factor", "tdd7",
           "smb_interval", "pkpd_initial_dia", "pkpd_initial_peak",
           "isf_fusion_min", "isf_fusion_max", "smb_tail_damping"}
BOOL = {"sensitivity_raises_target", "resistance_lowers_target"}


def _coerce(setting, raw):
    if raw is None:
        return None
    if setting in BOOL:
        if isinstance(raw, bool):
            return raw
        s = str(raw).strip().lower()
        if s in ("true", "1", "yes", "on"):
            return True
        if s in ("false", "0", "no", "off"):
            return False
        return None
    if setting in NUMERIC:
        try:
            return float(raw)
        except (ValueError, TypeError):
            m = re.search(r"-?\d+(?:\.\d+)?", str(raw))
            return float(m.group()) if m else None
    if setting == "learning_pace":
        s = str(raw).strip().lower()
        if "slow" in s:
            return "slow"
        if "fast" in s:
            return "fast"
        if "normal" in s:
            return "normal"
        return None
    if setting == "insulin_type":
        s = str(raw).strip().lower()
        if "ultra" in s or "fiasp" in s or "lyumjev" in s:
            return "ultrafast"
        if "rapid" in s or "humalog" in s or "novo" in s:
            return "rapid"
        if "standard" in s or "actrapid" in s:
            return "standard"
        return None
    return raw


def _flatten(d, prefix=""):
    """Flatten nested dicts to a single key->value map (keys joined by '.')."""
    out = {}
    if isinstance(d, dict):
        for k, v in d.items():
            if isinstance(v, dict):
                out.update(_flatten(v, f"{prefix}{k}."))
            else:
                out[f"{prefix}{k}"] = v
    return out


def _key_matches(key_lower, token_groups):
    for tokens in token_groups:
        if all(tok in key_lower for tok in tokens):
            return True
    return False


def detect_units(flat):
    """Look for a units preference in the export (mmol vs mg/dl)."""
    for k, v in flat.items():
        if "unit" in k.lower():
            sv = str(v).strip().lower()
            if "mmol" in sv:
                return "mmol"
            if "mg" in sv:
                return "mgdl"
    return None


def parse_aaps_export(raw_text, password=None):
    """
    raw_text: the file contents as a string.
    password: optional master password to decrypt an encrypted export.
    Returns {"ok": bool, "settings": {...}, "matched": {...}, "units": ...,
             "matched_count": int, "error": str|None, "message": str}
    """
    text = (raw_text or "").strip()
    if not text:
        return {"ok": False, "error": "empty_file"}

    try:
        doc = json.loads(text)
    except json.JSONDecodeError:
        return {"ok": False, "error": "not_json",
                "message": "This doesn't look like JSON. Export settings from AAPS "
                           "(Maintenance → Export settings) and use that file."}

    # AAPS encrypted format: security.algorithm == "v1", content is a base64 string.
    security = doc.get("security") if isinstance(doc, dict) else None
    content = doc.get("content") if isinstance(doc, dict) else None
    algorithm = (security or {}).get("algorithm") if isinstance(security, dict) else None

    source = None
    if isinstance(content, str) and algorithm and algorithm != "none":
        # Encrypted — need a password
        if not password:
            return {"ok": False, "error": "password_required",
                    "message": "This export is encrypted. Enter your AAPS master password to decrypt it."}
        salt_hex = (security or {}).get("salt")
        content_hash = (security or {}).get("content_hash")
        if not salt_hex:
            return {"ok": False, "error": "no_salt",
                    "message": "Encrypted file is missing its salt — it may be corrupted or an unsupported format."}
        try:
            decrypted = _decrypt_aaps(content, salt_hex, password, content_hash)
        except ValueError as e:
            msgs = {
                "wrong_password": "Wrong master password — the file couldn't be decrypted. Double-check and try again.",
                "hash_mismatch": "Decrypted but the integrity check failed; the file may be corrupted.",
                "bad_salt": "The file's salt is malformed.",
                "bad_content_b64": "The encrypted content is malformed.",
                "malformed_blob": "The encrypted content is malformed.",
                "empty_content": "The encrypted content is empty.",
            }
            return {"ok": False, "error": str(e), "message": msgs.get(str(e), "Decryption failed.")}
        try:
            source = json.loads(decrypted)
        except json.JSONDecodeError:
            return {"ok": False, "error": "decrypt_not_json",
                    "message": "Decryption produced unreadable data — likely a wrong password."}
    elif isinstance(content, str):
        # content is a string but not flagged encrypted — can't use it
        return {"ok": False, "error": "unexpected_content",
                "message": "Unexpected file format. Try re-exporting from AAPS."}
    else:
        # Unencrypted: content is a dict, or the whole doc is the map
        source = content if isinstance(content, dict) else doc

    flat = _flatten(source)
    if not flat:
        return {"ok": False, "error": "no_keys",
                "message": "No preference keys found in the file."}

    # Build a case-insensitive lookup of the export's keys
    lower_index = {}
    for k in flat:
        # last path segment (after any dotted nesting) is the real pref key
        seg = k.split(".")[-1]
        lower_index.setdefault(seg.lower(), k)
        lower_index.setdefault(k.lower(), k)

    def _in_bounds(setting, val):
        spec = SETTING_MAP[setting]
        if "min" in spec and isinstance(val, (int, float)):
            if val < spec["min"] or val > spec["max"]:
                return False
        return True

    settings = {}
    matched = {}
    rejected = {}
    used_keys = set()

    for setting, spec in SETTING_MAP.items():
        found = None
        # 1) exact key names, in priority order
        for cand in spec.get("keys", []):
            real = lower_index.get(cand.lower())
            if real and real not in used_keys:
                val = _coerce(setting, flat[real])
                if val is not None and _in_bounds(setting, val):
                    found = (real, flat[real], val)
                    break
                elif val is not None:
                    rejected[setting] = {"key": real, "raw": flat[real],
                                         "reason": "out_of_range"}
        # 2) loose fallback ONLY if no exact key matched — still bounded
        if not found:
            for tokens in spec.get("contains", []):
                for k, v in flat.items():
                    if k in used_keys:
                        continue
                    kl = k.split(".")[-1].lower()
                    if all(t in kl for t in tokens):
                        val = _coerce(setting, v)
                        if val is not None and _in_bounds(setting, val):
                            found = (k, v, val)
                            break
                if found:
                    break
        if found:
            real, raw, val = found
            settings[setting] = val
            matched[setting] = {"key": real, "raw": raw, "value": val}
            used_keys.add(real)

    units = detect_units(flat)
    return {
        "ok": True,
        "settings": settings,
        "matched": matched,
        "rejected": rejected,
        "units": units,
        "matched_count": len(matched),
        "total_keys": len(flat),
        "encrypted": bool(algorithm and algorithm != "none"),
        "error": None,
    }
