"""Turn a Newman JSON report into pass/fail per test folder, in plain English."""
from __future__ import annotations

import json
import re

from .mask import mask_header, mask_text, mask_tree, mask_url_path, mask_value

SNIPPET_LIMIT = 2000

_STATUS_RE = re.compile(r"expected response to have status code (\d+) but got (\d+)")
_HINT_KEYS = ("detail", "message", "error", "error_description", "msg", "reason")


def plain_reason(message: str) -> str:
    """Rewrite a Chai/Postman assertion message in plain English where we recognise the shape."""
    m = _STATUS_RE.search(message or "")
    if m:
        return f"Expected {m.group(1)}, got {m.group(2)}"
    if not message:
        return message
    return message[0].upper() + message[1:]


def body_hint(obj) -> str | None:
    """A short human explanation pulled from a JSON error body, if there is one."""
    if isinstance(obj, dict):
        for key in _HINT_KEYS:
            v = obj.get(key)
            if isinstance(v, str) and v.strip():
                return v.strip()
    return None


def _decode_body(response: dict | None):
    if not response:
        return None
    stream = response.get("stream")
    if not stream or "data" not in stream:
        return None
    try:
        raw = bytes(stream["data"]).decode("utf-8", errors="replace")
    except Exception:
        return None
    try:
        return json.loads(raw)
    except Exception:
        return raw


def _mask_snippet(body) -> str | None:
    if body is None:
        return None
    if isinstance(body, (dict, list)):
        text = json.dumps(mask_tree(body), indent=2, ensure_ascii=False)
    else:
        text = mask_text(str(body))
    if len(text) > SNIPPET_LIMIT:
        text = text[:SNIPPET_LIMIT] + "\n… (truncated)"
    return text


def _decode_request_body(body: dict | None):
    """Newman's execution.request.body, already resolved (variables substituted) - this is
    the actual payload that went over the wire, not the template."""
    if not body:
        return None
    mode = body.get("mode")
    if mode == "raw":
        raw = body.get("raw", "")
        try:
            return json.loads(raw)
        except Exception:
            return raw
    if mode in ("urlencoded", "formdata"):
        return {kv.get("key"): kv.get("value") for kv in body.get(mode, []) or [] if not kv.get("disabled")}
    return None


def _request_summary(request: dict | None) -> dict | None:
    """Method, resolved path (with path params filled in) and a masked body snippet for the
    request that was actually sent - so a failure can show not just what came back, but what
    payload or path param triggered it."""
    if not request:
        return None
    url = request.get("url") or {}
    path = "/" + "/".join(str(p) for p in (url.get("path") or []))
    path = mask_url_path(path)
    query = [kv for kv in (url.get("query") or []) if not kv.get("disabled")]
    if query:
        q = "&".join(f"{kv.get('key')}={mask_value(kv.get('value'), kv.get('key'))}" for kv in query)
        path += f"?{q}"
    headers = [{"key": h.get("key", ""), "value": mask_header(h.get("key", ""), str(h.get("value", "")))}
              for h in request.get("header", []) or []]
    body = _decode_request_body(request.get("body"))
    return {"method": request.get("method"), "path": path, "headers": headers,
            "snippet": _mask_snippet(body)}


def _response_summary(response: dict | None) -> dict | None:
    """Status, masked headers and a masked body snippet for one execution's response - shown
    for the target step of every case, pass or fail, not just failures."""
    if not response:
        return None
    headers = [{"key": h.get("key", ""), "value": mask_header(h.get("key", ""), str(h.get("value", "")))}
              for h in response.get("header", []) or []]
    body = _decode_body(response)
    return {"status": response.get("code"), "headers": headers, "snippet": _mask_snippet(body)}


def summarize(report: dict, cases: list[dict], item_index: dict[str, dict]) -> dict:
    """report: parsed Newman `--reporters json` output. item_index: see postman_gen.item_index."""
    per_case = {c["id"]: {"id": c["id"], "title": c["title"], "kind": c["kind"],
                           "endpoint": c["endpoint"], "ok": True, "reasons": [],
                           "response": None, "checks": []} for c in cases}
    run = report.get("run", {})
    responses_by_item = {}
    requests_by_item = {}
    for ex in run.get("executions", []):
        item_id = (ex.get("item") or {}).get("id")
        if item_id:
            responses_by_item[item_id] = ex.get("response")
            requests_by_item[item_id] = ex.get("request")
        meta = item_index.get(item_id)
        case = per_case.get(meta["case_id"]) if meta else None
        if not case:
            continue
        if any(a.get("error") for a in ex.get("assertions", [])):
            case["ok"] = False
        if meta["is_target"]:
            case["response"] = _response_summary(ex.get("response"))
            case["request"] = _request_summary(ex.get("request"))
            case["checks"] = [{"name": a.get("assertion", ""), "ok": not a.get("error")}
                              for a in ex.get("assertions", [])]

    for failure in run.get("failures", []):
        source_id = (failure.get("source") or {}).get("id")
        meta = item_index.get(source_id)
        case = per_case.get(meta["case_id"]) if meta else None
        if not case:
            continue
        case["ok"] = False
        error = failure.get("error") or {}
        msg = error.get("message", "")
        body = _decode_body(responses_by_item.get(source_id))
        reason = {
            "assertion": error.get("test") or failure.get("at") or "",
            "message": msg,
            "plain": plain_reason(msg),
            "hint": body_hint(body) if isinstance(body, dict) else None,
            "snippet": _mask_snippet(body),
            "kind": "other",
            # which call in the chain actually broke - a case can have several setup
            # steps plus the target, and a setup failure cascades into a confusing
            # target failure if you can't see which one failed first
            "step": meta["step_label"],
            "step_no": meta["step_no"],
            "is_target": meta["is_target"],
            # the actual payload/path params sent on the call that failed, so a failure
            # can be debugged without re-running the request by hand
            "request": _request_summary(requests_by_item.get(source_id)),
        }
        m = _STATUS_RE.search(msg)
        if m:
            reason["kind"] = "status"
            reason["expected_status"] = int(m.group(1))
            reason["got_status"] = int(m.group(2))
        case["reasons"].append(reason)
    for case in per_case.values():
        case["reasons"].sort(key=lambda r: r["step_no"])

    cases_out = list(per_case.values())
    passed = sum(1 for c in cases_out if c["ok"])
    return {"ok": passed == len(cases_out), "passed": passed, "total": len(cases_out), "cases": cases_out}


def summarize_collection(report: dict, unit_index: dict[str, dict]) -> dict:
    """Same idea as summarize(), but for a collection Atlas didn't generate - grouped by the
    collection's own top-level folders (unit_index: see postman_parse.unit_index) instead of
    our own case ids, since a third-party collection has no case/item_index of ours to use."""
    units: dict[str, dict] = {}
    for meta in unit_index.values():
        units.setdefault(meta["unit_id"], {"id": meta["unit_id"], "title": meta["unit_name"],
                                           "kind": "", "ok": True, "reasons": [], "response": None, "checks": []})

    run = report.get("run", {})
    responses_by_item = {}
    requests_by_item = {}
    for ex in run.get("executions", []):
        item_id = (ex.get("item") or {}).get("id")
        if item_id:
            responses_by_item[item_id] = ex.get("response")
            requests_by_item[item_id] = ex.get("request")
        meta = unit_index.get(item_id)
        if meta and any(a.get("error") for a in ex.get("assertions", [])):
            units[meta["unit_id"]]["ok"] = False

    for failure in run.get("failures", []):
        source_id = (failure.get("source") or {}).get("id")
        meta = unit_index.get(source_id)
        if not meta:
            continue
        unit = units[meta["unit_id"]]
        unit["ok"] = False
        error = failure.get("error") or {}
        msg = error.get("message", "")
        body = _decode_body(responses_by_item.get(source_id))
        reason = {
            "assertion": error.get("test") or failure.get("at") or "",
            "message": msg,
            "plain": plain_reason(msg),
            "hint": body_hint(body) if isinstance(body, dict) else None,
            "snippet": _mask_snippet(body),
            "kind": "other",
            # which request inside the folder broke - a folder can hold several requests
            "step": meta["request_name"],
            # the actual payload/path params sent on the call that failed
            "request": _request_summary(requests_by_item.get(source_id)),
        }
        m = _STATUS_RE.search(msg)
        if m:
            reason["kind"] = "status"
            reason["expected_status"] = int(m.group(1))
            reason["got_status"] = int(m.group(2))
        unit["reasons"].append(reason)

    units_out = list(units.values())
    passed = sum(1 for u in units_out if u["ok"])
    return {"ok": passed == len(units_out), "passed": passed, "total": len(units_out), "cases": units_out}
