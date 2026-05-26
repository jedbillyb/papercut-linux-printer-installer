#!/usr/bin/env python3
"""
papercut.py - Install PaperCut printers on Linux

Discovers your school's PaperCut server via mDNS, authenticates with your
credentials, and adds all printers to CUPS in one shot.

Usage:
    sudo python3 papercut.py
    sudo python3 papercut.py --server 10.10.5.19
    sudo python3 papercut.py --remove
"""

import argparse
import getpass
import json
import os
import re
import socket
import ssl
import subprocess
import sys
import time
import urllib.error
import urllib.request

try:
    from zeroconf import ServiceBrowser, Zeroconf
    HAS_ZEROCONF = True
except ImportError:
    HAS_ZEROCONF = False

# PaperCut Mobility Print ports
HTTP_PORT  = 9163
HTTPS_PORT = 9164

# mDNS service types to scan
MDNS_TYPES = [
    "_pc-printer-discovery._tcp.local.",
    "_ipp._tcp.local.",
    "_ipps._tcp.local.",
]

# Auth and printer-list endpoints tried in order
AUTH_PATHS    = ["/api/auth", "/api/v1/auth", "/auth", "/api/authenticate"]
PRINTER_PATHS = ["/api/printers", "/api/v1/printers", "/printers"]

TOKEN_RE = re.compile(
    r"(https?)://([^/:]+)(?::\d+)?/printers/([^/]+)/users/(\d+)/([0-9a-f]{64})"
)


# ── mDNS discovery ────────────────────────────────────────────────────────────

def _is_papercut(name: str, port: int) -> bool:
    name = name.lower()
    return (port in (HTTP_PORT, HTTPS_PORT)
            or "papercut" in name
            or "pc-printer" in name)


def discover_mdns(timeout: int = 5) -> str | None:
    if not HAS_ZEROCONF:
        return None

    found: list[str] = []

    class Listener:
        def add_service(self, zc, type_, name):
            info = zc.get_service_info(type_, name)
            if not info or not info.addresses:
                return
            if _is_papercut(name, info.port):
                ip = socket.inet_ntoa(info.addresses[0])
                if ip not in found:
                    found.append(ip)

        def remove_service(self, *_): pass
        def update_service(self, *_): pass

    zc = Zeroconf()
    browsers = [ServiceBrowser(zc, t, Listener()) for t in MDNS_TYPES]
    print(f"Scanning for PaperCut server via mDNS ({timeout}s)...", end=" ", flush=True)
    time.sleep(timeout)
    zc.close()

    if found:
        print(f"found {found[0]}")
        return found[0]
    print("not found")
    return None


# ── HTTP helpers ──────────────────────────────────────────────────────────────

def _ssl_ctx() -> ssl.SSLContext:
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx


def _post(url: str, data: dict) -> tuple[int, dict | None]:
    body = json.dumps(data).encode()
    req = urllib.request.Request(url, data=body,
                                  headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, context=_ssl_ctx(), timeout=5) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, None
    except Exception:
        return 0, None


def _get(url: str, token: str | None = None) -> tuple[int, any]:
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, context=_ssl_ctx(), timeout=5) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, None
    except Exception:
        return 0, None


# ── PaperCut API ──────────────────────────────────────────────────────────────

def _auth(base: str, username: str, password: str) -> tuple[str | None, dict | None]:
    creds = {"username": username, "password": password}
    for path in AUTH_PATHS:
        status, body = _post(base + path, creds)
        if status == 200 and body:
            token = (body.get("token") or body.get("authToken")
                     or body.get("access_token") or body.get("sessionToken"))
            return token, body
    return None, None


def _parse_printer_list(body: any, server: str, port: int,
                         scheme: str, user_id: int | None) -> list[dict]:
    printers = []
    items = body if isinstance(body, list) else body.get("printers", [])
    for item in items:
        # Some servers embed full IPP URLs in the response
        for field in ("uri", "ippUri", "url"):
            m = TOKEN_RE.search(str(item.get(field, "")))
            if m:
                printers.append(_printer_from_match(m))
                break
        else:
            name  = item.get("name") or item.get("printerName") or item.get("displayName", "")
            token = item.get("token") or item.get("userToken") or item.get("authToken", "")
            uid   = int(item.get("userId") or item.get("user_id") or user_id or 0)
            if name and token:
                printers.append({"name": name, "server": server, "port": port,
                                  "scheme": scheme, "user_id": uid, "token": token})
    return printers


def _printer_from_match(m: re.Match) -> dict:
    scheme, host, raw_name, uid, token = m.groups()
    host_only = host.split(":")[0]
    return {
        "name":    raw_name.replace("+", " ").replace("%2b", "+").replace("~2b", "+"),
        "server":  host_only,
        "port":    HTTPS_PORT if scheme == "https" else HTTP_PORT,
        "scheme":  scheme,
        "user_id": int(uid),
        "token":   token,
    }


def fetch_printers(server: str, username: str, password: str) -> list[dict]:
    for scheme, port in [("https", HTTPS_PORT), ("http", HTTP_PORT)]:
        base = f"{scheme}://{server}:{port}"
        auth_token, user_info = _auth(base, username, password)
        if user_info is None:
            continue

        # Auth response might already contain printer URLs
        raw = json.dumps(user_info)
        printers = [_printer_from_match(m) for m in TOKEN_RE.finditer(raw)]
        if printers:
            return printers

        user_id = (user_info.get("userId") or user_info.get("id")
                   or user_info.get("user_id"))

        # Try dedicated printer-list endpoints
        paths = PRINTER_PATHS[:]
        if user_id:
            paths = [f"{p}/users/{user_id}" for p in PRINTER_PATHS] + paths
        for path in paths:
            status, body = _get(base + path, auth_token)
            if status == 200 and body:
                found = _parse_printer_list(body, server, port, scheme, user_id)
                if found:
                    return found

    return []


# ── pcap fallback (hidden) ────────────────────────────────────────────────────

def fetch_from_pcap(path: str) -> list[dict]:
    result = subprocess.run(
        ["tshark", "-r", path, "-Y", "ipp", "-V"],
        capture_output=True, text=True
    )
    printers, seen = [], set()
    for m in TOKEN_RE.finditer(result.stdout):
        p = _printer_from_match(m)
        key = (p["server"], p["name"], p["user_id"])
        if key not in seen:
            seen.add(key)
            printers.append(p)
    return printers


# ── CUPS ──────────────────────────────────────────────────────────────────────

def _ipp_url(p: dict) -> str:
    scheme = "ipps" if p["scheme"] == "https" else "ipp"
    name   = p["name"].replace(" ", "+")
    return f"{scheme}://{p['server']}:{p['port']}/printers/{name}/users/{p['user_id']}/{p['token']}"


def _cups_name(p: dict) -> str:
    return p["name"].replace(" ", "-")


def _run(*cmd: str) -> bool:
    r = subprocess.run(cmd, capture_output=True, text=True)
    return r.returncode == 0


def _cups_ok() -> bool:
    return _run("which", "lpadmin")


def install_printers(printers: list[dict]) -> None:
    if os.geteuid() != 0:
        print("Run with sudo to install printers.")
        sys.exit(1)
    if not _cups_ok():
        print("CUPS not found - install it first:")
        print("  Ubuntu/Debian : sudo apt install cups")
        print("  Arch          : sudo pacman -S cups")
        print("  Fedora        : sudo dnf install cups")
        print("  Void          : sudo xbps-install cups")
        sys.exit(1)

    ok = 0
    for p in printers:
        name = _cups_name(p)
        exists = _run("lpstat", "-p", name)
        label  = "(update)" if exists else "(new)   "
        print(f"  {label} {name}...", end=" ", flush=True)
        if _run("lpadmin", "-p", name, "-v", _ipp_url(p),
                "-m", "everywhere", "-E", "-D", p["name"]):
            _run("cupsenable", name)
            _run("cupsaccept", name)
            print("done")
            ok += 1
        else:
            print("FAILED")

    print(f"\n{ok}/{len(printers)} printers installed.")
    if ok:
        print("Open any application and select a printer to test.")


def remove_printers(printers: list[dict]) -> None:
    if os.geteuid() != 0:
        print("Run with sudo to remove printers.")
        sys.exit(1)
    ok = 0
    for p in printers:
        name = _cups_name(p)
        print(f"  Removing {name}...", end=" ", flush=True)
        if _run("lpadmin", "-x", name):
            print("removed")
            ok += 1
        else:
            print("not found")
    print(f"\n{ok} printer(s) removed.")


# ── main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--server", metavar="HOST/IP",
                        help="PaperCut server address (skip mDNS discovery)")
    parser.add_argument("--remove", action="store_true",
                        help="remove previously installed printers")
    parser.add_argument("--pcap", metavar="FILE", help=argparse.SUPPRESS)
    args = parser.parse_args()

    # --- find the server ---
    if args.pcap:
        printers = fetch_from_pcap(args.pcap)
        if not printers:
            print("No printers found in capture.")
            sys.exit(1)
    else:
        server = args.server or discover_mdns()
        if not server:
            server = input("Server IP or hostname: ").strip()
        if not server:
            print("No server specified.")
            sys.exit(1)

        if args.remove:
            # For removal we don't need creds - just reconstruct names from a
            # quick unauthenticated printer-name query or list existing CUPS queues
            result = subprocess.run(["lpstat", "-p"], capture_output=True, text=True)
            names  = re.findall(r"printer (\S+)", result.stdout)
            if not names:
                print("No printers found in CUPS.")
                sys.exit(0)
            printers = [{"name": n.replace("-", " "), "server": server,
                          "port": HTTP_PORT, "scheme": "http",
                          "user_id": 0, "token": ""} for n in names]
            print(f"Found {len(printers)} printer(s) in CUPS:")
            for p in printers:
                print(f"  {_cups_name(p)}")
            remove_printers(printers)
            return

        # --- authenticate and fetch ---
        username = input("Username: ").strip()
        password = getpass.getpass("Password: ")

        print("Fetching printer list...", end=" ", flush=True)
        printers = fetch_printers(server, username, password)
        if not printers:
            print("failed.\nCould not fetch printers - check credentials and server address.")
            sys.exit(1)
        print(f"{len(printers)} printer(s) found\n")

    # --- show and install ---
    for p in printers:
        print(f"  {p['name']}")
    print()
    install_printers(printers)


if __name__ == "__main__":
    main()
