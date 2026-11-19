"""Shared fixtures: tmp paths, a minimal envd.toml, and a fresh Store."""

from __future__ import annotations

from pathlib import Path

import pytest
from envd.config import Config, load_config
from envd.core.state import Store

MINIMAL_TOML = """
[server]
domain = "staging.example.com"

[paths]
root = "{root}"

[forge]
type = "github"
secret = "hook-secret"
repos = ["acme/odoo-custom"]

[postgres]
admin_user = "postgres"
admin_password = "admin-pw"

[docker]
base_image = "odoo:20.0-20261112"
"""


@pytest.fixture()
def config(tmp_path: Path) -> Config:
    toml = tmp_path / "envd.toml"
    toml.write_text(MINIMAL_TOML.format(root=tmp_path / "srv"), encoding="utf-8")
    (tmp_path / "srv").mkdir(exist_ok=True)
    return load_config(toml)


@pytest.fixture()
def store(tmp_path: Path) -> Store:
    s = Store(tmp_path / "state.db")
    yield s
    s.close()
