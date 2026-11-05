"""Unit tests for the engine: filtering, masking, normalisation, links, test cases."""
import json

from atlas.engine.analysis import analyse
from atlas.engine.callmodel import build_call
from atlas.engine.filter import base_domain, classify, default_allowed_hosts
from atlas.engine.mask import mask_call
from atlas.engine.values import fingerprints

SALT = b"test-salt"
JWT = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NSJ9.abcdefghijklmnop"


def call(method, url, *, req=None, resp=None, status=200, headers=None, resp_headers=None, ts=0.0):
    h = {"content-type": "application/json", **(headers or {})}
    rh = {"content-type": "application/json", **(resp_headers or {})}
    return build_call(method=method, url=url, req_headers=h,
                      req_data=json.dumps(req) if req is not None else None,
                      status=status, resp_headers=rh,
                      resp_data=json.dumps(resp) if resp is not None else None, ts=ts)


def stored(calls):
    """What the store hands to analyse(): masked calls + fingerprints of the raw ones."""
    out = []
    for i, c in enumerate(calls, 1):
        fps = fingerprints(c, SALT)
        m = mask_call(c)
        m.update({"id": i, "seq": i, "fingerprints": fps})
        out.append(m)
    return out


# ------------------------------------------------------------------ callmodel

def test_http2_pseudo_headers_are_dropped():
    """Playwright's allHeaders() includes HTTP/2 pseudo-headers; neither `requests` nor Node's
    http client accept a header name with a colon, so generated tests would crash on them."""
    c = build_call(method="GET", url="https://api.shop.co/me",
                   req_headers={":authority": "api.shop.co", ":method": "GET", ":path": "/me",
                                ":scheme": "https", "authorization": "Bearer tok"},
                   req_data=None, status=200,
                   resp_headers={":status": "200", "content-type": "application/json"},
                   resp_data=json.dumps({"ok": True}))
    assert not any(k.startswith(":") for k in c["req_headers"])
    assert not any(k.startswith(":") for k in c["resp_headers"])
    assert c["req_headers"]["authorization"] == "Bearer tok"


# ------------------------------------------------------------------ filter

def test_noise_and_hosts():
    allowed = default_allowed_hosts("https://app.shop.co.in/login")
    assert allowed == ["shop.co.in"]
    assert base_domain("api.eu.example.com") == "example.com"
    kw = dict(method="GET", resource_type="fetch", status=200, response_content_type="application/json",
              allowed_hosts=allowed)
    assert classify(url="https://api.shop.co.in/v1/me", **kw) == "keep"
    assert classify(url="https://cdn.other.com/x.json", **kw) == "hidden"
    assert classify(url="https://www.google-analytics.com/g/collect", **kw) == "drop"
    assert classify(url="https://api.shop.co.in/rum", **kw) == "drop"
    assert classify(url="https://api.shop.co.in/v1/me", **{**kw, "method": "OPTIONS"}) == "drop"
    assert classify(url="https://api.shop.co.in/logo.png", **{**kw, "resource_type": "image"}) == "drop"
    assert classify(url="https://api.shop.co.in/page", **{**kw, "response_content_type": "text/html"}) == "drop"
    # Next.js's own dev-server error overlay, not a real endpoint of the app being tested
    assert classify(url="https://api.shop.co.in/__nextjs_original-stack-frames", **kw) == "drop"
    assert classify(url="https://api.shop.co.in/__nextjs_launch-editor", **kw) == "drop"


# ------------------------------------------------------------------ masking

def test_masking_never_leaks_secrets():
    c = call("POST", "https://x.com/api/login?api_key=abc123456",
             req={"email": "vishal@example.com", "password": "hunter2!", "card": {"number": "4111111111111111",
                  "cvv": "123"}, "pan": "ABCDE1234F", "mobile": "+91 9876543210", "note": "hi"},
             resp={"token": JWT, "user": {"email": "vishal@example.com", "id": 7}, "tokenType": "Bearer"},
             headers={"authorization": f"Bearer {JWT}", "cookie": "sid=s3cr3t; theme=dark",
                      "x-api-key": "k-123456"},
             resp_headers={"set-cookie": "sid=newsecret; HttpOnly"})
    m = mask_call(c)
    blob = json.dumps(m)
    for secret in ("vishal@example.com", "hunter2!", "4111111111111111", "ABCDE1234F", "9876543210",
                   JWT, "s3cr3t", "k-123456", "newsecret", "abc123456"):
        assert secret not in blob, secret
    assert m["req_body"]["note"] == "hi"
    assert m["resp_body"]["user"]["id"] == 7
    assert m["resp_body"]["tokenType"] == "Bearer"          # not a secret
    assert m["req_headers"]["authorization"] == "Bearer <masked:secret>"
    assert m["req_body"]["email"] == "<masked:email>"


def test_fingerprints_hold_no_raw_values():
    c = call("POST", "https://x.com/api/login", req={"password": "hunter2!"}, resp={"token": JWT})
    blob = json.dumps(fingerprints(c, SALT))
    assert "hunter2" not in blob and JWT not in blob


# ------------------------------------------------------------------ normalisation + links

def flow():
    return stored([
        call("POST", "https://shop.com/api/auth/register", status=201,
             req={"email": "a@b.co", "password": "pw123456", "username": "vishal"}, resp={"id": 1001}, ts=1),
        call("POST", "https://shop.com/api/auth/login", req={"email": "a@b.co", "password": "pw123456"},
             resp={"data": {"accessToken": JWT}}, ts=2),
        call("GET", "https://shop.com/api/products", resp={"items": [{"id": 5, "qty": 5}, {"id": 9, "qty": 1}]},
             headers={"authorization": f"Bearer {JWT}"}, ts=3),
        call("GET", "https://shop.com/api/products/9", resp={"id": 9, "name": "Hub"},
             headers={"authorization": f"Bearer {JWT}"}, ts=4),
        call("POST", "https://shop.com/api/orders", status=201, req={"productId": 9, "quantity": 1},
             resp={"orderId": "3f1c2a7e-0d4b-4a4e-9d0f-6f4b8b8f1c2a", "status": "PLACED"},
             headers={"authorization": f"Bearer {JWT}"}, ts=5),
        call("GET", "https://shop.com/api/orders/3f1c2a7e-0d4b-4a4e-9d0f-6f4b8b8f1c2a",
             resp={"orderId": "3f1c2a7e-0d4b-4a4e-9d0f-6f4b8b8f1c2a", "status": "PLACED"},
             headers={"authorization": f"Bearer {JWT}"}, ts=6),
    ])


def test_endpoints_are_templated():
    a = analyse(flow())
    keys = [e["key"] for e in a["endpoints"]]
    assert "GET shop.com/api/products/{product_id}" in keys
    assert "GET shop.com/api/orders/{order_id}" in keys
    assert "POST shop.com/api/auth/login" in keys           # literal words stay literal
    assert not any("api_id" in k for k in keys)


def test_links_found():
    a = analyse(flow())
    kinds = {(l["from"], tuple(l["from_loc"]["path"]), l["to"], l["to_loc"]["part"]) for l in a["links"]}
    assert (2, ("data", "accessToken"), 3, "header") in kinds         # token -> Authorization
    assert (5, ("orderId",), 6, "path") in kinds                       # orderId -> /orders/{id}
    assert (4, ("id",), 5, "body") in kinds                            # product id -> productId
    reuse = [l for l in a["links"] if l["kind"] == "reuse"]
    assert {tuple(l["to_loc"]["path"]) for l in reuse} >= {("email",), ("password",)}
    # quantity 1 must not be linked to anything (short value, not an id)
    assert not any(l["to"] == 5 and l["to_loc"]["path"] == ["quantity"] for l in a["links"])


def test_case_chain_and_variables():
    a = analyse(flow())
    happy = next(c for c in a["cases"] if c["endpoint"].endswith("/orders/{order_id}") and c["kind"] == "happy")
    labels = [s["label"] for s in happy["setup"]]
    assert labels[:2] == ["POST /api/auth/register", "POST /api/auth/login"]
    assert labels[-1] == "POST /api/orders"
    kinds = {n: v["kind"] for n, v in happy["vars"].items()}
    assert kinds.get("email") == "unique"                  # sign-up gets a fresh email each run
    assert happy["vars"]["password"]["kind"] == "env"
    assert happy["vars"]["password"]["default"]           # sign-up password has a default
    assert happy["needs_env"] == []
    assert any(v["kind"] == "extract" for v in happy["vars"].values())
    assert happy["selected"] is True


def test_old_recordings_with_http2_pseudo_headers_still_generate_clean_requests():
    """build_call() now strips these at capture time, but sessions recorded before that fix
    still have them stored - analysis must drop them too so generated tests stay runnable."""
    calls = flow()
    calls[2]["req_headers"][":authority"] = "shop.com"
    a = analyse(calls)
    products = next(c for c in a["cases"] if c["endpoint"].endswith("/api/products") and c["kind"] == "happy")
    assert not any(k.startswith(":") for k in products["target"]["headers"])


def test_dynamic_values_not_asserted():
    a = analyse(flow())
    happy = next(c for c in a["cases"] if c["endpoint"].endswith("/orders/{order_id}") and c["kind"] == "happy")
    for asrt in happy["target"]["assertions"]:
        if asrt["path"] == ["orderId"]:
            assert asrt["check"] == "type"


def test_negative_cases_are_opt_in():
    a = analyse(flow())
    for c in a["cases"]:
        assert c["selected"] == (c["kind"] == "happy")
