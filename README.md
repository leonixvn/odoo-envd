# odoo-envd

Manage multiple standalone Odoo sandboxes on one Linux Docker host. Each sandbox is a
separate Compose project with its own Odoo service, PostgreSQL service, database
network, database volume, and Odoo filestore volume.

## Run it

Use Python 3.10+ and Docker Compose v2 with support for `up --wait`. Run the CLI
on the Docker host as a user with Docker access. From this checkout:

```sh
python3 -m envd create trial-a --port 8069
python3 -m envd create trial-b
python3 -m envd list
python3 -m envd status trial-b
python3 -m envd credentials trial-b
```

Each sandbox gets a random, persistent hostname such as
`https://3d908af39dbd586c.greeni.leonix.cc/web/login`. The CLI reports it as
`url` and reports the loopback fallback as `local_url`. An explicit
`--port` keeps the loopback URL fixed. When omitted, Docker selects a port.

### Host routing

This branch targets Greeni. The `main` branch runs its own proxy on standalone hosts.

The generated Compose project uses this host's Coolify Traefik proxy. Odoo
joins the external `coolify` Docker network and advertises an HTTPS route
with the proxy's `letsencrypt` resolver. The proxy obtains a certificate
through HTTP-01 on port 80. Each PostgreSQL service stays on its private
sandbox network and publishes no host port.

The wildcard DNS record covers all generated names. HTTPS may take a moment
to become ready after Odoo starts. Existing standalone sandboxes receive a
hostname the next time they are started.

```sh
python3 -m envd logs trial-a
python3 -m envd stop trial-a
python3 -m envd start trial-a
python3 -m envd delete trial-a --yes
```

Stop retains data. Delete permanently removes that sandbox's PostgreSQL and
filestore volumes. A failed create retains its saved state. Run `start` to
retry or `delete --yes` to discard it. The first initialization runs with
temporary database-creation permission, which the engine revokes before
starting the Odoo web service. Cron is disabled for base trials.

The engine uses the official Odoo 20 Community image and PostgreSQL 16. It stores
local state under `${XDG_DATA_HOME:-~/.local/share}/odoo-envd` by default. Use
`--root PATH` before the command to choose a different location. Keep this
directory private. It holds credentials and maps sandboxes to their Compose
projects. Credentials returned by the CLI are the initial credentials and
will differ if the Odoo administrator changes the password.

## Phases

1. **Local sandbox lifecycle.** The CLI calls the lifecycle service in
   `envd/sandbox.py`. Compose handles container, network, and volume operations.
2. **Optional repository attachment.** A project sandbox will own PostgreSQL.
   Each selected branch will get its own Odoo service, database, and checkout.
3. **API.** A future API can call the lifecycle service used by the CLI.

This rebuild starts with a new sandbox inventory. Existing deployments from
the previous engine are outside that inventory. The new CLI does not migrate
or remove them.

## Verification

Run `python3 -m unittest discover -s tests -v`. For a live smoke test, create
two sandboxes, log in to both, and add a record and an attachment to each.
Stop and start the first sandbox and verify its records and attachments.
Delete the first sandbox and verify the second still serves its own data.
