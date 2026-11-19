"""Backups and dump-import endpoints.

Env-scoped routes address the env by slug (see envs router docstring).
"""

from __future__ import annotations

import shutil
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import FileResponse

from envd.api.deps import get_runner, get_store, require_scope
from envd.api.routers.envs import _resolve_env, create_env_row
from envd.api.schemas import BackupRequest, EnvCreate, ImportRequest, RestoreRequest
from envd.core.jobs import JobRunner
from envd.core.state import Store

router = APIRouter(tags=["backups"])


@router.get("/envs/{env}/backups")
def list_backups(
    env: str,
    kind: str | None = Query(default=None),
    store: Store = Depends(get_store),
    _token: dict = Depends(require_scope("read")),
) -> dict:
    row = _resolve_env(store, env)
    return {"items": store.list_backups(row["branch"], kind=kind)}


@router.post("/envs/{env}/backups", status_code=202)
def create_backup(
    env: str,
    body: BackupRequest | None = None,
    store: Store = Depends(get_store),
    runner: JobRunner = Depends(get_runner),
    _token: dict = Depends(require_scope("write")),
) -> dict:
    row = _resolve_env(store, env)
    params = {"kind": "manual"}
    if body and not body.with_filestore:
        params["with_filestore"] = False
    return {"job_id": runner.submit("backup", row["branch"], params), "branch": row["branch"]}


@router.get("/envs/{env}/backups/{backup_id}")
def get_backup(
    env: str,
    backup_id: int,
    store: Store = Depends(get_store),
    _token: dict = Depends(require_scope("read")),
) -> dict:
    row = _resolve_env(store, env)
    return _backup_of(store, row["branch"], backup_id)


@router.get("/envs/{env}/backups/{backup_id}/download")
def download_backup(
    env: str,
    backup_id: int,
    store: Store = Depends(get_store),
    _token: dict = Depends(require_scope("write")),
) -> FileResponse:
    """The native odoo zip (dump.sql + manifest + filestore) — restorable by
    any odoo 20 `db load`, the web db-manager, or odoo.sh tooling."""
    row = _resolve_env(store, env)
    backup = _backup_of(store, row["branch"], backup_id)
    path = Path(backup["path"])
    if not path.is_file():
        raise HTTPException(status_code=410, detail="backup file no longer on disk")
    return FileResponse(path, filename=path.name, media_type="application/zip")


@router.post("/envs/{env}/backups/{backup_id}/restore", status_code=202)
def restore_backup(
    request: Request,
    env: str,
    backup_id: int,
    body: RestoreRequest | None = None,
    store: Store = Depends(get_store),
    runner: JobRunner = Depends(get_runner),
    _token: dict = Depends(require_scope("write")),
) -> dict:
    row = _resolve_env(store, env)
    backup = _backup_of(store, row["branch"], backup_id)
    target_branch = (body.target_branch if body else None) or row["branch"]
    if target_branch == row["branch"]:
        return {
            "job_id": runner.submit("rollback", row["branch"], {"to": str(backup_id)}),
            "branch": row["branch"],
        }

    # cross-env restore: copy the zip into the target env's backups dir, then
    # import it there (native db load --force + outdated upgrade)
    source = Path(backup["path"])
    if not source.is_file():
        raise HTTPException(status_code=410, detail="backup file no longer on disk")
    cfg = request.app.state.cfg
    target_env = store.get_env(target_branch)
    if not target_env:
        target_env = create_env_row(
            request, store, EnvCreate(branch=target_branch, pinned=False)
        )
    target_dir = cfg.paths.backups / target_env["slug"] / "imports"
    target_dir.mkdir(parents=True, exist_ok=True)
    target_file = target_dir / f"restore-{backup_id}-{source.name}"
    shutil.copy2(source, target_file)
    rel_name = f"imports/{target_file.name}"
    job_id = runner.submit("import", target_branch, {"dump_file": rel_name, "force": True})
    return {"job_id": job_id, "branch": target_branch, "restored_from": row["branch"]}


@router.delete("/envs/{env}/backups/{backup_id}")
def delete_backup(
    env: str,
    backup_id: int,
    store: Store = Depends(get_store),
    _token: dict = Depends(require_scope("admin")),
) -> dict:
    row = _resolve_env(store, env)
    backup = _backup_of(store, row["branch"], backup_id)
    Path(backup["path"]).unlink(missing_ok=True)
    store.delete_backup(backup_id)
    return {"deleted": backup_id}


@router.post("/imports", status_code=202)
def import_from_url(
    request: Request,
    body: ImportRequest,
    store: Store = Depends(get_store),
    runner: JobRunner = Depends(get_runner),
    _token: dict = Depends(require_scope("write")),
) -> dict:
    """Restore an external dump (e.g. a production backup URL) into a branch
    env; `odoo db load` fetches it server-side and can neutralize on import."""
    env = _ensure_target_env(request, store, body.target_branch)
    params: dict = {"dump_url": body.dump_url}
    if body.neutralize:
        params["neutralize"] = True
    return {"job_id": runner.submit("import", env["branch"], params), "branch": env["branch"]}


@router.post("/imports/upload", status_code=202)
async def import_upload(
    request: Request,
    target_branch: str = Query(...),
    filename: str = Query(...),
    neutralize: bool = Query(default=False),
    store: Store = Depends(get_store),
    runner: JobRunner = Depends(get_runner),
    _token: dict = Depends(require_scope("write")),
) -> dict:
    """Raw-bytes upload variant: POST the zip as the request body.

    Machine-to-machine friendly: no multipart machinery, the filename travels
    as a query parameter and is validated before touching disk.
    """
    safe = filename
    if not safe.replace(".", "").replace("_", "").replace("-", "").isalnum():
        raise HTTPException(status_code=422, detail=f"unsafe filename {safe!r}")
    env = _ensure_target_env(request, store, target_branch)
    cfg = request.app.state.cfg
    target_dir = cfg.paths.backups / env["slug"] / "imports"
    target_dir.mkdir(parents=True, exist_ok=True)
    body = await request.body()
    if not body:
        raise HTTPException(status_code=422, detail="empty upload body")
    (target_dir / safe).write_bytes(body)
    params: dict = {"dump_file": f"imports/{safe}"}
    if neutralize:
        params["neutralize"] = True
    return {"job_id": runner.submit("import", env["branch"], params), "branch": env["branch"]}


def _ensure_target_env(request: Request, store: Store, branch: str) -> dict:
    existing = store.get_env(branch)
    if existing:
        return existing
    return create_env_row(request, store, EnvCreate(branch=branch))


def _backup_of(store: Store, branch: str, backup_id: int) -> dict:
    backup = store.get_backup(backup_id)
    if not backup or backup["env_branch"] != branch:
        raise HTTPException(
            status_code=404, detail=f"backup {backup_id} not found for {branch!r}"
        )
    return backup
