"""Request bodies for the REST API (response shapes stay plain dicts)."""

from __future__ import annotations

from pydantic import BaseModel, Field


class EnvCreate(BaseModel):
    branch: str = Field(min_length=1, max_length=100)
    with_demo: bool = False
    seed: str | None = None
    neutralize: bool = False
    pinned: bool = False
    image_tag: str | None = None
    direct_port: int | None = Field(default=None, ge=1024, le=65535)


class EnvPatch(BaseModel):
    pinned: bool | None = None
    upgrade_strategy: str | None = None
    on_deploy_failure: str | None = None
    workers: int | None = Field(default=None, ge=0, le=16)
    image_tag: str | None = None
    direct_port: int | None = Field(default=None, ge=1024, le=65535)


class DeployRequest(BaseModel):
    mode: str = "changed"  # changed | full | outdated


class RollbackRequest(BaseModel):
    to: str = "pre-deploy"  # "pre-deploy" | "latest" | "<backup_id>"
    sha: str | None = None


class ActionRequest(BaseModel):
    action: str  # start | stop | restart


class BackupRequest(BaseModel):
    with_filestore: bool = True


class ObfuscateRequest(BaseModel):
    pwd: str = Field(min_length=4)
    fields: str | None = None
    unobfuscate: bool = False
    vacuum: bool = False


class DuplicateRequest(BaseModel):
    target_branch: str
    neutralize: bool = False


class ImportRequest(BaseModel):
    dump_url: str = Field(min_length=8)
    target_branch: str
    neutralize: bool = False


class RestoreRequest(BaseModel):
    target_branch: str | None = None
