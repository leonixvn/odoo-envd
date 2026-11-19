"""Native odoo CLI + docker compose command wrappers.

Every Odoo-touching operation of the engine is a call into this module, which
builds the exact ``odoo-bin`` subcommands verified against the 20.0 source:

- ``db init``     cli/db.py:61-103   (creates DB + base + admin user)
- ``db dump``     cli/db.py:134-155  (zip with filestore; ``--no-filestore``)
- ``db load``     cli/db.py:107-130  (restores a zip; ``--force``, ``--neutralize``)
- ``db duplicate`` cli/db.py:157-170 (filestore-aware copy, optional neutralize)
- ``db drop``     cli/db.py:183-187  (removes DB + its filestore)
- ``module install`` cli/module.py:155-168 (``update_list`` + ``button_immediate_install``)
- ``module upgrade`` cli/module.py:189-208 (dependents cascade natively; ``--outdated``)
- ``module list`` cli/module.py:227-233 (name, disk version, db version, state, shortdesc)
- ``neutralize``  cli/neutralize.py:14-54 (in-place demo-safe)

Execution model: one-shot commands run as
``docker compose -p env_<slug> -f <project>/compose.yml run --rm odoo odoo …``
so they use the env's own image, config (ODOO_RC), data dir and filestore.
The image entrypoint (studied at odoo/docker 20.0/entrypoint.sh) prepends
``wait-for-psql.py`` and appends the db connection args taken from the
HOST/PORT/USER/PASSWORD env vars — our rendered conf deliberately omits
those keys (see envd/core/render.py).
"""

from __future__ import annotations

import re

from envd.config import Config
from envd.core.shell import RunResult, Shell

HEALTH_PROBE = "import urllib.request;urllib.request.urlopen('http://127.0.0.1:8069/web/login',timeout=5)"


class ComposeCmd:
    """docker compose invocation builder for one env's project."""

    def __init__(self, cfg: Config, slug: str) -> None:
        self.bin = cfg.docker.compose_bin.split()
        self.project = f"env_{slug}"
        self.compose_file = cfg.paths.envs / slug / "project" / "compose.yml"

    def base(self) -> list[str]:
        return [*self.bin, "-p", self.project, "-f", str(self.compose_file)]

    def run_odoo(self, odoo_args: list[str]) -> list[str]:
        return [*self.base(), "run", "--rm", "odoo", "odoo", *odoo_args]

    def up(self, force_recreate: bool = False) -> list[str]:
        args = [*self.base(), "up", "-d"]
        if force_recreate:
            args.append("--force-recreate")
        return args

    def down(self) -> list[str]:
        return [*self.base(), "down", "--remove-orphans"]

    def exec_odoo(self, cmd: list[str]) -> list[str]:
        return [*self.base(), "exec", "-T", "odoo", *cmd]


KNOWN_MODULE_STATES = frozenset(
    {"uninstallable", "uninstalled", "to install", "to upgrade", "to remove", "installed"}
)
MODULE_NAME_RE = re.compile(r"^[a-z0-9_]+$")


def parse_module_list(text: str) -> list[dict[str, str]]:
    """Parse ``odoo module list`` output.

    Line format (cli/module.py:148-153): name, disk(latest) version, db(installed)
    version, state, short description. States can be two words ("to upgrade",
    "to install", "to remove") and the description may contain spaces; log noise
    that does not match the shape is skipped. NB: the CLI's column names are
    inverted by design upstream; here the meanings are documented explicitly:
    ``disk_version`` = manifest on disk, ``db_version`` = version recorded in DB.
    """
    modules: list[dict[str, str]] = []
    for line in text.splitlines():
        tokens = line.strip().split()
        if len(tokens) < 4 or not MODULE_NAME_RE.match(tokens[0]):
            continue
        if tokens[3] == "to" and len(tokens) >= 5:
            state, description = "to " + tokens[4], " ".join(tokens[5:])
        else:
            state, description = tokens[3], " ".join(tokens[4:])
        if state not in KNOWN_MODULE_STATES:
            continue
        modules.append(
            {
                "name": tokens[0],
                "disk_version": tokens[1],
                "db_version": tokens[2],
                "state": state,
                "shortdesc": description,
            }
        )
    return modules


class OdooCLI:
    """Executes native odoo CLI commands inside the env's compose project."""

    def __init__(self, cfg: Config, slug: str, shell: Shell) -> None:
        self.cfg = cfg
        self.slug = slug
        self.db = f"env_{slug}"
        self.shell = shell
        self.compose = ComposeCmd(cfg, slug)

    # ------------------------------------------------------------ db ----

    def db_init(
        self,
        *,
        username: str = "admin",
        password: str,
        demo: bool = False,
        country: str = "",
        language: str = "en_US",
    ) -> RunResult:
        args = ["db", "init", self.db, "--username", username, "--password", password]
        if demo:
            args.append("--with-demo")
        if country:
            args += ["--country", country]
        args += ["--language", language]
        return self.shell.run(self.compose.run_odoo(args), timeout=1800)

    def db_dump(self, backup_name: str, *, with_filestore: bool = True,
                raw_pg_format: bool = False) -> RunResult:
        """Native backup. Paths are inside the container (/mnt/backups bind)."""
        args = ["db", "dump", self.db, f"/mnt/backups/{backup_name}"]
        if raw_pg_format:
            args += ["--format", "dump"]
        elif not with_filestore:
            args.append("--no-filestore")
        return self.shell.run(self.compose.run_odoo(args), timeout=3600)

    def db_load(self, backup_name: str, *, force: bool = True,
                neutralize: bool = False, move: bool = False) -> RunResult:
        """Native restore of a zip dump from /mnt/backups."""
        args = ["db", "load", self.db, f"/mnt/backups/{backup_name}"]
        if force:
            args.append("--force")
        if neutralize:
            args.append("--neutralize")
        if move:
            args.append("--move")
        return self.shell.run(self.compose.run_odoo(args), timeout=7200)

    def db_duplicate(self, source_db: str, *, force: bool = True,
                     neutralize: bool = False) -> RunResult:
        """Filestore-aware copy (seed → env, env → env)."""
        args = ["db", "duplicate", source_db, self.db]
        if force:
            args.append("--force")
        if neutralize:
            args.append("--neutralize")
        return self.shell.run(self.compose.run_odoo(args), timeout=7200)

    def db_drop(self) -> RunResult:
        return self.shell.run(self.compose.run_odoo(["db", "drop", self.db]), timeout=1800)

    # -------------------------------------------------------- module ----

    def module_install(self, modules: list[str]) -> RunResult | None:
        if not modules:
            return None
        args = ["module", "install", "-d", self.db, *modules]
        return self.shell.run(self.compose.run_odoo(args), timeout=3600)

    def module_upgrade(self, modules: list[str] | None = None, *,
                       outdated_only: bool = False) -> RunResult:
        """Upgrade named modules (dependents cascade), or all installed modules.

        ``outdated_only`` maps to ``--outdated``: only modules whose manifest
        version on disk is newer than the version recorded in the DB
        (cli/module.py:198-201; upstream field names are inverted by design).
        """
        args = ["module", "upgrade", "-d", self.db]
        targets = modules if modules is not None else ["all"]
        args += targets
        if outdated_only:
            args.append("--outdated")
        return self.shell.run(self.compose.run_odoo(args), timeout=7200)

    def module_list(self) -> RunResult:
        args = ["module", "list", "-d", self.db]
        return self.shell.run(self.compose.run_odoo(args), timeout=600, check=True)

    # ----------------------------------------------------- lifecycle ----

    def neutralize_db(self) -> RunResult:
        return self.shell.run(self.compose.run_odoo(["neutralize", "-d", self.db]), timeout=1800)

    def compose_up(self, force_recreate: bool = False) -> RunResult:
        return self.shell.run(self.compose.up(force_recreate), timeout=600)

    def compose_down(self) -> RunResult:
        return self.shell.run(self.compose.down(), timeout=600)

    def health_once(self) -> bool:
        """Single HTTP probe of the env's web client, exec'd in the container."""
        cmd = self.compose.exec_odoo(["python3", "-c", HEALTH_PROBE])
        return self.shell.probe(cmd, attempts=1)

    def health_wait(self, attempts: int = 30, interval: float = 2.0) -> bool:
        cmd = self.compose.exec_odoo(["python3", "-c", HEALTH_PROBE])
        return self.shell.probe(cmd, attempts=attempts, interval=interval, timeout=30)
