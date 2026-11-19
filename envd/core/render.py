"""Rendering of per-env artifacts: odoo.conf, compose.yml, .env.

Contract with the official odoo docker image entrypoint (studied in
``odoo/docker 20.0/entrypoint.sh``): the entrypoint injects
``--db_host/--db_port/--db_user/--db_password`` and runs ``wait-for-psql.py``
only when the config file **lacks** those keys. Therefore the rendered
``odoo.conf`` must never contain db connection keys — they travel through the
compose env vars (HOST/PORT/USER/PASSWORD). This invariant is asserted on every
render so a future template edit cannot break it silently.
"""

from __future__ import annotations

import re
from pathlib import Path
from string import Template

from envd.config import Config
from envd.tools.slug import host_label

FORBIDDEN_CONF_KEYS = re.compile(r"^\s*db_(host|port|user|password)\s*=", re.MULTILINE)

TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "templates"


class RenderError(ValueError):
    pass


def render_env(
    cfg: Config,
    *,
    slug: str,
    db_name: str,
    pg_user: str,
    pg_password: str,
    image_tag: str,
    workers: int = 0,
    max_cron_threads: int = 0,
    direct_port: int | None = None,
) -> dict[str, Path]:
    """Render the per-env compose project (compose.yml, odoo.conf, .env).

    Returns the written paths. Raises RenderError on template contract issues.
    """
    if not image_tag:
        raise RenderError("image_tag must be a non-empty docker tag")

    project_dir = cfg.paths.envs / slug / "project"
    project_dir.mkdir(parents=True, exist_ok=True)
    worktree = cfg.paths.envs / slug / "repo"
    backups_dir = cfg.paths.backups / slug
    backups_dir.mkdir(parents=True, exist_ok=True)

    ctx = {
        "slug": slug,
        "host_label": host_label(slug),
        "domain": cfg.server.domain,
        "db_name": db_name,
        "pg_host": cfg.postgres.host,
        "pg_port": str(cfg.postgres.port),
        "pg_user": pg_user,
        "pg_password": pg_password,
        "image_tag": image_tag,
        "workers": str(workers),
        "max_cron_threads": str(max_cron_threads),
        "worktree_addons": str(worktree / "addons"),
        "worktree_third_party": str(worktree / "third_party"),
        "enterprise_dir": str(cfg.paths.enterprise),
        "data_dir": str(cfg.paths.data),
        "backups_dir": str(backups_dir),
        "direct_port": str(direct_port) if direct_port else "",
    }

    conf = _render(TEMPLATES_DIR / "odoo.conf.tpl", ctx)
    if FORBIDDEN_CONF_KEYS.search(conf):
        raise RenderError(
            "rendered odoo.conf contains db connection keys; the docker image "
            "entrypoint must inject them via env vars (see module docstring)"
        )
    compose = _render(TEMPLATES_DIR / "compose.odoo.yml.tpl", ctx)
    compose = compose.replace(
        "    # OPTIONAL_PORTS\n",
        f'    ports:\n      - "{ctx["direct_port"]}:8069"\n' if ctx["direct_port"] else "",
    )
    env_file = _render_dotenv(ctx)

    paths = {
        "project_dir": project_dir,
        "compose": project_dir / "compose.yml",
        "conf": project_dir / "odoo.conf",
        "env": project_dir / ".env",
    }
    paths["compose"].write_text(compose, encoding="utf-8")
    paths["conf"].write_text(conf, encoding="utf-8")
    paths["env"].write_text(env_file, encoding="utf-8")
    return paths


def _render(tpl_path: Path, ctx: dict[str, str]) -> str:
    if not tpl_path.is_file():
        raise RenderError(f"template not found: {tpl_path}")
    tpl = Template(tpl_path.read_text(encoding="utf-8"))
    try:
        return tpl.substitute(ctx)
    except KeyError as exc:
        raise RenderError(f"template {tpl_path.name} needs value for {exc}") from exc


def _render_dotenv(ctx: dict[str, str]) -> str:
    # Read by the docker entrypoint and by compose variable substitution.
    lines = [
        f"HOST={ctx['pg_host']}",
        f"PORT={ctx['pg_port']}",
        f"USER={ctx['pg_user']}",
        f"PASSWORD={ctx['pg_password']}",
        f"IMAGE_TAG={ctx['image_tag']}",
    ]
    if ctx["direct_port"]:
        lines.append(f"DIRECT_PORT={ctx['direct_port']}")
    return "\n".join(lines) + "\n"
