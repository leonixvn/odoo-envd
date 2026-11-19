# devlog — autonomous build-loop rounds

Working protocol: each round builds a coherent chunk, then **reviews it** (re-read with
fresh eyes + run the gates) and records findings honestly. Findings are fixed in the
next round (or immediately when they break the gates). Gates for every round:
`ruff check .` clean and `pytest` green with N > 0.

The design contract lives in `/home/cuong1405/.opencode/plan/odoo-branch-env-cd.md`;
source-verified Odoo 20.0 receipts cited there (file:line) are the engine's ground truth.

---

## Round 1 — foundations: repo, config, state store, slug/port tools

**Built**: git repo + uv/ruff/pytest toolchain; `envd.config` (TOML loader with
validated defaults, paths derived from `[paths] root`); `envd.tools.slug` (branch →
db-slug/host-label/db-name/compose-project + git-ref validation — the security boundary
for remote-controlled inputs); `envd.tools.ports` (direct-port pool);
`envd.core.state` (SQLite WAL store: envs, jobs, backups, events, sha256-hashed tokens;
scope hierarchy admin ⊃ write ⊃ read; constant-time token lookup with timing burn on
miss); 77 tests.

**Fixed during the round** (caught by gates/tests):
- syntax: tuple-target annotation `clauses, args: list[str], list[Any]` invalid — split.
- `state.update_job` stored a raw Python list into SQLite → now JSON-encoded.
- `verify_token` returned a stale row: `last_used_at` read from the pre-UPDATE row.
- test bugs: duplicate `[paths]` TOML table (invalid TOML) in an override test;
  `db_slug("----")` expectation wrong (raises `BranchError`, not `"b"`).

**Review findings — to fix in Round 2:**
- **F1 (config, real):** `_build_paths` resolves *relative* path overrides against the
  process CWD, not `[paths] root` — a relative `mirror = "alt/repo.git"` would land
  in a random CWD. Resolve against root.
- **F2 (config, robustness):** unknown keys in `[server]/[postgres]/[docker]` are
  silently ignored — a typo like `admin_passwd` yields an empty password at runtime.
  Reject unknown keys with a listing `ConfigError`.
- **F3 (config, robustness):** `[policy] port_range` unpacks a 2-tuple without
  checking length → raw `ValueError` instead of `ConfigError`.
- **F4 (config, correctness):** `max_slug_len` unbounded; PG names are ≤63 chars and
  names are prefixed `env_` → cap at 59.
- **F5 (state, robustness):** `supersede_queued(kinds=())` renders `kind IN ()` → SQL
  syntax error. Guard with `StoreError`.
- **F6 (state, API shape):** `prune_backups` returns ids only; the caller must also
  unlink the zip files → return `(id, path)` pairs.
- **F7 (state, noted for API round):** `create_env` raises raw
  `sqlite3.IntegrityError` on branch/slug/db collisions — the API layer must map this
  to HTTP 409.
- **F8 (hook, noted for Round 3):** with an empty `forge.secret`, webhook verification
  must fail closed (reject unsigned requests), never accept.

---

## Round 2 — execution layer: fixes F1–F6, shell/gitops/render/odoo_cli/pg

**Design deviation (documented):** flow logic lives in Python
(`core/flows.py`, coming in Round 3) instead of the planned bash scripts — the
bash "kit" becomes a thin human/cron entrypoint. Reason: a dual bash+Python
implementation of the same operational sequences is a defect factory; the Python
steps are unit-testable and DRY_RUN-verified, while the runbook keeps hand-run
instructions. The plan's intent (human-runnable ops, engine-agnostic core) is
preserved.

**Built**:
- `core/shell.py`: subprocess executor — global dry-run switch, line-streaming
  logs, bounded output capture, `::` marker collection, secret redaction
  (values masked in echo lines and env dumps), probe-with-retries, env *merge*
  (never replace PATH/HOME).
- `core/gitops.py`: mirror fetch (flock-serialized), `refs/heads/*` sha
  resolution (mirror clones carry branches directly — no `origin/*` refs),
  worktree add/reset/remove, changed-module diff parser with third_party flag,
  apps.txt/requirements readers, escalation rule.
- `core/render.py` + `envd/templates/{odoo.conf,compose.odoo.yml}.tpl`:
  per-env project rendering with the docker-entrypoint contract asserted on
  every render (conf must NOT contain db_* keys — the image entrypoint injects
  them from HOST/PORT/USER/PASSWORD env vars; studied at odoo/docker
  20.0/entrypoint.sh).
- `core/odoo_cli.py`: native command wrappers (db init/dump/load/duplicate/drop,
  module install/upgrade/list incl. `--outdated`, neutralize) — exact subcommand
  shapes verified against `cli/db.py` / `cli/module.py`; `module list` parser
  handles two-word states ("to upgrade") and skips log noise; health probe via
  `compose exec python3 -c urllib`.
- `core/pg.py`: per-env role admin, exec'd *inside the infra postgres
  container* (no host port, no engine-side password), SQL-safe charset
  enforcement.
- `tools/pw.py`: SQL-safe credential generation.
- Tests: 129 total (real-git integration for mirror/worktree/diff, render
  contract, command shapes under dry-run, shell redaction/failure/probe).

**Fixed during the round** (gates/tests):
- shell echo line leaked secrets (only streamed output was masked) — the
  initial `$ cmd` echo is now masked too (caught by the pg command-shape test).
- mirror ref semantics: `origin/<branch>` doesn't exist in `--mirror` clones;
  all lookups now use `refs/heads/<branch>`.
- `parse_module_list` broke on two-word states ("to upgrade") and accepted log
  noise as modules; rewritten with state validation.
- changed-module parser treated stray files directly under `addons/` as
  modules; now requires a file *inside* a module dir (≥3 path parts).
- render: stray `$` in the direct-port compose injection; env vars must merge,
  not replace, the ambient environment (real-run PATH bug).
- gitops: missing-branch `rev-parse` raised raw ShellError before the friendly
  GitError; now mapped explicitly.
- several of my own bad test expectations (escalation threshold math, duplicate
  TOML tables, pg password charset, compose arg order).

**Review findings — to fix in Round 3:**
- **F9 (shell, diagnostics):** output truncation keeps the head; error tails
  need the end — add a bounded tail ring.
- **F10 (shell, robustness):** non-FileNotFoundError OSErrors from Popen
  (e.g. PermissionError) escape raw — wrap in ShellError.
- **F11 (render, correctness):** empty `image_tag` renders `image:` — validate
  before writing.
- **F12 (pg, idempotency):** ensure-flow needs `db_exists()` (via
  `pg_database`) to guard `db init` — the CLI refuses existing databases.
- **F13 (flows, contract):** flows must stop the service before `db load
  --force`, tolerate `worktree_remove` failure, warn when apps.txt is missing,
  and check `role_exists` before creating.
- **F14 (hook, security):** empty `forge.secret` must fail closed.

---

## Round 3 — engine heart: flows, job runner, forge adapters

**Built**:
- `core/flows.py`: operational step sequences — **ensure** (fetch → worktree →
  role → deps-image → render → db init *or* seed duplicate → module install →
  up → health + installed-count), **deploy** (fetch → diff → safety dump →
  upgrade with auto-rollback → recreate → health), **destroy**, **backup**
  (with retention), **rollback** (pre-deploy / latest / by id). Rollback
  semantics per the verified odoo receipts: upgrades commit per-module
  (`loading.py:236-253`), so failure recovery = restore point, never "run old
  code on new schema".
- `core/jobs.py`: JobRunner — thread pool, per-env flock, newest-sha-wins at
  submit (older queued deploys superseded), step-by-step job tracking with
  logs at `envd/logs/<id>.log` (secrets redacted), cancellation at step
  boundaries, env-state policy (RolledBack ⇒ env stays *running* on the
  previous sha).
- `core/forge.py`: GitHub/GitLab/Gitea push adapters, HMAC sha256 / token
  verification (constant time), **fail closed on empty secret**, repo
  allowlist, tags and invalid branch names ignored, deletion triggers
  (github/gitea `deleted:true`, gitlab zero-`after`).
- Dry-run semantics hardened with a `shell.run_read()` tier: **plan-safe**
  commands (git rev-parse/diff/show, mirror fetch — engine bookkeeping that
  cannot mutate envs) execute even in dry-run, so plans are realistic;
  everything else is echoed only. `changed_modules` now diffs against the
  **mirror** (source of truth) instead of the worktree; `apps.txt`/
  `requirements.txt` are read via `git show refs/heads/<branch>:<path>`.
- Tests: 157 total, including end-to-end job runs under dry-run with real git
  (ensure→deploy→rollback→destroy), per-env lock serialization, log
  redaction, forge signature matrices.

**Fixed during the round** (gates/tests):
- dry-run flaw: read helpers returned empty under dry-run, so flows couldn't
  plan; introduced the run_read plan-safe tier (found via ensure failing on
  sha resolution).
- forge `_verify` crashed with AttributeError on *missing* headers instead
  of refusing (fail-closed path was untested until now).
- flows read repo content from a worktree that dry-run never materializes →
  now read from the mirror via `git show`.
- invalid annotation syntax, private shell member access, hardcoded escalation
  count (now the real installed count from `odoo module list`, stored on the
  env row — also useful to the future API).

**Review findings — fixed immediately (both safety-critical):**
- **FL-A (flows, real bug):** the deploy auto-rollback restored the DB while
  the env container was still connected — `db load --force` drops the
  database and PostgreSQL refuses to drop while sessions exist; the env would
  also have been left with containers down. The rollback branch now stops
  the service, restores, restarts, re-probes health, and only then reports
  RolledBack (env keeps serving the previous sha — the promise, now true).
- **FL-B (flows):** backup `kind` was unvalidated (an API caller could forge
  `pre-deploy` rows); now restricted to `manual|daily`.

**Carried to Round 4:** API + CLI + remaining API-facing flows (duplicate,
neutralize, import), SSE, docs/api-contract.md; kit bash shim; cancel-race
hardening (running jobs only stop at step boundaries — acceptable, to
document).

---

## Round 4 — the extensive REST API, CLI, kit shim, contract

**Built**:
- `api/` package: `create_app(cfg, store, runner)` + routers — envs (CRUD,
  deploy/rollback/recreate/duplicate/neutralize/obfuscate/action/logs/modules/
  health), backups (list/create/download/restore-in-place/cross-env-restore/
  delete + `POST /imports` URL and raw-bytes upload), jobs (list/detail/logs/
  cancel), system (info/healthz), `POST /api/hook` (HMAC push → ensure|
  deploy|destroy, protected branches checked **before** any state access), SSE
  `GET /api/events`. PAT bearer auth with the scope hierarchy; token rows are
  sha256-hashed; env list/detail responses never leak the PG role password.
- new flows: neutralize, obfuscate (with the tool's own third-party warning
  in logs + contract), action (start/stop/restart), recreate, import (URL or
  uploaded dump → `db load` + `module upgrade all --outdated`); deploy gained
  an explicit `mode: full|outdated` override that bypasses the noop check.
- `envd/__main__.py` CLI: serve, token create/list/delete, flow (foreground
  — used by `kit/envd-kit.sh`), check. Kit bash shim added.
- `docs/api-contract.md` (frozen, GUI-facing), `envd.example.toml`.

**Fixed during the round** (all caught by the new API test suite):
- **API-1 (real bug, routing):** `{branch}` path params don't match slashes —
  every branch-scoped route 404'd for the canonical `feature/x` case. Redesign:
  paths carry the **slug** (slash-free), bodies/responses carry the branch;
  `_resolve_env` also accepts plain slash-free branch names. POST /api/envs now
  returns the slug for the GUI. (Considered `{branch:path}` + global route
  ordering — rejected as fragile across routers.)
- **API-2 (real bug, data):** the duplicate/seed path built the source DB
  name from the raw **branch** (`db duplicate env_feature/x …`) instead of the
  slug-derived `env_feature_x` — nonexistent database in real mode. The exact-
  command log assertion caught it; seed is now slug-mapped.
- **API-3 (real bug, observability):** the runner never wrote `log_path` onto
  the job row → `/api/jobs/{id}/logs` always returned empty. Now recorded at
  execution start.
- hook protection ordering: protected-branch refusal now precedes the env
  lookup (the delete of a protected-but-uncreated branch must still say
  "protected").
- missing-token now correctly yields 401 (starlette) vs 403 (bad token);
  tests updated to assert both.
- `deploy mode=full` on an unchanged env was a silent noop — explicit modes
  now force the deploy.
- `require_runner` helper was referenced before definition → replaced with
  the shared `get_runner` dependency.
- multipart upload dropped in favour of raw-bytes + filename query param
  (machine-to-machine, no `python-multipart` dependency).

Gates: ruff clean, pytest 187 passed (60 API tests through TestClient,
including webhook HMAC matrices and SSE).

---

## Round 5 — bootstrap layer, cron timers, CLI coverage, deep final review

**Built**:
- `bootstrap/bootstrap.sh` (idempotent server provisioning), infra compose
  (Traefik + pinned `postgres:16`), `docker/Dockerfile.base` (pinned odoo:20 +
  build tools for deps layers), `enterprise.ref` pin, systemd `odoo-envd.service`
  + backup/prune/weekly timers.
- `envd cron {daily-backup,prune,weekly-upgrade}` — the timer entrypoints,
  looping envs foreground; prune respects pinned + protected and measures
  staleness from last deploy.
- `docs/ops-runbook.md`: bootstrap, webhook registration, manual ops, offsite
  restore drill, prod→staging import, recovery paths, security notes.
- Tests for the CLI surface (check/token/flow/cron) and the import endpoints.

**Final review findings — fixed in this round:**
- **R5-A (real bug):** `import_flow` lacked the ensure-prelude — importing
  into a brand-new env would run `db load` against a compose project that was
  never rendered and a worktree that didn't exist. Now: fetch → worktree →
  role → deps-image → render → fetch-dump → stop → load → upgrade → up →
  health.
- **R5-B (real bug):** deploy never re-rendered, so `PATCH`ed workers/image
  tag only took effect on the next *ensure*. Deploy now runs deps-image +
  render steps (idempotent) before upgrading.
- **R5-C:** `_rollback_resolve` blew up with raw `ValueError`s (garbage `to`
  value, or a backup row whose file lives outside the env's backups dir) →
  clean `FlowError`s; rollback-by-id is now also verified to belong to the
  env (cross-env restores go through the import flow by design).
- **R5-D:** `_backup_register` recorded `with_filestore=True` even for
  `--no-filestore` dumps — the row metadata lied; now reflects the actual
  dump.

**Gates at close:** ruff clean; pytest **201 passed**
(slug/port/config/state/shell/gitops-with-real-git/render/odoo-cli/pg/flows/
jobs/forge/api/CLI), one known deprecation warning from starlette's
TestClient shim (external).

**Honest limits of this validation:** everything above runs under
`ENVD_DRY_RUN` with **real git** — the docker/psql/odoo command sequences
are asserted exactly, but no real container, Postgres or odoo binary was
spawned in this environment. The plan's pilot (§8 of the design doc) —
bootstrap on the deploy server and the five end-to-end checks including a
deliberate crash-deploy — is the remaining validation step and needs the
actual server.

**Defect ledger across all rounds:** R1: 8 findings (F1-F8). R2: 14 found
(F9-F14 + 7 in-round). R3: 2 safety-critical (FL-A/FL-B) + 7 in-round.
R4: 3 real API bugs (routing slashes, seed DB name from raw branch, missing
log_path) + 6 in-round. R5: 4 (import prelude, deploy re-render, rollback
ValueErrors, backup metadata). All fixed; none deferred.
