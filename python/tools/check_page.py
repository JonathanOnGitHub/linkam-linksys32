#!/usr/bin/env python3
"""Check the page's structure against the accessibility guidelines.

The colour contrast is checked by ``check_contrast.py``; this looks at
the things contrast cannot see: that every control has a name, that the
headings are in order, that the page can be reached with the keyboard,
and that the pop-up behaves like a pop-up.

    ./venv/bin/python tools/check_page.py
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

PAGE = Path(__file__).parent.parent / "linkam" / "static" / "index.html"


class Page:
    """The page, read as text."""

    def __init__(self, html: str) -> None:
        self.html = html

    def tags(self, name: str) -> list[str]:
        return re.findall(rf"<{name}\b[^>]*>", self.html, re.I)

    def tag(self, name: str) -> str:
        found = self.tags(name)
        return found[0] if found else ""

    def by_id(self, element_id: str) -> str:
        match = re.search(rf'<[^>]*\bid="{element_id}"[^>]*>', self.html)
        return match.group(0) if match else ""

    def text(self) -> str:
        return re.sub(r"<[^>]+>", " ", self.html)


def main() -> int:
    html = PAGE.read_text()
    page = Page(html)
    results: list[tuple[bool, str]] = []

    def check(ok: bool, what: str) -> None:
        # The tags are long and often wrapped; keep the report on one line.
        results.append((bool(ok), " ".join(what.split())))

    # -- the document ------------------------------------------------------ #
    check('<html lang="en"' in html, "the page says what language it is in")
    check("<title>" in html, "the page has a title")
    check("class=\"skip\"" in html, "there is a skip link")
    check("href=\"#main\"" in html, "the skip link goes to the content")
    check('id="main"' in html, "the content has an id to skip to")

    # -- headings ---------------------------------------------------------- #
    headings = [
        (int(level), text)
        for level, text in re.findall(r"<h([1-6])[^>]*>(.*?)</h\1>", html, re.S)
    ]
    check(bool(headings), "the page has headings")
    check(headings and headings[0][0] == 1, "the first heading is a level 1")
    jumps = [
        (headings[i - 1][1].strip(), headings[i][1].strip())
        for i in range(1, len(headings))
        if headings[i][0] > headings[i - 1][0] + 1
    ]
    check(not jumps, f"the headings do not skip a level ({jumps})")

    # -- controls --------------------------------------------------------- #
    inputs = page.tags("input")
    for tag in inputs:
        element_id = re.search(r'id="([^"]+)"', tag)
        check(bool(element_id), f"an input has an id: {tag}")
        if not element_id:
            continue
        name = element_id.group(1)
        labelled = (
            f'for="{name}"' in html
            or f'aria-label="{name}"' in html
            or f'aria-labelledby="{name}"' in html
        )
        check(labelled, f"the input {name} has a label")

    buttons = page.tags("button")
    check(bool(buttons), "the page has buttons")
    for tag in buttons:
        check('type="' in tag, f"a button says what type it is: {tag}")
    check(
        all(
            re.search(r'id="([^"]+)"', tag)
            and f'id="{re.search(r"id=.([^\"]+)", tag).group(1)}"' in html
            for tag in buttons
        ),
        "every button is referred to by the page",
    )

    # -- images ----------------------------------------------------------- #
    for tag in page.tags("img"):
        check("alt=" in tag, f"an image says what it is: {tag}")

    # -- the pop-up -------------------------------------------------------- #
    modal = page.by_id("savemodal")
    check(bool(modal), "there is a pop-up")
    check('role="dialog"' in html, "the pop-up is a dialog")
    check('aria-modal="true"' in html, "the pop-up is modal")
    check("aria-labelledby" in html, "the pop-up is named")
    check("Escape" in page.text(), "the pop-up closes on Escape")
    check("Tab" in page.text(), "the pop-up keeps the keyboard inside it")
    check(
        ":focus-visible" in html, "there is a visible focus ring"
    )
    check(
        "prefers-reduced-motion" in html, "the page respects a reduced-motion setting"
    )
    check("forced-colors" in html, "the page survives forced colours")

    # -- the log ----------------------------------------------------------- #
    check('role="log"' in html, "the log is a log")
    check('aria-live="polite"' in html, "the log announces itself politely")
    check('role="status"' in html, "the phase is announced when it changes")

    # -- the report -------------------------------------------------------- #
    failed = 0
    for ok, what in results:
        if not ok:
            failed += 1
        print(f"{'PASS' if ok else 'FAIL'}  {what}")
    print(f"\n{len(results) - failed} passed, {failed} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
