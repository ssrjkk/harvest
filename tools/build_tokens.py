"""Генератор CSS-переменных из design/tokens.json.

Использование:  python tools/build_tokens.py
Пишет:         docs/assets/tokens.css (сгенерирован — не править руками).

Порядок блоков: :root (тема paper — дефолт без data-theme),
[data-theme="ink"], [data-theme="moss"].
"""

from __future__ import annotations

import json
import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TOKENS = os.path.join(ROOT, "design", "tokens.json")
OUT = os.path.join(ROOT, "docs", "assets", "tokens.css")

ORDER = ("bg", "bg2", "ink", "ink2", "muted", "line", "line2", "dots",
         "gold", "gold-ink", "paper", "chip", "chip-ink",
         "ok", "warn", "err", "blue")


def _block(selector: str, values: dict) -> str:
    lines = [f"{selector}{{"]
    for key in ORDER:
        lines.append(f"  --{key}:{values[key]};")
    lines.append("}")
    return "\n".join(lines)


def main() -> int:
    with open(TOKENS, encoding="utf-8") as f:
        tokens = json.load(f)
    themes = tokens["themes"]
    for name in ("paper", "ink", "moss"):
        missing = [k for k in ORDER if k not in themes[name]]
        if missing:
            print(f"FAIL: тема {name}: нет ключей {missing}")
            return 1
    css = (
        "/* GENERATED from design/tokens.json by tools/build_tokens.py — не править руками. */\n"
        + _block(":root", themes["paper"])
        + "\n"
        + _block('[data-theme="ink"]', themes["ink"])
        + "\n"
        + _block('[data-theme="moss"]', themes["moss"])
        + "\n"
    )
    with open(OUT, "w", encoding="utf-8") as f:
        f.write(css)
    print(f"ok: {OUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
