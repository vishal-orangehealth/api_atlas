"""Unit tests for turning a Newman JSON report into plain-English pass/fail per case."""
from atlas.engine.newman_report import body_hint, plain_reason, summarize, summarize_collection
from atlas.gen.postman_gen import item_index
from atlas.project.postman_parse import unit_index

CASES = [
    {"id": "c1", "title": "GET /api/products returns 200", "kind": "happy", "endpoint": "/api/products",
     "setup": []},
    {"id": "c2", "title": "POST /api/auth/register returns 201", "kind": "happy", "endpoint": "/api/auth/register",
     "setup": [{}]},
]


def test_plain_reason_rewrites_status_mismatch():
    msg = "expected response to have status code 201 but got 409"
    assert plain_reason(msg) == "Expected 201, got 409"


def test_plain_reason_falls_back_to_capitalized_message():
    assert plain_reason("expected body to be an array") == "Expected body to be an array"


def test_body_hint_picks_first_known_key():
    assert body_hint({"detail": "email already exists"}) == "email already exists"
    assert body_hint({"message": "nope", "detail": "yes"}) == "yes"
    assert body_hint({"other": "x"}) is None
    assert body_hint("not a dict") is None


def test_summarize_all_passed_when_no_failures():
    idx = item_index(CASES)
    target_c1 = next(k for k, v in idx.items() if v["case_id"] == "c1" and v["is_target"])
    report = {"run": {"executions": [
        {"item": {"id": target_c1}, "response": {}, "assertions": [{"assertion": "status is 200", "skipped": False}]},
    ], "failures": []}}
    result = summarize(report, CASES, idx)
    assert result["ok"] is True
    assert result["passed"] == 2  # c2 has no executions at all -> stays "ok" by default
    assert {c["id"] for c in result["cases"]} == {"c1", "c2"}


def test_summarize_captures_response_and_checks_for_a_passing_case():
    """A pass shouldn't be a bare checkmark - the response (status, headers, body) and which
    assertions ran are captured for the target step regardless of pass/fail."""
    idx = item_index(CASES)
    target_c1 = next(k for k, v in idx.items() if v["case_id"] == "c1" and v["is_target"])
    body_bytes = list(b'{"items": [1, 2], "total": 2}')
    report = {"run": {"executions": [
        {"item": {"id": target_c1},
         "response": {"code": 200, "header": [{"key": "Authorization", "value": "Bearer abc123"},
                                               {"key": "content-type", "value": "application/json"}],
                      "stream": {"type": "Buffer", "data": body_bytes}},
         "assertions": [{"assertion": "status is 200", "skipped": False},
                        {"assertion": "total is integer", "skipped": False}]},
    ], "failures": []}}
    result = summarize(report, CASES, idx)
    c1 = next(c for c in result["cases"] if c["id"] == "c1")
    assert c1["ok"] is True
    assert c1["response"]["status"] == 200
    assert '"total": 2' in c1["response"]["snippet"]
    assert c1["checks"] == [{"name": "status is 200", "ok": True}, {"name": "total is integer", "ok": True}]
    # the Authorization header value is masked even on a pass - this is still a secret
    auth = next(h for h in c1["response"]["headers"] if h["key"] == "Authorization")
    assert "abc123" not in auth["value"]


def test_summarize_reports_status_mismatch_with_body_hint():
    idx = item_index(CASES)
    target_c2 = next(k for k, v in idx.items() if v["case_id"] == "c2" and v["is_target"])
    body_bytes = list(b'{"detail": "email already exists"}')
    report = {"run": {
        "executions": [
            {"item": {"id": target_c2},
             "response": {"code": 409, "stream": {"type": "Buffer", "data": body_bytes}},
             "assertions": [{"assertion": "status is 201", "skipped": False,
                              "error": {"message": "expected response to have status code 201 but got 409",
                                        "test": "status is 201"}}]},
        ],
        "failures": [
            {"error": {"message": "expected response to have status code 201 but got 409", "test": "status is 201"},
             "at": "assertion:0 in test-script", "source": {"id": target_c2}},
        ],
    }}
    result = summarize(report, CASES, idx)
    assert result["ok"] is False
    assert result["passed"] == 1
    c2 = next(c for c in result["cases"] if c["id"] == "c2")
    assert c2["ok"] is False
    assert len(c2["reasons"]) == 1
    reason = c2["reasons"][0]
    assert reason["plain"] == "Expected 201, got 409"
    assert reason["kind"] == "status"
    assert reason["expected_status"] == 201
    assert reason["got_status"] == 409
    assert reason["hint"] == "email already exists"
    assert "email already exists" in reason["snippet"]
    c1 = next(c for c in result["cases"] if c["id"] == "c1")
    assert c1["ok"] is True
    assert c1["reasons"] == []


CHAIN_CASE = [
    {"id": "c3", "title": "GET /api/orders/{id} returns 200", "kind": "happy", "endpoint": "/api/orders/{id}",
     "setup": [{"label": "POST /api/auth/login"}, {"label": "POST /api/orders"}],
     "target": {"label": "GET /api/orders/{id}"}},
]


def test_summarize_labels_which_step_in_the_chain_actually_failed():
    """A case can have several setup calls plus the target. If an early setup call breaks
    (e.g. login returns 500), the target then fails too for an unrelated-looking reason
    (e.g. missing token -> 401) - without a step label there's no way to tell the two apart,
    which was the actual confusion this test guards against."""
    idx = item_index(CHAIN_CASE)
    login_id = next(k for k, v in idx.items() if v["case_id"] == "c3" and v["step_no"] == 1)
    target_id = next(k for k, v in idx.items() if v["case_id"] == "c3" and v["is_target"])
    report = {"run": {
        "executions": [
            {"item": {"id": login_id}, "response": {"code": 500},
             "assertions": [{"assertion": "status is 200", "skipped": False,
                             "error": {"message": "expected response to have status code 200 but got 500"}}]},
            {"item": {"id": target_id}, "response": {"code": 401},
             "assertions": [{"assertion": "status is 200", "skipped": False,
                             "error": {"message": "expected response to have status code 200 but got 401"}}]},
        ],
        "failures": [
            {"error": {"message": "expected response to have status code 200 but got 500"},
             "at": "assertion:0 in test-script", "source": {"id": login_id}},
            {"error": {"message": "expected response to have status code 200 but got 401"},
             "at": "assertion:0 in test-script", "source": {"id": target_id}},
        ],
    }}
    result = summarize(report, CHAIN_CASE, idx)
    c3 = next(c for c in result["cases"] if c["id"] == "c3")
    assert len(c3["reasons"]) == 2
    setup_reason, target_reason = c3["reasons"]
    assert setup_reason["step"] == "POST /api/auth/login"
    assert setup_reason["is_target"] is False
    assert setup_reason["got_status"] == 500
    assert target_reason["step"] == "GET /api/orders/{id}"
    assert target_reason["is_target"] is True
    assert target_reason["got_status"] == 401


def test_summarize_captures_the_request_that_was_actually_sent():
    """A failure needs to show what payload / path param triggered it, not just what came
    back - otherwise debugging means re-running the request by hand to find out."""
    idx = item_index(CHAIN_CASE)
    target_id = next(k for k, v in idx.items() if v["case_id"] == "c3" and v["is_target"])
    report = {"run": {
        "executions": [
            {"item": {"id": target_id},
             "request": {"method": "GET",
                         "url": {"path": ["api", "orders", "42"], "query": []},
                         "header": [{"key": "Authorization", "value": "Bearer sometoken123"}],
                         "body": None},
             "response": {"code": 401},
             "assertions": [{"assertion": "status is 200", "skipped": False,
                             "error": {"message": "expected response to have status code 200 but got 401"}}]},
        ],
        "failures": [
            {"error": {"message": "expected response to have status code 200 but got 401"},
             "at": "assertion:0 in test-script", "source": {"id": target_id}},
        ],
    }}
    result = summarize(report, CHAIN_CASE, idx)
    c3 = next(c for c in result["cases"] if c["id"] == "c3")
    assert c3["request"]["method"] == "GET"
    assert c3["request"]["path"] == "/api/orders/42"
    reason = c3["reasons"][0]
    assert reason["request"]["method"] == "GET"
    assert reason["request"]["path"] == "/api/orders/42"
    # the Authorization header is still masked in the request we show back
    auth = next(h for h in reason["request"]["headers"] if h["key"] == "Authorization")
    assert "sometoken123" not in auth["value"]


def test_summarize_captures_request_body_for_a_failed_post():
    idx = item_index(CASES)
    target_c2 = next(k for k, v in idx.items() if v["case_id"] == "c2" and v["is_target"])
    report = {"run": {
        "executions": [
            {"item": {"id": target_c2},
             "request": {"method": "POST", "url": {"path": ["api", "auth", "register"], "query": []},
                         "header": [], "body": {"mode": "raw", "raw": '{"email": "a@b.com", "password": "hunter2"}'}},
             "response": {"code": 409},
             "assertions": [{"assertion": "status is 201", "skipped": False,
                             "error": {"message": "expected response to have status code 201 but got 409"}}]},
        ],
        "failures": [
            {"error": {"message": "expected response to have status code 201 but got 409"},
             "at": "assertion:0 in test-script", "source": {"id": target_c2}},
        ],
    }}
    result = summarize(report, CASES, idx)
    c2 = next(c for c in result["cases"] if c["id"] == "c2")
    reason = c2["reasons"][0]
    assert reason["request"]["path"] == "/api/auth/register"
    assert "<masked:email>" in reason["request"]["snippet"]
    assert "<masked:secret>" in reason["request"]["snippet"]
    assert "hunter2" not in reason["request"]["snippet"]


def test_summarize_masks_secrets_in_snippet():
    idx = item_index(CASES)
    target_c1 = next(k for k, v in idx.items() if v["case_id"] == "c1" and v["is_target"])
    body_bytes = list(b'{"token": "abc123", "detail": "bad creds"}')
    report = {"run": {
        "executions": [{"item": {"id": target_c1},
                        "response": {"code": 401, "stream": {"type": "Buffer", "data": body_bytes}},
                        "assertions": [{"assertion": "status is 200", "skipped": False,
                                        "error": {"message": "expected response to have status code 200 but got 401"}}]}],
        "failures": [{"error": {"message": "expected response to have status code 200 but got 401"},
                      "at": "assertion:0 in test-script", "source": {"id": target_c1}}],
    }}
    result = summarize(report, CASES, idx)
    c1 = next(c for c in result["cases"] if c["id"] == "c1")
    assert "abc123" not in c1["reasons"][0]["snippet"]
    assert "<masked:secret>" in c1["reasons"][0]["snippet"]


COLLECTION_WITH_IDS = {
    "info": {"name": "Shop"},
    "item": [
        {"name": "Auth", "id": "folder-auth", "item": [
            {"name": "Login", "id": "req-login", "request": {"method": "POST", "url": "{{baseUrl}}/api/auth/login"}},
        ]},
        {"name": "Health check", "id": "req-health", "request": {"method": "GET", "url": "{{baseUrl}}/health"}},
    ],
}


def test_summarize_collection_groups_by_top_level_folder():
    """Running a collection Atlas didn't generate - no case ids of ours, so pass/fail groups
    by the collection's own top-level folders instead."""
    idx = unit_index(COLLECTION_WITH_IDS)
    report = {"run": {
        "executions": [{"item": {"id": "req-login"}, "response": {},
                        "assertions": [{"assertion": "status is 200", "skipped": False}]}],
        "failures": [],
    }}
    result = summarize_collection(report, idx)
    assert result["total"] == 2  # "Auth" folder + the bare "Health check" request
    auth = next(u for u in result["cases"] if u["id"] == "folder:Auth")
    assert auth["ok"] is True
    health = next(u for u in result["cases"] if u["id"] == "req-health")
    assert health["ok"] is True  # no execution at all -> stays ok by default, same as summarize()


def test_summarize_collection_reports_a_folder_level_failure():
    idx = unit_index(COLLECTION_WITH_IDS)
    report = {"run": {
        "executions": [{"item": {"id": "req-login"}, "response": {"code": 500},
                        "assertions": [{"assertion": "status is 200", "skipped": False,
                                        "error": {"message": "expected response to have status code 200 but got 500"}}]}],
        "failures": [{"error": {"message": "expected response to have status code 200 but got 500",
                                "test": "status is 200"}, "at": "assertion:0 in test-script",
                      "source": {"id": "req-login"}}],
    }}
    result = summarize_collection(report, idx)
    assert result["ok"] is False
    auth = next(u for u in result["cases"] if u["id"] == "folder:Auth")
    assert auth["ok"] is False
    assert auth["title"] == "Auth"
    assert auth["reasons"][0]["plain"] == "Expected 200, got 500"
