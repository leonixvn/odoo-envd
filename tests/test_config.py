"""envd.toml loading: defaults, path resolution, validation errors."""

from __future__ import annotations

from pathlib import Path

import pytest
from envd.config import ConfigError, load_config

from conftest import MINIMAL_TOML


def test_minimal_config_defaults(config):
    assert config.server.domain == "staging.example.com"
    assert config.server.listen_host == "127.0.0.1"
    assert config.server.listen_port == 8100
    assert config.forge.type == "github"
    assert config.forge.repos == ("acme/odoo-custom",)
    assert config.docker.base_image == "odoo:20.0-20261112"
    assert config.policy.upgrade_strategy == "diff"
    assert config.policy.on_deploy_failure == "rollback"
    assert config.policy.protect == ("main", "master")
    assert config.policy.port_range == (8100, 8199)
    assert config.policy.retention.pre_deploy == 5


def test_paths_derive_from_root(config, tmp_path: Path):
    root = tmp_path / "srv"
    assert config.paths.root == root
    assert config.paths.mirror == root / "repo.git"
    assert config.paths.envs == root / "envs"
    assert config.paths.data == root / "data"
    assert config.paths.backups == root / "backups"
    assert config.paths.enterprise == root / "enterprise"
    assert config.paths.state_db == root / "envd" / "state.db"
    assert config.paths.logs == root / "envd" / "logs"


def test_relative_root_rejected(tmp_path: Path):
    toml = tmp_path / "envd.toml"
    toml.write_text('[paths]\nroot = "srv/relative"\n', encoding="utf-8")
    with pytest.raises(ConfigError, match="absolute"):
        load_config(toml)


def test_missing_root_rejected(tmp_path: Path):
    toml = tmp_path / "envd.toml"
    toml.write_text("[server]\ndomain = 'x.com'\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="root is required"):
        load_config(toml)


@pytest.mark.parametrize(
    "extra",
    [
        '[forge]\ntype = "bitbucket"\n',
        "[policy]\nprotect = []\n",
        "[policy]\nport_range = [9000, 8000]\n",
        "[policy]\nupgrade_strategy = 'wild'\n",
        "[policy]\non_deploy_failure = 'explode'\n",
        "[policy]\nprune_ttl_days = 0\n",
        "[policy]\nretention = { pre_deploy = 0 }\n",
    ],
)
def test_invalid_values_rejected(tmp_path: Path, extra: str):
    toml = tmp_path / "envd.toml"
    toml.write_text(MINIMAL_TOML.format(root=tmp_path / "srv") + "\n" + extra, encoding="utf-8")
    (tmp_path / "srv").mkdir(exist_ok=True)
    with pytest.raises(ConfigError):
        load_config(toml)


def test_overridden_path_is_honoured(tmp_path: Path):
    toml = tmp_path / "envd.toml"
    body = MINIMAL_TOML.format(root=tmp_path / "srv")
    body = body.replace("[paths]\n", "")  # drop the default section before overriding
    body += (
        f"\n[paths]\nroot = '{tmp_path / 'srv'}'\n"
        f"mirror = '{tmp_path / 'alt' / 'repo.git'}'\n"
    )
    toml.write_text(body, encoding="utf-8")
    (tmp_path / "srv").mkdir(exist_ok=True)
    cfg = load_config(toml)
    assert cfg.paths.mirror == tmp_path / "alt" / "repo.git"
    assert cfg.paths.envs == tmp_path / "srv" / "envs"


def test_broken_toml_rejected(tmp_path: Path):
    toml = tmp_path / "envd.toml"
    toml.write_text("[paths\nroot = '''", encoding="utf-8")
    with pytest.raises(ConfigError, match="invalid TOML"):
        load_config(toml)


def test_missing_file_rejected(tmp_path: Path):
    with pytest.raises(ConfigError, match="not found"):
        load_config(tmp_path / "nope.toml")
