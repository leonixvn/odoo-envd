"""Exact native command shapes for odoo CLI + compose (dry-run shell)."""

from __future__ import annotations

import envd.config as config_mod
import pytest
from envd.core.odoo_cli import ComposeCmd, OdooCLI, parse_module_list
from envd.core.shell import Shell


@pytest.fixture()
def dry_run():
    config_mod.set_dry_run(True)
    yield
    config_mod.set_dry_run(False)


@pytest.fixture()
def compose(config):
    return ComposeCmd(config, "feature_x")


class TestComposeCmd:
    def test_run_odoo_shape(self, compose, config):
        cmd = compose.run_odoo(["db", "drop", "env_feature_x"])
        prefix = ["docker", "compose", "-p", "env_feature_x",
                  "-f", str(config.paths.envs / "feature_x" / "project" / "compose.yml")]
        assert cmd == [*prefix, "run", "--rm", "odoo", "odoo", "db", "drop", "env_feature_x"]

    def test_up_down_exec(self, compose):
        assert compose.up(force_recreate=True)[-3:] == ["up", "-d", "--force-recreate"]
        assert compose.up()[-2:] == ["up", "-d"]
        assert compose.down()[-2:] == ["down", "--remove-orphans"]
        assert compose.exec_odoo(["python3", "-c", "x"])[-3:] == ["python3", "-c", "x"]


class TestOdooCLI:
    def test_db_init(self, config, dry_run):
        log: list[str] = []
        cli = OdooCLI(config, "feature_x", Shell(log=log.append))
        cli.db_init(password="admin-pw", demo=True, country="VN")
        line = log[0]
        assert "db init env_feature_x" in line
        assert "--with-demo" in line
        assert "--country VN" in line
        assert "--password admin-pw" in line

    def test_db_dump_variants(self, config, dry_run):
        log: list[str] = []
        cli = OdooCLI(config, "feature_x", Shell(log=log.append))
        cli.db_dump("daily/x.zip")
        assert "db dump env_feature_x /mnt/backups/daily/x.zip" in log[0]
        assert "--no-filestore" not in log[0]
        cli.db_dump("pre/x.zip", with_filestore=False)
        assert "--no-filestore" in log[1]
        cli.db_dump("raw/x", raw_pg_format=True)
        assert "--format dump" in log[2]

    def test_db_load_flags(self, config, dry_run):
        log: list[str] = []
        cli = OdooCLI(config, "feature_x", Shell(log=log.append))
        cli.db_load("pre/x.zip", neutralize=True)
        line = log[0]
        assert "db load env_feature_x /mnt/backups/pre/x.zip" in line
        assert "--force" in line and "--neutralize" in line

    def test_db_duplicate_and_drop(self, config, dry_run):
        log: list[str] = []
        cli = OdooCLI(config, "feature_x", Shell(log=log.append))
        cli.db_duplicate("env_seed", neutralize=True)
        assert "db duplicate env_seed env_feature_x" in log[0]
        assert "--neutralize" in log[0]
        cli.db_drop()
        assert "db drop env_feature_x" in log[1]

    def test_module_install(self, config, dry_run):
        log: list[str] = []
        cli = OdooCLI(config, "feature_x", Shell(log=log.append))
        assert cli.module_install([]) is None
        cli.module_install(["crm", "demo_app"])
        assert "module install -d env_feature_x crm demo_app" in log[0]

    def test_module_upgrade_modes(self, config, dry_run):
        log: list[str] = []
        cli = OdooCLI(config, "feature_x", Shell(log=log.append))
        cli.module_upgrade(["mod_a", "mod_b"])
        assert "module upgrade -d env_feature_x mod_a mod_b" in log[0]
        cli.module_upgrade(["all"], outdated_only=True)
        assert "module upgrade -d env_feature_x all --outdated" in log[1]

    def test_neutralize_and_health(self, config, dry_run):
        log: list[str] = []
        cli = OdooCLI(config, "feature_x", Shell(log=log.append))
        cli.neutralize_db()
        assert "neutralize -d env_feature_x" in log[0]
        cli.health_once()
        assert "exec -T odoo python3 -c" in log[1]
        assert "urllib.request.urlopen" in log[1]


class TestParseModuleList:
    def test_parse(self):
        text = (
            "base 20.0.1.6 20.0.1.6 installed Base\n"
            "demo_app 20.0.1.1.0 20.0.1.0.0 to upgrade Demo App\n"
        )
        mods = parse_module_list(text)
        assert mods[0]["name"] == "base"
        assert mods[0]["state"] == "installed"
        assert mods[1]["name"] == "demo_app"
        assert mods[1]["disk_version"] == "20.0.1.1.0"
        assert mods[1]["db_version"] == "20.0.1.0.0"
        assert mods[1]["shortdesc"] == "Demo App"

    def test_skips_noise(self):
        assert parse_module_list("INFO some log line\n") == []
        assert parse_module_list("") == []
