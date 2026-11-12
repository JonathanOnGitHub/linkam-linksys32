"""Tests for the web GUI.

No hardware is needed: a fake programmer stands in for the instrument,
and the HTTP server is started on an ephemeral port and driven with
``urllib``.
"""

from __future__ import annotations

import csv
import json
import shutil
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from linkam.protocol import ProgrammerState, Telemetry
from linkam.video import Frame
from linkam.webapp import StageController, make_server, parse_steps

#: The page, and the tools that check it.
PAGE = Path(__file__).parent.parent / "linkam" / "static" / "index.html"
PLOT_CHECK = Path(__file__).parent.parent / "tools" / "check_plot.js"
CONTRAST_CHECK = Path(__file__).parent.parent / "tools" / "check_contrast.py"
PAGE_CHECK = Path(__file__).parent.parent / "tools" / "check_page.py"


# --------------------------------------------------------------------------- #
# A fake programmer
# --------------------------------------------------------------------------- #


class FakeProgrammer:
    """Enough of the programmer interface for the controller and the runner."""

    def __init__(self):
        self.port = "fake"
        self._state = ProgrammerState(connected=True)
        self._listeners = []
        self.ramps = []
        self.started = 0
        self.stopped = 0
        self.opened = 0
        self.closed = 0

    # -- lifecycle ---------------------------------------------------------- #
    def open(self):
        self.opened += 1
        self._state.connected = True

    def close(self):
        self.closed += 1
        self._state.connected = False

    # -- telemetry ---------------------------------------------------------- #
    def on_telemetry(self, callback):
        self._listeners.append(callback)

    def emit(self, **values):
        """Deliver a telemetry frame to the listeners."""
        tlm = Telemetry(raw="fake")
        tlm.values.update(values)
        tlm.units.update({k: "°C" for k in values})
        for cb in list(self._listeners):
            cb(tlm)

    # -- commands ----------------------------------------------------------- #
    def programme_ramp(self, row, rate, limit, time_s):
        self.ramps.append({"row": row, "rate": rate, "limit": limit, "time_s": time_s})

    def start(self):
        self.started += 1

    def stop(self):
        self.stopped += 1

    def wait_for_temperature(self, target_c, tolerance_c=0.5, timeout=3600.0, **kw):
        self._state.temperature = target_c
        return target_c

    def wait_for_stable_temperature(self, target_c, duration_s=0.0, tolerance_c=0.5):
        return target_c


@pytest.fixture
def controller(tmp_path):
    """A controller with a fake programmer, open and recording."""
    prog = FakeProgrammer()
    ctl = StageController(prog, output_dir=tmp_path / "out")
    ctl.open()
    yield ctl
    ctl.close()


# --------------------------------------------------------------------------- #
# Step parsing
# --------------------------------------------------------------------------- #


def test_parse_steps_reads_triples():
    steps = parse_steps("25:2:60,60:5:600")
    assert [(s.target_c, s.rate_c_per_min, s.hold_s) for s in steps] == [
        (25.0, 2.0, 60.0),
        (60.0, 5.0, 600.0),
    ]


def test_parse_steps_defaults_the_rate_and_the_hold():
    steps = parse_steps("25")
    assert steps[0].target_c == 25.0
    assert steps[0].rate_c_per_min == 10.0
    assert steps[0].hold_s == 0.0


def test_parse_steps_rejects_an_empty_spec():
    with pytest.raises(ValueError):
        parse_steps("  ,  ")


def test_parse_steps_rejects_a_bad_triple():
    with pytest.raises(ValueError):
        parse_steps("25:notanumber:60")


# --------------------------------------------------------------------------- #
# The recording
# --------------------------------------------------------------------------- #


def test_the_csv_has_a_header(controller):
    header = controller._csv_path.read_text().splitlines()[0]
    assert header.split(",") == [
        "timestamp",
        "elapsed_s",
        "temperature_c",
        "sensor2_c",
        "programmed_rate_c_per_min",
        "programmed_setpoint_c",
        "phase",
        "event",
    ]


def test_a_sample_is_recorded(controller):
    controller._programmer.emit(temperature=22.5, temperature2=1.0)
    rows = list(csv.reader(controller._csv_path.read_text().splitlines()))
    assert len(rows) == 2, "the header and one sample"
    assert float(rows[1][2]) == pytest.approx(22.5)
    assert float(rows[1][3]) == pytest.approx(1.0)


def test_the_recording_can_be_paused_and_resumed(controller):
    controller.set_recording(False)
    controller._programmer.emit(temperature=10.0)
    controller.set_recording(True)
    controller._programmer.emit(temperature=11.0)
    rows = list(csv.reader(controller._csv_path.read_text().splitlines()))
    assert len(rows) == 2, "only the sample taken while recording"
    assert float(rows[1][2]) == pytest.approx(11.0)


def test_the_programmed_rate_and_setpoint_are_recorded(controller):
    controller._programmer.emit(temperature=20.0, rate=2.0, limit=30.0)
    rows = list(csv.reader(controller._csv_path.read_text().splitlines()))
    assert float(rows[1][4]) == pytest.approx(2.0)
    assert float(rows[1][5]) == pytest.approx(30.0)


# --------------------------------------------------------------------------- #
# The commands
# --------------------------------------------------------------------------- #


def test_a_ramp_is_programmed_and_started(controller):
    controller.start_ramp(40.0, 5.0, 30.0)
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline and not controller._programmer.ramps:
        time.sleep(0.01)
    assert controller._programmer.ramps
    ramp = controller._programmer.ramps[0]
    assert ramp["limit"] == pytest.approx(40.0)
    assert ramp["rate"] == pytest.approx(5.0)
    assert ramp["time_s"] == pytest.approx(30.0)


def test_a_ramp_is_refused_outside_the_instruments_range(controller):
    with pytest.raises(ValueError):
        controller.start_ramp(9000.0, 5.0, 0.0)
    with pytest.raises(ValueError):
        controller.start_ramp(40.0, -1.0, 0.0)


def test_two_programmes_cannot_run_at_once(controller):
    controller.start_ramp(40.0, 5.0, 30.0)
    with pytest.raises(RuntimeError):
        controller.start_ramp(50.0, 5.0, 30.0)


def test_stop_stops_the_instrument(controller):
    result = controller.stop()
    assert result["stopped"]
    assert controller._programmer.stopped >= 1


def test_a_multi_step_programme_runs_every_step(controller):
    controller.start_programme("25:2:0,40:2:0")
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline and len(controller._programmer.ramps) < 2:
        time.sleep(0.01)
    assert [r["limit"] for r in controller._programmer.ramps] == [25.0, 40.0]


def test_the_programmer_is_opened_only_once(controller):
    """A second open would open a second fd and start a second reader.

    Two reader threads on one hidraw node steal each other's reply
    blocks, so the controller must never let that happen.
    """
    controller.start_ramp(30.0, 5.0, 1.0)
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline and not controller._programmer.ramps:
        time.sleep(0.01)
    time.sleep(0.2)
    assert controller._programmer.opened == 1


# --------------------------------------------------------------------------- #
# The state
# --------------------------------------------------------------------------- #


def test_the_state_carries_the_latest_sample(controller):
    controller._programmer.emit(temperature=19.25, temperature2=2.0)
    state = controller.state()
    assert state["temperature_c"] == pytest.approx(19.25)
    assert state["sensor2_c"] == pytest.approx(2.0)
    assert state["connected"] is True


def test_a_subscriber_receives_the_events(controller):
    subscription = controller.subscribe()
    controller._programmer.emit(temperature=21.0)
    event = subscription.get(timeout=2.0)
    assert event["type"] == "telemetry"
    assert event["temperature_c"] == pytest.approx(21.0)


def test_a_slow_subscriber_does_not_stall_the_link(controller):
    """The broadcast must never block the instrument's reader thread."""
    subscription = controller.subscribe()
    for i in range(500):
        controller._programmer.emit(temperature=20.0 + i * 0.01)
    assert subscription.qsize() <= 256, "the queue is bounded"


# --------------------------------------------------------------------------- #
# The HTTP surface
# --------------------------------------------------------------------------- #


@pytest.fixture
def server(controller):
    srv = make_server(controller, "127.0.0.1", 0)
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()
    srv.server_close()


def get(url):
    with urllib.request.urlopen(url, timeout=5) as response:
        return response.status, response.read()


def post(url, payload):
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            return response.status, json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode("utf-8"))


def test_the_simulator_can_drive_the_gui(tmp_path):
    """The GUI must work against the simulator, with no hardware."""
    from linkam.simulator import SimulatedProgrammer

    ctl = StageController(SimulatedProgrammer(start_c=20.0, ambient_c=20.0),
                          output_dir=tmp_path / "sim")
    ctl.open()
    try:
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline and ctl.state()["temperature_c"] is None:
            time.sleep(0.02)
        state = ctl.state()
        assert state["temperature_c"] is not None
        assert state["connected"] is True

        ctl.start_ramp(40.0, 10.0, 5.0)
        deadline = time.monotonic() + 5.0
        while (
            time.monotonic() < deadline
            and ctl.state()["programmed_setpoint_c"] is None
        ):
            time.sleep(0.02)
        assert ctl._programmer.running is True
        assert ctl.state()["programmed_setpoint_c"] == pytest.approx(40.0)
        assert ctl.state()["programmed_rate_c_per_min"] == pytest.approx(10.0)
    finally:
        ctl.close()


def test_the_loopback_servers_listen_on_both_families(controller):
    """`localhost` may resolve to ::1, so both must be served.

    On this machine `getent ahosts localhost` returns ::1 first, and a
    server that listened only on 127.0.0.1 was unreachable under the
    name "localhost": `curl -6 http://localhost:8765/` was refused.
    """
    from linkam.webapp import make_servers

    servers = make_servers(controller, "localhost", 0)
    try:
        assert len(servers) >= 1
        addresses = {s.server_address[0] for s in servers}
        assert "127.0.0.1" in addresses
        # The port must be the same on both, or the two are different
        # servers and a browser would land on only one of them.
        ports = {s.server_address[1] for s in servers}
        assert len(ports) == 1
    finally:
        for server in servers:
            server.server_close()


def test_both_loopback_families_answer(controller):
    from linkam.webapp import make_servers

    servers = make_servers(controller, "localhost", 0)
    threads = []
    try:
        for server in servers:
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            threads.append(thread)
        port = servers[0].server_address[1]
        for address in ("127.0.0.1", "[::1]"):
            try:
                status, body = get(f"http://{address}:{port}/")
            except Exception:
                continue  # no IPv6 on this machine
            assert status == 200
            assert b"Linkam hot stage" in body
    finally:
        for server in servers:
            server.shutdown()
            server.server_close()


def test_the_page_is_served(server):
    status, body = get(server + "/")
    assert status == 200
    assert b"Linkam hot stage" in body

def test_the_state_is_served(server):
    status, body = get(server + "/api/state")
    assert status == 200
    assert "temperature_c" in json.loads(body.decode("utf-8"))


def test_the_csv_is_served(server):
    status, body = get(server + "/api/data.csv")
    assert status == 200
    assert body.splitlines()[0].startswith(b"timestamp,elapsed_s")


def test_a_ramp_can_be_started_over_http(server):
    status, payload = post(
        server + "/api/ramp", {"target_c": 30, "rate_c_per_min": 5, "hold_s": 10}
    )
    assert status == 200
    assert payload["ok"]


def test_a_bad_ramp_is_rejected_over_http(server):
    status, payload = post(server + "/api/ramp", {"target_c": 30, "rate_c_per_min": -1})
    assert status == 400
    assert "error" in payload


def test_a_missing_field_is_rejected_over_http(server):
    status, payload = post(server + "/api/ramp", {})
    assert status == 400
    assert "error" in payload


def test_an_unknown_route_is_a_404(server):
    with pytest.raises(urllib.error.HTTPError) as excinfo:
        get(server + "/api/nope")
    assert excinfo.value.code == 404


def test_the_event_stream_opens_and_reports_the_state(server):
    request = urllib.request.Request(server + "/api/stream")
    with urllib.request.urlopen(request, timeout=5) as response:
        assert response.headers["Content-Type"].startswith("text/event-stream")
        line = response.readline().decode("utf-8")
    assert line.startswith("data: ")
    payload = json.loads(line[len("data: "):])
    assert payload["type"] == "state"


# --------------------------------------------------------------------------- #
# The page's own script
# --------------------------------------------------------------------------- #

def test_the_page_draws_a_plot_with_a_scale_on_both_axes():
    """The plot must carry numbers: degrees on one axis, times on the other.

    The page is one file of HTML with its script inline, so the script is
    run against a stubbed DOM by ``tools/check_plot.js``.  Without node the
    check is skipped -- the browser is the real test of this.
    """
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    result = subprocess.run(
        [node, str(PLOT_CHECK), str(PAGE)],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_the_page_colours_pass_the_contrast_checks():
    """Every colour the page puts together must be readable.

    The thresholds are WCAG 2.2's: 4.5:1 for text, 3:1 for the parts of a
    user interface that carry information.
    """
    result = subprocess.run(
        [sys.executable, str(CONTRAST_CHECK)],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_the_page_is_built_for_the_keyboard_and_a_screen_reader():
    """Every control has a name, the headings are in order, and the
    pop-up behaves like a pop-up."""
    result = subprocess.run(
        [sys.executable, str(PAGE_CHECK)],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stdout + result.stderr


# --------------------------------------------------------------------------- #
# Where the experiment's data goes
# --------------------------------------------------------------------------- #


def test_the_default_name_is_the_date_and_the_time(controller):
    """A sensible default, which the user can give a prefix to."""
    name = controller.default_name()
    assert name.startswith("experiment-")
    assert len(name) == len("experiment-20261112-191503")
    assert controller.default_name("run7").startswith("run7-experiment-")


def test_the_folders_are_listed(server, tmp_path):
    """The picker needs to know what is there."""
    folder = tmp_path / "somewhere"
    folder.mkdir()
    (folder / "alpha").mkdir()
    (folder / "beta").mkdir()
    (folder / "a-file.txt").write_text("not a folder")

    status, body = get(server + f"/api/dirs?path={folder}")
    assert status == 200
    payload = json.loads(body.decode("utf-8"))
    assert payload["dirs"] == ["alpha", "beta"], "only the folders"
    assert payload["parent"] == str(folder.resolve().parent)


def test_a_folder_that_is_not_there_is_refused(server):
    with pytest.raises(urllib.error.HTTPError) as excinfo:
        get(server + "/api/dirs?path=/no/such/folder/at/all")
    assert excinfo.value.code == 404


def test_the_output_folder_can_be_changed(controller, tmp_path):
    """The data goes where the user says."""
    controller._programmer.emit(temperature=21.0)
    before = controller._csv_path
    assert before.exists()

    target = tmp_path / "run7"
    controller.set_output_dir(target)

    assert controller.output_dir == target
    assert controller._csv_path == target / "telemetry.csv"
    assert controller._csv_path.exists(), "a new file is started there"
    assert before.exists(), "what was recorded stays where it was"

    controller._programmer.emit(temperature=22.0)
    rows = list(csv.reader(controller._csv_path.read_text().splitlines()))
    assert float(rows[1][2]) == pytest.approx(22.0)


def test_the_output_folder_can_be_set_over_http(server, tmp_path):
    status, payload = post(
        server + "/api/output",
        {"folder": str(tmp_path), "name": "run7-experiment-20261112-191503"},
    )
    assert status == 200
    assert payload["ok"]
    assert payload["name"] == "run7-experiment-20261112-191503"
    assert payload["output_dir"].endswith("run7-experiment-20261112-191503")


def test_a_name_with_a_path_in_it_is_refused(server, tmp_path):
    """A name is a name, not a path."""
    status, payload = post(
        server + "/api/output", {"folder": str(tmp_path), "name": "../escape"}
    )
    assert status == 400
    assert "error" in payload


def test_the_output_folder_is_refused_while_the_video_is_recording(
    camera_controller, tmp_path
):
    """Stopping the video in the middle of a programme is worse than
    making the user wait."""
    ctl, feed = camera_controller
    ctl.start_video_recording()
    try:
        with pytest.raises(RuntimeError):
            ctl.set_output_dir(tmp_path / "elsewhere")
    finally:
        ctl.stop_video_recording()


# --------------------------------------------------------------------------- #
# The camera
# --------------------------------------------------------------------------- #


class FakeCamera:
    """A camera that hands out one frame at a time.

    It stands in for the microscope's camera: the frames are plain, so a
    test can look at them.
    """

    def __init__(self, width=160, height=120):
        self.name = "fake"
        self.width = width
        self.height = height
        self.fps = 25.0
        self.opened = 0
        self.closed = 0
        self.reads = 0

    @property
    def size(self):
        return (self.width, self.height)

    def open(self):
        self.opened += 1

    def close(self):
        self.closed += 1

    def read(self):
        import numpy as np

        self.reads += 1
        image = np.zeros((self.height, self.width, 3), dtype=np.uint8)
        image[:, :, 0] = 40
        return Frame(image=image, timestamp=time.time(), index=self.reads)


@pytest.fixture
def camera_controller(tmp_path):
    """A controller with a camera, open and ready."""
    from linkam.camera import CameraFeed

    camera = FakeCamera()
    feed = CameraFeed(camera, output_dir=tmp_path / "out")
    ctl = StageController(
        FakeProgrammer(),
        output_dir=tmp_path / "out",
        camera=feed,
    )
    ctl.open()
    yield ctl, feed
    ctl.close()


@pytest.fixture
def camera_server(camera_controller):
    """The same, with the HTTP server in front of it."""
    ctl, feed = camera_controller
    srv = make_server(ctl, "127.0.0.1", 0)
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    yield ctl, feed, f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()
    srv.server_close()


def test_the_camera_opens_with_the_controller(camera_controller):
    ctl, feed = camera_controller
    assert feed.is_open
    assert ctl.state()["camera"]["open"] is True
    assert ctl.state()["camera"]["error"] is None


def test_the_camera_produces_frames(camera_controller):
    ctl, feed = camera_controller
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline and feed.state()["frames"] < 2:
        time.sleep(0.02)
    assert feed.state()["frames"] >= 2


def test_the_feed_serves_jpeg_frames(camera_controller):
    """The browser gets a JPEG of the picture."""
    ctl, feed = camera_controller
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline and feed.latest()[0] is None:
        time.sleep(0.02)
    jpeg, seq = feed.latest()
    assert jpeg is not None
    assert seq > 0
    assert jpeg[:2] == b"\xff\xd8", "it is a JPEG"
    assert jpeg[-2:] == b"\xff\xd9", "and it is complete"


def test_the_numbers_are_burned_into_the_frame(camera_controller):
    """The frame carries the time and the temperature.

    They are burned in rather than drawn over the picture in the
    browser, so a recording carries its own numbers.
    """
    import cv2
    import numpy as np

    ctl, feed = camera_controller
    ctl._programmer.emit(temperature=42.5)
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline and feed.latest()[0] is None:
        time.sleep(0.02)

    jpeg, _ = feed.latest()
    image = cv2.imdecode(np.frombuffer(jpeg, dtype=np.uint8), cv2.IMREAD_COLOR)
    assert image is not None
    assert image[: image.shape[0] // 2, -image.shape[1] // 2 :].any(), (
        "the temperature is in the upper right"
    )
    assert image[-image.shape[0] // 4 :, : image.shape[1] // 2].any(), (
        "the time is in the lower left"
    )


def test_the_video_stream_is_multipart(camera_server):
    """The browser gets the frames one after another."""
    ctl, feed, url = camera_server
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline and feed.latest()[0] is None:
        time.sleep(0.02)

    request = urllib.request.Request(url + "/api/video")
    with urllib.request.urlopen(request, timeout=5) as response:
        assert response.headers["Content-Type"].startswith("multipart/x-mixed-replace")
        chunk = response.read(200)
    assert chunk.startswith(b"--frame"), "it is a multipart stream"
    assert b"image/jpeg" in chunk


def test_recording_starts_with_the_programme(camera_controller, tmp_path):
    """The video is recorded while the programme runs, and stops with it.

    The fake programme finishes in a moment, so the recording is checked
    through the log rather than by polling for it: it must have started
    with the programme and stopped with it.
    """
    ctl, feed = camera_controller
    ctl.set_record_with_programme(True)
    ctl.start_ramp(40.0, 5.0, 1.0)

    def messages():
        return [e.get("message", "") for e in ctl.events()]

    deadline = time.monotonic() + 10.0
    while time.monotonic() < deadline:
        said = messages()
        if any("recording video to" in m for m in said) and any(
            "the video is in" in m for m in said
        ):
            break
        time.sleep(0.05)

    said = messages()
    assert any("recording video to" in m for m in said), (
        "the recording started with the programme"
    )
    assert any("the video is in" in m for m in said), (
        "the recording stopped with it"
    )
    assert not feed.state()["recording"]
    assert (tmp_path / "out" / "video.avi").exists()
    assert (tmp_path / "out" / "video.json").exists()


def test_recording_can_be_started_by_hand(camera_controller, tmp_path):
    ctl, feed = camera_controller
    path = ctl.start_video_recording()
    assert path["recording_path"].endswith("video.avi")
    assert feed.state()["recording"]

    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline and feed.state()["recorded_frames"] < 1:
        time.sleep(0.02)
    assert feed.state()["recorded_frames"] >= 1

    ctl.stop_video_recording()
    assert not feed.state()["recording"]


def test_a_second_recording_does_not_lose_the_first(camera_controller, tmp_path):
    """Two recordings in one run get two files."""
    ctl, feed = camera_controller
    ctl.start_video_recording()
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline and feed.state()["recorded_frames"] < 1:
        time.sleep(0.02)
    ctl.stop_video_recording()

    ctl.start_video_recording()
    assert feed.state()["recording_path"].endswith("video-2.avi")


def test_the_video_can_be_armed_over_http(camera_server):
    ctl, feed, url = camera_server
    status, payload = post(url + "/api/video", {"record": True})
    assert status == 200
    assert payload["record_with_programme"] is True
    assert ctl.state()["camera"]["record_with_programme"] is True


def test_the_video_can_be_recorded_over_http(camera_server):
    ctl, feed, url = camera_server
    status, payload = post(url + "/api/video", {"manual": True})
    assert status == 200
    assert payload["recording_path"].endswith("video.avi")

    status, payload = post(url + "/api/video", {"manual": False})
    assert status == 200
    assert not feed.state()["recording"]


def test_a_camera_that_will_not_open_does_not_stop_the_gui(tmp_path):
    """The camera is best-effort: the temperature link must not be held
    up by the microscope."""
    from linkam.camera import CameraFeed

    class Broken:
        name = "broken"

        def open(self):
            raise RuntimeError("no camera is connected")

        def close(self):
            pass

        def read(self):
            return None

    feed = CameraFeed(Broken(), output_dir=tmp_path / "out")
    ctl = StageController(FakeProgrammer(), output_dir=tmp_path / "out", camera=feed)
    ctl.open()
    try:
        state = ctl.state()
        assert state["camera"]["open"] is False
        assert "no camera" in state["camera"]["error"]
        # The temperature link still works.
        ctl._programmer.emit(temperature=30.0)
        assert ctl.state()["temperature_c"] == pytest.approx(30.0)
    finally:
        ctl.close()


def test_the_demo_mode_needs_no_hardware():
    """``--demo`` opens the whole page with nothing attached."""
    from argparse import Namespace

    from linkam.webapp import _resolve_mode

    args = Namespace(simulate=False, demo=True, camera="none")
    assert _resolve_mode(args) == (True, "synthetic")

    # A camera asked for by name still wins.
    args.camera = "aravis"
    assert _resolve_mode(args) == (True, "aravis")

    # And without it, the hardware is used as usual.
    args.demo = False
    assert _resolve_mode(args) == (False, "aravis")
    args.camera = "none"
    args.simulate = True
    assert _resolve_mode(args) == (True, "none")
