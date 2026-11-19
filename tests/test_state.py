"""SQLite store: envs, jobs, backups, events, tokens, scope hierarchy."""

from __future__ import annotations

import sqlite3

import pytest
from envd.core.state import (
    JOB_STATES,
    Store,
    StoreError,
    hash_token,
    scope_satisfied,
    utcnow,
)


def make_env(store: Store, branch: str = "feature/x", slug: str = "feature_x") -> dict:
    return store.create_env(
        branch=branch,
        slug=slug,
        db=f"env_{slug}",
        url=f"https://{slug.replace('_', '-')}.staging.example.com",
        admin_password="pw-123",
    )


class TestEnvs:
    def test_create_and_fetch(self, store):
        env = make_env(store)
        assert env["branch"] == "feature/x"
        assert env["db_name"] == "env_feature_x"
        assert env["state"] == "creating"
        assert env["pinned"] is False
        assert env["created_at"] == env["updated_at"]
        assert store.get_env("feature/x")["slug"] == "feature_x"

    def test_missing_env_is_none(self, store):
        assert store.get_env("nope") is None

    def test_slug_and_branch_uniqueness(self, store):
        make_env(store)
        with pytest.raises(sqlite3.IntegrityError):
            make_env(store, branch="feature/y")  # same slug, different branch
        with pytest.raises(sqlite3.IntegrityError):
            make_env(store, branch="feature/x")  # same branch

    def test_update_and_state_validation(self, store):
        make_env(store)
        env = store.update_env("feature/x", state="running", last_sha="abc", deploy_count=1)
        assert env["state"] == "running"
        assert env["last_sha"] == "abc"
        assert env["deploy_count"] == 1
        with pytest.raises(StoreError, match="invalid env state"):
            store.update_env("feature/x", state="exploded")
        with pytest.raises(StoreError, match="cannot update env fields"):
            store.update_env("feature/x", slug="other")

    def test_list_filters(self, store):
        make_env(store, branch="feature/a", slug="feature_a")
        make_env(store, branch="feature/b", slug="feature_b")
        store.update_env("feature/a", state="running")
        store.update_env("feature/b", state="running", pinned=1)
        assert len(store.list_envs()) == 2
        assert len(store.list_envs(state="running")) == 2
        assert len(store.list_envs(pinned=True)) == 1
        assert {e["branch"] for e in store.list_envs(pinned=True)} == {"feature/b"}

    def test_delete_and_used_ports(self, store):
        make_env(store)
        assert store.used_ports() == set()
        store.update_env("feature/x", direct_port=8123)
        assert store.used_ports() == {8123}
        store.delete_env("feature/x")
        assert store.get_env("feature/x") is None
        assert store.used_ports() == set()


class TestJobs:
    def test_lifecycle(self, store):
        jid = store.create_job("deploy", "feature/x", {"sha": "abc"})
        job = store.get_job(jid)
        assert job["state"] == "queued"
        assert job["params"] == {"sha": "abc"}
        assert job["steps"] == []
        store.update_job(jid, state="running", started_at=utcnow())
        store.update_job(jid, state="success", finished_at=utcnow(), steps=[{"name": "upgrade"}])
        job = store.get_job(jid)
        assert job["state"] == "success"
        assert job["steps"][0]["name"] == "upgrade"

    def test_state_validation(self, store):
        jid = store.create_job("deploy", None, {})
        with pytest.raises(StoreError):
            store.update_job(jid, state="dancing")

    def test_list_filters_and_order(self, store):
        a = store.create_job("deploy", "feature/a", {})
        b = store.create_job("deploy", "feature/a", {})
        c = store.create_job("backup", "feature/b", {})
        store.update_job(a, state="success")
        listed = store.list_jobs(env_branch="feature/a")
        assert [j["id"] for j in listed] == [b, a]  # newest first
        assert store.list_jobs(state="queued")[0]["id"] == c

    def test_supersede_queued(self, store):
        old = store.create_job("deploy", "feature/a", {})
        mid = store.create_job("deploy", "feature/a", {})
        store.create_job("deploy", "feature/a", {})
        store.create_job("backup", "feature/a", {})
        store.create_job("deploy", "feature/b", {})
        # newer deploy supersedes older queued deploys of the same env only
        affected = store.supersede_queued(("deploy", "ensure"), "feature/a", before_id=mid + 1)
        assert affected == 2
        assert store.get_job(old)["state"] == "superseded"
        assert store.get_job(mid)["state"] == "superseded"
        assert store.list_jobs(env_branch="feature/a", state="queued")[0]["kind"] == "backup"
        assert store.list_jobs(env_branch="feature/b", state="queued")


class TestBackups:
    def test_add_list_prune(self, store):
        make_env(store)
        for i in range(5):
            store.add_backup(
                env_branch="feature/x", kind="pre-deploy", path=f"/b/{i}.zip", sha=f"s{i}"
            )
        store.add_backup(env_branch="feature/x", kind="daily", path="/b/daily.zip")
        assert len(store.list_backups("feature/x")) == 6
        assert len(store.list_backups("feature/x", kind="daily")) == 1
        removed = store.prune_backups("feature/x", "pre-deploy", keep=3)
        assert len(removed) == 2
        assert {path for _, path in removed} == {"/b/0.zip", "/b/1.zip"}
        remaining = store.list_backups("feature/x", kind="pre-deploy")
        assert len(remaining) == 3
        assert remaining[0]["sha"] == "s4"  # newest kept
        assert store.get_backup(remaining[0]["id"])["path"] == "/b/4.zip"
        assert store.delete_backup(remaining[0]["id"]) is True

    def test_kind_validation(self, store):
        with pytest.raises(StoreError):
            store.add_backup(env_branch="x", kind="hourly", path="/x.zip")


class TestEvents:
    def test_stream_order(self, store):
        first = store.add_event("env.created", {"branch": "main"})
        store.add_event("job.finished", {"job": 1})
        events = store.events_after(first - 1)
        assert [e["id"] for e in events] == [first, first + 1]
        tail = store.events_after(first)
        assert [e["kind"] for e in tail] == ["job.finished"]


class TestTokens:
    def test_create_and_verify(self, store):
        _, token = store.create_token("gui", ("write",))
        assert token.startswith("envd_")
        record = store.verify_token(token)
        assert record is not None
        assert record["name"] == "gui"
        assert record["scopes"] == ("write",)
        assert record["last_used_at"] is not None
        assert store.verify_token("envd_bogus") is None

    def test_hash_is_sha256_of_token(self, store):
        _, token = store.create_token("t", ("read",))
        row = store._cx.execute("SELECT token_hash FROM tokens WHERE id = 1").fetchone()
        assert row["token_hash"] == hash_token(token)

    def test_scope_rules(self):
        assert scope_satisfied(("admin",), "read")
        assert scope_satisfied(("admin",), "write")
        assert scope_satisfied(("write",), "read")
        assert not scope_satisfied(("read",), "write")
        assert not scope_satisfied(("read",), "admin")
        with pytest.raises(StoreError):
            scope_satisfied(("read",), "ninja")

    def test_invalid_scopes_rejected(self, store):
        with pytest.raises(StoreError):
            store.create_token("bad", ("ninja",))
        with pytest.raises(StoreError):
            store.create_token("empty", ())

    def test_list_and_delete(self, store):
        token_id, _ = store.create_token("t1", ("read",))
        store.create_token("t2", ("admin",))
        names = [t["name"] for t in store.list_tokens()]
        assert names == ["t1", "t2"]
        assert store.delete_token(token_id) is True
        assert store.delete_token(token_id) is False

    def test_job_states_constant(self):
        assert "queued" in JOB_STATES
