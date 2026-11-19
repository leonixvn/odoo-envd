"""Rendering of per-env artifacts and the docker entrypoint contract."""

from __future__ import annotations

import re

from envd.core.render import FORBIDDEN_CONF_KEYS, render_env


def render(cfg, **kwargs):
    base = dict(
        slug="feature_x",
        db_name="env_feature_x",
        pg_user="env_feature_x",
        pg_password="pg-pw-123",
        image_tag="odoo-custom-base:20",
    )
    base.update(kwargs)
    return render_env(cfg, **base)


class TestRender:
    def test_files_written(self, config):
        paths = render(config)
        assert paths["compose"].is_file()
        assert paths["conf"].is_file()
        assert paths["env"].is_file()
        assert paths["project_dir"] == config.paths.envs / "feature_x" / "project"

    def test_conf_has_no_db_connection_keys(self, config):
        paths = render(config)
        text = paths["conf"].read_text()
        assert not re.search(r"^db_(host|port|user|password)", text, re.MULTILINE)
        # invariant checker agrees on both sides
        assert FORBIDDEN_CONF_KEYS.search("db_host = localhost")
        assert not FORBIDDEN_CONF_KEYS.search(text)

    def test_conf_content(self, config):
        conf = render(config)["conf"].read_text()
        assert "dbfilter = ^env_feature_x$" in conf
        assert "list_db = False" in conf
        assert "admin_passwd =" in conf
        assert "workers = 0" in conf
        assert "max_cron_threads = 0" in conf
        # canonical-first addons priority (enterprise → third_party → custom)
        assert conf.index("/mnt/enterprise") < conf.index("/mnt/third-party")
        assert conf.index("/mnt/third-party") < conf.index("/mnt/extra-addons")

    def test_compose_content(self, config):
        compose = render(config)["compose"].read_text()
        assert "image: odoo-custom-base:20" in compose
        assert "Host(`feature-x.staging.example.com`)" in compose
        assert "max-size: 10m" in compose
        assert "/mnt/extra-addons:ro" in compose
        assert f"{config.paths.enterprise}:/mnt/enterprise:ro" in compose
        assert "OPTIONAL_PORTS" not in compose
        assert "ports:" not in compose  # no direct port by default

    def test_compose_direct_port(self, config):
        compose = render(config, direct_port=8145)["compose"].read_text()
        assert "ports:" in compose
        assert '- "8145:8069"' in compose

    def test_dotenv_content(self, config):
        env = render(config)["env"].read_text()
        assert "HOST=postgres" in env
        assert "PORT=5432" in env
        assert "USER=env_feature_x" in env
        assert "PASSWORD=pg-pw-123" in env
        assert "IMAGE_TAG=odoo-custom-base:20" in env

    def test_backups_dir_created(self, config):
        render(config)
        assert (config.paths.backups / "feature_x").is_dir()

    def test_workers_override(self, config):
        conf = render(config, workers=2, max_cron_threads=1)["conf"].read_text()
        assert "workers = 2" in conf
        assert "max_cron_threads = 1" in conf
