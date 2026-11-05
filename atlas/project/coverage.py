"""Match a repo's Postman requests to explored endpoints by method + path shape, and badge coverage.

Matching ignores host and parameter *names* - Postman collections rarely name path variables the
same way our analysis does ({{productId}} vs {product_id}) - and compares only the method plus
which segments are literal vs variable. "happy path only" vs "happy + negative" is a heuristic:
a shape with more than one matching request is assumed to cover more than the happy path too,
since real suites usually group a validation/negative case right next to the happy one.
"""
from __future__ import annotations

NOT_COVERED, HAPPY_ONLY, HAPPY_AND_NEGATIVE = "not_covered", "happy_only", "happy_and_negative"


def endpoint_shape(method: str, template: str) -> str:
    segs = [s for s in template.split("/") if s]
    parts = ["*" if s.startswith("{") and s.endswith("}") else s for s in segs]
    return f"{method.upper()} /" + "/".join(parts)


def postman_request_shape(req: dict) -> str:
    parts = ["*" if p else s for s, p in zip(req["path_segments"], req["is_param"])]
    return f"{req['method'].upper()} /" + "/".join(parts)


def compute_coverage(endpoints: list[dict], postman_requests: list[dict]) -> list[dict]:
    """endpoints: analysis endpoint dicts (key, method, template, ...).
    postman_requests: postman_parse.requests_in() output.
    Returns one row per endpoint: {endpoint, method, template, badge, matches: [name, ...]}."""
    by_shape: dict[str, list[dict]] = {}
    for req in postman_requests:
        by_shape.setdefault(postman_request_shape(req), []).append(req)

    rows = []
    for ep in endpoints:
        shape = endpoint_shape(ep["method"], ep["template"])
        matches = by_shape.get(shape, [])
        if not matches:
            badge = NOT_COVERED
        elif len(matches) == 1:
            badge = HAPPY_ONLY
        else:
            badge = HAPPY_AND_NEGATIVE
        rows.append({
            "endpoint": ep["key"], "method": ep["method"], "template": ep["template"],
            "badge": badge, "matches": [m["name"] for m in matches],
        })
    return rows


def collection_endpoints(postman_requests: list[dict]) -> list[dict]:
    """Synthesize an "endpoints" list straight from a collection's own requests, deduped by
    shape - lets coverage be computed against a collection with no exploration involved at
    all. Every endpoint here is covered by construction (it was derived from a real request),
    so compute_coverage() against these never produces not_covered - only happy_only vs
    happy_and_negative, i.e. "does this endpoint have more than one test case already."
    """
    seen: dict[str, dict] = {}
    for req in postman_requests:
        shape = postman_request_shape(req)
        if shape not in seen:
            parts = ["{param}" if p else s for s, p in zip(req["path_segments"], req["is_param"])]
            template = "/" + "/".join(parts)
            seen[shape] = {"key": shape, "method": req["method"].upper(), "template": template}
    return list(seen.values())


def summary(rows: list[dict]) -> dict:
    counts = {NOT_COVERED: 0, HAPPY_ONLY: 0, HAPPY_AND_NEGATIVE: 0}
    for r in rows:
        counts[r["badge"]] += 1
    return {"total": len(rows), **counts}
