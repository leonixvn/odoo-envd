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
