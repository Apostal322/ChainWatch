"""Capture dashboard screenshots from a *running* ChainWatch service (for the README).

    pip install playwright==1.56.0   # uses an existing Chromium; see README
    python scripts/capture_screenshots.py --url http://127.0.0.1:8000 --prefix demo_

Only commit screenshots of the synthetic demo; the real dataset has no redistribution licence.

Everything captured is rendered from the live API; nothing is mocked.
"""

from __future__ import annotations

import argparse
from pathlib import Path

from playwright.sync_api import sync_playwright


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="http://127.0.0.1:8000")
    ap.add_argument("--out", type=Path, default=Path("docs/images"))
    ap.add_argument("--step", default="47")
    ap.add_argument("--prefix", default="")
    args = ap.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1440, "height": 1000}, color_scheme="light")
        page.goto(args.url)
        page.wait_for_selector("select")
        page.select_option("select >> nth=0", args.step)
        page.wait_for_selector("tbody tr.clickable")
        page.wait_for_timeout(500)
        page.click("tbody tr.clickable >> nth=0")
        page.wait_for_selector("svg[aria-label='Transaction graph neighbourhood']")
        page.get_by_role("button", name="2 hops").click()
        page.wait_for_timeout(800)
        page.screenshot(path=args.out / f"{args.prefix}replay.png", full_page=True)
        for tab, name in (
            ("Performance", "performance"),
            ("Models & experiments", "models"),
            ("Limitations", "limitations"),
        ):
            page.get_by_role("tab", name=tab).click()
            page.wait_for_timeout(2500)
            page.screenshot(path=args.out / f"{args.prefix}{name}.png", full_page=True)
        browser.close()


if __name__ == "__main__":
    main()
