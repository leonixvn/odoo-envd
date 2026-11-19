"""PostgreSQL role administration for per-env isolation.

Each env connects as its own login role (``env_<slug>``, CREATEDB so
``odoo db init`` can create the database as that role — odoo's own path,
``cli/server.py:101-113``). A role can create and own databases but can never
read another env's database.

Execution model: ``psql`` runs *inside the infra postgres container* via
``docker compose exec`` — no host port exposure, no password on the engine
side (the official postgres image trusts local socket access for POSTGRES_USER).
"""

from __future__ import annotations

import re

from envd.config import Config
from envd.core.shell import RunResult, Shell

ROLE_OK = re.compile(r"^[a-z0-9_]{1,50}$")
PASSWORD_OK = re.compile(r"^[A-Za-z0-9_-]{16,128}$")


DB_NAME_OK = re.compile(r"^[a-z0-9_]{1,63}$")


class PgError(ValueError):
    pass


class PgAdmin:
    def __init__(self, cfg: Config, shell: Shell) -> None:
        self.cfg = cfg
        self.shell = shell
        self.infra_compose = cfg.paths.root / "infra" / "docker-compose.yml"
        self.admin_user = cfg.postgres.admin_user

    def _psql(self, sql: str, *, check: bool = True) -> RunResult:
        cmd = [
            "docker", "compose", "-f", str(self.infra_compose),
            "exec", "-T", "postgres",
            "psql", "-U", self.admin_user, "-d", "postgres",
            "-v", "ON_ERROR_STOP=1", "-c", sql,
        ]
        return self.shell.run(cmd, timeout=120, check=check)

    def _require_role(self, role: str) -> str:
        if not ROLE_OK.match(role):
            raise PgError(f"invalid role name {role!r}")
        return role

    def create_role(self, role: str, password: str) -> None:
        """Create the per-env login role. Validates the password charset so the
        SQL literal is safe by construction (no quotes or backslashes)."""
        self._require_role(role)
        if not PASSWORD_OK.match(password):
            raise PgError("password charset must be [A-Za-z0-9_-], 16-128 chars")
        sql = f'CREATE ROLE "{role}" LOGIN PASSWORD \'{password}\' CREATEDB;'
        self._psql(sql)

    def drop_role(self, role: str) -> bool:
        """Drop the role if it exists; returns True when it existed."""
        self._require_role(role)
        if not self.role_exists(role):
            return False
        # a role owning a database cannot be dropped; env teardown drops the
        # database first, so failure here means leftover objects worth a look
        self._psql(f'DROP ROLE "{role}";')
        return True

    def role_exists(self, role: str) -> bool:
        self._require_role(role)
        result = self._psql(
            f"SELECT 1 FROM pg_roles WHERE rolname = '{role}';", check=False
        )
        # psql prints "(1 row)" under the result grid when a row matched
        return result.rc == 0 and "(1 row)" in result.output

    def db_exists(self, db_name: str) -> bool:
        """True when the database exists (guards `odoo db init`, which refuses
        existing databases)."""
        if not DB_NAME_OK.match(db_name):
            raise PgError(f"invalid database name {db_name!r}")
        result = self._psql(
            f"SELECT 1 FROM pg_database WHERE datname = '{db_name}';", check=False
        )
        return result.rc == 0 and "(1 row)" in result.output
