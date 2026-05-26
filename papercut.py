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
import http.cookiejar
import ipaddress
import os
import re
import socket
import ssl
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed

try:
    from zeroconf import ServiceBrowser, Zeroconf
    HAS_ZEROCONF = True
except ImportError:
    HAS_ZEROCONF = False

IPP_PORT     = 9163   # IPP — used in CUPS device URIs for print jobs
IPP_SSL_PORT = 9164   # IPPS — used in CUPS device URIs for print jobs
API_PORT     = 9191   # PaperCut web / API (HTTP)
API_SSL_PORT = 9192   # PaperCut web / API (HTTPS)

_DEBUG = False


def _dbg(*args) -> None:
    if _DEBUG:
        print("[debug]", *args)

MDNS_TYPES = [
    "_pc-printer-discovery._tcp.local.",
    "_ipp._tcp.local.",
    "_ipps._tcp.local.",
]

PAPERCUT_HOSTNAMES = [
    "rpc.pc-printer-discovery",
    "pc-printer-discovery",
    "papercut",
    "print",
    "printing",
]

TOKEN_RE = re.compile(
    r"(https?)://([^/:]+)(?::\d+)?/printers/([^/]+)/users/(\d+)/([0-9a-f]{64})"
)

# Matches PaperCut IPP device URIs registered in CUPS
_CUPS_PC_RE = re.compile(r"device for (\S+):\s+ipps?://[^:]+:\d+/printers/")


# ── network helpers ───────────────────────────────────────────────────────────

def _default_gateway() -> str | None:
    try:
        result = subprocess.run(["ip", "route"], capture_output=True, text=True)
        m = re.search(r"default via (\S+)", result.stdout)
        return m.group(1) if m else None
    except Exception:
        return None


def _local_networks() -> list[ipaddress.IPv4Network]:
    """Return all non-loopback local subnets (handles multiple interfaces)."""
    nets: list[ipaddress.IPv4Network] = []
    try:
        result = subprocess.run(["ip", "addr"], capture_output=True, text=True)
        for m in re.finditer(r"inet (\d+\.\d+\.\d+\.\d+/\d+)", result.stdout):
            net = ipaddress.IPv4Interface(m.group(1)).network
            if not net.is_loopback and net not in nets:
                nets.append(net)
    except Exception:
        pass
    return nets


def _port_open(ip: str, port: int, timeout: float = 0.3) -> bool:
    try:
        with socket.create_connection((ip, port), timeout=timeout):
            return True
    except Exception:
        return False


def _dns_query(server: str, hostname: str) -> str | None:
    """Send a raw DNS A query to a specific server. Returns IP or None."""
    try:
        query = b"\xab\xcd\x01\x00\x00\x01\x00\x00\x00\x00\x00\x00"
        for label in hostname.rstrip(".").split("."):
            query += bytes([len(label)]) + label.encode()
        query += b"\x00\x00\x01\x00\x01"

        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.settimeout(2)
        s.sendto(query, (server, 53))
        data, _ = s.recvfrom(512)
        s.close()

        # Walk past the question section to find first A record
        i = 12
        while i < len(data) and data[i] != 0:
            i += data[i] + 1
        i += 5  # skip null + qtype + qclass

        while i + 10 < len(data):
            i += 2  # name pointer or label start
            rtype = (data[i] << 8) | data[i + 1]
            rdlen = (data[i + 8] << 8) | data[i + 9]
            if rtype == 1 and rdlen == 4:  # A record
                return ".".join(str(b) for b in data[i + 10:i + 14])
            i += 10 + rdlen

    except Exception:
        pass
    return None


# ── discovery methods ─────────────────────────────────────────────────────────

def _start_mdns() -> tuple[object, list[str]] | tuple[None, None]:
    """Start mDNS listeners. Returns (zeroconf, found_list) — caller does the wait."""
    if not HAS_ZEROCONF:
        return None, None
    found: list[str] = []

    class Listener:
        def add_service(self, zc, type_, name):
            info = zc.get_service_info(type_, name)
            if not info or not info.addresses:
                return
            n = name.lower()
            if (info.port in (IPP_PORT, IPP_SSL_PORT)
                    or "papercut" in n or "pc-printer" in n):
                ip = socket.inet_ntoa(info.addresses[0])
                if ip not in found:
                    found.append(ip)
        def remove_service(self, *_): pass
        def update_service(self, *_): pass

    zc = Zeroconf()
    [ServiceBrowser(zc, t, Listener()) for t in MDNS_TYPES]
    return zc, found


def _discover_dns(gateway: str) -> str | None:
    """Try resolving common PaperCut hostnames via the gateway's DNS."""
    domains = ["local"]
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
                if _port_open(ip, API_PORT) or _port_open(ip, API_SSL_PORT):
                    return ip
            try:
                ip = socket.gethostbyname(fqdn)
                if _port_open(ip, API_PORT) or _port_open(ip, API_SSL_PORT):
                    return ip
            except Exception:
                pass
    return None


_SPINNER = [".  ", ".. ", "...", ".. "]

def _scan_hosts(hosts: list, label: str) -> str | None:
    """Port-scan a list of IPs for PaperCut ports. Returns first hit or None."""
    prefix = f"    scanning {label} ({len(hosts)} hosts)"

    def check(ip):
        for port in (API_PORT, API_SSL_PORT):
            if _port_open(ip, port):
                return ip
        return None

    with ThreadPoolExecutor(max_workers=200) as pool:
        futures = {pool.submit(check, str(h)): h for h in hosts}
        done = 0
        for future in as_completed(futures):
            spin = _SPINNER[done % len(_SPINNER)]
            print(f"\r{prefix}{spin}", end="", flush=True)
            done += 1
            result = future.result()
            if result:
                for f in futures:
                    f.cancel()
                print(f"\r{prefix}... found {result}")
                return result

    print(f"\r{prefix}... not found")
    return None


def _probe_live_24s(prefixes: list[str], label: str) -> list[str]:
    """Return /24 prefixes where .1 or .254 answers on a PaperCut port."""
    pfx = f"    probing {label} ({len(prefixes)} subnets)"
    live: list[str] = []

    def probe(prefix):
        for host in (f"{prefix}.1", f"{prefix}.254"):
            if (_port_open(host, API_PORT, timeout=0.2)
                    or _port_open(host, API_SSL_PORT, timeout=0.2)):
                return prefix
        return None

    with ThreadPoolExecutor(max_workers=300) as pool:
        futures = {pool.submit(probe, p): p for p in prefixes}
        done = 0
        for future in as_completed(futures):
            spin = _SPINNER[done % len(_SPINNER)]
            print(f"\r{pfx}{spin}", end="", flush=True)
            done += 1
            hit = future.result()
            if hit:
                live.append(hit)

    result = f"{len(live)} candidate(s)" if live else "no candidates"
    print(f"\r{pfx}... {result}")
    return live


def _discover_scan(networks: list[ipaddress.IPv4Network]) -> str | None:
    """Scan local subnets then the rest of the /16 — stopping as soon as found."""
    my_ip = _get_own_ip()
    gateway = _default_gateway()

    # 1. Local subnets (all detected interfaces)
    for network in networks:
        local_hosts = list(network.hosts())
        if my_ip:
            my_24 = my_ip.rsplit(".", 1)[0]
            local_hosts.sort(key=lambda h: (str(h).rsplit(".", 1)[0] != my_24))
        result = _scan_hosts(local_hosts, str(network))
        if result:
            return result

    if not gateway:
        return None

    parts = gateway.split(".")
    first, second = parts[0], parts[1]

    scanned_prefixes = {
        str(n.network_address).rsplit(".", 1)[0] for n in networks
    }

    # 2. Rest of the /16 (same first two octets as gateway)
    candidates_16 = [
        f"{first}.{second}.{c}"
        for c in range(256)
        if f"{first}.{second}.{c}" not in scanned_prefixes
    ]
    if candidates_16:
        live = _probe_live_24s(candidates_16, f"{first}.{second}.0.0/16")
        for prefix in live:
            result = _scan_hosts(
                [f"{prefix}.{i}" for i in range(1, 255)], f"{prefix}.0/24"
            )
            if result:
                return result

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

    mdns_msg = "  [1/3] mDNS broadcast"
    if HAS_ZEROCONF:
        zc, found = _start_mdns()
        for i in range(40):  # 4 s in 100 ms ticks
            print(f"\r{mdns_msg}{_SPINNER[i % len(_SPINNER)]}", end="", flush=True)
            time.sleep(0.1)
            if found:
                break
        zc.close()
        ip = found[0] if found else None
        print(f"\r{mdns_msg}... {'found ' + ip if ip else 'not found'}")
        if ip:
            return ip
    else:
        print(f"{mdns_msg}... skipped — install python3-zeroconf for faster discovery")

    gateway = _default_gateway()
    if gateway:
        msg = f"  [2/3] DNS via gateway ({gateway})"
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(_discover_dns, gateway)
            i = 0
            while not future.done():
                print(f"\r{msg}{_SPINNER[i % len(_SPINNER)]}", end="", flush=True)
                i += 1
                time.sleep(0.1)
        ip = future.result()
        print(f"\r{msg}... {'found ' + ip if ip else 'not found'}")
        if ip:
            return ip

    networks = _local_networks()
    if networks:
        print("  [3/3] port scan:")
        return _discover_scan(networks)

    return None


# ── HTTP helpers ──────────────────────────────────────────────────────────────

def _ssl_ctx() -> ssl.SSLContext:
    # Self-signed certs are common on internal school/office networks
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx


def _make_opener(jar: http.cookiejar.CookieJar) -> urllib.request.OpenerDirector:
    return urllib.request.build_opener(
        urllib.request.HTTPSHandler(context=_ssl_ctx()),
        urllib.request.HTTPCookieProcessor(jar),
    )


def _form_login(opener: urllib.request.OpenerDirector,
                base: str, username: str, password: str) -> str:
    """POST form credentials to /user. Returns response HTML."""
    url = f"{base}/user"
    body = urllib.parse.urlencode({
        "inputUsername": username,
        "inputPassword": password,
    }).encode()
    _dbg(f"POST {url}")
    req = urllib.request.Request(url, data=body,
                                  headers={"Content-Type": "application/x-www-form-urlencoded"})
    try:
        with opener.open(req, timeout=10) as r:
            html = r.read().decode("utf-8", errors="replace")
            _dbg(f"  → {r.status} ({len(html)} bytes)")
            return html
    except urllib.error.HTTPError as e:
        _dbg(f"  → {e.code}")
        return ""
    except Exception as e:
        _dbg(f"  → 0 ({e})")
        return ""


def _session_get(opener: urllib.request.OpenerDirector, url: str) -> tuple[int, str]:
    _dbg(f"GET {url}")
    try:
        with opener.open(url, timeout=10) as r:
            html = r.read().decode("utf-8", errors="replace")
            _dbg(f"  → {r.status} ({len(html)} bytes)")
            return r.status, html
    except urllib.error.HTTPError as e:
        _dbg(f"  → {e.code}")
        return e.code, ""
    except Exception as e:
        _dbg(f"  → 0 ({e})")
        return 0, ""


def _xmlrpc_printers(opener: urllib.request.OpenerDirector,
                     base: str, password: str) -> list[dict]:
    """Try PaperCut MF XML-RPC API; return any printers found in the response."""
    url = f"{base}/rpc/api/xmlrpc"
    body = (
        '<?xml version="1.0"?><methodCall>'
        '<methodName>api.listPrinters</methodName><params>'
        f'<param><value><string>{password}</string></value></param>'
        '<param><value><int>0</int></value></param>'
        '<param><value><int>200</int></value></param>'
        '</params></methodCall>'
    ).encode()
    _dbg(f"POST {url} (XML-RPC listPrinters)")
    req = urllib.request.Request(url, data=body,
                                  headers={"Content-Type": "text/xml"})
    try:
        with opener.open(req, timeout=10) as r:
            xml = r.read().decode("utf-8", errors="replace")
            _dbg(f"  → {r.status} ({len(xml)} bytes)")
            return [_printer_from_match(m) for m in TOKEN_RE.finditer(xml)]
    except Exception as e:
        _dbg(f"  → 0 ({e})")
        return []


def _dedup(printers: list[dict]) -> list[dict]:
    seen: set[tuple] = set()
    result = []
    for p in printers:
        key = (p["server"], p["name"], p["user_id"])
        if key not in seen:
            seen.add(key)
            result.append(p)
    return result


# ── PaperCut API ──────────────────────────────────────────────────────────────

def _printer_from_match(m: re.Match) -> dict:
    scheme, host, raw_name, uid, token = m.groups()
    return {
        "name":    raw_name.replace("+", " ").replace("%2b", "+").replace("~2b", "+"),
        "server":  host.split(":")[0],
        "port":    IPP_SSL_PORT if scheme == "https" else IPP_PORT,
        "scheme":  scheme,
        "user_id": int(uid),
        "token":   token,
    }


def fetch_printers(server: str, username: str, password: str) -> list[dict]:
    for scheme, port in [("https", API_SSL_PORT), ("http", API_PORT)]:
        base = f"{scheme}://{server}:{port}"
        jar = http.cookiejar.CookieJar()
        opener = _make_opener(jar)

        # Step 1: form POST login — follows the redirect and lands on the user page
        login_html = _form_login(opener, base, username, password)
        jsid = next((c.value for c in jar if c.name == "JSESSIONID"), None)
        _dbg(f"  JSESSIONID={jsid}")

        # Step 2: collect HTML from the login response + additional authenticated pages
        all_html = login_html
        for path in ("/user", "/user/printers"):
            _, html = _session_get(opener, base + path)
            all_html += html

        printers = _dedup([_printer_from_match(m) for m in TOKEN_RE.finditer(all_html)])
        if printers:
            return printers

        # Step 3: XML-RPC fallback
        printers = _xmlrpc_printers(opener, base, password)
        if printers:
            return _dedup(printers)

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


def _cups_papercut_printers() -> list[str]:
    """Return names of CUPS printers whose device URI is a PaperCut IPP endpoint."""
    result = subprocess.run(["lpstat", "-v"], capture_output=True, text=True)
    return [
        m.group(1)
        for line in result.stdout.splitlines()
        if (m := _CUPS_PC_RE.match(line))
    ]


def install_printers(printers: list[dict]) -> None:
    if os.geteuid() != 0:
        print("Run with sudo to install printers.")
        sys.exit(1)
    if not _cups_ok():
        print("CUPS not found — install it first:")
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


def remove_printers(names: list[str]) -> None:
    if os.geteuid() != 0:
        print("Run with sudo to remove printers.")
        sys.exit(1)
    ok = 0
    for name in names:
        print(f"  Removing {name}...", end=" ", flush=True)
        if _run("lpadmin", "-x", name):
            print("removed")
            ok += 1
        else:
            print("FAILED")
    print(f"\n{ok}/{len(names)} printer(s) removed.")


# ── main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--server", metavar="HOST/IP",
                        help="PaperCut server address (skip auto-discovery)")
    parser.add_argument("--remove", action="store_true",
                        help="remove PaperCut printers previously installed by this tool")
    parser.add_argument("--pcap", metavar="FILE", help=argparse.SUPPRESS)
    parser.add_argument("--debug", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()

    global _DEBUG
    _DEBUG = args.debug

    if args.pcap:
        printers = fetch_from_pcap(args.pcap)
        if not printers:
            print("No printers found in capture.")
            sys.exit(1)
        for p in printers:
            print(f"  {p['name']}")
        print()
        install_printers(printers)
        return

    if args.remove:
        names = _cups_papercut_printers()
        if not names:
            print("No PaperCut printers found in CUPS.")
            sys.exit(0)
        print(f"Removing {len(names)} PaperCut printer(s):")
        for n in names:
            print(f"  {n}")
        print()
        remove_printers(names)
        return

    server = args.server
    if not server:
        server = discover_server()
    if not server:
        print()
        print("See TROUBLESHOOTING.md if you need help finding your server address.")
        server = input("Server IP or hostname: ").strip()
    if not server:
        print("No server found. Use --server <ip> to specify it manually, or see TROUBLESHOOTING.md.")
        sys.exit(1)

    username = input("Username: ").strip()
    password = getpass.getpass("Password: ")

    print("Fetching printer list...", end=" ", flush=True)
    printers = fetch_printers(server, username, password)
    if not printers:
        print("failed.")
        print("Could not fetch printers — check credentials and server address.")
        sys.exit(1)
    print(f"{len(printers)} printer(s) found\n")

    for p in printers:
        print(f"  {p['name']}")
    print()
    install_printers(printers)


if __name__ == "__main__":
    main()
