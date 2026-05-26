#!/usr/bin/env python3
"""
PaperCut printer token discovery tool.

Extracts printer IPP URLs and user tokens from a network capture, then saves
them to a JSON config file for use with install.py.

Usage:
    python3 discover.py --pcap capture.pcapng
    python3 discover.py --pcap capture.pcapng --output printers.json
    sudo python3 discover.py --live --interface eth0

How to capture the traffic:
  1. Connect a Windows/Mac machine to the network
  2. Open Wireshark, capture on your network interface
  3. Open System Preferences > Printers (Mac) or Settings > Printers (Windows)
     so the PaperCut client queries the server
  4. Wait ~5 seconds, then stop the capture
  5. Save as .pcapng and run this script on it

Requirements: tshark  (apt/pacman/xbps install wireshark-cli  or  wireshark)
"""

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile


TOKEN_RE = re.compile(
    r"(https?)://([^/]+)/printers/([^/]+)/users/(\d+)/([0-9a-f]{64})"
)

DEFAULT_OUTPUT = "printers.json"


def run_tshark(args: list[str]) -> str:
    result = subprocess.run(["tshark"] + args, capture_output=True, text=True)
    if result.returncode != 0 and not result.stdout:
        print(f"tshark error: {result.stderr.strip()}", file=sys.stderr)
        sys.exit(1)
    return result.stdout


def extract_from_pcap(path: str) -> list[dict]:
    output = run_tshark(["-r", path, "-Y", "ipp", "-V"])
    return parse_output(output)


def parse_output(output: str) -> list[dict]:
    found = {}
    for m in TOKEN_RE.finditer(output):
        scheme, host, raw_name, user_id, token = m.groups()
        name = raw_name.replace("+", " ").replace("%2b", "+").replace("~2b", "+")
        port = 9164 if scheme == "https" else 9163
        key = (host.split(":")[0], name, user_id)
        if key not in found:
            found[key] = {
                "name": name,
                "server": host.split(":")[0],
                "port": port,
                "scheme": scheme,
                "user_id": int(user_id),
                "token": token,
            }
    return list(found.values())


def live_capture(interface: str, duration: int) -> list[dict]:
    print(f"Capturing on {interface} for {duration}s — "
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


def save_config(printers: list[dict], path: str) -> None:
    with open(path, "w") as f:
        json.dump(printers, f, indent=2)
    print(f"\nSaved {len(printers)} printer(s) to {path}")
    print(f"Next step: sudo python3 install.py --config {path}")


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter
    )
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--pcap", metavar="FILE", help="extract tokens from a .pcapng file")
    mode.add_argument("--live", action="store_true", help="capture live traffic")
    parser.add_argument("--interface", default="eth0",
                        help="network interface for live capture (default: eth0)")
    parser.add_argument("--duration", type=int, default=30,
                        help="live capture duration in seconds (default: 30)")
    parser.add_argument("--output", default=DEFAULT_OUTPUT, metavar="FILE",
                        help=f"output JSON config file (default: {DEFAULT_OUTPUT})")
    parser.add_argument("--print", action="store_true", dest="print_only",
                        help="print results to stdout instead of saving")
    args = parser.parse_args()

    if args.live:
        if os.geteuid() != 0:
            print("Live capture requires root (use sudo)")
            sys.exit(1)
        printers = live_capture(args.interface, args.duration)
    else:
        printers = extract_from_pcap(args.pcap)

    if not printers:
        print("No printer tokens found.")
        print("Make sure the capture includes IPP traffic (tcp port 9163 or 9164).")
        sys.exit(1)

    if args.print_only:
        print(json.dumps(printers, indent=2))
        return

    print(f"\nFound {len(printers)} printer(s):\n")
    print(f"  {'Name':<25} {'Server':<40} {'User ID'}")
    print("  " + "-" * 75)
    for p in printers:
        print(f"  {p['name']:<25} {p['server']:<40} {p['user_id']}")

    save_config(printers, args.output)


if __name__ == "__main__":
    main()
