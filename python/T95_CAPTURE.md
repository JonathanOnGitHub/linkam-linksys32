# Capturing the T95's USB traffic

## Why this is needed

Everything about the T95's USB link is now known **except one thing**:
the frame that changes the setpoint.  See `PROTOCOL_NOTES.md` §6,
question 10.

What we have established by experiment:

* The device is silent until written to.
* It answers an output report if and only if its **first two bytes are
  equal** — with one exception found so far (`0x52`, which answers
  with a single block instead of three), and one subtlety: the rule
  also depends on the rest of the payload, so it is not simply
  `byte[0] == byte[1]`.
* The answer is three 63-byte blocks; block 1 carries the current
  temperature as a little-endian float32 at offset 10.
* No ASCII command from the Linksys32 vocabulary has any effect.

The command frames are therefore a format we cannot guess by
reasoning — but we do not have to guess.  Linkam's own software speaks
them, and we can read them off the wire.

## What you need

1. **The T95's own control software** (the CD or installer that came
   with the controller).  *Not* Linksys32 — that is the 2003 package
   for the older RS-232 programmers and predates the T95 entirely.

2. **A USB capture.**  On Windows, USBPcap (part of Wireshark) is the
   easy answer.

## The capture

### On Windows (USBPcap + Wireshark)

1. Install Wireshark (it bundles USBPcap).
2. Plug the T95 into the **same machine** that will run the capture
   and the software.
3. Start a capture on the USB interface the T95 is on:
   * Wireshark → *Capture → Options* → pick the USBPcap interface, or
   * from a command prompt: `USBPcapCMD.exe -c -i \\.\USBPcap1 -o capture.pcapng`
4. Run Linkam's software and drive the T95.
5. Stop the capture.

### What to do in the software

The point is to make the command **recognisable**, so capture a short
sequence of deliberate actions:

1. Read the temperature (if the software polls, that is already the
   poll frame).
2. **Set the setpoint to a distinctive value** — e.g. 37 °C.  A frame
   containing a float32 of `37.0` is easy to spot.
3. Set it again to a different distinctive value — e.g. 41 °C.  Two
   frames that differ only in one float are the setpoint command.
4. Optionally: start and stop a ramp, and change the rate.

Keep the target low (≤ 60 °C) and let the stage cool afterwards — the
software will really move the stage, unlike our driver.

> Safety: this capture *will* move the stage. Use a modest setpoint and
> make sure the sample on the stage cannot be damaged.

### On Linux (usbmon)

If you can run the software under Wine on the Linux machine, the
capture can be taken here instead:

```bash
sudo mount -t debugfs none /sys/kernel/debug 2>/dev/null || true
sudo cat /sys/kernel/debug/usb/usbmon/5u > usbmon.txt   # 5 = bus number
```

The `usbmon` text format is harder to parse than a pcapng, so prefer
USBPcap if you have the choice.

## Turning the capture into command frames

With USBPcap, `tshark` reads the capture directly:

```bash
tshark -r capture.pcapng \
  -Y "usb.idVendor == 0x16da && usb.idProduct == 0x0002" \
  -T fields -e usb.endpoint_address -e usb.capdata > frames.txt
```

Then:

```bash
cd ~/Documents/Nottingham/Research/Linkam/linkam-linksys32/python
.venv/bin/python tools/parse_capture.py frames.txt
```

which prints the **host → device** reports — those are the commands —
and the device → host answers.

If `tshark` is not installed, `tools/parse_capture.py` also accepts a
plain text file with one report per line as `<endpoint hex> <payload
hex>`.

## Once we have the frames

The next step is to replay them: send the captured command frames to
the instrument and confirm which one changes the setpoint.  The
acceptance rule we have already mapped lets us predict which frames
the device will even answer, so we can build the replay carefully and
avoid the frames that wedge its OUT endpoint.

## If you cannot get the software

Then the remaining options are:

* **Ask Linkam.**  They have the firmware-side specification; a request
  for the T95's USB protocol (or a protocol description for the
  `16da:0002` HID device) is entirely reasonable, and they publish
  such things for other products.
* **The liquid nitrogen pump controller.**  It is also a Linkam USB
  device, so it very likely shares the same firmware conventions.  If
  it has its own software, capturing *its* traffic may reveal the
  command framing of the same protocol family.  Worth doing while the
  T95 capture is being set up.
* **Keep brute-forcing.**  It works — the acceptance-rule map is
  filling in nicely — but each frame the firmware dislikes can wedge
  the instrument's OUT endpoint, and each recovery costs a power cycle.
  It is slower than reading the answer off the wire.
