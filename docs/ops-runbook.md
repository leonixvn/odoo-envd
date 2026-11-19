# odoo-envd operations runbook

Human procedures for the deploy server. The engine's automatic paths are:
forge webhook → ensure/deploy/destroy. Everything below is the manual
equivalent plus recovery.

## 0. Prerequisites

- Debian/Ubuntu with: `git`, `curl`, docker + compose plugin, `uv`
- DNS: `*.odoo.example.com` and `api.odoo.example.com` → this host
- Secrets you will need: forge webhook secret, odoo/enterprise access token
  (GitHub PAT with repo access), Postgres superuser password for the infra
  container

## 1. Bootstrap (once)

```bash
sudo ./bootstrap/bootstrap.sh \
    --modules-url git@github.com:acme/odoo-custom.git \
    --enterprise-token ghp_…
```

This creates `/srv/odoo/{envs,data,backups,enterprise,envd,infra,locks}`,
starts traefik + postgres, clones the pinned enterprise, mirrors the modules
repo, builds `envd-base:20`, installs the engine as `odoo-envd.service` and
three timers (backup 02:30, prune 03:30, weekly upgrade Sun 05:00).

**Adjust before restarting:**
- `bootstrap/units/*.service`: the `WorkingDirectory`/`ExecStart` paths must
  match the engine clone location on this server.
- `/etc/odoo-envd/envd.toml`: domain, `[forge]` type/secret/repos,
  `[docker] base_image` (match the tag in `bootstrap/docker/Dockerfile.base`),
  `[postgres]` values.
- `bootstrap/docker-compose.infra.yml`: traefik ACME provider + email, the
  postgres superuser password (engine uses socket access only; the password
  protects the published port we never publish).

Then:

```bash
sudo systemctl restart odoo-envd
/home/odoo/Projects/odoo-envd/.venv/bin/envd check
curl -s http://127.0.0.1:8100/api/system/healthz -o /dev/null -w '%{http_code}\n'  # 204
```

## 2. Register the forge webhook

Repo → Settings → Webhooks → `http://<host>:8100/api/hook`, content type
`application/json`, secret from `[forge]`. For GitHub use
`X-Hub-Signature-256` (sha256). For GitLab, the token; Gitea, the signature.

Smoke test:

```bash
BRANCH=feature/smoke curl -X POST .../api/envs -H "Authorization: Bearer $TOKEN" \
  -d '{"branch": "feature/smoke"}'
```

…then delete the branch on the forge and watch the env disappear (or use
`DELETE .../api/envs/feature_smoke`).

## 3. Everyday manual operations (same code as the API)

```bash
KIT=/home/odoo/Projects/odoo-envd/.venv/bin/envd
$KIT flow deploy feature/x
$KIT flow backup feature/x --param kind=manual
$KIT flow rollback feature/x --param to=pre-deploy
$KIT flow destroy feature/x --param keep_db=true
$KIT cron daily-backup   # what the timer runs
$KIT cron prune
```

Logs for every job: `/srv/odoo/envd/logs/<job_id>.log` (secrets redacted).

## 4. Backup & restore drills

Backups are **native odoo zips** (`dump.sql` + `manifest.json` + `filestore/`)
under `/srv/odoo/backups/<slug>/{pre-deploy,daily,manual,imports}`.
Retention per `envd.toml [policy.retention]`.

**Drill: restore a backup on any odoo 20 (offsite!)**

```bash
curl -H "Authorization: Bearer $TOKEN" .../api/envs/<slug>/backups/<id>/download -o env.zip
docker run --rm -v $PWD:/d -v odoo-data:/var/lib/odoo odoo:20.0-20261112 \
    -- odoo db load drill_db /d/env.zip
```

**Drill: copy production into a staging env**

```bash
curl -X POST .../api/imports -H "Authorization: Bearer $TOKEN" \
  -d '{"dump_url": "https://backups.example/prod-2026-11-19.zip",
       "target_branch": "feature/prod-repro", "neutralize": true}'
```

The engine downloads server-side, `odoo db load -n` neutralizes on import,
then `module upgrade all --outdated` aligns the branch code.

## 5. Recovery

- **Failed deploy**: with `on_deploy_failure = rollback` the env restored the
  pre-deploy dump and serves the old sha; check
  `GET /api/jobs/<id>/logs`, fix the branch, push again. With `halt` the env
  is left stopped on purpose.
- **Half-upgraded env** (e.g. crash during `module upgrade`): the pre-deploy
  safety dump is the recovery point — `rollback to=pre-deploy`. Odoo
  upgrades commit per-module; there is no down-migration.
- **Engine itself**: it is stateless code + SQLite at
  `/srv/odoo/envd/state.db`. `systemctl restart odoo-envd` is safe mid-job
  (jobs are shell subprocesses with flock); the worst case is a job marked
  running that has actually finished — reconcile with `recreate`.
- **Docker daemon down**: all envs down (staging-accepted SPOF); `systemctl
  restart docker` and the compose `restart: unless-stopped` policy brings
  envs back.
- **Mirror stale** (engine cannot fetch): `git -C /srv/odoo/repo.git fetch
  --prune origin` by hand, check credentials/deploy keys.

## 6. Upgrading the engine

```bash
cd /home/odoo/Projects/odoo-envd && git pull
uv pip install -e . --group dev && systemctl restart odoo-envd
```

The API contract is additive-only; check `docs/api-contract.md` + the
`devlog.md` for behavior changes. Kit/unit files: re-run
`bootstrap/bootstrap.sh` (idempotent) or re-install the units by hand.

## 7. Security notes

- Tokens are stored sha256-hashed; a lost token is *deleted*, not recovered:
  `envd token delete --id N`.
- The API binds 127.0.0.1 by default. Expose for the GUI machine via
  Traefik (`api.<domain>` → host proxy) or WireGuard only. The webhook is the
  only unauthenticated route — HMAC-verified, failing closed without a
  secret.
- `/api/docs` + `/api/openapi.json` are unauthenticated (shape only, no
  data); front them with the same TLS + IP allowlist if unwanted.
- Enterprise code never enters the modules repo; it is a pinned clone at
  `/srv/odoo/enterprise`, mounted read-only into every env. Verify your
  Odoo Enterprise contract covers self-hosted staging copies.
- The engine needs root-level docker access (docker group equivalent); run
  it as a dedicated system user with docker group membership if your policy
  requires, keeping `/srv/odoo` ownership consistent.
