"""CLI entrypoint: check, token, flow, cron modes (serve excluded — blocking)."""

from __future__ import annotations

import io
from contextlib import redirect_stdout
from datetime import UTC, datetime, timedelta
from pathlib import Path

import envd.config as config_mod
import pytest
from envd.__main__ import main
from envd.core.jobs import JobRunner
from envd.core.state import Store
from envd.tools.pw import generate_password
from envd.tools.slug import db_slug

from test_gitops import RepoFactory, _git


@pytest.fixture()
def dry_run():
    config_mod.set_dry_run(True)
    yield
    config_mod.set_dry_run(False)


def cli(argv: list[str]) -> tuple[int, str]:
    out = io.StringIO()
    with redirect_stdout(out):
        rc = main(argv)
    return rc, out.getvalue()


@pytest.fixture()
def server(tmp_path: Path, dry_run):
    factory = RepoFactory(tmp_path)
    factory.commit({"addons/mod_a/__manifest__.py": "{'name': 'A'}"})
    _git(factory.seed, "checkout", "-b", "feature/x")
    factory.commit({"apps.txt": "crm\n"}, branch="feature/x")
    mirror = tmp_path / "repo.git"
    factory.clone_mirror(mirror)
    root = tmp_path / "srv"
    root.mkdir()
    toml = tmp_path / "envd.toml"
    toml.write_text(
        f"[server]\ndomain = 'staging.example.com'\n\n"
        f"[paths]\nroot = '{root}'\nmirror = '{mirror}'\n",
        encoding="utf-8",
    )
    cfg_store = Store(root / "envd" / "state.db")
    slug = db_slug("feature/x")
    cfg_store.create_env(
        branch="feature/x", slug=slug, db=f"env_{slug}",
        url="https://feature-x.staging.example.com",
        admin_password=generate_password(), pg_password=generate_password(),
    )
    runner = JobRunner(_load_cfg(toml), cfg_store)
    job = runner.submit("ensure", "feature/x", foreground=True)
    assert cfg_store.get_job(job)["state"] == "success"
    return {"toml": toml, "root": root, "store": cfg_store}


def _load_cfg(toml: Path):
    from envd.config import load_config

    return load_config(toml)


class TestCheck:
    def test_check_ok(self, server):
        rc, out = cli(["-c", str(server["toml"]), "check"])
        assert rc == 0
        assert "config OK" in out
        assert "staging.example.com" in out

    def test_check_missing_config(self, tmp_path: Path):
        rc, _ = cli(["-c", str(tmp_path / "nope.toml"), "check"])
        assert rc == 2


class TestToken:
    def test_create_list_delete(self, server):
        rc, out = cli(["-c", str(server["toml"]), "token", "create",
                       "--name", "gui", "--scopes", "admin"])
        assert rc == 0
        assert "envd_" in out
        token = next(line for line in out.splitlines() if line.startswith("envd_"))
        assert server["store"].verify_token(token) is not None

        rc, out = cli(["-c", str(server["toml"]), "token", "list"])
        assert rc == 0 and "gui" in out
        rc, out = cli(["-c", str(server["toml"]), "token", "delete", "--id", "1"])
        assert rc == 0 and "deleted" in out
        assert server["store"].verify_token(token) is None


class TestFlow:
    def test_unknown_flow_rejected(self, server):
        rc, _ = cli(["-c", str(server["toml"]), "flow", "explode", "feature/x"])
        assert rc == 2

    def test_missing_env_rejected(self, server):
        rc, _ = cli(["-c", str(server["toml"]), "flow", "deploy", "nope/branch"])
        assert rc == 3

    def test_deploy_success_and_log_path(self, server):
        rc, out = cli(["-c", str(server["toml"]), "flow", "deploy", "feature/x"])
        assert rc == 0
        assert "success" in out
        assert "log:" in out


class TestCron:
    def test_daily_backup_runs_for_running_envs(self, server):
        rc, out = cli(["-c", str(server["toml"]), "cron", "daily-backup"])
        assert rc == 0
        assert "feature/x: success" in out
        backups = server["store"].list_backups("feature/x", kind="daily")
        assert len(backups) == 1

    def test_prune_destroys_stale_unpinned(self, server):
        old = (datetime.now(UTC) - timedelta(days=30)).isoformat()
        server["store"].update_env("feature/x", last_deploy_at=old)
        rc, out = cli(["-c", str(server["toml"]), "cron", "prune"])
        assert rc == 0
        assert "pruned:success" in out
        assert server["store"].get_env("feature/x") is None

    def test_prune_keeps_fresh_and_pinned(self, server):
        # fresh env stays
        rc, out = cli(["-c", str(server["toml"]), "cron", "prune"])
        assert rc == 0
        assert server["store"].get_env("feature/x") is not None
        # pinned env stays even when stale
        old = (datetime.now(UTC) - timedelta(days=30)).isoformat()
        server["store"].update_env("feature/x", last_deploy_at=old, pinned=1)
        rc, out = cli(["-c", str(server["toml"]), "cron", "prune"])
        assert "pruned" not in out
        assert server["store"].get_env("feature/x") is not None

    def test_weekly_upgrade_submits_full_mode(self, server):
        rc, out = cli(["-c", str(server["toml"]), "cron", "weekly-upgrade"])
        assert rc == 0
        assert "feature/x: success" in out
        jobs = server["store"].list_jobs(env_branch="feature/x")
        assert any(j["params"].get("mode") == "full" for j in jobs)
