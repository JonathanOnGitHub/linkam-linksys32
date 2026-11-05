# Linkam Linksys32 — Python replacement

Python implementation of the temperature-control + simultaneous-video-capture
subset of **Linkam Linksys32**, reverse-engineered from the shipped CD.

> Scope: this replaces the *experiment loop* of Linksys32 — ramping a Linkam
> temperature programmer to a setpoint and holding it while recording video.
> It does **not** reimplement the .IML data format, the thermal analysis /
> DSC modules, the stage modules (MDS, shear, tensile), or the licensing.

---

## Layout

```
linkam/
  protocol.py    Linkam temperature programmer protocol + command vocabulary
  transport.py   physical-link abstraction (serial / HID)
  hid.py         USB HID transport for the Linkam T95 (16da:0002)
  video.py       video capture backends (DirectShow / V4L2)
  experiment.py  orchestration: ramp + hold + concurrent capture
  simulator.py   software simulator of the programmer (no hardware needed)
  diagnostics.py environment checks (read-only, no instrument needed)
  cli.py         command-line entry point
PROTOCOL_NOTES.md  what was recovered, and what is still unverified
RUNBOOK.md     step-by-step first-run checklist for real hardware
tools/
  find_adapter.py  identify the instrument (USB HID) or the USB-to-RS232 adapter
  t95_hid_probe.py read-only HID reconnaissance: descriptor + framing probe
tests/           110 tests, no hardware required
```

> New to this, and holding real hardware? Start with **`RUNBOOK.md`** — it
> is an ordered checklist that takes you from "adapter plugged in" to a
> first measured temperature, with the failure modes explained.

---

## Two links, one protocol

The Linkam application layer is **line-oriented ASCII text** regardless of
the physical link.  That was established statically from the shipped
binaries: `LinksysActXLib.ocx` reads replies with VB's
`__vbaLineInputStr`, and the telemetry frame is a plain string such as

```
Temp 23.4 °C~Rate 10 °C/min~Limit 25.4 °C
```

Only the *framing* differs between links, so the package is split at
that seam:

| Link | Transport | Notes |
|------|-----------|-------|
| RS-232 (legacy Linksys32) | `transport.SerialTransport` | ASCII line, CR/LF/CRLF, baud rate |
| USB HID (**Linkam T95**) | `hid.HidTransport` | ASCII line inside an HID report |

Everything above the transport — telemetry parsing, ramp sequencing,
stall detection, CSV output — is shared.  `LinkamProgrammer` is the
serial one; `HidProgrammer` is the T95 one, and it inherits all of the
ramp logic.  The command vocabulary (`Prog start`, `prog ramp 1 rate 5
limit 40 time 60`, …) is **identical** on both.

### The T95 is USB HID, not a serial port

The T95 enumerates as `16da:0002` and appears as a `/dev/hidraw*` node.
It needs **no** USB-to-RS232 adapter.  On Linux:

```bash
python -m linkam.cli doctor      # reports the HID device and its permissions
python tools/find_adapter.py      # same, with the "what do I do next" hints
```

Permission to open the node comes from a udev rule:

```
# /etc/udev/rules.d/70-linkam-t95.rules
SUBSYSTEM=="hidraw", ATTRS{idVendor}=="16da", ATTRS{idProduct}=="0002", \
    MODE="0660", GROUP="plugdev", TAG+="uaccess"
```

---

## Install

```bash
pip install -r requirements.txt
```

---

## First: check the machine is ready

Before plugging anything in, run the environment check. It is entirely
read-only — it does not open the port or send a command to the instrument:

```bash
python -m linkam.cli doctor
```

Then, once the instrument is plugged in, identify it:

```bash
python tools/find_adapter.py
```

This lists every serial port with its USB vendor/product IDs, the
kernel driver bound to it, and a judgement on whether it is a genuine
**USB-to-RS232** adapter or a **USB-to-TTL** breakout (the latter will
*not* drive the instrument).

`doctor` reports:

* Python version and whether `pyserial` / `numpy` / `opencv-python` import;
* every serial port, with a judgement on whether it looks like a genuine
  **USB-to-RS232** adapter or a **USB-to-TTL** breakout (the latter will
  *not* drive the instrument);
* whether the video backend is usable on this platform;
* the protocol questions that can only be settled against real hardware;
* whether `socat` is available for serial sniffing.

Exit code is `0` when nothing failed, `1` if something did. Add `--json` for
machine-readable output.

The most valuable thing it can tell you: if your adapter is a USB-to-TTL
breakout, it will say so. A TTL port has no RS-232 transceiver, so its
levels are wrong and the programmer will never answer — a failure that looks
identical to a framing bug.

---

## Use

```bash
# check this machine is ready (read-only)
python -m linkam.cli doctor

# list serial ports
python -m linkam.cli ports

# settle the framing against real hardware (read-only queries)
python -m linkam.cli probe --port /dev/ttyUSB0 --baud 9600

# isothermal experiment: ramp to 95 C at 2 C/min, hold 10 min, record video
python -m linkam.cli run \
    --port /dev/ttyUSB0 --baud 9600 \
    --target 95 --rate 2 --hold 600 \
    --video-backend dshow --video-device 0 \
    --out results/exp1
```

Outputs in `--out`:

| File | Contents |
|---|---|
| `telemetry.csv` | timestamp, elapsed, and every measured channel |
| `capture.mp4` | the video |
| `frames/` | individual JPEGs (only with `--save-frames`) |
| `run.json` | run metadata |

### The T95 (USB HID)

The T95 uses `--hid` instead of `--port`, and needs no serial adapter:

```bash
# reconnaissance: find the device, dump the HID report descriptor,
# and work out how the ASCII payload is framed (all read-only)
python tools/t95_hid_probe.py 30

# probe the link (read-only)
python -m linkam.cli probe --hid

# temperature only, modest limit, no video
python -m linkam.cli run --hid \
    --target 40 --rate 5 --hold 60 --no-video \
    --out results/first_test
```

`probe --hid` tries each candidate report framing (`raw`, `id_text`,
`text_padded`, `length_prefixed`) and reports which one the instrument
answers.  Pass the winner back with `--hid-framing`.  Once that is
settled it should be hard-coded as the default.

### As a library

```python
from linkam import Experiment, IsothermalRun

exp = Experiment(
    target_c=95.0,
    rate_c_per_min=2.0,
    hold_s=600.0,
    output_dir="results/exp1",
    serial_port="/dev/ttyUSB0",
    video_backend="dshow",
    video_device=0,
)
result = IsothermalRun(exp).start()
print(result.summary())
```

For the T95, use `HidProgrammer` directly — it is the same object with a
different transport, so every method (`programme_ramp`, `start`, `stop`,
`on_telemetry`, `wait_for_temperature`) behaves identically:

```python
from linkam import HidProgrammer

p = HidProgrammer()
p.open()
p.on_telemetry(lambda t: print(f"{t.temperature} {t.units.get('temperature')}"))
input("Ctrl-C to stop\n")
p.close()
```

---

## How it works

**Temperature.** The Linkam programmer ramps *in the hardware*. Once
`Prog start` is issued the instrument heats/cool at the programmed rate
without any further host involvement. The host's job is to (a) send the
programme and (b) log telemetry. This matters: a stalled Python process
cannot cause an overshoot, because the ramp does not depend on the host.

Telemetry arrives every **0.2977 s** as a single text string:

```
Temp 23.4 °C~Vac 0.1 mBar;Rate 10 °C/min~Limit 25.4 °C
```

`~` separates fields, `;` separates sub-values within a field.

**Video.** The Linkam VideoFusion card registers the *standard* DirectShow
Video Capture filter CLSID (`{17CCA71B-…}`) under the friendly name
"Conexant's BtPCI Capture", and `lnkFusion.dll` drives it with the standard
`ICaptureGraphBuilder2` + `SampleGrabber`. So no video reverse engineering
was needed — any DirectShow/V4L2 capture library opens it.

**Concurrency.** Temperature and capture run in separate threads and are
correlated by wall-clock timestamp, not frame index. A slow encoder cannot
stall the temperature record.

---

## Hardware connection

The temperature programmer is an **RS-232** device. From the Linksys32
manual shipped on the CD:

> "The File is used to save and open various files and to connect the
> software to the programmer via the **RS232 comm port on the PC**."

> "If you see the message 'No Comm — Comm link broken...' the **RS232 cable**
> has been connected to a different Comm port than selected in the
> Temperature Programmer Setup menu. Check the back of your PC..."

There is no USB anywhere in the software (`USB` appears zero times in the
manual), and the firmware tool's `CHIPDEF.INI` specifies RS-232 baud rates
of 4800 / 9600 / 19200 / 38400.

### What you need

| | |
|---|---|
| Cable | **USB-to-RS232 serial adapter** (genuine RS-232, ±5 V levels) |
| Not | a USB-to-TTL cable (FTDI/CH340 breakout) — the levels are wrong |
| Connector | DB9; check the gender on your programmer and whether a null-modem crossover is needed |

If the programmer has a DB9 **female** socket you want a DB9 **male** on the
adapter (straight-through). If it has a DB9 **male** you want a DB9 **female**
and may need a crossover. "Comm link broken" usually means wrong wiring or
framing rather than a broken cable.

### Finding the right port and speed

```bash
python -m linkam.cli ports              # list serial ports
python -m linkam.cli probe --port /dev/ttyUSB0
```

`probe` sweeps baud rate × terminator × read-only command and reports the
combination that answered, e.g.:

```
LINK ESTABLISHED
  baudrate   : 19200
  terminator : 'crlf'
  command    : 'MA6'
  response   : 'Temp 23.4 °C'
```

Then use those values for the run.

---

## Platform notes

| Platform | Temperature link | Video link |
|---|---|---|
| Windows | `COM` port, e.g. `COM3` | DirectShow via `cv2.CAP_DSHOW` — works with the stock Linkam driver |
| Linux | `/dev/ttyUSB0` via USB-serial adapter | **No mainline Linux driver exists for the Conexant BT848.** Capture on a Windows host and stream, or use a USB capture stick / V4L2 camera instead |

---

## Rehearse without hardware

The package includes a simulator of the programmer that speaks the real wire
format and honours the real command grammar, so you can exercise the whole
workflow before touching the instrument:

```bash
python -m linkam.simulator --target 95 --rate 20 --duration 10
```

It prints telemetry exactly as the hardware would send it, and reports the
commands it received.

---

## Before it will work on real hardware

Three things are not yet confirmed statically — see `PROTOCOL_NOTES.md` §2:

1. the command terminator (CR / LF / CRLF) — `probe()` settles it;
2. whether requests carry a checksum;
3. the baud rate for your particular programmer (`CHIPDEF.INI` suggests
   4800/9600/19200/38400 depending on model and clock).

The fastest way to close all three at once is to run the shipped Linksys32
alongside a serial sniffer:

```bash
socat -v -x /dev/ttyUSB0,raw,echo=0 -
```

That shows exactly what the vendor software sends and expects.

---

## First run on real hardware

Recommended order. Each step is gated on the previous one working.

**1. Environment.** `python -m linkam.cli doctor` — must not report FAIL.

**2. Link.** Settle the framing. This issues read-only queries only, so it is
safe on a cold instrument:

```bash
python -m linkam.cli probe --port /dev/ttyUSB0 --baud 9600
```

It sweeps baud × terminator and prints the combination that answered. If it
prints `NO RESPONSE`, work through the checklist it prints: programmer
powered on, correct DB9 gender/crossover, and confirm the adapter is
genuine RS-232 rather than TTL.

**3. Temperature only.** Before adding video, prove the temperature link on
its own — it is the part you cannot work around:

```python
from linkam import LinkamProgrammer

p = LinkamProgrammer("/dev/ttyUSB0", 9600)
p.open()
p.set_rate(5.0)
p.set_limit(40.0)      # use a safe, non-damaging limit for the first test
p.start()
print(p.wait_for_temperature(40.0, timeout=600))
p.stop()
p.close()
```

**4. Video.** Only once temperature is reliable. Use `--no-video` on a
`run` command to isolate the two paths.

### Safety notes

* There is **no overshoot protection** in this code. Nothing checks that the
  measured temperature is tracking the requested limit, and `programme_ramp`
  writes to profile row 1 without confirming what is already in it.
* The ramp executes **in the hardware**, so a stalled Python process cannot
  cause an overshoot — but a *wrong* limit can, and the host will not notice.
* Start with a modest rate and a limit well below anything that would
  damage the sample.
* The instrument may refuse to ramp when unlicensed, independently of the
  host software. Watch for `Not licensed` / `Demonstration expired` /
  `Comms locked off` in the status channel.

### If the ramp is refused

If the instrument will not heat or cool, `wait_for_temperature` notices
that the temperature is being reported but is not moving, and raises
`ProgrammerNotRamping` rather than hanging until the ramp timeout:

```
ProgrammerNotRamping: the programmer is reporting 23.40 C but is not moving
toward 95.00 C (closest approach 23.40 C) after 120s. The ramp was most
likely refused: check for an unlicensed instrument, demonstration mode or
OEM mode, confirm the programmed rate is non-zero, and check that the limit
differs from the current temperature.
```

This is on by default with a 120 s window. It is rate-aware: a genuine
slow ramp is judged against the rate the programmer reports, so a 1 C/min
ramp is not mistaken for a stalled one.

```python
from linkam import ProgrammerNotRamping

try:
    p.wait_for_temperature(95.0, stall_timeout=60.0)
except ProgrammerNotRamping as exc:
    print(exc)
```

Tune it with `--stall-timeout <seconds>` (use `0` to disable and wait for
the full ramp timeout) and `--min-progress <celsius>` on the `run`
command.

---

## Scope limits

This package drives the **temperature programmer** only. It does **not**
implement the stage command vocabulary (`MA*`, `MM*`, `MV*`, `ZA`/`ZF`,
`stdy`, `cw`, …). If your unit is an MDS gradient stage, stage positioning
and stage registers are out of scope and will not work — see
`PROTOCOL_NOTES.md` §1.1b.

Video capture assumes a **Conexant BT848** card (the VideoFusion device).
A modern USB camera will not be driven by the `dshow` backend as written.

---

## Provenance

Derived from the files on the Linksys32 CD (Linkam Scientific Instruments,
Linksys32 thermal analysis / microscopy control software, 2003). See
`PROTOCOL_NOTES.md` for what was recovered, the sources it came from, and
what remains unverified.
