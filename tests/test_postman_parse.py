"""Unit tests for the tolerant Postman v2.0/v2.1 collection reader."""
from atlas.project.postman_parse import environment_vars, requests_in, top_level_folders, unit_index

COLLECTION_V21 = {
    "info": {"name": "Shop API"},
    "item": [
        {"name": "Auth", "item": [
            {"name": "Login", "request": {"method": "POST",
             "url": {"raw": "{{baseUrl}}/api/auth/login", "host": ["{{baseUrl}}"],
                     "path": ["api", "auth", "login"]}}},
        ]},
        {"name": "Products", "item": [
            {"name": "Get product", "request": {"method": "GET",
             "url": {"raw": "{{baseUrl}}/api/products/:productId", "host": ["{{baseUrl}}"],
                     "path": ["api", "products", ":productId"]}}},
            {"name": "Get product 404", "request": {"method": "GET",
             "url": {"raw": "{{baseUrl}}/api/products/:productId", "host": ["{{baseUrl}}"],
                     "path": ["api", "products", ":productId"]}}},
        ]},
    ],
}

# Older-style export: url as a plain string, variable segment via {{var}}
COLLECTION_V20_STRING_URL = {
    "info": {"name": "Legacy"},
    "item": [
        {"name": "List orders", "request": {"method": "GET", "url": "https://api.shop.co/orders/{{userId}}"}},
    ],
}

ENVIRONMENT = {"values": [{"key": "baseUrl", "value": "https://api.shop.co"},
                          {"key": "token", "value": ""}]}

# A real export always has item ids - used for unit_index tests specifically
COLLECTION_WITH_IDS = {
    "info": {"name": "Shop API"},
    "item": [
        {"name": "Auth", "id": "folder-auth", "item": [
            {"name": "Login", "id": "req-login", "request": {"method": "POST", "url": "{{baseUrl}}/api/auth/login"}},
            {"name": "Logout", "id": "req-logout", "request": {"method": "POST", "url": "{{baseUrl}}/api/auth/logout"}},
        ]},
        {"name": "Health check", "id": "req-health", "request": {"method": "GET", "url": "{{baseUrl}}/health"}},
    ],
}


def test_requests_in_walks_nested_folders_and_tracks_folder_path():
    reqs = requests_in(COLLECTION_V21)
    assert len(reqs) == 3
    login = next(r for r in reqs if r["name"] == "Login")
    assert login["method"] == "POST"
    assert login["folder_path"] == ["Auth"]
    assert login["path_segments"] == ["api", "auth", "login"]
    assert login["is_param"] == [False, False, False]


def test_path_variable_segment_detected():
    reqs = requests_in(COLLECTION_V21)
    product = next(r for r in reqs if r["name"] == "Get product")
    assert product["path_segments"] == ["api", "products", ":productId"]
    assert product["is_param"] == [False, False, True]


def test_string_url_with_double_brace_variable_parses():
    reqs = requests_in(COLLECTION_V20_STRING_URL)
    assert len(reqs) == 1
    r = reqs[0]
    assert r["method"] == "GET"
    assert r["path_segments"] == ["orders", "{{userId}}"]
    assert r["is_param"] == [False, True]


def test_top_level_folders_lists_only_folders():
    folders = top_level_folders(COLLECTION_V21)
    assert [f["name"] for f in folders] == ["Auth", "Products"]


def test_environment_vars_reads_key_value_pairs():
    assert environment_vars(ENVIRONMENT) == {"baseUrl": "https://api.shop.co", "token": ""}


def test_unit_index_groups_nested_requests_by_top_level_folder():
    idx = unit_index(COLLECTION_WITH_IDS)
    assert idx["req-login"] == {"unit_id": "folder:Auth", "unit_name": "Auth", "request_name": "Login"}
    assert idx["req-logout"] == {"unit_id": "folder:Auth", "unit_name": "Auth", "request_name": "Logout"}


def test_unit_index_bare_top_level_request_is_its_own_unit():
    idx = unit_index(COLLECTION_WITH_IDS)
    assert idx["req-health"] == {"unit_id": "req-health", "unit_name": "Health check",
                                 "request_name": "Health check"}


def test_unit_index_request_name_distinguishes_requests_within_a_folder():
    """Two different requests in the same folder are the same unit, but a failure inside one of
    them should still be able to say which request it was."""
    idx = unit_index(COLLECTION_WITH_IDS)
    assert idx["req-login"]["request_name"] != idx["req-logout"]["request_name"]
    assert idx["req-login"]["unit_id"] == idx["req-logout"]["unit_id"]
