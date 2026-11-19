"""Branch-name safety: slugs, DB names, hostnames, git ref validation.

Everything the engine derives from a *remote-controlled* input (the forge
webhook branch name) passes through here. The rules:

- db slug:   ``[a-z0-9_]`` only — used in DB names, roles, compose projects
- host label: the same slug with ``_`` → ``-`` — used in hostnames
- git refs: validated against git's own branch-name rules before any git
  command is built (defense in depth; the kit also quotes every expansion)
"""

from __future__ import annotations

import re

_DB_SAFE = re.compile(r"[^a-z0-9_]+")
_COLLAPSE = re.compile(r"_+")
_FORBIDDEN_REF = re.compile(r"(?:^[/.\-]|[~^:?*\[\\\s]|\.lock$|\.\.|/@|/$|//)")


class BranchError(ValueError):
    """Raised for branch names that cannot be handled safely."""


def validate_branch(branch: str) -> str:
    """Validate a git branch name (subset of ``git check-ref-format`` rules)."""
    if not branch or not isinstance(branch, str):
        raise BranchError("empty branch name")
    if branch.startswith("refs/"):
        raise BranchError(f"expected a branch name, got a ref: {branch!r}")
    if _FORBIDDEN_REF.search(branch):
        raise BranchError(f"unsupported characters or pattern in branch name: {branch!r}")
    if len(branch) > 100:
        raise BranchError(f"branch name too long ({len(branch)} chars)")
    return branch


def db_slug(branch: str, max_len: int = 40) -> str:
    """Map a branch name to a safe slug used by DB/role/project names."""
    slug = _COLLAPSE.sub("_", _DB_SAFE.sub("_", branch.lower())).strip("_")
    if not slug:
        raise BranchError(f"branch {branch!r} maps to an empty slug")
    if len(slug) > max_len:
        slug = slug[:max_len].rstrip("_") or "b"
    if slug[0].isdigit():
        # DB names/roles are prefixed anyway (env_), but keep the slug itself
        # leadable by a letter so hostnames and logins stay predictable.
        slug = f"b{slug[:max_len - 1]}"
    return slug


def host_label(slug: str) -> str:
    """Hostname label for an env: ``feature_x`` → ``feature-x``."""
    return slug.replace("_", "-")


def db_name(slug: str) -> str:
    """Postgres database/role name for an env."""
    return f"env_{slug}"


def compose_project(slug: str) -> str:
    """docker compose project name for an env."""
    return f"env_{slug}"


def env_url(slug: str, domain: str) -> str:
    """Public URL of an env."""
    return f"https://{host_label(slug)}.{domain}"


def is_protected(branch: str, protect: tuple[str, ...]) -> bool:
    return branch in protect
