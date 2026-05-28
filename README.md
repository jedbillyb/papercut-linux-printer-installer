# PaperCut Linux Printer Installer

> Add your school's PaperCut printers to Linux in one command - no proprietary client needed.

[![Python](https://img.shields.io/badge/python-3.8%2B-blue)](https://www.python.org/downloads/)
[![Platform](https://img.shields.io/badge/platform-Linux-lightgrey)](https://kernel.org/)
[![License](https://img.shields.io/badge/license-MIT-green)](LICENSE)

Automatically discovers your PaperCut Mobility Print server and installs all available printers into CUPS as standard IPP Everywhere queues. Works on any distro that runs CUPS. On Linux, CUPS will show an authentication popup the first time you send a job — enter your normal school/work login credentials there.

---

## Features

- **Zero config** - finds the server via mDNS, DNS lookup, or subnet scan automatically
- **Driverless** - uses IPP Everywhere; no manufacturer drivers required
- **Full quality** - documents sent as PDF, no rasterisation or colour loss
- **Safe removal** - `--remove` only touches printers this tool installed, nothing else

---

## Requirements

- Linux with CUPS installed
- Python 3.8+
- [`zeroconf`](https://pypi.org/project/zeroconf/) *(optional - enables mDNS discovery; falls back to DNS/scan without it)*

### Install dependencies

| Distro | Command |
|--------|---------|
| Ubuntu / Debian | `sudo apt install cups cups-ipp-utils python3-zeroconf` |
| Arch | `sudo pacman -S cups python-zeroconf` |
| Fedora | `sudo dnf install cups cups-ipptool python3-zeroconf` |
| Void | `sudo xbps-install cups cups-filters python3-zeroconf` |

Or via pip (in a venv or with `--break-system-packages`):

```bash
pip install -r requirements.txt
```

---

## Installation

```bash
git clone https://github.com/jedbillyb/papercut-linux-printer-installer
cd papercut-linux-printer-installer
```

No build step required - `papercut.py` is a single self-contained script.

---

## Usage

```bash
# Auto-discover server and install all printers
sudo python3 papercut.py

# Skip discovery and specify the server directly
sudo python3 papercut.py --server 10.10.5.19

# Remove all PaperCut printers previously installed by this tool
sudo python3 papercut.py --remove

# List installed PaperCut printers (no sudo needed)
python3 papercut.py --list

# Preview what would be installed without touching CUPS
python3 papercut.py --dry-run
```

### Example output

```
Searching for PaperCut server...
  [1/3] mDNS broadcast... not found
  [2/3] DNS via gateway (10.10.0.1)... not found
  [3/3] port scan:
    scanning 10.10.0.0/20 (4094 hosts).......... found 10.10.5.19
Fetching printer list... 4 printer(s) found

  Library Mono
  Library Colour
  Staff Room
  Admin Office

  (new)    Library-Mono... done
  (new)    Library-Colour... done
  (new)    Staff-Room... done
  (new)    Admin-Office... done

4/4 printers ready.
Open any application and select a printer to test.
```

Re-running the script is safe — already-installed printers are skipped:

```
Fetching printer list... 4 printer(s) found

  Library Mono
  Library Colour
  Staff Room
  Admin Office

  (skip)   Library-Mono... already installed
  (skip)   Library-Colour... already installed
  (skip)   Staff-Room... already installed
  (skip)   Admin-Office... already installed

4/4 printers ready.
Open any application and select a printer to test.
```

---

## How it works

PaperCut Mobility Print exposes each printer as a standard IPP endpoint. This script:

1. **Discovers** the server using three methods in order:
   - mDNS broadcast (`_pc-printer-discovery._tcp`, `_ipp._tcp`, `_ipps._tcp`)
   - DNS lookup of common PaperCut hostnames via your default gateway
   - Port scan — local and explicitly-routed subnets first, then the rest of the /16 matching the gateway
2. **Fetches** the printer list from the Mobility Print API (no credentials required at install time)
3. **Installs** each printer into CUPS via `lpadmin` using the `everywhere` (driverless) driver

---

## Troubleshooting

**Discovery fails or takes too long**
Specify the server IP directly to skip discovery:
```bash
sudo python3 papercut.py --server <ip-or-hostname>
```

**"CUPS not found"**
Install CUPS using the command for your distro in the table above.

**Printers install but jobs fail with "document format not supported"**
Re-run the script — it patches the printer PPDs and reloads cupsd so the fix takes effect immediately:
```bash
sudo python3 papercut.py --server 10.1.1.12
```
If the problem persists, install `cups-filters` for your distro (adds PDF conversion support).

**Print dialog selects A3 (or other size) but job prints on A4**
Use `lp -o media=iso_a3_297x420mm` from the command line instead — see [TROUBLESHOOTING.md](TROUBLESHOOTING.md) for the full explanation and a media size reference table.

**Printers install but jobs don't print**
Make sure your account has print credit/quota remaining in PaperCut.

**`lpadmin` reports failure**
Your CUPS service may not be running. Start it with:
```bash
sudo systemctl start cups
```

For more detail - including how to find your server address and notes on VLAN-segmented school networks - see [TROUBLESHOOTING.md](TROUBLESHOOTING.md).

---

## Contributing

Pull requests are welcome. Please keep changes focused - one fix or feature per PR. If you're adding support for a new PaperCut API path or discovery method, include a brief description of where you found the endpoint.

---

<div align="center">
<sub><a href="LICENSE">MIT</a> © <a href="https://github.com/jedbillyb">jedbillyb</a> · Made with ❤️</sub>
</div>
