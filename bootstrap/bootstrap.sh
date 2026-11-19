#!/usr/bin/env bash
# odoo-envd server bootstrap — run once (and again to refresh) on the deploy
# server. Idempotent for the parts that matter (dirs, units, images).
#
# Usage:
#   sudo ./bootstrap/bootstrap.sh \
#        --modules-url git@github.com:acme/odoo-custom.git \
#        [--enterprise-token ghp_…] [--root /srv/odoo]
#
# Assumptions (see docs/ops-runbook.md):
#   - Debian/Ubuntu with git, curl, docker + compose plugin, uv
#   - DNS: *.odoo.example.com → this host; api.odoo.example.com → this host

set -euo pipefail

MODULES_URL=""
ENTERPRISE_TOKEN=""
ROOT="/srv/odoo"
ENGINE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

while [ $# -gt 0 ]; do
    case "$1" in
        --modules-url) MODULES_URL="$2"; shift 2 ;;
        --enterprise-token) ENTERPRISE_TOKEN="$2"; shift 2 ;;
        --root) ROOT="$2"; shift 2 ;;
        *) echo "unknown option: $1" >&2; exit 2 ;;
    esac
done

echo "==> directories under ${ROOT}"
install -d -m 755 "${ROOT}"/{envs,data,backups,enterprise,envd,envd/logs,infra,locks,images}

echo "==> infra stack (traefik + postgres)"
install -m 644 "${ENGINE_DIR}/bootstrap/docker-compose.infra.yml" "${ROOT}/infra/docker-compose.yml"
docker compose --project-directory "${ROOT}/infra" up -d

echo "==> pinned enterprise clone"
REF="$(tr -d '[:space:]' < "${ENGINE_DIR}/bootstrap/enterprise.ref")"
if [ -d "${ROOT}/enterprise/.git" ]; then
    git -C "${ROOT}/enterprise" fetch --tags origin
    git -C "${ROOT}/enterprise" checkout --quiet "${REF}"
    git -C "${ROOT}/enterprise" reset --hard origin/"${REF}" 2>/dev/null || true
elif [ -n "${ENTERPRISE_TOKEN}" ]; then
    git clone --depth 1 --branch "${REF}" \
        "https://x-access-token:${ENTERPRISE_TOKEN}@github.com/odoo/enterprise.git" \
        "${ROOT}/enterprise"
else
    echo "    NOTE: no --enterprise-token and no existing clone." >&2
    echo "    Re-run with a token, or clone odoo/enterprise to ${ROOT}/enterprise yourself." >&2
fi

echo "==> modules mirror"
if [ -n "${MODULES_URL}" ]; then
    if [ -d "${ROOT}/repo.git" ]; then
        git -C "${ROOT}/repo.git" remote set-url origin "${MODULES_URL}"
    else
        git clone --mirror "${MODULES_URL}" "${ROOT}/repo.git"
    fi
else
    echo "    NOTE: no --modules-url; clone your custom-modules repo as a --mirror to ${ROOT}/repo.git" >&2
fi

echo "==> base image (odoo:20 + build tools)"
docker build --pull -t envd-base:20 -f "${ENGINE_DIR}/bootstrap/docker/Dockerfile.base" \
    "${ENGINE_DIR}/bootstrap/docker"

echo "==> engine venv + service"
if command -v uv >/dev/null; then
    (cd "${ENGINE_DIR}" && uv venv && uv pip install -e . --group dev)
else
    (cd "${ENGINE_DIR}" && python3 -m venv .venv && .venv/bin/pip install -e .)
fi

install -d -m 755 /etc/odoo-envd
if [ ! -f /etc/odoo-envd/envd.toml ]; then
    install -m 600 "${ENGINE_DIR}/envd.example.toml" /etc/odoo-envd/envd.toml
    echo "    EDIT /etc/odoo-envd/envd.toml now (domain, forge secret, repos)." >&2
fi

install -m 644 "${ENGINE_DIR}/bootstrap/units/odoo-envd.service" /etc/systemd/system/
install -m 644 "${ENGINE_DIR}"/bootstrap/units/envd-*.service "${ENGINE_DIR}"/bootstrap/units/envd-*.timer \
    /etc/systemd/system/ 2>/dev/null || true
systemctl daemon-reload
systemctl enable --now odoo-envd.service
systemctl enable --now envd-backup.timer envd-prune.timer envd-weekly.timer 2>/dev/null || true

echo "==> smoke checks"
sleep 2
systemctl is-active odoo-envd.service
curl -fsS "http://127.0.0.1:$(grep -oP 'listen_port\s*=\s*\K[0-9]+' /etc/odoo-envd/envd.toml || echo 8100)/api/system/healthz" -o /dev/null \
    && echo "healthz OK" || echo "healthz FAILED — check: journalctl -u odoo-envd -e"

echo
echo "Next steps:"
echo "  1. edit /etc/odoo-envd/envd.toml (domain, forge type/secret, repos, base image)"
echo "  2. systemctl restart odoo-envd"
echo "  3. create a GUI token:     ${ENGINE_DIR}/.venv/bin/envd token create --name gui --scopes admin"
echo "  4. register the forge webhook → http://<host>:8100/api/hook (HMAC secret from config)"
echo "  5. push a branch and watch: journalctl -u odoo-envd -f"
