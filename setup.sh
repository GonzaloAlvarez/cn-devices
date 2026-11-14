#!/usr/bin/env bash
# cn-devices — idempotent bring-up on kaiser.
#
#   kauket get kaiser.cn_devices_env            # installs .env (0600)
#   kauket get kaiser.cn_devices_key_devices    # installs keys/devices (0600)
#   kauket get kaiser.cn_devices_key_pfsense    # installs keys/pfsense (0600)
#   ./setup.sh
#
# Steps: check .env + keys → create data/ → docker compose up -d → wait for
# /api/health → upsert the PocketBase superuser from .env → seed.py configure
# (lazy ping, Authentik OIDC provider, operator user + permissions) →
# seed.py devices (devices.toml). Re-running is safe; nothing is regenerated.
set -euo pipefail
cd "$(dirname "$0")"

case "${1:-}" in
  -h|--help) sed -n '/^#!/d; /^[^#]/q; s/^# \{0,1\}//p' "$0"; exit 0 ;;
  "") ;;
  *) echo "unknown argument: $1 (try --help)" >&2; exit 2 ;;
esac

log() { printf '\n\033[1;36m[setup]\033[0m %s\n' "$*"; }
die() { printf '\033[1;31m[FAIL]\033[0m %s\n' "$*" >&2; exit 1; }
env_get() { grep -E "^${1}=" .env 2>/dev/null | head -1 | cut -d= -f2- || true; }

[[ -f .env ]] || die ".env not found. Run: kauket get kaiser.cn_devices_env"
for k in devices pfsense; do
  [[ -f keys/$k ]] || die "keys/$k missing. Run: kauket get kaiser.cn_devices_key_$k"
done
chmod 700 keys
mkdir -p data

log "docker compose up -d --remove-orphans"
docker compose up -d --remove-orphans

log "waiting for UpSnap /api/health"
for _ in $(seq 1 30); do
  curl -fsS http://127.0.0.1:8087/api/health >/dev/null 2>&1 && break
  sleep 2
done
curl -fsS http://127.0.0.1:8087/api/health >/dev/null || die "UpSnap did not become healthy"

SU_EMAIL="$(env_get UPSNAP_SUPERUSER_EMAIL)"
SU_PASS="$(env_get UPSNAP_SUPERUSER_PASSWORD)"
[[ -n "$SU_EMAIL" && -n "$SU_PASS" ]] || die "UPSNAP_SUPERUSER_EMAIL / UPSNAP_SUPERUSER_PASSWORD missing in .env"
API=http://127.0.0.1:8087/api
su_json() { python3 -c 'import json,sys; print(json.dumps({sys.argv[1]: sys.argv[2], "password": sys.argv[3], "passwordConfirm": sys.argv[3]}))' "$1" "$SU_EMAIL" "$SU_PASS"; }
su_login() { curl -s -o /dev/null -w "%{http_code}" -X POST "$API/collections/_superusers/auth-with-password" -H "Content-Type: application/json" -d "$(su_json identity)"; }
log "ensuring PocketBase superuser ${SU_EMAIL}"
if [[ "$(su_login)" == "200" ]]; then
  echo "  superuser login ok"
else
  # UpSnap's binary ignores PocketBase's `superuser upsert` subcommand (it just
  # serves), so use the installer token PocketBase prints on a fresh data dir.
  TOKEN="$(docker compose logs upsnap 2>&1 | grep -o 'pbinstall/[A-Za-z0-9._-]*' | tail -1 | cut -d/ -f2 || true)"
  [[ -n "$TOKEN" ]] || die "superuser login failed and no installer token in the log; create it at https://devices.lab.gn.al/_/ or fix UPSNAP_SUPERUSER_* in .env"
  CODE="$(curl -s -o /dev/null -w "%{http_code}" -X POST "$API/collections/_superusers/records" -H "Authorization: $TOKEN" -H "Content-Type: application/json" -d "$(su_json email)")"
  [[ "$CODE" == "200" ]] || die "superuser creation returned HTTP $CODE"
  [[ "$(su_login)" == "200" ]] || die "superuser created but login still fails"
  echo "  superuser created"
fi

log "seed.py configure"
python3 seed.py configure
log "seed.py devices"
python3 seed.py devices

cat <<BANNER


===============================================================
cn-devices is up.

  LAN     :  https://devices.lab.gn.al       (step-ca leaf via cn-home traefik-lan)
             https://devices.kaiser.lan      (301 → canonical)
  Tailnet :  https://devices.lab.gn.al       (LE wildcard via traefik-lab → infra-exit)
  Admin   :  https://devices.lab.gn.al/_/    (PocketBase superuser, break-glass)

  Login with Authentik (group infra-admins). Inventory: devices.toml →
  python3 seed.py devices. Enrol a new host: host/enroll.sh <user@host>.
===============================================================
BANNER
