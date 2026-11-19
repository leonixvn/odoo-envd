"""SQLite-backed state store: envs, jobs, backups, events, tokens.

Design points:

- single connection + WAL: the API threads and the job workers share one
  process; every store call is serialized behind an RLock, calls are short
  (no user code runs inside the lock).
- the kit scripts never touch this database: they print ``::marker::`` lines
  on stdout and the engine maps markers onto this store (§ jobs.py).
- tokens are stored sha256-hashed; lookups use ``hmac.compare_digest``.
- timestamps are UTC ISO-8601 strings.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import secrets
import sqlite3
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS envs (
    branch          TEXT PRIMARY KEY,
    slug            TEXT NOT NULL UNIQUE,
    db_name         TEXT NOT NULL UNIQUE,
    url             TEXT NOT NULL,
    state           TEXT NOT NULL DEFAULT 'creating',
    pinned          INTEGER NOT NULL DEFAULT 0,
    with_demo       INTEGER NOT NULL DEFAULT 0,
    seed            TEXT,
    last_sha        TEXT,
    last_deploy_at  TEXT,
    deploy_count    INTEGER NOT NULL DEFAULT 0,
    image_tag       TEXT,
    upgrade_strategy TEXT,
    on_deploy_failure TEXT,
    workers         INTEGER NOT NULL DEFAULT 0,
    direct_port     INTEGER,
    admin_password  TEXT,
    pg_password     TEXT,
    deps_hash       TEXT,
    installed_count INTEGER,
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS jobs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    env_branch  TEXT,
    kind        TEXT NOT NULL,
    state       TEXT NOT NULL DEFAULT 'queued',
    params      TEXT NOT NULL DEFAULT '{}',
    steps       TEXT NOT NULL DEFAULT '[]',
    error       TEXT,
    log_path    TEXT,
    created_at  TEXT NOT NULL,
    started_at  TEXT,
    finished_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_jobs_env   ON jobs(env_branch);
CREATE INDEX IF NOT EXISTS idx_jobs_state ON jobs(state);
CREATE TABLE IF NOT EXISTS backups (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    env_branch     TEXT NOT NULL,
    kind           TEXT NOT NULL,
    path           TEXT NOT NULL,
    sha            TEXT,
    image_tag      TEXT,
    with_filestore INTEGER NOT NULL DEFAULT 1,
    size_bytes     INTEGER,
    created_at     TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_backups_env ON backups(env_branch);
CREATE TABLE IF NOT EXISTS events (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    kind       TEXT NOT NULL,
    payload    TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS tokens (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    name         TEXT NOT NULL,
    token_hash   TEXT NOT NULL UNIQUE,
    scopes       TEXT NOT NULL,
    created_at   TEXT NOT NULL,
    last_used_at TEXT
);
"""

ENV_STATES = ("creating", "deploying", "running", "failed", "stopped", "destroying")
JOB_STATES = ("queued", "running", "success", "failed", "superseded", "cancelled")
BACKUP_KINDS = ("pre-deploy", "daily", "manual")
SCOPES = ("read", "write", "admin")


def utcnow() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


class StoreError(ValueError):
    pass


class Store:
    def __init__(self, db_path: str | Path) -> None:
        path = Path(db_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        self._cx = sqlite3.connect(path, check_same_thread=False, timeout=30)
        self._cx.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        with self._lock, self._cx:
            self._cx.execute("PRAGMA journal_mode=WAL")
            self._cx.executescript(SCHEMA)

    def close(self) -> None:
        with self._lock:
            self._cx.close()

    # ------------------------------------------------------------- envs --

    def create_env(
        self,
        *,
        branch: str,
        slug: str,
        db: str,
        url: str,
        with_demo: bool = False,
        seed: str | None = None,
        image_tag: str | None = None,
        admin_password: str | None = None,
        pg_password: str | None = None,
        pinned: bool = False,
    ) -> dict[str, Any]:
        now = utcnow()
        with self._lock, self._cx:
            self._cx.execute(
                "INSERT INTO envs (branch, slug, db_name, url, state, pinned, with_demo,"
                " seed, image_tag, admin_password, pg_password, created_at, updated_at)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (branch, slug, db, url, "creating", int(pinned), int(with_demo), seed,
                 image_tag, admin_password, pg_password, now, now),
            )
        return self.get_env(branch)  # type: ignore[return-value]

    def get_env(self, branch: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._cx.execute("SELECT * FROM envs WHERE branch = ?", (branch,)).fetchone()
        return _env_row(row) if row else None

    def get_env_by_slug(self, slug: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._cx.execute("SELECT * FROM envs WHERE slug = ?", (slug,)).fetchone()
        return _env_row(row) if row else None

    def list_envs(
        self, state: str | None = None, pinned: bool | None = None
    ) -> list[dict[str, Any]]:
        query = "SELECT * FROM envs"
        clauses: list[str] = []
        args: list[Any] = []
        if state:
            clauses.append("state = ?")
            args.append(state)
        if pinned is not None:
            clauses.append("pinned = ?")
            args.append(int(pinned))
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        query += " ORDER BY created_at"
        with self._lock:
            rows = self._cx.execute(query, args).fetchall()
        return [_env_row(r) for r in rows]

    def update_env(self, branch: str, **fields: Any) -> dict[str, Any]:
        allowed = {
            "state", "pinned", "with_demo", "seed", "last_sha", "last_deploy_at",
            "deploy_count", "image_tag", "upgrade_strategy", "on_deploy_failure",
            "workers", "direct_port", "admin_password", "pg_password", "deps_hash",
            "installed_count",
        }
        unknown = set(fields) - allowed
        if unknown:
            raise StoreError(f"cannot update env fields: {sorted(unknown)}")
        if not fields:
            return self.get_env(branch)  # type: ignore[return-value]
        if "state" in fields and fields["state"] not in ENV_STATES:
            raise StoreError(f"invalid env state {fields['state']!r}")
        sets = ", ".join(f"{name} = ?" for name in fields)
        with self._lock, self._cx:
            self._cx.execute(
                f"UPDATE envs SET {sets}, updated_at = ? WHERE branch = ?",
                (*fields.values(), utcnow(), branch),
            )
        return self.get_env(branch)  # type: ignore[return-value]

    def delete_env(self, branch: str) -> None:
        with self._lock, self._cx:
            self._cx.execute("DELETE FROM envs WHERE branch = ?", (branch,))

    def used_ports(self) -> set[int]:
        with self._lock:
            rows = self._cx.execute(
                "SELECT direct_port FROM envs WHERE direct_port IS NOT NULL"
            ).fetchall()
        return {row["direct_port"] for row in rows}

    # ------------------------------------------------------------- jobs --

    def create_job(self, kind: str, env_branch: str | None, params: dict[str, Any]) -> int:
        with self._lock, self._cx:
            cur = self._cx.execute(
                "INSERT INTO jobs (kind, env_branch, params, created_at)"
                " VALUES (?,?,?,?)",
                (kind, env_branch, json.dumps(params), utcnow()),
            )
            return int(cur.lastrowid)

    def get_job(self, job_id: int) -> dict[str, Any] | None:
        with self._lock:
            row = self._cx.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
        return _job_row(row) if row else None

    def list_jobs(
        self,
        env_branch: str | None = None,
        state: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        query = "SELECT * FROM jobs"
        clauses: list[str] = []
        args: list[Any] = []
        if env_branch:
            clauses.append("env_branch = ?")
            args.append(env_branch)
        if state:
            clauses.append("state = ?")
            args.append(state)
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        query += " ORDER BY id DESC LIMIT ? OFFSET ?"
        args.extend([limit, offset])
        with self._lock:
            rows = self._cx.execute(query, args).fetchall()
        return [_job_row(r) for r in rows]

    def update_job(self, job_id: int, **fields: Any) -> None:
        allowed = {"state", "steps", "error", "log_path", "started_at", "finished_at"}
        unknown = set(fields) - allowed
        if unknown:
            raise StoreError(f"cannot update job fields: {sorted(unknown)}")
        if "state" in fields and fields["state"] not in JOB_STATES:
            raise StoreError(f"invalid job state {fields['state']!r}")
        sets = ", ".join(f"{name} = ?" for name in fields)
        if "steps" in fields:
            fields["steps"] = json.dumps(fields["steps"])
        with self._lock, self._cx:
            self._cx.execute(f"UPDATE jobs SET {sets} WHERE id = ?", (*fields.values(), job_id))

    def supersede_queued(self, kinds: tuple[str, ...], env_branch: str, before_id: int) -> int:
        """Mark older queued jobs of one env as superseded (newest-sha-wins)."""
        if not kinds:
            raise StoreError("supersede_queued requires at least one job kind")
        with self._lock, self._cx:
            cur = self._cx.execute(
                "UPDATE jobs SET state = 'superseded', finished_at = ?"
                " WHERE env_branch = ? AND state = 'queued' AND id < ?"
                f" AND kind IN ({','.join('?' * len(kinds))})",
                (utcnow(), env_branch, before_id, *kinds),
            )
            return cur.rowcount

    # ---------------------------------------------------------- backups --

    def add_backup(
        self,
        *,
        env_branch: str,
        kind: str,
        path: str,
        sha: str | None = None,
        image_tag: str | None = None,
        with_filestore: bool = True,
        size_bytes: int | None = None,
    ) -> int:
        if kind not in BACKUP_KINDS:
            raise StoreError(f"invalid backup kind {kind!r}")
        with self._lock, self._cx:
            cur = self._cx.execute(
                "INSERT INTO backups (env_branch, kind, path, sha, image_tag,"
                " with_filestore, size_bytes, created_at) VALUES (?,?,?,?,?,?,?,?)",
                (env_branch, kind, path, sha, image_tag, int(with_filestore),
                 size_bytes, utcnow()),
            )
            return int(cur.lastrowid)

    def get_backup(self, backup_id: int) -> dict[str, Any] | None:
        with self._lock:
            row = self._cx.execute("SELECT * FROM backups WHERE id = ?", (backup_id,)).fetchone()
        return dict(row) if row else None

    def list_backups(self, env_branch: str, kind: str | None = None) -> list[dict[str, Any]]:
        query = "SELECT * FROM backups WHERE env_branch = ?"
        args: list[Any] = [env_branch]
        if kind:
            query += " AND kind = ?"
            args.append(kind)
        query += " ORDER BY id DESC"
        with self._lock:
            rows = self._cx.execute(query, args).fetchall()
        return [dict(r) for r in rows]

    def delete_backup(self, backup_id: int) -> bool:
        with self._lock, self._cx:
            cur = self._cx.execute("DELETE FROM backups WHERE id = ?", (backup_id,))
            return bool(cur.rowcount)

    def prune_backups(self, env_branch: str, kind: str, keep: int) -> list[tuple[int, str]]:
        """Drop rows beyond the retention count; returns removed ``(id, path)`` pairs
        so the caller can unlink the zip files from disk."""
        with self._lock, self._cx:
            rows = self._cx.execute(
                "SELECT id, path FROM backups WHERE env_branch = ? AND kind = ?"
                " ORDER BY id DESC LIMIT -1 OFFSET ?",
                (env_branch, kind, keep),
            ).fetchall()
            removed = [(row["id"], row["path"]) for row in rows]
            if removed:
                ids = [rid for rid, _ in removed]
                marks = ",".join("?" * len(ids))
                self._cx.execute(f"DELETE FROM backups WHERE id IN ({marks})", ids)
        return removed

    # ---------------------------------------------------------- events --

    def add_event(self, kind: str, payload: dict[str, Any]) -> int:
        with self._lock, self._cx:
            cur = self._cx.execute(
                "INSERT INTO events (kind, payload, created_at) VALUES (?,?,?)",
                (kind, json.dumps(payload), utcnow()),
            )
            return int(cur.lastrowid)

    def events_after(self, last_id: int = 0, limit: int = 100) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._cx.execute(
                "SELECT * FROM events WHERE id > ? ORDER BY id LIMIT ?", (last_id, limit)
            ).fetchall()
        return [
            {
                "id": row["id"],
                "kind": row["kind"],
                "payload": json.loads(row["payload"]),
                "created_at": row["created_at"],
            }
            for row in rows
        ]

    # ---------------------------------------------------------- tokens --

    def create_token(self, name: str, scopes: tuple[str, ...]) -> tuple[int, str]:
        if not scopes or any(s not in SCOPES for s in scopes):
            raise StoreError(f"scopes must be non-empty subset of {SCOPES}")
        token = f"envd_{secrets.token_urlsafe(32)}"
        with self._lock, self._cx:
            cur = self._cx.execute(
                "INSERT INTO tokens (name, token_hash, scopes, created_at) VALUES (?,?,?,?)",
                (name, hash_token(token), ",".join(sorted(set(scopes))), utcnow()),
            )
            return int(cur.lastrowid), token

    def verify_token(self, token: str) -> dict[str, Any] | None:
        """Constant-time token lookup; updates last_used_at on success."""
        if not token:
            return None
        digest = hash_token(token)
        with self._lock:
            row = self._cx.execute(
                "SELECT * FROM tokens WHERE token_hash = ?", (digest,)
            ).fetchone()
            if row is None:
                # burn a compare to keep timing flat whether the row exists or not
                hmac.compare_digest(digest, "0" * 64)
                return None
            now = utcnow()
            self._cx.execute(
                "UPDATE tokens SET last_used_at = ? WHERE id = ?", (now, row["id"])
            )
            self._cx.commit()
        return {
            "id": row["id"],
            "name": row["name"],
            "scopes": tuple(row["scopes"].split(",")),
            "last_used_at": now,
        }

    def list_tokens(self) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._cx.execute(
                "SELECT id, name, scopes, created_at, last_used_at FROM tokens ORDER BY id"
            ).fetchall()
        return [dict(r) for r in rows]

    def delete_token(self, token_id: int) -> bool:
        with self._lock, self._cx:
            cur = self._cx.execute("DELETE FROM tokens WHERE id = ?", (token_id,))
            return bool(cur.rowcount)


def _env_row(row: sqlite3.Row) -> dict[str, Any]:
    env = dict(row)
    env["pinned"] = bool(env["pinned"])
    env["with_demo"] = bool(env["with_demo"])
    return env


def _job_row(row: sqlite3.Row) -> dict[str, Any]:
    job = dict(row)
    job["params"] = json.loads(job["params"])
    job["steps"] = json.loads(job["steps"])
    return job


def scope_satisfied(token_scopes: tuple[str, ...], required: str) -> bool:
    """Scope hierarchy: admin ⊃ write ⊃ read."""
    if required not in SCOPES:
        raise StoreError(f"unknown scope {required!r}")
    if "admin" in token_scopes:
        return True
    if required == "write":
        return "write" in token_scopes
    if required == "read":
        return "read" in token_scopes or "write" in token_scopes
    return False
