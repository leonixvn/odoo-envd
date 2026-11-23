import errno
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from envd import proxy
from envd.compose import ComposeError


class StandaloneProxyTests(unittest.TestCase):
    def test_existing_listener_prevents_proxy_start(self):
        with tempfile.TemporaryDirectory() as directory, patch(
            "envd.proxy.Compose"
        ) as compose, patch("envd.proxy.socket.socket") as socket, patch(
            "envd.proxy.subprocess.run"
        ) as docker:
            run = compose.return_value.run
            run.return_value = ""
            docker.return_value.returncode = 0
            docker.return_value.stdout = ""
            listener = socket.return_value.__enter__.return_value
            listener.bind.side_effect = OSError(errno.EADDRINUSE, "in use")
            with self.assertRaisesRegex(ComposeError, "TCP port 80 is already in use"):
                proxy.ensure(Path(directory))
            run.assert_called_once_with("ps", "--status", "running", "-q", "proxy")

    def test_docker_published_host_port_prevents_start(self):
        with patch("envd.proxy.subprocess.run") as docker:
            docker.return_value.returncode = 0
            docker.return_value.stdout = "80/tcp\n0.0.0.0:443->8443/tcp\n"
            with self.assertRaisesRegex(ComposeError, "TCP port 443 is already in use"):
                proxy._check_ports()

    def test_existing_owned_proxy_can_be_reused(self):
        with tempfile.TemporaryDirectory() as directory, patch(
            "envd.proxy.Compose"
        ) as compose, patch("envd.proxy._check_ports") as check_ports:
            run = compose.return_value.run
            run.return_value = "owned-container-id"
            proxy.ensure(Path(directory))
            check_ports.assert_not_called()
            run.assert_called_with("up", "-d", "proxy")
