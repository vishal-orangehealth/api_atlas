"""Merge calls into endpoints: /users/123/orders/9f8a... -> /users/{user_id}/orders/{order_id}."""
from __future__ import annotations

import re

from .callmodel import path_segments
from .mask import is_masked

_PARAM_RES = [
    re.compile(r"^\d+$"),
    re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"),
    re.compile(r"^[0-9a-fA-F]{16,}$"),
    re.compile(r"^(?=.*\d)(?=.*[A-Za-z])[A-Za-z0-9_-]{20,}$"),
    re.compile(r"^\d{4}-\d{2}-\d{2}"),
    re.compile(r"^[A-Z]{2,6}[-_]?\d{3,}$"),          # ORD-10023, TXN123456
]


def looks_like_param(seg: str) -> bool:
    return is_masked(seg) or any(r.match(seg) for r in _PARAM_RES)


def _slugish(seg: str) -> bool:
    return bool(re.search(r"\d", seg)) or ("-" in seg and len(seg) > 8) or len(seg) > 24


def singular(word: str) -> str:
    w = re.sub(r"[^a-zA-Z0-9]", "_", word).strip("_").lower() or "id"
    if w.endswith("ies") and len(w) > 4:
        return w[:-3] + "y"
    if w.endswith("ses") and len(w) > 4:
        return w[:-2]
    if w.endswith("s") and not w.endswith("ss") and len(w) > 3:
        return w[:-1]
    return w


def _param_name(prev: str | None, used: set[str]) -> str:
    base = f"{singular(prev)}_id" if prev and not prev.startswith("{") else "id"
    name, n = base, 2
    while name in used:
        name, n = f"{base}_{n}", n + 1
    used.add(name)
    return name


def build_templates(calls: list[dict], linked_path_segments: set[tuple[int, int]]) -> dict[int, dict]:
    """Return {call_id: {"key", "template", "params": [{"name", "index"}]}}."""
    shapes: dict[int, list] = {}
    for c in calls:
        segs = path_segments(c["path"])
        shapes[c["id"]] = [
            None if (looks_like_param(s) or (c["id"], i) in linked_path_segments) else s
            for i, s in enumerate(segs)
        ]

    # second pass: a segment that varies between otherwise identical paths is a parameter
    groups: dict[tuple, list[dict]] = {}
    for c in calls:
        groups.setdefault((c["method"], c["host"], len(shapes[c["id"]])), []).append(c)
    for (_m, _h, n), members in groups.items():
        if len(members) < 2:
            continue
        for p in range(n):
            buckets: dict[tuple, set] = {}
            for c in members:
                shape = shapes[c["id"]]
                if shape[p] is None:
                    continue
                key = tuple("*" if i == p else (x if x is not None else "{}") for i, x in enumerate(shape))
                buckets.setdefault(key, set()).add(shape[p])
            for key, values in buckets.items():
                if len(values) >= 2 and all(_slugish(v) for v in values):
                    for c in members:
                        shape = shapes[c["id"]]
                        if shape[p] in values:
                            k2 = tuple("*" if i == p else (x if x is not None else "{}") for i, x in enumerate(shape))
                            if k2 == key:
                                shape[p] = None

    out: dict[int, dict] = {}
    for c in calls:
        segs = path_segments(c["path"])
        shape = shapes[c["id"]]
        used: set[str] = set()
        parts, params = [], []
        for i, s in enumerate(shape):
            if s is None:
                prev = shape[i - 1] if i > 0 else None
                name = _param_name(prev, used)
                parts.append("{" + name + "}")
                params.append({"name": name, "index": i, "example": segs[i]})
            else:
                parts.append(s)
        template = "/" + "/".join(parts) if parts else "/"
        if c["path"].endswith("/") and template != "/":
            template += "/"
        out[c["id"]] = {
            "key": f"{c['method']} {c['host']}{template}",
            "template": template,
            "params": params,
        }
    return out
