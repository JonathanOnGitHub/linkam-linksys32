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
  t95.py         the T95 protocol, re-exported for convenience
  t95_protocol.py  the T95's binary frame format and command vocabulary
  programme.py   multi-step temperature programmes (host-driven stepping)
  video.py       video capture backends (DirectShow / V4L2)
  experiment.py  orchestration: ramp + hold + concurrent capture
  simulator.py   software simulator of the programmer (no hardware needed)
  diagnostics.py environment checks (read-only, no instrument needed)
  cli.py         command-line entry point
  webapp.py      a small web GUI: live temperature, ramps, CSV recording
  static/index.html  the GUI's page
PROTOCOL_NOTES.md  what was recovered, and what is still unverified
RUNBOOK.md     step-by-step first-run checklist for real hardware
tools/
  find_adapter.py  identify the instrument (USB HID) or the USB-to-RS232 adapter
  t95_hid_probe.py read-only HID reconnaissance: descriptor + framing probe
tests/           235 tests, no hardware required
```

> New to this, and holding real hardware? Start with **`RUNBOOK.md`** — it
> is an ordered checklist that takes you from "adapter plugged in" to a
> first measured temperature, with the failure modes explained.

---

## Two links, two protocols

The legacy RS-232 link is **line-oriented ASCII text**.  That was
established statically from the shipped binaries: `LinksysActXLib.ocx`
reads replies with VB's `__vbaLineInputStr`, and the telemetry frame is
a plain string such as

```
Temp 23.4 °C~Rate 10 °C/min~Limit 25.4 °C
```

`~` separates fields, `;` separates sub-values within a field.

The T95's USB HID link is a **binary block protocol** instead, and it
has been recovered in full from Linkam's own SDK — see
`linkam/t95_protocol.py` and `PROTOCOL_NOTES.md` §5.6.

So the package is split at the seam between the physical link and the
application layer:

| Link | Transport | Application layer |
|------|-----------|-------------------|
| RS-232 (legacy Linksys32) | `transport.SerialTransport` | ASCII line, CR/LF/CRLF, baud rate |
| USB HID (**Linkam T95**) | `hid.HidTransport` | binary 64-byte blocks, command ids |

Everything above the transport — telemetry normalisation, ramp
sequencing, stall detection, CSV output — is shared.  `LinkamProgrammer`
is the serial one; `HidProgrammer` is the T95 one, and it inherits all
of the ramp logic.

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

# set the stage temperature -- THIS MOVES THE STAGE
python -m linkam.cli set --port /dev/ttyUSB0 --target 95 --rate 2

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
# find the device and report the hidraw node
python tools/find_adapter.py

# READ THE TEMPERATURE (read-only)
python -m linkam.cli read --hid

# SET THE TEMPERATURE -- THIS MOVES THE STAGE
python -m linkam.cli set --hid --target 95 --rate 2

# a series, and machine-readable output
python -m linkam.cli read --hid --count 60 --interval 1
python -m linkam.cli read --hid --json

# the HID report descriptor (read-only)
python tools/t95_hid_probe.py --descriptor-only
```

> **Reading the temperature works — and so does setting it.**
>
> The T95's USB link is **not** the ASCII, line-oriented application
> layer that Linksys32 uses over RS-232.  It is a **binary block
> protocol**, and it has now been recovered in full from **Linkam's own
> SDK** (their commercial device driver, published unstripped with full
> debug information in the `DiamondLightSource/linkam3` repository).
>
> * A **status poll** is one command, `eCMDT95_GETLINK` (`0x0300`),
>   written as a single 64-byte HID block.  The device answers with
>   three 63-byte blocks carrying the stage temperature as a
>   little-endian float32 at payload offset 0.
> * **The instrument does not report its programme.**  The status
>   payload has no field that changes when a rate or a setpoint is
>   programmed — Linkam's own SDK keeps those values host-side for the
>   same reason, and so does this package
>   (`HidProgrammer.programmed_rate` / `programmed_setpoint`).
> * **Setting the temperature** is one command, `eCMDT95_SETMULT`
>   (`0x0301`), carrying a 96-byte `MultiCommand` payload:
>   `cmd[0]=0x0382` (rate), `cmd[1]=0x0383` (setpoint),
>   `cmd[2]=0x0380` (start).  Rate, setpoint and start go out
>   together, so the instrument can never act on a half-applied
>   programme.
>
> See `linkam/t95_protocol.py` for the framing and the command
> vocabulary, and `PROTOCOL_NOTES.md` §5.6 for the evidence.
>
> The one thing left to check against real hardware is that the
> controller's firmware matches this framing; if the status poll does
> not answer, the firmware may be too old, and Linkam publishes a
> bootloader programme for updating it (`PROTOCOL_NOTES.md` §5.9).
>
> ```bash
> # read the temperature
> python -m linkam.cli read --hid
>
> # set the temperature -- THIS MOVES THE STAGE
> python -m linkam.cli set --hid --target 95 --rate 2
> ```

Two behaviours to know about:

* the controller enumerates **twice** on power-up — first as
  `16da:0001 Linkam Bootloader`, then as `16da:0002 Linkam T95`;
* after a power cycle it answers **nothing** for about a minute while
  it warms up.  This is easy to mistake for a dead link.

If every write starts failing with `ETIMEDOUT`, the firmware has
stopped servicing its OUT endpoint; power-cycle the controller (off,
wait 10 s, on) and replug the USB cable.

### The web GUI

A small browser interface: the stage temperature live on screen, ramps
and holds, multi-step programmes, and everything recorded to a CSV as
it happens.

```bash
# start it -- this opens the instrument
python -m linkam.webapp --hid

# then open http://127.0.0.1:8765/

# or try it with no hardware at all -- nothing is moved
python -m linkam.webapp --simulate
```

It listens on **both** `127.0.0.1` and `::1`, and prints the URLs it is
serving.  Both matter: on this machine `getent ahosts localhost`
returns `::1` **first**, so a server that listened only on IPv4 was
unreachable under the name `localhost` — `curl -6 http://localhost:8765/`
was refused, and a browser could show nothing at all.  If the page still
does not come up, use the numeric address the server prints, and check
that you are on the same machine (`--host 0.0.0.0` makes it reachable
from another one).

It needs no web framework — it is the standard library's
`http.server` — so nothing has to be installed.

**One process at a time.**  This matters more than usual for this
instrument: the T95's reply blocks are indistinguishable from one poll
to the next, so **two processes polling the same hidraw node steal each
other's blocks** and both read garbage.  The web server is the process
that owns the instrument; the browser is only a view, and it never
touches the hardware.  So do not run `linkam.cli` at the same time as
the GUI — and do not run two copies of the GUI.

The page shows:

* the stage temperature, large, updating twice a second, with a
  sparkline of the last few minutes;
* the second thermocouple (TC2), the programmed target and rate, and
  what the programme is doing;
* controls for a single ramp-and-hold, and for a multi-step programme
  in the same `target:rate:hold` form the command line uses;
* a log of the programme's phase changes, and a link to download the
  CSV.

The recording starts when the server does and goes to
`results/webui/<timestamp>/telemetry.csv`, one row per sample:

```
timestamp,elapsed_s,temperature_c,sensor2_c,programmed_rate_c_per_min,programmed_setpoint_c,phase,event
```

`--out` puts it somewhere else, and `--host 0.0.0.0` makes it reachable
from another machine on the network.

Two things it does **not** do, because it cannot:

* it does not check the setpoint or the rate against the **cable's**
  limits, which the SDK reads from the controller.  An out-of-range
  setpoint is accepted here and may be refused by the instrument;
* it does not report the instrument's own view of the programme,
  because the T95 does not report it (see `PROTOCOL_NOTES.md` §5.6.6).
  The rate and the setpoint shown are the ones this host programmed.

Starting a ramp moves the stage, so the browser asks for confirmation
first, and there is a single always-visible **Stop** button.

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

On the RS-232 link the programme is the ASCII compound command
`prog ramp 1 rate 5 limit 40 time 60`. On the T95's USB link it is one
binary command, `eCMDT95_SETMULT`, carrying a `MultiCommand` with the
rate, the limit, the hold time and the start — so the two paths are
equivalent in what they mean and different only in how they are
encoded.

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

## Multi-step temperature programmes

The T95's programmer model is **one ramp plus a hold** — a rate, a limit
and a dwell time.  There is no native multi-segment programme table, so
a multi-step programme is driven by the host: send the next step's ramp,
notice when the temperature has arrived, send the step after that.  The
ramps still execute inside the instrument, so the host is never in the
control loop.

`linkam/programme.py` does this, and handles the awkward part: the
instrument runs the hold itself, so if the host waited for its own view
of the hold to finish before sending the next ramp, the instrument's
hold would already have expired and the stage would start cooling in
the gap.  The runner therefore **chains** — it arms the next step just
before the current hold expires, so the instrument ramps straight on
and the soak is preserved.

```python
from linkam.programme import Programme, ProgrammeRunner, Step

programme = Programme(
    name="melt-and-soak",
    steps=[
        Step(target_c=25.0, rate_c_per_min=5.0, hold_s=60.0, label="warm"),
        Step(target_c=60.0, rate_c_per_min=2.0, hold_s=600.0, label="soak"),
    ],
)
result = ProgrammeRunner(programmer, programme, on_event=print).run()
```

`on_event` is called for every phase change (`step_start`, `reached`,
`hold`, `step_end`, `programme_end`, `error`), which is what a UI would
render.  `runner.abort()` stops the run from another thread — that is
the Stop button.

---

## The Allied Vision camera (Vimba X)

`linkam/video.py` has a `VimbaXBackend` for Allied Vision cameras,
which is what the Alvium 1800 U-508c speaks.  Vimba X is the
GenICam-compliant successor to Vimba, and its Python binding is
`vmbpy`:

```bash
pip install vmbpy
```

On Linux the `vmbpy` wheel bundles the VmbC libraries but **not** the
transport layer, so Vimba X itself has to be installed as well and the
GenTL found through `GENICAM_GENTL64_PATH`.  Without it the SDK sees no
devices and the backend says so.

```python
from linkam.video import VimbaXBackend

with VimbaXBackend(0, exposure_us=5000, frame_rate=30) as cam:
    frame = cam.read()
```

```bash
# list the cameras the SDK can see
python -c "from linkam.video import VimbaXBackend; print(VimbaXBackend.list_cameras())"
```

---

## Hardware: the T95 and the camera on the same machine

**Put the T95 on a USB 2.0-only hub and the camera on the USB 3 port.**

This is not superstition.  The T95 is a USB 2.0 device with small,
latency-sensitive HID transfers.  A USB 3 camera on the same root hub
can starve them, and a starved T95 stops servicing its OUT endpoint
altogether: the symptom is a write that times out with `ETIMEDOUT`, and
the controller then needs a **power cycle** to recover.  That failure
looks exactly like a framing bug, and it is the single most expensive
thing to diagnose.

The important detail is that the hub must be **USB 2.0 only**, not a
USB 3 hub.  A USB 3 hub carries the T95 on the same USB 2 lanes as the
hub's USB 3 traffic, so it does not isolate anything; a USB 2.0-only
hub puts the T95 behind a transaction translator and keeps it clear of
the camera entirely.  On a laptop that has only USB-C ports, a USB 2.0
hub is the answer, and it needs to be powered if the camera and the
stage are both drawing from the same port.

The environment check now looks for exactly this:

```bash
python -m linkam.cli doctor
```

```
[warn] usb topology   T95 on usb1 at high speed (USB 2.0) — shares its root
                      hub with USB 3 device(s): 1-3.2 ...
```

and `lsusb -t` shows the tree.

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

The T95's binary protocol is implemented for the **temperature**
commands only. The SDK's command set is much larger — vacuum, motors,
tensile, humidity, the plunger stages — and none of that is covered
here.

Video capture assumes a **Conexant BT848** card (the VideoFusion device).
A modern USB camera will not be driven by the `dshow` backend as written.

---

## Provenance

Two sources, and they cover different links:

**The RS-232 link** is derived from the files on the Linksys32 CD (Linkam
Scientific Instruments, Linksys32 thermal analysis / microscopy control
software, 2003) — `LinksysActXLib.ocx`, `CommX.ocx`, `Linksys32.exe` and
the help file.

**The T95's USB HID link** is derived from **Linkam's own SDK** — the
commercial device driver their NEXUS and LINK software is built on.
The Linux build (`libLinkamSDK.so`, copyright 2018-2023 Linkam Scientific
Instruments) is published unstripped, with full DWARF debug information,
in the public `DiamondLightSource/linkam3` repository; its public
headers (`USB.h`, `CommsAPI.h`, `LinkamSDK.h`, `Message.h`) are in the
same place.  `pylinkam` on PyPI is the Python binding to it.

See `PROTOCOL_NOTES.md` for what was recovered from each, the evidence,
and what remains unverified.

> The SDK binary and a per-machine licence file (`Linkam.lsk`) are not
> redistributed with `pylinkam`; they have to be obtained from Linkam.
> If a licence is available, `pylinkam` is the supported way to drive
> the stage and this driver is unnecessary.
