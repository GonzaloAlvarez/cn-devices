# cn-devices

Device power and SSH-reachability dashboard for the homelab: which machines are
on, which accept SSH, wake the ones that can be woken, shut down or sleep the
ones you are done with, and add new machines from a browser.

[UpSnap](https://github.com/seriousm4x/UpSnap) (Go + PocketBase + SvelteKit) on
`kaiser.lan`, plus a thin helper layer that gives every device an SSH probe
with a dedicated key, a scheduled power action over the same key, and a
Wake-on-LAN relay through pfSense for subnets kaiser is not attached to.

| Where | URL | Certificate |
|---|---|---|
| LAN + tailnet (canonical) | `https://devices.lab.gn.al` | step-ca leaf on the LAN (cn-home traefik-lan), LE wildcard on the tailnet (traefik-lab → infra-exit → kaiser) |
| LAN alias | `https://devices.kaiser.lan` | 301 → canonical |
| Break-glass admin | `https://devices.lab.gn.al/_/` | PocketBase superuser (Kauket `kaiser.cn_devices_superuser`) |

Login is Authentik OIDC (group `infra-admins`); the PocketBase superuser is the
break-glass. UpSnap's own per-user device permissions apply after login — the
operator's user is pre-created and granted everything by `seed.py`.

## Services

| Service | Image | Network | Purpose |
|---|---|---|---|
| `upsnap` | `ghcr.io/seriousm4x/upsnap:5` | **host** (`127.0.0.1:8087`) | the app; host netns so magic packets reach kaiser's real interface; `cap_drop ALL` + `NET_RAW` (ICMP) + `DAC_OVERRIDE` (uid-1000-owned `data/`, `keys/`) |
| `exporter` | `python:3.13-alpine` (`exporter/exporter.py`, stdlib) | **host** (`0.0.0.0:8088`) | polls UpSnap every 15 s as the read-only `exporter` user and serves `/metrics` (Prometheus) + `/devices.json` (homepages) + `/healthz`; UFW admits 8088 from the docker bridges only (`setup.sh`), the LAN is denied, the tailnet gets both paths through traefik-lan on `devices.lab.gn.al` |
| `backup` | `offen/docker-volume-backup:v2.43.4` | bridge | weekly archive of `./data` → S3 `cloudnet-lab-storage/devices/` + raidnas WebDAV `/devices/`; stops `upsnap` for the seconds the tar takes (SQLite + WAL, no sqlite3 in the image) |

No Tailscale sidecar, no Consul, no watchdog: the tailnet reaches the LAN copy
through the subnet route like neptune's names do
(`cn-root-docker/tailnet/traefik/dynamic/devices.yml`, ACL `tag:infra → 10.1.1.140:443`).

## How a device is represented

UpSnap runs three per-device shell commands; the helpers in `bin/` are mounted
read-only at `/opt/devices` inside the container and the keys at `/keys`.

| UpSnap field | Value | What it does |
|---|---|---|
| `ping_cmd` | `/opt/devices/probe devices gonzalo@<address>` | **status**: exit 0 only when the host accepts the `devices` key *and* its forced command answers `probe`. Empty = plain ICMP (devices without SSH). |
| port `SSH/22` | one port record per device | TCP connect → the second dot. Shared records would share status, so `seed.py` creates one per device. |
| `shutdown_cmd` | `/opt/devices/power devices gonzalo@<address> <poweroff\|reboot\|suspend>` | asks the forced command to schedule the action in 2 s and returns; UpSnap then polls the probe until the host is gone |
| `wake_cmd` | empty (10.1.1.0/24: UpSnap's own broadcast) · `/opt/devices/wake {{ DEVICE_MAC }} <bcast>` (other subnets: pfSense `wol`) · `/opt/devices/nowake <why>` (Wi-Fi/Pi: the button explains itself) | wake |

UpSnap facts the helpers compensate for (verified in the 5.7.x source):
`ping_cmd` gets **no templating and no timeout** (hence `timeout` inside
`probe` and the hard-coded address); `wake_cmd`/`shutdown_cmd` do get
`{{ DEVICE_IP }}`/`{{ DEVICE_MAC }}`; commands run with `/bin/sh -c`; a
non-zero exit is "offline" / "failed" and stderr becomes the error toast.

### Reading the dashboard

| Status dot | SSH/22 dot | Meaning |
|---|---|---|
| green | green | on, sshd up, `devices` key accepted |
| red | green | on, sshd up, but not enrolled / key refused / host key changed |
| red | red | off or unreachable |

Probes run every 30 s around the clock (`UPSNAP_INTERVAL`; `lazy_ping` is
off so the exporter, Grafana and the homepages always see fresh state — one
SSH login per device per 30 s on the enrolled hosts). Wake/shutdown flows probe
on their own regardless.

## Inventory as code

`devices.toml` is the source of truth; `python3 seed.py devices` upserts it
(matched by name) and re-grants the operator's permissions. Adding a device in
the UI is fine for one-offs; `python3 seed.py export` prints the live set in the
same format so the file can be brought back in sync.

```toml
[[device]]
name = "elitedesk"
address = "10.1.1.98"            # IP, or an FQDN with a dot for DHCP-only hosts
mac = "c8:d9:d2:1e:74:69"        # required; 00:00:00:00:00:00 when unknown and unwakeable
netmask = "255.255.255.0"        # required; drives the broadcast for wake = "direct"
groups = ["Boxes"]
wake = "direct"                  # direct | relay (+ broadcast = "…") | none (+ wake_note)
shutdown = "poweroff"            # poweroff | reboot | suspend | none
link = "https://10.1.1.98:8006"
description = "HP EliteDesk 800 G4 SFF — Proxmox VE. WoL armed."
```

Current set: elitedesk (Proxmox, the only WoL-capable one), printer (Wi-Fi NUC),
kiosk (Pi 3B), eye (Radxa, roams), klaus and minerva (Macs, `suspend`),
lenovoyoga and lenovot14 (laptops, `suspend`, DHCP names). kaiser resolves the
`.lan` names through pfSense, so DHCP-only hosts work by FQDN; they read
"offline" while away.

## Enrolling a host

```sh
host/enroll.sh [-i ~/.ssh/<admin-key>] [-k <devices-private-key>] gonzalo@<host>
```

Run from the operator Mac with a key that already has SSH + sudo on the host.
It installs `host/devices-cmd` as root (`/usr/local/sbin` on Linux,
`/usr/local/bin` on macOS) and appends one line to the user's
`~/.ssh/authorized_keys`:

```
restrict,command="/usr/local/sbin/devices-cmd" ssh-ed25519 … devices@kaiser
```

so the `devices` key can do exactly `probe`, `poweroff`, `reboot`, `suspend`
and nothing else (`restrict` disables pty, forwarding and agent). Power actions
use `sudo -n` (amun grants `NOPASSWD`; macOS sleep uses `pmset sleepnow`
without sudo). With `-k` the script self-tests (`probe` → `ok`, `id` → denied);
without it, test from kaiser:

```sh
docker compose -p cn-devices exec upsnap /opt/devices/probe devices gonzalo@<host> && echo ok
```

amun's `remoteaccess` role manages `authorized_keys` with `state: present`
(non-exclusive), so re-provisioning keeps the line. Copying uses `ssh … cat`
rather than `scp` because Proxmox hosts run sshd without the SFTP subsystem.
First real run of any change to the dispatcher: `rpid11.lan`.

## Secrets and keys (Kauket)

| id | destination on kaiser | notes |
|---|---|---|
| `kaiser.cn_devices_env` | `~/cn-devices/.env` 0600 | TZ, pfSense relay host/user, superuser creds, OIDC client, backup targets (`.env.example` documents every key) |
| `kaiser.cn_devices_key_devices` | `~/cn-devices/keys/devices` 0600 | **retained** — the probe/power key; rotating it means re-enrolling every host |
| `kaiser.cn_devices_key_pfsense` | `~/cn-devices/keys/pfsense` 0600 | **retained** — authorized on the pfSense user `devices` |
| `kaiser.cn_devices_oidc` | record | Authentik client printed by `cn-authentik/setup-devices-oidc.sh` |
| `kaiser.cn_devices_superuser` | record | PocketBase break-glass |

`host/devices.pub` (public half) is tracked. The operator's main key never
lands on kaiser.

**pfSense relay.** A dedicated pfSense user `devices` (privilege *User - System:
Shell account access* only, shell `/bin/tcsh`, the relay pubkey in its
authorized keys) runs `/usr/local/bin/wol -i <broadcast> <mac>`; pfSense has an
interface on every VLAN so one relay covers them all. It was created through a
PHP script over admin SSH (`config.inc` + `auth.inc`: `local_user_set_password`
with the 2.8 `{item, idx}` wrapper, `write_config`, `local_user_set`). Test:
`ssh -i keys/pfsense devices@10.1.0.1 /usr/local/bin/wol -i 10.1.1.255 <mac>`.

## Setup and operations

First install on kaiser:

```sh
git clone https://github.com/GonzaloAlvarez/cn-devices.git ~/cn-devices && cd ~/cn-devices
kauket get kaiser.cn_devices_env
kauket get kaiser.cn_devices_key_devices
kauket get kaiser.cn_devices_key_pfsense
./setup.sh          # compose up, superuser, seed.py configure + devices
amun docker         # boot unit docker-compose@cn-devices + 5-min reconcile cron
```

`setup.sh` is idempotent: it opens UFW 8088 to the docker bridges, waits for `/api/health`, logs in as the superuser
or creates it from PocketBase's installer token (UpSnap's binary ignores the
`superuser upsert` subcommand and just serves), then runs `seed.py configure`
(30 s interval, lazy ping off, `setup_completed`, the Authentik `oidc` provider,
the operator user + permissions, the read-only `exporter` user) and `seed.py devices`.

| Task | Command |
|---|---|
| Deploy a change | `cd ~/cn-devices && git pull && docker compose up -d --force-recreate --no-deps upsnap` (bind mounts: `bin/` is a directory mount, edits are live) |
| Add / change devices | edit `devices.toml`, push, `git pull && python3 seed.py devices` |
| Upgrade UpSnap | read the release notes, `docker compose pull && docker compose up -d`, re-run `python3 seed.py devices` if the schema changed. Watchtower is monitor-only here (UpSnap migrates on boot). |
| Wake / shut down from a script | `GET /api/upsnap/wake/<id>`, `GET /api/upsnap/shutdown/<id>` with a user JWT (`POST /api/collections/users/auth-with-password`, or superuser impersonation) |
| Backup now | `docker exec cn-devices-backup-1 /usr/bin/backup` → object in S3 `devices/`, file on `http://raidnas.lan:5005/cloudlabbackup/devices/` |
| Restore | `docker compose stop upsnap`, untar the archive's `pb_data/` over `./data/`, `docker compose start upsnap` (devices are also re-creatable from `devices.toml`) |
| Resiliency | `systemctl restart docker-compose@cn-devices`; `~/dev/amun-docker/verify --host kaiser.lan` |

## Observability

UpSnap itself exposes no metrics; the `exporter` service derives them from its
API (read-only user, read permission on every device, nothing else):

| Metric | Labels | Meaning |
|---|---|---|
| `upsnap_device_online` | `device`, `group` | 1 = UpSnap status `online` (SSH probe, or ICMP for probe-less devices) |
| `upsnap_device_pending` | `device`, `group` | 1 while a wake/shutdown is in flight |
| `upsnap_device_port_open` | `device`, `group`, `port`, `number` | TCP port dot (`SSH`/22 everywhere) |
| `upsnap_device_info` | `device`, `group`, `probe`, `wake`, `shutdown`, `address` | capabilities derived from the device's commands |
| `upsnap_devices_total`, `upsnap_devices_online`, `upsnap_exporter_up`, `upsnap_exporter_last_poll_timestamp_seconds` | — | totals and exporter health |

Scraped by kaiser's Prometheus (`devices-exporter` → `host.docker.internal:8088`)
and by the VPS Prometheus (`https://devices.lab.gn.al/metrics` through
traefik-lab). The **Devices** dashboard (uid `devices`, same JSON in
`cn-observability/config/grafana/provisioning/dashboards/` and
`cn-root-docker/tailnet/grafana/provisioning/dashboards/`) shows online count,
a now-table (power, ssh, probe/wake/shutdown capabilities), 7-day uptime and
24 h state timelines for power and SSH on `grafana.kaiser.lan` and
`grafana.lab.gn.al`. `/devices.json` feeds the **Devices** widget on
`home.kaiser.lan` (dynacat, via `host.docker.internal`) and `home.lab.gn.al`
(Glance, via traefik-lab). Health probes: kaiser `blackbox-https` and VPS
`blackbox-tailnet-http` on `/api/health` (`BlackboxProbeFailed`); a dead
exporter trips the generic `PrometheusTargetDown`. Logs: kaiser's host-wide
promtail (`{project="cn-devices"}`).

## Troubleshooting

- **A device is red but its SSH dot is green.** The probe is refused: not
  enrolled (`host/enroll.sh`), or the host key changed (a rebuilt host) —
  remove its line from `data/known_hosts` (`StrictHostKeyChecking=accept-new`
  records the first key it sees). Test by hand with the exec command above.
- **A Wi-Fi Mac is green but its SSH dot is red.** UpSnap's port check is a
  TCP connect with a fixed 500 ms budget; a sleeping Wi-Fi radio often misses
  it while the 4 s SSH probe succeeds (minerva does this). Trust the status dot.
- **Wake says "cannot be woken over the network".** That is `bin/nowake` for
  Wi-Fi laptops and Raspberry Pis; it is deliberate.
- **Wake times out on a wired host.** WoL must be armed on the target (BIOS +
  `ethtool -s <nic> wol g` persisted, see `cn-infra/README.md`); for hosts off
  kaiser's subnet set `wake = "relay"` and check the pfSense relay test above.
- **Shutdown returns "sudo -n denied".** The host lacks passwordless sudo; use
  `shutdown = "suspend"` on macOS (`pmset sleepnow` needs none).
- **UpSnap listens on 0.0.0.0:8090.** `UPSNAP_HTTP_LISTEN` is read by the app
  (the image ENV defaults it) and the exec-form CMD never expands the
  placeholder; the compose file sets both the env and the explicit `command`.
- **`unable to open database file (14)`.** Root without `DAC_OVERRIDE` cannot
  write the uid-1000-owned `data/`; the compose file adds that capability.
- **All devices show the same SSH dot.** They share one port record; run
  `python3 seed.py devices` (one record per device).
- **Exporter red / dashboard empty.** `docker compose logs exporter`; the
  common cause is the `exporter` user's password drifting from `.env` — `python3
  seed.py configure` re-syncs it. From the Mac: `curl -s https://devices.lab.gn.al/metrics | head`.
- **Homepage widget says "exporter answered 503".** The exporter is up but its
  last UpSnap poll failed (see above); `/devices.json` returns the error text.
- **Login page has no Authentik button.** `python3 seed.py configure` writes
  the provider from `.env`; the redirect URI in Authentik is strictly
  `https://devices.lab.gn.al/api/oauth2-redirect`, so only the canonical origin
  can log in (the `.kaiser.lan` alias redirects).

## Acceptance (2026-11-14)

Enrolled with self-test: rpid11 (testbed), elitedesk, printer, kiosk, klaus,
minerva. LAN `https://devices.lab.gn.al/api/health` 200 with a step-ca leaf,
`devices.kaiser.lan` 301; tailnet 200 with the LE wildcard; both Prometheus
probes 1; public IP → 404. Power cycle on elitedesk through the API:
shutdown → offline in 3 s, wake → online in 27 s. pfSense relay: `wol` as
`devices` exit 0. Backup: first archive in S3 and on the WebDAV share.

## License

MIT for the files in this repository; UpSnap is MIT by its authors.
