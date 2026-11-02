"""Local, multi-process-safe state: job registry, interactive sessions, audit log.

SQLite in WAL mode with a busy timeout handles several MCP server processes
(several agents) at once. The audit log is append-only JSONL, one line per tool call.
"""

from __future__ import annotations

import fcntl
import json
import os
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    job_id       TEXT PRIMARY KEY,
    target       TEXT NOT NULL,          -- user@host
    name         TEXT NOT NULL,
    workdir      TEXT NOT NULL,
    script_path  TEXT,
    resources    TEXT,                   -- JSON
    submitted_at REAL NOT NULL,
    last_state   TEXT,
    exit_status  INTEGER,
    last_checked REAL,
    cancelled    INTEGER NOT NULL DEFAULT 0,
    client       TEXT
);
CREATE INDEX IF NOT EXISTS jobs_name ON jobs(target, name, submitted_at);
CREATE TABLE IF NOT EXISTS sessions (
    session_id   TEXT PRIMARY KEY,
    target       TEXT NOT NULL,
    job_id       TEXT,
    tmux_name    TEXT NOT NULL,
    log_path     TEXT NOT NULL,
    cmd_dir      TEXT NOT NULL,
    workdir      TEXT NOT NULL,
    resources    TEXT,
    created_at   REAL NOT NULL,
    state        TEXT NOT NULL,          -- starting | queued | ready | ended
    shell_ready  INTEGER NOT NULL DEFAULT 0,
    pending_run  TEXT,                   -- run id of a command still executing
    pending_off  INTEGER,
    run_seq      INTEGER NOT NULL DEFAULT 0
);
"""

TERMINAL_STATES = {"F", "X"}


class State:
    def __init__(self, state_dir: Path):
        self.dir = state_dir
        self.dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.db_path = self.dir / "state.db"
        self.audit_path = self.dir / "audit.jsonl"
        with self.db() as con:
            con.executescript(SCHEMA)

    @contextmanager
    def db(self) -> Iterator[sqlite3.Connection]:
        con = sqlite3.connect(self.db_path, timeout=30, isolation_level=None)
        con.row_factory = sqlite3.Row
        try:
            con.execute("PRAGMA journal_mode=WAL")
            con.execute("PRAGMA busy_timeout=30000")
            yield con
        finally:
            con.close()

    @contextmanager
    def lock(self, name: str, timeout: float = 60) -> Iterator[None]:
        """Cross-process mutex (submission, per-session command execution)."""
        path = self.dir / f"{name}.lock"
        with open(path, "a+") as fh:
            deadline = time.monotonic() + timeout
            while True:
                try:
                    fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if time.monotonic() > deadline:
                        from .errors import PolicyError

                        raise PolicyError(f"resource '{name}' is busy (another agent is using it); retry shortly")
                    time.sleep(0.1)
            try:
                yield
            finally:
                fcntl.flock(fh, fcntl.LOCK_UN)

    # ------------------------------------------------------------------ jobs
    def add_job(self, **row: Any) -> None:
        row.setdefault("submitted_at", time.time())
        if isinstance(row.get("resources"), dict):
            row["resources"] = json.dumps(row["resources"])
        cols = ", ".join(row)
        qs = ", ".join("?" for _ in row)
        with self.db() as con:
            con.execute(f"INSERT OR REPLACE INTO jobs ({cols}) VALUES ({qs})", list(row.values()))

    def get_job(self, job_id: str) -> dict | None:
        with self.db() as con:
            r = con.execute("SELECT * FROM jobs WHERE job_id = ?", (job_id,)).fetchone()
        return dict(r) if r else None

    def find_job(self, target: str, job_id: str) -> dict | None:
        """Accept a full id or the numeric prefix ("1234" for "1234.pbs-m1.metacentrum.cz")."""
        with self.db() as con:
            r = con.execute(
                "SELECT * FROM jobs WHERE target = ? AND (job_id = ? OR job_id LIKE ?) "
                "ORDER BY submitted_at DESC LIMIT 1",
                (target, job_id, job_id.split(".")[0] + ".%"),
            ).fetchone()
        return dict(r) if r else None

    def latest_job_by_name(self, target: str, name: str) -> dict | None:
        with self.db() as con:
            r = con.execute(
                "SELECT * FROM jobs WHERE target = ? AND name = ? ORDER BY submitted_at DESC LIMIT 1",
                (target, name),
            ).fetchone()
        return dict(r) if r else None

    def jobs(self, target: str, active_only: bool = False, limit: int = 100) -> list[dict]:
        q = "SELECT * FROM jobs WHERE target = ?"
        if active_only:
            q += " AND (last_state IS NULL OR last_state NOT IN ('F','X')) AND cancelled = 0"
        q += " ORDER BY submitted_at DESC LIMIT ?"
        with self.db() as con:
            return [dict(r) for r in con.execute(q, (target, limit))]

    def update_job(self, job_id: str, **fields: Any) -> None:
        if not fields:
            return
        sets = ", ".join(f"{k} = ?" for k in fields)
        with self.db() as con:
            con.execute(f"UPDATE jobs SET {sets} WHERE job_id = ?", [*fields.values(), job_id])

    def submissions_since(self, target: str, since: float) -> int:
        with self.db() as con:
            return con.execute(
                "SELECT COUNT(*) FROM jobs WHERE target = ? AND submitted_at > ?", (target, since)
            ).fetchone()[0]

    # -------------------------------------------------------------- sessions
    def add_session(self, **row: Any) -> None:
        if isinstance(row.get("resources"), dict):
            row["resources"] = json.dumps(row["resources"])
        cols = ", ".join(row)
        qs = ", ".join("?" for _ in row)
        with self.db() as con:
            con.execute(f"INSERT INTO sessions ({cols}) VALUES ({qs})", list(row.values()))

    def get_session(self, session_id: str) -> dict | None:
        with self.db() as con:
            r = con.execute("SELECT * FROM sessions WHERE session_id = ?", (session_id,)).fetchone()
        return dict(r) if r else None

    def sessions(self, target: str, include_ended: bool = False) -> list[dict]:
        q = "SELECT * FROM sessions WHERE target = ?"
        if not include_ended:
            q += " AND state != 'ended'"
        q += " ORDER BY created_at DESC"
        with self.db() as con:
            return [dict(r) for r in con.execute(q, (target,))]

    def update_session(self, session_id: str, **fields: Any) -> None:
        if not fields:
            return
        sets = ", ".join(f"{k} = ?" for k in fields)
        with self.db() as con:
            con.execute(f"UPDATE sessions SET {sets} WHERE session_id = ?", [*fields.values(), session_id])

    def next_run_seq(self, session_id: str) -> int:
        with self.db() as con:
            con.execute("BEGIN IMMEDIATE")
            con.execute("UPDATE sessions SET run_seq = run_seq + 1 WHERE session_id = ?", (session_id,))
            seq = con.execute("SELECT run_seq FROM sessions WHERE session_id = ?", (session_id,)).fetchone()[0]
            con.execute("COMMIT")
        return seq

    # ----------------------------------------------------------------- audit
    def audit(self, entry: dict[str, Any]) -> None:
        entry = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "pid": os.getpid(), **entry}
        line = json.dumps(entry, default=str, ensure_ascii=False) + "\n"
        # O_APPEND writes of one line are atomic enough across processes for an audit trail.
        fd = os.open(self.audit_path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        try:
            os.write(fd, line.encode())
        finally:
            os.close(fd)

    def read_audit(self, last: int = 50) -> list[dict]:
        if not self.audit_path.exists():
            return []
        lines = self.audit_path.read_text().splitlines()[-last:]
        out = []
        for line in lines:
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        return out
