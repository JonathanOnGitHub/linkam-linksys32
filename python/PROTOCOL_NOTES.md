# Linkam serial protocol — reverse-engineering notes

Everything here was recovered statically from the files on the Linksys32 CD.
Nothing was guessed except where explicitly marked **UNVERIFIED**.

Sources:

| File | What it gave us |
|---|---|
| `Linksys32Help.chm` | The `ProgNewData` telemetry format and the 0.2977 s cadence; the semantics of Rate/Limit/Hold; the full ActiveX method list and event list |
| `LinksysActXLib.ocx` (v2.2.0.6) | The command keyword strings; the telemetry field-tag vocabulary; status/error strings; unit strings; the `WindowsAPI` import list |
| `CommX.ocx` | The serial framing layer: `WriteCharProtocol`, `ReadFrameHeader`, `ReadHexFrameHeaderData`, `ReadCrc32`, `CRCsDiffer`; **ZMODEM** for bulk file transfer; `GetVolumeInformationA` |
| `Linksys32.exe` | Which `lnk*.dll` camera back-ends are loaded and when; the `WindowsAPI` declarations |

---

## 1. The temperature programmer

### 1.1 Command keywords (verbatim from `LinksysActXLib.ocx`)

Found as UTF-16 string literals, contiguous in the file:

```
Prog start        Prog stop        Prog Heat        Prog Cool      Prog Hold
Prog rate <n>     Prog Limit <n>   Prog time <n>
prog ramp <n>     prog picdelay <n>   prog clearbuffer   Prog samplecal
prog stop         Prog Stop        Prog Rate
```

Note the case inconsistency in the original (`Prog rate` vs `prog rate`,
`Prog Limit` vs `prog limit`) — the firmware evidently accepts both, and the
Python implementation mirrors the exact strings the VB6 code sends.

### 1.1a The programmer accepts COMPOUND commands

This is the single most important protocol detail, and it is easy to miss.

The OCX contains a **command keyword table** at `0x1c938`–`0x1d400`:

```
cool  hold  rate  limit  time  start  stop  heat  ramp  samplecal
clearbuffer  picdelay  lnp  manual  auto  vac  shear  tensile  incubator
dsc  normal  cal  units  lock  unlock  intensity  clear  rotary
velocity  amplitude  frequency  gap  up  down  ref  heater  bottom
top  current  low  mid  high  set  reference  ...
```

and, separately at `0x1e308`–`0x1e398`, the **profile row builder**:

```
'prog ramp '   ' rate '   ' limit '   ' time '
'prog rate '   'prog limit '   'prog time '
' picdelay '   'prog picdelay '
```

The profile builder assembles a **single command line** containing a verb
plus one or more `keyword value` pairs:

```
prog ramp <row> rate <r> limit <lim> time <t>
```

So the grammar is:

```
<verb> [ <keyword> <value> ]*
```

with verbs `prog start|stop|heat|cool|hold`, `prog ramp`, `prog rate`,
`prog limit`, `prog time`, `prog picdelay`, `prog clearbuffer`,
`prog samplecal`.

Sending one compound command is preferable to four separate ones, because
the programmer can never act on a half-applied programme. The Python
implementation therefore uses `programme_ramp()` to issue a single
`prog ramp 1 rate <r> limit <lim> time <t>`.

### 1.1b Stage command vocabulary

These are the **stage** commands (MDS / gradient / tensile), not the
programmer:

```
M0  M1  Mp  MC  Pa0
MA1 MA6 MA7 MA8 MA9   MF1 MF2   MFP
MLX MLZ                MMm  MMm1000
MMR MMX MMY MMZ        MP   MR
MSA MSX MSZ            MVX  MVY  MVZ   MVZ20000 MVZ4000
MMV0 MMV1 MMVP
ZA  ZF  ZV   Zi  Zd  Zu  Zv   c0  c1
stdy  step  osc  relax   cw  acw   n/a
```

`stdy`/`step`/`osc`/`relax` are motion modes; `cw`/`acw` are directions.


### 1.2 Telemetry format

From `Linksys32Help.chm`, *TempControl ActiveX.htm*, "ProgNewData Event":

> As the Linkam programmers and other support items can be fitted with a wide
> variety of options all the data is passed by this event as a string of
> varying length. The general format is as follows but the order may change
> depending on the options found by the programmer e.g. for a single
> temperature control with vacuum option the following string would be
> passed.
>
> `Temp 23.4 °C~Vac 0.1 mBar;Rate 10 °C/min~Limit 25.4 °C`
>
> Occurs every time new data is sub-sampled by the programmer and serial
> communications module to give a value every 0.2977 seconds.

So:

* fields separated by `~`
* a field is `<tag> <number> <unit>`
* optional fields are prefixed with `~`
* a field can carry several sub-values separated by `;`
  (here `Vac 0.1 mBar;Rate 10 °C/min` is one field with two measurements)

### 1.3 Field vocabulary

Recovered from the `LinksysActXLib.ocx` string table (`0x17e7c`–`0x18168`).
Mandatory: `Temp`, `TempCal`, `Temp2`, `Rate`, `Rate2`, `Limit`, `Limit2`,
`Ramp Row`, `Ramp2 Row2`.
Optional (`~`): `Vac`, `DSC`, `DSCCal`, `Res`, `Intensity`, `Force`,
`Strain`, `Stress`, `Dist`, `RH`, `CO2`, `O2`, `PH1`, `PH2`, `FLOW`,
`Press`, `Current`, `IO`.

Units: `°C`, `Pa`, `mBar`, `Torr`, `Micro`, `PH`, `cc/min`, `mA`, `um`, `%`,
`N/mm2`.

### 1.4 Status and error strings

```
Under Range            Stage sensor open circuit    Power surge to the stage
Hardware error         Cooling too fast             Profile finished
Heating  Cooling       Holding at limit             Holding limit time
Holding temperature
Programmer not connected            Programmer 2 not connected
Demonstration expired              Not licensed
OEM mode                            Comms locked off
Invalid locale setting             Set display to 96 DPI
Data error                         File input/output error
Registry error                     Profile error
Communications error               CSS450 error
Motor data error                   Error in the received data
Received data not recognised       Cannot open the comm port
Comm link broken                   Power on your Linkam equipment
```

### 1.5 Control semantics

From *Programmer Module.htm*:

* **Rate** — heating/cooling rate in °C/min.
* **Limit** — temperature limit in °C.
* **Hold** — hold time, switchable between minutes and seconds.
* **Cool** — "If the programmer is heating or holding, Cool will force cooling
  at the current rate."
* **Heat** — symmetric.
* **Hold** — "If the programmer is heating or cooling, Hold will force the
  current temperature to be held." If already holding at the end of a ramp,
  Hold stops the countdown.
* **Start Profile** — runs the profile table if "Use Profile" is set,
  otherwise uses the Rate/Limit/Hold boxes.

This means the ramp is executed **inside the hardware**, not by the host.
Once `Prog start` is issued, the programmer ramps autonomously. That is the
single most important fact for the Python design: the host is not in the
control loop, so a stalled Python process cannot cause an overshoot.

### 1.6 Per-stage limits

`ProgLim.lim` (CSV) gives the allowed envelope per programmer/stage:

```
seq,id,description,programmer,min,max,rate_resolution,max_rate,sensor
1,129,TMS/CI - THM 600,TMS/CI - MDS 600,-200,600,0.01,130.00,None
8,137,CSS 450,CSS 450,-200,450,0.01,30.00,Lvdt
22,150,Vacuum Stage,Vacuum Stage,-200,350,0.01,130.00,Vac
```

The error string *"The programmer's limits and rates cannot be recognised.
Contact Linkam for an update"* is raised when the connected programmer is not
in this table.

---

## 2. Framing

### 2.1 What is known

* `CommX.ocx` implements the transport. Named routines:
  `WriteCharProtocol`, `ReadFrameHeader`, `ReadBinaryHeaderData`,
  `ReadHexFrameHeaderData`, `ReadByte`, `GetHeaderType`, `GetPad`,
  `ReadCrc32`, `CRCsDiffer`, `SendDataFrame`, `SendFile`, `ReadFileData`.
* It links `WriteFile`, `ReadFile`, `SetCommState`, `SetupComm`,
  `SetCommTimeouts`, `PurgeComm`, `ClearCommError`, `WaitCommEvent`.
* `LinksysActXLib.ocx` uses `__vbaFileOpen` + `__vbaLineInputStr`, i.e.
  VB's **line input**. That is the strongest single piece of evidence that
  the application layer is **line-oriented text**.
* `CommX.ocx` also contains a complete **ZMODEM** implementation
  (`ZRINIT`, `ZRQINIT`, `ZPAD`, `ZDLE`, `ZFIN`, `ZSKIP`, `ZRPOS`, `ZACK`,
  `ZCAN`, `ZEOF`, `ZSINIT`, `RINIT`) with CRC-32. That is for **bulk file
  transfer** (the firmware updater), not for the temperature link.

### 2.2 What is UNVERIFIED

1. **The exact command terminator.** CR, LF or CRLF. Most Linkam
   programmers of this era accept all three.
2. **Whether a checksum is appended to requests.** `ReadCrc32` exists in
   `CommX.ocx`, but it may belong only to the ZMODEM path.
3. **Whether responses are checksummed.** The status strings are plain
   prose (`"Cooling too fast"`), so at least the *data* responses look like
   plain text.
4. **The request/response correlation.** Does the programmer reply to
   `Prog rate 10` with an ack, or is it silent until the next telemetry?

### 2.3 How to settle it

`LinkamProgrammer.probe()` implements the experiment: it issues a read-only
query under each framing variant and reports which one the hardware
answers. Run it against the real instrument once, then set
`strict_framing` accordingly and hard-code the result.

A good first probe sequence, in increasing risk:

```
MA6        (read a stage register)
Prog rate 0  (set rate to zero — harmless)
Prog Limit <current>   (set limit to where it already is — harmless)
```

---

## 3. Video

### 3.1 The device is a standard DirectShow camera

`VideoFusion/Bt848.inf`:

```
%MsftMfg%=Microsoft
%BT848.VideoDeviceDesc%=BT848.Main , PCI\VEN_109E&DEV_0350

[VideoCapture.AddReg]
HKR,,CLSID,,{17CCA71B-ECD7-11D0-B908-00A0C9223196}
HKR,,FriendlyName,,%BT848.Capture%

[Strings]
BT848.Capture = "Conexant's BtPCI Capture"
```

`{17CCA71B-ECD7-11D0-B908-00A0C9223196}` is the **standard DirectShow Video
Capture filter CLSID**. The driver is a normal KS (`bt848.sys`) driver for
the Conexant/Brooktree BT848.

`Linksys32/lnkFusion.dll` confirms the consumer side:

```
?MakeFilterGraph@@YAJPAUIGraphBuilder@@@Z
?FindMyCaptureDevice@@YAJPAPAUIBaseFilter@@@Z
?CreateSampleGrabber@@YAJPAUIFilterGraph@@@Z
?ShowPropPage@@YAJPAUIBaseFilter@@@Z
strings: "SampleGrabber"  "Conexant's BtPCI Capture"  "NullRenderer"
```

So the original code does the standard thing: `ICaptureGraphBuilder2`,
locate the device by `FriendlyName`, insert a `SampleGrabber`, render to
`NullRenderer`.

**Therefore: no reverse engineering is needed for the video path.** On
Windows, `cv2.VideoCapture(0, cv2.CAP_DSHOW)` (or PyAV) opens the card
directly. The `Linksys32` software adds nothing to the capture path except
its own ROI/format/record bookkeeping.

### 3.2 Camera back-ends

| DLL | Exports | Camera |
|---|---|---|
| `lnkQCam.dll` | `InitQCamHandle`, `GetQCamExposureNow`, `GetQCamExposureValues`, `SetQCamExposureValues`, `SetQCamRGBMultipliers`, `SetQCamQueue`, `QCamDestroy`, `QCamSendSettings` | QImaging |
| `lnkPIX6XX.dll` | `InitPixHandle`, `SetPixFrameRate`, `StartPixFrameThread` | PixeLINK PL-A6xx |
| `lnkFusion.dll` | `MakeFilterGraph`, `RunGraph`, `DestroyGraph`, `SetAnalogInput`, `SetColorStandard`, `GetColorStandard`, `ShowVideoStandard`, `SetFusionFrameRate` | BT848 / VideoFusion |
| `lnkJVC1030.dll` | `InitKYF1030`, `StartJVCFrameThread` | JVC KY-F1030 |
| `lnkFrame.dll` | `InitLeadObject`, `GetDeviceInfo`, `GetFrameRate`, `GetMostRecentFrame`, `UnlockFrame`, `SetFramePointers`, `GetLeadHandle` | generic frame provider |

`Linksys32.exe` additionally declares `SetJVCExposureValues` and
`SetJVCWBValues`, which are absent from `lnkJVC1030.dll`'s export table —
they are probably resolved at runtime from the camera's own driver.

### 3.3 Timing

From *Video Setup.htm*:

> On older slower computers the Lower Frame Rate option can be used to halve
> the live video frame rate.

and

> If the AE auto exposure option is not recommended while mapping or
> measuring intensity.

> Please note that changing the exposure time will have an impact on the
> video frame rate.

So the video path is a plain continuous DirectShow capture at a chosen
frame rate. Nothing exotic.

---

## 4. Licensing

The vendor software validates its licence against the **installation media's
volume serial number** (`GetVolumeInformationA` from `kernel32`). The
specific registration constants and key-file layout are deliberately **not
reproduced here**: this project does not implement, bypass, or interact with
the licence check, so documenting those internals would be out of scope and
would add nothing to driving the instrument.

What matters operationally is that **licence state is reported over the
wire** by the programmer itself, as one of:

```
Not licensed            Demonstration expired
OEM mode                Comms locked off
```

So the **hardware may refuse to ramp when unlicensed**, independently of the
host software. Worth checking on the actual instrument before assuming the
Python driver can drive it — watch for these strings in the telemetry/status
channel.

---

## 5. The T95: a USB HID link, not a serial port

The Linkam **T95** is a USB HID device, so none of the RS-232 framing
above applies to its physical layer.  It enumerates as:

| Property | Value |
|---|---|
| USB VID:PID | `16da:0002` |
| Product string | `Linkam T95` |
| HID unique ID | `1234` |
| Class | USB HID |
| Endpoints | 2 interrupt |
| Maximum IN report | 64 bytes |
| `bInterval` | 1 ms |
| Linux node | `/dev/hidraw1` (stable alias `/dev/input/by-id/usb-Linkam_T95_1234-hidraw`) |
| Driver | `hid-generic` |

Because it is a HID device it needs **no** USB-to-RS232 adapter — the
earlier RS-232-centric analysis in this document still applies to the
*application layer* (the command keywords, the telemetry format and the
semantics of Rate/Limit/Hold), but the *framing* is different.

### 5.1 The HID report descriptor (RETRIEVED)

The report descriptor was read successfully from the device with
`HIDIOCGRDESC`.  It is 29 bytes:

```
06 a0 ff   Usage Page (0xFFA0)          -- Linkam vendor page
09 01      Usage (0x01)
a1 01      Collection (Application)
09 01      Usage (0x01)
15 00      Logical Minimum (0)
26 ff 00   Logical Maximum (255)
75 08      Report Size (8)
95 3f      Report Count (63)              -- 63 bytes
81 82      Input  (Data, Var, Abs)
09 02      Usage (0x02)
75 08      Report Size (8)
95 3f      Report Count (63)              -- 63 bytes
91 82      Output (Data, Var, Abs)
c0         End Collection
```

Decoded, that means:

| Property | Value |
|---|---|
| Usage page | `0xFFA0` (Linkam vendor-defined) |
| Collections | 1 (Application) |
| INPUT report | usage `0x01`, 63 bytes, **no report ID** |
| OUTPUT report | usage `0x02`, 63 bytes, **no report ID** |

So the T95 uses **no report IDs** and a **63-byte payload** in each
direction.  The framing is therefore "ASCII at byte 0, zero-padded to
63" — the `text_padded` framing in `linkam/hid.py`.

Other USB details from `lsusb -v`:

* One interface, HID 1.10, `bNumDescriptors` 1.
* EP 1 IN: interrupt, 64-byte max packet, `bInterval` 1.
* EP 1 OUT: interrupt, 64-byte max packet, `bInterval` 1.
* `bmAttributes` 0xc0 — **self powered**, `MaxPower` 100 mA.
* `bcdUSB` 2.00.

### 5.2 Getting the descriptor out: an ioctl bug worth recording

`HIDIOCGRDESC` initially failed with `EINVAL` on every attempt.  The
cause was **not** a driver conflict (as first assumed) but the ioctl
request number.

The kernel's `hidraw_report_descriptor` handler is reached through
`_IOC(dir, type, nr, size)`, and the layout from
`<asm-generic/ioctl.h>` is:

```
_IOC(dir, type, nr, size) = (dir << 30) | (size << 16) | (type << 8) | nr
```

The first implementation used the wrong shift for the size field
(`size << 14` instead of `size << 16`), so the encoded size was 1
instead of 4100 and the kernel rejected every call.  With the shifts
correct and the size set to `sizeof(struct hidraw_report_descriptor)`
(4 + 4096 = 4100), the descriptor comes back immediately.

This is worth recording because the symptom — `EINVAL` from
`HIDIOCGRDESC` — looks exactly like "another driver owns the
interface", which sent the investigation down the wrong path for a
while.

### 5.3 What was observed directly

* The device is **silent until prompted**.  A 20 s passive listen on the
  hidraw node produced **zero** unsolicited input frames.
* hidraw output reports are accepted at lengths 2, 4, 8, 16, 32 and 64.
  A **1-byte** output report is rejected with `EINVAL`.
* `HIDIOCSINPUT` and `HIDIOCSFEATURE` both fail with `EPIPE` — the device
  implements neither.
* `HIDIOCGRAWNAME` -> `Linkam T95`; `HIDIOCGRAWUNIQ` -> `1234`;
  `HIDIOCGRAWINFO` -> vendor `0x16da`, product `0x0002`.

### 5.4 The OUT endpoint stops responding

After the first few writes, **every** write to the OUT endpoint began
failing with `ETIMEDOUT` — including plain zero-filled reports of every
size, and including via the `HIDIOCSOUTPUT` ioctl as well as `write()`.
Reads still returned no data, and the device's descriptors still read
correctly, so it was still enumerated and healthy.

This is **not** a framing problem: a wrong payload would be *accepted*
and ignored, not time out.  It means the firmware had stopped servicing
the endpoint.  Recovering requires either a power cycle of the stage
(off, wait, on) plus a USB replug, or a `USBDEVFS_RESET` on the USB
device (which needs root — see `tools/reset_usb_device.sh`).

### 5.5 The application layer is BINARY, not ASCII

The hypothesis in the previous revision of this document — that the T95
carries the *same ASCII strings* as the RS-232 link inside its HID
reports — is **refuted**.  The T95 speaks a binary block protocol.

The evidence:

* The device's answer is not text.  It is three 63-byte blocks whose
  first byte is a type (1, 2, 3) and which carry **little-endian
  float32** values, not ASCII.
* Not one ASCII command has any effect.  `Prog rate 5`, `Prog Limit 30`,
  `Prog stop`, `prog ramp 1 rate 5 limit 30 time 60`, `MA6`, `MC`,
  `MF1`, `MSX`, `MVX` and `prog` were each sent, in a dozen different
  encodings (bare ASCII, ASCII behind an equal-byte gate, length-prefixed,
  length duplicated, ASCII at four different offsets).  None produced an
  acknowledgement, and none changed a single field of the status.

The consequence is that `linkam/protocol.py:HidProgrammer` does **not**
inherit the ASCII application layer from `LinkamProgrammer`.  It
overrides the read loop with a status poll (`linkam/t95.py`).  The
inherited ASCII command methods are still present and still mean what
they mean, but **they do nothing on this link** and must not be used to
drive a ramp until the command frame format is found.

### 5.6 The frame acceptance rule

The device answers an output report **if and only if its first two bytes
are equal**.  This was established by mutating one byte of a 63-byte
report at a time:

| Request | Answered? |
|---|---|
| byte 0 = byte 1 (any value: 00, 01, 02, 03, 10, 41, 7f, ff, and the ASCII pairs `??`, `MM`) | yes |
| byte 0 != byte 1 | no — the device says nothing at all |
| bytes 2..62 set to anything, with byte 0 == byte 1 | yes, and the answer is **identical** |

That last row is what shows this is a **status poll** and not a
request/response exchange: the whole of the request past the first two
bytes is ignored.

The answer is always **three 63-byte blocks**, delivered together:

```
block 1 (type 1)   01 03 00 03 88 00 00 00 00 00 | f32@10 | f32@14 | f32@18 | f32@22 | 00...
block 2 (type 2)   02 03 00 ... 01 @34 ... 48 @51 ... 00...
block 3 (type 3)   03 03 00 ... 00...
```

Block 1's four floats are:

| Offset | Observed | Reading |
|---|---|---|
| 10 | 23.44 → 23.64 °C, drifting smoothly by ~0.01 °C/min | **the current temperature** — confirmed by its magnitude, its smoothness and its slow drift |
| 14 | ~-45, jumping randomly by ±1.5 between polls | a **second sensor with nothing connected** to it: noise, not a setpoint |
| 18 | 0.7000, constant | unknown; provisionally "rate" |
| 22 | 0.3500, constant | unknown; provisionally "limit" |

The last two are named by analogy with the Linksys32 vocabulary
(`Temp`, `Temp2`, `Rate`, `Limit`), which is a **guess** — they do not
move, so nothing has confirmed what they are.

The poll frame is implemented in `linkam/t95.py:build_status_poll`.

### 5.7 Still UNVERIFIED for the T95

**Resolved:**

2. ~~Whether the T95 uses report IDs at all.~~  **No** — the descriptor
   defines one input and one output report, neither with a report ID.
3. ~~The output report length it expects.~~  **63 bytes**, from the
   descriptor's Report Count.
4. ~~Whether the telemetry is pushed periodically or only in response to
   a query.~~  **Only in response to a query.**  A 12 s passive listen
   produces nothing; a poll produces three blocks immediately.
5. ~~The application-level encoding inside the 63-byte report.~~  It is
   **binary** (§5.5), not ASCII.

**Still open:**

1. **The command frames.**  What has to be written to change the
   setpoint, start a ramp or stop one.  This is the blocker for
   "set temperature"; see §6.

   Ruled out so far, by experiment:

   * every ASCII command from the Linksys32 vocabulary, in a dozen
     encodings (§5.5);
   * the request being **symmetric** with the reply — i.e. a command
     being `[gate][gate][…][float32 argument at offset 10, 14, 18 or
     22]`, with the command code in the gate bytes.  64 combinations
     (command codes 0x00–0x0f × four argument offsets) were sent and
     the status was re-polled after each; **nothing moved**.  Note
     that these frames all satisfy the acceptance gate, so they are
     safe to send — the instrument did not wedge on any of them.

6. What the two constant floats at offsets 18 and 22 are.
7. What block 2's flag byte (offset 34) and constant byte (offset 51)
   mean.

### 5.8 Two behaviours that will bite you

**The device needs a warm-up.**  After a power cycle it enumerates
immediately but answers **nothing** for roughly a minute.  Frames sent
during that window are silently dropped.  This is easy to mistake for
a broken link.

**The OUT endpoint wedges.**  A frame the firmware does not like can
leave the device refusing further writes with `ETIMEDOUT` — including
frames as innocuous as a plain 2-byte write, and including via the
`HIDIOCSOUTPUT` ioctl as well as `write()`.  Reads still return nothing
and the descriptors still read correctly, so the device is still
enumerated and healthy; it has simply stopped servicing the endpoint.

Recovery is a **power cycle of the stage** (off, wait 10 s, on) plus a
USB replug.  A `USBDEVFS_RESET` on the USB device also works but needs
root (`tools/reset_usb_device.sh`) and did not always re-enumerate.

Do not confuse this with a framing problem: a wrong payload would be
*accepted and ignored*, not time out.

### 5.9 The T95 has a bootloader mode

On power-up the controller enumerates **twice**:

```
16da:0001  Linkam Bootloader     (~5 s)
16da:0002  Linkam T95             (normal mode)
```

`linkam/hid.py` looks for `16da:0002`, so it correctly ignores the
bootloader.  The udev rule matches `16da:0002` only, so the bootloader
gets no extra permissions — which is fine, since it is never used.

This also explains a confusing observation: `lsusb` can show the device
as `16da:0001` in the middle of a session if it is power-cycled.


### 5.10 Note on the telemetry encoding

On the RS-232 link the telemetry contains `°C` — a **degree sign**,
which is UTF-8 (`0xC2 0xB0`), not ASCII.  Decoding payloads as ASCII
with `errors="replace"` silently corrupts the unit on every frame.  The
HID transport decodes UTF-8.

The T95's own telemetry is binary and carries no text at all (§5.5).

---

## 6. Summary of open questions

### The RS-232 link (legacy programmers)

| # | Question | How to resolve |
|---|---|---|
| 1 | Command terminator (CR / LF / CRLF) | `probe()` against hardware |
| 2 | Request checksum present? | `probe()`; compare with `ReadCrc32` usage in `CommX.ocx` |
| 3 | Response framing / ack format | capture a raw session with a serial sniffer |
| 4 | Baud rate for a given programmer | `CHIPDEF.INI` `Baud=` rows; try 9600/19200/38400 |
| 5 | Does the hardware require a licence to ramp? | observe `Not licensed` in telemetry |
| 6 | Does `Prog start` need a preceding `Prog Hold`/`Prog rate`? | try the documented order |

A **serial sniffer** (e.g. `socat -v -x /dev/ttyUSB0,raw,echo=0 -` or a
two-port loopback with pyserial) running alongside the real Linksys32
software is the fastest way to close questions 1–3 in one pass: it shows
both directions of the exact traffic the shipped software generates.

### The T95 (USB HID)

| # | Question | Status |
|---|---|---|
| 7 | Report framing | **Solved** — ASCII-in-HID is refuted; the link is binary (§5.5) |
| 8 | Report IDs and output size | **Solved** — no report IDs, 63-byte reports (§5.1) |
| 9 | Telemetry cadence | **Solved** — the device is silent until polled (§5.7) |
| 10 | **The command frames** | **OPEN** — this is what blocks setting a temperature |

Question 10 is the one that matters now.  Everything needed to *read* a
temperature is in place (`linkam/t95.py`, `linkam.cli read --hid`);
nothing is known about how to *write* one.

The most reliable way to close it is to capture what Linkam's own
software sends.  That needs a Windows host with the T95 control
software, running alongside a USB capture (or a USB sniffer between
the host and the controller).  A cheaper option is to brute-force the
command space from the host side, but note the hazards in §5.8: frames
the firmware dislikes can wedge the OUT endpoint, and each recovery
costs a power cycle.

