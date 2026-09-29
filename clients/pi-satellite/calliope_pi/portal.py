"""The Wi-Fi setup network, as the ESP32 satellite has it.

When the board has no network, it opens an open Wi-Fi network called
calliope-sat-XXXX (the last four of its MAC). Join it from a phone: every
address answers with one page (the phone shows it as a sign-in page), which
lists the networks it found, takes the password and the hub's address
(pre-filled), and joins. If the network cannot be joined, the setup network
comes back with the reason on the page.

    python3 -m calliope_pi.portal              open it now, until the board is online
    python3 -m calliope_pi.portal --if-offline open it if the board has been offline
                                               for OFFLINE_S (the netcheck timer)

Root only, and the standard library only: it has to work on the first boot,
before any package is installed. NetworkManager does the rest: its hotspot
(ipv4.method shared) runs the DHCP and DNS, and a dnsmasq line points every
name at the board. The Wi-Fi password goes into a NetworkManager keyfile
(mode 0600), never onto a command line."""

from __future__ import annotations

import argparse
import html
import json
import os
import re
import subprocess
import sys
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs

from . import paths, system
from .state import load, save

AP_CON = "calliope-setup"
AP_ADDRESS = "10.42.0.1"
DNSMASQ = Path("/etc/NetworkManager/dnsmasq-shared.d/calliope-portal.conf")
CONNECTIONS = Path("/etc/NetworkManager/system-connections")
OFFLINE_FILE = Path("/run/calliope-offline-since")
OFFLINE_S = 180
JOIN_S = 45
OPEN_S = 600          # then try the networks it knows again, in case the router was only away


def nmcli(*args: str, timeout: float = 30) -> tuple[int, str]:
    try:
        done = subprocess.run(["nmcli", *args], capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as e:
        return 1, str(e)
    return done.returncode, (done.stdout + done.stderr).strip()


def online() -> bool:
    """A network with a route out: Wi-Fi as a client, or Ethernet. The setup
    network itself does not count."""
    code, out = nmcli("-t", "-f", "TYPE,STATE,CONNECTION", "device")
    if code:
        return False
    for line in out.splitlines():
        kind, state, conn = (line.split(":", 2) + ["", ""])[:3]
        if kind in ("wifi", "ethernet") and state == "connected" and conn != AP_CON:
            return True
    return False


def ap_name() -> str:
    try:
        return f"calliope-sat-{system.satellite_id()[-4:]}"
    except RuntimeError:
        return "calliope-sat"


def scan() -> list[dict]:
    """The networks in range, strongest first, one per name."""
    nmcli("device", "wifi", "rescan", timeout=15)
    time.sleep(3)
    code, out = nmcli("-t", "-f", "SSID,SIGNAL,SECURITY,FREQ", "device", "wifi", "list", timeout=20)
    seen: dict[str, dict] = {}
    for line in out.splitlines() if code == 0 else []:
        parts = re.split(r"(?<!\\):", line)
        if len(parts) < 4 or not parts[0]:
            continue
        ssid = parts[0].replace("\\:", ":")
        try:
            signal = int(parts[1])
        except ValueError:
            signal = 0
        freq = parts[3].split()[0] if parts[3] else ""
        band = "5 GHz" if freq.startswith("5") else "2.4 GHz" if freq.startswith("2") else ""
        entry = {"ssid": ssid, "signal": signal, "secure": parts[2] not in ("", "--"), "band": band}
        if ssid not in seen or signal > seen[ssid]["signal"]:
            seen[ssid] = entry
    return sorted(seen.values(), key=lambda e: -e["signal"])


def keyfile(ssid: str, password: str) -> str:
    """A NetworkManager keyfile for one network, WPA-PSK or open."""
    lines = ["[connection]", f"id=calliope-wifi-{ssid}", f"uuid={uuid.uuid4()}", "type=wifi",
             "autoconnect=true", "autoconnect-priority=10", "", "[wifi]", "mode=infrastructure",
             f"ssid={ssid}", ""]
    if password:
        lines += ["[wifi-security]", "key-mgmt=wpa-psk", f"psk={password}", ""]
    lines += ["[ipv4]", "method=auto", "", "[ipv6]", "method=auto", ""]
    return "\n".join(lines)


def _safe(ssid: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]", "_", ssid)[:48] or "network"


def ap_up() -> bool:
    DNSMASQ.parent.mkdir(parents=True, exist_ok=True)
    DNSMASQ.write_text(f"address=/#/{AP_ADDRESS}\n")
    nmcli("connection", "delete", AP_CON)
    code, out = nmcli("connection", "add", "type", "wifi", "ifname", "wlan0", "con-name", AP_CON,
                      "autoconnect", "no", "ssid", ap_name(), "802-11-wireless.mode", "ap",
                      "802-11-wireless.band", "bg", "ipv4.method", "shared",
                      "ipv4.addresses", f"{AP_ADDRESS}/24", "ipv6.method", "disabled")
    if code == 0:
        code, out = nmcli("connection", "up", AP_CON, timeout=30)
    if code:
        print(f"the setup network did not open: {out[:200]}", flush=True)
    return code == 0


def ap_down() -> None:
    nmcli("connection", "down", AP_CON)
    nmcli("connection", "delete", AP_CON)
    try:
        DNSMASQ.unlink()
    except FileNotFoundError:
        pass


def join(ssid: str, password: str) -> tuple[bool, str]:
    """Write the keyfile, bring it up, wait for the board to be online."""
    CONNECTIONS.mkdir(parents=True, exist_ok=True)
    path = CONNECTIONS / f"calliope-wifi-{_safe(ssid)}.nmconnection"
    path.write_text(keyfile(ssid, password))
    os.chmod(path, 0o600)
    nmcli("connection", "reload")
    code, out = nmcli("connection", "up", f"calliope-wifi-{ssid}", timeout=JOIN_S)
    deadline = time.monotonic() + JOIN_S
    while time.monotonic() < deadline:
        if online():
            return True, ""
        time.sleep(2)
    path.unlink(missing_ok=True)
    nmcli("connection", "reload")
    why = "the password was not accepted" if re.search(r"secrets|psk|auth", out, re.I) else \
        (out.splitlines()[-1][:160] if out else "no answer")
    return False, f"Could not join {ssid}: {why}."


def set_hub(hub: str) -> None:
    st = load()
    st.hub = hub
    save(st)
    try:  # the agent runs as calliope and must go on reading it
        import pwd
        u = pwd.getpwnam("calliope")
        os.chown(paths.state_file(), u.pw_uid, u.pw_gid)
    except (KeyError, OSError, ImportError):
        pass


PAGE = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Calliope satellite setup</title>
<style>
body{{font:16px/1.4 system-ui,sans-serif;margin:0;padding:24px 16px;
  background:#111;color:#eee;max-width:520px}}
h1{{font-size:20px;margin:0 0 4px}} p{{color:#aaa;margin:0 0 16px}} label{{display:block;margin:14px 0 4px}}
select,input{{width:100%;box-sizing:border-box;font:inherit;padding:10px;border-radius:8px;
  border:1px solid #444;background:#1c1c1c;color:#eee}}
button{{margin-top:20px;width:100%;font:inherit;padding:12px;border:0;border-radius:8px;
  background:#2f6fff;color:#fff}}
.err{{background:#3a1515;border:1px solid #803030;padding:10px;border-radius:8px;margin-bottom:12px}}
small{{color:#888}}
</style></head><body>
<h1>{name}</h1><p>Choose the network this satellite should join.</p>
{error}
<form method="post" action="/join">
<label for="ssid">Network</label>
<select id="ssid" name="ssid">{options}<option value="">Another network…</option></select>
<label for="other">Or type its name</label><input id="other" name="other" autocomplete="off">
<label for="password">Password</label>
<input id="password" name="password" type="password" autocomplete="off">
<label for="hub">Hub address</label>
<input id="hub" name="hub" value="{hub}" placeholder="wss://calliope.example.com" autocomplete="off">
<small>5 GHz is better for Bluetooth audio on a Pi 3: its 2.4 GHz Wi-Fi shares the Bluetooth radio.</small>
<button type="submit">Join</button></form></body></html>"""

DONE = """<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Joining</title>
<style>body{{font:16px/1.4 system-ui,sans-serif;padding:24px 16px;background:#111;color:#eee}}</style></head>
<body><h1>Joining {ssid}</h1><p>This network closes now. Once the satellite is on {ssid} it appears on the
hub's Satellites tab, ready to adopt. If it could not join, {name} comes back
in about a minute with the reason.</p></body></html>"""


class Portal:
    def __init__(self) -> None:
        self.networks: list[dict] = []
        self.error = ""
        self.chosen: tuple[str, str, str] | None = None
        self.got = threading.Event()

    def page(self) -> str:
        opts = "".join(
            f'<option value="{html.escape(n["ssid"], quote=True)}">{html.escape(n["ssid"])} '
            f'({n["band"] or "?"}, {n["signal"]}%{", open" if not n["secure"] else ""})</option>'
            for n in self.networks)
        hub = load().hub or ""
        err = f'<div class="err">{html.escape(self.error)}</div>' if self.error else ""
        return PAGE.format(name=html.escape(ap_name()), options=opts, hub=html.escape(hub, quote=True),
                           error=err)

    def handler(portal):  # noqa: N805 - the portal, closed over by the handler class
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args) -> None:
                pass

            def _send(self, body: str, status: int = 200) -> None:
                data = body.encode()
                self.send_response(status)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(data)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self) -> None:  # noqa: N802
                self._send(portal.page())

            def do_POST(self) -> None:  # noqa: N802
                n = min(int(self.headers.get("Content-Length") or 0), 8192)
                form = {k: v[0] for k, v in parse_qs(self.rfile.read(n).decode(errors="replace")).items()}
                ssid = (form.get("other") or form.get("ssid") or "").strip()
                hub = (form.get("hub") or "").strip()
                if not ssid:
                    portal.error = "Choose a network, or type its name."
                    return self._send(portal.page())
                if hub and not hub.startswith(("wss://", "ws://")):
                    portal.error = "The hub address starts with wss:// (or ws:// on a test network)."
                    return self._send(portal.page())
                portal.chosen = (ssid, form.get("password") or "", hub)
                self._send(DONE.format(ssid=html.escape(ssid), name=html.escape(ap_name())))
                portal.got.set()
        return Handler

    def run_once(self) -> bool:
        """Open the setup network until someone joins a network or OPEN_S
        passes. True when the board is online at the end."""
        self.networks = scan()
        if not ap_up():
            return False
        server = ThreadingHTTPServer(("0.0.0.0", 80), self.handler())
        threading.Thread(target=server.serve_forever, daemon=True).start()
        print(f"setup network {ap_name()} is open; {len(self.networks)} networks listed", flush=True)
        try:
            got = self.got.wait(OPEN_S)
            time.sleep(2 if got else 0)   # let the page reach the phone
        finally:
            server.shutdown()
            server.server_close()
            ap_down()
        if not got:
            time.sleep(20)                # give the networks it already knows a chance
            return online()
        self.got.clear()
        ssid, password, hub = self.chosen
        if hub:
            set_hub(hub)
        ok, why = join(ssid, password)
        self.error = why
        print(f"joined {ssid}" if ok else why, flush=True)
        return ok

    def run(self) -> None:
        while not online():
            self.run_once()


def offline_long_enough(now: float | None = None) -> bool:
    """For the netcheck timer: remember when the board went offline, and
    say so once it has been OFFLINE_S."""
    now = now or time.time()
    if online():
        OFFLINE_FILE.unlink(missing_ok=True)
        return False
    try:
        since = float(OFFLINE_FILE.read_text())
    except (OSError, ValueError):
        OFFLINE_FILE.write_text(str(now))
        return False
    return now - since >= OFFLINE_S


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="calliope-portal")
    ap.add_argument("--if-offline", action="store_true")
    args = ap.parse_args(argv)
    if args.if_offline:
        if offline_long_enough():
            subprocess.run(["systemctl", "--no-block", "start", "calliope-portal.service"], check=False)
        return 0
    Portal().run()
    OFFLINE_FILE.unlink(missing_ok=True)
    print(json.dumps({"online": True}), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
