"""Local command-line interface for the sandbox manager."""

import argparse
import json
import os
import sys
from pathlib import Path

from envd.compose import ComposeError
from envd.sandbox import SandboxError, Sandboxes


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="envd", description="Manage Odoo sandboxes.")
    default_root = Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local/share") / "odoo-envd"
    parser.add_argument("--root", type=Path, default=default_root, help="sandbox state directory")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("list", help="list saved sandboxes")
    for command, description in (
        ("create", "provision and start a base Odoo sandbox"),
        ("start", "start a sandbox or retry interrupted provisioning"),
        ("stop", "stop a sandbox and retain its data"),
        ("status", "show container state and the login URL"),
        ("credentials", "show the initial Odoo administrator login"),
        ("logs", "show recent container logs"),
        ("delete", "destroy a sandbox and its database and filestore"),
    ):
        subparser = commands.add_parser(command, help=description)
        subparser.add_argument("name")
        if command == "create":
            subparser.add_argument("--port", type=int, default=0, help="loopback port (default: auto)")
        elif command == "delete":
            subparser.add_argument("--yes", required=True, action="store_true", help="confirm data deletion")
        elif command == "logs":
            subparser.add_argument("--lines", type=int, default=100)
    args = parser.parse_args(argv)
    manager = Sandboxes(args.root)
    try:
        match args.command:
            case "list":
                result = manager.names()
            case "create":
                result = manager.create(args.name, args.port)
            case "logs":
                print(manager.logs(args.name, args.lines), end="")
                return 0
            case "start":
                result = manager.start(args.name)
            case "stop":
                manager.stop(args.name)
                result = {"name": args.name, "stopped": True}
            case "status":
                result = manager.status(args.name)
            case "credentials":
                result = manager.credentials(args.name)
            case "delete":
                manager.delete(args.name)
                result = {"name": args.name, "deleted": True}
        print(json.dumps(result, indent=2))
        return 0
    except (ComposeError, SandboxError, ValueError, OSError) as exc:
        print(f"envd: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
