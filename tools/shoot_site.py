"""Скриншоты сайта для README: python tools/shoot_site.py [--url URL] [--out docs/screenshots].

Требуется: pip install playwright + python -m playwright install chromium --only-shell.
Снимает 4 кадра (hero, пульт, мастер новичка, про-справочник) в дефолтной
тёмной теме. Анимации заморожены (reduced-motion) для детерминированности.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT_DEFAULT = ROOT / "docs" / "screenshots"

SHOTS: tuple[tuple[str, str], ...] = (
    ("shot-hero.png", "hero"),
    ("shot-live.png", "live"),
    ("shot-wizard.png", "wizard"),
    ("shot-pro.png", "pro"),
)


def main() -> int:
    ap = argparse.ArgumentParser(description="Снять скриншоты сайта для README.")
    ap.add_argument("--url", default="https://ssrjkk.github.io/harvest/")
    ap.add_argument("--out", default=str(OUT_DEFAULT))
    args = ap.parse_args()

    from playwright.sync_api import sync_playwright

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    with sync_playwright() as p:
        browser = p.chromium.launch()
        ctx = browser.new_context(
            viewport={"width": 1440, "height": 900},
            device_scale_factor=1,
            reduced_motion="reduce",
        )
        page = ctx.new_page()
        page.goto(args.url, wait_until="networkidle")
        page.wait_for_selector("h1", timeout=15000)

        def snap(name: str, anchor: str | None) -> None:
            if anchor:
                page.evaluate(
                    """(id) => {
                        const el = document.getElementById(id);
                        el.scrollIntoView({block: 'start'});
                        window.scrollBy(0, -90);
                    }""", anchor)
                page.wait_for_timeout(500)
            else:
                page.evaluate("window.scrollTo(0, 0)")
                page.wait_for_timeout(400)
            page.screenshot(path=str(out / name))
            print(f"ok: {name}")

        snap("shot-hero.png", None)
        snap("shot-live.png", "live")
        snap("shot-wizard.png", "app")
        # Про-режим + вкладка справочника.
        page.evaluate("document.getElementById('modePro').click()")
        page.evaluate(
            "document.querySelector('.play-tabs button[data-tab=\"t-ref\"]').click()")
        snap("shot-pro.png", "app")

        browser.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
