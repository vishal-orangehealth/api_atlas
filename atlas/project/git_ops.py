"""Git plumbing for the changes -> verify -> PR flow. Every call is explicit and scoped to a
throwaway worktree: we never touch the user's own checkout, and GIT_TERMINAL_PROMPT=0 means a
git command that would need interactive auth fails fast instead of hanging.
"""
from __future__ import annotations

import os
import re
import subprocess
import time
from pathlib import Path

from .scan import git_env


class GitError(RuntimeError):
    pass


def _run(args: list[str], cwd: Path | None = None, timeout: int = 60) -> str:
    try:
        r = subprocess.run(["git", *args], cwd=str(cwd) if cwd else None, capture_output=True,
                           text=True, timeout=timeout, env=git_env())
    except subprocess.TimeoutExpired as e:
        raise GitError(f"git {' '.join(args)} timed out") from e
    except FileNotFoundError as e:
        raise GitError("git isn't installed") from e
    if r.returncode != 0:
        raise GitError(r.stderr.strip() or r.stdout.strip() or f"git {' '.join(args)} failed")
    return r.stdout


def fetch(root: Path) -> None:
    _run(["-C", str(root), "fetch", "origin"], timeout=120)


def branch_name(flow: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", flow.lower()).strip("-")[:40] or "flow"
    return f"atlas/{slug}-{int(time.time())}"


def create_worktree(root: Path, worktrees_dir: Path, branch: str, base_ref: str) -> Path:
    """New worktree on a new branch from base_ref (e.g. 'origin/main'). Never touches root's own
    working tree - `git worktree add` always checks out into a fresh directory."""
    worktrees_dir.mkdir(parents=True, exist_ok=True)
    path = worktrees_dir / branch.replace("/", "__")
    _run(["-C", str(root), "worktree", "add", "-b", branch, str(path), base_ref])
    return path


def remove_worktree(root: Path, worktree_path: Path) -> None:
    try:
        _run(["-C", str(root), "worktree", "remove", "--force", str(worktree_path)])
    except GitError:
        pass  # best-effort cleanup


def diff(worktree_path: Path) -> str:
    """Stage everything and diff against HEAD, so new (untracked) files show up too, not just
    modified ones - `git diff` alone is silent about brand-new files."""
    _run(["-C", str(worktree_path), "add", "-A"])
    return _run(["-C", str(worktree_path), "diff", "--cached"])


def commit_all(worktree_path: Path, message: str) -> None:
    _run(["-C", str(worktree_path), "add", "-A"])
    _run(["-C", str(worktree_path), "commit", "-q", "-m", message])


def push(worktree_path: Path, branch: str) -> None:
    _run(["-C", str(worktree_path), "push", "-u", "origin", branch], timeout=120)


_SSH_RE = re.compile(r"^git@[^:]+:(?P<owner>[^/]+)/(?P<repo>.+?)(?:\.git)?/?$")
_HTTPS_RE = re.compile(r"^https?://[^/]+/(?P<owner>[^/]+)/(?P<repo>.+?)(?:\.git)?/?$")


def parse_owner_repo(remote_url: str) -> tuple[str, str] | None:
    for pattern in (_SSH_RE, _HTTPS_RE):
        m = pattern.match(remote_url.strip())
        if m:
            return m.group("owner"), m.group("repo")
    return None
