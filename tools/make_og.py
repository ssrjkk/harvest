"""OG-картинка сайта 1200x630 в фирменном стиле (тёмный фон, золото, condensed).

Использование:  python tools/make_og.py [--out docs/assets/og.png]
Шрифты: системные Windows (Impact, Consolas). Без внешних зависимостей.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT_DEFAULT = ROOT / "docs" / "assets" / "og.png"
sys.path.insert(0, str(ROOT))

from core.version import VERSION  # noqa: E402  (путь к корню добавлен выше)

BG = (20, 17, 10)
GOLD = (212, 167, 44)
CREAM = (240, 232, 208)
MUTED = (138, 128, 104)
GREEN = (95, 173, 86)


def main() -> int:
    ap = argparse.ArgumentParser(description="Сгенерировать OG-картинку сайта.")
    ap.add_argument("--out", default=str(OUT_DEFAULT))
    args = ap.parse_args()

    from PIL import Image, ImageDraw, ImageFont

    W, H = 1200, 630
    img = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(img)

    def font(name: str, size: int):
        for p in (f"C:\\Windows\\Fonts\\{name}", f"/usr/share/fonts/{name}"):
            if os.path.exists(p):
                try:
                    return ImageFont.truetype(p, size)
                except OSError:
                    pass
        return ImageFont.load_default()

    f_disp = font("impact.ttf", 150)
    f_mono = font("consola.ttf", 34)
    f_small = font("consola.ttf", 28)

    # Золотая плашка слева + рамка.
    d.rectangle([0, 0, 26, H], fill=GOLD)
    d.rectangle([0, 0, W - 1, H - 1], outline=GOLD, width=4)
    d.text((90, 90), f"harvest v{VERSION}", font=f_mono, fill=GOLD)
    d.text((86, 170), "ФЕРМА", font=f_disp, fill=CREAM)
    d.text((86, 320), "ТЕСТНЕТОВ", font=f_disp, fill=GOLD)
    d.text((90, 500), "EVM TESTNET FARMER · MIT · NO DROP PROMISES",
           font=f_small, fill=MUTED)
    # Мини-сетка поля справа внизу.
    cw, gap, ox, oy = 26, 8, 880, 380
    states = [GREEN, GOLD, (44, 38, 24), GREEN, GOLD, (44, 38, 24),
              GOLD, (44, 38, 24), GREEN]
    for i, col in enumerate(states):
        x = ox + (i % 3) * (cw + gap)
        y = oy + (i // 3) * (cw + gap)
        d.rectangle([x, y, x + cw, y + cw], fill=col)

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    img.save(out)
    print(f"ok: {out} ({os.path.getsize(out)} bytes)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
