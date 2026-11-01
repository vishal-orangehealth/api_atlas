"""Infer a schema from captured samples and work out which fields are dynamic."""
from __future__ import annotations

import json
import re

from .mask import masked_kind
from .values import is_id_key

UUID_RE = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")
DATETIME_RE = re.compile(r"^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}")
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
URI_RE = re.compile(r"^https?://")


def type_of(v) -> str:
    if v is None:
        return "null"
    if isinstance(v, bool):
        return "boolean"
    if isinstance(v, int):
        return "integer"
    if isinstance(v, float):
        return "number"
    if isinstance(v, str):
        return "string"
    if isinstance(v, list):
        return "array"
    if isinstance(v, dict):
        return "object"
    return "string"


def format_of(v) -> str | None:
    if not isinstance(v, str):
        return None
    mk = masked_kind(v)
    if mk:
        return f"masked:{mk}"
    if UUID_RE.match(v):
        return "uuid"
    if DATETIME_RE.match(v):
        return "date-time"
    if DATE_RE.match(v):
        return "date"
    if URI_RE.match(v):
        return "uri"
    return None


def new_node() -> dict:
    return {"types": [], "count": 0, "props": {}, "prop_counts": {}, "items": None,
            "values": [], "formats": []}


def add_sample(node: dict, v) -> None:
    node["count"] += 1
    t = type_of(v)
    if t not in node["types"]:
        node["types"].append(t)
    if t == "object":
        node.setdefault("obj_count", 0)
        node["obj_count"] += 1
        for k, child in v.items():
            node["prop_counts"][k] = node["prop_counts"].get(k, 0) + 1
            add_sample(node["props"].setdefault(k, new_node()), child)
    elif t == "array":
        if node["items"] is None:
            node["items"] = new_node()
        for item in v[:10]:
            add_sample(node["items"], item)
    else:
        enc = json.dumps(v, sort_keys=True)
        if enc not in node["values"] and len(node["values"]) < 6:
            node["values"].append(enc)
        f = format_of(v)
        if f and f not in node["formats"]:
            node["formats"].append(f)


def infer(samples: list) -> dict:
    root = new_node()
    for s in samples:
        add_sample(root, s)
    return root


def to_json_schema(node: dict | None) -> dict:
    if not node or not node["types"]:
        return {}
    types = node["types"]
    out: dict = {"type": types[0] if len(types) == 1 else types}
    if "object" in types:
        oc = node.get("obj_count", 0)
        out["properties"] = {k: to_json_schema(c) for k, c in node["props"].items()}
        req = [k for k, n in node["prop_counts"].items() if n == oc and oc > 0]
        if req:
            out["required"] = req
    if "array" in types and node["items"]:
        out["items"] = to_json_schema(node["items"])
    if node["formats"] and len(node["formats"]) == 1:
        out["format"] = node["formats"][0]
    return out


def fields(node: dict | None, path=None, key=None, required=True, out=None) -> list[dict]:
    """Flatten a schema node into a list of fields with their stats."""
    if out is None:
        out = []
    if node is None or not node["types"]:
        return out
    path = path or []
    if path:
        values = [json.loads(v) for v in node["values"]]
        fmt = node["formats"][0] if len(node["formats"]) == 1 else None
        dynamic = (
            (fmt is not None and fmt != "uri")
            or is_id_key(key)
            or len(values) > 1
            or any(isinstance(v, str) and len(v) > 40 for v in values)
        )
        out.append({
            "path": path, "key": key, "types": list(node["types"]), "required": required,
            "format": fmt, "samples": node["count"], "values": values, "dynamic": dynamic,
        })
    if "object" in node["types"]:
        oc = node.get("obj_count", 0)
        for k, child in node["props"].items():
            fields(child, path + [k], k, node["prop_counts"].get(k, 0) == oc, out)
    if "array" in node["types"] and node["items"]:
        fields(node["items"], path + ["*"], key, True, out)
    return out
