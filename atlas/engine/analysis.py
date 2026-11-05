"""Turn a captured session into endpoints, links and test cases.

Everything here is deterministic: same calls in, same analysis out.
"""
from __future__ import annotations

import copy
import hashlib
import re

from .callmodel import path_segments
from .deps import auth_kind, find_links
from .mask import is_masked, masked_kind
from .normalize import _slugish, build_templates, looks_like_param, singular
from .schema import fields as schema_fields
from .schema import infer, to_json_schema, type_of

SIGNUP_RE = re.compile(r"(register|signup|sign-up|sign_up|/users/?$|/accounts/?$|/customers/?$|/members/?$)", re.I)
UNIQUE_KEY_RE = re.compile(r"(email|mail|phone|mobile|username|user_name|login|handle|msisdn)", re.I)
GENERIC_SEGMENTS = {"api", "v1", "v2", "v3", "v4", "rest", "public", "internal", "graphql"}

SEND_HEADER_DENY = {
    "host", "content-length", "connection", "accept-encoding", "user-agent", "accept-language",
    "origin", "referer", "pragma", "cache-control", "priority", "dnt", "upgrade-insecure-requests",
    "if-none-match", "if-modified-since", "cookie", "te", "accept",
}
AUTH_HEADERS = {"authorization", "x-api-key", "api-key", "apikey", "x-auth-token", "x-access-token",
                "x-session-token"}

MAX_ASSERTIONS = 12


# ---------------------------------------------------------------- helpers

def snake(s: str) -> str:
    s = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", str(s))
    s = re.sub(r"[^A-Za-z0-9]+", "_", s).strip("_").lower()
    if not s:
        s = "value"
    if s[0].isdigit():
        s = "v_" + s
    return s


def short_id(*parts) -> str:
    return hashlib.sha1("|".join(map(str, parts)).encode()).hexdigest()[:10]


def get_path(obj, path):
    for p in path:
        if isinstance(obj, dict) and p in obj:
            obj = obj[p]
        elif isinstance(obj, list) and isinstance(p, int) and p < len(obj):
            obj = obj[p]
        else:
            return None
    return obj


def set_path(obj, path, value):
    for p in path[:-1]:
        obj = obj[p]
    obj[path[-1]] = value


def loc_key(call_id, loc) -> str:
    return f"{call_id}:{loc['side']}:{loc['part']}:{'/'.join(map(str, loc['path']))}"


def resource_name(template: str) -> str:
    lits = [s for s in path_segments(template) if not s.startswith("{") and s.lower() not in GENERIC_SEGMENTS]
    return singular(lits[-1]) if lits else "item"


def is_signup(call: dict, template: str) -> bool:
    return call["method"] == "POST" and bool(SIGNUP_RE.search(template))


def ok_status(s) -> bool:
    return isinstance(s, int) and 200 <= s < 400


# ---------------------------------------------------------------- analysis

def analyse(calls: list[dict]) -> dict:
    """calls: visible, masked, ordered calls with "id", "seq", "fingerprints"."""
    # order by when each request started: responses can finish out of order
    calls = sorted(calls, key=lambda c: (c.get("ts") or 0, c["seq"]))
    by_id = {c["id"]: c for c in calls}

    raw_links = find_links(calls)
    links = []
    for ln in raw_links:
        to_loc = ln["to_loc"]
        if to_loc["part"] == "path":
            seg = path_segments(by_id[ln["to"]]["path"])[to_loc["path"][0]]
            if ln["kind"] == "reuse" or ln["from_loc"]["part"] == "path":
                # /api/products then /api/products/2: the word "products" is not a value
                if not looks_like_param(seg):
                    continue
            elif not (ln["from_idk"] or looks_like_param(seg) or _slugish(seg)):
                continue
        links.append(ln)

    linked_path = {(ln["to"], ln["to_loc"]["path"][0]) for ln in links if ln["to_loc"]["part"] == "path"}
    templates = build_templates(calls, linked_path)
    for c in calls:
        c["endpoint"] = templates[c["id"]]["key"]
        c["template"] = templates[c["id"]]["template"]
        c["params"] = templates[c["id"]]["params"]

    hosts = []
    for c in calls:
        if c["origin"] not in hosts:
            hosts.append(c["origin"])

    endpoints: dict[str, dict] = {}
    for c in calls:
        ep = endpoints.setdefault(c["endpoint"], {
            "key": c["endpoint"], "method": c["method"], "host": c["host"], "origin": c["origin"],
            "template": c["template"], "params": [p["name"] for p in c["params"]],
            "call_ids": [], "statuses": [], "auth": None, "auth_always": True,
        })
        ep["call_ids"].append(c["id"])
        if c["status"] not in ep["statuses"]:
            ep["statuses"].append(c["status"])
        ak = auth_kind(c)
        ep["auth"] = ep["auth"] or ak
        ep["auth_always"] = ep["auth_always"] and ak is not None

    token_issuers = set()
    for ln in links:
        if ln["to_loc"]["part"] in ("header", "cookie") and ln["kind"] == "response":
            token_issuers.add(by_id[ln["from"]]["endpoint"])

    for ep in endpoints.values():
        ep_calls = [by_id[i] for i in ep["call_ids"]]
        rep = next((c for c in ep_calls if ok_status(c["status"])), ep_calls[0])
        ep["sample_call_id"] = rep["id"]
        ok_calls = [c for c in ep_calls if c["status"] == rep["status"]]
        resp_samples = [c["resp_body"] for c in ok_calls if c["resp_kind"] == "json"]
        req_samples = [c["req_body"] for c in ep_calls if c["req_kind"] == "json"]
        ep["resp_node"] = infer(resp_samples) if resp_samples else None
        ep["req_node"] = infer(req_samples) if req_samples else None
        ep["response_schema"] = to_json_schema(ep["resp_node"])
        ep["request_schema"] = to_json_schema(ep["req_node"])
        ep["response_fields"] = schema_fields(ep["resp_node"])
        ep["issues_token"] = ep["key"] in token_issuers
        deps = []
        for ln in links:
            if ln["to"] in ep["call_ids"] and by_id[ln["from"]]["endpoint"] != ep["key"]:
                src = by_id[ln["from"]]["endpoint"]
                label = _field_label(ln["from_loc"])
                item = {"endpoint": src, "field": label, "used_in": _field_label(ln["to_loc"]), "kind": ln["kind"]}
                if item not in deps:
                    deps.append(item)
        ep["depends_on"] = deps

    builder = CaseBuilder(calls, by_id, links, hosts)
    cases = []
    for ep in endpoints.values():
        cases.extend(builder.cases_for(ep))

    needs_env = sorted({e for case in cases for e in case["needs_env"]})
    return {
        "hosts": hosts,
        "endpoints": list(endpoints.values()),
        "links": links,
        "cases": cases,
        "needs_env": needs_env,
        "calls": {c["id"]: {"endpoint": c["endpoint"]} for c in calls},
    }


def _field_label(loc) -> str:
    part, path = loc["part"], loc["path"]
    if part == "header":
        return f"header {path[0]}"
    if part == "cookie":
        return f"cookie {path[0]}"
    if part == "path":
        return f"path segment {path[0] + 1}"
    if part == "query":
        return f"query {path[0]}"
    return "body " + ".".join(str(p) for p in path) if path else "body"


# ---------------------------------------------------------------- test cases

class CaseBuilder:
    def __init__(self, calls, by_id, links, hosts):
        self.calls = calls
        self.by_id = by_id
        self.hosts = hosts
        self.links_to: dict[int, list[dict]] = {}
        for ln in links:
            self.links_to.setdefault(ln["to"], []).append(ln)

    # chain of setup calls a target needs, in capture order
    def chain(self, target_id: int) -> list[int]:
        needed, stack = set(), [target_id]
        while stack:
            cid = stack.pop()
            for ln in self.links_to.get(cid, []):
                if ln["from"] not in needed and ln["from"] != target_id:
                    needed.add(ln["from"])
                    stack.append(ln["from"])
        return sorted(needed, key=lambda i: (self.by_id[i].get("ts") or 0, self.by_id[i]["seq"]))

    def cases_for(self, ep: dict) -> list[dict]:
        target = self.by_id[ep["sample_call_id"]]
        base = self.build_flow(target)
        if base is None:
            return []
        cases = []

        def add(kind, title, selected, mutate, expect, variant=""):
            flow = copy.deepcopy(base)
            if mutate and mutate(flow["target"]) is False:
                return
            flow["target"]["expect"] = expect
            if kind != "happy":
                flow["target"]["assertions"] = []
                flow["target"]["extract"] = []
            cid = short_id(ep["key"], kind, variant)
            cases.append({
                "id": cid, "endpoint": ep["key"], "kind": kind, "title": title, "selected": selected,
                "setup": flow["setup"], "target": flow["target"], "vars": flow["vars"],
                "needs_env": sorted({v["env"] for v in flow["vars"].values()
                                     if v["kind"] == "env" and v.get("default") is None}),
            })

        status = target["status"]
        name = f"{ep['method']} {ep['template']}"
        add("happy", f"{name} returns {status}", True, None, {"status": status})

        if ep["auth"] and ep["auth_always"]:
            def no_auth(step):
                before = len(step["headers"])
                step["headers"] = {k: v for k, v in step["headers"].items() if k.lower() not in AUTH_HEADERS
                                   and k.lower() != "cookie"}
                if ep["auth"] == "cookie":
                    step["fresh_session"] = True
                elif len(step["headers"]) == before:
                    return False
            # opt-in: the browser may send a token to endpoints that don't need one
            add("no_auth", f"{name} without auth is rejected", False, no_auth, {"status_in": [401, 403]})

        body = target["req_body"]
        if target["req_kind"] == "json" and isinstance(body, dict) and body:
            keys = list(body.keys())
            for k in keys[:6]:
                def drop(step, k=k):
                    step["body"].pop(k, None)
                add("missing_field", f"{name} without '{k}' is rejected", False, drop,
                    {"status_range": [400, 499]}, k)
            for k in keys[:4]:
                wrong = _wrong_value(body[k])
                if wrong is _SKIP:
                    continue

                def wrong_type(step, k=k, wrong=wrong):
                    step["body"][k] = wrong
                add("wrong_type", f"{name} with wrong type for '{k}' is rejected", False, wrong_type,
                    {"status_range": [400, 499]}, k)

            def empty(step):
                step["body"] = {}
            add("empty_body", f"{name} with empty body is rejected", False, empty, {"status_range": [400, 499]})

        for p in target["params"]:
            def invalid(step, p=p):
                idx = p["index"]
                example = p["example"]
                if re.fullmatch(r"\d+", example):
                    bad = "999999999"
                elif re.fullmatch(r"[0-9a-fA-F-]{36}", example):
                    bad = "00000000-0000-4000-8000-000000000000"
                else:
                    bad = "atlas-does-not-exist"
                step["path"][idx] = bad
            add("invalid_id", f"{name} with unknown {p['name']} returns 4xx", False, invalid,
                {"status_range": [400, 499]}, p["name"])
        return cases

    # ------------------------------------------------------------ flow IR

    def build_flow(self, target: dict) -> dict | None:
        setup_ids = self.chain(target["id"])
        chain_ids = setup_ids + [target["id"]]
        in_chain = set(chain_ids)
        vars_: dict[str, dict] = {}
        var_by_loc: dict[str, str] = {}
        steps = {}

        def new_var(base: str, spec: dict) -> str:
            name, n = snake(base), 2
            while name in vars_:
                name, n = f"{snake(base)}_{n}", n + 1
            vars_[name] = spec
            return name

        # 1) variables extracted from responses / reused from earlier requests
        extracts: dict[int, list[dict]] = {}
        assigns: dict[int, list[str]] = {}
        consumer_ref: dict[str, dict] = {}
        for cid in chain_ids:
            for ln in self.links_to.get(cid, []):
                if ln["from"] not in in_chain:
                    continue
                if ln["to_loc"]["part"] == "cookie":
                    continue  # the HTTP session carries cookies
                src_key = loc_key(ln["from"], ln["from_loc"])
                if src_key not in var_by_loc:
                    producer = self.by_id[ln["from"]]
                    base = self._var_base(producer, ln["from_loc"])
                    if ln["kind"] == "response":
                        if ln["from_loc"]["part"] == "body":
                            vtype = type_of(get_path(producer["resp_body"], ln["from_loc"]["path"]))
                        else:
                            vtype = "string"
                        name = new_var(base, {"kind": "extract", "from": ln["from"], "vtype": vtype})
                        extracts.setdefault(ln["from"], []).append(
                            {"var": name, "source": ln["from_loc"]["part"], "path": ln["from_loc"]["path"]})
                    else:
                        value = self._request_value(producer, ln["from_loc"])
                        spec = self._value_spec(producer, ln["from_loc"], value)
                        name = new_var(base, spec)
                        assigns.setdefault(ln["from"], []).append(name)
                    var_by_loc[src_key] = name
                consumer_ref[loc_key(cid, ln["to_loc"])] = {
                    "var": var_by_loc[src_key], "prefix": ln["to_loc"].get("prefix")}

        # 2) render each step
        for cid in chain_ids:
            call = self.by_id[cid]
            steps[cid] = self._step(call, consumer_ref, var_by_loc, vars_, new_var, assigns, extracts.get(cid, []))

        setup = [steps[i] for i in setup_ids]
        tgt = steps[target["id"]]
        tgt["assertions"] = self._assertions(target)
        return {"setup": setup, "target": tgt, "vars": vars_}

    def _var_base(self, call: dict, loc: dict) -> str:
        part, path = loc["part"], loc["path"]
        if part == "header":
            h = path[0].lower()
            if h == "authorization":
                return "token"
            return re.sub(r"^x[-_]", "", h)
        if part == "path":
            for p in call.get("params", []):
                if p["index"] == path[0]:
                    return p["name"]
            return "path_value"
        keys = [p for p in path if isinstance(p, str)]
        key = keys[-1] if keys else "value"
        if snake(key) in ("id", "uuid", "key", "code"):
            return f"{resource_name(call['template'])}_{snake(key)}"
        return key

    def _request_value(self, call: dict, loc: dict):
        part, path = loc["part"], loc["path"]
        if part == "body":
            return get_path(call["req_body"], path)
        if part == "query":
            for k, v in call["query"]:
                if k == path[0]:
                    return v
        if part == "path":
            return path_segments(call["path"])[path[0]]
        if part == "header":
            return call["req_headers"].get(path[0])
        return None

    def _value_spec(self, call: dict, loc: dict, value) -> dict:
        """How a generated test should produce a value that the user typed in."""
        key = next((p for p in reversed(loc["path"]) if isinstance(p, str)), "") if loc["part"] != "header" else loc["path"][0]
        mk = masked_kind(value)
        signup = is_signup(call, call["template"])
        env_name = "ATLAS_" + snake(key).upper()
        if mk in ("email",) and signup:
            return {"kind": "unique", "unique": "email"}
        if mk == "phone" and signup:
            return {"kind": "unique", "unique": "phone"}
        if mk == "secret" and signup:
            return {"kind": "env", "env": env_name, "default": "Atlas#Test1234"}
        if mk:
            return {"kind": "env", "env": env_name, "default": None}
        if signup and isinstance(value, str) and UNIQUE_KEY_RE.search(key or ""):
            return {"kind": "unique", "unique": "string", "base": re.sub(r"[^a-zA-Z]", "", value)[:10] or "atlas"}
        return {"kind": "literal", "value": value}

    def _step(self, call, consumer_ref, var_by_loc, vars_, new_var, assigns, extracts) -> dict:
        cid = call["id"]
        step_assigns = list(assigns.get(cid, []))

        def ref_for(loc, value):
            """Return a value IR for a request location."""
            r = consumer_ref.get(loc_key(cid, loc))
            as_str = isinstance(value, str) or loc["part"] != "body"
            if r:
                ir = {"$var": r["var"], "as": "str" if as_str else "raw"}
                if r.get("prefix"):
                    return {"$fmt": [r["prefix"], ir]}
                return ir
            own = var_by_loc.get(loc_key(cid, loc))
            if own:  # this step introduces a reused value
                return {"$var": own, "as": "str" if as_str else "raw"}
            mk = masked_kind(value)
            if mk:
                spec = self._value_spec(call, loc, value)
                base = loc["path"][0] if loc["part"] in ("header", "query") else \
                    next((p for p in reversed(loc["path"]) if isinstance(p, str)), "value")
                if loc["part"] == "header" and base.lower() == "authorization":
                    base = "token"
                    spec = {"kind": "env", "env": "ATLAS_AUTH_TOKEN", "default": None}
                elif loc["part"] == "header":
                    spec = {"kind": "env", "env": "ATLAS_" + snake(base).upper(), "default": None}
                name = new_var(base, spec)
                var_by_loc[loc_key(cid, loc)] = name
                step_assigns.append(name)
                return {"$var": name, "as": "str"}
            return value

        # path
        segs = path_segments(call["path"])
        path_ir = [ref_for({"side": "req", "part": "path", "path": [i]}, s) for i, s in enumerate(segs)]
        # query
        query_ir = [[k, ref_for({"side": "req", "part": "query", "path": [k]}, v)] for k, v in call["query"]]
        # headers
        headers_ir = {}
        for name, value in call["req_headers"].items():
            if name in SEND_HEADER_DENY or name.startswith("sec-") or name.startswith(":"):
                continue  # HTTP/2 pseudo-headers from old recordings, pre-dating the capture-side filter
            if name == "content-type" and call["req_kind"] != "text":
                continue  # the HTTP client sets it for JSON and form bodies
            if name in ("x-requested-with",):
                continue
            loc = {"side": "req", "part": "header", "path": [name]}
            from .mask import BEARER_RE
            m = BEARER_RE.match(value.strip())
            r = consumer_ref.get(loc_key(cid, loc))
            if r:
                headers_ir[_title(name)] = {"$fmt": [r["prefix"] or "", {"$var": r["var"], "as": "str"}]}
            elif m and is_masked(m.group(2)):
                vname = var_by_loc.get(loc_key(cid, loc))
                if not vname:
                    vname = new_var("auth_token", {"kind": "env", "env": "ATLAS_AUTH_TOKEN", "default": None})
                    var_by_loc[loc_key(cid, loc)] = vname
                    step_assigns.append(vname)
                headers_ir[_title(name)] = {"$fmt": [m.group(1) + " ", {"$var": vname, "as": "str"}]}
            else:
                headers_ir[_title(name)] = ref_for(loc, value)
        # cookies that no captured call produced: the user's existing browser session
        cookie_hdr = call["req_headers"].get("cookie")
        if cookie_hdr:
            linked = {tuple(ln["to_loc"]["path"]) for ln in self.links_to.get(cid, []) if ln["to_loc"]["part"] == "cookie"}
            from .callmodel import parse_cookie_header
            names = list(parse_cookie_header(cookie_hdr).keys())
            if names and not any((n,) in linked for n in names) and self._needs_cookie(call):
                vname = next((n for n, s in vars_.items() if s.get("env") == "ATLAS_COOKIE"), None)
                if not vname:
                    vname = new_var("cookie", {"kind": "env", "env": "ATLAS_COOKIE", "default": None})
                    step_assigns.append(vname)
                headers_ir["Cookie"] = {"$var": vname, "as": "str"}
        # body
        body_ir = None
        if call["req_kind"] in ("json", "form"):
            body_ir = copy.deepcopy(call["req_body"])
            from .values import leaves
            for path, value, _key in leaves(call["req_body"]):
                if not path:
                    continue
                new = ref_for({"side": "req", "part": "body", "path": path}, value)
                if new is not value:
                    set_path(body_ir, path, new)
            if not isinstance(body_ir, (dict, list)):
                body_ir = ref_for({"side": "req", "part": "body", "path": []}, body_ir)
        elif call["req_kind"] == "text":
            body_ir = call["req_body"]

        host_index = self.hosts.index(call["origin"]) if call["origin"] in self.hosts else 0
        return {
            "call_id": cid,
            "name": f"{call['method'].lower()}_{snake(call['template'])}",
            "label": f"{call['method']} {call['template']}",
            "method": call["method"],
            "host_index": host_index,
            "path": path_ir,
            "query": query_ir,
            "headers": headers_ir,
            "body_kind": call["req_kind"],
            "body": body_ir,
            "content_type": call["req_headers"].get("content-type"),
            "assigns": step_assigns,
            "extract": extracts,
            "expect": {"status": call["status"]},
            "fresh_session": False,
            "assertions": [],
            "params": call.get("params", []),
            "req_body": call["req_body"],
            "req_kind": call["req_kind"],
        }

    def _needs_cookie(self, call: dict) -> bool:
        # only pass a cookie through when the request has no other way to authenticate
        return not any(h in call["req_headers"] for h in AUTH_HEADERS)

    def _assertions(self, call: dict) -> list[dict]:
        if call["resp_kind"] != "json":
            return []
        body = call["resp_body"]
        out = [{"path": [], "check": "type", "type": type_of(body)}]
        node = infer([c["resp_body"] for c in self.calls
                      if c.get("endpoint") == call.get("endpoint") and c["status"] == call["status"]
                      and c["resp_kind"] == "json"])
        ep_fields = schema_fields(node)
        for f in ep_fields:
            if len(out) >= MAX_ASSERTIONS:
                break
            path = f["path"]
            if len(path) > 2 or "*" in path[1:] or not f["required"]:
                continue
            if path[0] == "*" and not isinstance(body, list):
                continue
            types = [t for t in f["types"] if t != "null"]
            if len(f["types"]) != 1 or not types:
                if types and "null" in f["types"]:
                    out.append({"path": path, "check": "present"})
                continue
            if (not f["dynamic"] and f["samples"] >= 2 and len(f["values"]) == 1
                    and isinstance(f["values"][0], (bool, str)) and len(str(f["values"][0])) <= 30
                    and not is_masked(f["values"][0]) and path[0] != "*"):
                out.append({"path": path, "check": "equals", "value": f["values"][0]})
            else:
                out.append({"path": path, "check": "type", "type": types[0]})
        return out


_SKIP = object()


def _wrong_value(v):
    if isinstance(v, bool):
        return "not-a-boolean"
    if isinstance(v, (int, float)):
        return "not-a-number"
    if isinstance(v, str):
        if is_masked(v):
            return _SKIP
        return 12345
    if isinstance(v, list):
        return "not-a-list"
    if isinstance(v, dict):
        return "not-an-object"
    return _SKIP


def _title(name: str) -> str:
    return "-".join(p[:1].upper() + p[1:] for p in name.split("-"))
