"""Forge webhook adapters: signatures (fail closed), repo allowlist, push parsing."""

from __future__ import annotations

import hashlib
import hmac
import json

import pytest
from envd.config import ForgeConfig
from envd.core.forge import ForgeError, verify_and_parse

SECRET = "hook-secret"


def github_body(**extra) -> bytes:
    payload = {
        "ref": "refs/heads/feature/x",
        "after": "abc123",
        "repository": {"full_name": "acme/odoo-custom"},
    }
    payload.update(extra)
    return json.dumps(payload).encode()


def sign(body: bytes) -> str:
    return "sha256=" + hmac.new(SECRET.encode(), body, hashlib.sha256).hexdigest()


def forge(kind: str, secret: str = SECRET, repos: tuple = ("acme/odoo-custom",)):
    return ForgeConfig(type=kind, secret=secret, repos=repos)


class TestGitHub:
    def test_push_updated(self):
        body = github_body()
        event = verify_and_parse(forge("github"), {"X-Hub-Signature-256": sign(body)}, body)
        assert event is not None
        assert event.action == "updated"
        assert event.branch == "feature/x"
        assert event.repo == "acme/odoo-custom"

    def test_created_and_deleted(self):
        body = github_body(created=True)
        event = verify_and_parse(forge("github"), {"X-Hub-Signature-256": sign(body)}, body)
        assert event.action == "created"

        body = github_body(deleted=True, after="0000000000000000000000000000000000000000")
        event = verify_and_parse(forge("github"), {"X-Hub-Signature-256": sign(body)}, body)
        assert event.action == "deleted"
        assert event.sha is None

    def test_bad_signature_rejected(self):
        body = github_body()
        with pytest.raises(ForgeError, match="signature mismatch"):
            verify_and_parse(forge("github"), {"X-Hub-Signature-256": sign(b"tampered")}, body)

    def test_missing_signature_rejected(self):
        with pytest.raises(ForgeError, match="signature mismatch"):
            verify_and_parse(forge("github"), {}, github_body())

    def test_sha1_kind_rejected(self):
        body = github_body()
        with pytest.raises(ForgeError, match="sha256"):
            verify_and_parse(
                forge("github"), {"X-Hub-Signature-256": "sha1=deadbeef"}, body
            )


class TestGitea:
    def test_push_and_delete(self):
        body = github_body()
        headers = {"X-Gitea-Signature": hmac.new(SECRET.encode(), body, hashlib.sha256).hexdigest()}
        event = verify_and_parse(forge("gitea"), headers, body)
        assert event.action == "updated"

        body = github_body(deleted=True)
        headers = {"X-Gitea-Signature": hmac.new(SECRET.encode(), body, hashlib.sha256).hexdigest()}
        event = verify_and_parse(forge("gitea"), headers, body)
        assert event.action == "deleted"


class TestGitLab:
    def _body(self, **extra) -> bytes:
        payload = {
            "ref": "refs/heads/feature/x",
            "before": "1111111111111111111111111111111111111111",
            "after": "2222222222222222222222222222222222222222",
            "checkout_sha": "2222222222222222222222222222222222222222",
            "project": {"path_with_namespace": "acme/odoo-custom"},
        }
        payload.update(extra)
        return json.dumps(payload).encode()

    def test_updated(self):
        event = verify_and_parse(forge("gitlab"), {"X-Gitlab-Token": SECRET}, self._body())
        assert event.action == "updated"
        assert event.branch == "feature/x"

    def test_created_via_zero_before(self):
        body = self._body(before="0" * 40)
        event = verify_and_parse(forge("gitlab"), {"X-Gitlab-Token": SECRET}, body)
        assert event.action == "created"

    def test_deleted_via_zero_after(self):
        body = self._body(after="0" * 40, checkout_sha=None)
        event = verify_and_parse(forge("gitlab"), {"X-Gitlab-Token": SECRET}, body)
        assert event.action == "deleted"

    def test_bad_token_rejected(self):
        with pytest.raises(ForgeError, match="token mismatch"):
            verify_and_parse(forge("gitlab"), {"X-Gitlab-Token": "nope"}, self._body())


class TestFailClosed:
    def test_empty_secret_rejects_everything(self):
        with pytest.raises(ForgeError, match="not configured"):
            verify_and_parse(forge("github", secret=""), {}, github_body())

    def test_other_repo_ignored(self):
        body = github_body()
        event = verify_and_parse(
            forge("github", repos=("other/repo",)),
            {"X-Hub-Signature-256": sign(body)},
            body,
        )
        assert event is None

    def test_tag_push_ignored(self):
        body = github_body(ref="refs/tags/v1.0")
        event = verify_and_parse(forge("github"), {"X-Hub-Signature-256": sign(body)}, body)
        assert event is None

    def test_weird_branch_ignored(self):
        body = github_body(ref="refs/heads/-weird")
        event = verify_and_parse(forge("github"), {"X-Hub-Signature-256": sign(body)}, body)
        assert event is None

    def test_non_push_payload_ignored(self):
        body = json.dumps({"zen": "Design for failure"}).encode()
        event = verify_and_parse(forge("github"), {"X-Hub-Signature-256": sign(body)}, body)
        assert event is None
