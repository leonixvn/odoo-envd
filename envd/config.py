"""Engine configuration: envd.toml loader with validated defaults.

The config file is the single source of server-side truth: paths, forge
credentials, Postgres admin access, docker image pins and lifecycle policies.
Secrets never appear anywhere else (no repo content, no GUI payloads).
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

ForgeType = Literal["github", "gitlab", "gitea"]
UpgradeStrategy = Literal["diff", "outdated"]
DeployFailureAction = Literal["rollback", "halt"]


class ConfigError(ValueError):
    """Raised when envd.toml is structurally invalid or values are out of range."""


@dataclass(frozen=True)
class ServerConfig:
    listen_host: str = "127.0.0.1"
    listen_port: int = 8100
    domain: str = "odoo.example.com"


@dataclass(frozen=True)
class ForgeConfig:
    type: ForgeType = "github"
    secret: str = ""
    repos: tuple[str, ...] = ()


@dataclass(frozen=True)
class PostgresConfig:
    # Host as seen *from the env containers* (docker service name).
    host: str = "postgres"
    port: int = 5432
    # Engine-side admin access, used once per env to create the per-env role.
    admin_user: str = "postgres"
    admin_password: str = ""


@dataclass(frozen=True)
class DockerConfig:
    base_image: str = "odoo:20.0"
    compose_bin: str = "docker compose"


@dataclass(frozen=True)
class Retention:
    pre_deploy: int = 5
    daily: int = 7
    manual: int = 20


@dataclass(frozen=True)
class Policy:
    protect: tuple[str, ...] = ("main", "master")
    prune_ttl_days: int = 14
    retention: Retention = field(default_factory=Retention)
    weekly_full_upgrade: bool = True
    with_demo: bool = False
    country: str = ""
    language: str = "en_US"
    upgrade_strategy: UpgradeStrategy = "diff"
    on_deploy_failure: DeployFailureAction = "rollback"
    max_slug_len: int = 40
    job_workers: int = 4
    job_lock_timeout: int = 600
    port_range: tuple[int, int] = (8100, 8199)
    basic_auth_on_edge: bool = True


@dataclass(frozen=True)
class Paths:
    root: Path
    mirror: Path
    envs: Path
    data: Path
    backups: Path
    enterprise: Path
    state_db: Path
    logs: Path


@dataclass(frozen=True)
class Config:
    server: ServerConfig
    forge: ForgeConfig
    postgres: PostgresConfig
    docker: DockerConfig
    policy: Policy
    paths: Paths

    @property
    def dry_run(self) -> bool:
        return _DRY_RUN


_DRY_RUN = False


def set_dry_run(value: bool) -> None:
    """Global dry-run switch, honoured by the subprocess layer and the kit."""
    global _DRY_RUN
    _DRY_RUN = value


def dry_run_active() -> bool:
    """Read the global dry-run switch."""
    return _DRY_RUN


def load_config(path: str | Path) -> Config:
    """Load and validate envd.toml. Raises ConfigError with actionable messages."""
    p = Path(path)
    if not p.is_file():
        raise ConfigError(f"config file not found: {p}")
    try:
        raw = tomllib.loads(p.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"invalid TOML in {p}: {exc}") from exc

    root_raw = raw.get("paths", {}).get("root")
    if not root_raw:
        raise ConfigError("[paths] root is required (server-side base directory)")
    root = Path(root_raw).expanduser()
    if not root.is_absolute():
        raise ConfigError(f"[paths] root must be absolute: {root_raw!r}")

    paths = _build_paths(root, raw.get("paths", {}))
    server = _section(raw, "server", ServerConfig)
    forge_raw = dict(raw.get("forge", {}))
    forge = ForgeConfig(
        type=_forge_type(forge_raw.get("type", "github")),
        secret=str(forge_raw.get("secret", "")),
        repos=tuple(str(r) for r in forge_raw.get("repos", [])),
    )
    postgres = _section(raw, "postgres", PostgresConfig)
    docker = _section(raw, "docker", DockerConfig)
    policy_raw = dict(raw.get("policy", {}))
    ret_raw = dict(policy_raw.get("retention", {}))
    retention = Retention(
        pre_deploy=_positive(ret_raw, "pre_deploy", Retention().pre_deploy),
        daily=_positive(ret_raw, "daily", Retention().daily),
        manual=_positive(ret_raw, "manual", Retention().manual),
    )
    protect = tuple(str(b) for b in policy_raw.get("protect", Policy().protect))
    if not protect:
        raise ConfigError("[policy] protect must list at least one protected branch")
    for name in protect:
        if not name or name.startswith("-"):
            raise ConfigError(f"[policy] invalid protected branch name {name!r}")
    port_range = policy_raw.get("port_range", Policy().port_range)
    if not isinstance(port_range, (list, tuple)) or len(port_range) != 2:
        raise ConfigError(f"[policy] port_range must be [low, high], got {port_range!r}")
    port_lo, port_hi = int(port_range[0]), int(port_range[1])
    if not (1024 <= port_lo < port_hi <= 65535):
        raise ConfigError(f"[policy] port_range invalid: ({port_lo}, {port_hi})")
    strategy = policy_raw.get("upgrade_strategy", Policy().upgrade_strategy)
    if strategy not in ("diff", "outdated"):
        raise ConfigError(f"[policy] upgrade_strategy must be 'diff'|'outdated', got {strategy!r}")
    failure = policy_raw.get("on_deploy_failure", Policy().on_deploy_failure)
    if failure not in ("rollback", "halt"):
        raise ConfigError(f"[policy] on_deploy_failure must be 'rollback'|'halt', got {failure!r}")
    max_slug_len = _positive(policy_raw, "max_slug_len", Policy().max_slug_len)
    if max_slug_len > 59:
        # names are prefixed env_ and PostgreSQL identifiers are limited to 63 chars
        raise ConfigError("[policy] max_slug_len must be <= 59 (env_ prefix + PG 63-char limit)")
    policy = Policy(
        protect=protect,
        prune_ttl_days=_positive(policy_raw, "prune_ttl_days", Policy().prune_ttl_days),
        retention=retention,
        weekly_full_upgrade=bool(policy_raw.get("weekly_full_upgrade", True)),
        with_demo=bool(policy_raw.get("with_demo", False)),
        country=str(policy_raw.get("country", "")),
        language=str(policy_raw.get("language", "en_US")),
        upgrade_strategy=strategy,
        on_deploy_failure=failure,
        max_slug_len=max_slug_len,
        job_workers=_positive(policy_raw, "job_workers", Policy().job_workers),
        job_lock_timeout=_positive(policy_raw, "job_lock_timeout", Policy().job_lock_timeout),
        port_range=(int(port_lo), int(port_hi)),
        basic_auth_on_edge=bool(policy_raw.get("basic_auth_on_edge", True)),
    )
    return Config(
        server=server, forge=forge, postgres=postgres,
        docker=docker, policy=policy, paths=paths,
    )


def _build_paths(root: Path, section: dict) -> Paths:
    def child(key: str, default: str) -> Path:
        value = section.get(key)
        if value is None:
            return root / default
        # relative overrides resolve against root, never against the process CWD
        p = Path(str(value)).expanduser()
        return p if p.is_absolute() else root / p

    return Paths(
        root=root,
        mirror=child("mirror", "repo.git"),
        envs=child("envs", "envs"),
        data=child("data", "data"),
        backups=child("backups", "backups"),
        enterprise=child("enterprise", "enterprise"),
        state_db=child("state_db", "envd/state.db"),
        logs=child("logs", "envd/logs"),
    )


def _section(raw: dict, name: str, cls):
    data = dict(raw.get(name, {}))
    unknown = set(data) - set(cls.__dataclass_fields__)
    if unknown:
        # typos (e.g. admin_passwd) must fail loudly, not silently use defaults
        raise ConfigError(f"[{name}] unknown keys: {sorted(unknown)}")
    return cls(**data)


def _forge_type(value: str) -> ForgeType:
    if value not in ("github", "gitlab", "gitea"):
        raise ConfigError(f"[forge] type must be github|gitlab|gitea, got {value!r}")
    return value


def _positive(section: dict, key: str, default: int) -> int:
    value = section.get(key, default)
    if not isinstance(value, int) or value <= 0:
        raise ConfigError(f"{key} must be a positive integer, got {value!r}")
    return value
