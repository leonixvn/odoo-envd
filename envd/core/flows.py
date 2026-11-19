"""Operational flows: step sequences executed by the job runner.

A flow is a pure function ``FlowContext → list[Step]``. Steps are small named
actions; the runner executes them in order, records per-step state in the job
row, streams logs, and handles failure policy. All Odoo operations go through
:class:`envd.core.odoo_cli.OdooCLI` (native CLI inside the env's compose
project); all git operations through :class:`envd.core.gitops.GitOps`.

Rollback semantics (the deploy safety net): ``odoo module upgrade`` commits
per-module (odoo/modules/loading.py:236-253), so a failed upgrade can leave a
partially upgraded database. The deploy flow therefore takes a safety dump
(``odoo db dump --no-filestore``) *before* upgrading and, on failure with the
``rollback`` policy, restores it via ``odoo db load --force`` and raises
:class:`RolledBack` — the env keeps serving the previous sha.
"""

from __future__ import annotations

import shutil
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from envd.config import Config, dry_run_active
from envd.core.gitops import GitOps
from envd.core.odoo_cli import OdooCLI, parse_module_list
from envd.core.pg import PgAdmin
from envd.core.render import render_env
from envd.core.shell import Shell, ShellError
from envd.core.state import Store, utcnow
from envd.tools.slug import db_name as env_db_name
from envd.tools.slug import db_slug


class FlowError(RuntimeError):
    pass


class RolledBack(FlowError):
    """A deploy failed and the pre-deploy safety dump was restored."""

    def __init__(self, message: str, kept_sha: str | None) -> None:
        self.kept_sha = kept_sha
        super().__init__(message)


@dataclass
class Step:
    name: str
    action: Callable[[FlowContext], None]


@dataclass
class FlowContext:
    cfg: Config
    store: Store
    shell: Shell
    env: dict
    params: dict
    git: GitOps
    odoo: OdooCLI
    pg: PgAdmin
    emit: Callable[[str, dict], None]
    # scratch state shared by the steps of one flow execution
    flow: dict = field(default_factory=dict)


def _slug(ctx: FlowContext) -> str:
    return ctx.env["slug"]


def _log(ctx: FlowContext, message: str) -> None:
    ctx.shell.log(message)


# --------------------------------------------------------------------- ensure


def ensure_flow(ctx: FlowContext) -> list[Step]:
    return [
        Step("fetch", _ensure_fetch),
        Step("worktree", _ensure_worktree),
        Step("pg-role", _ensure_role),
        Step("deps-image", _ensure_deps_image),
        Step("render", _ensure_render),
        Step("database", _ensure_database),
        Step("modules", _ensure_modules),
        Step("compose-up", _ensure_compose_up),
        Step("health", _ensure_health),
    ]


def _ensure_fetch(ctx: FlowContext) -> None:
    ctx.git.fetch()
    ctx.flow["new_sha"] = ctx.git.sha_of(ctx.env["branch"])


def _ensure_worktree(ctx: FlowContext) -> None:
    ctx.git.worktree_add(_slug(ctx), ctx.env["branch"])


def _ensure_role(ctx: FlowContext) -> None:
    role = ctx.env["db_name"]
    if not ctx.pg.role_exists(role):
        ctx.pg.create_role(role, ctx.env["pg_password"])


def _ensure_deps_image(ctx: FlowContext) -> None:
    """Build the per-env pip-deps layer when the branch's requirements changed.

    Docker layer caching makes an unchanged hash a no-op; the image tag is
    recorded on the env row so every later render uses it.
    """
    branch = ctx.env["branch"]
    requirements = ctx.git.read_branch_file(branch, "requirements.txt")
    if requirements is None:
        ctx.env = ctx.store.update_env(ctx.env["branch"], image_tag=ctx.cfg.docker.base_image)
        ctx.flow["image_tag"] = ctx.cfg.docker.base_image
        return
    new_hash = ctx.git.requirements_hash(branch)
    if new_hash == ctx.env.get("deps_hash") and ctx.env.get("image_tag"):
        ctx.flow["image_tag"] = ctx.env["image_tag"]
        return
    image_tag = f"envd-env-{_slug(ctx)}:latest"
    dockerfile = ctx.cfg.paths.envs / _slug(ctx) / "project" / "Dockerfile.deps"
    dockerfile.parent.mkdir(parents=True, exist_ok=True)
    dockerfile.write_text(
        f"FROM {ctx.cfg.docker.base_image}\n"
        "COPY requirements.txt /tmp/deps-requirements.txt\n"
        "RUN pip install --no-cache-dir -r /tmp/deps-requirements.txt\n",
        encoding="utf-8",
    )
    worktree = ctx.git.worktree_path(_slug(ctx))
    ctx.shell.run(
        ["docker", "build", "-t", image_tag, "-f", str(dockerfile), str(worktree)],
        timeout=1800,
    )
    ctx.env = ctx.store.update_env(
        ctx.env["branch"], image_tag=image_tag, deps_hash=new_hash
    )
    ctx.flow["image_tag"] = image_tag


def _ensure_render(ctx: FlowContext) -> None:
    render_env(
        ctx.cfg,
        slug=_slug(ctx),
        db_name=ctx.env["db_name"],
        pg_user=ctx.env["db_name"],
        pg_password=ctx.env["pg_password"],
        image_tag=ctx.flow["image_tag"],
        workers=ctx.env.get("workers") or 0,
        max_cron_threads=0,
        direct_port=ctx.env.get("direct_port"),
    )


def _ensure_database(ctx: FlowContext) -> None:
    if ctx.pg.db_exists(ctx.env["db_name"]):
        _log(ctx, f"database {ctx.env['db_name']} already exists — skipping init")
        return
    seed = ctx.env.get("seed") or ctx.params.get("seed")
    if seed:
        # seed is stored as a BRANCH name; DB names derive from its slug
        source_db = env_db_name(db_slug(seed, max_len=ctx.cfg.policy.max_slug_len))
        ctx.odoo.db_duplicate(
            source_db, force=True, neutralize=bool(ctx.params.get("neutralize"))
        )
        ctx.flow["db_created"] = True
        ctx.flow["from_seed"] = True
        return
    ctx.odoo.db_init(
        password=ctx.env["admin_password"],
        demo=bool(ctx.env["with_demo"]),
        country=ctx.cfg.policy.country,
        language=ctx.cfg.policy.language,
    )
    ctx.flow["db_created"] = True


def _ensure_modules(ctx: FlowContext) -> None:
    if not ctx.flow.get("db_created") or ctx.flow.get("from_seed"):
        # seed databases already carry their modules; missing db → init did the minimum
        return
    apps = ctx.git.apps(ctx.env["branch"])
    if not apps:
        ctx.emit(
            "env.warning",
            {"branch": ctx.env["branch"], "message": "apps.txt missing or empty — base only"},
        )
        return
    ctx.odoo.module_install(apps)


def _ensure_compose_up(ctx: FlowContext) -> None:
    ctx.odoo.compose_up(force_recreate=False)


def _installed_count(ctx: FlowContext) -> int:
    """Live count of installed modules via the native ``odoo module list``."""
    result = ctx.odoo.module_list()
    modules = parse_module_list(result.output)
    return len([m for m in modules if m["state"] == "installed"])


def _ensure_health(ctx: FlowContext) -> None:
    if not ctx.odoo.health_wait():
        raise FlowError(f"env {ctx.env['branch']} did not become healthy in time")
    fields: dict = {
        "state": "running",
        "last_deploy_at": utcnow(),
        "installed_count": _installed_count(ctx),
    }
    # import flows reuse this step without a fresh sha — never wipe last_sha
    if ctx.flow.get("new_sha"):
        fields["last_sha"] = ctx.flow["new_sha"]
    ctx.env = ctx.store.update_env(ctx.env["branch"], **fields)
    ctx.emit("env.created", {"branch": ctx.env["branch"], "url": ctx.env["url"]})


# -------------------------------------------------------------------- deploy


def deploy_flow(ctx: FlowContext) -> list[Step]:
    return [
        Step("fetch", _deploy_fetch),
        Step("diff", _deploy_diff),
        Step("safety-dump", _deploy_safety_dump),
        Step("upgrade", _deploy_upgrade),
        Step("recreate", _deploy_recreate),
        Step("health", _deploy_health),
    ]


def _deploy_fetch(ctx: FlowContext) -> None:
    ctx.git.fetch()
    ctx.flow["old_sha"] = ctx.env.get("last_sha")
    # the webhook knows the after-sha; trust it, else resolve from the mirror
    ctx.flow["new_sha"] = ctx.params.get("sha") or ctx.git.sha_of(ctx.env["branch"])


def _deploy_diff(ctx: FlowContext) -> None:
    old, new = ctx.flow.get("old_sha"), ctx.flow["new_sha"]
    if old == new and not ctx.params.get("mode"):
        # no new commits and no explicit API mode — nothing to deploy
        _log(ctx, f"env {ctx.env['branch']} already at {new} — nothing to deploy")
        ctx.flow["noop"] = True
        ctx.flow["changed"], ctx.flow["third_party"] = [], False
        return
    changed, third = ctx.git.changed_modules(_slug(ctx), old, new)
    ctx.flow["changed"], ctx.flow["third_party"] = changed, third


def _deploy_safety_dump(ctx: FlowContext) -> None:
    if ctx.flow.get("noop"):
        return
    ts = time.strftime("%Y%m%d_%H%M%S")
    name = f"pre-deploy/{ts}.zip"
    ctx.odoo.db_dump(name, with_filestore=False)
    host_path = ctx.cfg.paths.backups / _slug(ctx) / name
    size = host_path.stat().st_size if host_path.exists() else None
    ctx.store.add_backup(
        env_branch=ctx.env["branch"], kind="pre-deploy", path=str(host_path),
        sha=ctx.flow.get("old_sha"), image_tag=ctx.env.get("image_tag"),
        with_filestore=False, size_bytes=size,
    )
    ctx.flow["safety_zip"] = name
    for _backup_id, path in ctx.store.prune_backups(
        ctx.env["branch"], "pre-deploy", ctx.cfg.policy.retention.pre_deploy
    ):
        Path(path).unlink(missing_ok=True)


def _deploy_upgrade(ctx: FlowContext) -> None:
    if ctx.flow.get("noop"):
        return
    # explicit mode from the API/webhook wins over the env/config strategy
    mode = ctx.params.get("mode")
    strategy = mode or ctx.env.get("upgrade_strategy") or ctx.cfg.policy.upgrade_strategy
    escalation = ctx.git.escalate_to_full_upgrade(
        ctx.flow["changed"], ctx.flow["third_party"],
        installed=ctx.env.get("installed_count") or 50,
    )
    try:
        if strategy == "outdated":
            ctx.odoo.module_upgrade(["all"], outdated_only=True)
        elif strategy == "full":
            ctx.odoo.module_upgrade(["all"])
        elif escalation:
            _log(ctx, "escalating to full upgrade (third_party churn or broad change)")
            ctx.odoo.module_upgrade(["all"])
        elif ctx.flow["changed"]:
            ctx.odoo.module_upgrade(ctx.flow["changed"])
        else:
            _log(ctx, "no module-level changes — code-only redeploy")
    except ShellError as exc:
        policy = ctx.env.get("on_deploy_failure") or ctx.cfg.policy.on_deploy_failure
        if policy != "rollback" or not ctx.flow.get("safety_zip"):
            raise
        # stop the service first: db load --force drops the database, which
        # fails while the env container holds live connections
        _log(ctx, f"upgrade failed: {exc}; stopping service and restoring safety dump")
        ctx.odoo.compose_down()
        ctx.odoo.db_load(ctx.flow["safety_zip"], force=True)
        ctx.odoo.compose_up(force_recreate=True)
        if not ctx.odoo.health_wait(attempts=15):
            raise FlowError(
                f"rollback of {ctx.env['branch']} restored the dump but the env "
                "did not become healthy — manual attention needed"
            ) from exc
        _log(ctx, f"env restored to {ctx.flow.get('old_sha')} and back online")
        raise RolledBack(
            f"deploy failed and was rolled back to {ctx.flow.get('old_sha')}",
            ctx.flow.get("old_sha"),
        ) from exc


def _deploy_recreate(ctx: FlowContext) -> None:
    if ctx.flow.get("noop"):
        return
    ctx.odoo.compose_up(force_recreate=True)


def _deploy_health(ctx: FlowContext) -> None:
    if ctx.flow.get("noop"):
        ctx.store.add_event("job.info", {"branch": ctx.env["branch"], "noop": True})
        return
    if not ctx.odoo.health_wait():
        raise FlowError(f"env {ctx.env['branch']} did not become healthy after deploy")
    ctx.env = ctx.store.update_env(
        ctx.env["branch"], state="running", last_sha=ctx.flow["new_sha"],
        last_deploy_at=utcnow(), deploy_count=ctx.env.get("deploy_count", 0) + 1,
        installed_count=_installed_count(ctx),
    )
    ctx.emit("env.updated", {"branch": ctx.env["branch"], "sha": ctx.flow["new_sha"]})


# ------------------------------------------------------------------- destroy


def destroy_flow(ctx: FlowContext) -> list[Step]:
    return [
        Step("compose-down", _destroy_compose_down),
        Step("drop-database", _destroy_database),
        Step("drop-role", _destroy_role),
        Step("worktree", _destroy_worktree),
        Step("cleanup", _destroy_cleanup),
    ]


def _destroy_compose_down(ctx: FlowContext) -> None:
    ctx.odoo.compose_down()


def _destroy_database(ctx: FlowContext) -> None:
    if ctx.params.get("keep_db"):
        return
    if ctx.pg.db_exists(ctx.env["db_name"]):
        ctx.odoo.db_drop()


def _destroy_role(ctx: FlowContext) -> None:
    if ctx.params.get("keep_db"):
        return
    ctx.pg.drop_role(ctx.env["db_name"])


def _destroy_worktree(ctx: FlowContext) -> None:
    if ctx.git.worktree_exists(_slug(ctx)):
        ctx.git.worktree_remove(_slug(ctx))


def _destroy_cleanup(ctx: FlowContext) -> None:
    project = ctx.cfg.paths.envs / _slug(ctx)
    if not dry_run_active():
        # filesystem destruction only in real mode: in dry-run the git and
        # docker commands above were echoed, not run — removing their targets
        # would corrupt the mirror's worktree registry
        if not ctx.params.get("keep_backups"):
            shutil.rmtree(ctx.cfg.paths.backups / _slug(ctx), ignore_errors=True)
        shutil.rmtree(project, ignore_errors=True)
    ctx.store.delete_env(ctx.env["branch"])
    ctx.emit("env.destroyed", {"branch": ctx.env["branch"]})


# -------------------------------------------------------------------- backup


def backup_flow(ctx: FlowContext) -> list[Step]:
    return [Step("dump", _backup_dump), Step("register", _backup_register)]


def _backup_dump(ctx: FlowContext) -> None:
    kind = ctx.params.get("kind", "manual")
    if kind not in ("manual", "daily"):
        raise FlowError(f"backup kind must be 'manual' or 'daily', got {kind!r}")
    ts = time.strftime("%Y%m%d_%H%M%S")
    name = f"{kind}/{ts}.zip"
    ctx.odoo.db_dump(name, with_filestore=ctx.params.get("with_filestore", True))
    ctx.flow["backup"] = name
    ctx.flow["kind"] = kind


def _backup_register(ctx: FlowContext) -> None:
    host_path = ctx.cfg.paths.backups / _slug(ctx) / ctx.flow["backup"]
    size = host_path.stat().st_size if host_path.exists() else None
    ctx.store.add_backup(
        env_branch=ctx.env["branch"], kind=ctx.flow["kind"], path=str(host_path),
        sha=ctx.env.get("last_sha"), image_tag=ctx.env.get("image_tag"),
        with_filestore=True, size_bytes=size,
    )
    keep = {
        "manual": ctx.cfg.policy.retention.manual,
        "daily": ctx.cfg.policy.retention.daily,
    }.get(ctx.flow["kind"])
    if keep:
        for _backup_id, path in ctx.store.prune_backups(ctx.env["branch"], ctx.flow["kind"], keep):
            Path(path).unlink(missing_ok=True)
    ctx.emit("env.backed_up", {"branch": ctx.env["branch"], "backup": ctx.flow["backup"]})


# ------------------------------------------------------------------ rollback


def rollback_flow(ctx: FlowContext) -> list[Step]:
    return [
        Step("resolve", _rollback_resolve),
        Step("stop", _rollback_stop),
        Step("restore", _rollback_restore),
        Step("reset", _rollback_reset),
        Step("start", _rollback_start),
        Step("health", _rollback_health),
    ]


def _rollback_resolve(ctx: FlowContext) -> None:
    to = ctx.params.get("to", "pre-deploy")
    if to == "pre-deploy":
        backups = ctx.store.list_backups(ctx.env["branch"], kind="pre-deploy")
    elif to == "latest":
        backups = ctx.store.list_backups(ctx.env["branch"])
    else:
        backups = [b for b in [ctx.store.get_backup(int(to))] if b]
    if not backups:
        raise FlowError(f"no backup found for rollback ({to})")
    backup = backups[0]  # list_backups is newest-first
    zip_path = Path(backup["path"])
    if not zip_path.exists():
        raise FlowError(f"backup file missing on disk: {zip_path}")
    ctx.flow["backup_zip"] = zip_path.relative_to(ctx.cfg.paths.backups / _slug(ctx)).as_posix()
    ctx.flow["rollback_sha"] = ctx.params.get("sha") or backup.get("sha")
    ctx.emit(
        "env.rolling_back",
        {"branch": ctx.env["branch"], "backup": ctx.flow["backup_zip"]},
    )


def _rollback_stop(ctx: FlowContext) -> None:
    ctx.odoo.compose_down()


def _rollback_restore(ctx: FlowContext) -> None:
    ctx.odoo.db_load(ctx.flow["backup_zip"], force=True)


def _rollback_reset(ctx: FlowContext) -> None:
    ctx.git.fetch()
    if ctx.flow["rollback_sha"]:
        ctx.git.worktree_reset(_slug(ctx), ctx.flow["rollback_sha"])


def _rollback_start(ctx: FlowContext) -> None:
    ctx.odoo.compose_up(force_recreate=True)


def _rollback_health(ctx: FlowContext) -> None:
    if not ctx.odoo.health_wait():
        raise FlowError(f"env {ctx.env['branch']} did not become healthy after rollback")
    fields: dict = {"state": "running", "last_deploy_at": utcnow()}
    if ctx.flow.get("rollback_sha"):
        fields["last_sha"] = ctx.flow["rollback_sha"]
    ctx.env = ctx.store.update_env(ctx.env["branch"], **fields)
    ctx.emit("env.rolled_back", {"branch": ctx.env["branch"], "sha": ctx.env["last_sha"]})


# ------------------------------------------------------- neutralize/action


def neutralize_flow(ctx: FlowContext) -> list[Step]:
    return [Step("neutralize", _neutralize_db)]


def _neutralize_db(ctx: FlowContext) -> None:
    ctx.odoo.neutralize_db()
    ctx.emit("env.neutralized", {"branch": ctx.env["branch"]})


def action_flow(ctx: FlowContext) -> list[Step]:
    return [Step("compose", _compose_action)]


def _compose_action(ctx: FlowContext) -> None:
    action = ctx.params.get("action")
    if action not in ("start", "stop", "restart"):
        raise FlowError(f"action must be start|stop|restart, got {action!r}")
    ctx.odoo.compose_action(action)
    ctx.store.update_env(ctx.env["branch"], state="running" if action != "stop" else "stopped")
    ctx.emit("env.action", {"branch": ctx.env["branch"], "action": action})


def obfuscate_flow(ctx: FlowContext) -> list[Step]:
    return [Step("obfuscate", _obfuscate_db)]


def _obfuscate_db(ctx: FlowContext) -> None:
    password = ctx.params.get("pwd")
    if not password:
        raise FlowError("obfuscate requires 'pwd'")
    ctx.shell.log(
        "WARNING: odoo's own obfuscate tool states it is NOT safe for "
        "third-party transfer — use for support dumps only"
    )
    ctx.odoo.obfuscate(
        password,
        fields=ctx.params.get("fields"),
        unobfuscate=bool(ctx.params.get("unobfuscate")),
        vacuum=bool(ctx.params.get("vacuum")),
    )
    ctx.emit("env.obfuscated", {"branch": ctx.env["branch"]})


# ------------------------------------------------------------------ recreate


def recreate_flow(ctx: FlowContext) -> list[Step]:
    """Drop DB + role, then rebuild from the branch (fresh database)."""
    prelude = [
        Step("compose-down", _destroy_compose_down),
        Step("drop-database", _destroy_database),
        Step("drop-role", _destroy_role),
    ]
    return prelude + ensure_flow(ctx)


# --------------------------------------------------------------------- import


def import_flow(ctx: FlowContext) -> list[Step]:
    """Restore an external dump (URL or uploaded file) into an env.

    Native `odoo db load` (accepts URLs directly, cli/db.py:223-231); we
    download server-side so the zip is in the env's backups/imports dir and
    the container only ever reads local disk. Afterwards the env's code is
    aligned to the restored database via `module upgrade all --outdated`.
    """
    return [
        Step("fetch-dump", _import_fetch),
        Step("db-load", _import_load),
        Step("upgrade", _import_upgrade),
        Step("compose-up", _ensure_compose_up),
        Step("health", _ensure_health),
    ]


def _import_fetch(ctx: FlowContext) -> None:
    dump_name = ctx.params.get("dump_file")
    if dump_name:
        # already server-side (upload or cross-env restore): verify it exists
        host_path = ctx.cfg.paths.backups / _slug(ctx) / dump_name
        if not host_path.is_file():
            raise FlowError(f"dump file not found: {host_path}")
        ctx.flow["dump_name"] = dump_name
        return

    dump_url = ctx.params.get("dump_url")
    if not dump_url:
        raise FlowError("import requires dump_url or dump_file")
    base = dump_url.rstrip("/").rsplit("/", 1)[-1]
    safe_chars = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._-")
    if not base or not set(base) <= safe_chars or base.startswith("."):
        raise FlowError(f"unsafe dump filename derived from URL: {base!r}")
    ctx.flow["dump_name"] = f"imports/{base}"
    if dry_run_active():
        return
    import httpx

    target = ctx.cfg.paths.backups / _slug(ctx) / "imports" / base
    target.parent.mkdir(parents=True, exist_ok=True)
    with (
        httpx.Client(follow_redirects=True, timeout=600) as client,
        client.stream("GET", dump_url) as response,
    ):
        if response.status_code != 200:
            raise FlowError(f"dump download failed: HTTP {response.status_code}")
        with open(target, "wb") as fh:
            for chunk in response.iter_bytes(65536):
                fh.write(chunk)


def _import_load(ctx: FlowContext) -> None:
    ctx.odoo.db_load(
        ctx.flow["dump_name"],
        force=bool(ctx.params.get("force", True)),
        neutralize=bool(ctx.params.get("neutralize")),
    )


def _import_upgrade(ctx: FlowContext) -> None:
    # the restored DB was built from another code state: bring branch code up
    ctx.odoo.module_upgrade(["all"], outdated_only=True)


FLOWS: dict[str, Callable[[FlowContext], list[Step]]] = {
    "ensure": ensure_flow,
    "deploy": deploy_flow,
    "destroy": destroy_flow,
    "backup": backup_flow,
    "rollback": rollback_flow,
    "neutralize": neutralize_flow,
    "obfuscate": obfuscate_flow,
    "action": action_flow,
    "recreate": recreate_flow,
    "import": import_flow,
}
