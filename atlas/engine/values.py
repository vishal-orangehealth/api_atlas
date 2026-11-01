"""Fingerprint the values inside a call so dependencies can be found later
without ever storing the raw values.

Each fingerprint is {"loc": location, "h": hash, "weak": bool, "idk": bool}
  loc  = {"side": "req"|"resp", "part": "body"|"query"|"header"|"cookie"|"path", "path": [...], "prefix": "Bearer "?}
  h    = salted sha256 of the canonical value (salt lives only in memory)
  weak = short value (e.g. id 5) - only linked when both ends look like ids
  idk  = the field name looks like an identifier
"""
from __future__ import annotations

import hashlib
import re

from .callmodel import parse_cookie_header, parse_set_cookie, path_segments
from .mask import BEARER_RE

ID_KEY_RE = re.compile(r"(^id$|_id$|Id$|ID$|^uuid$|_uuid$|Uuid$|^slug$|^key$|^code$|Code$|_code$|^ref$|Ref$)")

STOPWORDS = {
    "true", "false", "null", "none", "success", "ok", "error", "failed", "failure", "pending",
    "active", "inactive", "enabled", "disabled", "yes", "no", "application/json", "json",
    "bearer", "basic", "get", "post", "put", "patch", "delete", "default", "en-us", "en-in",
    "undefined", "string", "user", "admin", "card", "cash", "upi", "draft", "open", "closed",
}

IGNORED_REQ_HEADERS = {
    "host", "content-length", "connection", "accept-encoding", "user-agent", "accept", "accept-language",
    "origin", "referer", "pragma", "cache-control", "priority", "dnt", "upgrade-insecure-requests",
    "if-none-match", "if-modified-since", "content-type", "te", "cookie",
}
IGNORED_RESP_HEADERS = {
    "content-length", "content-type", "date", "server", "connection", "keep-alive", "vary", "etag",
    "cache-control", "expires", "last-modified", "transfer-encoding", "content-encoding",
    "access-control-allow-origin", "access-control-allow-credentials", "access-control-allow-headers",
    "access-control-allow-methods", "access-control-expose-headers", "strict-transport-security",
    "x-content-type-options", "x-frame-options", "x-xss-protection", "referrer-policy", "age", "via",
    "alt-svc", "x-powered-by", "report-to", "nel", "set-cookie", "content-security-policy",
}

MAX_LEAVES = 3000


def is_id_key(key) -> bool:
    return isinstance(key, str) and bool(ID_KEY_RE.search(key))


def canonical(value, key=None) -> tuple[str, bool] | None:
    """Return (canonical string, weak) if the value is a usable link candidate."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, float):
        if not value.is_integer():
            return None
        value = int(value)
    if isinstance(value, int):
        s = str(value)
        if len(s) >= 4:
            return s, False
        return (s, True) if value >= 1 else None
    if isinstance(value, str):
        s = value.strip()
        if not s or len(s) > 4000 or s.startswith("<masked:"):
            return None
        if s.lower() in STOPWORDS:
            return None
        if s.isdigit():
            n = int(s)
            if len(s) >= 4:
                return str(n) if not s.startswith("0") else s, False
            return (str(n), True) if n >= 1 else None
        if len(s) < 4:
            return (s, True) if is_id_key(key) else None
        return s, False
    return None


def _leaves(obj, path, out, key=None):
    if len(out) >= MAX_LEAVES:
        return
    if isinstance(obj, dict):
        for k, v in obj.items():
            _leaves(v, path + [k], out, k)
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            _leaves(v, path + [i], out, key)
    else:
        out.append((path, obj, key))


def leaves(obj) -> list[tuple[list, object, object]]:
    out: list = []
    _leaves(obj, [], out)
    return out


def _fp(salt: bytes, loc: dict, value, key) -> dict | None:
    c = canonical(value, key)
    if c is None:
        return None
    s, weak = c
    h = hashlib.sha256(salt + s.encode("utf-8")).hexdigest()[:20]
    return {"loc": loc, "h": h, "weak": weak, "idk": is_id_key(key)}


def request_fingerprints(call: dict, salt: bytes) -> list[dict]:
    fps: list[dict] = []

    def add(loc, value, key):
        fp = _fp(salt, loc, value, key)
        if fp:
            fps.append(fp)

    segs = path_segments(call["path"])
    for i, seg in enumerate(segs):
        prev = segs[i - 1] if i > 0 else None
        # a path segment after a collection name behaves like an id
        add({"side": "req", "part": "path", "path": [i]}, seg, "id" if prev else None)
    for k, v in call.get("query", []):
        add({"side": "req", "part": "query", "path": [k]}, v, k)
    for name, value in call.get("req_headers", {}).items():
        if name == "cookie":
            for ck, cv in parse_cookie_header(value).items():
                add({"side": "req", "part": "cookie", "path": [ck]}, cv, ck)
            continue
        if name in IGNORED_REQ_HEADERS or name.startswith("sec-"):
            continue
        m = BEARER_RE.match(value.strip())
        if m:
            add({"side": "req", "part": "header", "path": [name], "prefix": m.group(1) + " "}, m.group(2), name)
        else:
            add({"side": "req", "part": "header", "path": [name]}, value, name)
    if call.get("req_kind") in ("json", "form"):
        for path, value, key in leaves(call.get("req_body")):
            add({"side": "req", "part": "body", "path": path}, value, key)
    return fps


def response_fingerprints(call: dict, salt: bytes) -> list[dict]:
    fps: list[dict] = []

    def add(loc, value, key):
        fp = _fp(salt, loc, value, key)
        if fp:
            fps.append(fp)

    for name, value in call.get("resp_headers", {}).items():
        if name == "set-cookie":
            for ck, cv in parse_set_cookie(value).items():
                add({"side": "resp", "part": "cookie", "path": [ck]}, cv, ck)
            continue
        if name in IGNORED_RESP_HEADERS:
            continue
        add({"side": "resp", "part": "header", "path": [name]}, value, name)
    if call.get("resp_kind") == "json":
        for path, value, key in leaves(call.get("resp_body")):
            add({"side": "resp", "part": "body", "path": path}, value, key)
    return fps


def fingerprints(call: dict, salt: bytes) -> list[dict]:
    return request_fingerprints(call, salt) + response_fingerprints(call, salt)
