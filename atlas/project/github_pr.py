"""Open a pull request for a pushed branch: GITHUB_TOKEN REST API, then `gh`, then a plain
compare link as a last resort. We never store a token - it's read from the environment for this
one call. ATLAS_GITHUB_API overrides the API base URL so tests can point this at a local fake
server instead of a real GitHub.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import urllib.error
import urllib.request

from .git_ops import parse_owner_repo


def api_base() -> str:
    return os.environ.get("ATLAS_GITHUB_API", "https://api.github.com")


def compare_url(owner: str, repo: str, base: str, branch: str) -> str:
    return f"https://github.com/{owner}/{repo}/compare/{base}...{branch}?expand=1"


def _via_token(owner: str, repo: str, base: str, branch: str, title: str, body: str, token: str) -> dict:
    url = f"{api_base()}/repos/{owner}/{repo}/pulls"
    payload = json.dumps({"title": title, "head": branch, "base": base, "body": body}).encode()
    req = urllib.request.Request(url, data=payload, method="POST", headers={
        "Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json",
        "Content-Type": "application/json", "User-Agent": "api-atlas",
    })
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            data = json.loads(r.read())
        return {"ok": True, "url": data.get("html_url"), "method": "token", "error": None}
    except urllib.error.HTTPError as e:
        detail = e.read().decode(errors="ignore")
        return {"ok": False, "error": f"GitHub API returned {e.code}: {detail[:500]}"}
    except urllib.error.URLError as e:
        return {"ok": False, "error": str(e.reason)}


def _via_gh(owner: str, repo: str, base: str, branch: str, title: str, body: str) -> dict | None:
    gh = shutil.which("gh")
    if not gh:
        return None
    try:
        r = subprocess.run([gh, "pr", "create", "--repo", f"{owner}/{repo}", "--base", base,
                            "--head", branch, "--title", title, "--body", body],
                           capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.TimeoutExpired) as e:
        return {"ok": False, "error": str(e)}
    if r.returncode != 0:
        return {"ok": False, "error": r.stderr.strip() or r.stdout.strip()}
    lines = [ln for ln in r.stdout.strip().splitlines() if ln.startswith("http")]
    return {"ok": True, "url": lines[-1] if lines else r.stdout.strip(), "method": "gh", "error": None}


def open_pr(remote_url: str, base: str, branch: str, title: str, body: str) -> dict:
    """{"ok", "url", "method", "error"}. ok=False with a url means: no PR was auto-created, but
    here's a compare link the user can open by hand."""
    parsed = parse_owner_repo(remote_url) if remote_url else None
    if not parsed:
        return {"ok": False, "url": None, "method": None,
                "error": "Could not determine the GitHub owner/repo from the remote URL."}
    owner, repo = parsed
    token = os.environ.get("GITHUB_TOKEN")
    last_error = None
    if token:
        result = _via_token(owner, repo, base, branch, title, body, token)
        if result["ok"]:
            return result
        last_error = result["error"]
    gh_result = _via_gh(owner, repo, base, branch, title, body)
    if gh_result and gh_result["ok"]:
        return gh_result
    return {"ok": False, "method": "compare_link", "url": compare_url(owner, repo, base, branch),
            "error": last_error or (gh_result or {}).get("error")}
