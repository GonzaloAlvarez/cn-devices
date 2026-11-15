#!/usr/bin/env python3
"""seed.py — configure UpSnap (PocketBase) and upsert the device inventory.

    python3 seed.py configure [--dry-run]   lazy ping, Authentik OIDC provider,
                                            operator user + full permissions
    python3 seed.py devices   [--dry-run]   upsert ports / groups / devices from
                                            devices.toml, then re-grant permissions
    python3 seed.py export                  print the live devices as TOML

Reads .env (UPSNAP_SUPERUSER_*, OIDC_*, OPERATOR_EMAIL) and talks to
UPSNAP_URL (default http://127.0.0.1:8087) as the PocketBase superuser.
Standard library only (python3 >= 3.11 for tomllib).
"""
import argparse
import json
import secrets
import sys
import tomllib
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
BIN = "/opt/devices"


# ── plumbing ────────────────────────────────────────────────────────────────

def load_env(path):
    env = {}
    for line in Path(path).read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        env[k.strip()] = v.strip()
    return env


class PB:
    def __init__(self, url, token=None, dry=False):
        self.url = url.rstrip("/")
        self.token = token
        self.dry = dry

    def call(self, method, path, body=None, params=None, auth=True):
        url = self.url + path
        if params:
            url += "?" + urllib.parse.urlencode(params)
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(url, data=data, method=method)
        req.add_header("Content-Type", "application/json")
        if auth and self.token:
            req.add_header("Authorization", "Bearer " + self.token)
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                raw = r.read()
                return json.loads(raw) if raw else {}
        except urllib.error.HTTPError as e:
            detail = e.read().decode(errors="replace")
            sys.exit(f"{method} {path} → HTTP {e.code}: {detail}")

    def write(self, method, path, body, label):
        if self.dry:
            print(f"  [dry-run] {method} {path} {json.dumps(body)[:200]}")
            return {}
        res = self.call(method, path, body) or {}
        print(f"  {label}")
        return res

    def auth(self, identity, password):
        res = self.call("POST", "/api/collections/_superusers/auth-with-password",
                        {"identity": identity, "password": password}, auth=False)
        self.token = res["token"]

    def first(self, collection, flt):
        res = self.call("GET", f"/api/collections/{collection}/records",
                        params={"filter": flt, "perPage": 1})
        items = res.get("items", [])
        return items[0] if items else None

    def all(self, collection):
        res = self.call("GET", f"/api/collections/{collection}/records",
                        params={"perPage": 500})
        return res.get("items", [])

    def collection(self, name):
        return self.call("GET", f"/api/collections/{name}")


def q(s):
    return "'" + str(s).replace("'", "\\'") + "'"


# ── configure ───────────────────────────────────────────────────────────────

def configure(pb, env):
    print("settings: lazy_ping")
    settings = pb.all("settings_private")
    if not settings:
        sys.exit("settings_private is empty — has UpSnap finished its first start?")
    s = settings[0]
    lazy = env.get("UPSNAP_LAZY_PING", "false").strip().lower() in ("1", "true", "yes", "on")
    if s.get("lazy_ping") is lazy:
        print(f"  lazy_ping already {lazy}")
    else:
        pb.write("PATCH", f"/api/collections/settings_private/records/{s['id']}",
                 {"lazy_ping": lazy}, f"lazy_ping → {lazy}")

    print("settings: setup_completed + interval")
    pub = pb.all("settings_public")
    if pub and not pub[0].get("setup_completed"):
        pb.write("PATCH", f"/api/collections/settings_public/records/{pub[0]['id']}",
                 {"setup_completed": True}, "setup_completed → true (skip the first-run wizard)")
    interval = env.get("UPSNAP_INTERVAL", "*/30 * * * * *")
    if s.get("interval") != interval:
        pb.write("PATCH", f"/api/collections/settings_private/records/{s['id']}",
                 {"interval": interval}, f"interval → {interval}")

    print("users collection: Authentik OIDC provider")
    cid, csec = env.get("OIDC_CLIENT_ID", ""), env.get("OIDC_CLIENT_SECRET", "")
    base = env.get("OIDC_BASE_URL", "https://auth.lab.gn.al/application/o").rstrip("/")
    if not cid or not csec:
        print("  OIDC_CLIENT_ID/SECRET not set — skipping provider")
    else:
        users = pb.collection("users")
        oauth2 = dict(users.get("oauth2") or {})
        provider = {
            "name": "oidc",
            "displayName": "Authentik",
            "clientId": cid,
            "clientSecret": csec,
            "authURL": base + "/authorize/",
            "tokenURL": base + "/token/",
            "userInfoURL": base + "/userinfo/",
            "pkce": None,
        }
        others = [p for p in oauth2.get("providers", []) if p.get("name") != "oidc"]
        oauth2["providers"] = others + [provider]
        oauth2["enabled"] = True
        pb.write("PATCH", "/api/collections/users", {"oauth2": oauth2},
                 "oauth2 provider oidc (Authentik) set")

    print("operator user + permissions")
    user = ensure_operator(pb, env)
    if user:
        grant_all(pb, user)

    print("exporter user (read-only)")
    exp = ensure_exporter(pb, env)
    if exp:
        grant_read(pb, exp)


def ensure_operator(pb, env):
    email = env.get("OPERATOR_EMAIL", "")
    if not email:
        print("  OPERATOR_EMAIL not set — skipping")
        return None
    user = pb.first("users", f"email={q(email)}")
    if user:
        print(f"  user {email} exists (id={user['id']})")
        if not user.get("verified"):
            pb.write("PATCH", f"/api/collections/users/records/{user['id']}",
                     {"verified": True}, "verified → true (needed for OIDC e-mail linking)")
        return user
    fields = {f["name"] for f in pb.collection("users").get("fields", [])}
    pw = secrets.token_urlsafe(24)
    body = {"email": email, "password": pw, "passwordConfirm": pw,
            "verified": True, "emailVisibility": False}
    if "username" in fields:
        body["username"] = email.split("@")[0]
    res = pb.write("POST", "/api/collections/users/records", body,
                   f"user {email} created (random password; logs in through Authentik)")
    return res or {"id": "dry-run"}


def ensure_exporter(pb, env):
    email, pw = env.get("UPSNAP_EXPORTER_EMAIL", ""), env.get("UPSNAP_EXPORTER_PASSWORD", "")
    if not email or not pw:
        print("  UPSNAP_EXPORTER_EMAIL/PASSWORD not set — skipping")
        return None
    user = pb.first("users", f"email={q(email)}")
    fields = {f["name"] for f in pb.collection("users").get("fields", [])}
    if user:
        # keep the stored password in sync with .env (rotation = change .env, re-run)
        pb.write("PATCH", f"/api/collections/users/records/{user['id']}",
                 {"password": pw, "passwordConfirm": pw, "verified": True}, f"user {email} password synced")
        return user
    body = {"email": email, "password": pw, "passwordConfirm": pw, "verified": True, "emailVisibility": False}
    if "username" in fields:
        body["username"] = "exporter"
    res = pb.write("POST", "/api/collections/users/records", body, f"user {email} created")
    return res or {"id": "dry-run"}


def grant_read(pb, user):
    """Exporter: read every device, no create/update/delete/power."""
    coll = pb.collection("permissions")
    dev_coll_id = pb.collection("devices")["id"]
    device_ids = [d["id"] for d in pb.all("devices")]
    body = {"user": user["id"]}
    for f in coll.get("fields", []):
        if f["type"] == "relation" and f.get("collectionId") == dev_coll_id:
            body[f["name"]] = device_ids if f["name"] == "read" else []
        elif f["type"] == "bool":
            body[f["name"]] = False
    existing = pb.first("permissions", f"user={q(user['id'])}")
    if existing:
        pb.write("PATCH", f"/api/collections/permissions/records/{existing['id']}", body,
                 f"read-only permissions updated for {user.get('email', user['id'])} ({len(device_ids)} devices)")
    else:
        pb.write("POST", "/api/collections/permissions/records", body,
                 f"read-only permissions created for {user.get('email', user['id'])} ({len(device_ids)} devices)")


def grant_all(pb, user):
    """Give the operator every right the permissions collection models.

    UpSnap's `permissions` collection has one record per user with relation
    fields pointing at devices (read/update/delete/power…) and booleans
    (create). Introspect instead of hard-coding field names so upstream
    additions are covered automatically.
    """
    coll = pb.collection("permissions")
    dev_coll_id = pb.collection("devices")["id"]
    device_ids = [d["id"] for d in pb.all("devices")]
    body = {}
    for f in coll.get("fields", []):
        if f["type"] == "relation" and f.get("collectionId") == dev_coll_id:
            body[f["name"]] = device_ids
        elif f["type"] == "bool":
            body[f["name"]] = True
    body["user"] = user["id"]
    existing = pb.first("permissions", f"user={q(user['id'])}")
    if existing:
        pb.write("PATCH", f"/api/collections/permissions/records/{existing['id']}", body,
                 f"permissions updated for {user.get('email', user['id'])} ({len(device_ids)} devices)")
    else:
        pb.write("POST", "/api/collections/permissions/records", body,
                 f"permissions created for {user.get('email', user['id'])} ({len(device_ids)} devices)")


# ── devices ─────────────────────────────────────────────────────────────────

def render(dev, defaults):
    d = {**defaults, **dev}
    user, key, addr = d["user"], d["key"], d["address"]
    rec = {
        "name": d["name"],
        "ip": addr,
        "mac": d.get("mac", "00:00:00:00:00:00"),
        "netmask": d.get("netmask", "255.255.255.0"),  # required by UpSnap even for FQDN devices
        "description": d.get("description", ""),
        "link": d.get("link", ""),
        "link_open": "new_tab" if d.get("link") else "",
        "wake_timeout": int(d.get("wake_timeout", 180)),
        "shutdown_timeout": int(d.get("shutdown_timeout", 120)),
        "shutdown_confirm": bool(d.get("shutdown_confirm", True)),
        "wake_confirm": bool(d.get("wake_confirm", False)),
        "track_ip": False,
    }
    rec["ping_cmd"] = f"{BIN}/probe {key} {user}@{addr}" if d.get("probe", True) else ""
    wake = d.get("wake", "none")
    if wake == "direct":
        rec["wake_cmd"] = ""
    elif wake == "relay":
        rec["wake_cmd"] = f"{BIN}/wake {{{{ DEVICE_MAC }}}} {d['broadcast']}"
    else:
        rec["wake_cmd"] = f"{BIN}/nowake {d.get('wake_note', 'no Wake-on-LAN')}"
    action = d.get("shutdown", "none")
    rec["shutdown_cmd"] = f"{BIN}/power {key} {user}@{addr} {action}" if action != "none" else ""
    return rec, d.get("ports", []), d.get("groups", [])


def ensure_named(pb, collection, body, flt, label):
    rec = pb.first(collection, flt)
    if rec:
        changed = {k: v for k, v in body.items() if rec.get(k) != v}
        if changed:
            pb.write("PATCH", f"/api/collections/{collection}/records/{rec['id']}", changed,
                     f"{label} updated ({', '.join(changed)})")
        else:
            print(f"  {label} unchanged")
        return rec["id"]
    res = pb.write("POST", f"/api/collections/{collection}/records", body, f"{label} created")
    return res.get("id", "dry-run")


def ensure_device_ports(pb, device, wanted, catalogue, refcount):
    """UpSnap keeps port status per *port record*, so every device needs its
    own records (sharing one SSH/22 record across devices makes the dot
    meaningless). Reuse a record only when this device is its sole owner,
    otherwise create one; orphans are swept afterwards."""
    existing = device.get("expand", {}).get("ports", []) if device else []
    ids = []
    for key in wanted:
        want = catalogue[key]
        match = next((p for p in existing
                      if p.get("name") == want["name"] and int(p.get("number", -1)) == int(want["number"])
                      and refcount.get(p["id"], 0) == 1), None)
        if match:
            ids.append(match["id"])
        else:
            res = pb.write("POST", "/api/collections/ports/records",
                           {"name": want["name"], "number": int(want["number"])},
                           f"port {want['name']}/{want['number']} created for {device.get('name', 'new device')}")
            ids.append(res.get("id", "dry-run"))
    return ids


def sweep_orphan_ports(pb):
    used = {pid for d in pb.all("devices") for pid in d.get("ports", [])}
    for prt in pb.all("ports"):
        if prt["id"] not in used:
            pb.write("DELETE", f"/api/collections/ports/records/{prt['id']}", None,
                     f"orphan port {prt['name']}/{prt['number']} deleted")


def devices(pb, env, inv):
    defaults = inv.get("defaults", {})
    catalogue = inv.get("ports", {})
    print("groups")
    group_ids = {}
    for dev in inv.get("device", []):
        for g in dev.get("groups", defaults.get("groups", [])):
            if g not in group_ids:
                group_ids[g] = ensure_named(pb, "device_groups", {"name": g}, f"name={q(g)}", f"group {g}")
    email = env.get("OPERATOR_EMAIL", "")
    user = pb.first("users", f"email={q(email)}") if email else None
    print("devices")
    refcount = {}
    for d in pb.all("devices"):
        for pid in d.get("ports", []):
            refcount[pid] = refcount.get(pid, 0) + 1
    for dev in inv.get("device", []):
        rec, ports, groups = render(dev, defaults)
        current = pb.call("GET", "/api/collections/devices/records",
                          params={"filter": f"name={q(rec['name'])}", "perPage": 1, "expand": "ports"}).get("items", [])
        current = current[0] if current else None
        rec["ports"] = ensure_device_ports(pb, current or {"name": rec["name"]}, ports, catalogue, refcount)
        rec["groups"] = [group_ids[g] for g in groups]
        if user:
            rec["created_by"] = user["id"]
        ensure_named(pb, "devices", rec, f"name={q(rec['name'])}", f"device {rec['name']}")
    if not pb.dry:
        sweep_orphan_ports(pb)
    print("operator + exporter permissions")
    if user and not pb.dry:
        grant_all(pb, user)
    else:
        print("  skipped (no operator user yet or dry-run)")
    exp_email = env.get("UPSNAP_EXPORTER_EMAIL", "")
    exp = pb.first("users", f"email={q(exp_email)}") if exp_email else None
    if exp and not pb.dry:
        grant_read(pb, exp)


def export(pb):
    print("# live UpSnap devices (seed.py export)")
    for d in pb.all("devices"):
        print("\n[[device]]")
        for k in ("name", "ip", "mac", "netmask", "description", "link", "ping_cmd", "wake_cmd", "shutdown_cmd"):
            v = d.get(k, "")
            if v != "":
                print(f'{k} = {json.dumps(v)}')


# ── main ────────────────────────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("command", choices=["configure", "devices", "export"])
    ap.add_argument("--env", default=str(HERE / ".env"))
    ap.add_argument("--inventory", default=str(HERE / "devices.toml"))
    ap.add_argument("--url", default=None)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    env = load_env(args.env) if Path(args.env).exists() else {}
    url = args.url or env.get("UPSNAP_URL", "http://127.0.0.1:8087")
    pb = PB(url, dry=args.dry_run)
    identity, password = env.get("UPSNAP_SUPERUSER_EMAIL"), env.get("UPSNAP_SUPERUSER_PASSWORD")
    if not identity or not password:
        sys.exit("UPSNAP_SUPERUSER_EMAIL / UPSNAP_SUPERUSER_PASSWORD missing in .env")
    pb.auth(identity, password)

    if args.command == "configure":
        configure(pb, env)
    elif args.command == "devices":
        with open(args.inventory, "rb") as fh:
            inv = tomllib.load(fh)
        devices(pb, env, inv)
    else:
        export(pb)


if __name__ == "__main__":
    main()
