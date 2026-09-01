# Troubleshooting

## Auto-discovery fails

Auto-discovery works by scanning your local network for a PaperCut server. It can fail for a few reasons:

### You need to be on the school network

Discovery requires your machine to be on the **same network segment** as the print server. This means:

- **On campus Wi-Fi or a wired connection** - discovery should work
- **At home / off-campus** - discovery will not work; you need to specify the server address manually (see below)
- **On a guest or BYOD Wi-Fi network** - many schools isolate these networks from internal infrastructure using VLANs, which will block discovery even if you're physically on campus

If your school uses strict VLAN segmentation (common in universities and larger secondary schools), the print server may simply be unreachable from the student network. In that case, ask IT whether student devices are permitted to print directly via IPP.

---

## Finding your PaperCut server address

### Option 1 - Ask IT

The simplest option. Ask your school's IT helpdesk for the PaperCut server IP address or hostname. They may know it as the "print server address."

### **Option 2 - Try the common PaperCut DNS hostname**

Many schools configure a DNS entry specifically for PaperCut discovery. Try:

```
nslookup pc-printer-discovery
```

If this returns an IP, use that as your server address.

### Option 3 - Find it on a Windows machine that already has PaperCut installed

On a Windows PC that can already print via PaperCut, open a Command Prompt and run:

```
nslookup papercut
nslookup print
nslookup printing
```

If any of these return an IP address, that is your server. You can also check:

```
ipconfig /all
```

Look for a DNS suffix (e.g. `school.internal`) and try:

```
nslookup papercut.school.internal
```

### Option 4 - Check the PaperCut client config on Windows

If the PaperCut client is installed on a Windows machine, the server address is often stored in:

```
C:\Program Files\PaperCut MF Client\client.properties
```

or

```
C:\Program Files (x86)\PaperCut MF Client\client.properties
```

Open it in Notepad and look for a line like `server-ip=10.10.5.19`.

---

## Using `--server` to skip discovery

Once you have the server address, pass it directly:

```bash
sudo python3 papercut.py --server 10.10.5.19
# or using a hostname:
sudo python3 papercut.py --server print.school.internal
```

This bypasses all discovery and goes straight to fetching the printer list.

---

## Printers install but jobs don't print

- Make sure your account has print credit or quota remaining in PaperCut
- Check that the CUPS service is running: `sudo systemctl start cups`
- Try printing a test page from CUPS: open `http://localhost:631` in a browser

## Jobs send but nothing prints / no credential popup appeared

PaperCut Mobility Print always requires user authentication to track quota - if CUPS doesn't know to ask for credentials, the job is sent without them and silently rejected by the server.

This happens when `auth-info-required` was not set on the printer, which can occur if the printer was installed by an older version of this script.

Re-running the installer fixes all installed printers automatically:

```bash
sudo python3 papercut.py --server <ip>
```

On the next print attempt, CUPS will show an authentication popup - enter your normal school/work username and password. Most applications remember this for future jobs.

## `lpadmin` fails during install

CUPS may not be installed or running:

```bash
# Install CUPS (see README for your distro)
sudo systemctl enable --now cups
```

## "Could not fetch printers" after finding the server

If discovery succeeds but the script fails to fetch printers, run with `--debug` to see exactly what URLs are being tried and what the server is returning:

```bash
sudo python3 papercut.py --server 10.10.5.19 --debug
```

This prints every HTTP request, its status code, and the response body. Share this output when asking for help.

---

## Printers show as unavailable after install

Run:

```bash
sudo cupsenable <printer-name>
sudo cupsaccept <printer-name>
```

---

## Non-default paper sizes (A3, Letter, etc.) print as A4

PaperCut Mobility Print uses its own non-standard media keywords and ignores PPD-style option names sent by the GTK/Firefox print dialog. The installer works around both issues automatically, but older installs (before v1.0.4) may not have the fix applied.

Re-run the installer to patch existing printers:

```bash
sudo python3 papercut.py --server <ip>
```

This rewrites the PPD's media keywords to match the server's exact values and installs a backend wrapper that translates `PageSize=A3` into the `media=` IPP attribute PaperCut actually honours. After re-running, select your paper size in the print dialog as normal.

---

## Immutable / read-only systems (Fedora Silverblue, Kinoite, openSUSE MicroOS, SteamOS)

On these distros `/usr` is a read-only ostree/btrfs snapshot, so the installer cannot write its option-translating backend wrapper to `/usr/lib/cups/backend`.

**Printers still install and print.** The installer detects the read-only mount, prints a note, and falls back to the stock `ipp`/`ipps` backends. The PPD media-keyword patch (which lives in `/etc/cups/ppd`, writable everywhere) still applies, so the printer's paper sizes are still advertised correctly.

What you lose is the translation of PPD-style options into IPP attributes. Paper size and duplex chosen in the GTK/Firefox print dialog may be ignored by the PaperCut server, which then falls back to its own default (usually A4, single-sided). Pass the IPP options directly instead:

```bash
lp -d PRINTER -o media=ISO_A3 -o sides=two-sided-long-edge file.pdf
```

If you want the wrapper anyway, unlock `/usr` first:

```bash
sudo rpm-ostree usroverlay          # Silverblue / Kinoite - transient
sudo python3 papercut.py --server <ip>
```

Note that `usroverlay` is discarded on reboot, and after a reboot the wrapper backend is gone while the queue's device URI still points at `papercut-ipp://`, which makes jobs fail. Re-run the installer without the overlay to move the queues back to the plain `ipp://` backend.

`--finishing` cannot work on an immutable system at all: vendor print filters are binaries that CUPS will only load from `/usr/lib/cups/filter`.

---

## Jobs fail with "document format not supported"

CUPS parses PPDs into an in-memory MIME database at startup and validates incoming jobs against that cache - not against the PPD file on disk. Re-running the script patches the PPD files and sends SIGHUP to cupsd to force a re-parse, so the fix takes effect immediately without a full restart:

```bash
sudo python3 papercut.py --server <ip>
```

If jobs still fail after re-running, restart cupsd manually to force a full reload:

```bash
sudo systemctl restart cups   # systemd
sudo sv restart cupsd         # runit / Void Linux
```

## Stapling does nothing on a normal queue

Expected. Mobility Print cannot staple. Check for yourself:

```bash
curl http://YOUR-SERVER:9163/printers
```

Every queue reports only `{mediaSizes, resolutions, color, duplex}`. There is no
finishing field, so `-o finishings=4`, `-o StapleLocation=...` and every other variant
are accepted and silently discarded. Do not keep trying different values.

Use `--finishing` with your printer's own driver instead. See "Stapling and hole punch"
in the README.

## Finishing queue hangs on "Connecting to printer"

The queue cannot reach LPD on the server.

```bash
# Is the LPD port open?
nc -vz YOUR-SERVER 515

# Is the server address being routed somewhere unexpected?
ip route get YOUR-SERVER-IP
```

A split-tunnel VPN is the usual cause. Broad routes such as `8.0.0.0/6` can swallow a
private server address and send print traffic down the tunnel, where it times out.
Add a more specific route back to your LAN gateway:

```bash
sudo ip route replace 10.1.1.0/24 via YOUR-LAN-GATEWAY
```

Re-apply it when the VPN comes up and when the network changes. Wi-Fi re-association
drops interface routes, so a VPN-only hook is not enough on a laptop.

## Finishing install says "No finishing command found in the driver output"

The driver installed, but it did not emit a finishing command for your printer. Usually
it is the wrong driver: the right brand but the wrong model or generation. Re-check the
model on the machine itself, or in the PaperCut web interface at
`http://YOUR-SERVER:9191`.

The queues still print normally. They just will not staple.

## Finishing install fails with "No .deb inside"

You downloaded the Red Hat package. Fetch the Ubuntu or Debian one instead. If your
vendor ships only an `.rpm`, unpack it yourself and pass the directory:

```bash
rpm2cpio driver.rpm | cpio -idmv -D ~/driver-files
sudo python3 papercut.py --finishing --driver ~/driver-files
```

## Still stuck?

Ask. I would rather help than have you give up on it.

- **[Open an issue](https://github.com/jedbillyb/papercut-linux-printer-installer/issues)** - best for anything others might hit too
- **Email** - <jedbillyb@gmail.com>
- **Web** - [jedbillyb.com](https://jedbillyb.com)

Include your distro, the output of `python3 papercut.py --debug`, and
`curl http://YOUR-SERVER:9163/printers` if you can reach the server.
