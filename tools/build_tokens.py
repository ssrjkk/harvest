"""Генератор CSS-переменных из design/tokens.json.

Использование:  python tools/build_tokens.py
Пишет:         docs/assets/tokens.css (сгенерирован — не править руками).

Порядок блоков: :root (тема paper — дефолт без data-theme),
html[data-theme="paper"], html[data-theme="ink"], html[data-theme="moss"].
Селекторы квалифицированы элементом html: голый [data-theme="..."] имеет
специфичность (0,1,0) — как у псевдокласса :root, и при ничьей выигрывает
порядок. Раньше terminal.css грузился последним и его :root перебивал ВСЕ
темы (проверено в браузере: paper/ink/moss резолвились в terminal-значения);
файл удалён, но html[...] остаётся защитой от любого чужого :root.
html[...] даёт (0,1,1) и всегда выигрывает у :root любого файла.
Явный блок paper нужен тоже: раньше paper жил только в голом :root и
проигрывал terminal :root по порядку.
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


def _block(selector: str, values: dict, tokens: dict) -> str:
    lines = [f"{selector}{{"]
    for key in ORDER:
        lines.append(f"  --{key}:{values[key]};")
    # Шкала/шрифты — единый источник (раньше были мёртвыми в tokens.json).
    scale = tokens.get("scale", {})
    if isinstance(scale.get("space"), list) and scale["space"]:
        lines.append(f"  --space:{'px '.join(str(x) for x in scale['space'])}px;".replace("px px", "px"))
        for i, sp in enumerate(scale["space"]):
            lines.append(f"  --sp-{i}:{sp}px;")
    if "border" in scale:
        lines.append(f"  --bd:{scale['border']}px;")
    font = tokens.get("font", {})
    for fk in ("display", "mono", "sans"):
        if fk in font:
            lines.append(f"  --font-{fk}:{font[fk]};")
    lines.append("}")
    return "\n".join(lines)


def _lum(hex6: str) -> float:
    h = hex6.strip().lstrip("#")
    if len(h) != 6:
        return 0.0
    try:
        r, g, b = (int(h[i : i + 2], 16) / 255.0 for i in (0, 2, 4))
    except ValueError:
        return 0.0

    def _c(c: float) -> float:
        return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4

    return 0.2126 * _c(r) + 0.7152 * _c(g) + 0.0722 * _c(b)


def _contrast(a: str, b: str) -> float:
    la, lb = _lum(a), _lum(b)
    hi, lo = max(la, lb), min(la, lb)
    return (hi + 0.05) / (lo + 0.05)


def main() -> int:
    with open(TOKENS, encoding="utf-8") as f:
        tokens = json.load(f)
    themes = tokens["themes"]
    for name in ("paper", "ink", "moss"):
        missing = [k for k in ORDER if k not in themes[name]]
        if missing:
            print(f"FAIL: тема {name}: нет ключей {missing}")
            return 1
        # Контраст muted/ink на bg — предупреждение, не fail (дизайн правит руками).
        cr = _contrast(themes[name].get("muted", "#888"), themes[name].get("bg", "#000"))
        if cr < 4.5:
            print(f"WARN: тема {name}: muted/bg контраст {cr:.2f} < 4.5 (мелкий текст)")
    css = (
        "/* GENERATED from design/tokens.json by tools/build_tokens.py — не править руками. */\n"
        + _block(":root", themes["paper"], tokens)
        + "\n"
        + _block('html[data-theme="paper"]', themes["paper"], tokens)
        + "\n"
        + _block('html[data-theme="ink"]', themes["ink"], tokens)
        + "\n"
        + _block('html[data-theme="moss"]', themes["moss"], tokens)
        + "\n"
    )
    with open(OUT, "w", encoding="utf-8") as f:
        f.write(css)
    print(f"ok: {OUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
