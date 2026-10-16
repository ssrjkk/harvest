"""Общая сетка состояния кошельков (этап 2, п.2 ТЗ).

Один источник для сайта и бота: формат строки — символы "iwoer"
(idle/working/ok/err/rest). publish_dash.py и бот используют эти функции.

Глифы для показа: · idle, ■ working, ▣ ok, x err, ~ rest.
Сетка: по 10 в ряд, макс. 100 клеток + «+N».
"""

from __future__ import annotations

GLYPHS = {"i": "·", "w": "■", "o": "▣", "e": "x", "r": "~"}

CELLS_PER_ROW = 10
CELLS_MAX = 100


def cell_state(actions: int, attempts: int, last_cycle: int, latest: int | None) -> str:
    """Один символ состояния: i idle, w working, o ok, e err, r rest."""
    if attempts > actions:
        return "e"
    if latest is not None and last_cycle == latest:
        return "o" if actions > 0 else "w"
    if last_cycle >= 0:
        return "r"
    return "i"


def build_cells(rows: list[tuple[int, int, int | None]], latest: int | None) -> str:
    """Строка iwoer из (actions, attempts, last_cycle)."""
    return "".join(
        cell_state(a or 0, t or 0, -1 if c is None else c, latest) for a, t, c in rows
    )


def render_grid(cells: str, total: int = 0) -> str:
    """Глифы сетки 10 в ряд, макс. 100 + «+N» (п.2 ТЗ)."""
    shown = (cells or "")[:CELLS_MAX]
    glyphs = "".join(GLYPHS.get(c, "·") for c in shown)
    rows = [glyphs[i : i + CELLS_PER_ROW] for i in range(0, len(glyphs), CELLS_PER_ROW)]
    # Пустая ферма: один ряд idle вместо пустой строки.
    if not rows:
        rows = ["·" * CELLS_PER_ROW]
    else:
        rows = [r.ljust(CELLS_PER_ROW, "·") for r in rows]
    text = "\n".join(" ".join(ch for ch in row) for row in rows)
    rest = max(0, (total or 0) - len(shown))
    if rest:
        text += f"\n+N: +{rest}"
    return text
