"""The HTTP API the UI uses: analysis, case edits and exports."""
import io
import json
import zipfile

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
