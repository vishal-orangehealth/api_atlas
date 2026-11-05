"""End-to-end test of the changes -> verify -> PR flow: a real local bare git remote standing in
for GitHub, and a fake GitHub API for PR creation (ATLAS_GITHUB_API override). No real network.
"""
import json
import os
import subprocess

import requests

from atlas.engine.callmodel import build_call
from atlas.server import create_app
from atlas.store import Store
from fake_github import FakeGitHub
from flow_helpers import ServerThread, free_port

COLLECTION = {
    "info": {"name": "Shop", "schema": "https://schema.getpostman.com/json/collection/v2.1.0/collection.json"},
    "variable": [{"key": "apiBase", "value": "https://shop.com"}],
    "item": [{"name": "Existing", "request": {"method": "GET",
             "url": {"raw": "{{apiBase}}/api/existing", "host": ["{{apiBase}}"], "path": ["api", "existing"]}}}],
}
ENVIRONMENT = {"name": "Shop env", "values": [{"key": "apiBase", "value": "https://shop.com", "enabled": True}]}


def _git(root, *args, check=True):
    env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t.co",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t.co", "GIT_TERMINAL_PROMPT": "0"}
    return subprocess.run(["git", "-C", str(root), *args], check=check, capture_output=True, text=True, env=env)


def _seed_session(app):
    store, rec = app.state.store, app.state.recorder
    s = store.create_session("Shop", "https://shop.com", ["shop.com"])
    c = build_call(method="GET", url="https://shop.com/api/me", req_headers={"content-type": "application/json"},
                   req_data=None, status=200, resp_headers={"content-type": "application/json"},
                   resp_data=json.dumps({"id": 1, "name": "Vi"}), ts=1.0)
    rec.ingest(s["id"], c)
    return s["id"]


def _make_repo_with_fake_github_remote(tmp_path, remote_url):
    """A local clone whose origin *looks like* a GitHub URL (so PR creation can parse owner/repo)
    but is actually rewritten, via git's own insteadOf config, to a local bare repo - so fetch and
    push work for real with no network at all."""
    work = tmp_path / "work"
    work.mkdir()
    _git(work, "init", "-q", "-b", "main")
    (work / "shop.postman_collection.json").write_text(json.dumps(COLLECTION, indent=2) + "\n")
    (work / "shop.postman_environment.json").write_text(json.dumps(ENVIRONMENT, indent=2) + "\n")
    _git(work, "add", ".")
    _git(work, "commit", "-q", "-m", "init")
    bare = tmp_path / "origin.git"
    subprocess.run(["git", "clone", "-q", "--bare", str(work), str(bare)], check=True, capture_output=True)

    local = tmp_path / "local"
    subprocess.run(["git", "clone", "-q", str(bare), str(local)], check=True, capture_output=True)
    _git(local, "remote", "set-url", "origin", remote_url)
    _git(local, "config", f"url.{bare}.insteadOf", remote_url)
    return local, bare


def test_full_changes_flow_against_local_remote_and_fake_github(tmp_path, monkeypatch):
    monkeypatch.setenv("ATLAS_HOME", str(tmp_path / "home"))
    local, bare = _make_repo_with_fake_github_remote(tmp_path, "https://github.com/acme/shop.git")

    app = create_app(Store(tmp_path / "t.db"))
    sid = _seed_session(app)
    port = free_port()
    base = f"http://127.0.0.1:{port}"
    with ServerThread(app, port), FakeGitHub(status=201, body={"html_url": "https://github.com/acme/shop/pull/7"}) as gh:
        monkeypatch.setenv("ATLAS_GITHUB_API", gh.base_url)
        monkeypatch.setenv("GITHUB_TOKEN", "sekrit")

        p = requests.post(f"{base}/api/projects", json={"path": str(local)}).json()
        assert p["remote"] == "https://github.com/acme/shop.git"
        assert p["default_branch"] == "main"

        a = requests.get(f"{base}/api/sessions/{sid}/analysis").json()
        case_ids = [c["id"] for c in a["cases"] if c["selected"]]
        assert case_ids

        cov_before = requests.get(f"{base}/api/projects/{p['id']}/coverage",
                                  params={"collection": "shop.postman_collection.json", "sid": sid}).json()
        assert cov_before["summary"]["not_covered"] == 1  # GET /api/me not covered by the hand-written collection yet

        r = requests.post(f"{base}/api/projects/{p['id']}/changes", json={
            "sid": sid, "case_ids": case_ids, "flow_name": "Me flow",
            "target": {"mode": "existing", "collection": "shop.postman_collection.json",
                      "env": "shop.postman_environment.json"},
        })
        assert r.ok, r.text
        change = r.json()
        assert change["branch"].startswith("atlas/me-flow-")
        assert change["has_remote"] is True
        assert "Me flow" in change["diff"]
        assert change["warnings"] == []
        assert change["coverage_before"]["not_covered"] == 1
        assert change["coverage_after"]["not_covered"] == 0  # the new folder now covers it

        # the real local checkout is completely untouched - everything happened in a worktree
        status = _git(local, "status", "--porcelain").stdout
        assert status.strip() == ""
        assert _git(local, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip() == "main"

        sub = requests.post(f"{base}/api/projects/{p['id']}/changes/{change['change_id']}/submit")
        assert sub.ok, sub.text
        result = sub.json()
        assert result == {"ok": True, "url": "https://github.com/acme/shop/pull/7", "method": "token", "error": None}

        # the bare "GitHub" remote really has the new branch with our commit
        branches = _git(bare, "branch", "--list", change["branch"]).stdout
        assert change["branch"] in branches
        show = _git(bare, "log", change["branch"], "-1", "--name-only").stdout
        assert "shop.postman_collection.json" in show

        # the fake GitHub API really received the PR request
        assert len(gh.requests) == 1
        assert gh.requests[0]["body"]["base"] == "main"
        assert gh.requests[0]["body"]["head"] == change["branch"]


def test_changes_requires_at_least_one_selected_case(tmp_path, monkeypatch):
    monkeypatch.setenv("ATLAS_HOME", str(tmp_path / "home"))
    local, _bare = _make_repo_with_fake_github_remote(tmp_path, "https://github.com/acme/shop.git")
    app = create_app(Store(tmp_path / "t.db"))
    sid = _seed_session(app)
    port = free_port()
    base = f"http://127.0.0.1:{port}"
    with ServerThread(app, port):
        p = requests.post(f"{base}/api/projects", json={"path": str(local)}).json()
        r = requests.post(f"{base}/api/projects/{p['id']}/changes", json={
            "sid": sid, "case_ids": [], "flow_name": "x",
            "target": {"mode": "existing", "collection": "shop.postman_collection.json"},
        })
        assert r.status_code == 400


def test_submit_without_a_preview_fails_cleanly(tmp_path, monkeypatch):
    monkeypatch.setenv("ATLAS_HOME", str(tmp_path / "home"))
    local, _bare = _make_repo_with_fake_github_remote(tmp_path, "https://github.com/acme/shop.git")
    app = create_app(Store(tmp_path / "t.db"))
    port = free_port()
    base = f"http://127.0.0.1:{port}"
    with ServerThread(app, port):
        p = requests.post(f"{base}/api/projects", json={"path": str(local)}).json()
        r = requests.post(f"{base}/api/projects/{p['id']}/changes/not-a-real-id/submit")
        assert r.status_code == 404
