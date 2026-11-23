"""The reverse proxy owned by a standalone odoo-envd installation."""

import errno
import fcntl
import hashlib
import json
import os
import re
import socket
import subprocess
from pathlib import Path

from envd.compose import Compose, ComposeError

NETWORK = "odoo-envd-ingress"


def domain() -> str:
    value = os.environ.get("ENVD_DOMAIN", "").lower().rstrip(".")
    label = r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?"
    if len(value) > 236 or not re.fullmatch(rf"{label}(?:\.{label})+", value):
        raise ComposeError("Set ENVD_DOMAIN to the base domain for sandbox hostnames.")
    return value


def _port_conflict(port: str | int) -> ComposeError:
    return ComposeError(
        f"TCP port {port} is already in use. "
        "The standalone branch requires its own proxy on ports 80 and 443."
    )


def _check_ports() -> None:
    published = subprocess.run(
        ["docker", "ps", "--format", "{{.Ports}}"],
        capture_output=True, text=True, check=False,
    )
    if published.returncode:
        raise ComposeError(f"Could not inspect published Docker ports: {published.stderr.strip()}")
    # Docker may publish through NAT without a host socket a bind probe can see.
    occupied = re.search(r":(80|443)->\d+/tcp\b", published.stdout)
    if occupied:
        raise _port_conflict(occupied.group(1))
    for port in (80, 443):
        for family, address in ((socket.AF_INET, "0.0.0.0"), (socket.AF_INET6, "::")):
            try:
                with socket.socket(family, socket.SOCK_STREAM) as listener:
                    if family == socket.AF_INET6:
                        listener.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 1)
                    listener.bind((address, port))
            except OSError as exc:
                if exc.errno == errno.EACCES:
                    # Docker can bind privileged ports even if this CLI cannot.
                    continue
                if family == socket.AF_INET6 and exc.errno in (
                    errno.EAFNOSUPPORT, errno.EADDRNOTAVAIL, errno.EPROTONOSUPPORT,
                ):
                    continue
                if exc.errno == errno.EADDRINUSE:
                    raise _port_conflict(port) from exc
                raise


def ensure(root: Path) -> None:
    directory = root / ".proxy"
    directory.mkdir(mode=0o700, exist_ok=True)
    identity = hashlib.sha256(os.fsencode(root)).hexdigest()[:12]
    compose = Compose(directory, f"envd-proxy-{identity}")
    with (directory / "lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        (directory / "compose.json").write_text(json.dumps({
            "services": {
                "proxy": {
                    "image": "traefik:v3.7",
                    "command": [
                        "--providers.docker=true",
                        "--providers.docker.exposedbydefault=false",
                        f"--providers.docker.constraints=Label(`envd.proxy`,`{NETWORK}`)",
                        "--entrypoints.http.address=:80",
                        "--entrypoints.https.address=:443",
                        "--certificatesresolvers.letsencrypt.acme.httpchallenge.entrypoint=http",
                        "--certificatesresolvers.letsencrypt.acme.storage=/acme/acme.json",
                    ],
                    "ports": ["80:80", "443:443"],
                    "volumes": ["/var/run/docker.sock:/var/run/docker.sock:ro", "acme:/acme"],
                    "networks": ["ingress"],
                    "restart": "unless-stopped",
                },
            },
            "networks": {"ingress": {"name": NETWORK}},
            "volumes": {"acme": {}},
        }))
        if not compose.run("ps", "--status", "running", "-q", "proxy").strip():
            _check_ports()
        compose.run("up", "-d", "proxy")
