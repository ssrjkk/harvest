"""PNG-карточка статуса (P4, п.9 ТЗ этапа 2): Pillow, палитра сайта.

Цвета — design/tokens.json (тема Ink), фолбэк — константы MASTER MAP.
Сеть, цикл, сетка iwoer, health. Шрифты: DejaVu/arial по путям, иначе
встроенный Pillow (Docker ставит fonts-dejavu-core, см. deploy/Dockerfile).
"""

from __future__ import annotations

import io
import json
import logging
import os

logger = logging.getLogger(__name__)

# MASTER MAP §5 (фолбэк, если tokens.json недоступен).
_FALLBACK = {
    "bg": "#14110A", "bg2": "#1E1810", "ink": "#F0E8D0", "muted": "#8A8068",
    "gold": "#D4A72C", "ok": "#5FAD56", "warn": "#E07B39", "err": "#D8503F",
    "paper": "#F0E8D0", "line": "#2C2618",
}

_FONT_PATHS = (
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf",
    "C:/Windows/Fonts/arial.ttf",
    "C:/Windows/Fonts/consola.ttf",
)

try:
    from PIL import Image, ImageDraw, ImageFont

    _PIL_OK = True
except Exception:  # noqa: BLE001
    _PIL_OK = False


def available() -> bool:
    return _PIL_OK


def _palette() -> dict[str, str]:
    try:
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(os.path.join(root, "design", "tokens.json"), encoding="utf-8") as f:
            ink = json.load(f)["themes"]["ink"]
        return {k: str(ink.get(k) or _FALLBACK[k]) for k in _FALLBACK}
    except Exception:  # noqa: BLE001
        return dict(_FALLBACK)


def _font(size: int):
    for path in _FONT_PATHS:
        if not os.path.exists(path):
            continue
        try:
            return ImageFont.truetype(path, size)
        except Exception:  # noqa: BLE001
            logger.debug("шрифт %s не подошёл", path)
    return ImageFont.load_default(size=size)


def _safe_int(value) -> int:
    """Число из снапшота PnL. Битое значение — ноль, а не исключение.

    Снапшот лежит в БД и может быть повреждён: карточка должна рисоваться
    всегда, а не падать из-за одного испорченного поля.
    """
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def render_card(state: dict, cells: str = "", total: int = 0) -> bytes:
    """PNG-карточка 760×~560: заголовок, сеть, метрики, сетка, health."""
    pal = _palette()
    running = bool(state.get("running"))
    paused = bool(state.get("paused"))
    mark = "[PAUSE]" if running and paused else ("[RUN]" if running else "[STOP]")
    mark_color = pal["warn"] if paused and running else (pal["ok"] if running else pal["err"])

    shown = (cells or "")[:100]
    # PnL рисуется двумя строками — сетку сдвигаем ниже, чтобы не наезжала.
    pnl = state.get("pnl")
    has_pnl = isinstance(pnl, dict) and bool(pnl)
    cols, cell, pad, top = 10, 34, 28, (306 if has_pnl else 250)
    rows_n = max(1, (len(shown) + cols - 1) // cols)
    width = pad * 2 + cols * cell
    height = top + rows_n * cell + 96

    img = Image.new("RGB", (width, height), pal["bg"])
    draw = ImageDraw.Draw(img)
    f_title = _font(30)
    f_big = _font(24)
    f_med = _font(20)

    draw.text((pad, 18), "harvest // ПОЛЕ", font=f_title, fill=pal["gold"])
    draw.text((pad, 58), f"{mark}  {state.get('network', '—')} · {state.get('chain_id', '—')}",
              font=f_big, fill=mark_color)
    draw.line([(pad, 100), (width - pad, 100)], fill=pal["line"], width=2)

    draw.text((pad, 114), f"кошельки  {state.get('wallet_count', '—')}", font=f_med, fill=pal["ink"])
    draw.text((pad, 142), f"цикл {state.get('cycle', '—')} / ∞", font=f_med, fill=pal["ink"])
    draw.text((pad, 170), f"ошибки  {state.get('errors', 0)}", font=f_med, fill=pal["ink"])
    try:
        health = float(state.get("health_factor", 0.0) or 0.0)
    except (TypeError, ValueError):
        health = 0.0
    draw.text((pad, 198), f"health {health:.2f}", font=f_med, fill=pal["ink"])

    # PnL последнего прогона: успешных действий и дельта фарма.
    if has_pnl:
        ok = _safe_int(pnl.get("actions_ok"))
        fail = _safe_int(pnl.get("actions_fail"))
        try:
            farm = float(pnl.get("farm_delta"))
        except (TypeError, ValueError):
            farm = None
        draw.text((pad, 226), f"PnL  успешных {ok} · отказов {fail}", font=f_med, fill=pal["ink"])
        if farm is not None:
            col = pal["ok"] if farm > 0 else (pal["err"] if farm < 0 else pal["muted"])
            draw.text((pad, 254), f"Δ фарма  {farm:+.6f}", font=f_med, fill=col)

    glyphs = {"i": ("·", pal["muted"]), "w": ("■", pal["gold"]), "o": ("▣", pal["ok"]),
              "e": ("x", pal["err"]), "r": ("~", pal["warn"])}
    f_cell = _font(22)
    for i, c in enumerate(shown):
        ch, color = glyphs.get(c, glyphs["i"])
        x = pad + (i % cols) * cell
        y = top + (i // cols) * cell
        draw.rectangle([x + 2, y + 2, x + cell - 2, y + cell - 2],
                       outline=pal["line"], width=1)
        draw.text((x + 9, y + 5), ch, font=f_cell, fill=color)
    rest = max(0, (total or 0) - len(shown))
    foot_y = top + rows_n * cell + 12
    if rest:
        draw.text((pad, foot_y), f"+{rest}", font=f_med, fill=pal["muted"])
        foot_y += 28
    draw.text((pad, foot_y), f"обновлено {state.get('updated', '')}",
              font=f_med, fill=pal["muted"])

    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()
