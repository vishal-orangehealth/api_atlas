"""Tolerant reader for Postman collections (v2.0 and v2.1 share the item/request tree we need)."""
from __future__ import annotations

import re

VAR_RE = re.compile(r"^\{\{.+\}\}$")


def is_param_segment(seg: str) -> bool:
    """A path segment Postman (or the collection author) treats as variable, not literal."""
    if not seg:
        return False
    return seg.startswith(":") or bool(VAR_RE.match(seg))


def _url_path_segments(url) -> list[str]:
    """Return the path segments of a request url, string or v2.0/v2.1 object form."""
    if isinstance(url, dict):
        path = url.get("path")
        if isinstance(path, list):
            # entries can be plain strings or {"type": "...", "value": "..."} in some exports
            return [p["value"] if isinstance(p, dict) else str(p) for p in path if p not in (None, "")]
        raw = url.get("raw", "")
    else:
        raw = url or ""
    # strip scheme://host and query/hash from a raw url string
    raw = re.sub(r"^[a-zA-Z][a-zA-Z0-9+.-]*://[^/]*", "", raw)
    raw = raw.split("?", 1)[0].split("#", 1)[0]
    return [s for s in raw.split("/") if s]


def _url_raw(url) -> str:
    if isinstance(url, dict):
        return url.get("raw") or "/" + "/".join(_url_path_segments(url))
    return url or ""


def walk_items(items: list[dict], folder_path: list[str] | None = None):
    """Yield (folder_path, item_dict) for every leaf request in a Postman item tree."""
    folder_path = folder_path or []
    for item in items or []:
        children = item.get("item")
        if isinstance(children, list):
            yield from walk_items(children, folder_path + [item.get("name", "")])
        elif "request" in item:
            yield folder_path, item


def requests_in(collection: dict) -> list[dict]:
    """Flatten a collection into leaf requests: method, path segments, folder, name, raw url."""
    out = []
    for folder_path, item in walk_items(collection.get("item", [])):
        req = item["request"]
        if isinstance(req, str):  # some old exports put the method+url directly as a string
            continue
        url = req.get("url", "")
        segs = _url_path_segments(url)
        out.append({
            "name": item.get("name", ""),
            "method": (req.get("method") or "GET").upper(),
            "path_segments": segs,
            "is_param": [is_param_segment(s) for s in segs],
            "folder_path": folder_path,
            "raw_url": _url_raw(url),
            "item_id": item.get("id"),
        })
    return out


def top_level_folders(collection: dict) -> list[dict]:
    """The collection's immediate folders: {id, name}, for "add to this folder" pickers."""
    return [{"id": it.get("id"), "name": it.get("name", "")}
            for it in collection.get("item", []) if isinstance(it.get("item"), list)]


def unit_index(collection: dict) -> dict[str, dict]:
    """Map every leaf request's id -> {"unit_id", "unit_name", "request_name"}, grouped by the
    collection's own top-level folders (a bare top-level request is its own unit). Used to
    report pass/fail per folder when running a collection Atlas didn't generate, where there's
    no case id scheme of our own to group by. request_name is the individual request's own
    name, so a failure inside a multi-request folder can still say which request broke."""
    idx: dict[str, dict] = {}
    for folder_path, item in walk_items(collection.get("item", [])):
        item_id = item.get("id")
        if not item_id:
            continue
        request_name = item.get("name", "")
        if folder_path:
            idx[item_id] = {"unit_id": f"folder:{folder_path[0]}", "unit_name": folder_path[0],
                            "request_name": request_name}
        else:
            idx[item_id] = {"unit_id": item_id, "unit_name": request_name, "request_name": request_name}
    return idx


def environment_vars(environment: dict) -> dict[str, str]:
    return {v["key"]: v.get("value", "") for v in environment.get("values", []) if v.get("key")}
