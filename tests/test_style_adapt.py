"""Unit tests for adapting generated Postman items to a target repo's own variable conventions."""
import json

from atlas.engine.analysis import analyse
from atlas.engine.callmodel import build_call
from atlas.engine.values import fingerprints
from atlas.engine.mask import mask_call
from atlas.gen import postman_gen
from atlas.gen.common import selected_cases
from atlas.project.style_adapt import adapt_items, extra_variable_declarations

JWT = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NSJ9.abcdefghijklmnop"
SALT = b"salt"


def _call(method, url, *, req=None, resp=None, status=200, headers=None, ts=0.0):
    h = {"content-type": "application/json", **(headers or {})}
    c = build_call(method=method, url=url, req_headers=h, req_data=json.dumps(req) if req else None,
                   status=status, resp_headers={"content-type": "application/json"},
                   resp_data=json.dumps(resp) if resp is not None else None, ts=ts)
    fps = fingerprints(c, SALT)
    m = mask_call(c)
    m.update({"id": int(ts), "seq": int(ts), "fingerprints": fps})
    return m


def _generated_collection():
    calls = [
        _call("POST", "https://shop.com/api/auth/login", req={"email": "a@b.co", "password": "pw123456"},
             resp={"token": JWT}, ts=1),
        _call("GET", "https://shop.com/api/me", resp={"id": 1, "name": "Vi"},
             headers={"authorization": f"Bearer {JWT}"}, ts=2),
    ]
    a = analyse(calls)
    cases = selected_cases(a, {})
    files = postman_gen.generate(cases, a["hosts"], "Shop")
    coll = json.loads(next(v for k, v in files.items() if k.endswith(".postman_collection.json")))
    return coll, cases, a["hosts"]


def _all_var_names(cases):
    names = set()
    for c in cases:
        names |= set(c["vars"].keys())
    return names


def _script_lines(node):
    if isinstance(node, dict):
        if node.get("type") == "text/javascript":
            yield from node.get("exec", [])
        for v in node.values():
            yield from _script_lines(v)
    elif isinstance(node, list):
        for v in node:
            yield from _script_lines(v)


def test_base_url_renamed_everywhere():
    coll, cases, hosts = _generated_collection()
    var_names = _all_var_names(cases)
    style = {"base_url_var": "apiBase", "token_var": None, "variable_scope": "collection"}
    adapted = adapt_items(coll["item"], var_names, hosts, style)
    dumped = json.dumps(adapted)
    assert "{{baseUrl}}" not in dumped
    assert "{{apiBase}}" in dumped


def test_chain_variable_prefixed_with_atlas_in_interpolation_and_set_calls():
    coll, cases, hosts = _generated_collection()
    var_names = _all_var_names(cases)
    assert "token" in var_names  # extracted from the login response
    style = {"base_url_var": "baseUrl", "token_var": None, "variable_scope": "collection"}
    adapted = adapt_items(coll["item"], var_names, hosts, style)
    dumped = json.dumps(adapted)  # {{var}} interpolations live in plain JSON fields (url/headers)
    assert "{{token}}" not in dumped
    assert "{{atlas_token}}" in dumped
    lines = list(_script_lines(adapted))  # .set("name", ...) lives inside a script's raw exec strings
    assert not any('pm.collectionVariables.set("token"' in ln for ln in lines)
    assert any('pm.collectionVariables.set("atlas_token"' in ln for ln in lines)


def test_body_field_access_with_same_name_as_a_variable_is_not_touched():
    """The login response's own 'token' field is type-checked via body["token"] in its own
    target step - that bracket access must survive untouched even though "token" is also a
    chain-variable name elsewhere in the collection."""
    coll, cases, hosts = _generated_collection()
    var_names = _all_var_names(cases)
    style = {"base_url_var": "baseUrl", "token_var": None, "variable_scope": "collection"}
    adapted = adapt_items(coll["item"], var_names, hosts, style)
    lines = list(_script_lines(adapted))
    assert any('body["token"]' in ln for ln in lines)
    assert not any('body["atlas_token"]' in ln for ln in lines)


def test_variable_scope_rescoped_to_environment():
    coll, cases, hosts = _generated_collection()
    var_names = _all_var_names(cases)
    style = {"base_url_var": "baseUrl", "token_var": None, "variable_scope": "environment"}
    adapted = adapt_items(coll["item"], var_names, hosts, style)
    lines = list(_script_lines(adapted))
    assert not any("pm.collectionVariables.set(" in ln for ln in lines)
    assert any('pm.environment.set("atlas_token"' in ln for ln in lines)


def test_extra_variable_declarations_skips_existing_and_sorts():
    decls = extra_variable_declarations({"token", "email"}, existing_keys={"atlas_email"})
    assert decls == [{"key": "atlas_token", "value": ""}]
