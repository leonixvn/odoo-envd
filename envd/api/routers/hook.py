"""Forge webhook ingestion: push → ensure | deploy | destroy.

Unauthenticated but HMAC-verified (fail closed — see core.forge). The hook is
the automatic path: branch create → ensure, push → deploy (with the webhook's
after-sha), deletion → destroy. Protected branches are never destroyed
automatically.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request

from envd.api.deps import get_cfg, get_runner, get_store
from envd.api.routers.envs import create_env_row
from envd.api.schemas import EnvCreate
from envd.config import Config, dry_run_active
from envd.core.flows import FlowError
from envd.core.forge import ForgeError, verify_and_parse
from envd.core.jobs import JobRunner
from envd.core.state import Store

router = APIRouter(tags=["hook"])


@router.post("/hook")
async def hook(
    request: Request,
    cfg: Config = Depends(get_cfg),
    store: Store = Depends(get_store),
    runner: JobRunner = Depends(get_runner),
) -> dict:
    body = await request.body()
    headers = {k: v for k, v in request.headers.items()}
    try:
        event = verify_and_parse(cfg.forge, headers, body)
    except ForgeError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc

    if event is None:
        return {"accepted": False, "reason": "ignored (repo, ref or noise)"}

    # protection is absolute and checked before anything touches state
    if event.branch in cfg.policy.protect:
        return {"accepted": False, "reason": "protected branch"}

    if event.action == "deleted":
        env = store.get_env(event.branch)
        if env is None:
            return {"accepted": False, "reason": "no env for branch"}
        job_id = runner.submit("destroy", event.branch, {})
        return {"accepted": True, "action": "destroy", "job_id": job_id, "branch": event.branch}

    if event.action == "created":
        env = create_env_row(
            request, store, EnvCreate(branch=event.branch, seed=None)
        )
        job_id = runner.submit("ensure", event.branch, {})
        return {"accepted": True, "action": "ensure", "job_id": job_id, "branch": event.branch}

    # updated push → deploy with the webhook's sha (newest-sha-wins)
    env = store.get_env(event.branch)
    if env is None:
        # a push to a branch we never saw created (engine was offline) → ensure
        create_env_row(request, store, EnvCreate(branch=event.branch, seed=None))
        job_id = runner.submit("ensure", event.branch, {})
        return {"accepted": True, "action": "ensure", "job_id": job_id, "branch": event.branch}
    params: dict = {}
    if event.sha:
        params["sha"] = event.sha
    try:
        job_id = runner.submit("deploy", event.branch, params)
    except FlowError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {"accepted": True, "action": "deploy", "job_id": job_id, "branch": event.branch}


@router.get("/hook")
def hook_info() -> dict:
    return {
        "usage": "POST forge push webhooks here",
        "dry_run": dry_run_active(),
        "note": "signature verification fails closed; see docs/api-contract.md",
    }
