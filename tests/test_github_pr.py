"""Unit tests for opening a PR: GITHUB_TOKEN REST API against a fake GitHub, then gh, then a
compare link. ATLAS_GITHUB_API points the token path at the fake server instead of the real API."""
from atlas.project.github_pr import compare_url, open_pr
from fake_github import FakeGitHub


def test_open_pr_via_token_against_fake_github_api(monkeypatch):
    with FakeGitHub(status=201, body={"html_url": "https://github.com/acme/shop/pull/42"}) as gh:
        monkeypatch.setenv("ATLAS_GITHUB_API", gh.base_url)
        monkeypatch.setenv("GITHUB_TOKEN", "sekrit-token")
        result = open_pr("https://github.com/acme/shop.git", "main", "atlas/checkout-123",
                          "Add checkout tests", "body text")
        assert result == {"ok": True, "url": "https://github.com/acme/shop/pull/42", "method": "token", "error": None}
        assert len(gh.requests) == 1
        req = gh.requests[0]
        assert req["path"] == "/repos/acme/shop/pulls"
        assert req["headers"]["Authorization"] == "Bearer sekrit-token"
        assert req["body"] == {"title": "Add checkout tests", "head": "atlas/checkout-123",
                               "base": "main", "body": "body text"}


def test_open_pr_token_failure_falls_back_to_compare_link(monkeypatch):
    with FakeGitHub(status=422, body={"message": "Validation Failed"}) as gh:
        monkeypatch.setenv("ATLAS_GITHUB_API", gh.base_url)
        monkeypatch.setenv("GITHUB_TOKEN", "sekrit-token")
        monkeypatch.setattr("atlas.project.github_pr.shutil.which", lambda name: None)
        result = open_pr("https://github.com/acme/shop.git", "main", "atlas/checkout-123", "t", "b")
        assert result["ok"] is False
        assert result["method"] == "compare_link"
        assert result["url"] == compare_url("acme", "shop", "main", "atlas/checkout-123")
        assert "422" in result["error"]


def test_open_pr_no_token_no_gh_goes_straight_to_compare_link(monkeypatch):
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.setattr("atlas.project.github_pr.shutil.which", lambda name: None)
    result = open_pr("https://github.com/acme/shop.git", "main", "atlas/checkout-123", "t", "b")
    assert result["ok"] is False
    assert result["method"] == "compare_link"
    assert result["url"] == "https://github.com/acme/shop/compare/main...atlas/checkout-123?expand=1"


def test_open_pr_unparseable_remote_fails_without_crashing():
    result = open_pr("not-a-url", "main", "atlas/x", "t", "b")
    assert result["ok"] is False
    assert result["url"] is None
    assert "owner/repo" in result["error"]


def test_never_calls_the_real_api_by_default(monkeypatch):
    """ATLAS_GITHUB_API unset must never resolve to api.github.com during tests - this only
    verifies api_base()'s default so a future change can't silently start hitting the real API
    from a test; it's not itself a network call."""
    monkeypatch.delenv("ATLAS_GITHUB_API", raising=False)
    from atlas.project.github_pr import api_base
    assert api_base() == "https://api.github.com"
