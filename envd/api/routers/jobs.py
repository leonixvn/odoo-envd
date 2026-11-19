"""Jobs: status, logs, cancellation."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query

from envd.api.deps import Page, get_runner, get_store, require_scope
from envd.core.jobs import JobRunner
from envd.core.state import Store

router = APIRouter(prefix="/jobs", tags=["jobs"])


@router.get("")
def list_jobs(
    env: str | None = None,
    state: str | None = None,
    page: Page = Depends(),
    store: Store = Depends(get_store),
    _token: dict = Depends(require_scope("read")),
) -> dict:
    items = store.list_jobs(env_branch=env, state=state, limit=1000)
    return {"items": page.slice(items), "total": len(items)}


@router.get("/{job_id}")
def get_job(
    job_id: int,
    store: Store = Depends(get_store),
    _token: dict = Depends(require_scope("read")),
) -> dict:
    job = store.get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail=f"job {job_id} not found")
    return job


@router.post("/{job_id}/cancel", status_code=202)
def cancel_job(
    job_id: int,
    store: Store = Depends(get_store),
    runner: JobRunner = Depends(get_runner),
    _token: dict = Depends(require_scope("write")),
) -> dict:
    if not store.get_job(job_id):
        raise HTTPException(status_code=404, detail=f"job {job_id} not found")
    if not runner.cancel(job_id):
        raise HTTPException(status_code=409, detail=f"job {job_id} is already finished")
    return {"job_id": job_id, "cancelled": True}


@router.get("/{job_id}/logs")
def job_logs(
    job_id: int,
    tail: int = Query(default=200, ge=1, le=5000),
    store: Store = Depends(get_store),
    _token: dict = Depends(require_scope("read")),
) -> dict:
    job = store.get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail=f"job {job_id} not found")
    return {"job": job_id, "log": _read_tail(job.get("log_path"), tail)}


def _read_tail(log_path: str | None, tail: int) -> str:
    if not log_path:
        return ""
    try:
        with open(log_path, encoding="utf-8") as fh:
            lines = fh.readlines()
    except OSError:
        return ""
    return "".join(lines[-tail:])
