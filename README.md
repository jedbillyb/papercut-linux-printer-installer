# PaperCut Linux Printer Installer

Adds your school's PaperCut printers to Linux in one command. No proprietary
client required - uses standard IPP and CUPS.

## Usage

```bash
sudo python3 papercut.py
```

That's it. The script auto-discovers the server via mDNS, prompts for your
school username and password, then installs all your printers into CUPS.

### Options

```bash
# Skip mDNS and specify the server directly
sudo python3 papercut.py --server 10.10.5.19

# Remove all installed printers
sudo python3 papercut.py --remove
```

## Requirements

- Linux with CUPS installed (`sudo apt/pacman/dnf/xbps install cups`)
- Python 3.8+
- `python3-zeroconf` for mDNS auto-discovery

### Install dependencies

| Distro | Command |
|--------|---------|
| Ubuntu/Debian | `sudo apt install cups python3-zeroconf` |
| Arch | `sudo pacman -S cups python-zeroconf` |
| Fedora | `sudo dnf install cups python3-zeroconf` |
| Void | `sudo xbps-install cups python3-zeroconf` |

## How it works

PaperCut exposes each printer as a standard IPP endpoint with a per-user auth
token in the URL. This script authenticates with your credentials, retrieves
the list of printer URLs, and adds them to CUPS using driverless (IPP Everywhere)
printing. Documents are sent as PDF - no rasterisation, full quality.
