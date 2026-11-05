"""Unit tests for scanning a local repo for Postman files, its Newman command, and git info."""
import json
import os
import subprocess

from atlas.project.scan import detect_newman_command, find_postman_files, git_info, scan


def _git(root, *args):
    env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t.co",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t.co"}
    subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True, env=env)


def test_find_postman_files_by_extension_and_shape(tmp_path):
    (tmp_path / "shop.postman_collection.json").write_text(json.dumps({"info": {"name": "Shop"}, "item": []}))
    (tmp_path / "shop.postman_environment.json").write_text(json.dumps({"name": "Shop Env", "values": []}))
    (tmp_path / "unrelated.json").write_text(json.dumps({"hello": "world"}))
    (tmp_path / "node_modules").mkdir()
    (tmp_path / "node_modules" / "pkg.postman_collection.json").write_text(json.dumps({"item": []}))
    found = find_postman_files(tmp_path)
    assert [c["name"] for c in found["collections"]] == ["Shop"]
    assert [e["name"] for e in found["environments"]] == ["Shop Env"]


def test_find_postman_files_ignores_misnamed_or_malformed_json(tmp_path):
    (tmp_path / "broken.postman_collection.json").write_text("{not json")
    (tmp_path / "no_item.postman_collection.json").write_text(json.dumps({"info": {}}))  # missing "item"
    found = find_postman_files(tmp_path)
    assert found["collections"] == []


def test_detect_newman_command_from_package_json(tmp_path):
    (tmp_path / "package.json").write_text(json.dumps({"scripts": {"test:api": "newman run collection.json"}}))
    assert detect_newman_command(tmp_path) == "npm run test:api"


def test_detect_newman_command_from_ci_workflow(tmp_path):
    wf = tmp_path / ".github" / "workflows"
    wf.mkdir(parents=True)
    (wf / "ci.yml").write_text("steps:\n  - run: newman run collection.json -e env.json\n")
    assert "newman run" in detect_newman_command(tmp_path)


def test_detect_newman_command_absent(tmp_path):
    assert detect_newman_command(tmp_path) is None


def test_git_info_outside_a_repo(tmp_path):
    info = git_info(tmp_path)
    assert info == {"is_repo": False, "remote": None, "default_branch": None, "current_branch": None}


def test_git_info_reads_remote_and_default_branch(tmp_path):
    _git(tmp_path, "init", "-q", "-b", "main")
    (tmp_path / "README.md").write_text("hi")
    _git(tmp_path, "add", "README.md")
    _git(tmp_path, "commit", "-q", "-m", "init")
    _git(tmp_path, "remote", "add", "origin", "https://github.com/acme/shop.git")
    info = git_info(tmp_path)
    assert info["is_repo"] is True
    assert info["remote"] == "https://github.com/acme/shop.git"
    assert info["current_branch"] == "main"
    # no origin/HEAD without a real remote, so it falls back to a local main/master branch
    assert info["default_branch"] == "main"


def test_scan_combines_everything(tmp_path):
    (tmp_path / "shop.postman_collection.json").write_text(json.dumps({"info": {"name": "Shop"}, "item": []}))
    _git(tmp_path, "init", "-q")
    result = scan(tmp_path)
    assert result["collections"][0]["name"] == "Shop"
    assert result["environments"] == []
    assert result["git"]["is_repo"] is True
