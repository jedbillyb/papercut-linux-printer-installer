#!/usr/bin/env python3
"""
papercut.py - Install PaperCut printers on Linux

Discovers your school's PaperCut server automatically and adds all printers
to CUPS in one shot.

Usage:
    sudo python3 papercut.py
    sudo python3 papercut.py --server 10.10.5.19
    sudo python3 papercut.py --finishing --driver ~/Downloads/driver.zip
    sudo python3 papercut.py --remove

Help, bug reports and "it does not work on my school's setup" are all welcome:
https://github.com/jedbillyb/papercut-linux-printer-installer/issues
jedbillyb@gmail.com  |  https://jedbillyb.com
"""

import argparse
import difflib
import ipaddress
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

# Matches PaperCut IPP device URIs registered in CUPS — covers both the
# bare ipp/ipps backends used by older installs and the papercut-ipp(s)
# wrapper installed by current versions of this script.
_CUPS_PC_RE = re.compile(
    r"device for (\S+):\s+(?:papercut-)?ipps?://[^:]+:\d+/printers/"
)

WRAPPER_BACKEND_DIR = "/usr/lib/cups/backend"
WRAPPER_BACKEND_NAMES = ("papercut-ipp", "papercut-ipps")

# Shell script installed as /usr/lib/cups/backend/papercut-ipp and
# papercut-ipps.  CUPS picks a backend by URI scheme; the wrapper
# rewrites PPD-style options the IPP backend would otherwise pass through
# verbatim (PageSize=A3, Duplex=DuplexNoTumble) into the IPP attributes
# PaperCut Mobility Print actually honours (media=ISO_A3, sides=...).
#
# Without this, jobs from the GTK/Firefox print dialog get `PageSize A3`
# in the IPP request — the server doesn't recognise that name and prints
# at its default size (A4).  Direct `lp -o media=ISO_A3` works because
# `media` *is* a real IPP attribute.
_WRAPPER_BACKEND_SCRIPT = r"""#!/bin/sh
# Auto-installed by papercut.py — do not edit; re-run the installer to update.
self=$(basename "$0")
real=${self#papercut-}

if [ $# -eq 0 ]; then
    echo "network $self \"Unknown\" \"PaperCut option-translating wrapper ($real)\""
    exit 0
fi

job=$1 user=$2 title=$3 copies=$4 options=$5
shift 5

ppd="/etc/cups/ppd/${PRINTER}.ppd"

map_pagesize() {
    val=$1
    if [ -r "$ppd" ]; then
        m=$(awk -v v="$val" '
            /^\*cupsPageSizeName / {
                k=$2; sub(/:$/,"",k)
                gsub(/"/,"",$3)
                if (k==v) { print $3; exit }
            }' "$ppd")
        if [ -n "$m" ]; then printf %s "$m"; return; fi
    fi
    printf %s "$val"
}

new=
for tok in $options; do
    case $tok in
        PageSize=*)
            new="$new media=$(map_pagesize "${tok#PageSize=}")"
            ;;
        Duplex=None|Duplex=False|Duplex=No)
            new="$new sides=one-sided"
            ;;
        Duplex=DuplexNoTumble)
            new="$new sides=two-sided-long-edge"
            ;;
        Duplex=DuplexTumble)
            new="$new sides=two-sided-short-edge"
            ;;
        *)
            new="$new $tok"
            ;;
    esac
done

DEVICE_URI=${DEVICE_URI#papercut-}
export DEVICE_URI

exec "/usr/lib/cups/backend/$real" "$job" "$user" "$title" "$copies" "${new# }" "$@"
"""

_PT_TO_IPU = 2540.0 / 72  # PostScript points → IPP 1/100-mm units

# Matches *cupsPageSizeName and *cupsIPPAttr media/PageSize lines respectively.
# Groups: (1) keyword prefix  (2) PPD size name  (3) `: "`  (4) current value  (5) `"`
_CUPS_PAGE_NAME_RE = re.compile(
    r'^(\*cupsPageSizeName\s+)(\S+?)(\s*:\s*")([^"]+)(")', re.MULTILINE
)
_CUPS_IPP_ATTR_RE = re.compile(
    r'^(\*cupsIPPAttr\s+media/PageSize\s+)(\S+?)(\s*:\s*")([^"]+)(")', re.MULTILINE
)


# ── IPP media-keyword helpers ─────────────────────────────────────────────────

def _query_media_map(server: str, port: int, printer: str) -> dict[str, tuple[int, int]]:
    """Return {server_keyword: (x_ipu, y_ipu)} from IPP get-printer-attributes.

    Zips media-supported keywords with media-col-database dimension entries,
    which PaperCut Mobility Print returns in the same order.  Returns {} on
    any failure so callers degrade gracefully.
    """
    try:
        result = subprocess.run(
            ["ipptool", "-tv", f"ipp://{server}:{port}/printers/{printer}",
             "/usr/share/cups/ipptool/get-printer-attributes.test"],
            capture_output=True, text=True, timeout=15,
        )
        out = result.stdout
        m_kw = re.search(r'media-supported\s+\([^)]+\)\s*=\s*([^\n]+)', out)
        m_db = re.search(r'media-col-database\s+\([^)]+\)\s*=\s*([^\n]+)', out)
        if not (m_kw and m_db):
            return {}
        keywords = [k.strip() for k in m_kw.group(1).split(',')]
        dims = re.findall(r'x-dimension=(\d+)\s+y-dimension=(\d+)', m_db.group(1))
        if len(dims) != len(keywords):
            return {}
        return {kw: (int(x), int(y)) for kw, (x, y) in zip(keywords, dims)}
    except Exception:
        return {}


def _match_ppd_to_server(ppd_content: str, media_map: dict[str, tuple[int, int]]) -> dict[str, str]:
    """Return {ppd_size_name: server_keyword} by matching *PaperDimension to media_map.

    Converts PPD points to IPP 1/100-mm units and finds the closest server
    keyword by Euclidean distance (orientation-agnostic).  Only accepts
    matches within 0.1 mm total error.
    """
    if not media_map:
        return {}
    dim_re = re.compile(
        r'^\*PaperDimension\s+(\S+?):\s+"([\d.]+)\s+([\d.]+)"', re.MULTILINE
    )
    out: dict[str, str] = {}
    for m in dim_re.finditer(ppd_content):
        ppd_name = m.group(1)
        w_ipu = round(float(m.group(2)) * _PT_TO_IPU)
        h_ipu = round(float(m.group(3)) * _PT_TO_IPU)
        best_kw, best_dist = None, float('inf')
        for kw, (x, y) in media_map.items():
            dist = min(abs(w_ipu - x) + abs(h_ipu - y),
                       abs(w_ipu - y) + abs(h_ipu - x))
            if dist < best_dist:
                best_dist, best_kw = dist, kw
        if best_dist < 10 and best_kw:
            out[ppd_name] = best_kw
    return out


# ── network helpers ───────────────────────────────────────────────────────────

def _default_gateway() -> str | None:
    try:
        result = subprocess.run(["ip", "route"], capture_output=True, text=True)
        m = re.search(r"default via (\S+)", result.stdout)
        return m.group(1) if m else None
    except Exception:
        return None


def _local_networks() -> list[ipaddress.IPv4Network]:
    """Return reachable subnets: interface networks plus explicitly routed prefixes."""
    nets: list[ipaddress.IPv4Network] = []
    try:
        result = subprocess.run(["ip", "addr"], capture_output=True, text=True)
        for m in re.finditer(r"inet (\d+\.\d+\.\d+\.\d+/\d+)", result.stdout):
            net = ipaddress.IPv4Interface(m.group(1)).network
            if not net.is_loopback and net not in nets:
                nets.append(net)
    except Exception:
        pass
    try:
        result = subprocess.run(["ip", "route"], capture_output=True, text=True)
        for line in result.stdout.splitlines():
            parts = line.split()
            if not parts or parts[0] in ("default", "broadcast", "local"):
                continue
            try:
                net = ipaddress.IPv4Network(parts[0], strict=False)
                if not net.is_loopback and net not in nets and net.prefixlen >= 16:
                    nets.append(net)
            except ValueError:
                pass
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
    for t in MDNS_TYPES:
        ServiceBrowser(zc, t, Listener())
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

    _pc_ports = (API_PORT, API_SSL_PORT, IPP_PORT, IPP_SSL_PORT)
    for hostname in PAPERCUT_HOSTNAMES:
        for domain in domains:
            fqdn = f"{hostname}.{domain}"
            ip = _dns_query(gateway, fqdn)
            if ip and ip != gateway:
                if any(_port_open(ip, p) for p in _pc_ports):
                    return ip
            try:
                ip = socket.gethostbyname(fqdn)
                if any(_port_open(ip, p) for p in _pc_ports):
                    return ip
            except Exception:
                pass
    return None


_SPINNER = [".  ", ".. ", "...", ".. "]

def _scan_hosts(hosts: list, label: str) -> str | None:
    """Port-scan a list of IPs for PaperCut ports. Returns first hit or None."""
    prefix = f"    scanning {label} ({len(hosts)} hosts)"

    def check(ip):
        for port in (API_PORT, API_SSL_PORT, IPP_PORT, IPP_SSL_PORT):
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
            for port in (API_PORT, API_SSL_PORT, IPP_PORT, IPP_SSL_PORT):
                if _port_open(host, port, timeout=0.2):
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


# ── Mobility Print API ────────────────────────────────────────────────────────

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


def fetch_printers(server: str) -> list[dict]:
    for scheme, port in [("http", IPP_PORT), ("https", IPP_SSL_PORT)]:
        url = f"{scheme}://{server}:{port}/printers"
        _dbg(f"GET {url}")
        ctx = _ssl_ctx() if scheme == "https" else None
        try:
            with urllib.request.urlopen(url, context=ctx, timeout=10) as r:
                items = json.loads(r.read())
                _dbg(f"  → {r.status} ({len(items)} printers)")
                return [
                    {"name": item["name"], "server": server,
                     "port": port, "scheme": scheme}
                    for item in items
                    if item.get("name")
                ]
        except urllib.error.HTTPError as e:
            _dbg(f"  → {e.code}")
        except Exception as e:
            _dbg(f"  → 0 ({e})")
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
    # Route through the papercut-ipp wrapper backend so PPD-style options
    # (PageSize, Duplex) get translated to IPP keywords PaperCut Mobility
    # Print recognises.  See _WRAPPER_BACKEND_SCRIPT for the why.
    scheme = "papercut-ipps" if p["scheme"] == "https" else "papercut-ipp"
    name   = p["name"].replace(" ", "+")
    uri    = f"{scheme}://{p['server']}:{p['port']}/printers/{name}"
    if p.get("token"):
        uri += f"/users/{p['user_id']}/{p['token']}"
    return uri


def _install_wrapper_backend() -> None:
    """Write /usr/lib/cups/backend/papercut-ipp(s) if missing or outdated."""
    if not os.path.isdir(WRAPPER_BACKEND_DIR):
        return
    for name in WRAPPER_BACKEND_NAMES:
        path = os.path.join(WRAPPER_BACKEND_DIR, name)
        try:
            with open(path) as f:
                if f.read() == _WRAPPER_BACKEND_SCRIPT:
                    continue
        except FileNotFoundError:
            pass
        except OSError:
            pass
        with open(path, "w") as f:
            f.write(_WRAPPER_BACKEND_SCRIPT)
        os.chmod(path, 0o700)  # CUPS requires backends to be 0700 and root-owned


def _cups_name(p: dict) -> str:
    return p["name"].replace(" ", "-")


def _run(*cmd: str) -> bool:
    return subprocess.run(cmd, capture_output=True, text=True).returncode == 0


def _cups_ok() -> bool:
    return _run("which", "lpadmin")


def _apply_ppd_patches(content: str, ppd_to_server: dict[str, str] | None = None) -> str:
    """Return PPD content with required patches applied.

    Patches applied:
    1. cupsFilter2 direct PDF pass-through (prevents pdftopdf invocation).
    2. cupsPageSizeName + cupsIPPAttr media/PageSize values replaced with the
       server's actual media-supported keyword (e.g. ISO_A3) so CUPS sends the
       keyword the server advertises rather than a PWG self-describing name it
       doesn't recognise.  Requires ppd_to_server from _match_ppd_to_server().
    """
    if "application/pdf application/pdf" not in content:
        content += '\n*cupsFilter2: "application/pdf application/pdf 0 -"\n'

    if ppd_to_server:
        def _rewrite(m: re.Match) -> str:
            server_kw = ppd_to_server.get(m.group(2))
            if server_kw and server_kw != m.group(4):
                return m.group(1) + m.group(2) + m.group(3) + server_kw + m.group(5)
            return m.group(0)
        content = _CUPS_PAGE_NAME_RE.sub(_rewrite, content)
        content = _CUPS_IPP_ATTR_RE.sub(_rewrite, content)

    return content


def _patch_ppd_pdf(cups_name: str, server: str, port: int, ipp_printer: str) -> bool:
    """Patch the printer's PPD in CUPS with all required fixes.

    Writes the patched PPD via lpadmin -P. The caller is responsible
    for sending SIGHUP to cupsd afterwards to force a re-parse, since
    cupsd caches the parsed PPD in memory and lpadmin -P alone does
    not reliably trigger a reload.
    """
    ppd_path = f"/etc/cups/ppd/{cups_name}.ppd"
    tmp_path = f"/tmp/papercut-{cups_name}.ppd"
    try:
        with open(ppd_path) as f:
            original = f.read()
        media_map = _query_media_map(server, port, ipp_printer)
        ppd_to_server = _match_ppd_to_server(original, media_map)
        patched = _apply_ppd_patches(original, ppd_to_server)
        if patched == original:
            return False
        with open(tmp_path, "w") as f:
            f.write(patched)
        ok = _run("lpadmin", "-p", cups_name, "-P", tmp_path)
        try:
            os.unlink(tmp_path)
        except Exception:
            pass
        return ok
    except Exception:
        return False


def _device_uri_info(cups_name: str) -> tuple[str, int, str] | None:
    """Return (server, port, ipp_printer) from CUPS lpstat -v, or None."""
    result = subprocess.run(["lpstat", "-v", cups_name], capture_output=True, text=True)
    m = re.search(r'(?:papercut-)?ipps?://([^:/]+):(\d+)/printers/([^/\s]+)', result.stdout)
    return (m.group(1), int(m.group(2)), m.group(3)) if m else None


def _test_ppd(path: str) -> None:
    """Dry-run PPD patch logic on an arbitrary file and print a unified diff.

    Exits 0 if patches were applied, 1 if the file already looks clean.

    To obtain a PPD for testing:
      lpstat -l -p | grep PPD       # shows the PPD path for each printer
      ls /etc/cups/ppd/             # or browse installed PPDs directly
    """
    try:
        with open(path) as f:
            original = f.read()
    except OSError as e:
        print(f"Error reading {path}: {e}", file=sys.stderr)
        sys.exit(2)

    ppd_to_server: dict[str, str] = {}
    basename = os.path.basename(path)
    if basename.endswith(".ppd"):
        info = _device_uri_info(basename[:-4])
        if info:
            server, port, ipp_printer = info
            ppd_to_server = _match_ppd_to_server(original, _query_media_map(server, port, ipp_printer))
    if not ppd_to_server:
        print("Note: could not query server; only cupsFilter2 patch shown.", file=sys.stderr)

    patched = _apply_ppd_patches(original, ppd_to_server)
    if patched == original:
        print("PPD already clean — no patches needed.")
        sys.exit(1)

    diff = difflib.unified_diff(
        original.splitlines(keepends=True),
        patched.splitlines(keepends=True),
        fromfile=f"{path} (original)",
        tofile=f"{path} (patched)",
    )
    sys.stdout.writelines(diff)
    sys.exit(0)


def _cups_papercut_printers() -> list[str]:
    """Return names of CUPS printers whose device URI is a PaperCut IPP endpoint."""
    result = subprocess.run(["lpstat", "-v"], capture_output=True, text=True)
    return [
        m.group(1)
        for line in result.stdout.splitlines()
        if (m := _CUPS_PC_RE.match(line))
    ]


def install_printers(printers: list[dict], dry_run: bool = False) -> None:
    if not dry_run:
        if os.geteuid() != 0:
            print("Run with sudo to install printers.")
            sys.exit(1)
        if not _cups_ok():
            print("CUPS not found — install it first:")
            print("  Ubuntu/Debian : sudo apt install cups cups-ipp-utils")
            print("  Arch          : sudo pacman -S cups")
            print("  Fedora        : sudo dnf install cups cups-ipptool")
            print("  Void          : sudo xbps-install cups")
            sys.exit(1)

    if not dry_run:
        _install_wrapper_backend()

    ok = skipped = ppd_patched = 0
    patched_names: list[str] = []
    seen_names: dict[str, str] = {}  # cups name → original printer name
    for p in printers:
        name = _cups_name(p)
        if name in seen_names:
            print(f"  (warn)   {p['name']!r} and {seen_names[name]!r} both normalize to"
                  f" {name!r} — skipping {p['name']!r}")
            continue
        seen_names[name] = p["name"]

        already = _run("lpstat", "-p", name)

        ipp_printer = p["name"].replace(" ", "+")

        if dry_run:
            if already:
                ppd_path = f"/etc/cups/ppd/{name}.ppd"
                ppd_needed = False
                try:
                    with open(ppd_path) as f:
                        content = f.read()
                    media_map = _query_media_map(p["server"], p["port"], ipp_printer)
                    ppd_to_server = _match_ppd_to_server(content, media_map)
                    ppd_needed = _apply_ppd_patches(content, ppd_to_server) != content
                except (FileNotFoundError, PermissionError):
                    pass
                suffix = " (would patch PPD)" if ppd_needed else ""
                print(f"  (would skip)    {name}... already installed{suffix}")
            else:
                print(f"  (would install) {name}...")
            continue

        if already:
            # Re-set -v to migrate older installs from bare ipp:// to the
            # papercut-ipp wrapper scheme.  Harmless if already correct.
            _run("lpadmin", "-p", name, "-v", _ipp_url(p),
                 "-o", "auth-info-required=username,password")
            patched = _patch_ppd_pdf(name, p["server"], p["port"], ipp_printer)
            suffix = " (PPD patched)" if patched else ""
            print(f"  (skip)   {name}... already installed{suffix}")
            skipped += 1
            if patched:
                ppd_patched += 1
                patched_names.append(name)
            continue
        print(f"  (new)    {name}...", end=" ", flush=True)
        if _run("lpadmin", "-p", name, "-v", _ipp_url(p),
                "-m", "everywhere", "-E", "-D", p["name"],
                "-o", "auth-info-required=username,password"):
            _run("cupsenable", name)
            _run("cupsaccept", name)
            if _patch_ppd_pdf(name, p["server"], p["port"], ipp_printer):
                ppd_patched += 1
                patched_names.append(name)
            print("done")
            ok += 1
        else:
            print("FAILED")

    if dry_run:
        return

    if patched_names:
        # cupsd parses PPDs into an in-memory MIME database on startup; lpadmin -P
        # updates the file but the running daemon keeps the stale parse until SIGHUP.
        print("Reloading cupsd...", end=" ", flush=True)
        subprocess.run(["pkill", "-HUP", "cupsd"], check=False)
        time.sleep(0.5)
        failed_verify = []
        verification_skipped = False
        for name in patched_names:
            try:
                result = subprocess.run(
                    ["ipptool", "-tv", f"ipp://localhost:631/printers/{name}",
                     "/usr/share/cups/ipptool/get-printer-attributes.test"],
                    capture_output=True, text=True,
                )
                if "application/pdf" not in result.stdout:
                    failed_verify.append(name)
            except FileNotFoundError:
                print("\nWarning: ipptool not found — skipping PPD verification.")
                print("If jobs fail with 'document format not supported', restart CUPS manually:")
                print("  sudo systemctl restart cups   # systemd")
                print("  sudo sv restart cupsd         # runit/Void")
                verification_skipped = True
                break
        if not failed_verify and not verification_skipped:
            print("done")
        if failed_verify:
            print(f"\nError: PPD patch did not take effect for: {', '.join(failed_verify)}")
            print("Try restarting CUPS manually and re-running the script:")
            print("  sudo systemctl restart cups   # systemd")
            print("  sudo sv restart cupsd         # runit/Void")
            sys.exit(1)

    ready = ok + skipped
    print(f"\n{ready}/{len(printers)} printers ready.")
    if ready:
        print("Open any application and select a printer to test.")


def _remove_wrapper_backends() -> None:
    """Remove papercut-ipp(s) backend scripts if no PaperCut printers remain."""
    if _cups_papercut_printers():
        return
    removed = []
    for name in WRAPPER_BACKEND_NAMES:
        path = os.path.join(WRAPPER_BACKEND_DIR, name)
        try:
            os.unlink(path)
            removed.append(name)
        except FileNotFoundError:
            pass
        except OSError as e:
            print(f"  Warning: could not remove {path}: {e}")
    if removed:
        print(f"  Removed backend wrapper(s): {', '.join(removed)}")


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
    _remove_wrapper_backends()


# ── vendor drivers and finishing queues ───────────────────────────────────────
#
# Mobility Print cannot staple.  Its capability model, served verbatim at
# http://<server>:9163/printers, is only:
#
#     {mediaSizes, resolutions, color, duplex}
#
# It inherits that schema from Google Cloud Print, which has no finishing
# field at all, so an IPP "finishings" attribute sent to a Mobility Print
# queue is accepted and then silently dropped.  No amount of picking
# different finishings enums changes this.
#
# Windows staples because PaperCut Print Deploy hands it a real vendor
# driver, which writes the finishing command into the print data itself as
# PJL (@PJL SET STAPLE=TOPLEFT) before the job ever reaches the server.
# The same trick works on Linux: install the vendor PPD plus its filters,
# then point a queue at the server's LPD port instead of Mobility Print.
#
# The vendor driver is NOT shipped with this tool.  Vendor licences
# generally forbid public redistribution, so you supply the package.

LPD_PORT = 515

# Queues this tool creates for finishing are named "<Printer>-Finishing" and
# sit alongside the plain IPP queue rather than replacing it.  The IPP queue
# stays the better default: driverless, no vendor binaries, nothing to rot.
FINISHING_SUFFIX = "-Finishing"

VENDOR_PPD_DIR  = "/usr/share/ppd/papercut-vendor"
CUPS_FILTER_DIR = "/usr/lib/cups/filter"

_CUPS_FINISHING_RE = re.compile(
    r"device for (\S+" + re.escape(FINISHING_SUFFIX) + r"):\s+lpd://"
)

# Vendors namespace finishing options differently (FFStaple on Fujifilm,
# StapleLocation on Ricoh), so match the meaningful part of the keyword
# rather than a fixed list of names.
_FINISHING_KEYWORDS = ("staple", "punch", "finish", "fold", "stitch", "bind")

_PPD_OPENUI_RE     = re.compile(r"^\*OpenUI\s+\*([A-Za-z0-9_]+)", re.M)
_PPD_CUPSFILTER_RE = re.compile(r'^\*cupsFilter2?:\s*"([^"]+)"', re.M)


def _ar_members(path: str) -> dict[str, bytes]:
    """Parse a .deb (ar archive) without needing binutils installed."""
    members: dict[str, bytes] = {}
    with open(path, "rb") as fh:
        if fh.read(8) != b"!<arch>\n":
            return members
        while True:
            header = fh.read(60)
            if len(header) < 60:
                break
            name = header[0:16].decode("ascii", "replace").strip().rstrip("/")
            try:
                size = int(header[48:58].decode("ascii").strip())
            except ValueError:
                break
            members[name] = fh.read(size)
            if size % 2:
                fh.read(1)
    return members


def _extract_vendor_package(path: str, dest: str) -> bool:
    """Unpack a vendor driver package into dest.

    Accepts the .zip a vendor portal hands you, a .deb, or a directory that
    has already been unpacked.  Returns True if anything was extracted.
    """
    import shutil
    import tarfile
    import zipfile

    if os.path.isdir(path):
        shutil.copytree(path, dest, dirs_exist_ok=True)
        return True

    lowered = path.lower()

    if lowered.endswith(".zip"):
        staging = os.path.join(dest, "_zip")
        os.makedirs(staging, exist_ok=True)
        with zipfile.ZipFile(path) as zf:
            zf.extractall(staging)
        # Vendor zips wrap the real package; recurse into the first .deb.
        for root, _dirs, files in os.walk(staging):
            for fname in files:
                if fname.lower().endswith(".deb"):
                    return _extract_vendor_package(os.path.join(root, fname), dest)
        print(f"  No .deb inside {os.path.basename(path)}.")
        print("  If you downloaded the Red Hat package, fetch the Ubuntu/Debian one instead.")
        return False

    if lowered.endswith(".deb"):
        members = _ar_members(path)
        data = next((v for k, v in members.items() if k.startswith("data.tar")), None)
        if data is None:
            return False
        blob = os.path.join(dest, "data.tar")
        with open(blob, "wb") as fh:
            fh.write(data)
        with tarfile.open(blob) as tf:
            tf.extractall(dest)
        os.unlink(blob)
        return True

    print(f"  Unsupported package type: {os.path.basename(path)}")
    print("  Supply the vendor's .zip or .deb, or a directory you unpacked yourself.")
    return False


def _find_driver_files(root: str) -> tuple[str | None, list[str]]:
    """Locate the PPD and any CUPS filters inside an unpacked vendor package."""
    ppd: str | None = None
    filters: list[str] = []
    for dirpath, _dirs, files in os.walk(root):
        for fname in files:
            full = os.path.join(dirpath, fname)
            if fname.lower().endswith(".ppd") and ppd is None:
                ppd = full
            elif os.sep + "cups" + os.sep + "filter" + os.sep in full + os.sep:
                filters.append(full)
    return ppd, filters


def _ppd_finishing_options(ppd_path: str) -> dict[str, list[str]]:
    """Return the finishing options a PPD exposes, as {option: [values]}."""
    try:
        with open(ppd_path, encoding="utf-8", errors="replace") as fh:
            content = fh.read()
    except OSError:
        return {}

    found: dict[str, list[str]] = {}
    for option in _PPD_OPENUI_RE.findall(content):
        if not any(k in option.lower() for k in _FINISHING_KEYWORDS):
            continue
        values = re.findall(
            r"^\*" + re.escape(option) + r"\s+([A-Za-z0-9_]+)", content, re.M
        )
        if values:
            found[option] = values
    return found


def _ppd_pdf_filter(ppd_path: str) -> str | None:
    """Return the filter a PPD uses for application/pdf input."""
    try:
        with open(ppd_path, encoding="utf-8", errors="replace") as fh:
            content = fh.read()
    except OSError:
        return None
    for rule in _PPD_CUPSFILTER_RE.findall(content):
        parts = rule.split()
        if parts and parts[0] == "application/pdf" and parts[-1] not in ("-", ""):
            return parts[-1]
    return None


def _blank_pdf() -> bytes:
    """A minimal one-page A4 PDF, used to preview filter output."""
    objs = [
        b"<</Type/Catalog/Pages 2 0 R>>",
        b"<</Type/Pages/Kids[3 0 R]/Count 1>>",
        b"<</Type/Page/Parent 2 0 R/MediaBox[0 0 595 842]/Resources<<>>>>",
    ]
    out = bytearray(b"%PDF-1.4\n")
    offsets: list[int] = []
    for i, body in enumerate(objs, start=1):
        offsets.append(len(out))
        out += b"%d 0 obj" % i + body + b"endobj\n"
    xref = len(out)
    out += b"xref\n0 %d\n" % (len(objs) + 1)
    out += b"0000000000 65535 f \n"
    for off in offsets:
        out += b"%010d 00000 n \n" % off
    out += b"trailer<</Size %d/Root 1 0 R>>\nstartxref\n%d\n%%%%EOF\n" % (
        len(objs) + 1, xref)
    return bytes(out)


def _pjl_preview(ppd_path: str, filter_name: str, options: str,
                 printer: str) -> list[str]:
    """Run a vendor filter on a blank page and return the @PJL lines it emits.

    This proves a finishing option actually reaches the printer without
    printing anything.  If the staple command is absent here it will be
    absent on paper too.

    Two traps, both of which show up as an immediate segfault:

    * the filter expects the environment cupsd sets up, hence the env block
    * PRINTER must name a queue that actually exists in CUPS.  The filter
      looks it up and dereferences the result without checking, so a
      placeholder name crashes it.  Call this only after the queue is made.
    """
    import tempfile

    binary = os.path.join(CUPS_FILTER_DIR, filter_name)
    if not os.path.isfile(binary):
        return []

    env = dict(os.environ)
    env.update({
        "PPD": ppd_path,
        "PRINTER": printer,
        "CUPS_SERVERROOT": "/etc/cups",
        "CUPS_DATADIR": "/usr/share/cups",
        "CUPS_CACHEDIR": "/var/cache/cups",
        "CUPS_STATEDIR": "/run/cups",
        "RIP_CACHE": "8m",
        "SOFTWARE": "CUPS/2.4",
        "CHARSET": "utf-8",
        "CONTENT_TYPE": "application/pdf",
        "TMPDIR": tempfile.gettempdir(),
    })

    with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tmp:
        tmp.write(_blank_pdf())
        pdf_path = tmp.name

    try:
        proc = subprocess.run(
            [binary, "1", os.environ.get("USER", "root"), "pjl-preview", "1",
         options, pdf_path],
            capture_output=True, timeout=60, env=env,
        )
    except (subprocess.TimeoutExpired, OSError):
        return []
    finally:
        os.unlink(pdf_path)

    head = proc.stdout[:8192].decode("latin-1", "replace")
    return [ln.strip() for ln in head.splitlines() if "@PJL" in ln]


def _install_driver_files(ppd_src: str, filters: list[str]) -> str | None:
    """Copy a vendor PPD and its filters into place.  Returns the PPD path."""
    import shutil

    os.makedirs(VENDOR_PPD_DIR, exist_ok=True)
    ppd_dest = os.path.join(VENDOR_PPD_DIR, os.path.basename(ppd_src))
    try:
        shutil.copy2(ppd_src, ppd_dest)
        os.chmod(ppd_dest, 0o644)
        for filt in filters:
            dest = os.path.join(CUPS_FILTER_DIR, os.path.basename(filt))
            shutil.copy2(filt, dest)
            os.chmod(dest, 0o755)
    except OSError as exc:
        print(f"  Could not install driver files: {exc}")
        return None
    return ppd_dest


def _lpd_uri(server: str, queue: str, user: str) -> str:
    # LPD has no authentication.  The username in the URI is the only thing
    # PaperCut can attribute the job to, so it must be the PaperCut login,
    # not the local Linux user.
    from urllib.parse import quote
    return f"lpd://{quote(user, safe='')}@{server}:{LPD_PORT}/{quote(queue, safe='')}"


def _cups_finishing_printers() -> list[str]:
    """Return names of finishing queues previously installed by this tool."""
    result = subprocess.run(["lpstat", "-v"], capture_output=True, text=True)
    return [
        m.group(1)
        for line in result.stdout.splitlines()
        if (m := _CUPS_FINISHING_RE.match(line))
    ]


def install_finishing_queues(printers: list[dict], driver: str, user: str,
                             dry_run: bool = False) -> None:
    """Install LPD queues that render through a vendor PPD so finishing works."""
    import tempfile

    if not dry_run and os.geteuid() != 0:
        print("Run with sudo to install finishing queues.")
        sys.exit(1)

    workdir = tempfile.mkdtemp(prefix="papercut-driver-")
    print(f"Unpacking driver: {os.path.basename(driver)}...", end=" ", flush=True)

    if driver.lower().endswith(".ppd"):
        ppd_src, filters = driver, []
        print("PPD supplied directly")
    else:
        if not _extract_vendor_package(driver, workdir):
            print("failed.")
            sys.exit(1)
        ppd_src, filters = _find_driver_files(workdir)
        if not ppd_src:
            print("failed.")
            print("No .ppd found in that package.")
            sys.exit(1)
        print(f"found {os.path.basename(ppd_src)} + {len(filters)} filter(s)")

    finishing = _ppd_finishing_options(ppd_src)
    if not finishing:
        print()
        print("That PPD exposes no finishing options, so it cannot staple.")
        print("Check you downloaded the driver for this exact printer model.")
        sys.exit(1)

    print("\nFinishing options this driver supports:")
    for option, values in finishing.items():
        print(f"  {option:<20} {' '.join(values)}")

    if dry_run:
        print(f"\nWould install {len(printers)} finishing queue(s). No changes made.")
        return

    ppd_path = _install_driver_files(ppd_src, filters)
    if not ppd_path:
        sys.exit(1)

    print()
    ok = 0
    first_queue = ""
    for p in printers:
        name = _cups_name(p) + FINISHING_SUFFIX
        uri  = _lpd_uri(p["server"], p["name"], user)
        print(f"  {name}...", end=" ", flush=True)
        if _run("lpadmin", "-p", name, "-v", uri, "-P", ppd_path,
                "-E", "-D", f"{p['name']} (finishing)",
                "-o", "printer-is-shared=false"):
            _run("cupsenable", name)
            _run("cupsaccept", name)
            print("done")
            ok += 1
            first_queue = first_queue or name
        else:
            print("FAILED")

    # Prove the driver emits a finishing command, without printing a page.
    # Has to happen after a queue exists; see _pjl_preview.
    filter_name = _ppd_pdf_filter(ppd_src)
    if first_queue and filter_name and filters:
        option, values = next(iter(finishing.items()))
        chosen = next((v for v in values if v.lower() not in ("none", "off")), None)
        if chosen:
            lines = _pjl_preview(ppd_path, filter_name, f"{option}={chosen}",
                                 first_queue)
            hits = [ln for ln in lines
                    if any(k in ln.lower() for k in _FINISHING_KEYWORDS)]
            print(f"\nChecking {option}={chosen} reaches the printer:")
            if hits:
                for ln in hits:
                    print(f"  {ln}")
                print("  Driver emits the finishing command.")
            else:
                print("  No finishing command found in the driver output.")
                print("  The queues still work for normal printing, but this")
                print("  driver may not drive the finisher on this model.")

    option = next(iter(finishing))
    values = [v for v in finishing[option] if v.lower() not in ("none", "off")]
    print(f"\n{ok}/{len(printers)} finishing queue(s) installed.")
    print("\nPrint with finishing:")
    print(f"  lp -d <Printer>{FINISHING_SUFFIX} -o {option}={values[0] if values else '<value>'} "
          "-o Duplex=DuplexNoTumble <file>")
    print("\nIf a job hangs on \"Connecting to printer\", the LPD port is not")
    print(f"reachable. Check port {LPD_PORT} is open on the server, and that a VPN")
    print("is not routing the server address into a tunnel.")


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
    parser.add_argument("--list", action="store_true",
                        help="list PaperCut printers currently installed by this tool and exit")
    parser.add_argument("--dry-run", action="store_true",
                        help="show what would be installed/skipped without modifying CUPS")
    parser.add_argument("--finishing", action="store_true",
                        help="also install LPD queues that can staple/hole-punch "
                             "(needs --driver; Mobility Print cannot do finishing)")
    parser.add_argument("--driver", metavar="PATH",
                        help="vendor driver package (.zip/.deb), unpacked directory, "
                             "or a .ppd - required by --finishing")
    parser.add_argument("--papercut-user", metavar="NAME",
                        help="PaperCut username for finishing queues; LPD has no auth, "
                             "so this is what the job is billed to")
    parser.add_argument(
        "--test-ppd", metavar="FILE",
        help=(
            "test PPD patch logic on FILE without modifying CUPS (no sudo needed). "
            "Prints a unified diff and exits 0 if patches were applied, 1 if already clean. "
            "To get a PPD: lpstat -l -p | grep PPD  or ls /etc/cups/ppd/"
        ),
    )
    parser.add_argument("--pcap", metavar="FILE", help=argparse.SUPPRESS)
    parser.add_argument("--debug", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()

    global _DEBUG
    _DEBUG = args.debug

    if args.driver and not args.finishing:
        print("--driver only applies with --finishing.")
        sys.exit(1)
    if args.finishing and not args.driver:
        print("--finishing needs --driver <path to your vendor driver package>.")
        print()
        print("This tool does not ship vendor drivers; their licences forbid")
        print("public redistribution. Download the Linux driver for your printer")
        print("model from the manufacturer, then pass the file here.")
        print("See README.md, section \"Stapling and hole punch\".")
        sys.exit(1)

    if args.test_ppd:
        _test_ppd(args.test_ppd)
        return

    if args.pcap:
        printers = fetch_from_pcap(args.pcap)
        if not printers:
            print("No printers found in capture.")
            sys.exit(1)
        for p in printers:
            print(f"  {p['name']}")
        print()
        install_printers(printers, dry_run=args.dry_run)
        return

    if args.list:
        for n in _cups_papercut_printers():
            print(n)
        for n in _cups_finishing_printers():
            print(f"{n} (finishing)")
        sys.exit(0)

    if args.remove:
        names = _cups_papercut_printers() + _cups_finishing_printers()
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

    print("Fetching printer list...", end=" ", flush=True)
    printers = fetch_printers(server)
    if not printers:
        print("failed.")
        print("Could not fetch printers — check server address.")
        sys.exit(1)
    print(f"{len(printers)} printer(s) found\n")

    for p in printers:
        print(f"  {p['name']}")
    print()
    install_printers(printers, dry_run=args.dry_run)

    if args.finishing:
        user = args.papercut_user
        if not user:
            user = input("PaperCut username (for finishing job accounting): ").strip()
        if not user:
            print("A PaperCut username is required for finishing queues.")
            sys.exit(1)
        print()
        install_finishing_queues(printers, args.driver, user, dry_run=args.dry_run)


if __name__ == "__main__":
    main()
