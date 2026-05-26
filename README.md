<p align="right"><a href="https://jedbillyb.com"><img src="https://img.shields.io/badge/jedbillyb.com-000?style=for-the-badge&logo=archlinux&logoColor=blue" /></a></p>

# PaperCut Linux Printer Installer

> Add your school's PaperCut printers to Linux in one command - no proprietary client needed.

[![Python](https://img.shields.io/badge/python-3.8%2B-blue)](https://www.python.org/downloads/)
[![Platform](https://img.shields.io/badge/platform-Linux-lightgrey)](https://kernel.org/)
[![License](https://img.shields.io/badge/license-MIT-green)](LICENSE)

Automatically discovers your PaperCut print server, authenticates with your school credentials, and installs all your printers into CUPS as standard IPP Everywhere queues. Works on any distro that runs CUPS.

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
| Ubuntu / Debian | `sudo apt install cups python3-zeroconf` |
| Arch | `sudo pacman -S cups python-zeroconf` |
| Fedora | `sudo dnf install cups python3-zeroconf` |
| Void | `sudo xbps-install cups python3-zeroconf` |

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
# Auto-discover server, authenticate, and install all printers
sudo python3 papercut.py

# Skip discovery and specify the server directly
sudo python3 papercut.py --server 10.10.5.19

# Remove all PaperCut printers previously installed by this tool
sudo python3 papercut.py --remove
```

The script prompts for your school username and password. Credentials are used only for the API request and are never stored.

### Example output

```
Searching for PaperCut server...
  [1/3] mDNS broadcast... not found
  [2/3] DNS via gateway (10.10.0.1)... not found
  [3/3] port scan:
    scanning 10.10.0.0/20 (4094 hosts).......... found 10.10.5.19
Username: jsmith
Password:
Fetching printer list... 4 printer(s) found

  Library Mono
  Library Colour
  Staff Room
  Admin Office

  (new)    Library-Mono... done
  (new)    Library-Colour... done
  (new)    Staff-Room... done
  (new)    Admin-Office... done

4/4 printers installed.
Open any application and select a printer to test.
```

---

## How it works

PaperCut exposes each printer as a standard IPP endpoint with a per-user auth token embedded in the URL. This script:

1. **Discovers** the server using three methods in order:
   - mDNS broadcast (`_pc-printer-discovery._tcp`, `_ipp._tcp`, `_ipps._tcp`)
   - DNS lookup of common PaperCut hostnames via your default gateway
   - Port scan - local subnet first, then the /16, then the full /8 if needed
2. **Authenticates** against the PaperCut REST API with your credentials
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

## License

[MIT](LICENSE)

---

<div align="center">
<sub>MIT © <a href="https://jedbillyb.com">jedbillyb</a> · Made with ❤️</sub>
</div>
