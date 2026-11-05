"""Edit Postman collection/environment files while keeping their formatting, and say so plainly
when a round-trip would also touch lines we didn't mean to change.

This isn't a full format-preserving JSON editor - there isn't a tractable one for arbitrary hand
edited JSON. Instead: detect the file's indent width and trailing-newline convention, re-serialize
the whole document with json.dumps using that indent (Python dicts keep insertion order, so every
untouched key/array entry serializes back exactly as it was), and diff old vs new text. For a file
shaped like a standard Postman export - true of both Postman's own export and our own generator,
both 2-space indent with ": " after keys - the diff is just our own addition. A file that doesn't
round-trip cleanly (a different indent, trailing commas, reordered keys from another tool) shows up
as extra removed lines in the diff, which we surface as a warning instead of hiding it.
"""
from __future__ import annotations

import difflib
import json
import re
from pathlib import Path


def detect_indent(text: str) -> int | str:
    for line in text.splitlines():
        m = re.match(r"^( +|\t+)\S", line)
        if m:
            unit = m.group(1)
            return "\t" if unit[0] == "\t" else len(unit)
    return 2


def _dump(data: dict, indent, trailing_newline: bool) -> str:
    text = json.dumps(data, indent=indent, ensure_ascii=False)
    return text + "\n" if trailing_newline else text


def _diff(old_text: str, new_text: str, path_label: str) -> dict:
    old_lines = old_text.splitlines(keepends=True)
    new_lines = new_text.splitlines(keepends=True)
    diff_lines = list(difflib.unified_diff(old_lines, new_lines, fromfile=path_label, tofile=path_label))
    removed = sum(1 for ln in diff_lines if ln.startswith("-") and not ln.startswith("---"))
    added = sum(1 for ln in diff_lines if ln.startswith("+") and not ln.startswith("+++"))
    # a clean append only ever removes the one or two array-closing lines it re-adds right after
    unrelated_changes = removed > 2
    return {"diff": "".join(diff_lines), "added_lines": added, "removed_lines": removed,
            "unrelated_changes": unrelated_changes}


def add_folder_to_collection(path: Path, folder_item: dict, extra_variables: list[dict] | None = None) -> dict:
    """Append one new top-level folder (and any new collection variables) to an existing
    collection file. Returns {diff, new_text, added_lines, removed_lines, unrelated_changes}."""
    old_text = path.read_text(encoding="utf-8")
    indent = detect_indent(old_text)
    trailing_newline = old_text.endswith("\n")
    data = json.loads(old_text)
    data.setdefault("item", []).append(folder_item)
    if extra_variables:
        existing = {v.get("key") for v in data.get("variable", [])}
        new_vars = [v for v in extra_variables if v["key"] not in existing]
        if new_vars:
            data.setdefault("variable", []).extend(new_vars)
    new_text = _dump(data, indent, trailing_newline)
    result = _diff(old_text, new_text, str(path))
    result["new_text"] = new_text
    return result


def add_missing_env_vars(path: Path, var_names: list[str]) -> dict:
    """Append any of var_names not already present, each with an empty value to fill in."""
    old_text = path.read_text(encoding="utf-8")
    indent = detect_indent(old_text)
    trailing_newline = old_text.endswith("\n")
    data = json.loads(old_text)
    existing = {v.get("key") for v in data.get("values", [])}
    missing = [n for n in var_names if n not in existing]
    if missing:
        data.setdefault("values", []).extend({"key": n, "value": "", "enabled": True} for n in missing)
    new_text = _dump(data, indent, trailing_newline)
    result = _diff(old_text, new_text, str(path))
    result["new_text"] = new_text
    result["added_vars"] = missing
    return result


def new_collection_text(collection: dict) -> str:
    return json.dumps(collection, indent=2, ensure_ascii=False) + "\n"
