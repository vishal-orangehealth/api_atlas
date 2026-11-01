"""Shared helpers for end-to-end tests: run the demo app and drive a recording."""
import asyncio
import socket
import threading
import time

import uvicorn


def free_port() -> int:
    s = socket.socket(); s.bind(("127.0.0.1", 0)); p = s.getsockname()[1]; s.close(); return p


class ServerThread:
    def __init__(self, app, port):
        self.server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error"))
        self.thread = threading.Thread(target=self.server.run, daemon=True)

    def __enter__(self):
        self.thread.start()
        for _ in range(100):
            if self.server.started:
                return self
            time.sleep(0.05)
        raise RuntimeError("server did not start")

    def __exit__(self, *a):
        self.server.should_exit = True
        self.thread.join(5)


async def record_demo_flow(recorder, store, base_url: str) -> str:
    """Record the full demo shop flow headlessly and return the session id."""
    from atlas.engine.filter import default_allowed_hosts
    s = store.create_session("Atlas Shop", base_url, default_allowed_hosts(base_url))
    sid = s["id"]
    await recorder.start(sid, base_url)
    page = recorder.active[sid]["context"].pages[0]
    await page.wait_for_selector("[data-add='1']")
    await page.click("#register"); await page.wait_for_timeout(300)
    await page.click("#login"); await page.wait_for_selector("#who:has-text('Hi')")
    await page.select_option("#cat", "accessories"); await page.wait_for_timeout(300)
    await page.click("[data-view='2']"); await page.wait_for_timeout(300)
    await page.click("[data-add='2']"); await page.wait_for_selector("#cart:has-text('item')")
    await page.click("#order"); await page.wait_for_selector("#orderbox:has-text('PLACED')")
    await page.wait_for_timeout(300)
    await recorder.stop(sid)
    return sid
