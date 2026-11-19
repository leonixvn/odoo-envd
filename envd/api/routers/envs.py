"""Environment lifecycle endpoints.

Path convention: routes address an env by its **slug** (slash-free, stable —
`feature/x` → `feature_x`). Responses always carry the full `branch` name too,
and `_resolve_env` also accepts plain branch names, so `main` works directly;
branches containing slashes are addressed by their slug (returned by
POST /api/envs and GET /api/envs).
"""

from __future__ import annotations

import sqlite3

from fastapi import APIRouter, Depends, HTTPException, Query, Request

from envd.api.deps import Page, get_runner, get_store, public_env, require_scope, secret_env
from envd.api.schemas import (
    ActionRequest,
    DeployRequest,
    DuplicateRequest,
    EnvCreate,
    EnvPatch,
    RollbackRequest,
)
from envd.core.jobs import JobRunner
from envd.core.odoo_cli import OdooCLI, parse_module_list
from envd.core.shell import Shell
from envd.core.state import Store
from envd.tools.ports import PortPool
from envd.tools.pw import generate_password
from envd.tools.slug import BranchError, db_slug, env_url, validate_branch

router = APIRouter(prefix="/envs", tags=["envs"])


def _resolve_env(store: Store, ident: str) -> dict:
    env = store.get_env_by_slug(ident) or store.get_env(ident)
    if not env:
        raise HTTPException(status_code=404, detail=f"env {ident!r} not found")
    return env


def create_env_row(request: Request, store: Store, body: EnvCreate) -> dict:
    """Create the env row (idempotent on branch, fails cleanly on collisions)."""
    cfg = request.app.state.cfg
    try:
        validate_branch(body.branch)
        slug = db_slug(body.branch, max_len=cfg.policy.max_slug_len)
    except BranchError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    existing = store.get_env(body.branch)
    if existing:
        return existing

    direct_port = body.direct_port
    if direct_port is not None:
        pool = PortPool(*cfg.policy.port_range)
        try:
            pool.validate(direct_port)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        if direct_port in store.used_ports():
            raise HTTPException(status_code=409, detail=f"port {direct_port} already in use")

    try:
        return store.create_env(
            branch=body.branch,
            slug=slug,
            db=f"env_{slug}",
            url=env_url(slug, cfg.server.domain),
            with_demo=body.with_demo or cfg.policy.with_demo,
            seed=body.seed,
            image_tag=body.image_tag or cfg.docker.base_image,
            admin_password=generate_password(),
            pg_password=generate_password(),
            pinned=body.pinned,
        )
    except sqlite3.IntegrityError as exc:
        raise HTTPException(
            status_code=409,
            detail=f"branch or slug collision (another branch already maps to {slug})",
        ) from exc


@router.get("")
def list_envs(
    state: str | None = None,
    pinned: bool | None = None,
    page: Page = Depends(),
    store: Store = Depends(get_store),
    _token: dict = Depends(require_scope("read")),
) -> dict:
    items = [public_env(e) for e in store.list_envs(state=state, pinned=pinned)]
    return {"items": page.slice(items), "total": len(items)}


@router.get("/{env}/modules")
def env_modules(
    env: str,
    request: Request,
    refresh: bool = Query(default=False),
    store: Store = Depends(get_store),
    _token: dict = Depends(require_scope("read")),
) -> dict:
    """Live module inventory via the native `odoo module list`.

    Runs a one-shot compose command synchronously (seconds) — kept rare by the
    GUI; `installed_count` on the env row is the cheap cached value.
    """
    row = _resolve_env(store, env)
    if not refresh and row.get("installed_count") is not None:
        return {"branch": row["branch"], "installed_count": row["installed_count"], "modules": []}
    cfg = request.app.state.cfg
    odoo = OdooCLI(cfg, row["slug"], Shell())
    result = odoo.module_list()
    modules = parse_module_list(result.output)
    installed = [m for m in modules if m["state"] == "installed"]
    store.update_env(row["branch"], installed_count=len(installed))
    return {"branch": row["branch"], "installed_count": len(installed), "modules": modules}


@router.get("/{env}/logs")
def env_logs(
    env: str,
    tail: int = Query(default=200, ge=1, le=5000),
    job: int | None = None,
    store: Store = Depends(get_store),
    _token: dict = Depends(require_scope("read")),
) -> dict:
    row = _resolve_env(store, env)
    job_id = job
    if job_id is None:
        jobs = store.list_jobs(env_branch=row["branch"], limit=1)
        if not jobs:
            return {"branch": row["branch"], "log": ""}
        job_id = jobs[0]["id"]
    job_row = store.get_job(job_id)
    log_path = (job_row or {}).get("log_path")
    return {"branch": row["branch"], "job": job_id, "log": _read_tail(log_path, tail)}


def _read_tail(log_path: str | None, tail: int) -> str:
    if not log_path:
        return ""
    try:
        with open(log_path, encoding="utf-8") as fh:
            lines = fh.readlines()
    except OSError:
        return ""
    return "".join(lines[-tail:])


@router.get("/{env}/health")
def env_health(
    env: str,
    request: Request,
    store: Store = Depends(get_store),
    _token: dict = Depends(require_scope("read")),
) -> dict:
    row = _resolve_env(store, env)
    cfg = request.app.state.cfg
    odoo = OdooCLI(cfg, row["slug"], Shell())
    return {"branch": row["branch"], "http": odoo.health_once()}


@router.get("/{env}")
def env_detail(
    env: str,
    store: Store = Depends(get_store),
    _token: dict = Depends(require_scope("write")),
) -> dict:
    row = _resolve_env(store, env)
    detail = secret_env(row)
    detail["backups"] = {
        kind: len(store.list_backups(row["branch"], kind=kind))
        for kind in ("pre-deploy", "daily", "manual")
    }
    jobs = store.list_jobs(env_branch=row["branch"], limit=1)
    detail["last_job"] = jobs[0] if jobs else None
    return detail


@router.post("", status_code=202)
def create_env(
    request: Request,
    body: EnvCreate,
    store: Store = Depends(get_store),
    runner: JobRunner = Depends(get_runner),
    _token: dict = Depends(require_scope("write")),
) -> dict:
    existing = store.get_env(body.branch)
    env = create_env_row(request, store, body)
    params: dict = {}
    if body.neutralize:
        params["neutralize"] = True
    job_id = runner.submit("ensure", env["branch"], params)
    return {
        "job_id": job_id,
        "branch": env["branch"],
        "slug": env["slug"],
        "url": env["url"],
        "reused_existing": bool(existing),
    }


@router.patch("/{env}")
def patch_env(
    env: str,
    body: EnvPatch,
    store: Store = Depends(get_store),
    _token: dict = Depends(require_scope("write")),
) -> dict:
    row = _resolve_env(store, env)
    fields = body.model_dump(exclude_none=True)
    if not fields:
        raise HTTPException(status_code=422, detail="no fields to update")
    if "upgrade_strategy" in fields and fields["upgrade_strategy"] not in (
        "diff", "outdated", "full",
    ):
        raise HTTPException(status_code=422, detail="upgrade_strategy must be diff|outdated|full")
    if "on_deploy_failure" in fields and fields["on_deploy_failure"] not in ("rollback", "halt"):
        raise HTTPException(status_code=422, detail="on_deploy_failure must be rollback|halt")
    return public_env(store.update_env(row["branch"], **fields))


@router.delete("/{env}", status_code=202)
def destroy_env(
    env: str,
    keep_db: bool = Query(default=False),
    keep_backups: bool = Query(default=False),
    store: Store = Depends(get_store),
    runner: JobRunner = Depends(get_runner),
    _token: dict = Depends(require_scope("write")),
) -> dict:
    row = _resolve_env(store, env)
    job_id = runner.submit(
        "destroy", row["branch"], {"keep_db": keep_db, "keep_backups": keep_backups}
    )
    return {"job_id": job_id, "branch": row["branch"]}


@router.post("/{env}/deploy", status_code=202)
def deploy(
    env: str,
    body: DeployRequest | None = None,
    store: Store = Depends(get_store),
    runner: JobRunner = Depends(get_runner),
    _token: dict = Depends(require_scope("write")),
) -> dict:
    row = _resolve_env(store, env)
    mode = (body.mode if body else "changed") or "changed"
    if mode not in ("changed", "full", "outdated"):
        raise HTTPException(status_code=422, detail="mode must be changed|full|outdated")
    params = {"mode": mode} if mode != "changed" else {}
    job_id = runner.submit("deploy", row["branch"], params)
    return {"job_id": job_id, "branch": row["branch"], "mode": mode}


@router.post("/{env}/rollback", status_code=202)
def rollback(
    env: str,
    body: RollbackRequest | None = None,
    store: Store = Depends(get_store),
    runner: JobRunner = Depends(get_runner),
    _token: dict = Depends(require_scope("write")),
) -> dict:
    row = _resolve_env(store, env)
    to = (body.to if body else "pre-deploy") or "pre-deploy"
    params = {"to": to}
    if body and body.sha:
        params["sha"] = body.sha
    job_id = runner.submit("rollback", row["branch"], params)
    return {"job_id": job_id, "branch": row["branch"], "to": to}


@router.post("/{env}/recreate", status_code=202)
def recreate(
    env: str,
    store: Store = Depends(get_store),
    runner: JobRunner = Depends(get_runner),
    _token: dict = Depends(require_scope("write")),
) -> dict:
    row = _resolve_env(store, env)
    return {"job_id": runner.submit("recreate", row["branch"], {}), "branch": row["branch"]}


@router.post("/{env}/neutralize", status_code=202)
def neutralize(
    env: str,
    store: Store = Depends(get_store),
    runner: JobRunner = Depends(get_runner),
    _token: dict = Depends(require_scope("write")),
) -> dict:
    row = _resolve_env(store, env)
    return {"job_id": runner.submit("neutralize", row["branch"], {}), "branch": row["branch"]}


@router.post("/{env}/duplicate", status_code=202)
def duplicate(
    request: Request,
    env: str,
    body: DuplicateRequest,
    store: Store = Depends(get_store),
    runner: JobRunner = Depends(get_runner),
    _token: dict = Depends(require_scope("write")),
) -> dict:
    source = _resolve_env(store, env)
    target_body = EnvCreate(
        branch=body.target_branch,
        pinned=source["pinned"],
        seed=source["branch"],
        neutralize=body.neutralize,
    )
    target = create_env_row(request, store, target_body)
    params: dict = {"seed": source["branch"]}
    if body.neutralize:
        params["neutralize"] = True
    job_id = runner.submit("ensure", target["branch"], params)
    return {
        "job_id": job_id,
        "branch": target["branch"],
        "slug": target["slug"],
        "seeded_from": source["branch"],
    }


@router.post("/{env}/action", status_code=202)
def action(
    env: str,
    body: ActionRequest,
    store: Store = Depends(get_store),
    runner: JobRunner = Depends(get_runner),
    _token: dict = Depends(require_scope("write")),
) -> dict:
    row = _resolve_env(store, env)
    if body.action not in ("start", "stop", "restart"):
        raise HTTPException(status_code=422, detail="action must be start|stop|restart")
    return {"job_id": runner.submit("action", row["branch"], {"action": body.action})}
