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
