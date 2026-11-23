"""Sandbox lifecycle, shared by command-line and future API callers."""

import fcntl
import json
import re
import secrets
import tempfile
from contextlib import contextmanager
from pathlib import Path

from envd import proxy
from envd.compose import DATABASE, Compose, ComposeError, configuration


def _write_json(path: Path, data: dict) -> None:
    with tempfile.NamedTemporaryFile(mode="w", dir=path.parent, delete=False) as file:
        temporary = Path(file.name)
        try:
            json.dump(data, file, indent=2)
            file.write("\n")
            file.flush()
            temporary.replace(path)
        finally:
            temporary.unlink(missing_ok=True)


class SandboxError(RuntimeError):
    pass


class Sandboxes:
    def __init__(self, root: Path):
        self.root = root.expanduser().resolve()

    @contextmanager
    def _locked(self, name: str):
        if not re.fullmatch(r"[a-z][a-z0-9-]{0,39}", name):
            raise SandboxError("Use a name of up to 40 lowercase letters, digits, or dashes, starting with a letter.")
        locks = self.root / ".locks"
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        locks.mkdir(mode=0o700, exist_ok=True)
        with (locks / name).open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            yield self.root / name

    def names(self) -> list[str]:
        if not self.root.exists():
            return []
        return sorted(
            entry.name for entry in self.root.iterdir()
            if not entry.name.startswith(".") and (entry / "sandbox.json").is_file()
        )

    def create(self, name: str, port: int = 0) -> dict:
        if not 0 <= port <= 65535:
            raise SandboxError("Port must be between 0 and 65535 (0 lets Docker choose).")
        with self._locked(name) as directory:
            if directory.exists():
                raise SandboxError(f"Sandbox {name!r} already exists. Use start to resume it.")
            settings = {
                "project": f"envd-{name}-{secrets.token_hex(8)}",
                "admin_password": secrets.token_urlsafe(24),
                "odoo_db_password": secrets.token_hex(24),
                "postgres_password": secrets.token_hex(24),
                "port": port,
                "hostname": f"{secrets.token_hex(8)}.{proxy.domain()}",
            }
            proxy.ensure(self.root)
            # Save the sandbox identity and credentials before running Docker.
            with tempfile.TemporaryDirectory(prefix=".create-", dir=self.root) as stage:
                pending = Path(stage) / "sandbox"
                pending.mkdir(mode=0o700)
                _write_json(pending / "sandbox.json", settings)
                pending.rename(directory)
            try:
                return self._start(directory, settings)
            except ComposeError as exc:
                raise SandboxError(f"{exc}\nSandbox saved. Run 'envd start {name}' to retry.") from exc

    def start(self, name: str) -> dict:
        with self._locked(name) as directory:
            settings = self._settings(directory)
            proxy.ensure(self.root)
            return self._start(directory, settings)

    def _start(self, directory: Path, settings: dict) -> dict:
        if "hostname" not in settings:
            settings["hostname"] = f"{secrets.token_hex(8)}.{proxy.domain()}"
            _write_json(directory / "sandbox.json", settings)
        _write_json(directory / "compose.json", configuration(
            settings["port"], settings["postgres_password"],
            settings["odoo_db_password"], settings["project"], settings["hostname"],
            proxy.NETWORK,
        ))
        compose = Compose(directory, settings["project"])
        compose.run("up", "-d", "--wait", "--wait-timeout", "120", "postgres")
        ready = directory / "initialized"
        psql = ("exec", "-T", "postgres", "psql", "-X", "-v", "ON_ERROR_STOP=1", "-U", "postgres")
        if not ready.exists():
            password = settings["odoo_db_password"]
            compose.run(
                *psql, "-d", "postgres",
                sql=(
                    "DO $$ BEGIN IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'odoo') "
                    "THEN CREATE ROLE odoo LOGIN; END IF; END $$;\n"
                    f"ALTER ROLE odoo CREATEDB PASSWORD '{password}';\n"
                ),
                secrets=(password,),
            )
            admin_password = settings["admin_password"]
            # The service has never been exposed at this point. Discard a partial
            # database from an interrupted first initialization before retrying.
            compose.run(
                "run", "--rm", "--no-deps", "-T", "odoo",
                "odoo", "db", "init", DATABASE, "--force", "--password", admin_password,
                secrets=(admin_password,),
            )
            ready.touch(mode=0o600)
        compose.run(*psql, "-d", "postgres", "-c", "ALTER ROLE odoo NOCREATEDB")
        compose.run("up", "-d", "--wait", "--wait-timeout", "180", "odoo")
        return self._status(directory, settings)

    def stop(self, name: str) -> None:
        with self._locked(name) as directory:
            settings = self._settings(directory)
            if (directory / "compose.json").exists():
                Compose(directory, settings["project"]).run("stop")

    def delete(self, name: str) -> None:
        with self._locked(name) as directory:
            settings = self._settings(directory)
            if (directory / "compose.json").exists():
                Compose(directory, settings["project"]).run("down", "--volumes", "--remove-orphans")
            # Remove the entire record atomically from the inventory.
            with tempfile.TemporaryDirectory(prefix=".deleted-", dir=self.root) as stage:
                directory.rename(Path(stage) / "sandbox")

    def status(self, name: str) -> dict:
        with self._locked(name) as directory:
            return self._status(directory, self._settings(directory))

    def credentials(self, name: str) -> dict:
        with self._locked(name) as directory:
            settings = self._settings(directory)
            return {"login": "admin", "initial_password": settings["admin_password"]}

    def logs(self, name: str, lines: int = 100) -> str:
        with self._locked(name) as directory:
            settings = self._settings(directory)
            return Compose(directory, settings["project"]).run(
                "logs", "--no-color", "--tail", str(lines),
            )

    @staticmethod
    def _settings(directory: Path) -> dict:
        path = directory / "sandbox.json"
        if not path.is_file():
            raise SandboxError(f"Unknown sandbox {directory.name!r}.")
        return json.loads(path.read_text())

    @staticmethod
    def _status(directory: Path, settings: dict) -> dict:
        compose = Compose(directory, settings["project"])
        output = ""
        if (directory / "compose.json").exists():
            output = compose.run("ps", "--all", "--format", "json")
        # Compose versions emit either a JSON array or one object per line.
        if output.lstrip().startswith("["):
            containers = json.loads(output)
        else:
            containers = [json.loads(line) for line in output.splitlines() if line.strip()]
        services = {}
        url = None
        for container in containers:
            service = container["Service"]
            services[service] = {
                "state": container["State"],
                "health": container.get("Health") or None,
            }
            if service == "odoo" and container["State"] == "running":
                for publisher in container.get("Publishers") or []:
                    if publisher.get("TargetPort") == 8069 and publisher.get("PublishedPort"):
                        url = f"http://127.0.0.1:{publisher['PublishedPort']}/web/login"
                        break
        return {
            "name": directory.name,
            "project": settings["project"],
            "initialized": (directory / "initialized").exists(),
            "hostname": settings.get("hostname"),
            "url": f'https://{settings["hostname"]}/web/login' if settings.get("hostname") else url,
            "local_url": url,
            "services": services,
        }
