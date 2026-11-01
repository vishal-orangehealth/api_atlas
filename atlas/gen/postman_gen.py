"""Generate a Postman collection (v2.1) + environment from analysed test cases."""
from __future__ import annotations

import json
import uuid

from .common import is_fmt, is_var

SCHEMA = "https://schema.getpostman.com/json/collection/v2.1.0/collection.json"
CHAI_TYPES = {"string": "string", "integer": "number", "number": "number", "boolean": "boolean",
              "object": "object", "array": "array", "null": "null"}


def _uid(*parts) -> str:
    return str(uuid.uuid5(uuid.NAMESPACE_URL, "|".join(map(str, parts))))


def base_var(i: int) -> str:
    return "baseUrl" if i == 0 else f"baseUrl{i + 1}"


def _text(x) -> str:
    """Render a value IR as text with {{var}} placeholders (for URL, headers, query)."""
    if is_var(x):
        return "{{" + x["$var"] + "}}"
    if is_fmt(x):
        return "".join(_text(p) if is_var(p) else str(p) for p in x["$fmt"])
    if x is None:
        return ""
    if isinstance(x, bool):
        return "true" if x else "false"
    return str(x)


def _json_body(x) -> str:
    """JSON text with placeholders; numeric variables stay unquoted."""
    raw: dict[str, str] = {}

    def walk(o):
        if is_var(o):
            if o.get("as") == "raw":
                token = f"__ATLAS_RAW_{len(raw)}__"
                raw[token] = "{{" + o["$var"] + "}}"
                return token
            return "{{" + o["$var"] + "}}"
        if is_fmt(o):
            return _text(o)
        if isinstance(o, dict):
            return {k: walk(v) for k, v in o.items()}
        if isinstance(o, list):
            return [walk(v) for v in o]
        return o

    text = json.dumps(walk(x), indent=2, ensure_ascii=False)
    for token, val in raw.items():
        text = text.replace(f'"{token}"', val)
    return text


def _js(v) -> str:
    return json.dumps(v, ensure_ascii=False)


def _js_access(base: str, path: list) -> str:
    return base + "".join(f"[{_js(p)}]" for p in path)


def _prerequest(step: dict, specs: dict) -> list[str]:
    lines = []
    for name in step["assigns"]:
        spec = specs.get(name)
        if not spec:
            continue
        k = spec["kind"]
        if k == "env":
            d = spec.get("default")
            lines.append(f"var {('_' + name)} = pm.environment.get({_js(spec['env'])})" +
                         (f" || {_js(d)};" if d is not None else ";"))
            if d is None:
                lines.append(f"if (!{'_' + name}) {{ throw new Error('Set {spec['env']} in the environment'); }}")
            lines.append(f"pm.collectionVariables.set({_js(name)}, {'_' + name});")
        elif k == "unique":
            u = spec["unique"]
            stamp = "Date.now().toString(36) + Math.floor(Math.random() * 1e6).toString(36)"
            if u == "email":
                lines.append(f"pm.collectionVariables.set({_js(name)}, 'atlas_' + {stamp} + '@example.com');")
            elif u == "phone":
                lines.append(f"pm.collectionVariables.set({_js(name)}, '9' + String(Math.floor(Math.random() * 1e9)).padStart(9, '0'));")
            else:
                lines.append(f"pm.collectionVariables.set({_js(name)}, {_js(spec.get('base', 'atlas'))} + '_' + {stamp});")
        else:
            lines.append(f"pm.collectionVariables.set({_js(name)}, {_js(spec.get('value'))});")
    if step.get("fresh_session"):
        lines.append("try { pm.cookies.jar().clear(pm.request.url.toString()); } catch (e) {}")
    return lines


def _tests(step: dict, with_assertions: bool) -> list[str]:
    e = step["expect"]
    if "status" in e:
        lines = [f"pm.test('status is {e['status']}', function () {{ pm.response.to.have.status({int(e['status'])}); }});"]
    elif "status_in" in e:
        lines = [f"pm.test('status is one of {e['status_in']}', function () {{ pm.expect(pm.response.code).to.be.oneOf({_js(e['status_in'])}); }});"]
    else:
        lo, hi = e["status_range"]
        lines = [f"pm.test('status is {lo}-{hi}', function () {{ pm.expect(pm.response.code).to.be.within({lo}, {hi}); }});"]
    needs_body = step["extract"] or (with_assertions and step["assertions"])
    if needs_body:
        lines.append("var body = {}; try { body = pm.response.json(); } catch (e) {}")
    for ex in step["extract"]:
        if ex["source"] == "body":
            lines.append(f"pm.collectionVariables.set({_js(ex['var'])}, {_js_access('body', ex['path'])});")
        elif ex["source"] == "header":
            lines.append(f"pm.collectionVariables.set({_js(ex['var'])}, pm.response.headers.get({_js(ex['path'][0])}));")
    if with_assertions:
        for a in step["assertions"]:
            path = a["path"]
            if path and path[0] == "*":
                expr = _js_access("body[0]", path[1:])
                guard = "if (!body.length) return; "
            else:
                expr = _js_access("body", path)
                guard = ""
            label = ".".join(str(p) for p in path) or "body"
            if a["check"] == "type":
                t = CHAI_TYPES.get(a["type"], "object")
                if a["type"] == "integer":
                    check = f"pm.expect(Number.isInteger({expr})).to.be.true;"
                else:
                    check = f"pm.expect({expr}).to.be.an({_js(t)});"
                lines.append(f"pm.test({_js(label + ' is ' + a['type'])}, function () {{ {guard}{check} }});")
            elif a["check"] == "equals":
                lines.append(f"pm.test({_js(label + ' equals ' + json.dumps(a['value']))}, function () {{ {guard}pm.expect({expr}).to.eql({_js(a['value'])}); }});")
            elif a["check"] == "present":
                lines.append(f"pm.test({_js(label + ' is present')}, function () {{ {guard}pm.expect({expr}).to.not.equal(undefined); }});")
    return lines


def _request_item(step: dict, specs: dict, case_id: str, idx: int, is_target: bool) -> dict:
    base = "{{" + base_var(step["host_index"]) + "}}"
    path_parts = [_text(p) for p in step["path"]]
    query = [{"key": k, "value": _text(v)} for k, v in step["query"]]
    raw = base + "/" + "/".join(path_parts)
    if query:
        raw += "?" + "&".join(f"{q['key']}={q['value']}" for q in query)
    headers = [{"key": k, "value": _text(v)} for k, v in step["headers"].items()]
    request: dict = {
        "method": step["method"],
        "header": headers,
        "url": {"raw": raw, "host": [base], "path": path_parts, **({"query": query} if query else {})},
    }
    kind = step["body_kind"]
    if kind == "json":
        request["body"] = {"mode": "raw", "raw": _json_body(step["body"]), "options": {"raw": {"language": "json"}}}
        if not any(h["key"].lower() == "content-type" for h in headers):
            headers.append({"key": "Content-Type", "value": "application/json"})
    elif kind == "form":
        request["body"] = {"mode": "urlencoded",
                           "urlencoded": [{"key": k, "value": _text(v)} for k, v in (step["body"] or {}).items()]}
    elif kind == "text" and step["body"] is not None:
        request["body"] = {"mode": "raw", "raw": _text(step["body"])}
    events = []
    pre = _prerequest(step, specs)
    if pre:
        events.append({"listen": "prerequest", "script": {"type": "text/javascript", "exec": pre}})
    events.append({"listen": "test", "script": {"type": "text/javascript", "exec": _tests(step, is_target)}})
    return {
        "id": _uid(case_id, idx),
        "name": step["label"] if not is_target else f"▶ {step['label']}",
        "event": events,
        "request": request,
    }


def generate(cases: list[dict], hosts: list[str], suite_name: str = "app") -> dict[str, str]:
    folders = []
    env_vars: dict[str, str] = {}
    var_names: set[str] = set()
    for case in cases:
        specs = case["vars"]
        var_names.update(specs.keys())
        for spec in specs.values():
            if spec.get("kind") == "env":
                env_vars[spec["env"]] = spec.get("default") or ""
        items = [_request_item(s, specs, case["id"], i, False) for i, s in enumerate(case["setup"])]
        items.append(_request_item(case["target"], specs, case["id"], len(items), True))
        folders.append({"id": _uid(case["id"]), "name": case["title"], "item": items,
                        "description": f"[{case['kind']}] generated by API Atlas"})

    collection = {
        "info": {"_postman_id": _uid(suite_name, "collection"), "name": f"{suite_name} (API Atlas)",
                 "schema": SCHEMA,
                 "description": "Generated by API Atlas. Each folder is one self-contained test: setup requests first, the checked request last (▶)."},
        "item": folders,
        "variable": [{"key": base_var(i), "value": h} for i, h in enumerate(hosts)] +
                    [{"key": n, "value": ""} for n in sorted(var_names)],
    }
    environment = {
        "id": _uid(suite_name, "env"),
        "name": f"{suite_name} (API Atlas)",
        "values": [{"key": base_var(i), "value": h, "enabled": True} for i, h in enumerate(hosts)] +
                  [{"key": k, "value": v, "enabled": True, "type": "secret"} for k, v in sorted(env_vars.items())],
        "_postman_variable_scope": "environment",
    }
    slug = "".join(ch if ch.isalnum() else "-" for ch in suite_name.lower()).strip("-") or "app"
    readme = f"""# {suite_name} Postman collection

Generated by API Atlas: {len(cases)} test(s).

1. Import `{slug}.postman_collection.json` and `{slug}.postman_environment.json` into Postman.
2. Fill in the environment values ({', '.join(sorted(env_vars)) or 'none needed'}).
3. Run the collection, or from the command line:

```bash
npx newman run {slug}.postman_collection.json -e {slug}.postman_environment.json
```
"""
    return {
        f"{slug}.postman_collection.json": json.dumps(collection, indent=2, ensure_ascii=False),
        f"{slug}.postman_environment.json": json.dumps(environment, indent=2, ensure_ascii=False),
        "README.md": readme,
    }
