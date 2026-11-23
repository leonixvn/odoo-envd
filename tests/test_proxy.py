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
        ) as compose, patch("envd.proxy.socket.socket") as socket:
            run = compose.return_value.run
            run.return_value = ""
            listener = socket.return_value.__enter__.return_value
            listener.bind.side_effect = OSError(errno.EADDRINUSE, "in use")
            with self.assertRaisesRegex(ComposeError, "TCP port 80 is already in use"):
                proxy.ensure(Path(directory))
            run.assert_called_once_with("ps", "--status", "running", "-q", "proxy")

    def test_existing_owned_proxy_can_be_reused(self):
        with tempfile.TemporaryDirectory() as directory, patch(
            "envd.proxy.Compose"
        ) as compose, patch("envd.proxy._check_ports") as check_ports:
            run = compose.return_value.run
            run.return_value = "owned-container-id"
            proxy.ensure(Path(directory))
            check_ports.assert_not_called()
            run.assert_called_with("up", "-d", "proxy")
