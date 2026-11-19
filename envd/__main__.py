"""envd CLI: serve | token | flow | check.

The kit bash shim (kit/envd-kit.sh) calls ``envd flow`` — the same engine
code paths as the HTTP API, run in the foreground for humans and cron.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

DEFAULT_CONFIG = "/etc/odoo-envd/envd.toml"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="envd", description="odoo-envd CD engine")
    parser.add_argument("-c", "--config", default=DEFAULT_CONFIG)
    sub = parser.add_subparsers(dest="command", required=True)

    serve = sub.add_parser("serve", help="run the REST API")
    serve.add_argument("--host", default=None)
    serve.add_argument("--port", type=int, default=None)

    token = sub.add_parser("token", help="manage API tokens")
    token_sub = token.add_subparsers(dest="token_command", required=True)
    token_create = token_sub.add_parser("create")
    token_create.add_argument("--name", required=True)
    token_create.add_argument("--scopes", default="write", help="comma list: read,write,admin")
    token_sub.add_parser("list")
    token_del = token_sub.add_parser("delete")
    token_del.add_argument("--id", type=int, required=True)

    flow = sub.add_parser("flow", help="run one flow in the foreground (kit entrypoint)")
    flow.add_argument("kind")
    flow.add_argument("branch", nargs="?", default=None)
    flow.add_argument("--param", action="append", default=[], metavar="KEY=VALUE")

    cron = sub.add_parser("cron", help="timer entrypoints (systemd units)")
    cron_sub = cron.add_subparsers(dest="cron_mode", required=True)
    cron_sub.add_parser("daily-backup", help="native db dump for every running env")
    cron_sub.add_parser("prune", help="destroy stale unpinned envs")
    cron_sub.add_parser("weekly-upgrade", help="module upgrade all for every running env")

    sub.add_parser("check", help="validate the config and print a summary")

    args = parser.parse_args(argv)

    from envd.config import ConfigError, load_config

    try:
        cfg = load_config(args.config)
    except ConfigError as exc:
        print(f"config error: {exc}", file=sys.stderr)
        return 2

    if args.command == "serve":
        return _serve(cfg, args)
    if args.command == "token":
        return _token(cfg, args)
    if args.command == "flow":
        return _flow(cfg, args)
    if args.command == "cron":
        return _cron(cfg, args)
    if args.command == "check":
        return _check(cfg)
    return 1


def _store_of(cfg):
    from envd.core.state import Store

    cfg.paths.logs.mkdir(parents=True, exist_ok=True)
    return Store(cfg.paths.state_db)


def _serve(cfg, args) -> int:
    import uvicorn

    from envd.api.app import create_app
    from envd.core.jobs import JobRunner

    store = _store_of(cfg)
    runner = JobRunner(cfg, store)
    app = create_app(cfg, store, runner)
    uvicorn.run(
        app,
        host=args.host or cfg.server.listen_host,
        port=args.port or cfg.server.listen_port,
        log_level="info",
    )
    return 0


def _token(cfg, args) -> int:
    store = _store_of(cfg)
    try:
        if args.token_command == "create":
            scopes = tuple(s.strip() for s in args.scopes.split(",") if s.strip())
            token_id, token = store.create_token(args.name, scopes)
            print(f"token {args.name!r} (id={token_id}, scopes={','.join(scopes)}):")
            print(token)
            print("store it now — it is not recoverable from the database.")
        elif args.token_command == "list":
            for row in store.list_tokens():
                print(f"{row['id']:>3}  {row['name']:<20} {row['scopes']}")
        elif args.token_command == "delete":
            print("deleted" if store.delete_token(args.id) else "not found")
    finally:
        store.close()
    return 0


def _flow(cfg, args) -> int:
    from envd.core.flows import FLOWS, FlowError
    from envd.core.jobs import JobRunner

    if args.kind not in FLOWS:
        available = ", ".join(sorted(FLOWS))
        print(f"unknown flow {args.kind!r} (available: {available})", file=sys.stderr)
        return 2

    params: dict = {}
    for item in args.param:
        key, _, value = item.partition("=")
        if not key:
            print(f"--param must be KEY=VALUE, got {item!r}", file=sys.stderr)
            return 2
        params[key] = value

    store = _store_of(cfg)
    try:
        branch = args.branch or params.get("target_branch") or params.get("branch")
        if not branch:
            print("a branch is required for this flow", file=sys.stderr)
            return 2
        if store.get_env(branch) is None and args.kind not in ("import",):
            print(f"env for branch {branch!r} not found in state", file=sys.stderr)
            return 3
        runner = JobRunner(cfg, store)
        job_id = runner.submit(args.kind, branch, params, foreground=True)
        job = store.get_job(job_id)
        print(f"job {job_id}: {job['state']}")
        if job.get("error"):
            print(f"error: {job['error']}")
        log_path = cfg.paths.logs / f"{job_id}.log"
        if log_path.is_file():
            print(f"log: {log_path}")
        return 0 if job["state"] == "success" else 1
    except FlowError as exc:
        print(f"flow error: {exc}", file=sys.stderr)
        return 1
    finally:
        store.close()


def _cron(cfg, args) -> int:
    """Timer entrypoints: loops over envs, foreground jobs, one summary line."""
    from datetime import UTC, datetime, timedelta

    from envd.core.jobs import JobRunner

    store = _store_of(cfg)
    runner = JobRunner(cfg, store)
    outcomes: list[tuple[str, str]] = []
    try:
        envs = store.list_envs()
        if args.cron_mode == "daily-backup":
            for env in envs:
                if env["state"] != "running":
                    continue
                job_id = runner.submit("backup", env["branch"], {"kind": "daily"}, foreground=True)
                outcomes.append((env["branch"], store.get_job(job_id)["state"]))
        elif args.cron_mode == "weekly-upgrade":
            for env in envs:
                if env["state"] != "running":
                    continue
                job_id = runner.submit("deploy", env["branch"], {"mode": "full"}, foreground=True)
                outcomes.append((env["branch"], store.get_job(job_id)["state"]))
        elif args.cron_mode == "prune":
            ttl = timedelta(days=cfg.policy.prune_ttl_days)
            now = datetime.now(UTC)
            for env in envs:
                if env["pinned"] or env["branch"] in cfg.policy.protect:
                    continue
                stamp = env.get("last_deploy_at") or env.get("created_at")
                try:
                    last = datetime.fromisoformat(stamp)
                except (TypeError, ValueError):
                    continue
                if now - last.astimezone(UTC) < ttl:
                    continue
                job_id = runner.submit("destroy", env["branch"], {}, foreground=True)
                outcomes.append((env["branch"], f"pruned:{store.get_job(job_id)['state']}"))
        for branch, state in outcomes:
            print(f"{branch}: {state}")
        return 0
    finally:
        store.close()


def _check(cfg) -> int:
    print("config OK")
    print(f"  root:      {cfg.paths.root}")
    print(f"  domain:    {cfg.server.domain}")
    print(f"  forge:     {cfg.forge.type} ({', '.join(cfg.forge.repos) or 'no repos'})")
    print(f"  base image: {cfg.docker.base_image}")
    print(f"  protect:   {', '.join(cfg.policy.protect)}")
    failure = cfg.policy.on_deploy_failure
    print(f"  strategy:   {cfg.policy.upgrade_strategy} / {failure} on failure")
    missing = [
        path for path in (cfg.paths.mirror, cfg.paths.enterprise)
        if not Path(path).exists()
    ]
    for path in missing:
        print(f"  NOTE: {path} does not exist yet (bootstrap will create it)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
