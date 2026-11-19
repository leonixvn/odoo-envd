# odoo-envd

CD engine for **Odoo branch→database environments**: every branch of your custom-modules
repo becomes a full staging environment (git worktree + PostgreSQL database + Odoo
service), deployed on push, destroyed on branch deletion — driven end-to-end by
**native Odoo 20 tooling** (`odoo db init/dump/load/duplicate/drop`,
`odoo module install/upgrade/list`, `odoo neutralize`) and exposed through a REST API.

```
git push ──▶ forge webhook ──▶ odoo-envd ──▶ kit scripts ──▶ odoo-bin inside each env's
                    │                          │              docker compose project
                    │                          └── worktree, per-env PG role, traefik route
                    └── REST + SSE API  ◀── your Odoo GUI module (separate machine)
```

## Design contract

- **One env per branch**: worktree at `$root/envs/<slug>/repo` (bind-mounted `:ro`),
  one PG database + role (`env_<slug>`), one compose project (`env_<slug>`), routed as
  `https://<slug>.<domain>` by Traefik labels — zero edge configuration per env.
- **Native-first**: backup = `odoo db dump` (zip with/without filestore), restore =
  `odoo db load`, upgrade = `odoo module upgrade` (dependents cascade natively), fresh
  env = `odoo db init` + `odoo module install $(apps.txt)`, seed = `odoo db duplicate`.
  The engine stores only orchestration state (SQLite WAL) — never your data.
- **Rollback first-class**: every deploy takes a `--no-filestore` safety dump before
  upgrading; a failed upgrade auto-restores it (per-module commits make partial upgrades
  a real state — restore is the only correct recovery). Manual rollback from any backup
  is one API call.
- **Engine repo ≠ modules repo**: developers control what Odoo runs; this repo controls
  how it gets there. No secrets live in the modules repo.

## Repository layout

| Path | Purpose |
|---|---|
| `envd/` | FastAPI daemon: REST + SSE API, job runner (per-env flock), state store |
| `kit/` | Operational bash scripts (also runnable by hand) + `odoo.conf` / compose templates |
| `bootstrap/` | Server provisioning: docker/Traefik/Postgres, base image, systemd units + timers |
| `docs/api-contract.md` | Frozen REST contract consumed by the future GUI module |
| `docs/ops-runbook.md` | Human procedures: bootstrap, upgrade, recovery |
| `docs/devlog.md` | Autonomous build-loop rounds: what was built, reviewed, fixed |

## Quick start (deploy server)

```bash
# 1. bootstrap the server (docker, traefik, postgres, dirs, engine service)
sudo ./bootstrap/bootstrap.sh

# 2. configure
sudo cp envd.example.toml /etc/odoo-envd/envd.toml  # edit: domain, forge, secrets

# 3. create an API token for the GUI
envd token create --name gui --scopes admin

# 4. register the forge webhook → http://<server>:8100/api/hook (HMAC secret from config)
```

## Development

```bash
uv venv && uv pip install -e . --group dev
ruff check .
pytest            # unit tests + kit dry-run suite (ENVD_DRY_RUN=1, no docker needed)
```

The kit and the subprocess layer honour `ENVD_DRY_RUN=1`: commands are echoed, artifacts
rendered, nothing executes — that is how the test suite exercises the full operational
flow without a docker daemon.

## License

MIT — see [LICENSE](LICENSE).
