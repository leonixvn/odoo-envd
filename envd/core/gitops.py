"""Git operations: the shared mirror, per-env worktrees, changed-module computation.

Layout (all derived from the engine config):

- mirror:  ``<root>/repo.git`` — bare clone of the modules repo; the only place
  that talks to the forge remote; fetches are serialized with ``flock``.
- worktree: ``<envs>/<slug>/repo`` — detached checkout of ``origin/<branch>``;
  a disposable cache: every deploy ``reset --hard``s it, nobody edits it.

The changed-module computation is the heart of the fast deploy path: it turns a
git diff into the module-name list passed to ``odoo module upgrade``. The pure
parser is separate so it can be unit-tested without git.
"""

from __future__ import annotations

import hashlib
import re
from pathlib import Path

from envd.config import Config
from envd.core.shell import RunResult, Shell

MODULE_NAME = re.compile(r"^[a-z0-9_]+$")


class GitError(RuntimeError):
    pass


def changed_modules_from_lines(lines: list[str]) -> tuple[list[str], bool]:
    """Extract changed top-level module names from ``git diff --name-only`` output.

    Repo layout convention: ``addons/<module>/…`` and ``third_party/<module>/…``
    (one directory per module; third-party repos are pinned as per-module
    submodules).

    Returns ``(sorted module names, touched_third_party)`` — the flag drives the
    deploy escalation rule (third-party churn → full upgrade).
    """
    modules: set[str] = set()
    touched_third_party = False
    for line in lines:
        parts = line.strip().split("/")
        # a module shows up as a file INSIDE its directory (≥3 path parts);
        # "addons/foo" (2 parts) is a stray file directly under addons/, not a module
        if len(parts) >= 3 and parts[0] in ("addons", "third_party"):
            name = parts[1]
            if name and MODULE_NAME.match(name):
                modules.add(name)
                if parts[0] == "third_party":
                    touched_third_party = True
    return sorted(modules), touched_third_party


def parse_apps_txt(text: str) -> list[str]:
    """apps.txt: one app per line, ``#`` comments allowed, validated names."""
    apps: list[str] = []
    for raw in text.splitlines():
        name = raw.strip()
        if not name or name.startswith("#"):
            continue
        if not MODULE_NAME.match(name):
            raise GitError(f"invalid module name in apps.txt: {name!r}")
        apps.append(name)
    return apps


class GitOps:
    def __init__(self, cfg: Config, shell: Shell) -> None:
        self.cfg = cfg
        self.shell = shell
        self.mirror = cfg.paths.mirror

    def _require_mirror(self) -> None:
        if not (self.mirror / "config").is_file():
            raise GitError(
                f"mirror {self.mirror} is not a git repository — bootstrap or clone it"
            )

    def fetch(self) -> RunResult:
        """Fetch+prune the forge remote, serialized across all envs."""
        self._require_mirror()
        lock = self.mirror.parent / ".mirror-fetch.lock"
        return self.shell.run(
            ["flock", str(lock), "git", "-C", str(self.mirror),
             "fetch", "--prune", "origin"],
            timeout=600,
        )

    def sha_of(self, branch: str) -> str:
        """Current sha of a branch in the mirror (after fetch).

        Mirror clones hold branches directly as ``refs/heads/<name>`` (there
        are no ``origin/*`` remote-tracking refs in a --mirror clone).
        """
        self._require_mirror()
        result = self.shell.run(
            ["git", "-C", str(self.mirror), "rev-parse", f"refs/heads/{branch}"],
            check=False,
        )
        out = result.output.strip()
        if result.rc != 0 or not out:
            raise GitError(f"branch refs/heads/{branch} not found in mirror")
        return out

    def worktree_path(self, slug: str) -> Path:
        return self.cfg.paths.envs / slug / "repo"

    def worktree_exists(self, slug: str) -> bool:
        return (self.worktree_path(slug) / ".git").is_file()

    def worktree_add(self, slug: str, branch: str) -> RunResult:
        """Create the detached worktree for an env (idempotent)."""
        path = self.worktree_path(slug)
        if self.worktree_exists(slug):
            return RunResult(rc=0, duration=0.0)
        return self.shell.run(
            ["git", "-C", str(self.mirror), "worktree", "add", "--detach",
             str(path), f"refs/heads/{branch}"],
            timeout=120,
        )

    def worktree_reset(self, slug: str, sha: str) -> RunResult:
        """Point the env's worktree at exactly ``sha`` — the deploy primitive."""
        return self.shell.run(
            ["git", "-C", str(self.worktree_path(slug)), "reset", "--hard", sha],
            timeout=120,
        )

    def worktree_remove(self, slug: str) -> RunResult:
        path = self.worktree_path(slug)
        return self.shell.run(
            ["git", "-C", str(self.mirror), "worktree", "remove", "--force", str(path)],
            timeout=120,
        )

    def changed_modules(self, slug: str, old_sha: str, new_sha: str) -> tuple[list[str], bool]:
        """Module names changed between two shas + whether third_party was touched."""
        out = self.shell.capture(
            ["git", "-C", str(self.worktree_path(slug)), "diff", "--name-only",
             old_sha, new_sha, "--", "addons", "third_party"]
        )
        return changed_modules_from_lines(out.splitlines() if out else [])

    def apps(self, slug: str) -> list[str]:
        """The branch's install list; empty means 'install base only'."""
        apps_file = self.worktree_path(slug) / "apps.txt"
        if not apps_file.is_file():
            return []
        return parse_apps_txt(apps_file.read_text(encoding="utf-8"))

    def requirements_hash(self, slug: str) -> str:
        """Content hash of the branch's requirements.txt ('' when absent)."""
        req = self.worktree_path(slug) / "requirements.txt"
        if not req.is_file():
            return ""
        return hashlib.sha256(req.read_bytes()).hexdigest()

    def escalate_to_full_upgrade(
        self, changed: list[str], touched_third_party: bool, installed: int
    ) -> bool:
        """Conservative escalation: third-party churn or more than a third of the
        installed module set changed → ``module upgrade all`` instead of a list."""
        return touched_third_party or len(changed) > max(3, installed // 3)
