"""Shared dependencies: auth (PAT + scopes), pagination, app-state accessors."""

from __future__ import annotations

from fastapi import Depends, HTTPException, Query, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from envd.config import Config
from envd.core.jobs import JobRunner
from envd.core.state import Store, scope_satisfied

_bearer = HTTPBearer(auto_error=True)


def get_cfg(request: Request) -> Config:
    return request.app.state.cfg


def get_store(request: Request) -> Store:
    return request.app.state.store


def get_runner(request: Request) -> JobRunner:
    return request.app.state.runner


def require_scope(scope: str):
    """Bearer PAT auth with the scope hierarchy admin ⊃ write ⊃ read."""

    def dependency(
        request: Request,
        credentials: HTTPAuthorizationCredentials = Depends(_bearer),
    ) -> dict:
        record = request.app.state.store.verify_token(credentials.credentials)
        if record is None or not scope_satisfied(record["scopes"], scope):
            raise HTTPException(status_code=403, detail="invalid token or insufficient scope")
        return record

    return dependency


class Page:
    def __init__(
        self,
        page: int = Query(default=1, ge=1),
        per_page: int = Query(default=50, ge=1, le=100),
    ) -> None:
        self.page = page
        self.per_page = per_page

    def slice(self, items: list) -> list:
        start = (self.page - 1) * self.per_page
        return items[start : start + self.per_page]


def public_env(env: dict) -> dict:
    """Env row without secrets — list/detail-safe for read scope."""
    out = dict(env)
    out.pop("pg_password", None)
    out.pop("admin_password", None)
    return out


def secret_env(env: dict) -> dict:
    """Env row for write-scoped detail: keeps admin login, never the PG role."""
    out = dict(env)
    out.pop("pg_password", None)
    return out
