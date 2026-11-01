"""Find where values flow between calls.

Two kinds of links:
  response: a value returned by an earlier call is sent by a later call
            (login token -> Authorization header, orderId -> /orders/{id})
  reuse:    a value first typed into an earlier request is sent again later
            (email used in register, then again in login)
"""
from __future__ import annotations


def _ok(producer: dict, consumer: dict) -> bool:
    """Short values (like id 5) only link when both ends look like identifiers."""
    if producer["weak"] or consumer["weak"]:
        consumer_idish = consumer["idk"] or consumer["loc"]["part"] == "path"
        return producer["idk"] and consumer_idish
    return True


def find_links(calls: list[dict]) -> list[dict]:
    """calls: ordered list of dicts with "id" and "fingerprints"."""
    links: list[dict] = []
    first_seen: dict[str, tuple[str, int, dict]] = {}
    resp_index: dict[str, list[tuple[int, dict]]] = {}
    req_index: dict[str, list[tuple[int, dict]]] = {}

    for call in calls:
        cid = call["id"]
        fps = call.get("fingerprints") or []
        req_fps = [f for f in fps if f["loc"]["side"] == "req"]
        resp_fps = [f for f in fps if f["loc"]["side"] == "resp"]
        seen_here: set[tuple] = set()

        for fp in req_fps:
            h = fp["h"]
            origin = first_seen.get(h)
            if origin is None:
                continue
            link = None
            if origin[0] == "req":
                cands = [c for c in req_index.get(h, []) if c[0] != cid and _ok(c[1], fp)]
                if cands:
                    src = cands[0]  # earliest request that introduced the value
                    link = {"kind": "reuse", "from": src[0], "from_loc": src[1]["loc"], "from_idk": src[1]["idk"]}
            if link is None:
                cands = [c for c in resp_index.get(h, []) if c[0] != cid and _ok(c[1], fp)]
                if cands:
                    # cookies: the response that first set it; everything else: most recent producer
                    src = cands[0] if fp["loc"]["part"] == "cookie" else cands[-1]
                    link = {"kind": "response", "from": src[0], "from_loc": src[1]["loc"], "from_idk": src[1]["idk"]}
            if link:
                key = (str(fp["loc"]), link["from"])
                if key in seen_here:
                    continue
                seen_here.add(key)
                link.update({"to": cid, "to_loc": fp["loc"]})
                links.append(link)

        for fp in req_fps:
            req_index.setdefault(fp["h"], []).append((cid, fp))
            first_seen.setdefault(fp["h"], ("req", cid, fp["loc"]))
        for fp in resp_fps:
            resp_index.setdefault(fp["h"], []).append((cid, fp))
            first_seen.setdefault(fp["h"], ("resp", cid, fp["loc"]))
    return links


def auth_kind(call: dict) -> str | None:
    """How a request authenticates: "bearer", "header", "cookie" or None."""
    headers = call.get("req_headers", {})
    if "authorization" in headers:
        return "bearer"
    for name in headers:
        if name in ("x-api-key", "api-key", "apikey", "x-auth-token", "x-access-token", "x-session-token"):
            return "header"
    if "cookie" in headers:
        return "cookie"
    return None
