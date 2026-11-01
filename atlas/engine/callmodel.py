"""Turn raw network data into one plain dict per call.

Shape of a call dict (used everywhere in the engine):
  method, url, host, origin, path, query [[k, v], ...],
  req_headers {lower-name: value}, req_kind json|form|text|none, req_body,
  status, resp_headers {lower-name: value}, resp_kind json|text|none, resp_body,
  resource_type, duration_ms, ts
"""
from __future__ import annotations

import json
import time
from urllib.parse import parse_qsl, urlsplit

MAX_TEXT = 20_000
MAX_ARRAY = 25


def _trim(obj, depth: int = 0):
    """Keep stored bodies small: cap array length and nesting."""
    if depth > 12:
        return "…"
    if isinstance(obj, list):
        return [_trim(x, depth + 1) for x in obj[:MAX_ARRAY]]
    if isinstance(obj, dict):
        return {str(k): _trim(v, depth + 1) for k, v in obj.items()}
    if isinstance(obj, str) and len(obj) > 2000:
        return obj[:2000] + "…"
    return obj


def parse_body(content_type: str | None, data) -> tuple[str, object]:
    """Return (kind, body)."""
    if data is None or data == b"" or data == "":
        return "none", None
    if isinstance(data, (bytes, bytearray)):
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            return "text", f"<binary {len(data)} bytes>"
    else:
        text = str(data)
    ctype = (content_type or "").lower()
    if "x-www-form-urlencoded" in ctype:
        return "form", {k: v for k, v in parse_qsl(text, keep_blank_values=True)}
    stripped = text.strip()
    if "json" in ctype or stripped[:1] in ("{", "["):
        try:
            return "json", _trim(json.loads(stripped))
        except ValueError:
            pass
    return "text", text[:MAX_TEXT]


def build_call(*, method: str, url: str, req_headers: dict | None, req_data, status: int | None,
               resp_headers: dict | None, resp_data, resource_type: str | None = "fetch",
               duration_ms: float | None = None, ts: float | None = None) -> dict:
    parts = urlsplit(url)
    req_headers = {str(k).lower(): str(v) for k, v in (req_headers or {}).items()}
    resp_headers = {str(k).lower(): str(v) for k, v in (resp_headers or {}).items()}
    req_kind, req_body = parse_body(req_headers.get("content-type"), req_data)
    resp_kind, resp_body = parse_body(resp_headers.get("content-type"), resp_data)
    if resp_kind == "form":  # responses are never form-encoded in practice
        resp_kind, resp_body = "text", str(resp_data)[:MAX_TEXT]
    return {
        "method": (method or "GET").upper(),
        "url": url,
        "host": (parts.hostname or "").lower(),
        "origin": f"{parts.scheme}://{parts.netloc}",
        "path": parts.path or "/",
        "query": [[k, v] for k, v in parse_qsl(parts.query, keep_blank_values=True)],
        "req_headers": req_headers,
        "req_kind": req_kind,
        "req_body": req_body,
        "status": status,
        "resp_headers": resp_headers,
        "resp_kind": resp_kind,
        "resp_body": resp_body,
        "resource_type": resource_type,
        "duration_ms": round(duration_ms, 1) if duration_ms is not None else None,
        "ts": ts if ts is not None else time.time(),
    }


def path_segments(path: str) -> list[str]:
    return [s for s in (path or "/").split("/") if s != ""]


def parse_cookie_header(value: str) -> dict[str, str]:
    out = {}
    for part in (value or "").split(";"):
        if "=" in part:
            k, v = part.split("=", 1)
            out[k.strip()] = v.strip()
    return out


def parse_set_cookie(value: str) -> dict[str, str]:
    """Playwright joins multiple Set-Cookie headers with newlines."""
    out = {}
    for line in (value or "").split("\n"):
        first = line.split(";", 1)[0]
        if "=" in first:
            k, v = first.split("=", 1)
            out[k.strip()] = v.strip()
    return out
