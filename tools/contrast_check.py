"""Проверка контраста токенов по WCAG AA.

Использование:  python tools/contrast_check.py [--write]
Печатает таблицу и (с --write) обновляет docs/contrast.md.
Пары и пороги: обычный текст 4.5:1, крупный текст/UI 3:1.
Выход 1, если хоть одна пара не проходит.
"""

from __future__ import annotations

import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TOKENS = os.path.join(ROOT, "design", "tokens.json")
OUT_MD = os.path.join(ROOT, "docs", "contrast.md")

# (пара, назначение, порог)
PAIRS: tuple[tuple[str, str, str, float], ...] = (
    ("ink", "bg", "основной текст", 4.5),
    ("ink2", "bg", "вторичный текст", 4.5),
    ("muted", "bg", "подписи 11–13px", 4.5),
    ("gold-ink", "gold", "текст кнопок", 4.5),
    ("chip-ink", "chip", "инверсные плашки", 4.5),
    ("ok", "bg", "статус ok", 4.5),
    ("warn", "bg", "статус warn", 4.5),
    ("err", "bg", "статус err", 4.5),
    ("blue", "bg", "ссылки/инфо", 4.5),
    ("gold", "bg", "крупный текст/бренд", 3.0),
)


def _lum(hex_color: str) -> float:
    c = hex_color.lstrip("#")
    rgb = [int(c[i : i + 2], 16) / 255.0 for i in (0, 2, 4)]

    def lin(v: float) -> float:
        return v / 12.92 if v <= 0.03928 else ((v + 0.055) / 1.055) ** 2.4

    r, g, b = (lin(v) for v in rgb)
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def ratio(a: str, b: str) -> float:
    la, lb = _lum(a), _lum(b)
    hi, lo = max(la, lb), min(la, lb)
    return (hi + 0.05) / (lo + 0.05)


def main() -> int:
    write = "--write" in sys.argv
    with open(TOKENS, encoding="utf-8") as f:
        themes = json.load(f)["themes"]
    rows = ["# Контраст токенов (WCAG AA)", "",
            "Сгенерировано `tools/contrast_check.py`. Порог: 4.5:1 текст, 3:1 крупный/UI.", "",
            "| тема | пара | назначение | ratio | порог | итог |"]
    ok_all = True
    for theme_name in ("ink", "paper", "moss"):
        t = themes[theme_name]
        for fg, bg, purpose, need in PAIRS:
            r = ratio(t[fg], t[bg])
            ok = r >= need
            ok_all = ok_all and ok
            mark = "OK" if ok else "FAIL"
            line = f"| {theme_name} | {fg}/{bg} | {purpose} | {r:.2f} | {need} | {mark} |"
            print(line)
            rows.append(line)
    if write:
        with open(OUT_MD, "w", encoding="utf-8") as f:
            f.write("\n".join(rows) + "\n")
        print(f"wrote {OUT_MD}")
    return 0 if ok_all else 1


if __name__ == "__main__":
    raise SystemExit(main())
