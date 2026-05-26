# PaperCut Linux Printer Installer

Open-source Linux installer for PaperCut-managed print queues. No proprietary
PaperCut client required — uses Linux's built-in CUPS with standard IPP.

## How it works

PaperCut exposes each printer as a standard IPP (Internet Printing Protocol)
endpoint. The URL contains a per-user auth token, so no separate login is
needed at print time. This tool extracts those URLs from a network capture and
adds the printers to CUPS using driverless (IPP Everywhere) printing with PDF.

## Requirements

- Linux with CUPS installed
- Python 3.8+
- `tshark` (for `discover.py`)
- Connected to the same network as the PaperCut server

### Install CUPS and tshark

| Distro | Command |
|--------|---------|
| Ubuntu/Debian | `sudo apt install cups wireshark-cli` |
| Arch | `sudo pacman -S cups wireshark-cli` |
| Fedora | `sudo dnf install cups wireshark-cli` |
| Void | `sudo xbps-install cups wireshark` |

---

## Workflow

### Step 1 — Capture the printer traffic

You need a network capture that includes the PaperCut discovery traffic.
The easiest way is from any Windows or Mac machine already set up with PaperCut.

1. Install [Wireshark](https://www.wireshark.org/) on the Windows/Mac machine
2. Start a capture on the active network interface
3. Open **Settings → Bluetooth & devices → Printers** (Windows) or  
   **System Settings → Printers** (Mac) — this triggers the PaperCut client to query the server
4. Wait a few seconds, then stop the capture
5. **File → Export as pcapng** and copy the file to your Linux machine

### Step 2 — Extract printer config

```bash
python3 discover.py --pcap capture.pcapng
```

This creates `printers.json` with the server address, user ID, and per-printer tokens.

### Step 3 — Install the printers

```bash
sudo python3 install.py --config printers.json
```

Done. The printers will appear in any application's print dialog.

---

## Commands

### discover.py

```bash
# Extract from a pcap file (creates printers.json)
python3 discover.py --pcap capture.pcapng

# Save to a custom filename
python3 discover.py --pcap capture.pcapng --output myschool.json

# Print to stdout instead of saving
python3 discover.py --pcap capture.pcapng --print

# Live capture (must be on the network, requires root)
sudo python3 discover.py --live --interface eth0 --duration 30
```

### install.py

```bash
# Install all printers
sudo python3 install.py --config printers.json

# Preview the IPP URLs without installing
python3 install.py --config printers.json --list

# Test that the server is reachable
python3 install.py --config printers.json --test

# Remove all installed printers
sudo python3 install.py --config printers.json --remove
```

---

## Refreshing tokens

If your tokens expire or you switch user accounts, repeat Steps 1–3 with a
fresh capture. The new `printers.json` will replace the old one, and
re-running `install.py` will update the existing CUPS queues.

---

## Technical details

| Property | Value |
|----------|-------|
| Protocol | IPP 2.0 over HTTP/HTTPS |
| CUPS driver | IPP Everywhere (driverless) |
| Document format | PDF (primary) |
| Auth method | Per-user token embedded in IPP URL path |
| HTTP port | 9163 |
| HTTPS port | 9164 |

The per-user token in each URL is issued by PaperCut and identifies the user
for print accounting and release. It acts as a bearer credential — no
username/password is sent at print time.

---

## Troubleshooting

**`tshark: command not found`** — install `wireshark-cli` or `wireshark` package.

**No printers found in capture** — make sure the capture includes traffic on
port 9163. In Wireshark you can verify with the filter `tcp.port == 9163`.

**Server unreachable** — you must be on the same network (or VPN) as the
PaperCut server. The server hostname is stored in `printers.json`.

**`lpadmin: IPP Everywhere` error** — your CUPS version may be too old.
Update to CUPS 2.2+ or try installing `printer-driver-cups-pdf` as a fallback.
