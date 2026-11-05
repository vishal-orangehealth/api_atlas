"""SQLite storage. Only masked data is ever written here."""
from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
import uuid
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
  id TEXT PRIMARY KEY,
  name TEXT NOT NULL,
  target_url TEXT NOT NULL,
  created_at REAL NOT NULL,
  status TEXT NOT NULL,
  allowed_hosts TEXT NOT NULL,
  dropped INTEGER NOT NULL DEFAULT 0,
  error TEXT
);
CREATE TABLE IF NOT EXISTS calls (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
  seq INTEGER NOT NULL,
  hidden INTEGER NOT NULL DEFAULT 0,
  data TEXT NOT NULL,
  fingerprints TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS calls_session ON calls(session_id, seq);
CREATE TABLE IF NOT EXISTS case_overrides (
  session_id TEXT NOT NULL,
  case_id TEXT NOT NULL,
  data TEXT NOT NULL,
  PRIMARY KEY (session_id, case_id)
);
CREATE TABLE IF NOT EXISTS projects (
  id TEXT PRIMARY KEY,
  name TEXT NOT NULL,
  path TEXT NOT NULL,
  remote TEXT,
  default_branch TEXT,
  created_at REAL NOT NULL
);
"""


def default_home() -> Path:
    return Path(os.environ.get("ATLAS_HOME", Path.home() / ".api-atlas"))


class Store:
    def __init__(self, path: Path | str | None = None):
        if path is None:
            home = default_home()
            home.mkdir(parents=True, exist_ok=True)
            path = home / "atlas.db"
        self.path = str(path)
        self.lock = threading.RLock()
        self.db = sqlite3.connect(self.path, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA foreign_keys = ON")
        self.db.execute("PRAGMA journal_mode = WAL")
        self.db.executescript(SCHEMA)
        # a session left "recording" by a crash is just stopped
        self.db.execute("UPDATE sessions SET status='stopped' WHERE status='recording'")
        self.db.commit()

    # ---- sessions
    def create_session(self, name: str, target_url: str, allowed_hosts: list[str]) -> dict:
        sid = uuid.uuid4().hex[:12]
        with self.lock:
            self.db.execute(
                "INSERT INTO sessions (id, name, target_url, created_at, status, allowed_hosts) VALUES (?,?,?,?,?,?)",
                (sid, name, target_url, time.time(), "recording", json.dumps(allowed_hosts)))
            self.db.commit()
        return self.get_session(sid)

    def _session_row(self, row) -> dict:
        d = dict(row)
        d["allowed_hosts"] = json.loads(d["allowed_hosts"])
        return d

    def get_session(self, sid: str) -> dict | None:
        with self.lock:
            row = self.db.execute("SELECT * FROM sessions WHERE id=?", (sid,)).fetchone()
            if not row:
                return None
            d = self._session_row(row)
            counts = self.db.execute(
                "SELECT SUM(hidden=0) AS visible, SUM(hidden=1) AS hidden FROM calls WHERE session_id=?",
                (sid,)).fetchone()
            d["call_count"] = counts["visible"] or 0
            d["hidden_count"] = counts["hidden"] or 0
        return d

    def list_sessions(self) -> list[dict]:
        with self.lock:
            rows = self.db.execute(
                "SELECT s.*, (SELECT COUNT(*) FROM calls c WHERE c.session_id=s.id AND c.hidden=0) AS call_count "
                "FROM sessions s ORDER BY created_at DESC").fetchall()
        return [self._session_row(r) for r in rows]

    def update_session(self, sid: str, **fields) -> None:
        if "allowed_hosts" in fields:
            fields["allowed_hosts"] = json.dumps(fields["allowed_hosts"])
        cols = ", ".join(f"{k}=?" for k in fields)
        with self.lock:
            self.db.execute(f"UPDATE sessions SET {cols} WHERE id=?", (*fields.values(), sid))
            self.db.commit()

    def add_dropped(self, sid: str, n: int = 1) -> None:
        with self.lock:
            self.db.execute("UPDATE sessions SET dropped = dropped + ? WHERE id=?", (n, sid))
            self.db.commit()

    def delete_session(self, sid: str) -> None:
        with self.lock:
            self.db.execute("DELETE FROM calls WHERE session_id=?", (sid,))
            self.db.execute("DELETE FROM case_overrides WHERE session_id=?", (sid,))
            self.db.execute("DELETE FROM sessions WHERE id=?", (sid,))
            self.db.commit()

    # ---- calls
    def add_call(self, sid: str, call: dict, fingerprints: list[dict], hidden: bool) -> dict:
        with self.lock:
            seq = self.db.execute("SELECT COALESCE(MAX(seq), 0) + 1 FROM calls WHERE session_id=?",
                                  (sid,)).fetchone()[0]
            cur = self.db.execute(
                "INSERT INTO calls (session_id, seq, hidden, data, fingerprints) VALUES (?,?,?,?,?)",
                (sid, seq, int(hidden), json.dumps(call), json.dumps(fingerprints)))
            self.db.commit()
            cid = cur.lastrowid
        return {**call, "id": cid, "seq": seq, "hidden": hidden}

    def calls(self, sid: str, include_hidden: bool = False, with_fingerprints: bool = False) -> list[dict]:
        q = "SELECT * FROM calls WHERE session_id=?" + ("" if include_hidden else " AND hidden=0") + " ORDER BY seq"
        with self.lock:
            rows = self.db.execute(q, (sid,)).fetchall()
        out = []
        for r in rows:
            d = json.loads(r["data"])
            d.update({"id": r["id"], "seq": r["seq"], "hidden": bool(r["hidden"])})
            if with_fingerprints:
                d["fingerprints"] = json.loads(r["fingerprints"])
            out.append(d)
        return out

    def get_call(self, sid: str, call_id: int) -> dict | None:
        with self.lock:
            r = self.db.execute("SELECT * FROM calls WHERE session_id=? AND id=?", (sid, call_id)).fetchone()
        if not r:
            return None
        d = json.loads(r["data"])
        d.update({"id": r["id"], "seq": r["seq"], "hidden": bool(r["hidden"])})
        return d

    def set_hidden_by_hosts(self, sid: str, allowed: list[str]) -> None:
        from .engine.filter import host_allowed
        with self.lock:
            rows = self.db.execute("SELECT id, data FROM calls WHERE session_id=?", (sid,)).fetchall()
            for r in rows:
                host = json.loads(r["data"])["host"]
                self.db.execute("UPDATE calls SET hidden=? WHERE id=?", (int(not host_allowed(host, allowed)), r["id"]))
            self.db.commit()

    def reclassify_calls(self, sid: str, allowed_hosts: list[str]) -> int:
        """Re-run the current noise filter against already-stored calls - lets a filter.py
        update (e.g. a newly recognised noise path) clean up a session recorded before it,
        without re-exploring. Returns how many calls' hidden state changed."""
        from .engine.filter import classify
        with self.lock:
            rows = self.db.execute("SELECT id, hidden, data FROM calls WHERE session_id=?", (sid,)).fetchall()
            changed = 0
            for r in rows:
                c = json.loads(r["data"])
                verdict = classify(method=c["method"], url=c["url"], resource_type=c.get("resource_type"),
                                   status=c.get("status"), response_content_type=c["resp_headers"].get("content-type"),
                                   allowed_hosts=allowed_hosts)
                new_hidden = int(verdict != "keep")
                if new_hidden != r["hidden"]:
                    self.db.execute("UPDATE calls SET hidden=? WHERE id=?", (new_hidden, r["id"]))
                    changed += 1
            self.db.commit()
        return changed

    # ---- test case edits
    def overrides(self, sid: str) -> dict[str, dict]:
        with self.lock:
            rows = self.db.execute("SELECT case_id, data FROM case_overrides WHERE session_id=?", (sid,)).fetchall()
        return {r["case_id"]: json.loads(r["data"]) for r in rows}

    def set_override(self, sid: str, case_id: str, patch: dict) -> dict:
        current = self.overrides(sid).get(case_id, {})
        current.update({k: v for k, v in patch.items() if v is not None})
        with self.lock:
            self.db.execute("INSERT OR REPLACE INTO case_overrides (session_id, case_id, data) VALUES (?,?,?)",
                            (sid, case_id, json.dumps(current)))
            self.db.commit()
        return current

    # ---- projects
    def create_project(self, name: str, path: str, remote: str | None, default_branch: str | None) -> dict:
        pid = uuid.uuid4().hex[:12]
        with self.lock:
            self.db.execute(
                "INSERT INTO projects (id, name, path, remote, default_branch, created_at) VALUES (?,?,?,?,?,?)",
                (pid, name, path, remote, default_branch, time.time()))
            self.db.commit()
        return self.get_project(pid)

    def get_project(self, pid: str) -> dict | None:
        with self.lock:
            row = self.db.execute("SELECT * FROM projects WHERE id=?", (pid,)).fetchone()
        return dict(row) if row else None

    def list_projects(self) -> list[dict]:
        with self.lock:
            rows = self.db.execute("SELECT * FROM projects ORDER BY created_at DESC").fetchall()
        return [dict(r) for r in rows]

    def update_project(self, pid: str, **fields) -> None:
        cols = ", ".join(f"{k}=?" for k in fields)
        with self.lock:
            self.db.execute(f"UPDATE projects SET {cols} WHERE id=?", (*fields.values(), pid))
            self.db.commit()

    def delete_project(self, pid: str) -> None:
        with self.lock:
            self.db.execute("DELETE FROM projects WHERE id=?", (pid,))
            self.db.commit()
