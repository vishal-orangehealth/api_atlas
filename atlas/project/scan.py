"""Scan a local repo for Postman collections/environments, its Newman command, and git info."""
from __future__ import annotations

import json
import os
import re
import subprocess
from pathlib import Path

SKIP_DIRS = {".git", "node_modules", "dist", "build", "venv", ".venv", "__pycache__",
             ".pytest_cache", ".next", "coverage", "vendor"}
MAX_FILE_BYTES = 5_000_000
NEWMAN_RE = re.compile(r"\bnewman\s+run\b")
CI_FILES = [".github/workflows", ".gitlab-ci.yml", "Jenkinsfile", "azure-pipelines.yml",
            "circle.yml", ".circleci/config.yml"]


def git_env() -> dict:
    return {**os.environ, "GIT_TERMINAL_PROMPT": "0"}


def git(root: Path, *args: str) -> str | None:
    """Run a local, read-only git command; None on any failure (not a repo, no remote, etc.)."""
    try:
        r = subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True,
                           timeout=15, env=git_env())
    except (OSError, subprocess.TimeoutExpired):
        return None
    return r.stdout.strip() if r.returncode == 0 and r.stdout.strip() else None


def git_info(root: Path) -> dict:
    is_repo = git(root, "rev-parse", "--is-inside-work-tree") == "true"
    if not is_repo:
        return {"is_repo": False, "remote": None, "default_branch": None, "current_branch": None}
    # "config --get", not "remote get-url": the latter applies any url.*.insteadOf rewrite the
    # user has configured, which is the right behavior for git itself but not for reporting "this
    # is the remote" - we want the URL as configured (and as shown on GitHub), not a local mirror.
    remote = git(root, "config", "--get", "remote.origin.url")
    default_branch = None
    head_ref = git(root, "symbolic-ref", "refs/remotes/origin/HEAD")
    if head_ref and head_ref.startswith("refs/remotes/origin/"):
        default_branch = head_ref[len("refs/remotes/origin/"):]
    if not default_branch:
        for candidate in ("main", "master"):
            if git(root, "show-ref", "--verify", f"refs/heads/{candidate}"):
                default_branch = candidate
                break
    current_branch = git(root, "rev-parse", "--abbrev-ref", "HEAD")
    return {"is_repo": True, "remote": remote, "default_branch": default_branch or current_branch,
            "current_branch": current_branch}


def _iter_files(root: Path):
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS and not d.startswith(".")
                       or d in (".github", ".circleci")]
        for name in filenames:
            yield Path(dirpath) / name


def _read_json(path: Path) -> dict | None:
    try:
        if path.stat().st_size > MAX_FILE_BYTES:
            return None
        return json.loads(path.read_text(encoding="utf-8", errors="ignore"))
    except (OSError, ValueError):
        return None


def find_postman_files(root: Path) -> dict:
    """{"collections": [{"path", "name"}], "environments": [{"path", "name"}]} - paths relative to root."""
    collections, environments = [], []
    for path in _iter_files(root):
        if path.suffix != ".json":
            continue
        name = path.name
        is_named_collection = name.endswith(".postman_collection.json")
        is_named_environment = name.endswith(".postman_environment.json")
        if not (is_named_collection or is_named_environment):
            continue  # cheap pre-filter before reading file content
        data = _read_json(path)
        if data is None:
            continue
        rel = str(path.relative_to(root))
        if is_named_collection and isinstance(data.get("item"), list):
            collections.append({"path": rel, "name": data.get("info", {}).get("name") or path.stem})
        elif is_named_environment and isinstance(data.get("values"), list):
            environments.append({"path": rel, "name": data.get("name") or path.stem})
    return {"collections": collections, "environments": environments}


def detect_newman_command(root: Path) -> str | None:
    pkg = root / "package.json"
    if pkg.exists():
        data = _read_json(pkg) or {}
        for script_name, cmd in (data.get("scripts") or {}).items():
            if NEWMAN_RE.search(cmd):
                return f"npm run {script_name}"
    for rel in CI_FILES:
        p = root / rel
        candidates = [p] if p.is_file() else (list(p.glob("*.yml")) + list(p.glob("*.yaml")) if p.is_dir() else [])
        for c in candidates:
            try:
                text = c.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                continue
            m = NEWMAN_RE.search(text)
            if m:
                line = next((ln.strip() for ln in text.splitlines() if "newman run" in ln), m.group(0))
                return line
    return None


def scan(root: Path) -> dict:
    root = Path(root)
    files = find_postman_files(root)
    return {**files, "newman_command": detect_newman_command(root), "git": git_info(root)}
