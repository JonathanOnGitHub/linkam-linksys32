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

### 5.5 The application layer is BINARY, not ASCII — CONFIRMED

The hypothesis in an earlier revision of this document — that the T95
carries the *same ASCII strings* as the RS-232 link inside its HID
reports — is **refuted**.  The T95 speaks a binary block protocol.

The evidence:

* The device's answer is not text.  It is three 63-byte blocks whose
  first byte is the block index (1, 2, 3) and which carry
  **little-endian float32** values, not ASCII.
* Not one ASCII command has any effect.  `Prog rate 5`, `Prog Limit 30`,
  `Prog stop`, `prog ramp 1 rate 5 limit 30 time 60`, `MA6`, `MC`,
  `MF1`, `MSX`, `MVX` and `prog` were each sent, in a dozen different
  encodings (bare ASCII, ASCII behind an equal-byte gate, length-prefixed,
  length duplicated, ASCII at four different offsets).  None produced an
  acknowledgement, and none changed a single field of the status.

### 5.6 THE PROTOCOL — recovered from Linkam's own SDK

**This is the important section.  The whole command protocol is now
known**, and it was not guessed: it was read out of Linkam's commercial
SDK, which was published in the public repository
`DiamondLightSource/linkam3` as `linkamT96App/src/libLinkamSDK.so`
(18.7 MB, ELF x86-64, **unstripped, with full DWARF debug
information**, copyright 2018-2023 Linkam Scientific Instruments).

That library is the same device driver that Linkam's own NEXUS/LINK
software uses, and it drives the T95 and T96 over exactly this USB HID
link.  Its Linux build embeds HIDAPI, so it talks to the device with
`hid_open` / `hid_write` / `hid_read_timeout` — the same three calls a
Linux driver makes.

Reproducing the extraction:

```bash
curl -sSL -O https://raw.githubusercontent.com/DiamondLightSource/linkam3/HEAD/linkamT96App/src/libLinkamSDK.so
file libLinkamSDK.so            # ELF 64-bit, not stripped, with debug_info
nm -C libLinkamSDK.so | grep -i 'USBTxThread\|USBRxThread'
readelf --debug-dump=info libLinkamSDK.so > dbg.txt   # the command enumerations
```

The SDK headers (`USB.h`, `CommsAPI.h`, `LinkamSDK.h`, `Message.h`) are
in the same directory of the same repository.

#### 5.6.1 Where the protocol lives in the SDK

| SDK function | address | what it does |
|---|---|---|
| `USBTxThread::sendPacket` | `0x185680` | writes 64-byte blocks with `hid_write` |
| `USBTxThread::writePacket` | `0x18543c` | writes one block header + payload |
| `USBTxThread::makeCommand` | `0x1854b6` | splits a command into 64-byte blocks |
| `USBTxThread::sendCommand` | `0x18474a` | queues a command |
| `USBRxThread::readPacket` | `0x1841ba` | reads 63-byte reply blocks |
| `USBRxThread::readReply` | `0x1839b6` | reassembles the reply |
| `Stage::setValue` | `0x1c0b44` | sets the rate / the setpoint |
| `Stage::startHeating` | `0x1c1082` | starts the ramp |
| `Stage::stopHeating` | `0x1c1186` | stops the ramp |
| `DataPollThread::run` | `0x17c74c` | polls the controller |
| `StandardStage::parseReply` | `0x1c27da` | unpacks the status payload |
| `TemperatureControlledStage::getValue` | `0x1c2f76` | maps status fields to values |

#### 5.6.2 The wire format

**Host to device.**  One *command* is split into one or more 64-byte
blocks; each block is one `hid_write`:

```
offset  size  meaning
------  ----  ----------------------------------------------------
0       1     reserved, always 0x00
1       1     block index, 1-based
2       1     number of blocks in this command
3       2     command id, uint16 little endian
5       2     payload length, uint16 little endian
7       2     parameter 3, uint16 little endian
9       2     parameter 4, uint16 little endian
11      52    payload bytes 1..52
63      1     padding
```

Blocks 2..N carry 60 payload bytes each, at offset 3 — so every block
holds exactly 60 bytes of the *command stream* (the 8-byte header above
followed by the payload).  The block count is
`ceil((payload length + 8) / 60)`, never less than one.

**Device to host.**  The device answers with 63-byte blocks (the size
its report descriptor declares):

```
offset  size  meaning
------  ----  ----------------------------------------------------
0       1     block index, 1-based
1       1     number of blocks
2       60    command stream: header as above, then payload
62      1     padding
```

The only difference between the two directions is that the host's block
carries a leading `0x00` that the device's does not.

#### 5.6.3 The command vocabulary

Recovered from the SDK's DWARF enumerations (2 094 enumerators in all;
the T95 family is `eCMDT95*`):

| id | name | meaning |
|---|---|---|
| `0x0300` | `eCMDT95_GETLINK` / `eCMDT95_START` | the status poll |
| `0x0301` | `eCMDT95_SETMULT` | carry a `MultiCommand` |
| `0x0302` | `eCMDT95_SETRESET` | |
| `0x0303` | `eCMDT95_SETSTOP` | stop the ramp |
| `0x0304` | `eCMDT95_SETHEAT` | force heating |
| `0x0305` | `eCMDT95_SETCOOL` | force cooling |
| `0x0306` | `eCMDT95_SETHOLD` | hold at the current temperature |
| `0x0307` | `eCMDT95_SETRESN` | set the data resolution |
| `0x0308` | `eCMDT95_SETMAINSFREQ` | |
| `0x0380` | `eCMDT95MULTI_START` | sub-command: start the ramp |
| `0x0382` | `eCMDT95MULTI_SETRATE` | sub-command: ramp rate, °C/min |
| `0x0383` | `eCMDT95MULTI_SETLIMIT` | sub-command: setpoint, °C |
| `0x0384` | `eCMDT95MULTI_SETTIME` | sub-command: hold time, s |

#### 5.6.4 Setting the temperature

Setting a setpoint is **one** command: `eCMDT95_SETMULT` (`0x0301`)
whose payload is a 96-byte `MultiCommand`:

```c
struct MultiCommand {          // 96 bytes
    uint16_t cmd[16];          // offset 0
    float    value[16];        // offset 32
};
```

`cmd` is a list of sub-commands and `value` their arguments; a zero
`cmd` entry terminates the list.  This is exactly what the SDK's
`Stage::startHeating` sends — rate, limit and start in **one atomic
command**, so the instrument can never act on a half-applied programme:

```
cmd[0] = 0x0382  value[0] = rate    (°C/min)
cmd[1] = 0x0383  value[1] = setpoint (°C)
cmd[2] = 0x0380  value[2] = 0
cmd[3] = 0       (terminator)
```

Because the payload is 96 bytes it needs **two** 64-byte blocks.

#### 5.6.5 Reading the temperature

The SDK polls with `eCMDT95_GETLINK` (`0x0300`) and no payload.  The
answer's payload is 136 bytes:

| payload offset | type | meaning |
|---|---|---|
| 0 | float32 | `HEATER1_TEMP` — the stage temperature, °C |
| 4 | float32 | the second thermocouple channel |
| 8 | float32 | `HEATER_RATE` — the programmed rate, °C/min |
| 12 | float32 | `HEATER_SETPOINT` — the programmed setpoint, °C |

Those offsets are the SDK's own: `StandardStage::parseReply` copies the
payload to `stage + 0x80`, and `TemperatureControlledStage::getValue`
reads `stage + 0x80` for `HEATER1_TEMP`, `stage + 0x88` for
`HEATER_RATE` and `stage + 0x8c` for `HEATER_SETPOINT`.

**This is exactly what the empirical notes in §5.6 of the previous
revision observed**: the temperature at block offset 10, the rate at 18
and the limit at 22 — the payload offsets plus the 10 bytes of reply
header.  And the captured answer decodes byte for byte:

```
01 03 | 00 03 | 88 00 | 00 00 | 00 00 | 3c 80 bb 41 | 36 2e 36 c2 | 33 33 33 3f | 33 33 3b 33
^  ^     ^  ^     ^  ^           ^  ^     ^^^^^^^^^^^  ^^^^^^^^^^^  ^^^^^^^^^^^  ^^^^^^^^^^^
|  |     |  |     |  |           |  +----- parameter 4
|  |     |  |     |  |           +-------- parameter 3
|  |     |  |     |  +-------------------- payload length = 136
|  |     |  |     +-------------------------- command id = 0x0300 (GETLINK)
|  |     |  +-------------------------------- block count
|  |     +----------------------------------- block index
|  +----------------------------------------- (block index)
+------------------------------------------- (block index)
```

### 5.7 The "first two bytes must be equal" rule was wrong

An earlier revision of this document concluded that "an output report is
accepted if and only if its first two bytes are equal".  That rule does
not hold for the protocol Linkam's own software uses, and it is not
even self-consistent: the same byte pair is reported as accepted in one
experiment and rejected in another (for example `(0x00, 0x00)` was
accepted with a zero payload and rejected with a dense one).

The real rule is the SDK's: byte 0 is a reserved `0x00`, byte 1 is the
1-based block index and byte 2 is the block count.

The most likely explanation for the earlier observation is that the
controller was running **older firmware**.  Linkam's own support notes
say exactly this: *"T9X controller will not connect to LINK ... it may
be that the firmware version of your T9X controller is too old for the
installed version of LINK"*, and Linkam publishes a bootloader
programme specifically for updating it (§5.9).

### 5.8 Still UNVERIFIED for the T95

**Resolved since the previous revision:**

1. ~~Whether the T95 uses report IDs at all.~~  **No** — the descriptor
   defines one input and one output report, neither with a report ID.
2. ~~The output report length it expects.~~  **63 bytes** from the
   descriptor; but the SDK writes **64-byte blocks**, and on real
   hardware a 63-byte write times out while a 64-byte write is
   accepted.  Use 64.
3. ~~Whether the telemetry is pushed periodically or only in response to
   a query.~~  **Only in response to a query.**  A 12 s passive listen
   produces nothing; a poll produces three blocks immediately.
4. ~~The application-level encoding inside the 63-byte report.~~  It is
   **binary** (§5.5).
5. ~~**The command frames.**~~  **SOLVED** — §5.6.

**Still open:**

1. Whether the particular controller in hand runs firmware new enough
   for this framing.  If the status poll does not answer, update the
   firmware with Linkam's bootloader programme (§5.9) and try again.
2. What the second thermocouple channel (payload offset 4) is wired to.
3. The exact meaning of the `eCMDT9X_*` command family (`0x04c0`
   and up), which the SDK uses for controller and slot information.

### 5.9 The T95 has a bootloader mode, and Linkam publishes the updater

On power-up the controller enumerates **twice**:

```
16da:0001  Linkam Bootloader     (~5 s)
16da:0002  Linkam T95             (normal mode)
```

`linkam/hid.py` looks for `16da:0002`, so it correctly ignores the
bootloader.  The udev rule matches `16da:0002` only, so the bootloader
gets no extra permissions — which is fine, because it is never used.

This also explains a confusing observation: `lsusb` can show the device
as `16da:0001` in the middle of a session if it is power-cycled.

**Linkam publishes the bootloader programme publicly**, at

> https://drive.google.com/file/d/1_cqF_bQFy4igriZV_730pCMdf0tLE1Wh/view
> (`Linkam Bootloader Install.zip`)

and the procedure is documented at
`https://www.linkam.co.uk/support/controllers/general#firmware`:
install the programme, make sure only one Linkam device is connected,
power the controller **off**, click OK, then power it on **within 20
seconds**; the bootloader detects it and asks for a firmware file.  The
firmware image itself has to be requested from Linkam Technical
Support.

So there is a supported route to update the controller's firmware, if
the protocol in §5.6 turns out not to match the firmware in hand.

### 5.10 Two behaviours that will bite you

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

### 5.11 Note on the telemetry encoding

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

### The T95 (USB HID)

| # | Question | Status |
|---|---|---|
| 7 | Report framing | **Solved** — binary, §5.6.2 |
| 8 | Report IDs and output size | **Solved** — no report IDs; 63-byte input reports, **64-byte output blocks** |
| 9 | Telemetry cadence | **Solved** — the device is silent until polled |
| 10 | **The command frames** | **SOLVED** — §5.6.  Setting a setpoint is `eCMDT95_SETMULT` with a 96-byte `MultiCommand` |
| 11 | Does the controller's firmware match this framing? | try the status poll; if it does not answer, update the firmware (§5.9) |

**Question 11 is the only thing left.**  Everything needed to *read* and
to *write* a temperature is now known and implemented in
`linkam/t95_protocol.py`; it just has to be tried against the
controller in hand.

### The SDK, as an alternative to all of this

Linkam sells a **Linkam SDK** that drives the same controllers, over
both USB and serial, on Windows and Linux, with C++, C# and Python
interfaces.  It is the device driver their own software is built on.
Two open-source artefacts are already public:

* `pylinkam` on PyPI — Python bindings for the SDK
  (`pip install pylinkam`), which expose
  `connection.set_value(interface.StageValueType.HEATER_SETPOINT, 30)`
  and `connection.get_value(interface.StageValueType.HEATER1_TEMP)`.
* `DiamondLightSource/linkam3` — contains the SDK's Linux shared
  library, its public headers (`USB.h`, `CommsAPI.h`, `LinkamSDK.h`,
  `Message.h`, …) and a full EPICS driver.

The SDK binary and a per-machine licence file (`Linkam.lsk`) are not
redistributed with `pylinkam`; they have to be obtained from Linkam.
If the licence is available, `pylinkam` is the supported way to drive
the stage and this driver is unnecessary.  If it is not, the protocol
recovered in §5.6 is the fallback.
