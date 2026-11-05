"""Detect a repo's own Postman conventions, so requests we add fit in without surprises."""
from __future__ import annotations

import re
from collections import Counter

from .postman_parse import walk_items

VAR_RE = re.compile(r"\{\{([a-zA-Z0-9_.]+)\}\}")
_URL_NAME_RE = re.compile(r"url|host|base", re.I)
_TOKEN_NAME_RE = re.compile(r"token|auth|bearer|key", re.I)


def _requests(collection: dict):
    for folder_path, item in walk_items(collection.get("item", [])):
        req = item["request"]
        if not isinstance(req, str):
            yield folder_path, req


def _script_lines(collection: dict):
    def walk(node):
        for ev in node.get("event", []) or []:
            for line in (ev.get("script") or {}).get("exec", []) or []:
                yield line
        for child in node.get("item", []) or []:
            yield from walk(child)
    yield from walk(collection)


def detect_base_url_var(collection: dict) -> str | None:
    hosts = Counter()
    for _folder, req in _requests(collection):
        url = req.get("url")
        raw = url.get("raw") if isinstance(url, dict) else url
        m = re.match(r"^\{\{([a-zA-Z0-9_.]+)\}\}", raw or "")
        if m:
            hosts[m.group(1)] += 1
    if hosts:
        return hosts.most_common(1)[0][0]
    for v in collection.get("variable", []):
        if _URL_NAME_RE.search(v.get("key", "")):
            return v["key"]
    return None


def detect_token_var(collection: dict) -> str | None:
    names = Counter()
    for _folder, req in _requests(collection):
        for h in req.get("header", []) or []:
            if str(h.get("key", "")).lower() == "authorization":
                names.update(m.group(1) for m in VAR_RE.finditer(str(h.get("value", ""))))
        auth = req.get("auth") or {}
        if auth.get("type") == "bearer":
            for entry in auth.get("bearer", []) or []:
                if entry.get("key") == "token":
                    names.update(m.group(1) for m in VAR_RE.finditer(str(entry.get("value", ""))))
    if names:
        return names.most_common(1)[0][0]
    for v in collection.get("variable", []):
        if _TOKEN_NAME_RE.search(v.get("key", "")):
            return v["key"]
    return None


def detect_variable_scope(collection: dict) -> str:
    """'collection' or 'environment' - whichever of pm.collectionVariables.set / pm.environment.set
    the repo's own scripts use more. Defaults to 'environment' when neither appears."""
    counts = Counter()
    for line in _script_lines(collection):
        counts["collection"] += len(re.findall(r"pm\.collectionVariables\.set\(", line))
        counts["environment"] += len(re.findall(r"pm\.environment\.set\(", line))
    return counts.most_common(1)[0][0] if counts else "environment"


def detect_style(collection: dict) -> dict:
    return {
        "base_url_var": detect_base_url_var(collection) or "baseUrl",
        "token_var": detect_token_var(collection),
        "variable_scope": detect_variable_scope(collection),
    }
