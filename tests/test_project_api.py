"""Integration tests for project mode: folder picker, open/clone, scan, coverage."""
import json
import os
import subprocess
from pathlib import Path

import requests

from atlas.engine.callmodel import build_call
from atlas.server import create_app
from atlas.store import Store
from flow_helpers import ServerThread, free_port

COLLECTION = {
    "info": {"name": "Shop"},
    "item": [{"name": "Me", "request": {"method": "GET", "url": {"raw": "{{baseUrl}}/api/me",
             "host": ["{{baseUrl}}"], "path": ["api", "me"]}}}],
}


def _git(root, *args):
    env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t.co",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t.co"}
    subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True, env=env)


def _seed_session(app):
    store, rec = app.state.store, app.state.recorder
    s = store.create_session("Shop", "https://shop.com", ["shop.com"])
    c = build_call(method="GET", url="https://shop.com/api/me", req_headers={"content-type": "application/json"},
                   req_data=None, status=200, resp_headers={"content-type": "application/json"},
                   resp_data=json.dumps({"id": 1}), ts=1.0)
    rec.ingest(s["id"], c)
    return s["id"]


def test_fs_list_returns_subdirectories(tmp_path):
    (tmp_path / "repo-a").mkdir()
    (tmp_path / "repo-b").mkdir()
    (tmp_path / ".hidden").mkdir()
    (tmp_path / "a-file.txt").write_text("x")
    app = create_app(Store(tmp_path / "t.db"))
    port = free_port()
    base = f"http://127.0.0.1:{port}"
    with ServerThread(app, port):
        r = requests.get(f"{base}/api/fs/list", params={"path": str(tmp_path)})
        assert r.ok
        names = {e["name"] for e in r.json()["entries"]}
        assert names == {"repo-a", "repo-b"}


def test_open_local_project_folder(tmp_path):
    repo = tmp_path / "myrepo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    (repo / "x.txt").write_text("x")
    _git(repo, "add", "x.txt")
    _git(repo, "commit", "-q", "-m", "init")
    app = create_app(Store(tmp_path / "t.db"))
    port = free_port()
    base = f"http://127.0.0.1:{port}"
    with ServerThread(app, port):
        r = requests.post(f"{base}/api/projects", json={"path": str(repo)})
        assert r.ok, r.text
        p = r.json()
        assert p["name"] == "myrepo"
        assert p["path"] == str(repo)
        assert p["default_branch"] == "main"
        assert requests.get(f"{base}/api/projects").json() == [p]


def test_open_missing_local_folder_fails(tmp_path):
    app = create_app(Store(tmp_path / "t.db"))
    port = free_port()
    base = f"http://127.0.0.1:{port}"
    with ServerThread(app, port):
        r = requests.post(f"{base}/api/projects", json={"path": str(tmp_path / "nope")})
        assert r.status_code == 400


def test_clone_project_from_local_bare_remote(tmp_path, monkeypatch):
    monkeypatch.setenv("ATLAS_HOME", str(tmp_path / "home"))
    # build a tiny "GitHub" repo locally: a working copy with a collection, pushed to a bare remote
    work = tmp_path / "work"
    work.mkdir()
    _git(work, "init", "-q", "-b", "main")
    (work / "shop.postman_collection.json").write_text(json.dumps(COLLECTION))
    _git(work, "add", ".")
    _git(work, "commit", "-q", "-m", "init")
    bare = tmp_path / "shop.git"
    subprocess.run(["git", "clone", "-q", "--bare", str(work), str(bare)], check=True, capture_output=True)

    app = create_app(Store(tmp_path / "t.db"))
    port = free_port()
    base = f"http://127.0.0.1:{port}"
    with ServerThread(app, port):
        r = requests.post(f"{base}/api/projects", json={"clone_url": str(bare), "name": "Shop"})
        assert r.ok, r.text
        p = r.json()
        assert p["remote"] == str(bare)
        assert p["default_branch"] == "main"
        cloned_path = p["path"]
        assert (tmp_path / "home" / "projects").exists()

        detail = requests.get(f"{base}/api/projects/{p['id']}").json()
        assert detail["collections"] == [{"path": "shop.postman_collection.json", "name": "Shop"}]
        assert detail["git"]["remote"] == str(bare)

        # cloning again with the same name reuses the existing clone instead of erroring
        r2 = requests.post(f"{base}/api/projects", json={"clone_url": str(bare), "name": "Shop"})
        assert r2.ok
        assert r2.json()["path"] == cloned_path


def test_clone_bad_url_fails_cleanly(tmp_path, monkeypatch):
    monkeypatch.setenv("ATLAS_HOME", str(tmp_path / "home"))
    app = create_app(Store(tmp_path / "t.db"))
    port = free_port()
    base = f"http://127.0.0.1:{port}"
    with ServerThread(app, port):
        r = requests.post(f"{base}/api/projects", json={"clone_url": str(tmp_path / "not-a-repo"), "name": "x"})
        assert r.status_code == 400
        assert "git clone failed" in r.json()["detail"]


def test_project_coverage_matches_against_an_explored_session(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "shop.postman_collection.json").write_text(json.dumps(COLLECTION))
    app = create_app(Store(tmp_path / "t.db"))
    sid = _seed_session(app)
    port = free_port()
    base = f"http://127.0.0.1:{port}"
    with ServerThread(app, port):
        p = requests.post(f"{base}/api/projects", json={"path": str(repo)}).json()
        r = requests.get(f"{base}/api/projects/{p['id']}/coverage",
                         params={"collection": "shop.postman_collection.json", "sid": sid})
        assert r.ok, r.text
        data = r.json()
        row = next(c for c in data["coverage"] if c["endpoint"].endswith("/api/me"))
        assert row["badge"] == "happy_only"
        assert data["style"]["base_url_var"] == "baseUrl"
        assert data["folders"] == []


def test_coverage_rejects_path_traversal(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (tmp_path / "outside.postman_collection.json").write_text(json.dumps(COLLECTION))
    app = create_app(Store(tmp_path / "t.db"))
    sid = _seed_session(app)
    port = free_port()
    base = f"http://127.0.0.1:{port}"
    with ServerThread(app, port):
        p = requests.post(f"{base}/api/projects", json={"path": str(repo)}).json()
        r = requests.get(f"{base}/api/projects/{p['id']}/coverage",
                         params={"collection": "../outside.postman_collection.json", "sid": sid})
        assert r.status_code == 400


def test_case_script_tags_calls_already_in_the_collection(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "shop.postman_collection.json").write_text(json.dumps(COLLECTION))
    app = create_app(Store(tmp_path / "t.db"))
    store, rec = app.state.store, app.state.recorder
    s = store.create_session("Shop", "https://shop.com", ["shop.com"])
    for method, url in (("GET", "https://shop.com/api/me"), ("GET", "https://shop.com/api/orders")):
        c = build_call(method=method, url=url, req_headers={"content-type": "application/json"}, req_data=None,
                       status=200, resp_headers={"content-type": "application/json"},
                       resp_data=json.dumps({"id": 1}), ts=1.0)
        rec.ingest(s["id"], c)
    sid = s["id"]
    port = free_port()
    base = f"http://127.0.0.1:{port}"
    with ServerThread(app, port):
        p = requests.post(f"{base}/api/projects", json={"path": str(repo)}).json()
        a = requests.get(f"{base}/api/sessions/{sid}/analysis").json()
        me = next(c for c in a["cases"] if c["endpoint"].endswith("/api/me") and c["kind"] == "happy")
        orders = next(c for c in a["cases"] if c["endpoint"].endswith("/api/orders") and c["kind"] == "happy")

        r = requests.get(f"{base}/api/projects/{p['id']}/case-script",
                         params={"sid": sid, "case_id": me["id"], "collection": "shop.postman_collection.json"})
        assert r.ok, r.text
        items = r.json()["items"]
        assert len(items) == 1
        assert items[0]["already_in_collection"] is True
        assert items[0]["request"]["method"] == "GET"

        r2 = requests.get(f"{base}/api/projects/{p['id']}/case-script",
                          params={"sid": sid, "case_id": orders["id"], "collection": "shop.postman_collection.json"})
        assert r2.json()["items"][0]["already_in_collection"] is False


def test_case_script_unknown_case_404s(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "shop.postman_collection.json").write_text(json.dumps(COLLECTION))
    app = create_app(Store(tmp_path / "t.db"))
    sid = _seed_session(app)
    port = free_port()
    base = f"http://127.0.0.1:{port}"
    with ServerThread(app, port):
        p = requests.post(f"{base}/api/projects", json={"path": str(repo)}).json()
        r = requests.get(f"{base}/api/projects/{p['id']}/case-script",
                         params={"sid": sid, "case_id": "nope", "collection": "shop.postman_collection.json"})
        assert r.status_code == 404


COLLECTION_WITH_IDS = {
    "info": {"name": "Shop"},
    "item": [
        {"name": "Products", "id": "folder-products", "item": [
            {"name": "List products", "id": "req-list", "request": {"method": "GET",
             "url": {"raw": "{{baseUrl}}/api/products", "host": ["{{baseUrl}}"], "path": ["api", "products"]}}},
            {"name": "Get product", "id": "req-get", "request": {"method": "GET",
             "url": {"raw": "{{baseUrl}}/api/products/:id", "host": ["{{baseUrl}}"], "path": ["api", "products", ":id"]}}},
            {"name": "Get product 404", "id": "req-get-404", "request": {"method": "GET",
             "url": {"raw": "{{baseUrl}}/api/products/:id", "host": ["{{baseUrl}}"], "path": ["api", "products", ":id"]}}},
        ]},
        {"name": "Health check", "id": "req-health", "request": {"method": "GET",
         "url": {"raw": "{{baseUrl}}/health", "host": ["{{baseUrl}}"], "path": ["health"]}}},
    ],
}


def test_self_coverage_needs_no_exploration(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "shop.postman_collection.json").write_text(json.dumps(COLLECTION_WITH_IDS))
    app = create_app(Store(tmp_path / "t.db"))
    port = free_port()
    base = f"http://127.0.0.1:{port}"
    with ServerThread(app, port):
        p = requests.post(f"{base}/api/projects", json={"path": str(repo)}).json()
        r = requests.get(f"{base}/api/projects/{p['id']}/self-coverage",
                         params={"collection": "shop.postman_collection.json"})
        assert r.ok, r.text
        data = r.json()
        # List products has one request for its shape -> happy_only; Get product has two -> happy_and_negative
        assert data["summary"] == {"total": 3, "not_covered": 0, "happy_only": 2, "happy_and_negative": 1}
        badge_by_template = {row["template"]: row["badge"] for row in data["coverage"]}
        assert badge_by_template["/api/products/{param}"] == "happy_and_negative"
        assert badge_by_template["/api/products"] == "happy_only"
        assert badge_by_template["/health"] == "happy_only"


def test_run_collection_groups_results_by_top_level_folder(tmp_path, monkeypatch):
    """Runs the real endpoint with subprocess.run faked, so it needs no real Newman/Node -
    proves the collection is run exactly as it exists on disk, no Atlas generation involved."""
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "shop.postman_collection.json").write_text(json.dumps(COLLECTION_WITH_IDS))
    app = create_app(Store(tmp_path / "t.db"))
    port = free_port()
    base = f"http://127.0.0.1:{port}"
    with ServerThread(app, port):
        # create the project (and its git_info subprocess.run calls) before faking run/which
        p = requests.post(f"{base}/api/projects", json={"path": str(repo)}).json()

        monkeypatch.setattr("atlas.server.shutil.which", lambda name: "/usr/bin/fake-newman" if name == "newman" else None)

        def fake_run(cmd, capture_output, text, cwd, timeout):
            report_path = Path(cmd[cmd.index("--reporter-json-export") + 1])
            report = {"run": {
                "executions": [
                    {"item": {"id": "req-list"}, "response": {"code": 200}, "assertions": []},
                    {"item": {"id": "req-get"}, "response": {"code": 500},
                     "assertions": [{"assertion": "status is 200", "skipped": False,
                                     "error": {"message": "expected response to have status code 200 but got 500"}}]},
                ],
                "failures": [{"error": {"message": "expected response to have status code 200 but got 500",
                                        "test": "status is 200"}, "at": "assertion:0 in test-script",
                              "source": {"id": "req-get"}}],
            }}
            report_path.write_text(json.dumps(report))

            class R:
                returncode = 1
                stdout = stderr = ""
            return R()

        monkeypatch.setattr("atlas.server.subprocess.run", fake_run)
        r = requests.post(f"{base}/api/projects/{p['id']}/run-collection",
                          params={"collection": "shop.postman_collection.json"}, json={})
        assert r.ok, r.text
        result = r.json()
        assert result["total"] == 2  # "Products" folder + the bare "Health check" request
        products = next(u for u in result["cases"] if u["title"] == "Products")
        assert products["ok"] is False
        assert products["reasons"][0]["plain"] == "Expected 200, got 500"
        health = next(u for u in result["cases"] if u["title"] == "Health check")
        assert health["ok"] is True


def test_run_collection_without_newman_gives_install_hint(tmp_path, monkeypatch):
    monkeypatch.setattr("atlas.server.shutil.which", lambda name: None)
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "shop.postman_collection.json").write_text(json.dumps(COLLECTION_WITH_IDS))
    app = create_app(Store(tmp_path / "t.db"))
    port = free_port()
    base = f"http://127.0.0.1:{port}"
    with ServerThread(app, port):
        p = requests.post(f"{base}/api/projects", json={"path": str(repo)}).json()
        r = requests.post(f"{base}/api/projects/{p['id']}/run-collection",
                          params={"collection": "shop.postman_collection.json"}, json={})
        assert r.status_code == 400
        assert "newman" in r.json()["detail"].lower()


def test_delete_project(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    app = create_app(Store(tmp_path / "t.db"))
    port = free_port()
    base = f"http://127.0.0.1:{port}"
    with ServerThread(app, port):
        p = requests.post(f"{base}/api/projects", json={"path": str(repo)}).json()
        assert requests.delete(f"{base}/api/projects/{p['id']}").ok
        assert requests.get(f"{base}/api/projects/{p['id']}").status_code == 404
