"""Record the demo shop in a real (headless) browser, generate tests, run them against the app."""
import asyncio
import json
import os
import shutil
import subprocess
import sys

import pytest

from atlas.capture import Hub, Recorder
from atlas.demo.app import app as demo_app
from atlas.engine.analysis import analyse
from atlas.gen import postman_gen, pytest_gen
from atlas.store import Store
from flow_helpers import ServerThread, free_port, record_demo_flow

os.environ.setdefault("ATLAS_HEADLESS", "1")


@pytest.fixture(scope="module")
def recorded():
    port = free_port()
    base = f"http://127.0.0.1:{port}"
    store = Store(":memory:")
    recorder = Recorder(store, Hub())
    with ServerThread(demo_app, port):
        sid = asyncio.run(record_demo_flow(recorder, store, base))
        yield {"store": store, "sid": sid, "base": base,
               "analysis": analyse(store.calls(sid, with_fingerprints=True))}


def test_capture_keeps_only_api_calls(recorded):
    s = recorded["store"].get_session(recorded["sid"])
    calls = recorded["store"].calls(recorded["sid"])
    paths = [c["path"] for c in calls]
    assert "/api/telemetry/rum" not in paths     # beacon filtered out
    assert s["dropped"] >= 1
    assert "/api/auth/login" in paths and any(p.startswith("/api/orders/") for p in paths)
    blob = json.dumps(calls)
    assert "secret123" not in blob               # the demo password never hits the store


def test_endpoints(recorded):
    keys = {e["template"] for e in recorded["analysis"]["endpoints"]}
    assert {"/api/auth/register", "/api/auth/login", "/api/me", "/api/products/{product_id}",
            "/api/cart/items", "/api/orders", "/api/orders/{order_id}"} <= keys


def _write(files, out):
    shutil.rmtree(out, ignore_errors=True)
    os.makedirs(out)
    for name, content in files.items():
        with open(os.path.join(out, name), "w") as f:
            f.write(content)


def test_generated_pytest_suite_passes(recorded, tmp_path):
    a = recorded["analysis"]
    cases = [c for c in a["cases"] if c["selected"]]
    assert len(cases) >= 8
    out = tmp_path / "suite"
    _write(pytest_gen.generate(cases, a["hosts"], "Atlas Shop"), out)
    # the demo app from the fixture is still running at recorded["base"]
    r = subprocess.run([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", str(out)],
                       capture_output=True, text=True, cwd=out)
    assert r.returncode == 0, r.stdout[-4000:]


def test_negative_cases_behave(recorded, tmp_path):
    """Validation / not-found cases should pass against a well-behaved API."""
    a = recorded["analysis"]
    cases = [c for c in a["cases"] if c["kind"] in ("missing_field", "wrong_type", "empty_body", "invalid_id")]
    out = tmp_path / "neg"
    _write(pytest_gen.generate(cases, a["hosts"], "Atlas Shop"), out)
    # the demo app from the fixture is still running at recorded["base"]
    r = subprocess.run([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", str(out)],
                       capture_output=True, text=True, cwd=out)
    assert r.returncode == 0, r.stdout[-4000:]


NEWMAN = shutil.which("newman") or os.environ.get("ATLAS_NEWMAN")


@pytest.mark.skipif(not NEWMAN, reason="newman not installed (set ATLAS_NEWMAN to its path)")
def test_generated_postman_collection_passes(recorded, tmp_path):
    a = recorded["analysis"]
    cases = [c for c in a["cases"] if c["selected"]]
    files = postman_gen.generate(cases, a["hosts"], "Atlas Shop")
    out = tmp_path / "pm"
    _write(files, out)
    r = subprocess.run([NEWMAN, "run", str(out / "atlas-shop.postman_collection.json"), "-e",
                        str(out / "atlas-shop.postman_environment.json"), "--color", "off"],
                       capture_output=True, text=True)
    assert r.returncode == 0, r.stdout[-4000:]
