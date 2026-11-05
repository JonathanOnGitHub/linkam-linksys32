# Runbook — Linkam hot-stage, first temperature test

Prepared on this laptop (Debian, `jonathan-laptop`). Everything below is
already installed and tested; the only thing missing is the hardware.

---

## Which instrument do you have?

| Instrument | Link | Command prefix |
|---|---|---|
| **Linkam T95** | USB HID (`16da:0002`) | `--hid` |
| Older MSVP/TMS | RS-232 via USB-to-RS232 adapter | `--port /dev/ttyUSB0` |

**The T95 needs no serial adapter.** If `lsusb | grep 16da` shows
`Linkam T95`, follow the HID route below.

---

## TL;DR — in order

### If you have the T95 (USB HID)

```bash
cd ~/Documents/Nottingham/Research/Linkam/linkam-linksys32/python

# 1. plug in the T95 and confirm it is seen
.venv/bin/python tools/find_adapter.py

# 2. READ THE TEMPERATURE  (this works today)
.venv/bin/python -m linkam.cli read --hid

# 3. watch it change
.venv/bin/python -m linkam.cli read --hid --count 60 --interval 1
```

> **Reading the temperature works.  Setting it does not.**
>
> The T95's USB link is a **binary** protocol, not the ASCII vocabulary
> the Linksys32 software uses over RS-232, and the frame that changes
> the setpoint has not been found.  See `PROTOCOL_NOTES.md` §5.5–5.7.
> The `run` command below will therefore **not** ramp the stage, even
> though it reports success.

### If you have an older RS-232 programmer

```bash
cd ~/Documents/Nottingham/Research/Linkam/linkam-linksys32/python

# 1. plug in the USB-to-RS232 adapter
.venv/bin/python tools/find_adapter.py          # tells you the port

# 2. settle the framing (read-only, safe on a cold instrument)
.venv/bin/python -m linkam.cli probe --port /dev/ttyUSB0

# 3. FIRST temperature test — temperature only, modest limit
.venv/bin/python -m linkam.cli run \
    --port /dev/ttyUSB0 --baud 9600 \
    --target 40 --rate 5 --hold 60 --no-video \
    --out results/first_test
```

Do **not** skip the probe step and do **not** go straight to 95 °C. See
"Safety" below.

---

## What is ready

| Thing | Status |
|---|---|
| Repo cloned | `~/Documents/Nottingham/Research/Linkam/linkam-linksys32` |
| Virtualenv | `python/.venv` (Python 3.14.8) |
| Dependencies | pyserial 3.5, numpy 2.5.4, opencv-python 5.0.0, pytest 9.2.0 |
| Test suite | 110 passed (`pytest tests/ -q`) |
| `doctor` check | 0 FAIL, 1 WARN (video — expected, see below) |
| `socat` 1.8.1.3 | installed at `/usr/bin/socat`, for serial sniffing |
| Adapter finder | `tools/find_adapter.py` (detects both HID and serial) |
| T95 HID support | `linkam/hid.py`, `tools/t95_hid_probe.py` |
| udev rule | `/etc/udev/rules.d/70-linkam-t95.rules` (installed) |
| User permissions | `burley` is in `dialout` and `plugdev` |

The one WARN from `doctor` is the video backend: this laptop has no
Conexant BT848 driver (there is no mainline Linux driver for it), and
the two `/dev/video*` devices are the laptop's own **integrated webcam**.
That does not affect the temperature work at all — and given your
starting goal (measure temperature, set a target, set a ramp), you do
not need video yet. Use `--no-video` until the temperature link is
proven.

---

## Changes made to the repo

**1. `requirements.txt` — `serial>=3.5` → `pyserial>=3.5`**

`serial` is not a PyPI project (the available versions are `0.0.x`).
The distribution is called `pyserial`; the *import* name is `serial`,
which is what the code uses. With the original line, `pip install -r
requirements.txt` failed outright. It now installs cleanly.

**2. `linkam/experiment.py` — race between `telemetry.csv` and `run.json`**

The telemetry reader thread appends to a list continuously. The old code
read that list twice: once to write `telemetry.csv`, then again a few
lines later to fill in `final_temperature_c` and `telemetry_samples` in
`run.json`. If a frame arrived in between, `run.json` would report a
final temperature that **never appears in the CSV**, and a sample count
one higher than the number of rows written.

This was observed during rehearsal (`run.json` said `final_T=85.37`
while the last CSV row read `95.02`). The fix takes one snapshot under
the lock and uses it for every output. Added a regression test
(`tests/test_experiment.py::test_run_metadata_matches_telemetry_csv`)
that injects a frame exactly in the race window; it fails against the
old code and passes against the new.

This is a *reporting* bug — it did not affect the temperature control
itself. But it would have made you mistrust your own data.

**3. USB HID support for the T95 (new)**

The instrument in hand is a **T95**, which is a USB HID device, not a
serial port.  The changes:

* `linkam/transport.py` (new) — the physical-link seam.  `write_line` /
  `read_line`, with `SerialTransport` implementing the old RS-232 path.
* `linkam/hid.py` (new) — `HidTransport` for the T95, plus device
  discovery by VID:PID and by the hidraw `uevent`.
* `linkam/protocol.py` — `LinkamProgrammer` now talks to a transport
  instead of directly to pyserial.  `HidProgrammer` is a thin subclass
  that swaps in the HID transport.  **All the ramp logic, telemetry
  parsing and stall detection are shared and unchanged.**
* `linkam/experiment.py`, `linkam/cli.py` — `--hid` transport selection.
* `linkam/diagnostics.py`, `tools/find_adapter.py` — both now recognise
  the T95 and report whether the hidraw node is readable.
* `tools/t95_hid_probe.py` (new) — read-only reconnaissance: dumps the
  HID report descriptor and works out the report framing.
* `tests/test_hid.py`, `tests/test_hid_descriptor.py` (new) — 33 tests
  covering the HID framing, device discovery and the report-descriptor
  parser.

Two bugs worth knowing about, both caught by the new tests:

* The telemetry contains `°C`, which is **UTF-8**, not ASCII.  Decoding
  HID payloads as ASCII silently corrupts the unit on every frame.
* HID short items are encoded as `(tag << 4) | (type << 2) | size`, so
  the tag namespace depends on the item *type*.  A parser that assumes
  a flat tag table mis-decodes every descriptor.

---

## The T95 route in detail

### Step 0 — permissions (already done)

The T95 is a HID device, so the relevant node is `/dev/hidraw1`, not a
`/dev/tty*`.  Access is granted by:

```
# /etc/udev/rules.d/70-linkam-t95.rules
SUBSYSTEM=="hidraw", ATTRS{idVendor}=="16da", ATTRS{idProduct}=="0002", \
    MODE="0660", GROUP="plugdev", TAG+="uaccess"
```

Check it after plugging in:

```bash
ls -l /dev/hidraw*
```

You want `crw-rw----+ root plugdev`.  If it is `crw-------`, re-plug the
device; the rule is matched at enumeration.

### Step 1 — confirm the device is there

```bash
lsusb | grep 16da
.venv/bin/python tools/find_adapter.py
```

`find_adapter.py` reports the hidraw node and whether it is readable.

### Step 2 — read the temperature

```bash
.venv/bin/python -m linkam.cli read --hid
```

```
  23.6406 C  rate=0.7  limit=0.35
```

This sends a **status poll** and nothing else — it cannot move the
stage.  `--count N --interval S` for a series, `--json` for
machine-readable output.

### Step 3 — the report descriptor (read-only reconnaissance)

```bash
.venv/bin/python tools/t95_hid_probe.py --descriptor-only
```

The T95's descriptor is known and is 29 bytes:

```
Usage Page (0xFFA0)        -- Linkam vendor page
Collection (Application)
  INPUT  : usage 0x01, 63 bytes, no report ID
  OUTPUT : usage 0x02, 63 bytes, no report ID
```

So the payload is 63 bytes in each direction, with no report IDs.

### Step 3a — two things that look like faults but are not

* The device enumerates **twice** on power-up — first as
  `16da:0001 Linkam Bootloader`, then as `16da:0002 Linkam T95`.
  Only the second is the instrument you want; the package looks for
  `16da:0002` and ignores the bootloader.
* After a power cycle the device **answers nothing for about a
  minute**.  It enumerates immediately, but frames sent during the
  warm-up are silently dropped.  Wait, and do not mistake this for a
  dead link.

### Step 3b — if the instrument stops responding

Symptom: every write fails with `ETIMEDOUT`, including a plain 2-byte
write, while the device is still enumerated and its descriptor still
reads.  The firmware has stopped servicing its OUT endpoint.

That is **not** a framing problem — a wrong payload would be accepted
and ignored, not time out.  Recover, in order:

1. **Power-cycle the stage.** Switch it off, wait 10 s, switch it on.
2. **Unplug and replug the USB cable.**
3. **Reset the USB device** (needs your password):
   ```bash
   sudo ./tools/reset_usb_device.sh 16da:0002
   ```
   This unbinds and rebinds the USB device, which re-enumerates it
   without touching the cable.  (The `USBDEVFS_RESET` ioctl does *not*
   work here: the kernel refuses it with `ENOTTY` on a device whose
   interface is claimed by `hid-generic`.)  A reset is not always
   enough — if the firmware stays wedged, do the power cycle.

The stage is self-powered over USB (`bmAttributes` 0xc0, 100 mA), so
a USB replug alone may not restart the firmware — the power cycle is
the important one.

### Step 3c — raw byte-level probe

If the high-level tools say "no response" but the device is alive, use
the raw tool, which writes bytes directly and dumps whatever comes back
as hex:

```bash
.venv/bin/python tools/t95_squirt.py
.venv/bin/python tools/t95_squirt.py --commands MA6 --sizes 63 --terminators crlf
.venv/bin/python tools/t95_squirt.py --raw-hex '4d41360d0a'
```

**Careful:** the T95 does not answer every frame, and a frame it
dislikes can wedge its OUT endpoint (see Step 3b).  The T95 answers a
frame only if **its first two bytes are equal**; see
`PROTOCOL_NOTES.md` §5.6.

### Step 4 — what does *not* work yet

Setting the temperature.  The T95 speaks a **binary** protocol over HID
(`PROTOCOL_NOTES.md` §5.5), and the frame that changes the setpoint has
not been found.  Every ASCII command from the Linksys32 vocabulary has
been tried — `Prog rate`, `Prog Limit`, `Prog stop`, `prog ramp ...`,
`MA6`, `MC` and others, in a dozen encodings — and **none of them has
any effect**: no acknowledgement, and no change to any status field.

So the `run` command will not ramp the stage:

```bash
# THIS DOES NOT RAMP THE STAGE -- it reports success anyway
.venv/bin/python -m linkam.cli run --hid --target 40 --rate 5 --hold 60 --no-video
```

Do not use it to control the stage until the command frame is known.

### If the T95 does not answer

| Symptom | Likely cause |
|---|---|
| `find_adapter.py` does not see it | Not plugged in / not powered; check `lsusb` |
| `lsusb` shows `16da:0001 Linkam Bootloader` | Caught it during power-up; wait for it to re-enumerate as `16da:0002` |
| Cannot open `/dev/hidraw1` | udev rule not applied; re-plug, then check `ls -l` |
| `read --hid` says "did not answer the status poll" | Still warming up after power-on — wait a minute and retry |
| Every write fails with `ETIMEDOUT` | The OUT endpoint has wedged — power-cycle the stage (Step 3b) |

---

## The RS-232 route in detail

### Step 1 — identify the adapter

```bash
.venv/bin/python tools/find_adapter.py
```

It prints every serial port with the USB vendor/product IDs, the kernel
driver bound to it, and a verdict.

- `[OK] looks like a USB-to-RS232 adapter` → good, use that port.
- `[!!] USB-to-TTL breakout -- WILL NOT WORK` → **stop here.** Read below.
- `[ ? ] cannot tell` → probably fine; the adapter just has a bland
  product string. Continue to step 2 and let `probe` decide.

### Why the TTL warning matters

The Linkam programmer is **RS-232** (±5 V logic levels). A USB-to-TTL
breakout has no line-level transceiver, so it puts 0–3.3 V on the wire.
The programmer will never answer.

The failure looks exactly like a protocol/framing bug, so it can burn a
lot of time. If `probe` reports `NO RESPONSE` on every port and every
baud rate, suspect the adapter type before suspecting the code.

If you need to buy a replacement: a genuine **USB-to-RS232** adapter
(FTDI FT232, Prolific PL2303, or CH340/CH341 boards sold specifically
as "USB to RS232" with a DB9 socket), not a "USB-to-TTL" breakout.

---

## Step 2 — settle the framing (read-only)

Three things about the wire protocol were recovered statically but not
confirmed: the command terminator (CR / LF / CRLF), whether requests
carry a checksum, and the baud rate for your particular programmer.
`probe` settles the first two, and tries the baud rates, by issuing
**read-only queries only** — it is safe on a cold instrument.

```bash
.venv/bin/python -m linkam.cli probe --port /dev/ttyUSB0
```

On success it prints something like:

```
LINK ESTABLISHED
  baudrate   : 19200
  terminator : 'crlf'
  command    : 'MA6'
  response   : 'Temp 23.4 °C'
```

Use the baud it reports in step 3. If it says `terminator : 'cr'`, also
add `--strict-framing` to the run command.

**If it prints `NO RESPONSE`**, work through this list:

1. Is the programmer **powered on**? (Linkam units need their own PSU.)
2. Is the adapter genuine RS-232 (step 1)?
3. **DB9 gender**: programmer **female** socket → adapter **male**;
   programmer **male** → adapter **female**. Try a null-modem crossover
   if it is male-to-male or female-to-female.
4. Try the other USB port (some are hub-powered).
5. `lsusb` to confirm the adapter is enumerated at all.
6. Last resort, sniff what the vendor software sends:
   ```bash
   socat -v -x /dev/ttyUSB0,raw,echo=0 -
   ```
   Run the real Linksys32 alongside this and you will see the exact
   bytes it uses — that closes all three questions at once.

---

## Step 3 — first temperature test

Prove the temperature link on its own before adding anything else. It is
the part you cannot work around.

```bash
.venv/bin/python -m linkam.cli run \
    --port /dev/ttyUSB0 --baud 9600 \
    --target 40 --rate 5 --hold 60 --no-video \
    --out results/first_test
```

This sends one compound command, `prog ramp 1 rate 5 limit 40 time 60`,
then `Prog start`. The ramp itself executes **inside the instrument** —
once started, the hardware ramps on its own and the host only logs
telemetry. A stalled Python process cannot cause an overshoot.

Output in `results/first_test/`:

| File | Contents |
|---|---|
| `telemetry.csv` | timestamp, elapsed, and every measured channel |
| `run.json` | run metadata |
| `capture.mp4` | only if video was enabled |

Watch `telemetry.csv` while it runs:

```bash
watch -n1 'tail -2 results/first_test/telemetry.csv'
```

### If it will not ramp

`ProgrammerNotRamping` means the instrument is *reporting* temperature
but *not moving* — usually because it is unlicensed, in demonstration
mode, or OEM mode. The error message says so explicitly. Check the
status channel for `Not licensed`, `Demonstration expired` or
`Comms locked off`.

The check is rate-aware, so a genuinely slow ramp (1 °C/min) is not
mistaken for a stall. Tune with `--stall-timeout <seconds>` (0 disables
it) and `--min-progress <celsius>`.

### Then scale up

Once the link is proven and you have seen a real ramp, you can go to
your actual experiment:

```bash
.venv/bin/python -m linkam.cli run \
    --port /dev/ttyUSB0 --baud 9600 \
    --target 95 --rate 2 --hold 600 --no-video \
    --out results/exp1
```

---

## Measuring temperature only (no ramp)

If you just want to watch the current temperature without programming
anything, the telemetry channel reports it every 0.2977 s. This prints
live readings and sends no commands at all:

```python
from linkam import LinkamProgrammer

p = LinkamProgrammer("/dev/ttyUSB0", 9600)
p.open()
p.on_telemetry(lambda t: print(f"{t.temperature} {t.units.get('temperature')}"))
input("Ctrl-C to stop\n")
p.close()
```

---

## Safety

Read this before the first real run.

- **There is no overshoot protection in this code.** Nothing checks that
  the measured temperature is tracking the requested limit, and
  `programme_ramp` writes to profile row 1 without confirming what is
  already in it.
- The ramp executes in the hardware, so a stalled host process cannot
  cause an overshoot — but a **wrong limit** can, and the host will not
  notice. This is why step 3 starts at 40 °C.
- Start with a modest rate and a limit well below anything that would
  damage your sample.
- The instrument may refuse to ramp when unlicensed, independently of
  the host software.
- If you interrupt with Ctrl-C, the code sends `Prog stop`.

---

## Rehearse without hardware

The package ships a simulator that speaks the real wire format and
honours the real command grammar. Use it any time to check a change
without touching the instrument:

```bash
.venv/bin/python -m linkam.simulator --target 95 --rate 20 --duration 10
```

---

## Troubleshooting quick reference

| Symptom | Likely cause |
|---|---|
| `probe` → `NO RESPONSE` | Adapter is TTL not RS-232; wrong DB9 gender/crossover; programmer not powered; wrong baud |
| `ProgrammerNotRamping` | Unlicensed / demonstration / OEM mode; rate is 0; limit equals current temperature |
| `Comms locked off` in status | Instrument has locked the comm port |
| `Not licensed` | Hardware licence state — check with Linkam |
| Port not in `find_adapter` output | Driver not bound — `sudo modprobe cp210x` / `ftdi_sio` / `ch341` |
| `PermissionError` on the port | Not in `dialout` (you are: `groups` includes `dialout`) |
| Video fails to open | Expected on this laptop — the BT848 card has no Linux driver. Use `--no-video`. |

---

## Optional: capture video anyway

The Linkam VideoFusion card (Conexant BT848) is a PCI device with no
mainline Linux driver. Options, in order of effort:

1. **USB capture stick** — plug into the laptop; appears as
   `/dev/video2`; use `--video-backend v4l2 --video-device /dev/video2`.
   This is the cheapest route if you want video tomorrow.
2. **Windows host** — the stock driver registers as "Conexant's BtPCI
   Capture" and `cv2.CAP_DSHOW` opens it directly; capture there and
   stream.
3. **The laptop's own webcam** — `/dev/video0`, but that is the
   integrated camera, not the stage view. Only useful as a smoke test
   that the capture path works.

Video is a separate, best-effort path: frames are timestamped from the
wall clock, so a slow encoder cannot stall the temperature record.
