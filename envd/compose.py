"""Generate and run the Compose project for a sandbox."""

import subprocess
from pathlib import Path

ODOO_IMAGE = "odoo:20.0-20261112"
POSTGRES_IMAGE = "postgres:16"
DATABASE = "odoo"

ODOO_PROBE = (
    "from urllib.request import urlopen; "
    "response = urlopen('http://127.0.0.1:8069/web/login', timeout=5); "
    "assert response.status == 200 and b'oe_login_form' in response.read()"
)


class ComposeError(RuntimeError):
    pass


def configuration(
    port: int, postgres_password: str, odoo_db_password: str, project: str, hostname: str,
    ingress_network: str,
) -> dict:
    router = f"traefik.http.routers.{project}"
    redirect = f"{project}-redirect"
    return {
        "services": {
            "postgres": {
                "image": POSTGRES_IMAGE,
                "environment": {
                    "POSTGRES_USER": "postgres",
                    "POSTGRES_PASSWORD": postgres_password,
                },
                "volumes": ["postgres:/var/lib/postgresql/data"],
                "networks": ["database"],
                "healthcheck": {
                    "test": ["CMD", "pg_isready", "-h", "127.0.0.1", "-U", "postgres"],
                    "interval": "2s",
                    "timeout": "3s",
                    "retries": 60,
                },
                "restart": "unless-stopped",
            },
            "odoo": {
                "image": ODOO_IMAGE,
                "environment": {
                    "PGHOST": "postgres",
                    "PGPORT": "5432",
                    "PGUSER": "odoo",
                    "PGPASSWORD": odoo_db_password,
                },
                "command": [
                    "odoo", "-d", DATABASE, "--db-filter=^odoo$$",
                    "--no-database-list", "--max-cron-threads=0", "--proxy-mode",
                ],
                "depends_on": {"postgres": {"condition": "service_healthy"}},
                "ports": [f"127.0.0.1:{port}:8069"],
                "volumes": ["odoo:/var/lib/odoo"],
                "networks": ["database", "ingress"],
                "labels": {
                    "traefik.enable": "true",
                    "envd.proxy": ingress_network,
                    "traefik.docker.network": ingress_network,
                    f"{router}.rule": f"Host(`{hostname}`)",
                    f"{router}.entrypoints": "https",
                    f"{router}.tls.certresolver": "letsencrypt",
                    f"{router}.service": project,
                    f"{router}-http.rule": f"Host(`{hostname}`)",
                    f"{router}-http.entrypoints": "http",
                    f"{router}-http.middlewares": redirect,
                    f"{router}-http.service": project,
                    f"traefik.http.middlewares.{redirect}.redirectscheme.scheme": "https",
                    f"traefik.http.services.{project}.loadbalancer.server.port": "8069",
                },
                "healthcheck": {
                    "test": ["CMD", "python3", "-c", ODOO_PROBE],
                    "interval": "2s",
                    "timeout": "6s",
                    "retries": 60,
                },
                "restart": "unless-stopped",
            },
        },
        "networks": {
            "database": {"internal": True},
            "ingress": {"external": True, "name": ingress_network},
        },
        "volumes": {"postgres": {}, "odoo": {}},
    }


class Compose:
    def __init__(self, directory: Path, project: str):
        self.command = [
            "docker", "compose", "--project-name", project,
            "--project-directory", str(directory), "--env-file", "/dev/null",
            "-f", str(directory / "compose.json"),
        ]

    def run(
        self, *args: str, sql: str | None = None, secrets: tuple[str, ...] = (),
    ) -> str:
        result = subprocess.run(
            [*self.command, *args], input=sql, capture_output=True, text=True, check=False,
        )
        if result.returncode:
            detail = (result.stderr or result.stdout).strip()
            for secret in secrets:
                detail = detail.replace(secret, "[redacted]")
            raise ComposeError(f"Compose {args[0]} failed: {detail}")
        return result.stdout
