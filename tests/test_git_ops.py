"""Unit tests for the worktree/branch plumbing, against a real local git repo (no network)."""
import os
import subprocess

import pytest

from atlas.project.git_ops import GitError, branch_name, commit_all, create_worktree, diff, fetch, \
    parse_owner_repo, push, remove_worktree


def _git(root, *args):
    env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t.co",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t.co"}
    subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True, env=env)


@pytest.fixture
def repo_with_remote(tmp_path):
    """work (first commit) -> bare 'origin' -> local (a real clone with an origin remote)."""
    work = tmp_path / "work"
    work.mkdir()
    _git(work, "init", "-q", "-b", "main")
    (work / "shop.postman_collection.json").write_text('{"item": []}\n')
    _git(work, "add", ".")
    _git(work, "commit", "-q", "-m", "init")
    bare = tmp_path / "origin.git"
    subprocess.run(["git", "clone", "-q", "--bare", str(work), str(bare)], check=True, capture_output=True)
    local = tmp_path / "local"
    subprocess.run(["git", "clone", "-q", str(bare), str(local)], check=True, capture_output=True)
    return {"bare": bare, "local": local, "worktrees": tmp_path / "worktrees"}


def test_branch_name_is_slugged_and_unique_enough():
    a = branch_name("Checkout Flow!!")
    assert a.startswith("atlas/checkout-flow-")
    assert " " not in a and "!" not in a


def test_fetch_succeeds_against_a_real_remote(repo_with_remote):
    fetch(repo_with_remote["local"])  # no exception


def test_fetch_fails_cleanly_with_no_remote(tmp_path):
    _git(tmp_path, "init", "-q")
    with pytest.raises(GitError):
        fetch(tmp_path)


def test_create_worktree_checks_out_a_new_branch_from_origin_default(repo_with_remote):
    local, worktrees = repo_with_remote["local"], repo_with_remote["worktrees"]
    fetch(local)
    branch = branch_name("checkout")
    wt = create_worktree(local, worktrees, branch, "origin/main")
    assert wt.is_dir()
    assert (wt / "shop.postman_collection.json").exists()
    # the original checkout is untouched - still on main, no new files
    status = subprocess.run(["git", "-C", str(local), "status", "--porcelain"], capture_output=True, text=True)
    assert status.stdout.strip() == ""
    remove_worktree(local, wt)


def test_diff_shows_new_and_modified_files(repo_with_remote):
    local, worktrees = repo_with_remote["local"], repo_with_remote["worktrees"]
    fetch(local)
    wt = create_worktree(local, worktrees, branch_name("x"), "origin/main")
    (wt / "shop.postman_collection.json").write_text('{"item": [{"name": "new"}]}\n')
    (wt / "new_file.postman_environment.json").write_text('{"values": []}\n')
    d = diff(wt)
    assert "shop.postman_collection.json" in d
    assert "new_file.postman_environment.json" in d
    assert '"name": "new"' in d


def test_commit_and_push_a_new_branch(repo_with_remote):
    local, worktrees = repo_with_remote["local"], repo_with_remote["worktrees"]
    fetch(local)
    branch = branch_name("x")
    wt = create_worktree(local, worktrees, branch, "origin/main")
    (wt / "shop.postman_collection.json").write_text('{"item": [{"name": "new"}]}\n')
    diff(wt)  # stages the change, as the UI flow would before showing the diff
    commit_all(wt, "Add new flow")
    push(wt, branch)
    # the bare "origin" now has the branch
    refs = subprocess.run(["git", "-C", str(repo_with_remote["bare"]), "branch", "--list", branch],
                          capture_output=True, text=True).stdout
    assert branch in refs


def test_parse_owner_repo_https_and_ssh():
    assert parse_owner_repo("https://github.com/acme/shop.git") == ("acme", "shop")
    assert parse_owner_repo("https://github.com/acme/shop") == ("acme", "shop")
    assert parse_owner_repo("git@github.com:acme/shop.git") == ("acme", "shop")
    assert parse_owner_repo("not a url") is None
