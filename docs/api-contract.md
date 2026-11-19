# odoo-envd REST API — contract v1 (for the GUI module)

Frozen contract consumed by the future Odoo GUI module (a separate Odoo
database on another machine). The engine must keep these paths, verbs and
shapes; additive changes (new optional fields, new endpoints) are allowed.

- **Base**: `https://api.<your-domain>/api` (TLS via Traefik) or plain HTTP
  inside a private network (WireGuard).
- **Auth**: `Authorization: Bearer <PAT>` (created with `envd token create`).
  Scope hierarchy: **admin ⊃ write ⊃ read**.
- **Async pattern**: every mutation returns `202 {"job_id": N}`. Track via
  `GET /api/jobs/{id}` or the SSE stream. Jobs log to
  `GET /api/jobs/{id}/logs`. Idempotency: `POST /api/envs` on an existing
  branch re-ensures it (`reused_existing: true`).
- **Error shape**: FastAPI default (`{"detail": "..."}`), plus
  `404` unknown env/backup/job, `409` collisions (slug/port), `410` backup
  file gone, `422` invalid input, `403` auth/scope, `401` missing header.
- **OpenAPI**: served at `/api/openapi.json`, docs at `/api/docs`.

## Path convention: env identifier is the SLUG

Branches contain slashes (`feature/x`) which cannot be path segments. Every
env-scoped route takes the env's **slug** (`feature/x` → `feature_x`;
returned by `POST /api/envs` and present in every list item). Plain branch
names without slashes (`main`) also work directly. Responses always carry
both `branch` and `slug`.

## Environments

| Method & path | Scope | Notes |
|---|---|---|
| `GET /api/envs?state=&pinned=&page=&per_page=` | read | `{items: [env…], total}`; no credentials in items |
| `GET /api/envs/{env}` | write | detail incl. `admin_password` (odoo login), backups counts, last job |
| `POST /api/envs` | write | `{branch, with_demo?, seed?, neutralize?, pinned?, image_tag?, direct_port?}` → 202 |
| `PATCH /api/envs/{env}` | write | `{pinned?, upgrade_strategy?, on_deploy_failure?, workers?, image_tag?, direct_port?}` |
| `DELETE /api/envs/{env}?keep_db=&keep_backups=` | write | destroy (native `db drop` + role + worktree + route) |
| `POST /api/envs/{env}/deploy` | write | `{mode: changed\|full\|outdated}` → 202 |
| `POST /api/envs/{env}/rollback` | write | `{to: "pre-deploy"\|"latest"\|"<backup_id>", sha?}` → 202 |
| `POST /api/envs/{env}/recreate` | write | drop DB + rebuild from branch |
| `POST /api/envs/{env}/duplicate` | write | `{target_branch, neutralize?}` → env B seeded from env A (native `db duplicate`) |
| `POST /api/envs/{env}/neutralize` | write | native `odoo neutralize` — demo-safe (no mails) |
| `POST /api/envs/{env}/obfuscate` | write | `{pwd, fields?, unobfuscate?, vacuum?}` — **the tool's own source states it is not safe for third-party transfer** |
| `POST /api/envs/{env}/action` | write | `{action: start\|stop\|restart}` |
| `GET /api/envs/{env}/logs?tail=&job=` | read | latest or given job log text |
| `GET /api/envs/{env}/modules?refresh=1` | read | cached `installed_count`, or live parsed `odoo module list` |
| `GET /api/envs/{env}/health` | read | `{http: bool}` |

Env item fields: `branch, slug, db_name, url, state, pinned, with_demo, seed,
last_sha, last_deploy_at, deploy_count, image_tag, upgrade_strategy,
on_deploy_failure, workers, direct_port, installed_count, created_at,
updated_at`. States: `creating, deploying, running, stopped, failed`.

## Backups & imports

| Method & path | Scope | Notes |
|---|---|---|
| `GET /api/envs/{env}/backups?kind=` | read | kinds: `pre-deploy, daily, manual` |
| `POST /api/envs/{env}/backups` | write | `{with_filestore}` → native `db dump` |
| `GET /api/envs/{env}/backups/{id}` | read | manifest: sha, image_tag, filestore flag |
| `GET /api/envs/{env}/backups/{id}/download` | write | streams the zip (restorable by any odoo 20) |
| `POST /api/envs/{env}/backups/{id}/restore` | write | `{target_branch?}` — same env = rollback; other = cross-env restore (copy + import) |
| `DELETE /api/envs/{env}/backups/{id}` | admin | deletes row + file |
| `POST /api/imports` | write | `{dump_url, target_branch, neutralize?}` — prod backup URL → branch env |
| `POST /api/imports/upload?target_branch=&filename=` | write | raw zip as request body |

## Jobs, events, system

| Method & path | Scope | Notes |
|---|---|---|
| `GET /api/jobs?env=&state=&page=` | read | newest first |
| `GET /api/jobs/{id}` | read | steps with per-step state/duration/error |
| `POST /api/jobs/{id}/cancel` | write | queued: instant; running: next step boundary |
| `GET /api/jobs/{id}/logs?tail=` | read | full operational log (secrets redacted) |
| `GET /api/events?last_id=` | read | **SSE**: `event: <kind>`, `data: <json>`; kinds `job.queued/started/finished/failed/cancelled`, `env.created/updated/destroyed/neutralized/obfuscated/rolled_back/health`, `env.warning`. Heartbeat comments keep the stream warm. |
| `GET /api/system/info` | read | engine version, image pins, port pool, policy, env counts, dry_run flag |
| `GET /api/system/healthz` | open | 204 |
| `POST /api/hook` | open (HMAC) | forge push webhook — signature verified, fails closed |

## Webhook (the automatic path)

Register the forge webhook to `POST /api/hook` with the shared secret:

- GitHub: `X-Hub-Signature-256` (HMAC-SHA256 of the raw body, `sha256=…` prefix)
- Gitea: `X-Gitea-Signature` (HMAC-SHA256)
- GitLab: `X-Gitlab-Token` (shared secret compared in constant time)

Response: `{"accepted": bool, "action": ensure|deploy|destroy, "job_id", "branch"}`
or `{"accepted": false, "reason": …}` for ignored events (other repos, tags,
protected branches). Deletion of a protected branch never destroys its env.

## Example session

```bash
TOKEN=envd_…   # envd token create --name gui --scopes admin
H="Authorization: Bearer $TOKEN"

curl -H "$H" -X POST https://api.example.com/api/envs \
  -d '{"branch": "feature/new-checkout"}'
# → 202 {"job_id": 9, "branch": "feature/new-checkout", "slug": "feature_new_checkout", …}

curl -H "$H" https://api.example.com/api/jobs/9          # poll or subscribe /api/events
curl -H "$H" https://api.example.com/api/envs/feature_new_checkout
curl -H "$H" -X POST https://api.example.com/api/envs/feature_new_checkout/rollback \
  -d '{"to": "pre-deploy"}'
```

## GUI integration notes (for the module on the other machine)

- store engine URL + token in `res.config.settings` (or system parameters);
- sync env list via `GET /api/envs` (cron) or keep live via SSE
  `GET /api/events` (long-lived worker); map `slug` → records for all actions;
- the `admin_password` in env detail is the Odoo login for the env's web UI —
  offer a "copy login" action; never display the PG role password (the API
  never returns it);
- every mutation is a job: render buttons optimistically and reconcile from
  `job.finished`/`job.failed` events rather than the 202 alone;
- rollback restores the *database*: state after the backup point is lost —
  the GUI should say so explicitly.
