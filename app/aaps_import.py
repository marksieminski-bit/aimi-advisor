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


# Each internal setting -> list of substring patterns to look for in pref keys.
# First key whose name contains ALL of a pattern group's tokens wins.
SETTING_PATTERNS = {
    "lgs_threshold":        [["lgs"], ["hypo", "threshold"], ["safety", "threshold"]],
    "max_iob":              [["maxiob"], ["max", "iob"]],
    "max_basal":            [["maxbasal"], ["max", "basal"]],
    "dynisf_factor":        [["dynisf", "adjust"], ["dynisfadjust"], ["dynamic", "isf"]],
    "tdd7":                 [["tdd7"], ["tdd", "7"]],
    "smb_interval":         [["smb", "interval"]],
    "sensitivity_raises_target": [["sensitivity", "raises"], ["sens", "target"]],
    "resistance_lowers_target":  [["resistance", "lowers"], ["resist", "target"]],
    "pkpd_initial_dia":     [["pkpd", "dia"], ["initial", "dia"], ["dia", "h"]],
    "pkpd_initial_peak":    [["pkpd", "peak"], ["initial", "peak"]],
    "isf_fusion_min":       [["fusion", "min"], ["isf", "min", "factor"]],
    "isf_fusion_max":       [["fusion", "max"], ["isf", "max", "factor"]],
    "smb_tail_damping":     [["tail", "damping"], ["smb", "tail"]],
    "learning_pace":        [["learning", "pace"], ["pkpd", "learning"]],
    "insulin_type":         [["pkpd", "insulin", "preset"], ["insulin", "preset"]],
    "tap_g_blend":          [["tapg"], ["tap", "blend"], ["learned", "peak", "blend"]],
}

# Settings that should be coerced to a number / bool / choice
NUMERIC = {"lgs_threshold", "max_iob", "max_basal", "dynisf_factor", "tdd7",
           "smb_interval", "pkpd_initial_dia", "pkpd_initial_peak",
           "isf_fusion_min", "isf_fusion_max", "smb_tail_damping", "tap_g_blend"}
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

    settings = {}
    matched = {}
    used_keys = set()
    for setting, groups in SETTING_PATTERNS.items():
        for k, v in flat.items():
            if k in used_keys:
                continue
            if _key_matches(k.lower(), groups):
                val = _coerce(setting, v)
                if val is not None:
                    settings[setting] = val
                    matched[setting] = {"key": k, "raw": v, "value": val}
                    used_keys.add(k)
                    break

    units = detect_units(flat)
    return {
        "ok": True,
        "settings": settings,
        "matched": matched,
        "units": units,
        "matched_count": len(matched),
        "total_keys": len(flat),
        "encrypted": bool(algorithm and algorithm != "none"),
        "error": None,
    }
