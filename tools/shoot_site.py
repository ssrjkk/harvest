"""Скриншоты сайта для README: python tools/shoot_site.py [--url URL] [--out docs/screenshots].

Требуется: pip install playwright + python -m playwright install chromium --only-shell.
Снимает 4 кадра (hero, поле, мастер новичка, про-справочник) в дефолтной
тёмной теме. Анимации заморожены (reduced-motion) для детерминированности.

Матрица приёмки: --matrix снимает hero на 375/768/1440 × 3 темы × RU/EN
в docs/screenshots/matrix/ и проверяет отсутствие горизонтального скролла
и видимость бургера на 375px.
"""

from __future__ import annotations

import argparse
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
    ap.add_argument("--width", type=int, default=1440)
    ap.add_argument("--theme", default="ink", choices=["paper", "ink", "moss"])
    ap.add_argument("--lang", default="ru", choices=["ru", "en"])
    ap.add_argument("--matrix", action="store_true",
                    help="матрица 375/768/1440 × темы × языки + проверка скролла")
    args = ap.parse_args()

    from playwright.sync_api import sync_playwright

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    with sync_playwright() as p:
        browser = p.chromium.launch()
        if args.matrix:
            return _matrix(browser, args, out)
        ctx = browser.new_context(
            viewport={"width": args.width, "height": 900},
            device_scale_factor=1,
            reduced_motion="reduce",
        )
        ctx.add_init_script(
            f"localStorage.setItem('hv-theme','{args.theme}');"
            f"localStorage.setItem('hv-lang','{args.lang}');"
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


def _matrix(browser, args, out: Path) -> int:
    """Матрица приёмки: ширины × темы × языки. Проверяет скролл и бургер."""
    mdir = out / "matrix"
    mdir.mkdir(parents=True, exist_ok=True)
    fails = 0
    for width in (375, 768, 1440):
        for theme in ("paper", "ink", "moss"):
            for lang in ("ru", "en"):
                name = f"shot-{width}-{theme}-{lang}.png"
                ctx = browser.new_context(
                    viewport={"width": width, "height": 900},
                    device_scale_factor=1,
                    reduced_motion="reduce",
                )
                ctx.add_init_script(
                    f"localStorage.setItem('hv-theme','{theme}');"
                    f"localStorage.setItem('hv-lang','{lang}');"
                )
                page = ctx.new_page()
                page.goto(args.url, wait_until="networkidle")
                page.wait_for_selector("h1", timeout=15000)
                page.wait_for_timeout(400)
                page.screenshot(path=str(mdir / name))
                metrics = page.evaluate(
                    """() => ({
                        scrollW: document.documentElement.scrollWidth,
                        innerW: window.innerWidth,
                        burger: getComputedStyle(document.getElementById('navToggle')).display,
                        navLinks: document.querySelectorAll('#mainNav a').length
                    })"""
                )
                ok_scroll = metrics["scrollW"] <= metrics["innerW"] + 1
                ok_burger = (metrics["burger"] != "none") if width <= 900 else True
                ok_nav = metrics["navLinks"] == 9
                status = "ok" if (ok_scroll and ok_burger and ok_nav) else "FAIL"
                if status == "FAIL":
                    fails += 1
                print(f"{status}: {name} scroll={metrics['scrollW']}/{metrics['innerW']} "
                      f"burger={metrics['burger']} links={metrics['navLinks']}")
                ctx.close()
    browser.close()
    print(f"matrix done, fails={fails}")
    return 1 if fails else 0


if __name__ == "__main__":
    raise SystemExit(main())
