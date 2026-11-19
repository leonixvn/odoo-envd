"""Application factory: create_app(cfg, store, runner) → FastAPI."""

from __future__ import annotations

from fastapi import FastAPI

import envd
from envd.api.routers import backups, envs, events, hook, jobs, system
from envd.config import Config
from envd.core.jobs import JobRunner
from envd.core.state import Store


def create_app(cfg: Config, store: Store, runner: JobRunner) -> FastAPI:
    app = FastAPI(
        title="odoo-envd",
        version=envd.__version__,
        description=(
            "CD engine for Odoo branch→database environments. Mutations return "
            "202 + job_id; poll /api/jobs/{id} or subscribe /api/events (SSE). "
            "The contract is frozen in docs/api-contract.md."
        ),
        docs_url="/api/docs",
        openapi_url="/api/openapi.json",
    )
    app.state.cfg = cfg
    app.state.store = store
    app.state.runner = runner

    for router in (envs, backups, jobs, system, hook, events):
        app.include_router(router.router, prefix="/api")
    return app
