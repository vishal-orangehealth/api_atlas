"""Record API traffic from a real browser with Playwright."""
from __future__ import annotations

import asyncio
import logging
import os
import secrets
import time

from .engine.callmodel import build_call
from .engine.filter import classify
from .engine.mask import mask_call
from .engine.values import fingerprints
from .store import Store

log = logging.getLogger("atlas.capture")


class Hub:
    """Fan out live events to websocket listeners."""

    def __init__(self):
        self.listeners: dict[str, set[asyncio.Queue]] = {}

    def subscribe(self, sid: str) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=1000)
        self.listeners.setdefault(sid, set()).add(q)
        return q

    def unsubscribe(self, sid: str, q: asyncio.Queue) -> None:
        self.listeners.get(sid, set()).discard(q)

    def publish(self, sid: str, msg: dict) -> None:
        for q in list(self.listeners.get(sid, ())):
            try:
                q.put_nowait(msg)
            except asyncio.QueueFull:
                pass


def call_summary(c: dict) -> dict:
    return {"id": c["id"], "seq": c["seq"], "method": c["method"], "host": c["host"], "path": c["path"],
            "status": c["status"], "duration_ms": c.get("duration_ms"), "hidden": c.get("hidden", False),
            "ts": c.get("ts")}


class Recorder:
    def __init__(self, store: Store, hub: Hub):
        self.store = store
        self.hub = hub
        self.active: dict[str, dict] = {}
        self.salts: dict[str, bytes] = {}

    # ---------------------------------------------------------------- ingestion
    def salt(self, sid: str) -> bytes:
        # the salt never leaves memory, so stored hashes can't be reversed
        return self.salts.setdefault(sid, secrets.token_bytes(32))

    def ingest(self, sid: str, call: dict) -> dict | None:
        """Filter, fingerprint, mask and store one call. Returns the stored call or None."""
        session = self.store.get_session(sid)
        if session is None:
            return None
        verdict = classify(method=call["method"], url=call["url"], resource_type=call.get("resource_type"),
                           status=call.get("status"),
                           response_content_type=call["resp_headers"].get("content-type"),
                           allowed_hosts=session["allowed_hosts"])
        if verdict == "drop":
            self.store.add_dropped(sid)
            return None
        fps = fingerprints(call, self.salt(sid))   # raw values -> salted hashes
        masked = mask_call(call)                   # raw values never stored
        stored = self.store.add_call(sid, masked, fps, hidden=(verdict == "hidden"))
        self.hub.publish(sid, {"type": "call", "call": call_summary(stored)})
        return stored

    # ---------------------------------------------------------------- browser
    async def start(self, sid: str, url: str) -> None:
        from playwright.async_api import async_playwright

        headless = os.environ.get("ATLAS_HEADLESS", "") in ("1", "true", "yes")
        pw = await async_playwright().start()
        try:
            browser = await self._launch(pw, headless)
        except Exception as e:
            await pw.stop()
            msg = ("Could not start a browser. Run `playwright install chromium` once, then try again. "
                   f"({str(e).splitlines()[0][:200]})")
            self.store.update_session(sid, status="stopped", error=msg)
            self.hub.publish(sid, {"type": "status", "status": "stopped", "error": msg})
            raise RuntimeError(msg) from e

        context = await browser.new_context(viewport=None if not headless else {"width": 1280, "height": 800})
        state = {"pw": pw, "browser": browser, "context": context, "started": {}, "tasks": set(),
                 "stopping": False}
        self.active[sid] = state

        def on_request(request):
            state["started"][id(request)] = time.time()

        def on_response(response):
            t = asyncio.ensure_future(self._handle_response(sid, state, response))
            state["tasks"].add(t)
            t.add_done_callback(state["tasks"].discard)

        def on_close(*_):
            if not state["stopping"]:
                asyncio.ensure_future(self.stop(sid))

        context.on("request", on_request)
        context.on("response", on_response)
        context.on("close", on_close)
        browser.on("disconnected", on_close)

        page = await context.new_page()

        def on_page_close(_p):
            if not state["stopping"] and not [p for p in context.pages if not p.is_closed()]:
                asyncio.ensure_future(self.stop(sid))

        page.on("close", on_page_close)
        context.on("page", lambda p: p.on("close", on_page_close))
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=30_000)
        except Exception as e:  # keep recording: the user can navigate manually
            log.warning("navigation to %s failed: %s", url, e)
        self.hub.publish(sid, {"type": "status", "status": "recording"})

    async def _launch(self, pw, headless: bool):
        try:
            return await pw.chromium.launch(headless=headless)
        except Exception:
            # fall back to an installed Chrome / Edge
            for channel in ("chrome", "msedge"):
                try:
                    return await pw.chromium.launch(headless=headless, channel=channel)
                except Exception:
                    continue
            raise

    async def _handle_response(self, sid: str, state: dict, response) -> None:
        try:
            request = response.request
            rtype = request.resource_type
            if rtype not in ("xhr", "fetch"):
                return
            started = state["started"].pop(id(request), time.time())
            req_headers = await request.all_headers()
            resp_headers = await response.all_headers()
            try:
                body = await response.body()
            except Exception:
                body = None
            try:
                post = request.post_data_buffer
            except Exception:
                post = None
            timing = request.timing or {}
            duration = timing.get("responseEnd") if timing.get("responseEnd", -1) > 0 else None
            call = build_call(method=request.method, url=request.url, req_headers=req_headers, req_data=post,
                              status=response.status, resp_headers=resp_headers, resp_data=body,
                              resource_type=rtype, duration_ms=duration, ts=started)
            self.ingest(sid, call)
        except Exception as e:  # never let one bad response break the session
            log.debug("skipped a response: %s", e)

    async def stop(self, sid: str) -> None:
        state = self.active.pop(sid, None)
        if state:
            state["stopping"] = True
            if state["tasks"]:
                await asyncio.wait(list(state["tasks"]), timeout=5)
            for closer in (state["context"].close, state["browser"].close, state["pw"].stop):
                try:
                    await closer()
                except Exception:
                    pass
        self.salts.pop(sid, None)
        if self.store.get_session(sid):
            self.store.update_session(sid, status="stopped")
        self.hub.publish(sid, {"type": "status", "status": "stopped"})

    async def stop_all(self) -> None:
        for sid in list(self.active):
            await self.stop(sid)
