#!/usr/bin/env python3
"""
papercut.py - Install PaperCut printers on Linux

Discovers your school's PaperCut server automatically and adds all printers
to CUPS in one shot.

Usage:
    sudo python3 papercut.py
    sudo python3 papercut.py --server 10.10.5.19
    sudo python3 papercut.py --remove
"""

import argparse
import getpass
import ipaddress
import json
import os
import re
import socket
import ssl
import struct
import subprocess
import sys
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed

try:
    from zeroconf import ServiceBrowser, Zeroconf
    HAS_ZEROCONF = True
except ImportError:
    HAS_ZEROCONF = False

HTTP_PORT  = 9163
HTTPS_PORT = 9164

MDNS_TYPES = [
    "_pc-printer-discovery._tcp.local.",
    "_ipp._tcp.local.",
    "_ipps._tcp.local.",
]

# Common hostnames PaperCut servers use
PAPERCUT_HOSTNAMES = [
    "rpc.pc-printer-discovery",
    "pc-printer-discovery",
    "papercut",
    "print",
    "printing",
]

AUTH_PATHS    = ["/api/auth", "/api/v1/auth", "/auth", "/api/authenticate"]
PRINTER_PATHS = ["/api/printers", "/api/v1/printers", "/printers"]

TOKEN_RE = re.compile(
    r"(https?)://([^/:]+)(?::\d+)?/printers/([^/]+)/users/(\d+)/([0-9a-f]{64})"
)


# ── network helpers ───────────────────────────────────────────────────────────

def _default_gateway() -> str | None:
    try:
        result = subprocess.run(["ip", "route"], capture_output=True, text=True)
        m = re.search(r"default via (\S+)", result.stdout)
        return m.group(1) if m else None
    except Exception:
        return None


def _local_network() -> ipaddress.IPv4Network | None:
    """Return the local subnet (e.g. 10.10.0.0/20)."""
    try:
        result = subprocess.run(["ip", "addr"], capture_output=True, text=True)
        # Find the non-loopback inet address
        for m in re.finditer(r"inet (\d+\.\d+\.\d+\.\d+/\d+)", result.stdout):
            net = ipaddress.IPv4Interface(m.group(1)).network
            if not net.is_loopback:
                return net
    except Exception:
        pass
    return None


def _port_open(ip: str, port: int, timeout: float = 0.3) -> bool:
    try:
        with socket.create_connection((ip, port), timeout=timeout):
            return True
    except Exception:
        return False


def _dns_query(server: str, hostname: str) -> str | None:
    """Send a raw DNS A query to a specific server. Returns IP or None."""
    try:
        # Build minimal DNS query
        query = b"\xab\xcd\x01\x00\x00\x01\x00\x00\x00\x00\x00\x00"
        for label in hostname.rstrip(".").split("."):
            query += bytes([len(label)]) + label.encode()
        query += b"\x00\x00\x01\x00\x01"

        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.settimeout(2)
        s.sendto(query, (server, 53))
        data, _ = s.recvfrom(512)
        s.close()

        # Walk past the question section, find first A record
        i = 12
        while i < len(data) and data[i] != 0:
            i += data[i] + 1
        i += 5  # skip null + qtype + qclass

        while i + 10 < len(data):
            i += 2  # name (compressed pointer or label)
            rtype  = (data[i] << 8) | data[i+1]
            rdlen  = (data[i+8] << 8) | data[i+9]
            if rtype == 1 and rdlen == 4:  # A record
                return ".".join(str(b) for b in data[i+10:i+14])
            i += 10 + rdlen

    except Exception:
        pass
    return None


# ── discovery methods ─────────────────────────────────────────────────────────

def _discover_mdns(timeout: int = 5) -> str | None:
    if not HAS_ZEROCONF:
        return None
    found: list[str] = []

    class Listener:
        def add_service(self, zc, type_, name):
            info = zc.get_service_info(type_, name)
            if not info or not info.addresses:
                return
            n = name.lower()
            if (info.port in (HTTP_PORT, HTTPS_PORT)
                    or "papercut" in n or "pc-printer" in n):
                ip = socket.inet_ntoa(info.addresses[0])
                if ip not in found:
                    found.append(ip)
        def remove_service(self, *_): pass
        def update_service(self, *_): pass

    zc = Zeroconf()
    [ServiceBrowser(zc, t, Listener()) for t in MDNS_TYPES]
    time.sleep(timeout)
    zc.close()
    return found[0] if found else None


def _discover_dns(gateway: str) -> str | None:
    """Try resolving common PaperCut hostnames via the gateway's DNS."""
    # Get the local domain from the gateway (e.g. 10.10.0.1 -> try .local suffixes)
    local_net = _local_network()
    domains = ["local"]
    if local_net:
        # Derive likely internal domain from reverse DNS of gateway
        try:
            fqdn = socket.getfqdn(gateway)
            parts = fqdn.split(".")
            if len(parts) > 2:
                domains.insert(0, ".".join(parts[1:]))
        except Exception:
            pass

    for hostname in PAPERCUT_HOSTNAMES:
        for domain in domains:
            fqdn = f"{hostname}.{domain}"
            ip = _dns_query(gateway, fqdn)
            if ip and ip != gateway:
                if _port_open(ip, HTTP_PORT) or _port_open(ip, HTTPS_PORT):
                    return ip
            # Also try system DNS
            try:
                ip = socket.gethostbyname(fqdn)
                if _port_open(ip, HTTP_PORT) or _port_open(ip, HTTPS_PORT):
                    return ip
            except Exception:
                pass
    return None


def _discover_scan(network: ipaddress.IPv4Network) -> str | None:
    """Scan the local subnet for open PaperCut ports."""
    hosts = list(network.hosts())
    total = len(hosts)

    # Scan /24 first (fast), then rest of subnet if needed
    my_ip = _get_own_ip()
    if my_ip:
        same_24 = [h for h in hosts
                   if str(h).rsplit(".", 1)[0] == my_ip.rsplit(".", 1)[0]]
        rest    = [h for h in hosts if h not in same_24]
        ordered = same_24 + rest
    else:
        ordered = hosts

    print(f"  scanning {total} host(s)", end="", flush=True)

    def check(host):
        ip = str(host)
        for port in (HTTP_PORT, HTTPS_PORT):
            if _port_open(ip, port):
                return ip
        return None

    with ThreadPoolExecutor(max_workers=150) as pool:
        futures = {pool.submit(check, h): h for h in ordered}
        done = 0
        for future in as_completed(futures):
            done += 1
            if done % 50 == 0:
                print(".", end="", flush=True)
            result = future.result()
            if result:
                # Cancel remaining
                for f in futures:
                    f.cancel()
                print(f" found {result}")
                return result

    print(" not found")
    return None


def _get_own_ip() -> str | None:
    try:
        result = subprocess.run(["ip", "addr"], capture_output=True, text=True)
        for m in re.finditer(r"inet (\d+\.\d+\.\d+\.\d+)/", result.stdout):
            ip = m.group(1)
            if not ip.startswith("127."):
                return ip
    except Exception:
        pass
    return None


def discover_server() -> str | None:
    print("Searching for PaperCut server...")

    print("  mDNS...", end=" ", flush=True)
    ip = _discover_mdns(timeout=4)
    if ip:
        print(f"found {ip}")
        return ip
    print("not found")

    gateway = _default_gateway()
    if gateway:
        print(f"  DNS via gateway ({gateway})...", end=" ", flush=True)
        ip = _discover_dns(gateway)
        if ip:
            print(f"found {ip}")
            return ip
        print("not found")

    network = _local_network()
    if network:
        print(f"  port scan ({network})...", end=" ", flush=True)
        ip = _discover_scan(network)
        if ip:
            return ip

    return None


# ── HTTP helpers ──────────────────────────────────────────────────────────────

def _ssl_ctx() -> ssl.SSLContext:
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx


def _post(url: str, data: dict) -> tuple[int, dict | None]:
    body = json.dumps(data).encode()
    req  = urllib.request.Request(url, data=body,
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


def _printer_from_match(m: re.Match) -> dict:
    scheme, host, raw_name, uid, token = m.groups()
    return {
        "name":    raw_name.replace("+", " ").replace("%2b", "+").replace("~2b", "+"),
        "server":  host.split(":")[0],
        "port":    HTTPS_PORT if scheme == "https" else HTTP_PORT,
        "scheme":  scheme,
        "user_id": int(uid),
        "token":   token,
    }


def _parse_printer_list(body: any, server: str, port: int,
                         scheme: str, user_id: int | None) -> list[dict]:
    printers = []
    items = body if isinstance(body, list) else body.get("printers", [])
    for item in items:
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


def fetch_printers(server: str, username: str, password: str) -> list[dict]:
    for scheme, port in [("https", HTTPS_PORT), ("http", HTTP_PORT)]:
        base = f"{scheme}://{server}:{port}"
        auth_token, user_info = _auth(base, username, password)
        if user_info is None:
            continue

        raw = json.dumps(user_info)
        printers = [_printer_from_match(m) for m in TOKEN_RE.finditer(raw)]
        if printers:
            return printers

        user_id = (user_info.get("userId") or user_info.get("id")
                   or user_info.get("user_id"))
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
    return subprocess.run(cmd, capture_output=True, text=True).returncode == 0


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
        name   = _cups_name(p)
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
                        help="PaperCut server address (skip auto-discovery)")
    parser.add_argument("--remove", action="store_true",
                        help="remove previously installed printers")
    parser.add_argument("--pcap", metavar="FILE", help=argparse.SUPPRESS)
    args = parser.parse_args()

    if args.pcap:
        printers = fetch_from_pcap(args.pcap)
        if not printers:
            print("No printers found in capture.")
            sys.exit(1)
    else:
        if args.remove:
            result = subprocess.run(["lpstat", "-p"], capture_output=True, text=True)
            names  = re.findall(r"printer (\S+)", result.stdout)
            if not names:
                print("No printers found in CUPS.")
                sys.exit(0)
            print(f"Removing {len(names)} printer(s):")
            for n in names:
                print(f"  {n}")
            remove_printers([{"name": n, "server": "", "port": 0,
                               "scheme": "", "user_id": 0, "token": ""} for n in names])
            return

        server = args.server
        if not server:
            server = discover_server()
        if not server:
            server = input("Server IP or hostname: ").strip()
        if not server:
            print("No server found.")
            sys.exit(1)

        username = input("Username: ").strip()
        password = getpass.getpass("Password: ")

        print("Fetching printer list...", end=" ", flush=True)
        printers = fetch_printers(server, username, password)
        if not printers:
            print("failed.\nCould not fetch printers - check credentials and server address.")
            sys.exit(1)
        print(f"{len(printers)} printer(s) found\n")

    for p in printers:
        print(f"  {p['name']}")
    print()
    install_printers(printers)


if __name__ == "__main__":
    main()
