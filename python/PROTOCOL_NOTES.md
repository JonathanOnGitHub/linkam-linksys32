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

### 5.1 What was observed directly

* The device is **silent until prompted**.  A 30 s passive listen on the
  hidraw node produced **zero** unsolicited input frames.
* hidraw output reports are accepted at lengths 2, 4, 8, 16, 32 and 64.
  A **1-byte** output report is rejected with `EINVAL`.
* `HIDIOCSINPUT` and `HIDIOCSFEATURE` both fail with `EPIPE` — the device
  implements neither.
* `HIDIOCGRDESC` fails with `EINVAL` while `usbhid` holds interface 0.
  `lsusb -v` correspondingly reports `Report Descriptors: ** UNAVAILABLE **`.
  This is *not* yet proof that the device has no report descriptor; the
  ioctl may simply be refused because another driver owns the interface.

### 5.2 The architectural consequence

The application layer recovered in §1 is **line-oriented ASCII text**:
`LinksysActXLib.ocx` reads replies with VB's `__vbaLineInputStr`, and
the telemetry frame is a plain string.  It is therefore plausible — and
by far the cheapest hypothesis — that the T95 carries *the same ASCII
strings* inside HID report envelopes.

If that holds, then the entire protocol implementation above applies
unchanged, and only the transport needs replacing.  That is how the
package is built: `linkam/transport.py` defines the seam
(`write_line` / `read_line`), `linkam/transport.py:SerialTransport`
implements the RS-232 side, and `linkam/hid.py:HidTransport` implements
the HID side.  `LinkamProgrammer` and `HidProgrammer` share every line
of ramp logic, stall detection and telemetry parsing.

### 5.3 Candidate report framings

Until the real framing is observed, four candidates are supported:

| Framing | Layout of one report |
|---|---|
| `raw` | ASCII payload at byte 0 (report ID first, if the device uses IDs) |
| `id_text` | byte 0 is the report ID, ASCII from byte 1 |
| `text_padded` | ASCII at byte 0 of a report with no report ID, zero-padded |
| `length_prefixed` | a length byte (or 16-bit length) then that many ASCII bytes |

`tools/t95_hid_probe.py` tries each and reports which one answers.  It
sends only `MA6`, which reads a stage register and changes nothing.

### 5.4 Still UNVERIFIED for the T95

1. The actual report framing (see §5.3).
2. Whether the T95 uses report IDs at all.
3. The output report length it expects.
4. Whether the telemetry is pushed periodically (as on the serial link)
   or only in response to a query.
5. Whether the command terminator (CR / LF / CRLF) still applies inside
   the HID payload.
6. Whether the T95 supports the same command vocabulary as the older
   RS-232 programmers, or a subset.

### 5.5 Note on the telemetry encoding

The telemetry contains `°C` — a **degree sign**, which is UTF-8
(`0xC2 0xB0`), not ASCII.  Decoding HID payloads as ASCII with
`errors="replace"` silently corrupts the unit on every frame.  The HID
transport decodes UTF-8.

---

## 6. Summary of open questions

| # | Question | How to resolve |
|---|---|---|
| 1 | Command terminator (CR / LF / CRLF) | `probe()` against hardware |
| 2 | Request checksum present? | `probe()`; compare with `ReadCrc32` usage in `CommX.ocx` |
| 3 | Response framing / ack format | capture a raw session with a serial sniffer |
| 4 | Baud rate for a given programmer | `CHIPDEF.INI` `Baud=` rows; try 9600/19200/38400 |
| 5 | Does the hardware require a licence to ramp? | observe `Not licensed` in telemetry |
| 6 | Does `Prog start` need a preceding `Prog Hold`/`Prog rate`? | try the documented order |
| 7 | T95 report framing | `python tools/t95_hid_probe.py 30` |
| 8 | T95 report IDs and output size | the same probe, from the report descriptor |
| 9 | T95 telemetry cadence (push or poll?) | listen on the hidraw node after a query |

A **serial sniffer** (e.g. `socat -v -x /dev/ttyUSB0,raw,echo=0 -` or a
two-port loopback with pyserial) running alongside the real Linksys32
software is the fastest way to close questions 1–3 in one pass: it shows
both directions of the exact traffic the shipped software generates.

For the T95, questions 7–9 are answered by
`python tools/t95_hid_probe.py 30`, which is entirely read-only.
