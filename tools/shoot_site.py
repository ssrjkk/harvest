"""Скриншоты сайта для README: python tools/shoot_site.py [--url URL] [--out docs/screenshots].

Требуется: pip install playwright + python -m playwright install chromium --only-shell.
Снимает 4 кадра (hero, поле, мастер новичка, про-справочник) в дефолтной
тёмной теме. Анимации заморожены (reduced-motion) для детерминированности.

Сайт многостраничный, поэтому кадр задаёт свою страницу: якоря ленты (#field
на «Поле», #app на «Демо») больше не на одной странице и скроллом не достаются.
Готовность страницы ждём по её собственному узлу (h1 есть не везде).

Матрица приёмки: --matrix снимает главную на 375/768/1440 × 3 темы × RU/EN
в docs/screenshots/matrix/ и проверяет на всех 4 страницах отсутствие
горизонтального скролла, видимость бургера на 375px, состав навигации и
отсутствие ошибок JS (страничные скрипты выходят по своим якорям).
"""

from __future__ import annotations

import argparse
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT_DEFAULT = ROOT / "docs" / "screenshots"

# Синхронно с NAV_LINKS в tools/verify_site.py.
SITE_PAGES: tuple[str, ...] = ("index.html", "field.html", "demo.html", "docs.html")
NAV_LINKS = 4

# Признак «страница отрисована»: #tickerIn есть на всех страницах и виден при
# любой ширине. Ловить узел конкретной страницы нельзя — на «Демо» #proView
# скрыт в режиме «Новичок», .play-tabs — вкладки Про-режима, а #mainNav скрыт
# на бургере (≤900px). Наличие id=tickerIn на всех страницах держит verify_site.
PAGE_READY = "#tickerIn"

# (файл, страница, якорь, js-подготовка перед скриншотом)
SHOTS: tuple[tuple[str, str, str | None, str | None], ...] = (
    ("shot-hero.png", "index.html", None, None),
    ("shot-live.png", "field.html", "field", None),
    ("shot-wizard.png", "demo.html", "app", None),
    (
        "shot-pro.png",
        "demo.html",
        "app",
        'document.getElementById("modePro").click();'
        'document.querySelector(\'.play-tabs button[data-tab="t-ref"]\').click();',
    ),
)

METRICS = """() => ({
    scrollW: document.documentElement.scrollWidth,
    innerW: window.innerWidth,
    burger: getComputedStyle(document.getElementById('navToggle')).display,
    navLinks: document.querySelectorAll('#mainNav a').length
})"""


def _url(base: str, page: str) -> str:
    """Страница сайта относительно базы: .../harvest → .../harvest/field.html."""
    root = base.rstrip("/")
    if root.endswith(".html"):
        root = root.rsplit("/", 1)[0]
    return f"{root}/{page}"


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

        def snap(name: str, page_name: str, anchor: str | None, setup: str | None) -> None:
            page.goto(_url(args.url, page_name), wait_until="networkidle")
            page.wait_for_selector(PAGE_READY, timeout=15000)
            if setup:
                page.evaluate(setup)
                page.wait_for_timeout(300)
            if anchor:
                page.evaluate(
                    """(id) => {
                        const el = document.getElementById(id);
                        if (!el) throw new Error('нет якоря #' + id);
                        el.scrollIntoView({block: 'start'});
                        window.scrollBy(0, -90);
                    }""", anchor)
                page.wait_for_timeout(500)
            else:
                page.evaluate("window.scrollTo(0, 0)")
                page.wait_for_timeout(400)
            page.screenshot(path=str(out / name))
            print(f"ok: {name} ({page_name})")

        for name, page_name, anchor, setup in SHOTS:
            snap(name, page_name, anchor, setup)

        browser.close()
    return 0


def _matrix(browser, args, out: Path) -> int:
    """Матрица приёмки: ширины × темы × языки.

    Скриншот снимает главную (как раньше), а скролл/бургер/навигацию/ошибки JS
    проверяет на всех страницах — регресс на многостраничность иначе не видно.
    """
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
                errors: list[str] = []
                # _errs=errors — привязка к текущему списку, а не к переменной цикла.
                page.on("pageerror", lambda e, _errs=errors: _errs.append(str(e)))
                if width == 375:  # главный кадр снимаем один раз на тему/язык
                    page.goto(_url(args.url, "index.html"), wait_until="networkidle")
                    page.wait_for_selector(PAGE_READY, timeout=15000)
                    page.wait_for_timeout(400)
                    page.screenshot(path=str(mdir / name))
                for pg in SITE_PAGES:
                    errors.clear()
                    page.goto(_url(args.url, pg), wait_until="networkidle")
                    page.wait_for_selector(PAGE_READY, timeout=15000)
                    page.wait_for_timeout(250)
                    metrics = page.evaluate(METRICS)
                    ok_scroll = metrics["scrollW"] <= metrics["innerW"] + 1
                    ok_burger = (metrics["burger"] != "none") if width <= 900 else True
                    ok_nav = metrics["navLinks"] == NAV_LINKS
                    ok_js = not errors
                    status = "ok" if (ok_scroll and ok_burger and ok_nav and ok_js) else "FAIL"
                    if status == "FAIL":
                        fails += 1
                        print(f"{status}: {name} {pg} scroll={metrics['scrollW']}/{metrics['innerW']} "
                              f"burger={metrics['burger']} links={metrics['navLinks']} js={errors}")
                    else:
                        print(f"{status}: {name} {pg}")
                ctx.close()
    browser.close()
    print(f"matrix done, fails={fails}")
    return 1 if fails else 0


if __name__ == "__main__":
    raise SystemExit(main())
