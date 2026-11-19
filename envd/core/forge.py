"""Forge webhook adapters: GitHub / GitLab / Gitea push events.

Fail-closed rules, in order:

1. no configured secret → nothing is accepted (F14 fixed here);
2. signature/token mismatch → :class:`ForgeError`;
3. repo not on the allowlist → ignored (``None``);
4. not a branch push (tags, PRs, releases…) → ignored;
5. branch name fails git-ref validation → ignored (defense in depth — the
   gitops layer validates again).

Branch deletion is the destroy trigger: GitHub/Gitea send ``deleted: true``;
GitLab signals it with an all-zero ``after`` sha.
"""

from __future__ import annotations

import hashlib
import hmac
import json
from dataclasses import dataclass

from envd.config import ForgeConfig
from envd.tools.slug import validate_branch

ZERO_SHA = "0" * 40


class ForgeError(ValueError):
    """Signature/token verification failed — the hook must be rejected."""


@dataclass(frozen=True)
class PushEvent:
    action: str  # created | updated | deleted
    branch: str
    repo: str
    sha: str | None = None


def verify_and_parse(forge: ForgeConfig, headers: dict[str, str], body: bytes) -> PushEvent | None:
    """Verify + parse a push webhook. Raises ForgeError on bad signature.
    Returns None when the event is not actionable (other repo, tags, noise).
    """
    if not forge.secret:
        raise ForgeError("forge secret not configured — rejecting unsigned hook")
    _verify(forge, headers, body)
    payload = json.loads(body)
    repo = _repo_name(forge.type, payload)
    if repo is None or repo not in forge.repos:
        return None
    if forge.type == "gitlab":
        return _parse_gitlab(repo, payload)
    return _parse_github_like(repo, payload)


def _header(headers: dict[str, str], name: str) -> str:
    return (
        headers.get(name)
        or headers.get(name.lower())
        or headers.get(name.replace("-", "_"))
    )


def _verify(forge: ForgeConfig, headers: dict[str, str], body: bytes) -> None:
    if forge.type in ("github", "gitea"):
        if forge.type == "github":
            provided_header = _header(headers, "X-Hub-Signature-256")
            if provided_header is None:
                raise ForgeError("github signature mismatch")
            if not provided_header.startswith("sha256"):
                raise ForgeError("github signature must be sha256")
            provided = provided_header.removeprefix("sha256=")
        else:
            provided = _header(headers, "X-Gitea-Signature")
        digest = hmac.new(forge.secret.encode(), body, hashlib.sha256).hexdigest()
        if not provided or not hmac.compare_digest(provided, digest):
            raise ForgeError(f"{forge.type} signature mismatch")
    elif forge.type == "gitlab":
        provided = _header(headers, "X-Gitlab-Token")
        if not provided or not hmac.compare_digest(provided, forge.secret):
            raise ForgeError("gitlab token mismatch")
    else:  # pragma: no cover — config validation already restricts types
        raise ForgeError(f"unsupported forge {forge.type!r}")


def _repo_name(forge_type: str, payload: dict) -> str | None:
    if forge_type == "gitlab":
        return payload.get("project", {}).get("path_with_namespace")
    return (payload.get("repository") or {}).get("full_name")


def _branch(ref: str) -> str | None:
    if not ref.startswith("refs/heads/"):
        return None  # tags, notes, …
    try:
        return validate_branch(ref.removeprefix("refs/heads/"))
    except ValueError:
        return None


def _parse_github_like(repo: str, payload: dict) -> PushEvent | None:
    if "ref" not in payload:
        return None
    branch = _branch(payload["ref"])
    if branch is None:
        return None
    sha = payload.get("after")
    if payload.get("deleted"):
        return PushEvent("deleted", branch, repo, None)
    action = "created" if payload.get("created") else "updated"
    return PushEvent(action, branch, repo, sha)


def _parse_gitlab(repo: str, payload: dict) -> PushEvent | None:
    if "ref" not in payload:
        return None
    branch = _branch(payload["ref"])
    if branch is None:
        return None
    after = payload.get("after") or ""
    before = payload.get("before") or ""
    sha = payload.get("checkout_sha") or (after or None)
    if after and set(after) == {"0"}:
        return PushEvent("deleted", branch, repo, None)
    action = "created" if (before and set(before) == {"0"}) else "updated"
    return PushEvent(action, branch, repo, sha)
