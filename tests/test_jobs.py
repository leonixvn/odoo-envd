"""End-to-end job runs under dry-run: ensure → deploy → rollback → destroy.

Real git runs against a throwaway mirror; docker/psql/odoo commands are echoed
by the dry-run shell; Python-side artifacts (rendered project, state rows)
are real and asserted.
"""

from __future__ import annotations

from pathlib import Path

import envd.config as config_mod
import pytest
from envd.config import load_config
from envd.core.jobs import EnvLock, JobRunner
from envd.core.state import Store
from envd.tools.slug import db_slug

from test_gitops import RepoFactory, _git


@pytest.fixture()
def dry_run():
    config_mod.set_dry_run(True)
    yield
    config_mod.set_dry_run(False)


@pytest.fixture()
def server(tmp_path: Path, dry_run):
    """A minimal server: origin + mirror + config + store + runner."""
    factory = RepoFactory(tmp_path)
    factory.commit(
        {
            "addons/mod_a/__manifest__.py": "{'name': 'A', 'version': '20.0.1.0.0'}",
        }
    )
    _git(factory.seed, "checkout", "-b", "feature/x")
    first = factory.commit(
        {
            "apps.txt": "crm\nsale_management\nmod_a\n",
            "requirements.txt": "requests==2.32.0\n",
        },
        branch="feature/x",
    )
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
    cfg = load_config(toml)
    store = Store(root / "envd" / "state.db")
    runner = JobRunner(cfg, store)

    def add_commit(files: dict[str, str] | None = None) -> str:
        _git(factory.seed, "checkout", "feature/x")
        payload = files if files is not None else {"addons/mod_a/models/x.py": "y = 2"}
        return factory.commit(payload, branch="feature/x")

    return {
        "factory": factory,
        "cfg": cfg,
        "store": store,
        "runner": runner,
        "first_sha": first,
        "add_commit": add_commit,
    }


def make_env(server, branch="feature/x"):
    slug = db_slug(branch)
    return server["store"].create_env(
        branch=branch,
        slug=slug,
        db=f"env_{slug}",
        url=f"https://{slug}.staging.example.com",
        admin_password="admin-pw-1234567890",
        pg_password="pg-pw-1234567890",
    )


class TestEnsure:
    def test_full_ensure_marks_env_running(self, server):
        make_env(server)
        job_id = server["runner"].submit("ensure", "feature/x", foreground=True)
        job = server["store"].get_job(job_id)
        assert job["state"] == "success"
        assert [s["name"] for s in job["steps"]][:2] == ["fetch", "worktree"]
        env = server["store"].get_env("feature/x")
        assert env["state"] == "running"
        assert env["last_sha"] == server["first_sha"]
        # rendered artifacts are real
        project = server["cfg"].paths.envs / "feature_x" / "project"
        assert (project / "compose.yml").is_file()
        assert (project / "odoo.conf").is_file()
        assert (project / "Dockerfile.deps").is_file()
        # and the health step recorded the (dry-run) installed count
        assert env["installed_count"] == 0

    def test_ensure_is_idempotent(self, server):
        make_env(server)
        first = server["runner"].submit("ensure", "feature/x", foreground=True)
        second = server["runner"].submit("ensure", "feature/x", foreground=True)
        assert server["store"].get_job(first)["state"] == "success"
        assert server["store"].get_job(second)["state"] == "success"
        assert server["store"].get_env("feature/x")["state"] == "running"

    def test_failure_marks_env_failed(self, server, monkeypatch):
        make_env(server)
        runner = server["runner"]
        monkeypatch.setattr(
            "envd.core.flows._ensure_health", lambda ctx: (_ for _ in ()).throw(
                RuntimeError("probe failed")
            )
        )
        job_id = runner.submit("ensure", "feature/x", foreground=True)
        assert server["store"].get_job(job_id)["state"] == "failed"
        env = server["store"].get_env("feature/x")
        assert env["state"] == "failed"


class TestDeploy:
    def test_deploy_new_commits(self, server):
        make_env(server)
        server["runner"].submit("ensure", "feature/x", foreground=True)
        new_sha = server["add_commit"]()

        job_id = server["runner"].submit("deploy", "feature/x", foreground=True)
        job = server["store"].get_job(job_id)
        assert job["state"] == "success"
        env = server["store"].get_env("feature/x")
        assert env["last_sha"] == new_sha
        assert env["deploy_count"] == 1
        # a pre-deploy safety dump was registered
        backups = server["store"].list_backups("feature/x", kind="pre-deploy")
        assert len(backups) == 1
        assert backups[0]["sha"] == server["first_sha"]

    def test_deploy_noop_when_no_push(self, server):
        make_env(server)
        server["runner"].submit("ensure", "feature/x", foreground=True)
        job_id = server["runner"].submit("deploy", "feature/x", foreground=True)
        job = server["store"].get_job(job_id)
        assert job["state"] == "success"
        assert not server["store"].list_backups("feature/x", kind="pre-deploy")

    def test_escalation_to_full_upgrade_on_third_party(self, server):
        make_env(server)
        server["runner"].submit("ensure", "feature/x", foreground=True)
        server["add_commit"]({"third_party/oca_new/__manifest__.py": "{'name': 'N'}"})
        server["store"].update_env("feature/x", installed_count=1)
        job_id = server["runner"].submit("deploy", "feature/x", foreground=True)
        job = server["store"].get_job(job_id)
        assert job["state"] == "success"
        log = (server["cfg"].paths.logs / f"{job_id}.log").read_text()
        assert "escalating to full upgrade" in log


class TestRollback:
    def test_rollback_restores_predeploy(self, server):
        make_env(server)
        server["runner"].submit("ensure", "feature/x", foreground=True)
        old_sha = server["store"].get_env("feature/x")["last_sha"]
        server["add_commit"]()
        server["runner"].submit("deploy", "feature/x", foreground=True)

        # the safety dump was echoed, not written — materialize it so the
        # rollback can verify the file exists (as it would on a real server)
        backup = server["store"].list_backups("feature/x", kind="pre-deploy")[0]
        zip_path = Path(backup["path"])
        zip_path.parent.mkdir(parents=True, exist_ok=True)
        zip_path.write_bytes(b"PK\x03\x04fake-zip")

        job_id = server["runner"].submit("rollback", "feature/x", foreground=True)
        job = server["store"].get_job(job_id)
        assert job["state"] == "success"
        env = server["store"].get_env("feature/x")
        assert env["last_sha"] == old_sha


class TestDestroy:
    def test_destroy_removes_env_row(self, server):
        make_env(server)
        server["runner"].submit("ensure", "feature/x", foreground=True)
        job_id = server["runner"].submit("destroy", "feature/x", foreground=True)
        assert server["store"].get_job(job_id)["state"] == "success"
        assert server["store"].get_env("feature/x") is None
        # worktree directory survives dry-run (git removal was echoed only)


class TestEnvLock:
    def test_lock_serializes(self, tmp_path: Path):

        lock = EnvLock(tmp_path / "locks" / "env-x.lock", timeout=2.0)
        assert lock.acquire() is True
        other = EnvLock(tmp_path / "locks" / "env-x.lock", timeout=0.3)
        assert other.acquire() is False
        lock.release()
        assert other.acquire() is True
        other.release()

    def test_lock_context_manager(self, tmp_path: Path):
        with EnvLock(tmp_path / "locks" / "env-y.lock", timeout=1.0) as lock:
            assert lock._fd is not None


class TestJobMetadata:
    def test_unknown_kind_rejected(self, server):
        with pytest.raises(Exception, match="unknown job kind"):
            server["runner"].submit("explode", "feature/x", foreground=True)

    def test_cancel_queued(self, server):
        make_env(server)
        job_id = server["store"].create_job("deploy", "feature/x", {})
        assert server["runner"].cancel(job_id) is True
        assert server["store"].get_job(job_id)["state"] == "cancelled"
        assert server["runner"].cancel(job_id) is False

    def test_log_file_written_with_redactions(self, server):
        make_env(server)
        job_id = server["runner"].submit("ensure", "feature/x", foreground=True)
        log_file = server["cfg"].paths.logs / f"{job_id}.log"
        text = log_file.read_text()
        assert "CREATE ROLE" in text
        assert "pg-pw-1234567890" not in text
        assert "--- step: fetch" in text
