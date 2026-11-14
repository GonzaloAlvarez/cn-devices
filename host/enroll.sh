#!/usr/bin/env bash
# host/enroll.sh — enrol a host for the cn-devices dashboard.
#
#   host/enroll.sh [-i <admin-identity>] [-k <devices-private-key>] <user@host>
#
# Run from the operator Mac with an identity that already has SSH + sudo on
# the target (default ~/.ssh/gonzalo_main_private_key.pem). Idempotent:
#   1. installs host/devices-cmd as root (Linux /usr/local/sbin,
#      macOS /usr/local/bin), mode 0755;
#   2. appends the restricted `devices@kaiser` line to the user's
#      ~/.ssh/authorized_keys unless it is already there;
#   3. with -k, self-tests: `probe` must print ok, `id` must be denied.
# Touches neither sshd_config nor sudoers. First real run: rpid11.lan.
set -euo pipefail
cd "$(dirname "$0")/.."

IDENT="${HOME}/.ssh/gonzalo_main_private_key.pem"
DEVKEY=""
while (( $# )); do
  case "$1" in
    -i) IDENT="$2"; shift 2 ;;
    -k) DEVKEY="$2"; shift 2 ;;
    -h|--help) sed -n '/^#!/d; /^[^#]/q; s/^# \{0,1\}//p' "$0"; exit 0 ;;
    -*) echo "unknown option $1" >&2; exit 2 ;;
    *) break ;;
  esac
done
[[ $# -eq 1 ]] || { echo "usage: host/enroll.sh [-i identity] [-k devices-key] <user@host>" >&2; exit 2; }
TARGET="$1"
PUB="$(cat host/devices.pub)"
[[ "$PUB" == ssh-ed25519* ]] || { echo "host/devices.pub missing or malformed" >&2; exit 1; }

SSH=(ssh -o BatchMode=yes -o ConnectTimeout=8 -o StrictHostKeyChecking=accept-new -i "$IDENT")
log() { printf '\033[1;36m[enroll]\033[0m %s\n' "$*"; }

log "$TARGET: detecting OS"
OS="$("${SSH[@]}" -n "$TARGET" 'uname -s')"
case "$OS" in
  Darwin) DEST=/usr/local/bin/devices-cmd; GRP=wheel ;;
  Linux)  DEST=/usr/local/sbin/devices-cmd; GRP=root ;;
  *) echo "unsupported OS: $OS" >&2; exit 1 ;;
esac

log "$TARGET: installing $DEST"
# plain ssh + cat rather than scp: some hosts (Proxmox) run sshd without the
# SFTP subsystem that modern scp needs.
"${SSH[@]}" "$TARGET" "cat > /tmp/devices-cmd.$$" < host/devices-cmd
"${SSH[@]}" -n "$TARGET" "sudo -n install -o root -g $GRP -m 0755 /tmp/devices-cmd.$$ $DEST && rm -f /tmp/devices-cmd.$$"

log "$TARGET: ensuring authorized_keys line"
LINE="restrict,command=\"$DEST\" $PUB"
"${SSH[@]}" -n "$TARGET" "umask 077; mkdir -p ~/.ssh; touch ~/.ssh/authorized_keys; chmod 700 ~/.ssh; chmod 600 ~/.ssh/authorized_keys; \
  if grep -qF 'devices@kaiser' ~/.ssh/authorized_keys; then echo '  already present'; \
  else printf '%s\n' '$LINE' >> ~/.ssh/authorized_keys && echo '  added'; fi"

if [[ -n "$DEVKEY" ]]; then
  log "$TARGET: self-test with $DEVKEY"
  OUT="$(ssh -n -o BatchMode=yes -o ConnectTimeout=8 -o StrictHostKeyChecking=accept-new -i "$DEVKEY" "$TARGET" probe 2>&1 || true)"
  if [[ "$OUT" == "ok" ]]; then echo "  probe → ok"; else echo "  probe FAILED: $OUT" >&2; exit 1; fi
  if ssh -n -o BatchMode=yes -o ConnectTimeout=8 -i "$DEVKEY" "$TARGET" id >/dev/null 2>&1; then
    echo "  id was NOT denied — forced command not in effect" >&2; exit 1
  else
    echo "  id → denied (good)"
  fi
else
  log "no -k given; test from kaiser with:"
  echo "  docker compose -p cn-devices exec upsnap /opt/devices/probe devices $TARGET && echo ok"
fi
log "$TARGET enrolled"
