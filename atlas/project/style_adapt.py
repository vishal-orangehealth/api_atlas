"""Adapt our own generated Postman items to a target repo's variable naming/scope, and prefix our
chain variables with atlas_ so they never collide with the team's own variables.

Renaming is precise, not a blind text substitution: {{var}} interpolations are renamed wherever
they appear (url, headers, body), and the one other place a chain-variable name shows up as a bare
string - the first argument of pm.collectionVariables.set("name", ...) in our generated scripts -
is matched by its exact call shape. We never touch other string literals (like a body field access
such as body["id"]), even if a chain variable happens to share that name.
"""
from __future__ import annotations

import re

from ..gen.postman_gen import base_var

_BRACE_RE = re.compile(r"\{\{([a-zA-Z0-9_.]+)\}\}")
_SET_RE = re.compile(r'pm\.collectionVariables\.set\("([a-zA-Z_][a-zA-Z0-9_]*)"\s*,')


def _rewrite_braces(text: str, renames: dict[str, str]) -> str:
    return _BRACE_RE.sub(lambda m: "{{" + renames.get(m.group(1), m.group(1)) + "}}", text)


def _rewrite_set_calls(line: str, renames: dict[str, str]) -> str:
    return _SET_RE.sub(lambda m: f'pm.collectionVariables.set("{renames.get(m.group(1), m.group(1))}",', line)


def _is_script(node) -> bool:
    return isinstance(node, dict) and node.get("type") == "text/javascript" and isinstance(node.get("exec"), list)


def _walk(node, renames: dict[str, str]):
    if isinstance(node, str):
        return _rewrite_braces(node, renames)
    if isinstance(node, dict):
        if _is_script(node):
            return {**node, "exec": [_rewrite_set_calls(_rewrite_braces(ln, renames), renames) for ln in node["exec"]]}
        return {k: _walk(v, renames) for k, v in node.items()}
    if isinstance(node, list):
        return [_walk(v, renames) for v in node]
    return node


def _rescope_to_environment(node):
    """In place: pm.collectionVariables.set( -> pm.environment.set( in every script's exec lines."""
    if isinstance(node, dict):
        if _is_script(node):
            node["exec"] = [ln.replace("pm.collectionVariables.set(", "pm.environment.set(") for ln in node["exec"]]
        else:
            for v in node.values():
                _rescope_to_environment(v)
    elif isinstance(node, list):
        for v in node:
            _rescope_to_environment(v)


def adapt_items(node, var_names: set[str], hosts: list[str], style: dict):
    """node: a postman_gen folder/item (or list of them). Returns an adapted deep copy."""
    renames = {base_var(i): (style["base_url_var"] if i == 0 else f"{style['base_url_var']}{i + 1}")
              for i in range(len(hosts))}
    renames.update({name: f"atlas_{name}" for name in var_names})
    adapted = _walk(node, renames)
    if style["variable_scope"] == "environment":
        _rescope_to_environment(adapted)
    return adapted


def extra_variable_declarations(var_names: set[str], existing_keys: set[str]) -> list[dict]:
    """New atlas_-prefixed collection variables to declare, skipping any already present."""
    return [{"key": f"atlas_{name}", "value": ""} for name in sorted(var_names)
            if f"atlas_{name}" not in existing_keys]
