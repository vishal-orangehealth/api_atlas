"""FastAPI app: REST + websocket API and the web UI."""
from __future__ import annotations

import asyncio
import io
import json
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
from .engine.analysis import analyse
from .engine.filter import default_allowed_hosts
from .gen import postman_gen, pytest_gen
from .gen.common import selected_cases
from .store import Store

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


def create_app(store: Store | None = None) -> FastAPI:
    store = store or Store()
    hub = Hub()
    recorder = Recorder(store, hub)
    cache: dict[str, tuple] = {}

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
