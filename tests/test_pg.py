"""PG role administration: command shape (dry-run), validation, SQL safety."""

from __future__ import annotations

import envd.config as config_mod
import pytest
from envd.core.pg import PASSWORD_OK, PgAdmin, PgError
from envd.core.shell import MASK, Shell
from envd.tools.pw import generate_password, is_sql_safe


@pytest.fixture()
def dry_run():
    config_mod.set_dry_run(True)
    yield
    config_mod.set_dry_run(False)


@pytest.fixture()
def admin(config, dry_run):
    sh = Shell(redactions=["pg-pw-secret-123456"])
    return PgAdmin(config, sh)


class TestValidation:
    def test_generated_passwords_are_sql_safe(self):
        for _ in range(20):
            assert is_sql_safe(generate_password())
        assert PASSWORD_OK.match("pg-pw-secret-123456")

    def test_bad_role_rejected(self, admin):
        with pytest.raises(PgError, match="invalid role name"):
            admin.create_role('env_x"; DROP TABLE t; --', "some-password-123456")

    def test_bad_password_rejected(self, admin):
        with pytest.raises(PgError, match="charset"):
            admin.create_role("env_feature_x", "short")
        with pytest.raises(PgError, match="charset"):
            admin.create_role("env_feature_x", "has'quote")

    def test_role_ok_passes_validation(self, admin):
        admin.create_role("env_feature_x", "pg-pw-secret-123456")  # must not raise


class TestCommands:
    def test_create_role_command_shape(self, config, admin):
        log: list[str] = []
        admin.shell._log = log.append
        admin.create_role("env_feature_x", "pg-pw-secret-123456")
        line = log[0]
        assert "docker compose" in line and "psql" in line
        assert 'CREATE ROLE "env_feature_x"' in line
        assert "CREATEDB" in line
        # the password never appears in the log (redaction)
        assert "pg-pw-secret-123456" not in line
        assert MASK in line

    def test_drop_role_guarded_by_exists(self, config, admin, monkeypatch):
        log: list[str] = []
        admin.shell._log = log.append
        # in dry-run, role_exists() reads no output → False → no DROP issued
        assert admin.drop_role("env_feature_x") is False
        assert "DROP ROLE" not in "\n".join(log)
        # but the existence probe did run
        assert "pg_roles" in "\n".join(log)

    def test_role_exists_dry_run_false(self, config, admin):
        assert admin.role_exists("env_feature_x") is False

    def test_infra_compose_default(self, config, admin):
        assert admin.infra_compose == config.paths.root / "infra" / "docker-compose.yml"
        assert admin.admin_user == "postgres"
