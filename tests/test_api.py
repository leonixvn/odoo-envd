"""REST API end-to-end under dry-run: auth, envs, backups, jobs, hook, SSE."""

from __future__ import annotations

import hashlib
import hmac
import json
import time
from pathlib import Path

import envd.config as config_mod
import pytest
from envd.api.app import create_app
from envd.config import load_config
from envd.core.jobs import JobRunner
from envd.core.state import Store
from fastapi.testclient import TestClient

from test_gitops import RepoFactory, _git

SECRET = "hook-secret"


def wait_job(store: Store, job_id: int, timeout: float = 10.0) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        job = store.get_job(job_id)
        if job["state"] not in ("queued", "running"):
            return job
        time.sleep(0.05)
    raise AssertionError(f"job {job_id} did not finish in {timeout}s")


@pytest.fixture()
def dry_run():
    config_mod.set_dry_run(True)
    yield
    config_mod.set_dry_run(False)


@pytest.fixture()
def server(tmp_path: Path, dry_run):
    factory = RepoFactory(tmp_path)
    factory.commit({"addons/mod_a/__manifest__.py": "{'name': 'A'}"})
    _git(factory.seed, "checkout", "-b", "feature/x")
    factory.commit(
        {"apps.txt": "crm\nmod_a\n", "requirements.txt": "requests==2.32.0\n"},
        branch="feature/x",
    )
    mirror = tmp_path / "repo.git"
    factory.clone_mirror(mirror)
    root = tmp_path / "srv"
    root.mkdir()
    toml = tmp_path / "envd.toml"
    toml.write_text(
        f"[server]\ndomain = 'staging.example.com'\n\n"
        f"[paths]\nroot = '{root}'\nmirror = '{mirror}'\n\n"
        f"[forge]\ntype = 'github'\nsecret = '{SECRET}'\nrepos = ['acme/odoo-custom']\n",
        encoding="utf-8",
    )
    cfg = load_config(toml)
    store = Store(root / "envd" / "state.db")
    runner = JobRunner(cfg, store)
    app = create_app(cfg, store, runner)
    return {
        "cfg": cfg,
        "store": store,
        "runner": runner,
        "factory": factory,
        "client": TestClient(app),
    }


@pytest.fixture()
def tokens(server) -> dict:
    _, admin = server["store"].create_token("admin", ("admin",))
    _, write = server["store"].create_token("writer", ("write",))
    _, read = server["store"].create_token("reader", ("read",))
    return {"admin": admin, "write": write, "read": read}


def auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture()
def ensured(server, tokens) -> dict:
    """An env that already went through the ensure flow."""
    response = server["client"].post(
        "/api/envs", json={"branch": "feature/x"}, headers=auth(tokens["write"])
    )
    assert response.status_code == 202
    body = response.json()
    job = wait_job(server["store"], body["job_id"])
    assert job["state"] == "success", job
    return body | {"env": server["store"].get_env("feature/x")}


class TestAuth:
    def test_healthz_open(self, server):
        assert server["client"].get("/api/system/healthz").status_code == 204

    def test_missing_token_401_and_bad_token_403(self, server):
        assert server["client"].get("/api/envs").status_code == 401
        assert server["client"].get("/api/envs", headers=auth("envd_bogus")).status_code == 403

    def test_read_token_cannot_write(self, server, tokens):
        response = server["client"].post(
            "/api/envs", json={"branch": "feature/x"}, headers=auth(tokens["read"])
        )
        assert response.status_code == 403

    def test_write_token_cannot_admin(self, server, tokens, ensured):
        slug = "feature_x"
        backup = server["client"].post(
            f"/api/envs/{slug}/backups", json={"with_filestore": False},
            headers=auth(tokens["write"]),
        ).json()
        wait_job(server["store"], backup["job_id"])
        backup_id = server["store"].list_backups("feature/x", kind="manual")[0]["id"]
        assert (
            server["client"].delete(
                f"/api/envs/feature_x/backups/{backup_id}", headers=auth(tokens["write"])
            ).status_code
            == 403
        )
        assert (
            server["client"].delete(
                f"/api/envs/feature_x/backups/{backup_id}", headers=auth(tokens["admin"])
            ).status_code
            == 200
        )


class TestEnvs:
    def test_create_and_list(self, server, tokens, ensured):
        response = server["client"].get("/api/envs", headers=auth(tokens["read"]))
        body = response.json()
        assert body["total"] == 1
        item = body["items"][0]
        assert item["branch"] == "feature/x"
        assert item["state"] == "running"
        assert item["url"] == "https://feature-x.staging.example.com"
        # public list view carries no credentials
        assert "pg_password" not in item and "admin_password" not in item

    def test_detail_has_admin_password(self, server, tokens, ensured):
        detail = server["client"].get(
            "/api/envs/feature_x", headers=auth(tokens["write"])
        ).json()
        assert detail["admin_password"]
        assert "pg_password" not in detail
        assert detail["backups"]["pre-deploy"] == 0

    def test_invalid_branch_422(self, server, tokens):
        response = server["client"].post(
            "/api/envs", json={"branch": "bad branch..name"}, headers=auth(tokens["write"])
        )
        assert response.status_code == 422

    def test_slug_collision_409(self, server, tokens, ensured):
        response = server["client"].post(
            "/api/envs", json={"branch": "feature-x"}, headers=auth(tokens["write"])
        )
        assert response.status_code == 409

    def test_create_existing_reuses(self, server, tokens, ensured):
        response = server["client"].post(
            "/api/envs", json={"branch": "feature/x"}, headers=auth(tokens["write"])
        )
        assert response.status_code == 202
        assert response.json()["reused_existing"] is True

    def test_patch_and_validation(self, server, tokens, ensured):
        assert (
            server["client"].patch(
                "/api/envs/feature_x", json={"pinned": True, "upgrade_strategy": "full"},
                headers=auth(tokens["write"]),
            ).json()["pinned"]
            is True
        )
        response = server["client"].patch(
            "/api/envs/feature_x", json={"upgrade_strategy": "wild"}, headers=auth(tokens["write"])
        )
        assert response.status_code == 422

    def test_deploy_modes(self, server, tokens, ensured):
        response = server["client"].post(
            "/api/envs/feature_x/deploy", json={"mode": "full"}, headers=auth(tokens["write"])
        )
        assert response.status_code == 202
        job = wait_job(server["store"], response.json()["job_id"])
        assert job["state"] == "success"
        log = (server["cfg"].paths.logs / f"{job['id']}.log").read_text()
        assert "module upgrade -d env_feature_x all" in log

    def test_action_and_state(self, server, tokens, ensured):
        response = server["client"].post(
            "/api/envs/feature_x/action", json={"action": "stop"}, headers=auth(tokens["write"])
        )
        job = wait_job(server["store"], response.json()["job_id"])
        assert job["state"] == "success"
        assert server["store"].get_env("feature/x")["state"] == "stopped"
        bad = server["client"].post(
            "/api/envs/feature_x/action", json={"action": "explode"}, headers=auth(tokens["write"])
        )
        assert bad.status_code == 422

    def test_destroy(self, server, tokens, ensured):
        response = server["client"].delete(
            "/api/envs/feature_x?keep_db=true", headers=auth(tokens["write"])
        )
        job = wait_job(server["store"], response.json()["job_id"])
        assert job["state"] == "success"
        assert server["store"].get_env("feature/x") is None

    def test_404_on_unknown_env(self, server, tokens):
        assert (
            server["client"].get("/api/envs/nope", headers=auth(tokens["write"])).status_code
            == 404
        )

    def test_neutralize_and_recreate(self, server, tokens, ensured):
        response = server["client"].post(
            "/api/envs/feature_x/neutralize", headers=auth(tokens["write"])
        )
        wait_job(server["store"], response.json()["job_id"])
        response = server["client"].post(
            "/api/envs/feature_x/recreate", headers=auth(tokens["write"])
        )
        job = wait_job(server["store"], response.json()["job_id"])
        assert job["state"] == "success"
        assert server["store"].get_env("feature/x")["state"] == "running"


class TestDuplicate:
    def test_duplicate_env(self, server, tokens, ensured):
        # the target branch must exist in the repo: env B deploys B's code
        _git(server["factory"].seed, "checkout", "-b", "demo/feature-x-copy")
        server["factory"].commit(
            {"apps.txt": "crm\nmod_a\n# copy marker\n"}, branch="demo/feature-x-copy"
        )
        response = server["client"].post(
            "/api/envs/feature_x/duplicate",
            json={"target_branch": "demo/feature-x-copy", "neutralize": True},
            headers=auth(tokens["write"]),
        )
        assert response.status_code == 202
        body = response.json()
        job = wait_job(server["store"], body["job_id"])
        assert job["state"] == "success", job
        # the duplicate went through the seed path (db duplicate, no module install)
        log = (server["cfg"].paths.logs / f"{job['id']}.log").read_text()
        assert "db duplicate env_feature_x" in log

    def test_duplicate_to_missing_branch_fails_cleanly(self, server, tokens, ensured):
        response = server["client"].post(
            "/api/envs/feature_x/duplicate",
            json={"target_branch": "demo/no-such-branch"},
            headers=auth(tokens["write"]),
        )
        body = response.json()
        job = wait_job(server["store"], body["job_id"])
        assert job["state"] == "failed"
        assert "not found in mirror" in (job["error"] or "")


class TestBackupsAndRollback:
    def _materialize_latest(self, server, branch: str, kind: str = "manual") -> Path:
        backup = server["store"].list_backups(branch, kind=kind)[0]
        path = Path(backup["path"])
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"PK\x03\x04fake")
        return path

    def test_manual_backup_and_rollback(self, server, tokens, ensured):
        response = server["client"].post(
            "/api/envs/feature_x/backups", json={"with_filestore": False},
            headers=auth(tokens["write"]),
        )
        job = wait_job(server["store"], response.json()["job_id"])
        assert job["state"] == "success"
        listed = server["client"].get(
            "/api/envs/feature_x/backups", headers=auth(tokens["read"])
        ).json()["items"]
        assert listed[0]["kind"] == "manual"
        self._materialize_latest(server, "feature/x")

        response = server["client"].post(
            "/api/envs/feature_x/rollback", json={"to": "latest"}, headers=auth(tokens["write"])
        )
        job = wait_job(server["store"], response.json()["job_id"])
        assert job["state"] == "success", job["error"]

    def test_backup_download_missing_file_410(self, server, tokens, ensured):
        response = server["client"].post(
            "/api/envs/feature_x/backups", json={}, headers=auth(tokens["write"])
        )
        wait_job(server["store"], response.json()["job_id"])
        backup_id = server["store"].list_backups("feature/x", kind="manual")[0]["id"]
        assert (
            server["client"].get(
                f"/api/envs/feature_x/backups/{backup_id}/download", headers=auth(tokens["write"])
            ).status_code
            == 410
        )


class TestHook:
    def _push(self, server, payload: dict) -> object:
        body = json.dumps(payload).encode()
        headers = {
            "X-Hub-Signature-256": "sha256="
            + hmac.new(SECRET.encode(), body, hashlib.sha256).hexdigest(),
            "Content-Type": "application/json",
        }
        return server["client"].post("/api/hook", content=body, headers=headers)

    def test_created_ensures_env(self, server, tokens):
        response = self._push(server, {
            "ref": "refs/heads/feature/x",
            "after": "abc",
            "created": True,
            "repository": {"full_name": "acme/odoo-custom"},
        })
        assert response.status_code == 200
        body = response.json()
        assert body["accepted"] is True and body["action"] == "ensure"
        job = wait_job(server["store"], body["job_id"])
        assert job["state"] == "success"
        assert server["store"].get_env("feature/x")["state"] == "running"

    def test_updated_deploys_with_sha(self, server, tokens, ensured):
        _git(server["factory"].seed, "checkout", "feature/x")
        new_sha = server["factory"].commit(
            {"addons/mod_a/models/x.py": "z = 3"}, branch="feature/x"
        )
        response = self._push(server, {
            "ref": "refs/heads/feature/x",
            "after": new_sha,
            "repository": {"full_name": "acme/odoo-custom"},
        })
        body = response.json()
        assert body["action"] == "deploy"
        job = wait_job(server["store"], body["job_id"])
        assert job["state"] == "success"
        assert server["store"].get_env("feature/x")["last_sha"] == new_sha

    def test_deleted_destroys(self, server, tokens, ensured):
        response = self._push(server, {
            "ref": "refs/heads/feature/x",
            "after": "0" * 40,
            "deleted": True,
            "repository": {"full_name": "acme/odoo-custom"},
        })
        body = response.json()
        assert body["action"] == "destroy"
        wait_job(server["store"], body["job_id"])
        assert server["store"].get_env("feature/x") is None

    def test_protected_branch_never_destroyed(self, server, tokens):
        response = self._push(server, {
            "ref": "refs/heads/main",
            "after": "0" * 40,
            "deleted": True,
            "repository": {"full_name": "acme/odoo-custom"},
        })
        body = response.json()
        assert body["accepted"] is False and "protected" in body["reason"]

    def test_bad_signature_403(self, server, tokens):
        body = json.dumps({"ref": "refs/heads/x", "after": "a"}).encode()
        headers = {"X-Hub-Signature-256": "sha256=" + "0" * 64}
        assert server["client"].post("/api/hook", content=body, headers=headers).status_code == 403

    def test_other_repo_ignored(self, server, tokens):
        response = self._push(server, {
            "ref": "refs/heads/feature/x",
            "after": "a",
            "repository": {"full_name": "someone/else"},
        })
        assert response.json()["accepted"] is False


class TestJobsAndEvents:
    def test_jobs_listing_and_logs(self, server, tokens, ensured):
        body = server["client"].get(
            "/api/jobs?env=feature/x", headers=auth(tokens["read"])
        ).json()
        assert body["total"] >= 1
        job_id = body["items"][0]["id"]
        detail = server["client"].get(f"/api/jobs/{job_id}", headers=auth(tokens["read"])).json()
        assert detail["steps"]
        logs = server["client"].get(f"/api/jobs/{job_id}/logs", headers=auth(tokens["read"])).json()
        assert "--- step:" in logs["log"]

    def test_cancel_queued(self, server, tokens):
        job_id = server["store"].create_job("deploy", "feature/x", {})
        response = server["client"].post(
            f"/api/jobs/{job_id}/cancel", headers=auth(tokens["write"])
        )
        assert response.status_code == 202
        assert server["store"].get_job(job_id)["state"] == "cancelled"

    def test_events_sse_stream(self, server, tokens, ensured):
        with server["client"].stream(
            "GET", "/api/events?last_id=0", headers=auth(tokens["read"])
        ) as response:
            assert response.status_code == 200
            lines = []
            for line in response.iter_lines():
                lines.append(line)
                if len(lines) >= 3:
                    break
        text = "\n".join(lines)
        assert "event: job.queued" in text or "event: env." in text or "keepalive" in text


class TestSystem:
    def test_info(self, server, tokens):
        info = server["client"].get("/api/system/info", headers=auth(tokens["read"])).json()
        assert info["forge"] == "github"
        assert info["env_count"] == 0
        assert info["dry_run"] is True

    def test_openapi(self, server):
        response = server["client"].get("/api/openapi.json")
        assert response.status_code == 200
        assert "paths" in response.json()
