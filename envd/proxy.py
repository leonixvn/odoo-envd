"""Routing configuration for the Greeni host's Coolify proxy."""

import subprocess
from pathlib import Path

from envd.compose import ComposeError

NETWORK = "coolify"


def domain() -> str:
    return "greeni.leonix.cc"


def ensure(_root: Path) -> None:
    result = subprocess.run(
        ["docker", "network", "inspect", "--format", "{{.Name}}", NETWORK],
        capture_output=True, text=True, check=False,
    )
    if result.returncode:
        raise ComposeError(f"Greeni requires the Coolify ingress network: {result.stderr.strip()}")
