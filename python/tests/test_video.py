"""Tests for the numbers written onto the video frames.

The frames carry the time and the temperature so that a recording can
be read on its own, later, without a sidecar.
"""

from __future__ import annotations

import time

import numpy as np
import pytest

from linkam.video import annotate


@pytest.fixture
def frame():
    """A dark frame, so the writing on it can be seen."""
    return np.zeros((480, 640, 3), dtype=np.uint8)


def _ink(image, region) -> int:
    """How much is written in a region."""
    y0, y1, x0, x1 = region
    return int((image[y0:y1, x0:x1] > 40).sum())


def test_the_time_is_in_the_lower_left(frame):
    annotate(frame, when=time.time())
    assert _ink(frame, (380, 480, 0, 300)) > 0, "the time is there"
    assert _ink(frame, (0, 120, 0, 300)) == 0, "and nowhere else"


def test_the_temperature_is_in_the_upper_right(frame):
    annotate(frame, temperature_c=95.25, when=time.time())
    assert _ink(frame, (0, 120, 340, 640)) > 0, "the temperature is there"
    assert _ink(frame, (0, 120, 0, 300)) == 0, "and nowhere else"


def test_the_setpoint_is_written_under_the_temperature(frame):
    annotate(frame, temperature_c=95.25, target_c=95.0, when=time.time())
    assert _ink(frame, (35, 62, 340, 640)) > 0, "the setpoint is there"


def test_the_lettering_is_antialiased(frame):
    """A real font, not a stencil.

    The edges of the letters carry shades in between, which is what
    distinguishes a drawn font from OpenCV's own lettering.
    """
    annotate(frame, temperature_c=95.25, when=time.time())
    region = frame[0:120, 340:640]
    grey = region.max(axis=2)
    values = sorted(set(grey[grey > 40].tolist()))
    assert len(values) > 4, "there are shades between the letters"


def test_the_numbers_sit_on_a_flat_plate():
    """A flat panel behind the letters, not a blur under them.

    The panel is what makes white text readable against a bright
    picture.  It has to be flat: a shadow would be a blur, and a blur
    shows as a gradient, not as one value.
    """
    pytest.importorskip("PIL")
    from linkam.video import _load_font, _SANS_FONTS

    font = _load_font(_SANS_FONTS, 20)
    if font is None:
        pytest.skip("no font on this machine")

    bright = np.full((480, 640, 3), 200, dtype=np.uint8)
    annotate(bright, temperature_c=95.25, when=time.time())

    bbox = font.getbbox("95.25 °C")
    margin = max(10, round(480 * 0.022))
    left = 640 - margin - (bbox[2] - bbox[0])
    top = margin

    # A strip just above the letters: the panel, and nothing else.
    strip = bright[top - 3 : top - 1, left + 12 : left + 40].max(axis=2)
    assert (strip < 200).all(), "the panel is there"
    assert strip.std() < 1.0, "and it is flat, not a blur"
    assert strip.max() > 40, "and the picture shows through it"


def test_the_frame_is_not_changed_where_nothing_is_written(frame):
    """Only the text's own block is touched."""
    annotate(frame, temperature_c=95.25, when=time.time())
    untouched = frame[200:400, 0:300]
    assert not untouched.any(), "the rest of the frame is left alone"


def test_the_overlay_falls_back_without_pillow(frame, monkeypatch):
    """A machine without Pillow still gets the numbers."""
    import linkam.video as video

    real_import = __import__

    def no_pillow(name, *args, **kwargs):
        if name == "PIL" or name.startswith("PIL."):
            raise ImportError("no Pillow here")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr("builtins.__import__", no_pillow)
    video._font_cache.clear()
    annotate(frame, temperature_c=95.25, when=time.time())
    assert _ink(frame, (0, 120, 340, 640)) > 0, "the temperature is there"
    assert _ink(frame, (380, 480, 0, 300)) > 0, "and the time"


def test_a_real_font_is_used_when_there_is_one():
    """The font is a TrueType one, if the machine has any."""
    from linkam.video import _load_font, _MONO_FONTS, _SANS_FONTS

    pytest.importorskip("PIL")
    assert _load_font(_SANS_FONTS, 20) is not None, "a sans font"
    assert _load_font(_MONO_FONTS, 20) is not None, "a monospace font"
