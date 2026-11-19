"""System info and health (healthz is unauthenticated)."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Response

import envd
from envd.api.deps import get_cfg, get_store, require_scope
from envd.config import Config, dry_run_active
from envd.core.state import Store

router = APIRouter(prefix="/system", tags=["system"])


@router.get("/info")
def system_info(
    cfg: Config = Depends(get_cfg),
    store: Store = Depends(get_store),
    _token: dict = Depends(require_scope("read")),
) -> dict:
    envs = store.list_envs()
    return {
        "engine_version": envd.__version__,
        "domain": cfg.server.domain,
        "listen": f"{cfg.server.listen_host}:{cfg.server.listen_port}",
        "base_image": cfg.docker.base_image,
        "forge": cfg.forge.type,
        "forge_repos": list(cfg.forge.repos),
        "postgres_host": cfg.postgres.host,
        "port_range": list(cfg.policy.port_range),
        "ports_in_use": sorted(store.used_ports()),
        "upgrade_strategy": cfg.policy.upgrade_strategy,
        "on_deploy_failure": cfg.policy.on_deploy_failure,
        "prune_ttl_days": cfg.policy.prune_ttl_days,
        "retention": {
            "pre_deploy": cfg.policy.retention.pre_deploy,
            "daily": cfg.policy.retention.daily,
            "manual": cfg.policy.retention.manual,
        },
        "protect": list(cfg.policy.protect),
        "env_count": len(envs),
        "running": len([e for e in envs if e["state"] == "running"]),
        "dry_run": dry_run_active(),
    }


@router.get("/healthz")
def healthz(store: Store = Depends(get_store)) -> Response:
    """204 when the store answers — used by monitoring and the bootstrap check."""
    store.list_envs()
    return Response(status_code=204)
