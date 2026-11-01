"""Shared helpers for code generators."""
from __future__ import annotations

import re


def is_var(x) -> bool:
    return isinstance(x, dict) and "$var" in x and len(x) <= 2


def is_fmt(x) -> bool:
    return isinstance(x, dict) and "$fmt" in x and len(x) == 1


def ident(text: str, maxlen: int = 80) -> str:
    s = re.sub(r"[^A-Za-z0-9]+", "_", text).strip("_").lower()
    if not s or s[0].isdigit():
        s = "t_" + s
    return s[:maxlen].rstrip("_")


def unique_name(base: str, used: set[str]) -> str:
    name, n = base, 2
    while name in used:
        name, n = f"{base}_{n}", n + 1
    used.add(name)
    return name


def selected_cases(analysis: dict, overrides: dict | None = None) -> list[dict]:
    overrides = overrides or {}
    out = []
    for c in analysis["cases"]:
        o = overrides.get(c["id"], {})
        sel = o.get("selected", c["selected"])
        if sel:
            c = dict(c)
            if o.get("title"):
                c["title"] = o["title"]
            if o.get("expect_status"):
                c["target"] = dict(c["target"])
                c["target"]["expect"] = {"status": int(o["expect_status"])}
            out.append(c)
    return out


def used_vars(case: dict) -> set[str]:
    found: set[str] = set()

    def walk(x):
        if is_var(x):
            found.add(x["$var"])
        elif isinstance(x, dict):
            for v in x.values():
                walk(v)
        elif isinstance(x, list):
            for v in x:
                walk(v)

    for step in case["setup"] + [case["target"]]:
        walk([step["path"], step["query"], step["headers"], step["body"]])
    return found
