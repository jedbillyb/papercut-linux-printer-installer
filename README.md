# PaperCut Linux Printer Installer

> Add your school's PaperCut printers to Linux in one command - no proprietary client needed.

[![Python](https://img.shields.io/badge/python-3.8%2B-blue)](https://www.python.org/downloads/)
[![Platform](https://img.shields.io/badge/platform-Linux-lightgrey)](https://kernel.org/)
[![License](https://img.shields.io/badge/license-MIT-green)](LICENSE)

Automatically discovers your PaperCut Mobility Print server and installs all available printers into CUPS as standard IPP Everywhere queues. Works on any distro that runs CUPS. On Linux, CUPS will show an authentication popup the first time you send a job - enter your normal school/work login credentials there.

---

## Features

- **Zero config** - finds the server via mDNS, DNS lookup, or subnet scan automatically
- **Driverless** - uses IPP Everywhere; no manufacturer drivers required
- **Full quality** - documents sent as PDF, no rasterisation or colour loss
- **Stapling and hole punch** - optional finishing queues via your printer's own driver
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

# Install stapling/hole-punch queues too (see "Stapling and hole punch")
sudo python3 papercut.py --finishing --driver ~/Downloads/vendor-driver.zip

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

Re-running the script is safe - already-installed printers are skipped:

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
   - Port scan - local and explicitly-routed subnets first, then the rest of the /16 matching the gateway
2. **Fetches** the printer list from the Mobility Print API (no credentials required at install time)
3. **Installs** each printer into CUPS via `lpadmin` using the `everywhere` (driverless) driver
4. **Installs a thin backend wrapper** (`/usr/lib/cups/backend/papercut-ipp(s)`) that translates PPD-style print options (`PageSize=A3`, `Duplex=DuplexNoTumble`) into real IPP attributes (`media=`, `sides=`) before handing the job to the real backend — PaperCut Mobility Print ignores the PPD-style form and would otherwise silently fall back to its server default. On immutable distros, where `/usr` is read-only, this step is skipped and the queues use the stock `ipp`/`ipps` backends instead
5. **Patches each printer's PPD** to use the server's exact media keywords (e.g. `ISO_A3`) rather than the PWG self-describing names CUPS generates, so non-default paper sizes are not silently rejected

---

## Stapling and hole punch

Regular queues cannot staple. This is a hard limit of PaperCut Mobility Print, not a
bug in this tool. Ask the server what it can do:

```bash
curl http://YOUR-SERVER:9163/printers
```

Every queue reports the same four capabilities:

```
{mediaSizes, resolutions, color, duplex}
```

That schema comes from Google Cloud Print and has no finishing field, so a staple
request is accepted and then dropped. Sending different IPP `finishings` values will
not help. Windows staples because PaperCut Print Deploy gives it the manufacturer's
driver, which writes the staple into the print data as PJL before the job leaves the
machine.

`--finishing` does the same thing on Linux. It installs your printer's own driver and
adds a second queue per printer, named `<Printer>-Finishing`, that reaches the server
over LPD instead of Mobility Print.

### 1. Get your driver

This tool does not ship drivers. Manufacturer licences forbid redistributing them, so
you download it yourself. Find your printer's make and model, then search the
manufacturer's support site for a Linux, CUPS or PPD driver.

Get the model from the front of the machine, or from the PaperCut web interface at
`http://YOUR-SERVER:9191`. Some machines show only a certification code on the label:
search that code and the model name usually turns up.

Prefer the Ubuntu or Debian package. Take the `.zip` or `.deb` exactly as downloaded.

### 2. Install

```bash
sudo python3 papercut.py --finishing --driver ~/Downloads/your-driver.zip
```

You will be asked for your PaperCut username. LPD has no authentication, so that
username is the only thing the server can bill the job to. Pass it with
`--papercut-user NAME` to skip the prompt.

The installer reports which finishing options your driver exposes, then proves the
driver actually emits the command, without printing anything:

```
Finishing options this driver supports:
  FFStaple             None UpperLeftSingle LeftDouble TopDouble
  FFPunch              Off On

Checking FFStaple=UpperLeftSingle reaches the printer:
  @PJL SET FINISH=ON
  @PJL SET STAPLE=TOPLEFT
  Driver emits the finishing command.
```

If no command appears there, it will not staple on paper either. Check you have the
driver for that exact model.

### 3. Print

Use the option names the installer listed. They differ by manufacturer:

```bash
lp -d Library-Finishing -o FFStaple=UpperLeftSingle -o Duplex=DuplexNoTumble file.pdf
```

Keep using the plain queue for everyday printing. It needs no vendor driver and no
maintenance. The finishing queue exists only to reach the stapler.

`--remove` removes both kinds.

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
Re-run the script - it patches the printer PPDs and reloads cupsd so the fix takes effect immediately:
```bash
sudo python3 papercut.py --server 10.1.1.12
```
If the problem persists, install `cups-filters` for your distro (adds PDF conversion support).

**Printers install but jobs don't print**
Make sure your account has print credit/quota remaining in PaperCut.

**Immutable distro (Fedora Silverblue, Kinoite, MicroOS, SteamOS)**
`/usr` is read-only, so the backend wrapper cannot be installed. The script detects this and falls back to the stock `ipp`/`ipps` backends - printers install and print normally, but paper size and duplex chosen in the print dialog may be ignored. Pass them directly instead:
```bash
lp -d PRINTER -o media=ISO_A3 -o sides=two-sided-long-edge file.pdf
```
See [TROUBLESHOOTING.md](TROUBLESHOOTING.md#immutable--read-only-systems-fedora-silverblue-kinoite-opensuse-microos-steamos) for details.

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

## Need help?

Ask. Every school's PaperCut setup is a bit different, and I would rather help you get
it working than have you give up on it.

- **[Open an issue](https://github.com/jedbillyb/papercut-linux-printer-installer/issues)** - best for anything others might hit too
- **Email** - <jedbillyb@gmail.com>
- **Web** - [jedbillyb.com](https://jedbillyb.com)

Useful things to include: your distro, the output of `python3 papercut.py --debug`, and
`curl http://YOUR-SERVER:9163/printers` if you can reach the server.

---

<div align="center">
<sub><a href="LICENSE">MIT</a> © <a href="https://github.com/jedbillyb">jedbillyb</a></sub>
</div>
