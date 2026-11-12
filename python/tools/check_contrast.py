"""Check the page's colour pairings against WCAG 2.2.

The thresholds: 4.5:1 for body text, 3:1 for large text (18px and over)
and for the parts of a user interface that carry information, and 4.5:1
for anything below 24px.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

PAGE = Path(__file__).parent.parent / "linkam" / "static" / "index.html"


def luminance(hex_colour: str) -> float:
    r, g, b = (int(hex_colour[i : i + 2], 16) / 255 for i in (1, 3, 5))

    def channel(c):
        return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4

    r, g, b = channel(r), channel(g), channel(b)
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def contrast(a: str, b: str) -> float:
    la, lb = luminance(a), luminance(b)
    lighter, darker = max(la, lb), min(la, lb)
    return (lighter + 0.05) / (darker + 0.05)


def main() -> int:
    html = PAGE.read_text()
    colours = dict(re.findall(r"--([a-z0-9-]+):\s*(#[0-9a-fA-F]{6});", html))

    # What the page actually puts together, and what each pairing has to
    # reach: text, large text, or the parts of a user interface.
    #
    # The order is (foreground, background).  The University's own
    # accessibility grid is read the other way round -- each row is a
    # background and its columns are the text colours -- so "Rebel's
    # gold" says navy on it is 7.8:1 and white on it is 1.9:1.  The
    # badge that holds the stage therefore takes navy text.
    pairings = [
        ("navy", "white", 4.5, "body text on a card"),
        ("navy", "stone-40", 4.5, "body text on the page"),
        ("navy-80", "white", 4.5, "captions on a card"),
        ("navy-80", "stone-40", 4.5, "captions on the page"),
        ("navy-80", "navy-5", 4.5, "the numbers on the plot"),
        ("white", "navy", 4.5, "the header"),
        ("white", "navy-80", 4.5, "the recording badge"),
        ("white", "green", 4.5, "the ramping badge"),
        ("navy", "gold", 4.5, "the holding badge"),
        ("white", "red", 4.5, "the error badge"),
        ("white", "red", 4.5, "the stop button"),
        ("navy", "navy-5", 3.0, "the trace on the plot"),
        ("red", "navy-5", 3.0, "the setpoint line on the plot"),
        ("navy-60", "white", 3.0, "the border of an input"),
        ("navy-60", "white", 3.0, "the border of a card"),
        ("sky", "white", 3.0, "the focus ring"),
        ("white", "navy", 3.0, "the focus ring on the header"),
    ]

    failed = 0
    for fg, bg, need, what in pairings:
        if fg not in colours or bg not in colours:
            print(f"??     {what}: --{fg} or --{bg} is not in the page")
            failed += 1
            continue
        ratio = contrast(colours[fg], colours[bg])
        ok = ratio >= need
        if not ok:
            failed += 1
        print(
            f"{'PASS' if ok else 'FAIL'}  {ratio:5.2f}:1  (needs {need})  "
            f"{what}"
        )

    print(f"\n{len(pairings) - failed} passed, {failed} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
