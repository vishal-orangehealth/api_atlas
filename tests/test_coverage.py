"""Unit tests for matching Postman requests to explored endpoints and badging coverage."""
from atlas.project.coverage import collection_endpoints, compute_coverage, summary

ENDPOINTS = [
    {"key": "GET shop.com/api/products/{product_id}", "method": "GET",
     "template": "/api/products/{product_id}"},
    {"key": "POST shop.com/api/auth/login", "method": "POST", "template": "/api/auth/login"},
    {"key": "GET shop.com/api/orders", "method": "GET", "template": "/api/orders"},
]


def req(method, segs, is_param, name="x"):
    return {"name": name, "method": method, "path_segments": segs, "is_param": is_param}


def test_not_covered_when_no_matching_request():
    rows = compute_coverage(ENDPOINTS, [])
    assert {r["endpoint"]: r["badge"] for r in rows} == {
        "GET shop.com/api/products/{product_id}": "not_covered",
        "POST shop.com/api/auth/login": "not_covered",
        "GET shop.com/api/orders": "not_covered",
    }


def test_single_match_is_happy_only_regardless_of_param_name():
    # Postman names its variable :productId, ours is {product_id} - shape still matches
    requests = [req("GET", ["api", "products", ":productId"], [False, False, True], "Get product")]
    rows = compute_coverage(ENDPOINTS, requests)
    row = next(r for r in rows if "products" in r["endpoint"])
    assert row["badge"] == "happy_only"
    assert row["matches"] == ["Get product"]


def test_two_matches_for_same_shape_is_happy_and_negative():
    requests = [
        req("GET", ["api", "products", ":productId"], [False, False, True], "Get product"),
        req("GET", ["api", "products", ":productId"], [False, False, True], "Get product 404"),
    ]
    rows = compute_coverage(ENDPOINTS, requests)
    row = next(r for r in rows if "products" in r["endpoint"])
    assert row["badge"] == "happy_and_negative"
    assert set(row["matches"]) == {"Get product", "Get product 404"}


def test_literal_segments_must_match_exactly():
    # /api/orders (no params) should not match /api/products/:id
    requests = [req("GET", ["api", "products", ":productId"], [False, False, True])]
    rows = compute_coverage(ENDPOINTS, requests)
    orders_row = next(r for r in rows if r["endpoint"].endswith("/api/orders"))
    assert orders_row["badge"] == "not_covered"


def test_summary_counts_badges():
    requests = [req("POST", ["api", "auth", "login"], [False, False, False])]
    rows = compute_coverage(ENDPOINTS, requests)
    s = summary(rows)
    assert s == {"total": 3, "not_covered": 2, "happy_only": 1, "happy_and_negative": 0}


def test_collection_endpoints_dedupes_by_shape():
    requests = [
        req("GET", ["api", "products"], [False, False], "List products"),
        req("GET", ["api", "products", ":id"], [False, False, True], "Get product"),
        req("GET", ["api", "products", ":id"], [False, False, True], "Get product 404"),
    ]
    eps = collection_endpoints(requests)
    assert {(e["method"], e["template"]) for e in eps} == {("GET", "/api/products"), ("GET", "/api/products/{param}")}


def test_collection_endpoints_self_coverage_is_never_not_covered():
    """Every synthesized endpoint came from a real request, so self-coverage against the same
    collection can only ever be happy_only or happy_and_negative - never not_covered."""
    requests = [
        req("GET", ["api", "products"], [False, False], "List products"),
        req("GET", ["api", "products", ":id"], [False, False, True], "Get product"),
        req("GET", ["api", "products", ":id"], [False, False, True], "Get product 404"),
    ]
    eps = collection_endpoints(requests)
    rows = compute_coverage(eps, requests)
    badges = {r["badge"] for r in rows}
    assert "not_covered" not in badges
    s = summary(rows)
    assert s == {"total": 2, "not_covered": 0, "happy_only": 1, "happy_and_negative": 1}
