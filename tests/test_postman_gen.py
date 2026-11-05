"""Unit tests for atlas/gen/postman_gen.py pieces used outside of a full generate() call."""
import json

from atlas.engine.analysis import analyse
from atlas.engine.callmodel import build_call
from atlas.engine.mask import mask_call
from atlas.engine.values import fingerprints
from atlas.gen import postman_gen
from atlas.gen.common import selected_cases

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


def _cases():
    calls = [
        _call("POST", "https://shop.com/api/auth/login", req={"email": "a@b.co", "password": "pw123456"},
             resp={"token": "tok"}, ts=1),
        _call("GET", "https://shop.com/api/me", resp={"id": 1, "name": "Vi"},
             headers={"authorization": "Bearer tok"}, ts=2),
    ]
    a = analyse(calls)
    return selected_cases(a, {}), a["hosts"]


def test_case_items_matches_what_generate_builds_for_that_case():
    cases, hosts = _cases()
    me_case = next(c for c in cases if c["endpoint"].endswith("/api/me"))
    files = postman_gen.generate(cases, hosts, "Shop")
    full = json.loads(next(v for k, v in files.items() if k.endswith(".postman_collection.json")))
    full_folder = next(f for f in full["item"] if f["name"] == me_case["title"])

    items = postman_gen.case_items(me_case)

    assert [i["id"] for i in items] == [i["id"] for i in full_folder["item"]]
    assert [i["name"] for i in items] == [i["name"] for i in full_folder["item"]]


def test_case_items_includes_setup_then_target():
    cases, _hosts = _cases()
    me_case = next(c for c in cases if c["endpoint"].endswith("/api/me"))
    items = postman_gen.case_items(me_case)
    assert items[-1]["name"].startswith("▶")  # the target step is marked
    assert all(not i["name"].startswith("▶") for i in items[:-1])
