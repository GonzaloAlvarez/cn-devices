#!/usr/bin/env python3
"""cn-devices exporter — UpSnap device state as Prometheus metrics and JSON.

Polls UpSnap's PocketBase API as a read-only user and serves:
    /metrics       Prometheus text format
    /devices.json  {"updated": <unix>, "devices": [{name, status, ssh(open|closed|""), group, link, wake, shutdown}]}
    /healthz       200 while the last poll succeeded

Environment: UPSNAP_URL (default http://127.0.0.1:8087), EXPORTER_LISTEN
(default 127.0.0.1:8088), UPSNAP_EXPORTER_EMAIL / UPSNAP_EXPORTER_PASSWORD,
EXPORTER_POLL_SECONDS (default 15). Standard library only.
"""
import json
import os
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

UPSNAP = os.environ.get("UPSNAP_URL", "http://127.0.0.1:8087").rstrip("/")
LISTEN = os.environ.get("EXPORTER_LISTEN", "127.0.0.1:8088")
EMAIL = os.environ.get("UPSNAP_EXPORTER_EMAIL", "")
PASSWORD = os.environ.get("UPSNAP_EXPORTER_PASSWORD", "")
POLL = int(os.environ.get("EXPORTER_POLL_SECONDS", "15"))

state = {"ok": 0, "updated": 0.0, "metrics": "", "json": b"{}", "error": ""}
lock = threading.Lock()
token = {"value": None}


def api(method, path, body=None, params=None, auth=True):
    url = UPSNAP + path + ("?" + urllib.parse.urlencode(params) if params else "")
    req = urllib.request.Request(url, data=json.dumps(body).encode() if body is not None else None, method=method)
    req.add_header("Content-Type", "application/json")
    if auth and token["value"]:
        req.add_header("Authorization", "Bearer " + token["value"])
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.load(r)


def login():
    res = api("POST", "/api/collections/users/auth-with-password",
              {"identity": EMAIL, "password": PASSWORD}, auth=False)
    token["value"] = res["token"]


def esc(v):
    return str(v).replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def labels(d):
    return ",".join(f'{k}="{esc(v)}"' for k, v in d.items())


def capability(dev):
    wake = dev.get("wake_cmd", "")
    if not wake:
        wake_cap = "direct"
    elif "/wake " in wake:
        wake_cap = "relay"
    else:
        wake_cap = "none"
    sd = dev.get("shutdown_cmd", "")
    action = sd.split()[-1] if sd.startswith("/opt/devices/power ") else ("custom" if sd else "none")
    probe = "ssh" if dev.get("ping_cmd") else "icmp"
    return wake_cap, action, probe


def poll_once():
    if not token["value"]:
        login()
    try:
        res = api("GET", "/api/collections/devices/records",
                  params={"perPage": 200, "expand": "ports,groups", "sort": "name"})
    except urllib.error.HTTPError as e:
        if e.code in (401, 403):
            login()
            res = api("GET", "/api/collections/devices/records",
                      params={"perPage": 200, "expand": "ports,groups", "sort": "name"})
        else:
            raise
    devices = res.get("items", [])
    now = time.time()
    out = []
    out.append("# HELP upsnap_device_online 1 when UpSnap's status for the device is online (SSH probe or ICMP).")
    out.append("# TYPE upsnap_device_online gauge")
    out.append("# HELP upsnap_device_pending 1 while a wake or shutdown is in progress.")
    out.append("# TYPE upsnap_device_pending gauge")
    out.append("# HELP upsnap_device_port_open 1 when the device's TCP port check succeeded.")
    out.append("# TYPE upsnap_device_port_open gauge")
    out.append("# HELP upsnap_device_info Static facts about the device (capabilities derived from its commands).")
    out.append("# TYPE upsnap_device_info gauge")
    jdevs = []
    online = 0
    for d in devices:
        groups = d.get("expand", {}).get("groups", []) or []
        group = groups[0]["name"] if groups else ""
        base = {"device": d["name"], "group": group}
        st = d.get("status") or "unknown"
        is_online = 1 if st == "online" else 0
        online += is_online
        out.append(f"upsnap_device_online{{{labels(base)}}} {is_online}")
        out.append(f"upsnap_device_pending{{{labels(base)}}} {1 if st == 'pending' else 0}")
        ssh = None
        for p in d.get("expand", {}).get("ports", []) or []:
            pl = dict(base, port=p.get("name", str(p.get("number"))), number=str(p.get("number")))
            val = 1 if p.get("status") else 0
            out.append(f"upsnap_device_port_open{{{labels(pl)}}} {val}")
            if int(p.get("number", 0)) == 22:
                ssh = bool(val)
        wake_cap, action, probe = capability(d)
        il = dict(base, wake=wake_cap, shutdown=action, probe=probe, address=d.get("ip", ""))
        out.append(f"upsnap_device_info{{{labels(il)}}} 1")
        ssh_txt = "" if ssh is None else ("open" if ssh else "closed")
        jdevs.append({"name": d["name"], "status": st, "ssh": ssh_txt, "group": group,
                      "link": d.get("link", ""), "wake": wake_cap, "shutdown": action})
    out.append("# HELP upsnap_devices_total Number of devices in UpSnap.")
    out.append("# TYPE upsnap_devices_total gauge")
    out.append(f"upsnap_devices_total {len(devices)}")
    out.append("# HELP upsnap_devices_online Number of devices currently online.")
    out.append("# TYPE upsnap_devices_online gauge")
    out.append(f"upsnap_devices_online {online}")
    out.append("# HELP upsnap_exporter_up 1 when the last poll of UpSnap succeeded.")
    out.append("# TYPE upsnap_exporter_up gauge")
    out.append("upsnap_exporter_up 1")
    out.append("# HELP upsnap_exporter_last_poll_timestamp_seconds Unix time of the last successful poll.")
    out.append("# TYPE upsnap_exporter_last_poll_timestamp_seconds gauge")
    out.append(f"upsnap_exporter_last_poll_timestamp_seconds {int(now)}")
    with lock:
        state.update(ok=1, updated=now, error="", metrics="\n".join(out) + "\n",
                     json=json.dumps({"updated": int(now), "devices": jdevs}).encode())


def poller():
    while True:
        try:
            poll_once()
        except Exception as e:  # noqa: BLE001 — keep serving the last good state
            token["value"] = None
            with lock:
                state["ok"] = 0
                state["error"] = f"{type(e).__name__}: {e}"
                base = state["metrics"].split("upsnap_exporter_up")[0] if state["metrics"] else ""
                state["metrics"] = (base + "# TYPE upsnap_exporter_up gauge\nupsnap_exporter_up 0\n")
            print(f"poll failed: {e}", file=sys.stderr, flush=True)
        time.sleep(POLL)


class Handler(BaseHTTPRequestHandler):
    server_version = "cn-devices-exporter/1.0"

    def log_message(self, fmt, *args):  # quiet: scraped every 15-60 s
        return

    def do_GET(self):
        path = urllib.parse.urlparse(self.path).path
        with lock:
            ok, metrics, js, err = state["ok"], state["metrics"], state["json"], state["error"]
        if path == "/metrics":
            body = metrics.encode() if metrics else b"upsnap_exporter_up 0\n"
            self._send(200, "text/plain; version=0.0.4; charset=utf-8", body)
        elif path == "/devices.json":
            self._send(200 if ok else 503, "application/json", js if ok else json.dumps({"error": err}).encode())
        elif path == "/healthz":
            self._send(200 if ok else 503, "text/plain", b"ok\n" if ok else (err + "\n").encode())
        else:
            self._send(404, "text/plain", b"not found\n")

    def _send(self, code, ctype, body):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)


def main():
    if not EMAIL or not PASSWORD:
        sys.exit("UPSNAP_EXPORTER_EMAIL / UPSNAP_EXPORTER_PASSWORD not set")
    threading.Thread(target=poller, daemon=True).start()
    host, port = LISTEN.rsplit(":", 1)
    srv = ThreadingHTTPServer((host, int(port)), Handler)
    print(f"cn-devices exporter listening on {LISTEN}, polling {UPSNAP} every {POLL}s", flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    main()
