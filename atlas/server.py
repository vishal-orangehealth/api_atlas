"""FastAPI app: REST + websocket API and the web UI."""
from __future__ import annotations

import asyncio
import io
import json
import os
import re
import shutil
import subprocess
import tempfile
import uuid
import zipfile
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import __version__
from .capture import Hub, Recorder, call_summary
from .engine import newman_report
from .engine.analysis import analyse
from .engine.filter import default_allowed_hosts
from .gen import postman_gen, pytest_gen
from .gen.common import selected_cases
from .project import coverage as coverage_mod
from .project import edit as edit_mod
from .project import git_ops
from .project import github_pr
from .project import scan as scan_mod
from .project import style as style_mod
from .project import style_adapt
from .project.postman_parse import environment_vars, requests_in, top_level_folders, unit_index
from .store import Store, default_home

STATIC = Path(__file__).parent / "static"


class StartIn(BaseModel):
    url: str
    name: str | None = None


class HostsIn(BaseModel):
    allowed_hosts: list[str]


class CasePatch(BaseModel):
    selected: bool | None = None
    title: str | None = None
    expect_status: int | None = None


class BulkSelect(BaseModel):
    case_ids: list[str]
    selected: bool


class RequestOverride(BaseModel):
    method: str | None = None
    url: str | None = None
    headers: str | None = None  # "Key: value" per line
    body: str | None = None


class RunIn(BaseModel):
    env: dict[str, str] = {}
    case_ids: list[str] | None = None  # None = whatever's selected in the session
    overrides: dict[str, RequestOverride] = {}  # generated item id -> edits to apply before running


class ProjectIn(BaseModel):
    path: str | None = None
    clone_url: str | None = None
    name: str | None = None


class ChangeTarget(BaseModel):
    mode: str  # "existing" or "new"
    collection: str | None = None       # existing mode: relative path to the collection file
    collection_name: str | None = None  # new mode: name for the new collection
    new_path: str | None = None         # new mode: relative directory to write it in ("" = repo root)
    env: str | None = None              # relative path to an environment file to add missing vars to


class ChangeIn(BaseModel):
    sid: str
    case_ids: list[str]
    target: ChangeTarget
    flow_name: str = "New tests"
    env: dict[str, str] = {}


def create_app(store: Store | None = None) -> FastAPI:
    store = store or Store()
    hub = Hub()
    recorder = Recorder(store, hub)
    cache: dict[str, tuple] = {}
    pending_changes: dict[str, dict] = {}

    @asynccontextmanager
    async def lifespan(_app):
        yield
        await recorder.stop_all()

    app = FastAPI(title="API Atlas", version=__version__, lifespan=lifespan)
    app.state.store = store
    app.state.recorder = recorder

    def session_or_404(sid: str) -> dict:
        s = store.get_session(sid)
        if not s:
            raise HTTPException(404, "Session not found")
        return s

    def analysis_for(sid: str) -> dict:
        s = session_or_404(sid)
        calls = store.calls(sid, with_fingerprints=True)
        key = (len(calls), calls[-1]["id"] if calls else 0, json.dumps(s["allowed_hosts"]))
        hit = cache.get(sid)
        if hit and hit[0] == key:
            return hit[1]
        result = analyse(calls)
        cache[sid] = (key, result)
        return result

    # ------------------------------------------------------------ sessions
    @app.get("/api/health")
    def health():
        return {"ok": True, "version": __version__}

    @app.get("/api/sessions")
    def list_sessions():
        return store.list_sessions()

    @app.post("/api/sessions")
    async def start_session(body: StartIn):
        url = body.url.strip()
        if not urlsplit(url).scheme:
            url = "https://" + url
        parts = urlsplit(url)
        if parts.scheme not in ("http", "https") or not parts.hostname:
            raise HTTPException(400, "Enter a full URL, like https://app.example.com")
        name = (body.name or "").strip() or parts.hostname
        s = store.create_session(name, url, default_allowed_hosts(url))
        try:
            await recorder.start(s["id"], url)
        except RuntimeError as e:
            return JSONResponse({**store.get_session(s["id"]), "error": str(e)}, status_code=500)
        return store.get_session(s["id"])

    @app.get("/api/sessions/{sid}")
    def get_session(sid: str):
        s = session_or_404(sid)
        s["calls"] = [call_summary(c) for c in store.calls(sid, include_hidden=True)]
        return s

    @app.post("/api/sessions/{sid}/stop")
    async def stop_session(sid: str):
        session_or_404(sid)
        await recorder.stop(sid)
        return store.get_session(sid)

    @app.patch("/api/sessions/{sid}")
    def rename_session(sid: str, body: dict):
        session_or_404(sid)
        if body.get("name"):
            store.update_session(sid, name=str(body["name"])[:120])
        return store.get_session(sid)

    @app.delete("/api/sessions/{sid}")
    async def delete_session(sid: str):
        session_or_404(sid)
        await recorder.stop(sid)
        store.delete_session(sid)
        cache.pop(sid, None)
        return {"ok": True}

    @app.put("/api/sessions/{sid}/hosts")
    def set_hosts(sid: str, body: HostsIn):
        session_or_404(sid)
        hosts = sorted({h.strip().lower() for h in body.allowed_hosts if h.strip()})
        if not hosts:
            raise HTTPException(400, "Keep at least one host")
        store.update_session(sid, allowed_hosts=hosts)
        store.set_hidden_by_hosts(sid, hosts)
        cache.pop(sid, None)
        return store.get_session(sid)

    @app.post("/api/sessions/{sid}/reclassify")
    def reclassify_session(sid: str):
        """Re-run the current noise filter against already-recorded calls, so a filter.py update
        can clean up a session recorded before it, without re-exploring."""
        s = session_or_404(sid)
        changed = store.reclassify_calls(sid, s["allowed_hosts"])
        cache.pop(sid, None)
        return {"changed": changed, **store.get_session(sid)}

    @app.get("/api/sessions/{sid}/calls/{call_id}")
    def get_call(sid: str, call_id: int):
        c = store.get_call(sid, call_id)
        if not c:
            raise HTTPException(404, "Call not found")
        return c

    # ------------------------------------------------------------ analysis
    @app.get("/api/sessions/{sid}/analysis")
    def get_analysis(sid: str):
        a = analysis_for(sid)
        ov = store.overrides(sid)
        endpoints = []
        for ep in a["endpoints"]:
            endpoints.append({k: v for k, v in ep.items() if k not in ("resp_node", "req_node")})
        cases = []
        for c in a["cases"]:
            o = ov.get(c["id"], {})
            t = c["target"]
            expect = {"status": o["expect_status"]} if o.get("expect_status") else t["expect"]
            cases.append({
                "id": c["id"], "endpoint": c["endpoint"], "kind": c["kind"],
                "title": o.get("title") or c["title"], "selected": o.get("selected", c["selected"]),
                "default_title": c["title"], "expect": expect,
                "setup": [s["label"] for s in c["setup"]],
                "assertions": len(t["assertions"]), "needs_env": c["needs_env"],
            })
        selected = [c for c in cases if c["selected"]]
        needs_env = sorted({e for c in selected for e in c["needs_env"]})
        return {"hosts": a["hosts"], "endpoints": endpoints, "cases": cases, "needs_env": needs_env,
                "selected_count": len(selected)}

    @app.patch("/api/sessions/{sid}/cases/{case_id}")
    def patch_case(sid: str, case_id: str, body: CasePatch):
        session_or_404(sid)
        return store.set_override(sid, case_id, body.model_dump())

    @app.post("/api/sessions/{sid}/cases/select")
    def bulk_select(sid: str, body: BulkSelect):
        session_or_404(sid)
        for cid in body.case_ids:
            store.set_override(sid, cid, {"selected": body.selected})
        return {"ok": True}

    # ------------------------------------------------------------ export
    def _generated(sid: str, fmt: str) -> dict[str, str]:
        s = session_or_404(sid)
        a = analysis_for(sid)
        cases = selected_cases(a, store.overrides(sid))
        if not cases:
            raise HTTPException(400, "Select at least one test case first")
        gen = {"pytest": pytest_gen, "postman": postman_gen}.get(fmt)
        if not gen:
            raise HTTPException(404, "Unknown format")
        return gen.generate(cases, a["hosts"], s["name"])

    @app.get("/api/sessions/{sid}/preview/{fmt}")
    def preview(sid: str, fmt: str):
        return _generated(sid, fmt)

    @app.get("/api/sessions/{sid}/export/{fmt}")
    def export(sid: str, fmt: str):
        s = session_or_404(sid)
        slug = "".join(ch if ch.isalnum() else "-" for ch in s["name"].lower()).strip("-") or "app"
        if fmt == "har":
            return Response(json.dumps(_har(store.calls(sid)), indent=2), media_type="application/json",
                            headers={"Content-Disposition": f'attachment; filename="{slug}.har"'})
        files = _generated(sid, fmt)
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
            for name, content in files.items():
                z.writestr(f"{slug}-{fmt}/{name}", content)
        return Response(buf.getvalue(), media_type="application/zip",
                        headers={"Content-Disposition": f'attachment; filename="{slug}-{fmt}.zip"'})

    NEWMAN_TIMEOUT = 180

    def _apply_request_overrides(node: dict, overrides: dict) -> None:
        """Walk a generated collection's item tree and patch any request the user edited
        before running - keyed by the same item id the script preview showed them."""
        for item in node.get("item", []):
            if "item" in item:
                _apply_request_overrides(item, overrides)
                continue
            ov = overrides.get(item.get("id"))
            if not ov:
                continue
            req = item["request"]
            if ov.method:
                req["method"] = ov.method.upper()
            if ov.url is not None:
                req["url"] = ov.url
            if ov.headers is not None:
                headers = []
                for line in ov.headers.splitlines():
                    if ":" in line:
                        k, v = line.split(":", 1)
                        if k.strip():
                            headers.append({"key": k.strip(), "value": v.strip()})
                req["header"] = headers
            if ov.body is not None:
                if ov.body.strip():
                    req["body"] = {"mode": "raw", "raw": ov.body, "options": {"raw": {"language": "json"}}}
                else:
                    req.pop("body", None)

    def _newman_cmd() -> list[str] | None:
        """newman on PATH, else a pinned `npx newman@6`, else None (not installed)."""
        custom = os.environ.get("ATLAS_NEWMAN")
        if custom:
            return [custom]
        path_bin = shutil.which("newman")
        if path_bin:
            return [path_bin]
        npx = shutil.which("npx")
        return [npx, "-y", "newman@6"] if npx else None

    @app.post("/api/sessions/{sid}/newman")
    def run_newman(sid: str, body: RunIn):
        """Run the generated Postman collection with Newman and report pass/fail per test folder."""
        s = session_or_404(sid)
        a = analysis_for(sid)
        if body.case_ids is not None:
            wanted = set(body.case_ids)
            cases = [c for c in a["cases"] if c["id"] in wanted]
        else:
            cases = selected_cases(a, store.overrides(sid))
        if not cases:
            raise HTTPException(400, "Select at least one test case first")
        cmd0 = _newman_cmd()
        if cmd0 is None:
            raise HTTPException(400, "Newman isn't installed. Install it with `npm install -g newman`, "
                                      "or make sure Node's `npx` is on PATH.")
        files = postman_gen.generate(cases, a["hosts"], s["name"])
        coll = next(n for n in files if n.endswith(".postman_collection.json"))
        envf = next(n for n in files if n.endswith(".postman_environment.json"))
        if body.overrides:
            coll_data = json.loads(files[coll])
            _apply_request_overrides(coll_data, body.overrides)
            files[coll] = json.dumps(coll_data, indent=2, ensure_ascii=False)
        with tempfile.TemporaryDirectory(prefix="atlas-newman-") as tmp:
            for name, content in files.items():
                (Path(tmp) / name).write_text(content)
            report_path = Path(tmp) / "report.json"
            cmd = cmd0 + ["run", coll, "-e", envf, "--reporters", "json",
                          "--reporter-json-export", str(report_path), "--color", "off"]
            for k, v in body.env.items():
                if v:
                    cmd += ["--env-var", f"{k}={v}"]
            try:
                r = subprocess.run(cmd, capture_output=True, text=True, cwd=tmp, timeout=NEWMAN_TIMEOUT)
            except subprocess.TimeoutExpired:
                raise HTTPException(504, f"Newman timed out after {NEWMAN_TIMEOUT}s.")
            except FileNotFoundError:
                raise HTTPException(400, "Couldn't launch Newman. Install it with `npm install -g newman`.")
            if not report_path.exists():
                raise HTTPException(500, ("Newman didn't produce a report.\n" + r.stdout + r.stderr)[:4000])
            report = json.loads(report_path.read_text())
        return newman_report.summarize(report, cases, postman_gen.item_index(cases))

    # ------------------------------------------------------------ projects (project mode)
    @app.get("/api/fs/list")
    def fs_list(path: str | None = None):
        p = Path(path).expanduser() if path else Path.home()
        try:
            p = p.resolve()
        except OSError:
            raise HTTPException(400, "Invalid path")
        if not p.is_dir():
            raise HTTPException(400, "Not a directory")
        try:
            entries = sorted((c for c in p.iterdir() if c.is_dir() and not c.name.startswith(".")),
                             key=lambda c: c.name.lower())
        except PermissionError:
            raise HTTPException(403, "Permission denied")
        return {"path": str(p), "parent": str(p.parent) if p.parent != p else None,
                "entries": [{"name": c.name, "path": str(c), "is_repo": (c / ".git").is_dir()} for c in entries]}

    @app.post("/api/projects")
    def create_project(body: ProjectIn):
        if body.clone_url:
            slug = re.sub(r"[^A-Za-z0-9_-]+", "-",
                          (body.name or body.clone_url.rsplit("/", 1)[-1]).removesuffix(".git")).strip("-") or "project"
            root = default_home() / "projects" / slug
            if not root.exists():
                root.parent.mkdir(parents=True, exist_ok=True)
                env = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}
                try:
                    r = subprocess.run(["git", "clone", body.clone_url, str(root)],
                                       capture_output=True, text=True, timeout=120, env=env)
                except subprocess.TimeoutExpired:
                    raise HTTPException(504, "Cloning timed out.")
                except FileNotFoundError:
                    raise HTTPException(400, "git isn't installed.")
                if r.returncode != 0:
                    shutil.rmtree(root, ignore_errors=True)
                    raise HTTPException(400, f"git clone failed: {r.stderr.strip()[-2000:]}")
            name = body.name or slug
        elif body.path:
            root = Path(body.path).expanduser()
            if not root.is_dir():
                raise HTTPException(400, "That folder doesn't exist.")
            name = body.name or root.name
        else:
            raise HTTPException(400, "Provide a path or a clone_url.")
        info = scan_mod.git_info(root)
        return store.create_project(name, str(root), info["remote"], info["default_branch"])

    @app.get("/api/projects")
    def list_projects():
        return store.list_projects()

    def project_or_404(pid: str) -> dict:
        p = store.get_project(pid)
        if not p:
            raise HTTPException(404, "Project not found")
        return p

    @app.get("/api/projects/{pid}")
    def get_project(pid: str):
        p = project_or_404(pid)
        root = Path(p["path"])
        if not root.is_dir():
            return {**p, "missing": True}
        return {**p, **scan_mod.scan(root)}

    @app.delete("/api/projects/{pid}")
    def delete_project(pid: str):
        project_or_404(pid)
        store.delete_project(pid)
        return {"ok": True}

    def _collection_path(root: Path, rel: str) -> Path:
        coll_path = (root / rel).resolve()
        try:
            coll_path.relative_to(root.resolve())
        except ValueError:
            raise HTTPException(400, "Invalid collection path")
        if not coll_path.is_file():
            raise HTTPException(404, "Collection file not found")
        return coll_path

    @app.get("/api/projects/{pid}/coverage")
    def project_coverage(pid: str, collection: str, sid: str):
        p = project_or_404(pid)
        coll_path = _collection_path(Path(p["path"]), collection)
        try:
            coll = json.loads(coll_path.read_text())
        except ValueError:
            raise HTTPException(400, "Collection file isn't valid JSON")
        a = analysis_for(sid)
        rows = coverage_mod.compute_coverage(a["endpoints"], requests_in(coll))
        return {"coverage": rows, "summary": coverage_mod.summary(rows),
                "style": style_mod.detect_style(coll), "folders": top_level_folders(coll)}

    @app.get("/api/projects/{pid}/self-coverage")
    def self_coverage(pid: str, collection: str):
        """Coverage computed purely from the collection's own requests - no exploration
        needed. Every endpoint shown came from a real request in the file, so this only ever
        tells you "happy path only" vs "happy + negative", never "not covered"."""
        p = project_or_404(pid)
        coll_path = _collection_path(Path(p["path"]), collection)
        try:
            coll = json.loads(coll_path.read_text())
        except ValueError:
            raise HTTPException(400, "Collection file isn't valid JSON")
        reqs = requests_in(coll)
        eps = coverage_mod.collection_endpoints(reqs)
        rows = coverage_mod.compute_coverage(eps, reqs)
        return {"coverage": rows, "summary": coverage_mod.summary(rows), "style": style_mod.detect_style(coll)}

    @app.post("/api/projects/{pid}/run-collection")
    def run_collection(pid: str, collection: str, environment: str | None = None, body: RunIn = RunIn()):
        """Run a collection exactly as it exists in the repo, via Newman - no Atlas generation
        involved, so this works with zero exploration. Results group by the collection's own
        top-level folders, since there's no case id scheme of ours to key off."""
        p = project_or_404(pid)
        root = Path(p["path"])
        coll_path = _collection_path(root, collection)
        cmd0 = _newman_cmd()
        if cmd0 is None:
            raise HTTPException(400, "Newman isn't installed. Install it with `npm install -g newman`, "
                                      "or make sure Node's `npx` is on PATH.")
        with tempfile.TemporaryDirectory(prefix="atlas-run-collection-") as tmp:
            report_path = Path(tmp) / "report.json"
            cmd = cmd0 + ["run", str(coll_path), "--reporters", "json",
                          "--reporter-json-export", str(report_path), "--color", "off"]
            if environment:
                cmd += ["-e", str(_collection_path(root, environment))]
            for k, v in body.env.items():
                if v:
                    cmd += ["--env-var", f"{k}={v}"]
            try:
                subprocess.run(cmd, capture_output=True, text=True, cwd=tmp, timeout=NEWMAN_TIMEOUT)
            except subprocess.TimeoutExpired:
                raise HTTPException(504, f"Newman timed out after {NEWMAN_TIMEOUT}s.")
            if not report_path.exists():
                raise HTTPException(500, "Newman didn't produce a report.")
            report = json.loads(report_path.read_text())
        coll = json.loads(coll_path.read_text())
        return newman_report.summarize_collection(report, unit_index(coll))

    @app.get("/api/projects/{pid}/case-script")
    def case_script(pid: str, sid: str, case_id: str, collection: str):
        """The real generated Postman request(s) for one case, each tagged with whether that
        exact call already exists somewhere in the target collection."""
        p = project_or_404(pid)
        a = analysis_for(sid)
        case = next((c for c in a["cases"] if c["id"] == case_id), None)
        if not case:
            raise HTTPException(404, "Case not found")
        items = postman_gen.case_items(case)
        coll_path = _collection_path(Path(p["path"]), collection)
        try:
            existing = json.loads(coll_path.read_text())
        except ValueError:
            existing = {"item": []}
        existing_shapes = {coverage_mod.postman_request_shape(r) for r in requests_in(existing)}
        parsed = requests_in({"item": [{"name": "_", "item": items}]})
        for item, req in zip(items, parsed):
            item["already_in_collection"] = coverage_mod.postman_request_shape(req) in existing_shapes
        return {"items": items}

    # -------------------------------------------------------- changes -> verify -> PR
    def _new_folder_for(cases: list[dict], hosts: list[str], flow_name: str, style: dict) -> tuple[dict, set[str]]:
        files = postman_gen.generate(cases, hosts, flow_name)
        coll = json.loads(next(v for k, v in files.items() if k.endswith(".postman_collection.json")))
        var_names = {name for c in cases for name in c["vars"]}
        items = style_adapt.adapt_items(coll["item"], var_names, hosts, style)
        return {"name": flow_name, "item": items, "description": "Added by API Atlas"}, var_names

    def _pr_body(flow_name: str, cases: list[dict], before, after, needs_env: list[str]) -> str:
        endpoints = "\n".join(f"- `{c['endpoint']}` ({c['kind']})" for c in cases)
        cov_line = (f"Coverage: {before['happy_only'] + before['happy_and_negative']}/{before['total']} "
                    f"-> {after['happy_only'] + after['happy_and_negative']}/{after['total']} endpoints covered."
                    if before and after else "")
        env_line = ("\nFill in before running: " + ", ".join(f"`{e}`" for e in needs_env)) if needs_env else ""
        return (f"Adds {len(cases)} test case(s) for **{flow_name}**, generated by API Atlas.\n\n"
                f"**Endpoints**\n{endpoints}\n\n**{cov_line}**{env_line}\n")

    @app.post("/api/projects/{pid}/changes")
    def preview_change(pid: str, body: ChangeIn):
        p = project_or_404(pid)
        root = Path(p["path"])
        if not root.is_dir():
            raise HTTPException(400, "This project's folder no longer exists on disk.")
        a = analysis_for(body.sid)
        ov = store.overrides(body.sid)
        cases = [c for c in selected_cases(a, ov) if c["id"] in set(body.case_ids)]
        if not cases:
            raise HTTPException(400, "Select at least one test case first")

        info = scan_mod.git_info(root)
        if info["remote"]:
            try:
                git_ops.fetch(root)
            except git_ops.GitError as e:
                raise HTTPException(400, f"git fetch failed: {e}")
            base_ref = f"origin/{info['default_branch']}"
        else:
            base_ref = info["current_branch"] or info["default_branch"]
            if not base_ref:
                raise HTTPException(400, "This folder isn't a git repository yet.")
        branch = git_ops.branch_name(body.flow_name)
        try:
            wt = git_ops.create_worktree(root, default_home() / "worktrees" / pid, branch, base_ref)
        except git_ops.GitError as e:
            raise HTTPException(400, f"Couldn't create a worktree: {e}")

        needs_env = sorted({e for c in cases for e in c["needs_env"]})
        warnings = []
        before_cov = after_cov = None
        if body.target.mode == "existing":
            if not body.target.collection:
                raise HTTPException(400, "Choose a collection to add tests to.")
            coll_path = _collection_path(wt, body.target.collection)
            existing = json.loads(coll_path.read_text())
            before_cov = coverage_mod.summary(coverage_mod.compute_coverage(a["endpoints"], requests_in(existing)))
            style = style_mod.detect_style(existing)
            folder, var_names = _new_folder_for(cases, a["hosts"], body.flow_name, style)
            extra_vars = style_adapt.extra_variable_declarations(var_names, {v.get("key") for v in existing.get("variable", [])})
            r = edit_mod.add_folder_to_collection(coll_path, folder, extra_vars)
            coll_path.write_text(r["new_text"])
            if r["unrelated_changes"]:
                warnings.append(f"{body.target.collection}: this edit would also change "
                                f"{r['removed_lines']} lines we didn't intend to touch - check the diff.")
            after = json.loads(coll_path.read_text())
            after_cov = coverage_mod.summary(coverage_mod.compute_coverage(a["endpoints"], requests_in(after)))
        elif body.target.mode == "new":
            if not body.target.collection_name:
                raise HTTPException(400, "Name the new collection.")
            style = {"base_url_var": "baseUrl", "token_var": None, "variable_scope": "collection"}
            files = postman_gen.generate(cases, a["hosts"], body.target.collection_name)
            dest_dir = wt / (body.target.new_path or "")
            dest_dir.mkdir(parents=True, exist_ok=True)
            for name, content in files.items():
                if name != "README.md":
                    (dest_dir / name).write_text(content)
        else:
            raise HTTPException(400, "target.mode must be 'existing' or 'new'")

        if body.target.env and body.target.mode == "existing" and needs_env:
            env_path = _collection_path(wt, body.target.env)
            r = edit_mod.add_missing_env_vars(env_path, needs_env)
            env_path.write_text(r["new_text"])
            if r["unrelated_changes"]:
                warnings.append(f"{body.target.env}: this edit would also change "
                                f"{r['removed_lines']} lines we didn't intend to touch - check the diff.")

        diff_text = git_ops.diff(wt)

        change_id = uuid.uuid4().hex[:12]
        title = f"Add {len(cases)} API test(s): {body.flow_name}"
        pr_body = _pr_body(body.flow_name, cases, before_cov, after_cov, needs_env)
        pending_changes[change_id] = {"pid": pid, "worktree": str(wt), "branch": branch,
                                      "base_branch": info["default_branch"], "remote": info["remote"],
                                      "title": title, "body": pr_body}
        return {"change_id": change_id, "branch": branch, "base_branch": info["default_branch"],
                "diff": diff_text, "warnings": warnings,
                "coverage_before": before_cov, "coverage_after": after_cov, "needs_env": needs_env,
                "has_remote": bool(info["remote"]), "pr_title": title, "pr_body": pr_body}

    @app.post("/api/projects/{pid}/changes/{change_id}/submit")
    def submit_change(pid: str, change_id: str):
        change = pending_changes.get(change_id)
        if not change or change["pid"] != pid:
            raise HTTPException(404, "This change isn't ready any more. Preview it again.")
        if not change["remote"]:
            raise HTTPException(400, "This project has no git remote to push to.")
        wt = Path(change["worktree"])
        if not wt.is_dir():
            raise HTTPException(400, "This worktree no longer exists. Preview the change again.")
        try:
            git_ops.commit_all(wt, change["title"])
            git_ops.push(wt, change["branch"])
        except git_ops.GitError as e:
            raise HTTPException(400, f"git push failed: {e}")
        result = github_pr.open_pr(change["remote"], change["base_branch"], change["branch"],
                                   change["title"], change["body"])
        pending_changes.pop(change_id, None)
        return result

    # ------------------------------------------------------------ live updates
    @app.websocket("/ws/sessions/{sid}")
    async def ws(websocket: WebSocket, sid: str):
        await websocket.accept()
        q = hub.subscribe(sid)
        try:
            while True:
                msg = await q.get()
                await websocket.send_json(msg)
        except (WebSocketDisconnect, asyncio.CancelledError, RuntimeError):
            pass
        finally:
            hub.unsubscribe(sid, q)

    # ------------------------------------------------------------ UI
    app.mount("/static", StaticFiles(directory=STATIC), name="static")

    @app.get("/")
    def index():
        return FileResponse(STATIC / "index.html")

    return app


def _har(calls: list[dict]) -> dict:
    from datetime import datetime, timezone

    entries = []
    for c in calls:
        req_text = json.dumps(c["req_body"]) if c["req_kind"] in ("json", "form") else (c["req_body"] or "")
        resp_text = json.dumps(c["resp_body"]) if c["resp_kind"] == "json" else (c["resp_body"] or "")
        entries.append({
            "startedDateTime": datetime.fromtimestamp(c.get("ts") or 0, timezone.utc).isoformat(),
            "time": c.get("duration_ms") or 0,
            "request": {
                "method": c["method"], "url": c["url"], "httpVersion": "HTTP/1.1",
                "headers": [{"name": k, "value": v} for k, v in c["req_headers"].items()],
                "queryString": [{"name": k, "value": str(v)} for k, v in c["query"]],
                "cookies": [], "headersSize": -1, "bodySize": len(req_text),
                **({"postData": {"mimeType": c["req_headers"].get("content-type", ""), "text": req_text}}
                   if c["req_kind"] != "none" else {}),
            },
            "response": {
                "status": c["status"] or 0, "statusText": "", "httpVersion": "HTTP/1.1",
                "headers": [{"name": k, "value": v} for k, v in c["resp_headers"].items()],
                "cookies": [], "redirectURL": "", "headersSize": -1, "bodySize": len(resp_text),
                "content": {"size": len(resp_text), "mimeType": c["resp_headers"].get("content-type", ""),
                            "text": resp_text},
            },
            "cache": {}, "timings": {"send": 0, "wait": c.get("duration_ms") or 0, "receive": 0},
        })
    return {"log": {"version": "1.2", "creator": {"name": "API Atlas", "version": __version__},
                    "entries": entries}}
