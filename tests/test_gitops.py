"""Git operations against a real throwaway origin + mirror + worktree."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from envd.config import Config, load_config
from envd.core.gitops import (
    GitError,
    GitOps,
    changed_modules_from_lines,
    parse_apps_txt,
)
from envd.core.shell import Shell

GIT_ENV = {
    "GIT_AUTHOR_NAME": "t",
    "GIT_AUTHOR_EMAIL": "t@example.com",
    "GIT_COMMITTER_NAME": "t",
    "GIT_COMMITTER_EMAIL": "t@example.com",
}


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(cwd), *args],
        check=True,
        capture_output=True,
        text=True,
        env={**GIT_ENV, "PATH": "/usr/bin:/bin", "HOME": str(cwd)},
    ).stdout


class RepoFactory:
    """origin.git (bare) ← seed workdir; mirror = clone --mirror of origin."""

    def __init__(self, tmp_path: Path) -> None:
        self.origin = tmp_path / "origin.git"
        subprocess.run(["git", "init", "--bare", "-b", "main", str(self.origin)],
                       check=True, capture_output=True)
        self.seed = tmp_path / "seed"
        self.seed.mkdir()
        _git(self.seed, "init", "-b", "main")
        _git(self.seed, "remote", "add", "origin", str(self.origin))

    def commit(self, files: dict[str, str], branch: str = "main") -> str:
        for rel, content in files.items():
            target = self.seed / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")
        _git(self.seed, "add", "-A")
        _git(self.seed, "commit", "-m", f"add {len(files)} files")
        _git(self.seed, "push", "origin", branch)
        return _git(self.seed, "rev-parse", "HEAD").strip()

    def clone_mirror(self, target: Path) -> None:
        subprocess.run(["git", "clone", "--mirror", str(self.origin), str(target)],
                       check=True, capture_output=True)


@pytest.fixture()
def repo(tmp_path: Path):
    factory = RepoFactory(tmp_path)
    first = factory.commit(
        {
            "addons/mod_a/__manifest__.py": "{'name': 'A'}",
            "addons/mod_b/__manifest__.py": "{'name': 'B'}",
            "third_party/oca_dep/__manifest__.py": "{'name': 'dep'}",
            "apps.txt": "crm\n# comment\nsale_management\n",
            "requirements.txt": "requests==2.32.0\n",
        }
    )
    _git(factory.seed, "checkout", "-b", "feature/x")
    second = factory.commit(
        {
            "addons/mod_a/models/thing.py": "x = 1",
            "third_party/oca_new/__manifest__.py": "{'name': 'new'}",
            "requirements.txt": "requests==2.32.1\n",
        },
        branch="feature/x",
    )
    mirror = tmp_path / "repo.git"
    factory.clone_mirror(mirror)
    return {"factory": factory, "main": first, "feature": second, "mirror": mirror}


@pytest.fixture()
def cfg(tmp_path: Path, repo) -> Config:
    toml = tmp_path / "envd.toml"
    body = (
        "[server]\ndomain = 'staging.example.com'\n\n"
        f"[paths]\nroot = '{tmp_path / 'srv'}'\n"
        f"mirror = '{repo['mirror']}'\n"
    )
    toml.write_text(body, encoding="utf-8")
    (tmp_path / "srv").mkdir(exist_ok=True)
    return load_config(toml)


@pytest.fixture()
def ops(cfg: Config) -> GitOps:
    return GitOps(cfg, Shell())


class TestPureParsers:
    def test_changed_modules(self):
        lines = [
            "addons/mod_a/models/x.py",
            "addons/mod_b/__manifest__.py",
            "third_party/oca_dep/static/icon.svg",
            "deploy/bootstrap.sh",
            "README.md",
            "addons/stray_no_slash",
        ]
        modules, third = changed_modules_from_lines(lines)
        assert modules == ["mod_a", "mod_b", "oca_dep"]
        assert third is True

    def test_changed_modules_only_custom(self):
        modules, third = changed_modules_from_lines(["addons/mod_a/x.py"])
        assert modules == ["mod_a"]
        assert third is False

    def test_invalid_names_ignored(self):
        modules, _ = changed_modules_from_lines(["addons/UPPER/x.py", "addons/.hidden/x"])
        assert modules == []

    def test_apps_txt(self):
        assert parse_apps_txt("crm\n# comment\n\nsale_management\n") == ["crm", "sale_management"]

    def test_apps_txt_rejects_invalid(self):
        with pytest.raises(GitError, match="invalid module name"):
            parse_apps_txt("../evil\n")


class TestRealGit:
    def test_fetch_and_sha(self, repo, ops):
        ops.fetch()
        assert ops.sha_of("main") == repo["main"]
        assert ops.sha_of("feature/x") == repo["feature"]

    def test_sha_of_missing_branch(self, repo, ops):
        ops.fetch()
        with pytest.raises(GitError, match="not found in mirror"):
            ops.sha_of("nope")

    def test_fetch_without_mirror_raises(self, tmp_path: Path):
        # a dedicated config whose mirror is an empty directory (not a repo)
        empty = tmp_path / "empty.git"
        empty.mkdir()
        toml = tmp_path / "bare.toml"
        toml.write_text(
            f"[paths]\nroot = '{tmp_path / 'srv2'}'\nmirror = '{empty}'\n",
            encoding="utf-8",
        )
        cfg = load_config(toml)
        ops = GitOps(cfg, Shell())
        with pytest.raises(GitError, match="not a git repository"):
            ops.fetch()

    def test_worktree_lifecycle(self, repo, ops):
        ops.fetch()
        assert not ops.worktree_exists("feature_x")
        ops.worktree_add("feature_x", "feature/x")
        assert ops.worktree_exists("feature_x")
        path = ops.worktree_path("feature_x")
        assert (path / "apps.txt").is_file()
        # deploy primitive: reset to a sha
        ops.worktree_reset("feature_x", repo["main"])
        assert (path / "addons" / "mod_a" / "models").exists() is False
        ops.worktree_reset("feature_x", repo["feature"])
        assert (path / "addons" / "mod_a" / "models" / "thing.py").is_file()
        ops.worktree_remove("feature_x")
        assert not path.exists()

    def test_worktree_add_is_idempotent(self, repo, ops):
        ops.fetch()
        ops.worktree_add("feature_x", "feature/x")
        result = ops.worktree_add("feature_x", "feature/x")
        assert result.rc == 0 and result.dry_run is False

    def test_changed_modules_between_shas(self, repo, ops):
        ops.fetch()
        ops.worktree_add("feature_x", "feature/x")
        modules, third = ops.changed_modules("feature_x", repo["main"], repo["feature"])
        assert modules == ["mod_a", "oca_new"]
        assert third is True

    def test_changed_modules_same_sha(self, repo, ops):
        ops.fetch()
        ops.worktree_add("feature_x", "feature/x")
        modules, third = ops.changed_modules("feature_x", repo["feature"], repo["feature"])
        assert modules == [] and third is False

    def test_apps_and_requirements(self, repo, ops):
        ops.fetch()
        ops.worktree_add("feature_x", "feature/x")
        assert ops.apps("feature_x") == ["crm", "sale_management"]
        assert ops.requirements_hash("feature_x") != ""

    def test_escalation_rule(self, ops):
        assert ops.escalate_to_full_upgrade([], False, 100) is False
        assert ops.escalate_to_full_upgrade(["a"], True, 100) is True
        # threshold is max(3, installed // 3)
        assert ops.escalate_to_full_upgrade(["a", "b", "c", "d"], False, 5) is True
        assert ops.escalate_to_full_upgrade(["a", "b", "c"], False, 5) is False
        assert ops.escalate_to_full_upgrade(["a", "b", "c"], False, 100) is False
