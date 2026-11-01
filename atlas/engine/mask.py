"""Mask secrets and personal data before anything is stored, shown or exported.

Masked values become markers like "<masked:email>" so later stages know what kind
of value was there (and can swap in an env var or a unique test value).
"""
from __future__ import annotations

import copy
import re

from .callmodel import parse_cookie_header, parse_set_cookie

MARKER_RE = re.compile(r"^<masked:([a-z_]+)>$")

SECRET_HEADERS = {
    "authorization", "proxy-authorization", "cookie", "set-cookie", "x-api-key", "api-key", "apikey",
    "x-auth-token", "x-access-token", "x-refresh-token", "x-csrf-token", "x-xsrf-token",
    "x-amz-security-token", "x-session-token", "x-session-id", "x-client-secret",
}

_SECRET_KEY_PARTS = ("password", "passwd", "secret", "token", "apikey", "otp", "cvv", "cvc",
                     "cardnumber", "aadhaar", "aadhar", "ssn", "privatekey", "authorization",
                     "pincode_secret", "mpin", "tpin", "accountnumber", "ifsc_secret")
_SECRET_KEY_EXACT = {"pin", "pan", "pwd", "pass", "auth", "sig", "signature", "key"}
_EMAIL_KEYS = ("email", "mail")
_PHONE_KEYS = ("phone", "mobile", "msisdn", "contactnumber")

JWT_RE = re.compile(r"^eyJ[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]*$")
EMAIL_RE = re.compile(r"^[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}$")
EMAIL_ANY_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
CARD_RE = re.compile(r"^\d{13,19}$")
PAN_RE = re.compile(r"^[A-Z]{5}[0-9]{4}[A-Z]$")
PHONE_RE = re.compile(r"^\+?\d[\d\s-]{9,14}$")
BEARER_RE = re.compile(r"^(bearer|basic|token|jwt)\s+(.+)$", re.I)


def _norm_key(key) -> str:
    return re.sub(r"[^a-z0-9]", "", str(key).lower())


def key_kind(key) -> str | None:
    """Kind of secret implied by a field name, or None."""
    if key is None or isinstance(key, int):
        return None
    k = _norm_key(key)
    if not k:
        return None
    if k in _SECRET_KEY_EXACT or any(p in k for p in _SECRET_KEY_PARTS):
        if "token" in k and k.endswith("type"):  # tokenType: "Bearer" is not a secret
            return None
        if "expires" in k or "expiry" in k or k.endswith("ttl"):
            return None
        return "secret"
    if any(p in k for p in _EMAIL_KEYS):
        return "email"
    if any(p in k for p in _PHONE_KEYS):
        return "phone"
    return None


def _luhn_ok(digits: str) -> bool:
    total, alt = 0, False
    for ch in reversed(digits):
        d = int(ch)
        if alt:
            d *= 2
            if d > 9:
                d -= 9
        total += d
        alt = not alt
    return total % 10 == 0


def value_kind(value) -> str | None:
    """Kind of sensitive value recognised from the value itself, or None."""
    if not isinstance(value, str):
        if isinstance(value, int) and not isinstance(value, bool):
            s = str(value)
            if CARD_RE.match(s) and _luhn_ok(s):
                return "card"
        return None
    v = value.strip()
    if MARKER_RE.match(v):
        return None
    if JWT_RE.match(v):
        return "jwt"
    if EMAIL_RE.match(v):
        return "email"
    if CARD_RE.match(v) and _luhn_ok(v):
        return "card"
    if PAN_RE.match(v):
        return "pan"
    return None


def marker(kind: str) -> str:
    return f"<masked:{kind}>"


def is_masked(value) -> bool:
    return isinstance(value, str) and bool(MARKER_RE.match(value))


def masked_kind(value) -> str | None:
    if isinstance(value, str):
        m = MARKER_RE.match(value)
        if m:
            return m.group(1)
    return None


def mask_value(value, key=None):
    """Mask a single scalar given its field name."""
    if value is None or isinstance(value, bool):
        return value
    kk = key_kind(key)
    if kk == "secret":
        return marker("secret") if value != "" else value
    vk = value_kind(value)
    if vk:
        return marker(vk)
    if kk == "email" and isinstance(value, str) and "@" in value:
        return marker("email")
    if kk == "phone" and value != "" and PHONE_RE.match(str(value)):
        return marker("phone")
    return value


def mask_tree(obj, key=None):
    if isinstance(obj, dict):
        return {k: mask_tree(v, k) for k, v in obj.items()}
    if isinstance(obj, list):
        return [mask_tree(v, key) for v in obj]
    return mask_value(obj, key)


def mask_text(text: str) -> str:
    """Best-effort masking inside free text bodies."""
    if not isinstance(text, str):
        return text
    text = re.sub(r"eyJ[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]*", marker("jwt"), text)
    text = EMAIL_ANY_RE.sub(marker("email"), text)
    text = re.sub(r"\b\d{13,19}\b", lambda m: marker("card") if _luhn_ok(m.group(0)) else m.group(0), text)
    return text


def mask_header(name: str, value: str) -> str:
    name = name.lower()
    if name == "cookie":
        return "; ".join(f"{k}={marker('secret')}" for k in parse_cookie_header(value))
    if name == "set-cookie":
        return "\n".join(f"{k}={marker('secret')}" for k in parse_set_cookie(value))
    if name in ("authorization", "proxy-authorization"):
        m = BEARER_RE.match(value.strip())
        if m:
            return f"{m.group(1)} {marker('secret')}"
        return marker("secret")
    if name in SECRET_HEADERS or key_kind(name) == "secret":
        return marker("secret")
    vk = value_kind(value)
    return marker(vk) if vk else value


def mask_url_path(path: str) -> str:
    segs = path.split("/")
    out = []
    for s in segs:
        vk = value_kind(s)
        out.append(marker(vk) if vk else s)
    return "/".join(out)


def mask_call(call: dict) -> dict:
    """Return a masked deep copy of a call dict."""
    c = copy.deepcopy(call)
    c["path"] = mask_url_path(c["path"])
    c["query"] = [[k, mask_value(v, k)] for k, v in c.get("query", [])]
    q = "&".join(f"{k}={v}" for k, v in c["query"])
    c["url"] = f"{c['origin']}{c['path']}" + (f"?{q}" if q else "")
    c["req_headers"] = {k: mask_header(k, v) for k, v in c.get("req_headers", {}).items()}
    c["resp_headers"] = {k: mask_header(k, v) for k, v in c.get("resp_headers", {}).items()}
    for side in ("req", "resp"):
        kind, body = c.get(f"{side}_kind"), c.get(f"{side}_body")
        if kind in ("json", "form"):
            c[f"{side}_body"] = mask_tree(body)
        elif kind == "text":
            c[f"{side}_body"] = mask_text(body)
    return c
