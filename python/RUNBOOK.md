# Runbook — Linkam hot-stage, first temperature test

Prepared on this laptop (Debian, `jonathan-laptop`). Everything below is
already installed and tested; the only thing missing is the hardware.

---

## TL;DR — tomorrow, in order

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

Do **not** skip step 2 and do **not** go straight to 95 °C. See "Safety"
below.

---

## What is ready

| Thing | Status |
|---|---|
| Repo cloned | `~/Documents/Nottingham/Research/Linkam/linkam-linksys32` |
| Virtualenv | `python/.venv` (Python 3.14.8) |
| Dependencies | pyserial 3.5, numpy 2.5.4, opencv-python 5.0.0, pytest 9.2.0 |
| Test suite | 77 passed (`pytest tests/ -q`) |
| `doctor` check | 0 FAIL, 1 WARN (video — expected, see below) |
| `socat` 1.8.1.3 | installed at `/usr/bin/socat`, for serial sniffing |
| Adapter finder | `tools/find_adapter.py` |
| User permissions | `burley` is in `dialout` — serial ports are accessible |

The one WARN from `doctor` is the video backend: this laptop has no
Conexant BT848 driver (there is no mainline Linux driver for it), and
the two `/dev/video*` devices are the laptop's own **integrated webcam**.
That does not affect the temperature work at all — and given your
starting goal (measure temperature, set a target, set a ramp), you do
not need video yet. Use `--no-video` until the temperature link is
proven.

---

## Changes made to the repo

Two small fixes, both verified:

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

---

## Step 1 — identify the adapter

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
