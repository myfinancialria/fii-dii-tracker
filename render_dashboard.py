"""Render output/index.html → output/dashboard.jpg using headless Chromium."""
from __future__ import annotations

import sys
from pathlib import Path

OUT_DIR = Path(__file__).parent / "output"
HTML = OUT_DIR / "index.html"
JPG = OUT_DIR / "dashboard.jpg"


def main() -> int:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("Install Playwright: pip install playwright && python -m playwright install chromium",
              file=sys.stderr)
        return 1

    if not HTML.exists():
        print(f"{HTML} missing — run visualize.py first", file=sys.stderr)
        return 1

    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(
            viewport={"width": 1240, "height": 900},
            device_scale_factor=2,  # crisp text
        )
        page.goto(f"file://{HTML.absolute()}")
        # let fonts + embedded chart image settle
        page.wait_for_load_state("networkidle")
        page.wait_for_timeout(400)
        page.screenshot(
            path=str(JPG),
            full_page=True,
            type="jpeg",
            quality=88,
        )
        browser.close()

    print(f"Wrote {JPG} ({JPG.stat().st_size // 1024} KB)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
