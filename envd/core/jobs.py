"""Job runner: executes flows on a small thread pool with per-env locks.

Guarantees:

- **Per-env serialization**: one flock per env; a second job for the same env
  waits up to ``policy.job_lock_timeout`` and then fails loudly.
- **Newest-sha-wins**: submitting a deploy supersedes older *queued* deploys of
  the same env; superseded jobs exit before running anything.
- **Step tracking**: each flow step lands in the job row as it completes
  (state, timings, error), with the full log streamed to
  ``<root>/envd/logs/<job_id>.log``.
- **Failure policy**: for env-touching flows the env row moves to ``failed``
  unless the failure was a :class:`RolledBack` — a rolled-back deploy leaves
  the env *running on the previous sha*.
- **Cancellation**: queued jobs cancel instantly; running jobs stop at the
  next step boundary.
"""

from __future__ import annotations

import fcntl
import os
import time
from pathlib import Path
from typing import Any

from envd.config import Config
from envd.core.flows import FLOWS, FlowContext, FlowError, RolledBack
from envd.core.gitops import GitOps
from envd.core.odoo_cli import OdooCLI
from envd.core.pg import PgAdmin
from envd.core.shell import Shell
from envd.core.state import Store, utcnow

ENV_STATE_ON_FAILURE = {"ensure", "deploy", "rollback"}


class EnvLock:
    """Advisory exclusive lock, one file per env, blocking with deadline."""

    def __init__(self, path: Path, timeout: float) -> None:
        self.path = path
        self.timeout = timeout
        self._fd: int | None = None

    def acquire(self) -> bool:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._fd = os.open(str(self.path), os.O_CREAT | os.O_RDWR, 0o600)
        deadline = time.monotonic() + self.timeout
        while True:
            try:
                fcntl.flock(self._fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                return True
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    os.close(self._fd)
                    self._fd = None
                    return False
                time.sleep(0.25)

    def release(self) -> None:
        if self._fd is not None:
            fcntl.flock(self._fd, fcntl.LOCK_UN)
            os.close(self._fd)
            self._fd = None

    def __enter__(self) -> EnvLock:
        if not self.acquire():
            raise FlowError(f"could not acquire env lock {self.path} within timeout")
        return self

    def __exit__(self, *exc: object) -> None:
        self.release()


class JobRunner:
    def __init__(self, cfg: Config, store: Store) -> None:
        self.cfg = cfg
        self.store = store
        self.cfg.paths.logs.mkdir(parents=True, exist_ok=True)
        self._pool: Any = None  # created lazily; foreground mode needs no pool

    # -------------------------------------------------------------- submit --

    def submit(
        self,
        kind: str,
        env_branch: str | None,
        params: dict | None = None,
        *,
        foreground: bool = False,
    ) -> int:
        if kind not in FLOWS:
            raise FlowError(f"unknown job kind {kind!r}")
        params = dict(params or {})
        job_id = self.store.create_job(kind, env_branch, params)
        if kind == "deploy" and env_branch:
            self.store.supersede_queued(("deploy",), env_branch, before_id=job_id)
        self.store.add_event("job.queued", {"job": job_id, "kind": kind, "env": env_branch})
        if foreground:
            self._execute(job_id)
        else:
            if self._pool is None:
                from concurrent.futures import ThreadPoolExecutor

                self._pool = ThreadPoolExecutor(
                    max_workers=self.cfg.policy.job_workers, thread_name_prefix="envd"
                )
            self._pool.submit(self._execute, job_id)
        return job_id

    def cancel(self, job_id: int) -> bool:
        job = self.store.get_job(job_id)
        if not job or job["state"] not in ("queued", "running"):
            return False
        self.store.update_job(job_id, state="cancelled", finished_at=utcnow())
        self.store.add_event("job.cancelled", {"job": job_id})
        return True

    # ------------------------------------------------------------ execute --

    def _execute(self, job_id: int) -> None:
        job = self.store.get_job(job_id)
        if not job or job["state"] != "queued":
            return  # superseded or cancelled while queued

        env = self.store.get_env(job["env_branch"]) if job["env_branch"] else None
        lock = None
        if env:
            lock = EnvLock(
                self.cfg.paths.root / "locks" / f"env-{env['slug']}.lock",
                timeout=self.cfg.policy.job_lock_timeout,
            )
            if not lock.acquire():
                self.store.update_job(
                    job_id, state="failed", finished_at=utcnow(),
                    error="another job holds this environment's lock",
                )
                self.store.add_event("job.failed", {"job": job_id, "reason": "lock"})
                return

        log_path = self.cfg.paths.logs / f"{job_id}.log"
        try:
            with open(log_path, "a", encoding="utf-8") as log_file:
                shell = Shell(log=log_file.write)
                if env:
                    shell.add_redactions(env.get("pg_password"), env.get("admin_password"))
                ctx = self._build_context(job, env, shell)
                self.store.update_job(job_id, log_path=str(log_path))
                self._run_job(job_id, ctx)
        finally:
            if lock:
                lock.release()

    def _build_context(self, job: dict, env: dict | None, shell: Shell) -> FlowContext:
        slug = env["slug"] if env else ""
        return FlowContext(
            cfg=self.cfg,
            store=self.store,
            shell=shell,
            env=env or {},
            params=job["params"],
            git=GitOps(self.cfg, shell),
            odoo=OdooCLI(self.cfg, slug, shell) if slug else None,  # type: ignore[arg-type]
            pg=PgAdmin(self.cfg, shell),
            emit=lambda kind, payload: self.store.add_event(kind, payload),
        )

    def _run_job(self, job_id: int, ctx: FlowContext) -> None:
        job = self.store.get_job(job_id)
        kind = job["kind"]
        steps = FLOWS[kind](ctx)
        self.store.update_job(job_id, state="running", started_at=utcnow())
        self.store.add_event("job.started", {"job": job_id, "kind": kind})

        executed: list[dict] = []
        for step in steps:
            current = self.store.get_job(job_id)
            if current["state"] == "cancelled":
                self.store.update_job(job_id, finished_at=utcnow())
                return
            ctx.shell.log(f"--- step: {step.name}")
            started = time.monotonic()
            try:
                step.action(ctx)
                executed.append(
                    {"name": step.name, "state": "ok",
                     "duration": round(time.monotonic() - started, 2)}
                )
                self.store.update_job(job_id, steps=executed)
            except RolledBack as exc:
                executed.append(
                    {"name": step.name, "state": "rolled-back",
                     "duration": round(time.monotonic() - started, 2),
                     "error": str(exc)}
                )
                self._finish(job_id, ctx, executed, "failed", str(exc),
                             env_running=exc.kept_sha is not None)
                return
            except Exception as exc:
                executed.append(
                    {"name": step.name, "state": "failed",
                     "duration": round(time.monotonic() - started, 2),
                     "error": str(exc)}
                )
                self._finish(job_id, ctx, executed, "failed", str(exc))
                return

        executed.append({"name": "__done__", "state": "ok"})
        self._finish(job_id, ctx, executed, "success", None)

    def _finish(
        self,
        job_id: int,
        ctx: FlowContext,
        steps: list[dict],
        state: str,
        error: str | None,
        env_running: bool = False,
    ) -> None:
        self.store.update_job(
            job_id, state=state, finished_at=utcnow(), error=error, steps=steps
        )
        kind = self.store.get_job(job_id)["kind"]
        if ctx.env and kind in ENV_STATE_ON_FAILURE and state == "failed":
            new_state = "running" if env_running else "failed"
            ctx.store.update_env(ctx.env["branch"], state=new_state)
        self.store.add_event(
            "job.finished" if state == "success" else "job.failed",
            {"job": job_id, "kind": kind, "error": error},
        )
