#!/usr/bin/env python3
"""
PaperCut printer token discovery tool.

Discovers printers either by authenticating with your school credentials
(recommended) or by extracting tokens from a network capture.

Usage:
    python3 discover.py --server 10.10.5.19 --username jed --password secret
    python3 discover.py --pcap capture.pcapng
    sudo python3 discover.py --live --interface eth0
"""

import argparse
import getpass
import json
import os
import re
import subprocess
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request
import ssl


DEFAULT_OUTPUT = "printers.json"
TOKEN_RE = re.compile(
    r"(https?)://([^/]+)/printers/([^/]+)/users/(\d+)/([0-9a-f]{64})"
)

# Known PaperCut Mobility Print auth endpoints, tried in order
AUTH_ENDPOINTS = [
    "/api/auth",
    "/api/v1/auth",
    "/auth",
    "/api/authenticate",
]
PRINTER_ENDPOINTS = [
    "/api/printers",
    "/api/v1/printers",
    "/printers",
]


def ssl_ctx() -> ssl.SSLContext:
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx


def post_json(url: str, data: dict) -> tuple[int, dict | None]:
    body = json.dumps(data).encode()
    req = urllib.request.Request(url, data=body,
                                  headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, context=ssl_ctx(), timeout=5) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, None
    except Exception:
        return 0, None


def get_json(url: str, token: str | None = None) -> tuple[int, dict | list | None]:
    headers = {}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, context=ssl_ctx(), timeout=5) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, None
    except Exception:
        return 0, None


def try_auth(base: str, username: str, password: str) -> tuple[str | None, dict | None]:
    """Try each known auth endpoint. Returns (auth_token, user_info) or (None, None)."""
    creds = {"username": username, "password": password}
    for ep in AUTH_ENDPOINTS:
        status, body = post_json(base + ep, creds)
        if status == 200 and body:
            print(f"  Auth endpoint: {ep}  [OK]")
            token = (body.get("token") or body.get("authToken") or
                     body.get("access_token") or body.get("sessionToken"))
            return token, body
        elif status not in (0, 404, 405):
            print(f"  Auth endpoint: {ep}  [{status}]")
    return None, None


def try_get_printers(base: str, token: str | None,
                     username: str, user_id: int | None) -> list[dict]:
    """Try each known printer-list endpoint."""
    for ep in PRINTER_ENDPOINTS:
        # Try with user ID in path if we have it
        paths = [ep]
        if user_id:
            paths.insert(0, f"{ep}/users/{user_id}")
        for path in paths:
            status, body = get_json(base + path, token)
            if status == 200 and body:
                print(f"  Printer endpoint: {path}  [OK]")
                return parse_printer_list(body, base, user_id)
    return []


def parse_printer_list(body: dict | list, base: str, user_id: int | None) -> list[dict]:
    """Parse whatever the server returns into our standard format."""
    printers = []
    items = body if isinstance(body, list) else body.get("printers", [])
    from urllib.parse import urlparse
    parsed = urlparse(base)

    for item in items:
        # Handle various response shapes
        name = item.get("name") or item.get("printerName") or item.get("displayName", "")
        token = item.get("token") or item.get("userToken") or item.get("authToken", "")
        uid = item.get("userId") or item.get("user_id") or user_id or 0

        # Some servers return full IPP URLs directly
        ipp_url = item.get("uri") or item.get("ippUri") or item.get("url", "")
        if ipp_url:
            m = TOKEN_RE.search(ipp_url)
            if m:
                scheme, host, raw_name, uid_str, tok = m.groups()
                printers.append({
                    "name": raw_name.replace("+", " "),
                    "server": host.split(":")[0],
                    "port": int(host.split(":")[1]) if ":" in host else (9164 if scheme == "https" else 9163),
                    "scheme": scheme,
                    "user_id": int(uid_str),
                    "token": tok,
                })
                continue

        if name and token:
            printers.append({
                "name": name,
                "server": parsed.hostname,
                "port": parsed.port or (9164 if parsed.scheme == "https" else 9163),
                "scheme": parsed.scheme,
                "user_id": int(uid),
                "token": token,
            })
    return printers


def discover_via_credentials(server: str, username: str, password: str) -> list[dict]:
    """Authenticate to PaperCut and fetch printer list."""
    # Try HTTPS first, fall back to HTTP
    for scheme, port in [("https", 9164), ("http", 9163)]:
        base = f"{scheme}://{server}:{port}"
        print(f"\nTrying {base}...")

        auth_token, user_info = try_auth(base, username, password)

        if user_info is None and scheme == "https":
            continue

        if user_info is None:
            print("Authentication failed - check username/password.")
            sys.exit(1)

        user_id = (user_info.get("userId") or user_info.get("id") or
                   user_info.get("user_id"))

        printers = try_get_printers(base, auth_token, username, user_id)

        if printers:
            return printers

        # Last resort: if auth gave us printer URLs directly
        if isinstance(user_info, dict):
            raw = json.dumps(user_info)
            printers = [{"name": m.group(3).replace("+", " "),
                         "server": m.group(2).split(":")[0],
                         "port": 9164 if m.group(1) == "https" else 9163,
                         "scheme": m.group(1),
                         "user_id": int(m.group(4)),
                         "token": m.group(5)}
                        for m in TOKEN_RE.finditer(raw)]
            if printers:
                return printers

    print("\nCouldn't fetch printer list automatically.")
    print("The server API may use a different format.")
    print("Try the pcap method instead: python3 discover.py --pcap capture.pcapng")
    sys.exit(1)


# ── pcap / live capture ───────────────────────────────────────────────────────

def run_tshark(args: list[str]) -> str:
    result = subprocess.run(["tshark"] + args, capture_output=True, text=True)
    if result.returncode != 0 and not result.stdout:
        print(f"tshark error: {result.stderr.strip()}", file=sys.stderr)
        sys.exit(1)
    return result.stdout


def extract_from_pcap(path: str) -> list[dict]:
    output = run_tshark(["-r", path, "-Y", "ipp", "-V"])
    return parse_pcap_output(output)


def parse_pcap_output(output: str) -> list[dict]:
    found = {}
    for m in TOKEN_RE.finditer(output):
        scheme, host, raw_name, user_id, token = m.groups()
        name = raw_name.replace("+", " ").replace("%2b", "+").replace("~2b", "+")
        key = (host.split(":")[0], name, user_id)
        if key not in found:
            found[key] = {
                "name": name,
                "server": host.split(":")[0],
                "port": 9164 if scheme == "https" else 9163,
                "scheme": scheme,
                "user_id": int(user_id),
                "token": token,
            }
    return list(found.values())


def live_capture(interface: str, duration: int) -> list[dict]:
    print(f"Capturing on {interface} for {duration}s - "
          "open the printer list on your Windows/Mac machine now...")
    with tempfile.NamedTemporaryFile(suffix=".pcapng", delete=False) as f:
        tmp = f.name
    try:
        subprocess.run(
            ["tshark", "-i", interface, "-a", f"duration:{duration}",
             "-f", "tcp port 9163 or tcp port 9164", "-w", tmp],
            check=True
        )
        return extract_from_pcap(tmp)
    finally:
        os.unlink(tmp)


# ── output ────────────────────────────────────────────────────────────────────

def save_config(printers: list[dict], path: str) -> None:
    with open(path, "w") as f:
        json.dump(printers, f, indent=2)
    print(f"\nSaved {len(printers)} printer(s) to {path}")
    print(f"Next step: sudo python3 install.py --config {path}")


def print_table(printers: list[dict]) -> None:
    print(f"\nFound {len(printers)} printer(s):\n")
    print(f"  {'Name':<25} {'Server':<35} {'User ID'}")
    print("  " + "-" * 70)
    for p in printers:
        print(f"  {p['name']:<25} {p['server']:<35} {p['user_id']}")


# ── CLI ───────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter
    )
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--server",   metavar="HOST/IP",
                      help="PaperCut server address (use with --username/--password)")
    mode.add_argument("--pcap",     metavar="FILE",
                      help="extract tokens from a .pcapng capture file")
    mode.add_argument("--live",     action="store_true",
                      help="capture live traffic (requires root + tshark)")

    parser.add_argument("--username", help="your school username")
    parser.add_argument("--password", help="your school password (prompted if omitted)")
    parser.add_argument("--interface", default="eth0",
                        help="network interface for --live (default: eth0)")
    parser.add_argument("--duration", type=int, default=30,
                        help="capture duration in seconds for --live (default: 30)")
    parser.add_argument("--output", default=DEFAULT_OUTPUT, metavar="FILE",
                        help=f"output JSON config file (default: {DEFAULT_OUTPUT})")
    parser.add_argument("--print", action="store_true", dest="print_only",
                        help="print to stdout instead of saving to file")
    args = parser.parse_args()

    if args.server:
        username = args.username or input("Username: ")
        password = args.password or getpass.getpass("Password: ")
        printers = discover_via_credentials(args.server, username, password)
    elif args.live:
        if os.geteuid() != 0:
            print("Live capture requires root (use sudo)")
            sys.exit(1)
        printers = live_capture(args.interface, args.duration)
    else:
        printers = extract_from_pcap(args.pcap)

    if not printers:
        print("No printers found.")
        sys.exit(1)

    print_table(printers)

    if args.print_only:
        print("\n" + json.dumps(printers, indent=2))
        return

    save_config(printers, args.output)


if __name__ == "__main__":
    main()
