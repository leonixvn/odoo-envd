import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from envd.compose import ComposeError
from envd.sandbox import SandboxError, Sandboxes


class SandboxTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.manager = Sandboxes(self.root)
        ensure_proxy = patch("envd.proxy.ensure")
        ensure_proxy.start()
        self.addCleanup(ensure_proxy.stop)
        compose = patch("envd.sandbox.Compose")
        self.compose = compose.start()
        self.addCleanup(compose.stop)
        self.compose.return_value.run.return_value = ""

    def test_project_configuration_is_isolated(self):
        first = self.manager.create("first", 8069)
        second = self.manager.create("second")
        self.assertNotEqual(first["project"], second["project"])
        self.assertNotEqual(first["hostname"], second["hostname"])
        self.assertRegex(first["hostname"], r"^[0-9a-f]{16}\.greeni\.leonix\.cc$")
        self.manager.stop("first")
        self.assertEqual(self.manager.start("first")["hostname"], first["hostname"])
        self.assertEqual(self.manager.names(), ["first", "second"])
        config = json.loads((self.root / "first/compose.json").read_text())
        self.assertEqual(config["services"]["odoo"]["ports"], ["127.0.0.1:8069:8069"])
        labels = config["services"]["odoo"]["labels"]
        self.assertEqual(labels["traefik.docker.network"], "coolify")
        self.assertEqual(
            labels[f"traefik.http.routers.{first['project']}.rule"], f"Host(`{first['hostname']}`)",
        )
        self.assertNotIn("ports", config["services"]["postgres"])
        self.assertEqual(config["services"]["postgres"]["networks"], ["database"])
        self.assertTrue(config["networks"]["database"]["internal"])
        self.assertEqual(set(config["volumes"]), {"postgres", "odoo"})
        self.assertEqual((self.root / "first/sandbox.json").stat().st_mode & 0o777, 0o600)

    def test_stop_and_resume_skip_database_initialization(self):
        self.manager.create("trial")
        run = self.compose.return_value.run
        run.reset_mock()
        self.manager.stop("trial")
        run.assert_called_once_with("stop")
        run.reset_mock()
        self.manager.start("trial")
        self.assertFalse(any(call.args[0] == "run" for call in run.call_args_list))

    def test_failed_first_initialization_is_retryable(self):
        run = self.compose.return_value.run

        def fail_init(*args, **kwargs):
            if args[0] == "run":
                raise ComposeError("initialization interrupted")
            return ""

        run.side_effect = fail_init
        with self.assertRaisesRegex(SandboxError, "Sandbox saved"):
            self.manager.create("trial")
        self.assertFalse((self.root / "trial/initialized").exists())
        run.side_effect = None
        self.assertTrue(self.manager.start("trial")["initialized"])

    def test_failed_delete_retains_record_and_success_removes_only_target(self):
        first = self.manager.create("first")
        self.manager.create("second")
        run = self.compose.return_value.run
        run.side_effect = ComposeError("Docker unavailable")
        with self.assertRaises(ComposeError):
            self.manager.delete("first")
        self.assertEqual(self.manager.names(), ["first", "second"])
        run.side_effect = None
        self.manager.delete("first")
        self.compose.assert_called_with(self.root / "first", first["project"])
        run.assert_called_with("down", "--volumes", "--remove-orphans")
        self.assertEqual(self.manager.names(), ["second"])

    def test_status_reports_published_port(self):
        self.manager.create("trial")
        self.compose.return_value.run.return_value = json.dumps({
            "Service": "odoo", "State": "running", "Health": "healthy",
            "Publishers": [{"TargetPort": 8069, "PublishedPort": 49153}],
        })
        self.assertEqual(self.manager.status("trial")["local_url"], "http://127.0.0.1:49153/web/login")


if __name__ == "__main__":
    unittest.main()
