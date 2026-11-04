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
  protocol.py    Linkam temperature programmer serial protocol
  video.py       video capture backends (DirectShow / V4L2)
  experiment.py  orchestration: ramp + hold + concurrent capture
  simulator.py   software simulator of the programmer (no hardware needed)
  cli.py         command-line entry point
PROTOCOL_NOTES.md  what was recovered, and what is still unverified
tests/           23 tests, no hardware required
```

---

## Install

```bash
pip install -r requirements.txt
```

## Use

```bash
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

## Provenance

Derived from the files on the Linksys32 CD (Linkam Scientific Instruments,
Linksys32 thermal analysis / microscopy control software, 2003). See
`PROTOCOL_NOTES.md` for what was recovered, the sources it came from, and
what remains unverified.
