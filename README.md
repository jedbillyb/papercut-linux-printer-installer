# PaperCut Linux Printer Installer

Open-source Linux installer for PaperCut-managed print queues. No proprietary
PaperCut client required - uses Linux's built-in CUPS with standard IPP.

## How it works

PaperCut exposes each printer as a standard IPP (Internet Printing Protocol)
endpoint. The URL contains a per-user auth token, so no separate login is
needed at print time. This tool authenticates with your school credentials to
fetch those URLs, then adds the printers to CUPS using driverless (IPP Everywhere)
printing with PDF.

## Requirements

- Linux with CUPS installed
- Python 3.8+
- Connected to the same network as the PaperCut server

### Install CUPS

| Distro | Command |
|--------|---------|
| Ubuntu/Debian | `sudo apt install cups` |
| Arch | `sudo pacman -S cups` |
| Fedora | `sudo dnf install cups` |
| Void | `sudo xbps-install cups` |

---

## Workflow

### Step 1 - Discover printers (using your school login)

```bash
python3 discover.py --server 10.10.5.19 --username yourname
```

Enter your password when prompted. This saves `printers.json` with all your
printer URLs and auth tokens.

If you don't know the server IP, ask your IT department or check the hostname
your Windows machine uses for printing (usually something like
`rpc.pc-printer-discovery.schoolname.local`).

### Step 2 - Install the printers

```bash
sudo python3 install.py --config printers.json
```

Done. The printers will appear in any application's print dialog.

---

### Alternative: extract tokens from a network capture

If credential-based discovery doesn't work with your school's setup, you can
extract tokens from a Wireshark capture instead.

1. Install [Wireshark](https://www.wireshark.org/) on a Windows/Mac machine
2. Start a capture, open **Settings -> Printers**, wait a few seconds, stop
3. Save as `.pcapng`, copy to your Linux machine
4. Run:

```bash
python3 discover.py --pcap capture.pcapng
sudo python3 install.py --config printers.json
```

---

## Commands

### discover.py

```bash
# Discover using school credentials (recommended)
python3 discover.py --server 10.10.5.19 --username yourname
python3 discover.py --server 10.10.5.19 --username yourname --password secret

# Extract from a pcap file
python3 discover.py --pcap capture.pcapng

# Save to a custom filename
python3 discover.py --server 10.10.5.19 --username yourname --output myschool.json

# Print to stdout instead of saving
python3 discover.py --server 10.10.5.19 --username yourname --print

# Live capture (requires root + tshark)
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

If your tokens expire or you switch user accounts, just re-run Step 1 with
fresh credentials. Re-running `install.py` will update the existing CUPS queues.

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
for print accounting and release. It acts as a bearer credential - no
username/password is sent at print time.

---

## Troubleshooting

**Server unreachable** - make sure you are on the same network (or VPN) as the
PaperCut server. The server address is stored in `printers.json`.

**Credentials not working** - try the pcap method as a fallback (see above).

**`lpadmin: IPP Everywhere` error** - your CUPS version may be too old.
Update to CUPS 2.2+ or install `printer-driver-cups-pdf` as a fallback.
