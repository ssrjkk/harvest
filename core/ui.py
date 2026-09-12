"""Современный CLI-интерфейс: баннеры, панели, спиннеры, live-таблицы.

Использует rich, если он установлен и вывод — терминал (TTY). Иначе молча
переключается на plain-рендер (print без анимаций), чтобы пайпы, CI и серверы
без rich работали без поломок.

Детали и анимации работают только в TTY и тихо отключаются при пайпе,
поэтому интерфейс безопасен для любого окружения.
"""

import logging
import os
import sys
import time
from collections.abc import Callable
from typing import Any

logger = logging.getLogger(__name__)

_TTY = bool(getattr(sys.stdout, "isatty", lambda: False)()) and not os.environ.get("NO_COLOR")


def _wide_ok() -> bool:
    """Поддерживает ли stdout Unicode-глифы (консоль или UTF-8)."""
    if _TTY:
        return True
    return (getattr(sys.stdout, "encoding", "") or "").lower() in (
        "utf-8",
        "utf8",
        "utf_8",
        "cp65001",
    )


_WIDE = _wide_ok()

# Глифы -> ASCII-замена для кодировок без Unicode (например cp1251 при пайпе)
_ASCII_MAP = {
    "\u2588": "=",
    "\u2591": "-",  # █ ░
    "\u25cf": "o",
    "\u25cb": "o",  # ● ○
    "\u2713": "+",
    "\u2717": "x",  # ✓ ✗
    "\u26a0": "!",
    "\u2139": "i",  # ⚠ ℹ
    "\u2500": "-",
    "\u2550": "=",  # ─ ═
    "\u00b7": ".",  # ·
}


def _safe(text: str) -> str:
    if _WIDE:
        return text
    for k, v in _ASCII_MAP.items():
        text = text.replace(k, v)
    return text


try:
    from rich.console import Console
    from rich.live import Live
    from rich.markup import escape as markup_escape
    from rich.panel import Panel
    from rich.table import Table
    from rich.text import Text

    _HAS_RICH = True
except ImportError:
    _HAS_RICH = False

# Чарактеры анимаций (не-emoji, безопасны в терминале)
SPINNER_FRAMES = ["|", "/", "-", "\\"]
if _TTY and _WIDE:
    b = "\u280b\u2819\u2839\u2838\u283c\u2834\u2826\u2827\u280f\u280e"  # Braille
    try:
        frames = list(b)
        if len(frames) == 10:
            SPINNER_FRAMES = frames
    except Exception:
        pass
DOT_OK = "\u25cf"  # ● (через _safe)
BAR_DONE = "\u2588" if _WIDE else "="
BAR_LEFT = "\u2591" if _WIDE else "-"
MARK_OK = "\u2713" if _WIDE else "+"
MARK_ERR = "\u2717" if _WIDE else "x"
MARK_WARN = "\u26a0" if _WIDE else "!"
MARK_INFO = "\u2139" if _WIDE else "i"

# Градиент баннера (256-цветная палитра: циан -> зелёный -> жёлтый)
_GRADIENT_256 = [51, 45, 39, 75, 87, 117, 120, 157, 222, 229]
_GRADIENT_RICH = [
    "dark_cyan",
    "cyan",
    "medium_turquoise",
    "spring_green1",
    "green_yellow",
    "yellow",
    "bright_yellow",
]


def available() -> bool:
    return _TTY and _HAS_RICH


def _is_tty() -> bool:
    return _TTY


def asyncio_sleep(seconds: float) -> Any:
    import asyncio

    return asyncio.sleep(seconds)


# ---------- plain-цвета ----------

_ANSI = {
    "RED": "\x1b[31m",
    "GREEN": "\x1b[32m",
    "YELLOW": "\x1b[33m",
    "CYAN": "\x1b[36m",
    "MAGENTA": "\x1b[35m",
    "WHITE": "\x1b[37m",
    "BOLD": "\x1b[1m",
    "DIM": "\x1b[2m",
    "RESET": "\x1b[0m",
}


def _ansi(style: str | None, text: str) -> str:
    if not style:
        return text
    codes = ""
    for part in style.split():
        up = part.upper().strip("[]")
        for c in _ANSI:
            if c == up or c.lower() in part.lower():
                codes += _ANSI[c]
                break
    return f"{codes}{text}{_ANSI['RESET']}"


def _gradient_plain(text: str, ramp: list[int] = _GRADIENT_256) -> str:
    if not _TTY:
        return text
    if not text:
        return text
    n = len(ramp)
    out = []
    for i, ch in enumerate(text):
        c = ramp[int(i * n / max(len(text), 1)) % n]
        out.append(f"\x1b[38;5;{c}m{ch}")
    return "".join(out) + _ANSI["RESET"]


class UI:
    """Лёгкая фасада интерфейса. Всё работает и в plain-режиме."""

    def __init__(self, use_rich: bool | None = None) -> None:
        self.use_rich = available() if use_rich is None else (use_rich and _HAS_RICH)
        self.console = Console() if self.use_rich else None
        self._spin = 0

    # ---------- базовый вывод ----------

    def print(self, text: str = "", style: str | None = None, end: str = "\n") -> None:
        if self.use_rich:
            assert self.console is not None
            # Текст печати может содержать фрагменты из сообщений исключений
            # (web3/aiohttp и др.) — экранируем Rich-разметку, чтобы случайные
            # "[...]" не превращались в стили/визуальный спам.
            self.console.print(markup_escape(text), style=style, end=end)
        else:
            sys.stdout.write(_ansi(style, text) if style else text)
            sys.stdout.write(end)

    def out_ok(self, text: str) -> None:
        self.print(f"[ {text} ]", style="BOLD GREEN" if self.use_rich else "GREEN")

    def hint(self, text: str) -> None:
        """Приглушённая подсказка-деталь."""
        self.print(text, style="dim" if self.use_rich else "DIM WHITE")

    def toast(self, message: str, kind: str = "info") -> None:
        """Мгновенное уведомление (деталь интерфейса)."""
        style, mark = {
            "ok": ("bold green", MARK_OK),
            "err": ("bold red", MARK_ERR),
            "warn": ("bold yellow", MARK_WARN),
        }.get(kind, ("bold cyan", MARK_INFO))
        if self.use_rich:
            assert self.console is not None
            self.console.print(f"{mark} {markup_escape(message)}", style=style)
        else:
            sys.stdout.write(_ansi(style, f"{mark} ") + message + "\n")

    def menu_panel(self, title: str, body: str, width: int = 56) -> None:
        """Рамка главного меню: rich-Panel или простая рамка."""
        if self.use_rich:
            assert self.console is not None
            self.console.print(Panel(body, title=title, border_style="bright_blue", width=width))
        else:
            rule = "\u00b7" * width
            print(rule)
            print(f"  {title}")
            print(rule)
            for line in body.splitlines():
                print(line)
            print(rule)

    # ---------- панели ----------

    def panel(self, title: str, body: str, color: str = "cyan", width: int = 70) -> None:
        if self.use_rich:
            assert self.console is not None
            self.console.print(Panel(markup_escape(body), title=title, border_style=color, width=width))
        else:
            rule = "=" * width
            print(rule)
            print(f"  {title}")
            print(rule)
            for line in body.splitlines():
                print(f"  {line}")
            print(rule)

    # ---------- градиент и анимации ----------

    def frame(self) -> str:
        """Следующий кадр спиннера."""
        self._spin = (self._spin + 1) % len(SPINNER_FRAMES)
        f = SPINNER_FRAMES[self._spin]
        return _safe(f) if not _WIDE else f

    def gradient(self, text: str, ramp_rich: list[str] | None = None) -> Any:
        """Градиентный цвет текста. Возвращает rich Text или plain-строку."""
        if self.use_rich:
            t = Text()
            for i, ch in enumerate(text):
                color = (ramp_rich or _GRADIENT_RICH)[
                    int(i * len(ramp_rich or _GRADIENT_RICH) / max(len(text), 1)) % len(ramp_rich or _GRADIENT_RICH)
                ]
                t.append(ch, style=color)
            return t
        return _gradient_plain(text)

    def dot(self, latency_ms: float | None) -> str:
        """Цветной индикатор задержки сети (ANSI, работает в rich и plain)."""
        if latency_ms is None or latency_ms < 0:
            style = "RED"
        elif latency_ms <= 400:
            style = "GREEN"
        elif latency_ms <= 1200:
            style = "YELLOW"
        else:
            style = "RED"
        return _ansi(f"BOLD {style}", _safe(DOT_OK))

    def bar(self, progress: float, width: int = 22) -> str:
        """Сглаженная полоса прогресса (TTY: символы-блоки, иначе ASCII)."""
        p = max(0.0, min(1.0, progress))
        done = int(p * width)
        return BAR_DONE * done + BAR_LEFT * max(0, width - done) + f" {p * 100:5.1f}%"

    def divider(self, char: str = "\u2500", width: int = 40) -> None:
        self.print(_safe(char * width), style="dim" if self.use_rich else "DIM")

    # ---------- баннер ----------

    def banner(self, title: str, subtitle: str = "") -> None:
        """Градиентный титул баннера + подзаголовок."""
        if self.use_rich:
            art = self.gradient(title)
            lines = subtitle.splitlines()
            frame = Panel(
                Text("\n".join(lines)) if lines else Text(""),
                title=art,
                title_align="left",
                border_style="bright_blue",
                width=64,
                padding=(1, 2),
            )
            assert self.console is not None
            self.console.print(frame)
        else:
            rule = _TTY and "\u2550" * 60 or "=" * 60
            print(rule)
            print(_gradient_plain(f" {title}"))
            for line in subtitle.splitlines():
                print(f" {line}")
            print(rule)

    def typewrite(self, text: str, speed: float = 0.0015) -> None:
        """Плавный вывод строки (эффект печати). В plain-режиме просто print."""
        if _TTY:
            buf = ""
            for ch in text:
                buf += ch
                if len(buf) % 8 == 0:
                    sys.stdout.write(buf)
                    sys.stdout.flush()
                    buf = ""
                    time.sleep(speed)
                else:
                    time.sleep(speed * 0.5)
            if buf:
                sys.stdout.write(buf)
            sys.stdout.write("\n")
        else:
            print(text)

    # ---------- спиннер ----------

    async def spinner(self, label: str, coro: Any) -> Any:
        """Крутит спиннер над корутиной."""
        if self.use_rich:
            assert self.console is not None
            import rich.status

            with rich.status.Status(f"[cyan]{label}[/cyan]", console=self.console):
                return await coro
        if _TTY:
            import asyncio

            task = asyncio.ensure_future(coro)
            t0 = time.monotonic()
            while not task.done():
                sys.stdout.write(f"\r\x1b[K{self.frame()} {label} ... {time.monotonic() - t0:.0f}s")
                sys.stdout.flush()
                try:
                    await asyncio.wait_for(asyncio.shield(task), timeout=0.15)
                except TimeoutError:
                    pass
            sys.stdout.write("\r\x1b[K")
            sys.stdout.flush()
            return task.result()
        sys.stdout.write(f"{label}... ")
        sys.stdout.flush()
        t0 = time.monotonic()
        try:
            return await coro
        finally:
            print(f"{time.monotonic() - t0:.1f}s")

    # ---------- таблицы ----------

    def table(self, title: str, headers: list[str], rows: list[list[Any]]) -> None:
        """Одноразовая таблица (для отчётов/скриншотов)."""
        if self.use_rich:
            assert self.console is not None
            t = Table(title=title)
            for h in headers:
                t.add_column(str(h))
            for r in rows:
                t.add_row(*[str(x) for x in r])
            self.console.print(t)
        else:
            print(f"\n--- {title} ---")
            print(" | ".join(str(h) for h in headers))
            print("-" * 60)
            for r in rows:
                print(" | ".join(str(x) for x in r))

    async def live_table(
        self,
        title: str,
        render: Callable[[], list[list[str]]],
        headers: list[str],
        period: float = 3.0,
        stop: Callable[[], bool] = lambda: True,
        max_lines: int = 40,
        caption: Callable[[], str] | str | None = None,
    ) -> None:
        """Живая обновляющаяся таблица.

        render() возвращает строки (списки str). Если доступен rich — peak Live,
        иначе каждые period сек печатаем обновлённый отчёт с очисткой экрана.
        caption может быть callable, который строит строку заголовка-статуса
        (кол-во действий, задержка сети и т.п.) и рисуется над таблицей.
        """
        if self.use_rich:
            from rich.table import Table as _T

            with Live(_T(title=title), console=self.console, refresh_per_second=2, screen=True) as live:
                while not stop():
                    rows = render()
                    t = _T(title=title)
                    for h in headers:
                        t.add_column(h, overflow="fold")
                    for r in rows[:max_lines]:
                        t.add_row(*[str(x) for x in r])
                    cap = caption() if callable(caption) else caption
                    if cap:
                        t.caption = cap
                    live.update(t)
                    await asyncio_sleep(period)
        else:
            while not stop():
                rows = render()
                if _TTY:
                    sys.stdout.write("\x1b[H\x1b[2J")
                else:
                    sys.stdout.write("\n")
                sys.stdout.write(f"{'=' * 30} {title} {'=' * 30}\n")
                cap = caption() if callable(caption) else caption
                if cap:
                    sys.stdout.write(cap + "\n")
                sys.stdout.write(" | ".join(headers) + "\n")
                sys.stdout.write("-" * 60 + "\n")
                for r in rows[:max_lines]:
                    sys.stdout.write(" | ".join(str(x) for x in r) + "\n")
                sys.stdout.write("-" * 60 + "\n")
                sys.stdout.write("(Ctrl+C — выход)\n")
                sys.stdout.flush()
                await asyncio_sleep(period)

    # ---------- выбор в меню ----------

    def menu_key(self, label: str, key: str, style: str = "green") -> str:
        k = f"[{key}]"
        if self.use_rich:
            return f"  [bold {style}]{k}[/bold {style}] {label}"
        return f"  ({key})  {label}"

    def poll_keys(self) -> str:
        """Безблокирующее чтение нажатых клавиш (для live-экранов).

        Возвращает строку нажатых клавиш (нижний регистр) или "".
        Работает через msvcrt на Windows и select+termios на POSIX.
        При пайпе/не-TTY возвращает "" и ничего не ждёт.
        """
        if not _TTY:
            return ""
        try:
            import msvcrt

            buf = ""
            while msvcrt.kbhit():
                ch = msvcrt.getch()
                if ch in (b"\x00", b"\xe0"):
                    if msvcrt.kbhit():
                        msvcrt.getch()
                    continue
                buf += ch.decode("ascii", "replace").lower()
            return buf
        except ImportError:
            pass
        try:
            import select

            if select.select([sys.stdin], [], [], 0)[0]:
                return sys.stdin.read(1).lower()
        except (ImportError, OSError):
            pass
        return ""
