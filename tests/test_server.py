"""The HTTP API the UI uses: analysis, case edits and exports."""
import io
import json
import zipfile
from pathlib import Path

import requests

from atlas.engine.callmodel import build_call
from atlas.server import create_app
from atlas.store import Store
from flow_helpers import ServerThread, free_port


def seed(app):
    store, rec = app.state.store, app.state.recorder
    s = store.create_session("Shop", "https://shop.com", ["shop.com"])
    for i, (m, url, req, resp, status) in enumerate([
        ("POST", "https://shop.com/api/login", {"email": "a@b.co", "password": "pw123456"}, {"token": "tok_abcdefgh123"}, 200),
        ("GET", "https://shop.com/api/me", None, {"id": 1, "name": "Vi"}, 200),
        ("GET", "https://cdn.other.com/config.json", None, {"a": 1}, 200),
    ], 1):
        headers = {"content-type": "application/json"}
        if i == 2:
            headers["authorization"] = "Bearer tok_abcdefgh123"
        c = build_call(method=m, url=url, req_headers=headers, req_data=json.dumps(req) if req else None,
                       status=status, resp_headers={"content-type": "application/json"},
                       resp_data=json.dumps(resp), ts=float(i))
        rec.ingest(s["id"], c)
    return s["id"]


def test_api_flow(tmp_path):
    app = create_app(Store(tmp_path / "t.db"))
    sid = seed(app)
    port = free_port()
    base = f"http://127.0.0.1:{port}"
    with ServerThread(app, port):
        s = requests.get(f"{base}/api/sessions/{sid}").json()
        assert s["call_count"] == 2 and s["hidden_count"] == 1

        a = requests.get(f"{base}/api/sessions/{sid}/analysis").json()
        assert [e["template"] for e in a["endpoints"]] == ["/api/login", "/api/me"]
        me = next(c for c in a["cases"] if c["endpoint"].endswith("/api/me") and c["kind"] == "happy")
        assert me["setup"] == ["POST /api/login"]
        assert a["needs_env"] == ["ATLAS_EMAIL", "ATLAS_PASSWORD"]   # existing account, not a sign-up

        r = requests.patch(f"{base}/api/sessions/{sid}/cases/{me['id']}", json={"title": "Profile loads"})
        assert r.ok
        files = requests.get(f"{base}/api/sessions/{sid}/preview/pytest").json()
        test_file = next(v for k, v in files.items() if k.startswith("test_"))
        assert "def test_profile_loads" in test_file
        assert "tok_abcdefgh123" not in json.dumps(files)

        z = requests.get(f"{base}/api/sessions/{sid}/export/postman")
        names = zipfile.ZipFile(io.BytesIO(z.content)).namelist()
        assert any(n.endswith(".postman_collection.json") for n in names)
        har = requests.get(f"{base}/api/sessions/{sid}/export/har").json()
        assert len(har["log"]["entries"]) == 2

        # including the other host reveals its call
        requests.put(f"{base}/api/sessions/{sid}/hosts", json={"allowed_hosts": ["shop.com", "other.com"]})
        assert requests.get(f"{base}/api/sessions/{sid}").json()["call_count"] == 3

        assert requests.get(f"{base}/").status_code == 200
        assert requests.delete(f"{base}/api/sessions/{sid}").ok
        assert requests.get(f"{base}/api/sessions/{sid}").status_code == 404


def _deselect_all(base, sid):
    a = requests.get(f"{base}/api/sessions/{sid}/analysis").json()
    requests.post(f"{base}/api/sessions/{sid}/cases/select",
                  json={"case_ids": [c["id"] for c in a["cases"]], "selected": False})


def test_newman_case_ids_overrides_session_selection(tmp_path, monkeypatch):
    """An explicit case_ids list runs exactly those cases, regardless of what's "selected" in
    the session - this is what the project page's inline "Run flow" checklist relies on."""
    monkeypatch.setattr("atlas.server.shutil.which", lambda name: "/usr/bin/fake-newman" if name == "newman" else None)

    def fake_run(cmd, capture_output, text, cwd, timeout):
        report_path = Path(cmd[cmd.index("--reporter-json-export") + 1])
        report_path.write_text(json.dumps({"run": {"executions": [], "failures": []}}))

        class R:
            returncode = 0
            stdout = stderr = ""
        return R()

    monkeypatch.setattr("atlas.server.subprocess.run", fake_run)
    app = create_app(Store(tmp_path / "t.db"))
    sid = seed(app)
    port = free_port()
    base = f"http://127.0.0.1:{port}"
    with ServerThread(app, port):
        _deselect_all(base, sid)
        a = requests.get(f"{base}/api/sessions/{sid}/analysis").json()
        me = next(c for c in a["cases"] if c["endpoint"].endswith("/api/me"))
        # not selected in the session at all, but explicitly requested here
        r = requests.post(f"{base}/api/sessions/{sid}/newman", json={"case_ids": [me["id"]]})
        assert r.ok, r.text
        result = r.json()
        assert result["total"] == 1
        assert result["cases"][0]["id"] == me["id"]


def test_reclassify_hides_calls_that_are_noise_under_the_current_filter(tmp_path):
    """Simulates a session recorded before a filter.py update recognised a new noise pattern:
    the call is already stored and visible, and reclassify should hide it without re-exploring."""
    app = create_app(Store(tmp_path / "t.db"))
    store, rec = app.state.store, app.state.recorder
    s = store.create_session("Shop", "https://shop.com", ["shop.com"])
    noise = build_call(method="POST", url="https://shop.com/__nextjs_original-stack-frames",
                       req_headers={"content-type": "application/json"}, req_data=None, status=200,
                       resp_headers={"content-type": "application/json"}, resp_data=json.dumps({"frames": []}), ts=1.0)
    # bypass ingest()/classify() on purpose - this is what "recorded before the fix" looks like
    store.add_call(s["id"], noise, fingerprints=[], hidden=False)
    port = free_port()
    base = f"http://127.0.0.1:{port}"
    with ServerThread(app, port):
        before = requests.get(f"{base}/api/sessions/{s['id']}").json()
        assert before["call_count"] == 1

        r = requests.post(f"{base}/api/sessions/{s['id']}/reclassify")
        assert r.ok, r.text
        assert r.json()["changed"] == 1

        after = requests.get(f"{base}/api/sessions/{s['id']}").json()
        assert after["call_count"] == 0
        assert after["hidden_count"] == 1

        # calling it again with nothing left to change is a no-op
        r2 = requests.post(f"{base}/api/sessions/{s['id']}/reclassify")
        assert r2.json()["changed"] == 0


def test_newman_requires_selection(tmp_path):
    app = create_app(Store(tmp_path / "t.db"))
    sid = seed(app)
    port = free_port()
    base = f"http://127.0.0.1:{port}"
    with ServerThread(app, port):
        _deselect_all(base, sid)
        r = requests.post(f"{base}/api/sessions/{sid}/newman", json={})
        assert r.status_code == 400


def test_newman_not_installed_gives_hint(tmp_path, monkeypatch):
    monkeypatch.delenv("ATLAS_NEWMAN", raising=False)
    monkeypatch.setattr("atlas.server.shutil.which", lambda name: None)
    app = create_app(Store(tmp_path / "t.db"))
    sid = seed(app)
    port = free_port()
    base = f"http://127.0.0.1:{port}"
    with ServerThread(app, port):
        a = requests.get(f"{base}/api/sessions/{sid}/analysis").json()
        me = next(c for c in a["cases"] if c["endpoint"].endswith("/api/me"))
        requests.patch(f"{base}/api/sessions/{sid}/cases/{me['id']}", json={"selected": True})
        r = requests.post(f"{base}/api/sessions/{sid}/newman", json={})
        assert r.status_code == 400
        assert "newman" in r.json()["detail"].lower()
        assert "npm install" in r.json()["detail"]


def test_newman_run_parses_report_into_plain_english(tmp_path, monkeypatch):
    """Runs the real endpoint end-to-end with subprocess.run faked, so it needs no real Newman/Node."""
    monkeypatch.setattr("atlas.server.shutil.which", lambda name: "/usr/bin/fake-newman" if name == "newman" else None)

    def fake_run(cmd, capture_output, text, cwd, timeout):
        coll_path = next(Path(cwd) / c for c in cmd if c.endswith(".postman_collection.json"))
        report_path = Path(cmd[cmd.index("--reporter-json-export") + 1])
        coll = json.loads(coll_path.read_text())
        target = coll["item"][0]["item"][-1]
        body = json.dumps({"detail": "nope, not found"}).encode()
        report = {"run": {
            "executions": [{"item": {"id": target["id"]}, "assertions": [
                {"assertion": "status is 200", "skipped": False,
                 "error": {"message": "expected response to have status code 200 but got 404", "test": "status is 200"}}],
                "response": {"code": 404, "stream": {"type": "Buffer", "data": list(body)}}}],
            "failures": [{"error": {"message": "expected response to have status code 200 but got 404",
                                     "test": "status is 200"},
                          "at": "assertion:0 in test-script", "source": {"id": target["id"]}}],
        }}
        report_path.write_text(json.dumps(report))

        class R:
            returncode = 1
            stdout = stderr = ""
        return R()

    monkeypatch.setattr("atlas.server.subprocess.run", fake_run)
    app = create_app(Store(tmp_path / "t.db"))
    sid = seed(app)
    port = free_port()
    base = f"http://127.0.0.1:{port}"
    with ServerThread(app, port):
        _deselect_all(base, sid)
        a = requests.get(f"{base}/api/sessions/{sid}/analysis").json()
        me = next(c for c in a["cases"] if c["endpoint"].endswith("/api/me"))
        requests.patch(f"{base}/api/sessions/{sid}/cases/{me['id']}", json={"selected": True})
        r = requests.post(f"{base}/api/sessions/{sid}/newman", json={"env": {"ATLAS_PASSWORD": "pw123456"}})
        assert r.ok, r.text
        result = r.json()
        assert result["ok"] is False
        assert result["passed"] == 0 and result["total"] == 1
        case = result["cases"][0]
        assert case["ok"] is False
        reason = case["reasons"][0]
        assert reason["plain"] == "Expected 200, got 404"
        assert reason["hint"] == "nope, not found"
        assert reason["kind"] == "status" and reason["got_status"] == 404


def test_newman_applies_request_overrides_before_running(tmp_path, monkeypatch):
    """A user-edited method/url/headers/body for one generated item must actually be sent,
    not just displayed - the fake subprocess inspects the written collection to prove it."""
    monkeypatch.setattr("atlas.server.shutil.which", lambda name: "/usr/bin/fake-newman" if name == "newman" else None)
    seen = {}

    def fake_run(cmd, capture_output, text, cwd, timeout):
        coll_path = next(Path(cwd) / c for c in cmd if c.endswith(".postman_collection.json"))
        report_path = Path(cmd[cmd.index("--reporter-json-export") + 1])
        coll = json.loads(coll_path.read_text())
        target = coll["item"][0]["item"][-1]
        seen["method"] = target["request"]["method"]
        seen["url"] = target["request"]["url"]
        seen["headers"] = target["request"]["header"]
        seen["body"] = target["request"]["body"]["raw"]
        report_path.write_text(json.dumps({"run": {"executions": [], "failures": []}}))

        class R:
            returncode = 0
            stdout = stderr = ""
        return R()

    monkeypatch.setattr("atlas.server.subprocess.run", fake_run)
    app = create_app(Store(tmp_path / "t.db"))
    sid = seed(app)
    port = free_port()
    base = f"http://127.0.0.1:{port}"
    with ServerThread(app, port):
        _deselect_all(base, sid)
        a = requests.get(f"{base}/api/sessions/{sid}/analysis").json()
        me = next(c for c in a["cases"] if c["endpoint"].endswith("/api/me"))
        requests.patch(f"{base}/api/sessions/{sid}/cases/{me['id']}", json={"selected": True})

        # discover the target item id the same way the UI's case-script preview would
        from atlas.gen import postman_gen
        idx = postman_gen.item_index([c for c in [me]])  # id scheme only depends on case id + step index
        target_item_id = next(k for k, v in idx.items() if v["case_id"] == me["id"] and v["is_target"])

        r = requests.post(f"{base}/api/sessions/{sid}/newman", json={
            "env": {"ATLAS_PASSWORD": "pw123456"},
            "overrides": {target_item_id: {"method": "post", "url": "https://shop.com/api/me?debug=1",
                                           "headers": "X-Debug: 1\nmalformed-line",
                                           "body": '{"edited": true}'}},
        })
        assert r.ok, r.text
        assert seen["method"] == "POST"
        assert seen["url"] == "https://shop.com/api/me?debug=1"
        assert seen["headers"] == [{"key": "X-Debug", "value": "1"}]
        assert seen["body"] == '{"edited": true}'
