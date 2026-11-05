"""Unit tests for the projects table (open/clone project bookkeeping)."""
from atlas.store import Store


def test_create_and_list_projects(tmp_path):
    store = Store(tmp_path / "t.db")
    p = store.create_project("Shop API", "/repos/shop", "https://github.com/acme/shop.git", "main")
    assert p["name"] == "Shop API"
    assert p["path"] == "/repos/shop"
    assert p["remote"] == "https://github.com/acme/shop.git"
    assert p["default_branch"] == "main"
    assert store.get_project(p["id"]) == p
    assert store.list_projects() == [p]


def test_update_and_delete_project(tmp_path):
    store = Store(tmp_path / "t.db")
    p = store.create_project("Local only", "/repos/local", None, None)
    store.update_project(p["id"], default_branch="master")
    assert store.get_project(p["id"])["default_branch"] == "master"
    store.delete_project(p["id"])
    assert store.get_project(p["id"]) is None


def test_get_unknown_project_is_none(tmp_path):
    store = Store(tmp_path / "t.db")
    assert store.get_project("nope") is None
