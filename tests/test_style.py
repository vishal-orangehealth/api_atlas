"""Unit tests for detecting a repo's own Postman variable/scope conventions."""
from atlas.project.style import detect_base_url_var, detect_style, detect_token_var, detect_variable_scope

COLLECTION = {
    "info": {"name": "Shop"},
    "variable": [{"key": "apiBase", "value": "https://api.shop.co"}],
    "item": [
        {"name": "Auth", "item": [
            {"name": "Login", "event": [{"listen": "test", "script": {"exec": [
                "pm.environment.set('authToken', pm.response.json().token);"]}}],
             "request": {"method": "POST", "url": {"raw": "{{apiBase}}/api/auth/login"}}},
        ]},
        {"name": "Me", "request": {"method": "GET", "url": {"raw": "{{apiBase}}/api/me"},
         "header": [{"key": "Authorization", "value": "Bearer {{authToken}}"}]}},
    ],
}

COLLECTION_COLLECTION_SCOPE = {
    "info": {"name": "Shop2"},
    "item": [{"name": "x", "event": [{"listen": "test", "script": {"exec": [
        "pm.collectionVariables.set('id', 1);", "pm.collectionVariables.set('id2', 2);"]}}],
              "request": {"method": "GET", "url": "{{host}}/x"}}],
}

COLLECTION_NO_HINTS = {"info": {"name": "Empty"}, "item": []}


def test_detect_base_url_var_from_most_common_request_prefix():
    assert detect_base_url_var(COLLECTION) == "apiBase"


def test_detect_token_var_from_authorization_header():
    assert detect_token_var(COLLECTION) == "authToken"


def test_detect_variable_scope_prefers_more_frequent_convention():
    assert detect_variable_scope(COLLECTION_COLLECTION_SCOPE) == "collection"
    assert detect_variable_scope(COLLECTION) == "environment"


def test_detect_variable_scope_defaults_to_environment_with_no_scripts():
    assert detect_variable_scope(COLLECTION_NO_HINTS) == "environment"


def test_detect_style_falls_back_to_collection_variable_names_when_no_requests():
    style = detect_style(COLLECTION_NO_HINTS)
    assert style == {"base_url_var": "baseUrl", "token_var": None, "variable_scope": "environment"}


def test_detect_style_bundle():
    style = detect_style(COLLECTION)
    assert style == {"base_url_var": "apiBase", "token_var": "authToken", "variable_scope": "environment"}
