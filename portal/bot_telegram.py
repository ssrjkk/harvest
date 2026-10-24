"""Telegram-бот harvest PORTAL: статистика, старт/стоп, пауза, ссылки,
кнопка Mini App, живой мониторинг с алертами. Long-polling (без открытых
портов — дружит с любым хостингом).

Стиль сообщений — ops-board сайта (раздел 05): секции `harvest // X`,
маркеры `[RUN]`/`[STOP]`, `[ok]`/`[warn]`, таблицы в <pre>, эмодзи — только статус home.
Форматирование живёт в portal/bot_ops.py (там же чистые функции
команд /cmd, /cfg, /check, /ref).

Mini App открывается через WebAppInfo; Telegram сам дописывает initData к URL,
портальный фронтенд шлёт его в /api/tg/init и получает сессию.

Живой мониторинг: /monitor (или кнопка) запускает фоновую задачу, которая
каждые monitor_interval секунд шлёт владельцу текущий статус фермы, а при
остановке фермы — немедленный алерт. Монитор привязан к чату, откуда его
включили, и живёт в рамках процесса бота.
"""

from __future__ import annotations

import asyncio
import html
import logging
import re
import time

from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    LinkPreviewOptions,
    Message,
    WebAppInfo,
)

from portal import bot_ops as _bot_ops
from portal.bot_ops import (
    _FOOT,
    _fmt_wallets_page,
    _progress_bar,
)
from portal.bot_ops import (
    check_config_text as _check_config_text,
)
from portal.bot_ops import (
    fmt_balances as _fmt_balances,
)
from portal.bot_ops import (
    fmt_doctor as _fmt_doctor,
)
from portal.bot_ops import (
    fmt_history as _fmt_history,
)
from portal.bot_ops import (
    fmt_networks as _fmt_networks,
)
from portal.bot_ops import (
    fmt_pnl as _fmt_pnl,
)
from portal.bot_ops import (
    fmt_stats as _fmt_stats,
)
from portal.bot_ops import (
    fmt_status as _fmt_status,
)
from portal.bot_ops import (
    parse_cmd_args as _bot_ops_parse_cmd,
)
from portal.bot_ops import (
    parse_count as _parse_count,
)
from portal.bot_ops import (
    parse_cycles as _parse_cycles,
)
from portal.config import PortalConfig, load_links
from portal.farm import FarmDaemon

# Реэкспорт ops-форматтеров: тесты и внешний код используют имена
# portal.bot_telegram (реализация — portal/bot_ops.py).
__all__ = [
    "_fmt_balances", "_fmt_doctor", "_fmt_history", "_fmt_networks", "_fmt_pnl",
    "_fmt_stats", "_fmt_status", "_fmt_wallets_page", "_progress_bar",
    "build_dispatcher", "run_bot", "notify_owner", "send_wallets_file",
    "LiveMonitor", "CmdWizard", "CfgWizard",
    "BOT_COMMANDS_RU", "setup_bot_commands",
    "ConflictTracker", "bot_conflict_status", "install_token_redaction",
]


class CmdWizard(StatesGroup):
    """Мастер команды: сеть → кошельки → циклы → кран."""

    net = State()
    wallets = State()
    cycles = State()
    faucet = State()


class CfgWizard(StatesGroup):
    """Мастер конфига: сеть → кошельки (YAML уходит файлом)."""

    net = State()
    wallets = State()


_WIZ_NETS = (("vibe", "VIBE/VIBE"), ("robinhood", "ROBINHOOD"), ("flop", "FLOP"),
             ("arc", "ARC"), ("simple", "SIMPLE"))


def _wiz_cancel_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="[ОТМЕНА]", callback_data="wiz:cancel")]]
    )


def _wiz_nets_kb(prefix: str) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(text=f"[{label}]", callback_data=f"{prefix}:net:{slug}")
         for slug, label in _WIZ_NETS[:2]],
        [InlineKeyboardButton(text=f"[{label}]", callback_data=f"{prefix}:net:{slug}")
         for slug, label in _WIZ_NETS[2:4]],
        [InlineKeyboardButton(text=f"[{_WIZ_NETS[4][1]}]", callback_data=f"{prefix}:net:{_WIZ_NETS[4][0]}")],
        [InlineKeyboardButton(text="[ОТМЕНА]", callback_data="wiz:cancel")],
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _wiz_num_kb(prefix: str, values: list[str]) -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(text=f"[{v}]", callback_data=f"{prefix}:{v}") for v in values]]
    rows.append([InlineKeyboardButton(text="[ОТМЕНА]", callback_data="wiz:cancel")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def _wiz_start_cmd(message: Message, state: FSMContext) -> None:
    await state.clear()
    await state.set_state(CmdWizard.net)
    await message.answer(
        "<b>harvest // МАСТЕР</b>\nШаг 1/4 — сеть:",
        parse_mode=ParseMode.HTML,
        reply_markup=_wiz_nets_kb("wiz:cmd"),
    )


async def _wiz_start_cfg(message: Message, state: FSMContext) -> None:
    await state.clear()
    await state.set_state(CfgWizard.net)
    await message.answer(
        "<b>harvest // МАСТЕР</b>\nШаг 1/2 — сеть для YAML:",
        parse_mode=ParseMode.HTML,
        reply_markup=_wiz_nets_kb("wiz:cfg"),
    )


async def _wiz_finish_cmd(message: Message, state: FSMContext, skip_faucet: bool) -> None:
    data = await state.get_data()
    try:
        cmd = _bot_ops.build_command(data["net"], data["wallets"], data["cycles"], skip_faucet)
        filename, content = _bot_ops.build_config_yaml(data["net"], data["wallets"])
    except ValueError as e:
        await state.clear()
        await message.answer(f"[err] {_esc(e)}")
        return
    await state.clear()
    await message.answer(
        f"<b>harvest // ГОТОВО</b>\n<code>{_esc(cmd)}</code>\n{_FOOT}",
        parse_mode=ParseMode.HTML,
    )
    from aiogram.types import BufferedInputFile

    await message.bot.send_document(
        chat_id=message.from_user.id,
        document=BufferedInputFile(content.encode("utf-8"), filename=filename),
        caption="Конфиг к команде выше.",
    )


async def _wiz_finish_cfg(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    try:
        filename, content = _bot_ops.build_config_yaml(data["net"], data["wallets"])
        cmd = _bot_ops.build_command(data["net"], data["wallets"], 5)
    except ValueError as e:
        await state.clear()
        await message.answer(f"[err] {_esc(e)}")
        return
    await state.clear()
    from aiogram.types import BufferedInputFile

    await message.bot.send_document(
        chat_id=message.from_user.id,
        document=BufferedInputFile(content.encode("utf-8"), filename=filename),
        caption=f"<code>{_esc(cmd)}</code>",
        parse_mode=ParseMode.HTML,
    )


async def _wiz_handle_text(message: Message, state: FSMContext) -> bool:
    """Свободный ввод мастера (числа) или /cancel. True = consumed."""
    try:
        cur = await state.get_state()
    except Exception:  # noqa: BLE001
        return False
    text = (message.text or "").strip()
    if text == "/cancel":
        await state.clear()
        await message.answer("[ОТМЕНА] Мастер закрыт.")
        return True
    if text.startswith("/"):
        # Команда вместо ответа — выходим из мастера и отдаём дальше.
        await state.clear()
        return False
    if cur == CmdWizard.wallets.state:
        n = _parse_count(text)
        if n is None:
            await message.answer("[err] Нужно число 1..5000. Или /cancel.")
            return True
        await state.update_data(wallets=n)
        await state.set_state(CmdWizard.cycles)
        await message.answer(
            "Шаг 3/4 — кругов (0 — бесконечно):",
            reply_markup=_wiz_num_kb("wiz:cmd:c", ["1", "2", "5", "0"]),
        )
        return True
    if cur == CmdWizard.cycles:
        n = _parse_cycles(text)
        if n is None:
            await message.answer("[err] Нужно число 0..100000. Или /cancel.")
            return True
        await state.update_data(cycles=n)
        await state.set_state(CmdWizard.faucet)
        await message.answer(
            "Шаг 4/4 — кран:",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text="[С КРАНОМ]", callback_data="wiz:cmd:f:1"),
                InlineKeyboardButton(text="[БЕЗ КРАНА]", callback_data="wiz:cmd:f:0"),
            ], [InlineKeyboardButton(text="[ОТМЕНА]", callback_data="wiz:cancel")]]),
        )
        return True
    if cur == CfgWizard.wallets:
        n = _parse_count(text)
        if n is None:
            await message.answer("[err] Нужно число 1..5000. Или /cancel.")
            return True
        await state.update_data(wallets=n)
        await _wiz_finish_cfg(message, state)
        return True
    return False

logger = logging.getLogger(__name__)

# Отключает превью ссылок (t.me/ssrjkk_bot) во всех сообщениях бота.
_NO_PREVIEW = LinkPreviewOptions(is_disabled=True)
_BOT_DEFAULTS = DefaultBotProperties(parse_mode=ParseMode.HTML, link_preview_is_disabled=True)

# Интервал пульса монитора (сек) и его можно переопределить env.
_DEFAULT_MONITOR_INTERVAL_S = 600.0

# Ссылка на автора — в стартовом сообщении, help и футерах bot_ops.


def _allowed(cfg: PortalConfig, tg_id: int | None) -> bool:
    # Default-deny: без TELEGRAM_ALLOW_IDS бот никому не отвечает (сознательное
    # исключение — PORTAL_OPEN_ACCESS). Раньше пустой список открывал бота ЛЮБОМУ.
    if not tg_id:
        return False
    return cfg.telegram_user_allowed(tg_id)


def _esc(value: object) -> str:
    """HTML-экранирование значений из daemon.statistics()/live_stats (defense-in-depth)."""
    return html.escape(str(value), quote=True)


# Антифлуд тяжёлых операций (генерация, листинг и экспорт кошельков):
# расшифровка всех сидов и файлы — дорогая работа, не даём дёргать подряд.
_FLOOD_INTERVAL_S = 30.0
# Экспорт — 1 в минуту на пользователя (п.6 ТЗ) + запись в аудит-лог.
# Ключи текстом — та же чувствительность (п.6 ТЗ).
# Лёгкие чтения — короче, чтобы /balance не душил RPC, а /status не тормозил UI.
_FLOOD_INTERVALS: dict[str, float] = {
    "export": 60.0,
    "keys": 60.0,
    "status": 5.0,
    "history": 5.0,
    "doctor": 5.0,
    "balance": 10.0,
    "faucet": 15.0,
    "rpc": 15.0,
    "scheduler": 10.0,
}
# Автоудаление сообщений с файлами экспорта (сек): ключи не лежат в истории.
_EXPORT_AUTODELETE_S = 60.0
_flood_last: dict[tuple[int, str], float] = {}


def _flood_interval(op: str) -> float:
    return _FLOOD_INTERVALS.get(op, _FLOOD_INTERVAL_S)


def _flood_ok(tg_id: int | None, op: str) -> tuple[bool, int]:
    """True + 0, если операцию можно выполнять, иначе False + секунды ожидания."""
    now = time.monotonic()
    key = (tg_id or 0, op)
    wait = int(_flood_interval(op) - (now - _flood_last.get(key, 0.0)))
    if wait > 0:
        return False, wait
    _flood_last[key] = now
    return True, 0


class _RedactingFilter(logging.Filter):
    """Редакция токена бота во всех логах (п.8 ТЗ): bot<id>:<secret> → bot<TOKEN>."""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            from portal.bot_render import redact_token
        except Exception:  # noqa: BLE001
            return True
        if record.args:
            try:
                record.args = tuple(
                    redact_token(a) if isinstance(a, str) else a for a in record.args
                )
            except Exception:  # noqa: BLE001
                pass
        try:
            record.msg = redact_token(record.msg)
        except Exception:  # noqa: BLE001
            pass
        # Трейсбэк форматируется позже (Formatter добавляет текст исключения
        # с URL api.telegram.org/bot<id>:<secret>). Предформатируем здесь
        # в record.exc_text — форматтер использует его вместо exc_info.
        # Сам объект исключения НЕ трогаем (мутация args исказила бы raise).
        try:
            if record.exc_info and record.exc_info[0] is not None and not record.exc_text:
                import traceback as _tb

                record.exc_text = redact_token("".join(_tb.format_exception(*record.exc_info)))
        except Exception:  # noqa: BLE001
            pass
        return True


def install_token_redaction() -> None:
    """Вешает редакцию токена на portal/aiogram/root-логгеры (идемпотентно)."""
    for name in ("portal", "portal.bot_telegram", "aiogram", "aiogram.dispatcher", ""):
        lg = logging.getLogger(name)
        if not any(isinstance(f, _RedactingFilter) for f in lg.filters):
            lg.addFilter(_RedactingFilter())


class ConflictTracker:
    """Конфликт 409 getUpdates (п.7 ТЗ): счётчик + backoff + одно уведомление.

    aiogram сам ретраит polling и не падает; этот трекер убирает спам лога:
    пишет один понятный ряд, экспоненциальный backoff отдаёт вызывающему,
    после _NOTIFY_AFTER подряд конфликтов — одно уведомление владельцу.
    """

    _NOTIFY_AFTER = 5
    _BASE_DELAY_S = 5.0
    _MAX_DELAY_S = 300.0

    def __init__(self) -> None:
        self.consecutive = 0
        self.notified = False
        self.total = 0

    def hit(self) -> float:
        """Фиксирует конфликт. Возвращает backoff-задержку (сек)."""
        self.consecutive += 1
        self.total += 1
        delay = min(self._MAX_DELAY_S, self._BASE_DELAY_S * (2 ** (self.consecutive - 1)))
        logger.warning(
            "Telegram 409: другой экземпляр опрашивает токен "
            "(подряд %d, backoff %.0fс)",
            self.consecutive,
            delay,
        )
        return delay

    def resolve(self) -> None:
        self.consecutive = 0
        self.notified = False

    @property
    def active(self) -> bool:
        return self.consecutive > 0

    def needs_notify(self) -> bool:
        if self.consecutive >= self._NOTIFY_AFTER and not self.notified:
            self.notified = True
            return True
        return False

    def status(self) -> str:
        return "CONFLICT" if self.active else "ok"


_conflict = ConflictTracker()


def bot_conflict_status() -> str:
    """Строка для /doctor: bot: CONFLICT|ok (п.7 ТЗ)."""
    return _conflict.status()


class _ConflictLogFilter(logging.Filter):
    """Гасит спам aiogram 'terminated by other getUpdates request'.

    Первый ряд пишет трекер (понятный текст + backoff), повторы подавляются
    до разрешения. Совместим с assertLogs в тестах (запись не удаляется —
    подавляется только дубль на уровне handler? нет: filter возвращает False,
    запись отбрасывается — для трекера считаем здесь же).
    """

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            msg = record.getMessage()
        except Exception:  # noqa: BLE001
            return True
        if "terminated by other getUpdates request" in msg:
            _conflict.hit()
            # Оставляем каждый 10-й ряд, чтобы лог не молчал полностью.
            return _conflict.total % 10 == 1
        return True


async def notify_owner(cfg: PortalConfig, text: str) -> None:
    """Пуш-алерт владельцу (Telegram). Без токена/разрешённых — тихий no-op.

    Рендер через bot_render (п.7 ТЗ), токен вычищен (п.8 ТЗ).
    """
    if not cfg.telegram_token or not cfg.telegram_allow_ids:
        return
    try:
        from portal.bot_render import render_alert
    except Exception:  # noqa: BLE001
        render_alert = None  # type: ignore[assignment]
    body = render_alert(text) if render_alert is not None else text
    bot = Bot(token=cfg.telegram_token, default=_BOT_DEFAULTS)
    async with bot:
        for tg_id in cfg.telegram_allow_ids:
            try:
                # Алерты слаем plain-text: там могут быть символы типа &, которые
                # HTML-парсер Telegram не примет.
                plain = body.replace("<b>", "").replace("</b>", "")
                await bot.send_message(chat_id=tg_id, text=plain, parse_mode=None)
            except Exception:  # noqa: BLE001
                logger.warning("Не удалось отправить алерт %s", tg_id, exc_info=True)


BOT_COMMANDS_RU: list[tuple[str, str]] = [
    ("start", "Стенд — главный экран"),
    ("menu", "Меню — все разделы"),
    ("status", "Живое поле стенда"),
    ("monitor", "Мониторинг с алертами"),
    ("doctor", "Диагностика чейна"),
    ("diag", "Диагностика подробно"),
    ("history", "История прогонов"),
    ("pnl", "Отчёт последнего прогона"),
    ("balance", "Балансы кошельков"),
    ("wallets", "Список кошельков"),
    ("profiles", "Персоны флота"),
    ("export", "Скачать кошельки (с подтверждением)"),
    ("network", "Переключение сети"),
    ("pause", "Пауза прогона"),
    ("resume", "Продолжить прогон"),
    ("stop", "Остановить прогон"),
    ("faucet", "Проверка крана"),
    ("rpc", "Здоровье RPC"),
    ("scheduler", "Планировщик"),
    ("alerts", "Мониторинг балансов"),
    ("cmd", "Конструктор команды"),
    ("cfg", "Конструктор YAML"),
    ("check", "Проверка YAML"),
    ("ref", "Справочник параметров"),
    ("cancel", "Закрыть мастер"),
    ("help", "Справка"),
]


async def setup_bot_commands(bot: Bot) -> None:
    """setMyCommands + описание (п.7 ТЗ). Best-effort, без падения старта."""
    try:
        from aiogram.types import BotCommand, BotCommandScopeAllPrivateChats

        from portal.bot_render import redact_token

        commands = [BotCommand(command=c, description=d) for c, d in BOT_COMMANDS_RU]
        try:
            await bot.set_my_commands(commands, scope=BotCommandScopeAllPrivateChats())
        except Exception:  # noqa: BLE001
            logger.warning("setMyCommands не удался", exc_info=True)
        for text, method in (
            ("harvest PORTAL — живой стенд: прогоны, конструкторы, экспорт.",
             "set_my_description"),
            ("Стенд: поле, конструкторы команд, безопасный экспорт.",
             "set_my_short_description"),
        ):
            try:
                await getattr(bot, method)(text)
            except Exception:  # noqa: BLE001
                logger.warning("%s не удался: %s", method, redact_token(text))
    except Exception:  # noqa: BLE001
        logger.warning("setup_bot_commands пропущен", exc_info=True)


def _refresh_home_kb(refresh_cb: str) -> InlineKeyboardMarkup:
    """Клавиатура вида «Обновить + Домой» для экранов статистики/статуса.

    Каждый экран с edit_text должен давать путь назад в menu:home,
    иначе пользователь застревает без навигации.
    """
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="[ОБНОВИТЬ]", callback_data=refresh_cb)],
        [InlineKeyboardButton(text="[ДОМОЙ]", callback_data="menu:home")],
    ])


def _wallets_kb(page: int, pages: int) -> InlineKeyboardMarkup:
    row = []
    if page > 1:
        row.append(InlineKeyboardButton(text="◀", callback_data=f"wallets:page:{page-1}"))
    if page < pages:
        row.append(InlineKeyboardButton(text="▶", callback_data=f"wallets:page:{page+1}"))
    rows = [row] if row else []
    rows.append([InlineKeyboardButton(text="[ДОМОЙ]", callback_data="menu:home")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _wallets_empty_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="[+ СОЗДАТЬ]", callback_data="wallets:create")],
            [InlineKeyboardButton(text="[ДОМОЙ]", callback_data="menu:home")],
        ]
    )


class LiveMonitor:
    """Живой мониторинг фермы: периодический статус + алерт об остановке.

    Фоновая задача шлёт владельцу статус каждые interval_s. Если ферма
    была запущена, а стала остановленной — немедленный алерт. Управление
    (start/stop) — из команд/кнопок бота.
    """

    def __init__(
        self,
        bot: Bot,
        tg_id: int,
        daemon: FarmDaemon,
        interval_s: float = _DEFAULT_MONITOR_INTERVAL_S,
    ) -> None:
        self.bot = bot
        self.tg_id = tg_id
        self.daemon = daemon
        self.interval_s = max(float(interval_s), 5.0)
        self._task: asyncio.Task | None = None
        self._stop = asyncio.Event()
        self._was_running: bool | None = None

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    async def start(self) -> None:
        if self.running:
            return
        self._stop = asyncio.Event()
        self._was_running = None
        self._task = asyncio.create_task(self._loop())
        logger.info("Живой мониторинг включён (tg_id=%s, интервал %.0f с)", self.tg_id, self.interval_s)

    async def stop(self) -> None:
        if self._task is None:
            return
        self._stop.set()
        task, self._task = self._task, None
        try:
            await task
        except asyncio.CancelledError:
            pass
        except Exception:  # noqa: BLE001
            logger.warning("Мониторинг: ошибка при остановке", exc_info=True)
        logger.info("Живой мониторинг выключен (tg_id=%s)", self.tg_id)

    async def _loop(self) -> None:
        try:
            while not self._stop.is_set():
                await self.tick()
                try:
                    await asyncio.wait_for(self._stop.wait(), timeout=self.interval_s)
                except TimeoutError:
                    pass
        finally:
            if self._task is not None:
                # stop() уже обнулил _task; здесь задача завершается сама.
                pass

    async def tick(self) -> str | None:
        """Один срез: шлёт статус/алерт. Возвращает текст (для тестов)."""
        data = await self.daemon.statistics()
        running = bool(data.get("running"))
        if self._was_running is None:
            self._was_running = running
        elif self._was_running and not running:
            self._was_running = False
            text = "[АЛЕРТ] Прогон остановился.\n/status — детали"
            await self._send(text)
            return text
        text = _fmt_status(data)
        await self._send(text)
        return text

    async def _send(self, text: str) -> None:
        try:
            await self.bot.send_message(
                chat_id=self.tg_id, text=text, parse_mode=ParseMode.HTML, link_preview_options=_NO_PREVIEW
            )
        except Exception:  # noqa: BLE001
            logger.warning("Мониторинг: не удалось отправить статус %s", self.tg_id, exc_info=True)


async def send_wallets_file(bot: Bot, tg_id: int, daemon: FarmDaemon, fmt: str = "csv") -> None:
    """Отправляет владельцу файл со всеми кошельками (CSV/JSON).

    protect_content запрещает пересылку/сохранение из чата, сообщение
    с файлом best-effort удаляется через _EXPORT_AUTODELETE_S секунд,
    чтобы ключи не лежали в истории.
    """
    from aiogram.types import BufferedInputFile

    content, filename = await daemon.export_wallets(fmt)
    sent = await bot.send_document(
        chat_id=tg_id,
        document=BufferedInputFile(content.encode("utf-8-sig"), filename=filename),
        caption=f"Кошельки ({fmt.upper()}) — {len(content):,} байт",
        protect_content=True,
    )
    asyncio.get_running_loop().create_task(
        _auto_delete_file(bot, tg_id, sent.message_id, _EXPORT_AUTODELETE_S)
    )


async def _auto_delete_file(bot: Bot, chat_id: int, message_id: int, delay_s: float) -> None:
    """Удаляет сообщение с файлом через delay_s (best-effort, без шума)."""
    try:
        await asyncio.sleep(max(0.0, delay_s))
        await bot.delete_message(chat_id=chat_id, message_id=message_id)
    except Exception:  # noqa: BLE001
        pass


_HOME_TEXT = (
    "Это <b>harvest PORTAL</b> — живой стенд.\n"
    "Прогоны, поле, конструкторы команд и конфигов — всё здесь.\n\n"
    + _FOOT
)


def _menu_kb(cfg: PortalConfig, monitor_on: bool = False) -> InlineKeyboardMarkup:
    rows = []
    # URL Mini App — из env (PORTAL_MINIAPP_URL), не из кода (п.8 ТЗ).
    miniapp_url = (getattr(cfg, "miniapp_url", "") or "").strip() or cfg.public_base_url
    if miniapp_url:
        rows.append([InlineKeyboardButton(text="[MINI APP]", web_app=WebAppInfo(url=miniapp_url))])
    rows.extend(
        [
            [
                InlineKeyboardButton(text="[СТАРТ]", callback_data="farm:start"),
                InlineKeyboardButton(text="[СТОП]", callback_data="farm:stop"),
            ],
            [
                InlineKeyboardButton(text="[ПАУЗА]", callback_data="farm:pause"),
                InlineKeyboardButton(text="[РЕЗЮМ]", callback_data="farm:resume"),
            ],
            [
                InlineKeyboardButton(text="[СТАТА]", callback_data="stats"),
                InlineKeyboardButton(text="[ПОЛЕ]", callback_data="status"),
                InlineKeyboardButton(text="[СПРАВКА]", callback_data="ref"),
            ],
            [
                InlineKeyboardButton(text="[ДОКТОР]", callback_data="doctor"),
                InlineKeyboardButton(text="[ИСТОРИЯ]", callback_data="history"),
            ],
            [
                InlineKeyboardButton(text="[ЭКСПОРТ CSV]", callback_data="export:csv"),
                InlineKeyboardButton(text="[ЭКСПОРТ JSON]", callback_data="export:json"),
            ],
            [
                InlineKeyboardButton(text="[КОШЕЛЬКИ]", callback_data="wallets:page:1"),
                InlineKeyboardButton(text="[CMD]", callback_data="cmd:run"),
                InlineKeyboardButton(text="[CFG]", callback_data="cfg:run"),
            ],
            [
                InlineKeyboardButton(
                    text="Мониторинг: ВКЛ" if monitor_on else "Мониторинг: ВЫКЛ",
                    callback_data="monitor:toggle",
                ),
                InlineKeyboardButton(text="[ССЫЛКИ]", callback_data="links"),
            ],
            [
                InlineKeyboardButton(text="[СЕТИ]", callback_data="network:list"),
            ],
        ]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)


_HELP_TEXT = (
    "<b>harvest PORTAL</b> — QA-стенд: гоняем чужой тестнет ботами по сценариям.\n\n"
    "Поле и диагностика:\n"
    "• /status — живой статус (успех %, средние, health)\n"
    "• /monitor — живой мониторинг с алертами\n"
    "• /doctor — диагностика чейна, /diag — подробно\n"
    "• /history — история прогонов\n"
    "• /pnl — отчёт последнего прогона\n"
    "• /balance — балансы кошельков\n"
    "• /faucet — проверка крана (pre-flight)\n"
    "• /rpc — здоровье RPC\n\n"
    "Управление:\n"
    "• /network — переключение тестнет-сети\n"
    "• /pause — пауза, /resume — продолжить\n"
    "• /stop — остановить прогон\n"
    "• /scheduler — планировщик (или /schedule)\n"
    "• /alerts — мониторинг балансов\n\n"
    "Конструкторы (как вкладки Про на сайте):\n"
    "• /cmd сеть [кошельки] [циклы] [nofaucet] — готовая команда\n"
    "• Кнопка [CMD] — мастер: сеть → кошельки → циклы → кран\n"
    "• /cfg сеть [кошельки] — готовый YAML файлом\n"
    "• Кнопка [CFG] — мастер: сеть → кошельки\n"
    "• /check + YAML следующими строками — проверка валидатором\n"
    "• YAML можно прислать файлом (.yaml) — тоже проверю\n"
    "• /cancel — закрыть мастер\n"
    "• /menu — все разделы\n"
    "• /ref — сжатый справочник параметров\n\n"
    "Кошельки:\n"
    "• /wallets — список (ключи под спойлером)\n"
    "• /profiles — персоны флота\n"
    "• /export [json] — скачать все кошельки (CSV)\n\n"
    "/start — главное меню\n/help — эта справка\n\n"
    + _FOOT
)


def _fmt_rpc(health: dict) -> str:
    """Компактный RPC-статус для /rpc (HTML, без секретов)."""
    import html as _h

    if not isinstance(health, dict) or not health:
        return "RPC: нет данных."
    lines = ["<b>harvest // RPC</b>"]
    # Ожидаемая форма daemon.rpc_health(): {nodes:[{url,ok,latency_ms,...}], ...}
    nodes = health.get("nodes") if isinstance(health.get("nodes"), list) else None
    if nodes is not None:
        if not nodes:
            lines.append("нод нет")
        for n in nodes[:10]:
            if not isinstance(n, dict):
                continue
            url = _h.escape(str(n.get("url") or n.get("rpc") or "?")[:60])
            ok = n.get("ok", n.get("healthy", "?"))
            lat = n.get("latency_ms", n.get("latency", n.get("ms", "?")))
            mark = "[ok]" if ok is True or str(ok).lower() in {"ok", "true", "1"} else "[err]" if ok is False else "[?]"
            lines.append(f"{mark} <code>{url}</code> {lat}ms")
        if len(nodes) > 10:
            lines.append(f"+{len(nodes) - 10}…")
        return "\n".join(lines)
    # Fallback: плоский dict.
    for k, v in list(health.items())[:12]:
        lines.append(f"{_h.escape(str(k))}: <code>{_h.escape(str(v)[:80])}</code>")
    return "\n".join(lines)


def _fmt_scheduler(state: dict) -> str:
    """Компактный планировщик для /scheduler (HTML)."""
    import html as _h

    if not isinstance(state, dict):
        return "Планировщик: нет данных."
    _raw_tasks = state.get("tasks")
    tasks: list = _raw_tasks if isinstance(_raw_tasks, list) else []
    run = "запущен" if state.get("running") else "остановлен"
    lines = [f"<b>harvest // PLAN</b> ({run}, задач: {len(tasks)})"]
    for t in tasks[:10]:
        if not isinstance(t, dict):
            continue
        tid = _h.escape(str(t.get("task_id") or t.get("id") or "?")[:40])
        name = _h.escape(str(t.get("name") or "")[:40])
        nxt = _h.escape(str(t.get("next_run") or "—")[:32])
        en = "on" if t.get("enabled", True) else "off"
        lines.append(f"• <code>{tid}</code> {name} [{en}] → {nxt}")
    if len(tasks) > 10:
        lines.append(f"+{len(tasks) - 10}…")
    if not tasks:
        lines.append("задач нет — портал → Расписание")
    return "\n".join(lines)


def _networks_kb(networks: list[dict]) -> InlineKeyboardMarkup:
    """Кнопки сетей: старт по слагу, стоп, обновить."""
    rows = []
    for n in networks or []:
        slug = str(n.get("slug", ""))
        label = ("[СТОП] " if n.get("running") else "[СТАРТ] ") + str(n.get("name") or slug)
        rows.append([InlineKeyboardButton(text=label[:64], callback_data=f"network:start:{slug}")])
    rows.append(
        [
            InlineKeyboardButton(text="[СТОП]", callback_data="network:stop"),
            InlineKeyboardButton(text="[ОБНОВИТЬ]", callback_data="network:list"),
            InlineKeyboardButton(text="[ДОМОЙ]", callback_data="menu:home"),
        ]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _networks_view(daemon: FarmDaemon) -> tuple[str, InlineKeyboardMarkup]:
    networks = daemon.networks_info()
    return _fmt_networks(networks), _networks_kb(networks)


# ---------------- v1: Поле, навигация, мастер, Live (этап 2, п.2-6 ТЗ) ----------------
# Новые колбэки вида v1:экран:действие[:арг] (см. portal/bot_render.py).
# Legacy-колбэки без версии обрабатываются старыми ветками ниже без изменений.

from portal import bot_render as _R  # noqa: E402

_MASTER_TTL_S = 15 * 60.0
_LIVE_EVERY_S = 5.0
_LIVE_TTL_S = 10 * 60.0
_live: dict[int, asyncio.Task] = {}


class MasterWiz(StatesGroup):
    """Новый мастер 3 шага (п.4 ТЗ): сеть → масштаб → профиль."""

    wallets = State()
    cycles = State()


class EncWiz(StatesGroup):
    """Пароль для шифрованного экспорта (п.6 ТЗ)."""

    password = State()


# Профили шага 3/3 → флаги auto.py: единый источник — bot_ops.PROFILE_FLAGS
# (флагов тихий/обычный/быстрый в коде нет; отображение интерпретирующее).
_PROFILE_LABEL = {"quiet": "тихий", "normal": "обычный", "fast": "быстрый"}

# Родитель для [← Назад] (Поле — для большинства, иначе указано явно).
_BACK: dict[str, str] = {
    "net": "v1:home:show", "scale": "v1:home:show", "wallets": "v1:home:show",
    "history": "v1:home:show", "builder": "v1:home:show", "ref": "v1:home:show",
    "diag": "v1:home:show", "danger": "v1:home:show", "run_stop": "v1:home:show",
    "expask": "v1:danger:show", "master": "v1:builder:show",
}


def _nav_kb(back_cb: str, lang: str = "ru") -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text=_R.tr("btn_back", lang), callback_data=back_cb),
        InlineKeyboardButton(text=_R.tr("btn_home", lang), callback_data="v1:home:show"),
    ]])


def _menu_screen_kb(lang: str = "ru", live_on: bool = False) -> InlineKeyboardMarkup:
    """Клавиатура экрана Меню: все вторичные разделы (те же колбэки, что и кнопки)."""
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=_R.tr("btn_wallets", lang), callback_data="v1:wallets:page:1"),
         InlineKeyboardButton(text=_R.tr("btn_profiles", lang), callback_data="v1:profiles:show"),
         InlineKeyboardButton(text=_R.tr("btn_history", lang), callback_data="v1:history:show")],
        [InlineKeyboardButton(text=_R.tr("btn_builder", lang), callback_data="v1:builder:show"),
         InlineKeyboardButton(text=_R.tr("btn_net", lang), callback_data="v1:net:show"),
         InlineKeyboardButton(text=_R.tr("btn_diag", lang), callback_data="v1:diag:show")],
        [InlineKeyboardButton(text=_R.tr("btn_scale", lang), callback_data="v1:scale:show"),
         InlineKeyboardButton(text=_R.tr("btn_ref", lang), callback_data="v1:ref:show"),
         InlineKeyboardButton(text=_R.tr("btn_danger", lang), callback_data="v1:danger:show")],
        [InlineKeyboardButton(text=_R.tr("btn_live_off" if live_on else "btn_live_on", lang),
                               callback_data="v1:home:live")],
        [InlineKeyboardButton(text=_R.tr("btn_back", lang), callback_data="v1:home:show"),
         InlineKeyboardButton(text=_R.tr("btn_home", lang), callback_data="v1:home:show")],
    ])


async def _safe_edit(message: Message, text: str, **kwargs) -> bool:
    """edit_text с молчаливым глотанием 'message is not modified' (п.2 ТЗ)."""
    try:
        await message.edit_text(text, **kwargs)
        return True
    except Exception as e:  # noqa: BLE001
        if "message is not modified" in str(e).lower():
            return False
        raise


def _daemon_db_path(daemon: FarmDaemon) -> str | None:
    path = getattr(daemon, "db_path", None)
    return path if isinstance(path, str) and path else None


async def _mode_lang(daemon: FarmDaemon, tg_id: int | None) -> tuple[str, str]:
    try:
        from portal import bot_settings as _bs

        return await _bs.get_settings(_daemon_db_path(daemon), tg_id or 0)
    except Exception:  # noqa: BLE001
        return ("novice", "ru")


async def _home_state(daemon: FarmDaemon) -> dict:
    """Состояние для render_home: статистика + сеть + клетки."""
    try:
        stats = await daemon.statistics()
    except Exception:  # noqa: BLE001
        stats = {"running": False}
    pool = stats.get("pool", {}) or {}
    slug = str(stats.get("network") or "")
    name, chain = slug or "—", ""
    try:
        infos = daemon.networks_info() if hasattr(daemon, "networks_info") else []
    except Exception:  # noqa: BLE001
        infos = []
    if not isinstance(infos, list):
        infos = []
    for n in infos:
        if (slug and n.get("slug") == slug) or (not slug and n.get("current")):
            name = str(n.get("name") or n.get("slug") or "—")
            chain = n.get("chain_id", "")
            slug = str(n.get("slug") or slug)
            break
    try:
        cells, total = await daemon.wallet_cells()
    except Exception:  # noqa: BLE001
        try:
            total = int(stats.get("wallet_count") or 0)
        except (TypeError, ValueError):
            total = 0
        cells = ("w" if stats.get("running") else "i") * min(total, 100)
    try:
        count = int(stats.get("wallet_count") or total or 0)
    except (TypeError, ValueError):
        count = total
    started = getattr(daemon, "_started_at", None)
    uptime_s = (time.monotonic() - started) if isinstance(started, (int, float)) else 0
    return {
        "running": bool(stats.get("running")), "paused": bool(pool.get("paused")),
        "network": name or "—", "chain_id": chain if chain != "" else "—",
        "wallet_count": count, "cycle": pool.get("cycles", "—"), "cycles_total": "∞",
        "errors": pool.get("errors", 0), "actions": pool.get("actions", 0),
        "uptime_s": uptime_s,
        "health_factor": stats.get("health_factor", 0.0),
        "workers": pool.get("dyn_workers", 0), "rpc_ok": "—", "gas_limit": "—",
        "pnl": stats.get("pnl"),
        "_cells": cells, "_total": total or count,
    }


def _home_kb(running: bool, paused: bool, lang: str = "ru", live_on: bool = False) -> InlineKeyboardMarkup:
    def _b(key: str, cbd: str) -> InlineKeyboardButton:
        return InlineKeyboardButton(text=_R.tr(key, lang), callback_data=cbd)

    run_btn = _b("btn_stop", "v1:run:stop") if running else _b("btn_start", "v1:run:start")
    pau_btn = _b("btn_resume", "v1:run:resume") if (running and paused) else _b("btn_pause", "v1:run:pause")
    return InlineKeyboardMarkup(inline_keyboard=[
        [run_btn, pau_btn],
        [InlineKeyboardButton(text="Открыть поле",
                              web_app=WebAppInfo(url="https://ssrjkk.github.io/harvest/"))],
        [_b("btn_refresh", "v1:home:refresh"), _b("btn_menu", "v1:menu:show")],
    ])


async def _render_home_tuple(
    daemon: FarmDaemon, tg_id: int | None
) -> tuple[str, InlineKeyboardMarkup, str]:
    """(текст, клавиатура, lang) домашнего экрана."""
    mode, lang = await _mode_lang(daemon, tg_id)
    st = await _home_state(daemon)
    text = _R.render_home(st, mode=mode, cells=st.pop("_cells"), cells_total=st.pop("_total"), lang=lang)
    kb = _home_kb(bool(st.get("running")), bool(st.get("paused")), lang,
                  live_on=bool(tg_id and _live.get(tg_id) is not None
                               and not _live[tg_id].done()))
    return text, kb, lang


async def _show_home_edit(call: CallbackQuery, daemon: FarmDaemon) -> None:
    text, kb, _lang = await _render_home_tuple(daemon, call.from_user.id if call.from_user else None)
    await _safe_edit(call.message, text, parse_mode=ParseMode.HTML,
                     reply_markup=kb, link_preview_options=_NO_PREVIEW)


async def _show_home_answer(message: Message, daemon: FarmDaemon) -> None:
    text, kb, _lang = await _render_home_tuple(
        daemon, message.from_user.id if message.from_user else None)
    await message.answer(text, parse_mode=ParseMode.HTML, reply_markup=kb)


def _master_expired(data: dict) -> bool:
    return (time.monotonic() - float(data.get("m_started") or 0.0)) > _MASTER_TTL_S


async def _master_nets_kb(lang: str = "ru") -> InlineKeyboardMarkup:
    from portal.networks import NETWORKS

    rows = []
    for i in range(0, len(NETWORKS), 2):
        rows.append([
            InlineKeyboardButton(text=f"[{n['name']}]", callback_data=f"v1:master:net:{n['slug']}")
            for n in NETWORKS[i : i + 2]
        ])
    rows.append([InlineKeyboardButton(
        text=_R.tr("btn_cancel", lang), callback_data="v1:master:cancel")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _master_num_kb(kind: str, values: list[str], lang: str = "ru") -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(text=f"[{v}]", callback_data=f"v1:master:{kind}:{v}")
             for v in values]]
    if kind == "w":
        rows[0].append(InlineKeyboardButton(text="[своё]", callback_data="v1:master:wcustom"))
    elif kind == "c":
        rows[0].append(InlineKeyboardButton(text="[своё]", callback_data="v1:master:ccustom"))
    rows.append([InlineKeyboardButton(
        text=_R.tr("btn_cancel", lang), callback_data="v1:master:cancel")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def _master_finish(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    try:
        cmd = _bot_ops.build_command(
            data["m_net"], data["m_wallets"], data["m_cycles"],
            not data.get("m_faucet", True))
    except (ValueError, KeyError) as e:
        await state.clear()
        await message.answer(f"[err] {_esc(e)}")
        return
    cmd = _bot_ops.apply_profile_flags(cmd, str(data.get("m_profile", "normal")))
    try:
        _fn, yaml_text = _bot_ops.build_config_yaml(data["m_net"], data["m_wallets"])
    except ValueError as e:
        await state.clear()
        await message.answer(f"[err] {_esc(e)}\n<code>{_esc(cmd)}</code>", parse_mode=ParseMode.HTML)
        return
    await state.update_data(m_cmd=cmd, m_yaml=yaml_text)
    await message.answer(
        _R.render_master_done(cmd),
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="[YAML файлом]", callback_data="v1:master:yaml"),
             InlineKeyboardButton(text="[Проверить]", callback_data="v1:master:verify")],
            [InlineKeyboardButton(text="[Начать заново]", callback_data="v1:master:restart"),
             InlineKeyboardButton(text="[Поле]", callback_data="v1:home:show")],
        ]),
    )


async def _master_handle_text(message: Message, state: FSMContext) -> bool:
    """Свободный ввод нового мастера (свои числа). True = consumed."""
    try:
        cur = await state.get_state()
    except Exception:  # noqa: BLE001
        return False
    text = (message.text or "").strip()
    if text == "/cancel":
        await state.clear()
        await message.answer("[ОТМЕНА] Мастер закрыт.")
        return True
    if text.startswith("/"):
        await state.clear()
        return False
    if cur == MasterWiz.wallets.state:
        n = _parse_count(text)
        if n is None:
            await message.answer("[err] Нужно число 1..5000. Или /cancel.")
            return True
        await state.update_data(m_wallets=n)
        await message.answer(
            "Шаг 2/3 — циклов (0 — бесконечно) или кнопкой:",
            reply_markup=_master_num_kb("c", ["2", "5", "∞"], "ru"),
        )
        return True
    if cur == MasterWiz.cycles.state:
        n = 0 if text == "∞" else _parse_cycles(text)
        if n is None:
            await message.answer("[err] Нужно число 0..100000 (0 — бесконечно). Или /cancel.")
            return True
        await state.update_data(m_cycles=n)
        await message.answer(
            "Шаг 3/3 — профиль:",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="[Тихий]", callback_data="v1:master:profile:quiet"),
                 InlineKeyboardButton(text="[Обычный]", callback_data="v1:master:profile:normal"),
                 InlineKeyboardButton(text="[Быстрый]", callback_data="v1:master:profile:fast")],
                [InlineKeyboardButton(text="[ОТМЕНА]", callback_data="v1:master:cancel")],
            ]),
        )
        return True
    return False


async def _enc_handle_text(message: Message, state: FSMContext, daemon: FarmDaemon) -> bool:
    """Пароль шифрованного экспорта. True = consumed."""
    try:
        cur = await state.get_state()
    except Exception:  # noqa: BLE001
        return False
    if cur != EncWiz.password.state:
        return False
    text = (message.text or "").strip()
    data = await state.get_data()
    if text == "/cancel":
        await state.clear()
        await message.answer("[ОТМЕНА] Экспорт отменён.")
        return True
    if text.startswith("/"):
        await state.clear()
        return False
    if not text:
        await message.answer("[err] Пустой пароль. Пришли пароль строкой или /cancel.")
        return True
    fmt = str(data.get("enc_fmt", "csv"))
    if fmt not in {"csv", "json"}:
        fmt = "csv"
    tg_id = message.from_user.id if message.from_user else 0
    try:
        await _send_encrypted(message.bot, tg_id, daemon, fmt, text)
        logger.info("[AUDIT] export user=%s fmt=enc-%s via=button", tg_id, fmt)
    except Exception:  # noqa: BLE001
        logger.warning("Шифрованный экспорт упал", exc_info=True)
        await message.answer("[err] Не удалось сформировать файл. Код: ENC-FAIL")
        await state.clear()
        return True
    try:
        await message.delete()
    except Exception:  # noqa: BLE001
        pass
    await state.clear()
    await message.answer("[ok] Зашифрованный файл выслан (удалится через 60 с). Удали сообщение с паролем.")
    return True


async def _send_encrypted(bot: Bot, tg_id: int, daemon: FarmDaemon, fmt: str, password: str) -> None:
    """Шифрованный экспорт (.enc, AES-256-CBC v2 первым вариантом, п.6 ТЗ).

    Пароль — только в памяти, в лог не пишется. Plaintext на диске живёт
    только внутри TemporaryDirectory.
    """
    import os
    import tempfile

    from aiogram.types import BufferedInputFile

    from core.exporter import encrypt_file

    content, filename = await daemon.export_wallets(fmt)
    with tempfile.TemporaryDirectory() as td:
        src = os.path.join(td, filename)
        with open(src, "w", encoding="utf-8-sig") as f:
            f.write(content)
        enc_path = encrypt_file(src, password)
        if not enc_path.endswith(".enc"):
            raise RuntimeError("шифрование не удалось")
        with open(enc_path, "rb") as f:
            data = f.read()
    enc_name = os.path.basename(enc_path)
    sent = await bot.send_document(
        chat_id=tg_id,
        document=BufferedInputFile(data, filename=enc_name),
        caption=f"Кошельки, шифр (.enc, {fmt.upper()}) — {len(data):,} байт",
        protect_content=True,
    )
    asyncio.get_running_loop().create_task(
        _auto_delete_file(bot, tg_id, sent.message_id, _EXPORT_AUTODELETE_S)
    )


async def _send_keys_temp(message: Message, text: str) -> None:
    """Ключи новым сообщением: protect + автоудаление 60с (п.6 ТЗ)."""
    sent = await message.answer(text, parse_mode=ParseMode.HTML, protect_content=True)
    try:
        chat_id = message.chat.id
    except AttributeError:
        return
    asyncio.get_running_loop().create_task(
        _auto_delete_file(message.bot, chat_id, sent.message_id, _EXPORT_AUTODELETE_S)
    )


async def _live_loop(
    chat_id: int,
    message: Message,
    daemon: FarmDaemon,
    every: float = _LIVE_EVERY_S,
    ttl: float = _LIVE_TTL_S,
) -> None:
    """Live 10 мин: правка поля каждые 5с, затем сам выключается (п.2 ТЗ)."""
    try:
        until = time.monotonic() + ttl
        while time.monotonic() < until:
            await asyncio.sleep(every)
            mode, lang = await _mode_lang(daemon, chat_id)
            st = await _home_state(daemon)
            text = _R.render_home(st, mode=mode, cells=st.pop("_cells"), cells_total=st.pop("_total"))
            kb = _home_kb(bool(st.get("running")), bool(st.get("paused")), lang, live_on=True)
            try:
                await _safe_edit(message, text, parse_mode=ParseMode.HTML,
                                 reply_markup=kb, link_preview_options=_NO_PREVIEW)
            except Exception:  # noqa: BLE001
                logger.warning("Live: правка не удалась, останавливаю", exc_info=True)
                break
    except asyncio.CancelledError:
        raise
    finally:
        if _live.get(chat_id) is asyncio.current_task():
            _live.pop(chat_id, None)


async def _live_toggle(call: CallbackQuery, daemon: FarmDaemon, lang: str = "ru") -> None:
    chat_id = call.from_user.id if call.from_user else 0
    task = _live.get(chat_id)
    if task is not None and not task.done():
        task.cancel()
        _live.pop(chat_id, None)
        try:
            await task
        except asyncio.CancelledError:
            pass
        await call.message.answer("Live выключен." if lang == "ru" else "Live off.")
    else:
        _live[chat_id] = asyncio.create_task(_live_loop(chat_id, call.message, daemon))
        await call.message.answer(
            "Live 10 мин включён: поле обновляется каждые 5 с."
            if lang == "ru" else "Live 10 min on: field refreshes every 5 s.")


def _danger_kb(mode: str, lang: str) -> InlineKeyboardMarkup:
    def _b(key: str, cbd: str) -> InlineKeyboardButton:
        return InlineKeyboardButton(text=_R.tr(key, lang), callback_data=cbd)

    return InlineKeyboardMarkup(inline_keyboard=[
        [_b("btn_csv", "v1:exp:ask:csv"), _b("btn_json", "v1:exp:ask:json")],
        [InlineKeyboardButton(text=_R.tr("btn_enc", lang) + " CSV", callback_data="v1:enc:ask:csv"),
         InlineKeyboardButton(text=_R.tr("btn_enc", lang) + " JSON", callback_data="v1:enc:ask:json")],
        [InlineKeyboardButton(
            text=f"[Режим: {_R.tr('mode_pro' if mode == 'pro' else 'mode_novice', lang)}]",
            callback_data="v1:danger:mode"),
         InlineKeyboardButton(text=f"[Язык: {lang.upper()}]", callback_data="v1:danger:lang")],
        [InlineKeyboardButton(text=_R.tr("btn_back", lang), callback_data="v1:home:show"),
         InlineKeyboardButton(text=_R.tr("btn_home", lang), callback_data="v1:home:show")],
    ])


async def _stale_home(call: CallbackQuery, daemon: FarmDaemon) -> None:
    """Устаревшая кнопка: «Экран устарел», сообщение → Поле (п.3 ТЗ)."""
    mode, lang = await _mode_lang(daemon, call.from_user.id if call.from_user else None)
    try:
        await call.answer(_R.tr("stale", lang))
    except Exception:  # noqa: BLE001
        pass
    await _show_home_edit(call, daemon)


async def _handle_v1(call: CallbackQuery, state: FSMContext | None, cfg: PortalConfig,
                     daemon: FarmDaemon) -> bool:
    """Ветка новых колбэков v1. True = обработано (включая stale)."""
    from aiogram.types import BufferedInputFile

    from portal import bot_settings as _bs

    data = call.data or ""
    uid = call.from_user.id if call.from_user else None
    if not data.startswith("v1:"):
        if re.match(r"^v\d+:", data):
            # Версия не текущая → устарело: ответ + перерисовка Поля (п.3 ТЗ).
            await _stale_home(call, daemon)
            return True
        return False  # legacy без версии — старые ветки
    parsed = _R.parse_cb(data)
    if parsed is None:
        await _stale_home(call, daemon)
        return True
    screen, action, arg = parsed
    db_path = _daemon_db_path(daemon)
    mode, lang = await _mode_lang(daemon, uid)

    async def _home() -> None:
        await _show_home_edit(call, daemon)

    async def _scale() -> None:
        count: int | str
        try:
            count = await daemon.wallet_count()
        except Exception:  # noqa: BLE001
            count = "—"
        await call.message.edit_text(
            _R.render_scale(count, lang), parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text=_R.tr("btn_add", lang, n=10),
                                      callback_data="v1:scale:add:10"),
                 InlineKeyboardButton(text=_R.tr("btn_add", lang, n=50),
                                      callback_data="v1:scale:add:50")],
                [InlineKeyboardButton(text=_R.tr("btn_back", lang),
                                      callback_data="v1:home:show"),
                 InlineKeyboardButton(text=_R.tr("btn_home", lang),
                                      callback_data="v1:home:show")],
            ]), link_preview_options=_NO_PREVIEW)

    if screen == "home" and action in {"show", "refresh"}:
        await _home()
        return True
    if screen == "home" and action == "live":
        await _live_toggle(call, daemon, lang)
        return True
    if screen == "run" and action == "start":
        ok = await daemon.start()
        await call.message.answer(f"[{'ok' if ok else 'err'}] start")
        await _home()
        return True
    if screen == "run" and action in {"pause", "resume"}:
        ok = await (daemon.resume() if action == "resume" else daemon.pause())
        await call.message.answer(f"[{'ok' if ok else 'err'}] {action}")
        await _home()
        return True
    if screen == "run" and action == "stop":
        st = await _home_state(daemon)
        await call.message.edit_text(
            _R.render_stop_confirm(st, lang), parse_mode=ParseMode.HTML,
            reply_markup=_nav_kb("v1:home:show", lang),
            link_preview_options=_NO_PREVIEW)
        return True
    if screen == "run" and action == "stop_yes":
        ok = await daemon.stop()
        await call.message.answer(f"[{'ok' if ok else 'err'}] stop")
        await _home()
        return True
    if screen == "profiles" and action == "show":
        try:
            wallets = await daemon.all_wallets()
        except Exception:  # noqa: BLE001
            wallets = []
        cfg_dict = getattr(daemon, "config", None) or {}
        beh = (cfg_dict.get("behavior") or {}) if isinstance(cfg_dict, dict) else {}
        summary = _bot_ops.profiles_summary(
            [w.get("address", "") for w in wallets if isinstance(w, dict)], beh)
        await call.message.edit_text(
            _R.render_profiles(summary, lang), parse_mode=ParseMode.HTML,
            reply_markup=_nav_kb("v1:home:show", lang), link_preview_options=_NO_PREVIEW)
        return True
    if screen == "menu" and action == "show":
        live_on = bool(uid and _live.get(uid) is not None and not _live[uid].done())
        await call.message.edit_text(
            "<b>harvest // МЕНЮ</b>" if lang == "ru" else "<b>harvest // MENU</b>",
            parse_mode=ParseMode.HTML,
            reply_markup=_menu_screen_kb(lang, live_on), link_preview_options=_NO_PREVIEW)
        return True
    if screen == "net" and action == "show":
        answer, kb = _networks_view(daemon)
        await call.message.edit_text(answer, parse_mode=ParseMode.HTML,
                                     reply_markup=kb, link_preview_options=_NO_PREVIEW)
        return True
    if screen == "scale" and action == "show":
        await _scale()
        return True
    if screen == "scale" and action == "add":
        try:
            n = max(1, min(int(arg or 10), 50))
        except (TypeError, ValueError):
            n = 10
        ok, wait = _flood_ok(uid, "create")
        if not ok:
            await call.message.answer(_R.tr("cooldown", lang, n=wait))
            return True
        try:
            created = await daemon.create_wallets(n)
            await call.message.answer(f"[ok] +{len(created)}")
        except RuntimeError:
            await call.message.answer("[err] БД фермы не подключена.")
        await _scale()
        return True
    if screen == "wallets" and action == "page":
        try:
            page = int(arg or 1)
        except (TypeError, ValueError):
            page = 1
        wallets = await daemon.all_wallets()
        text, pages = _R.render_wallets_list(wallets, page, 4, lang)
        rows: list[list[InlineKeyboardButton]] = []
        nav = []
        if page > 1:
            nav.append(InlineKeyboardButton(text="◀", callback_data=f"v1:wallets:page:{page-1}"))
        if page < pages:
            nav.append(InlineKeyboardButton(text="▶", callback_data=f"v1:wallets:page:{page+1}"))
        if nav:
            rows.append(nav)
        if wallets:
            rows.append([InlineKeyboardButton(text=_R.tr("btn_keys", lang),
                                              callback_data=f"v1:wallets:keysask:{page}")])
        else:
            rows.append([InlineKeyboardButton(text="[+ СОЗДАТЬ]", callback_data="wallets:create")])
        rows.append([InlineKeyboardButton(text=_R.tr("btn_back", lang),
                                          callback_data="v1:home:show"),
                     InlineKeyboardButton(text=_R.tr("btn_home", lang),
                                          callback_data="v1:home:show")])
        await call.message.edit_text(text, parse_mode=ParseMode.HTML,
                                     reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))
        return True
    if screen == "wallets" and action == "keysask":
        try:
            page = int(arg or 1)
        except (TypeError, ValueError):
            page = 1
        await call.message.edit_text(
            _R.render_keys_confirm(page, lang), parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text=_R.tr("btn_show", lang),
                                      callback_data=f"v1:wallets:keysyes:{page}")],
                [InlineKeyboardButton(text=_R.tr("btn_back", lang),
                                      callback_data=f"v1:wallets:page:{page}"),
                 InlineKeyboardButton(text=_R.tr("btn_home", lang),
                                      callback_data="v1:home:show")],
            ]), link_preview_options=_NO_PREVIEW)
        return True
    if screen == "wallets" and action == "keysyes":
        try:
            page = int(arg or 1)
        except (TypeError, ValueError):
            page = 1
        ok, wait = _flood_ok(uid, "keys")
        if not ok:
            await call.message.answer(_R.tr("cooldown", lang, n=wait))
            return True
        wallets = await daemon.all_wallets()
        content, _pages = _fmt_wallets_page(wallets, page)
        await _send_keys_temp(call.message, content)
        logger.info("[AUDIT] keys user=%s page=%s", uid, page)
        return True
    if screen == "history" and action == "show":
        answer = _fmt_history(await daemon.cycle_history())
        await call.message.edit_text(
            answer, parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text=_R.tr("btn_refresh", lang),
                                      callback_data="v1:history:show")],
                [InlineKeyboardButton(text=_R.tr("btn_back", lang),
                                      callback_data="v1:home:show"),
                 InlineKeyboardButton(text=_R.tr("btn_home", lang),
                                      callback_data="v1:home:show")],
            ]), link_preview_options=_NO_PREVIEW)
        return True
    if screen == "builder" and action == "show":
        await call.message.edit_text(
            _R.render_builder(lang), parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="CMD", callback_data="v1:master:cmd"),
                 InlineKeyboardButton(text="CFG", callback_data="v1:master:cfg")],
                [InlineKeyboardButton(text=_R.tr("btn_back", lang),
                                      callback_data="v1:home:show"),
                 InlineKeyboardButton(text=_R.tr("btn_home", lang),
                                      callback_data="v1:home:show")],
            ]), link_preview_options=_NO_PREVIEW)
        return True
    if screen == "ref" and action == "show":
        await call.message.edit_text(
            _bot_ops.REF_TEXT, parse_mode=ParseMode.HTML,
            reply_markup=_nav_kb("v1:home:show", lang), link_preview_options=_NO_PREVIEW)
        return True
    if screen == "diag" and action == "show":
        answer = _fmt_doctor(await daemon.statistics()) + f"\nbot: {bot_conflict_status()}"
        await call.message.edit_text(
            answer, parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text=_R.tr("btn_refresh", lang),
                                      callback_data="v1:diag:show")],
                [InlineKeyboardButton(text="[Карточка]", callback_data="v1:diag:card")],
                [InlineKeyboardButton(text=_R.tr("btn_back", lang),
                                      callback_data="v1:home:show"),
                 InlineKeyboardButton(text=_R.tr("btn_home", lang),
                                      callback_data="v1:home:show")],
            ]), link_preview_options=_NO_PREVIEW)
        return True
    if screen == "diag" and action == "card":
        from portal import status_card as _card

        if not _card.available():
            await call.message.answer("[err] Карточки недоступны: нет Pillow.")
            return True
        st = await _home_state(daemon)
        png = _card.render_card(st, cells=st.pop("_cells"), total=st.pop("_total"))
        await call.message.answer_photo(
            BufferedInputFile(png, filename="harvest-field.png"),
            caption=f"Поле · {st.get('network', '—')}",
        )
        logger.info("[AUDIT] card user=%s", uid)
        return True
    if screen == "danger" and action == "show":
        await call.message.edit_text(
            _R.render_danger(mode, lang), parse_mode=ParseMode.HTML,
            reply_markup=_danger_kb(mode, lang), link_preview_options=_NO_PREVIEW)
        return True
    if screen == "danger" and action == "mode":
        mode = await _bs.set_mode(db_path, uid or 0, "pro" if mode == "novice" else "novice")
        await call.message.edit_text(
            _R.render_danger(mode, lang), parse_mode=ParseMode.HTML,
            reply_markup=_danger_kb(mode, lang), link_preview_options=_NO_PREVIEW)
        return True
    if screen == "danger" and action == "lang":
        lang = await _bs.set_lang(db_path, uid or 0, "en" if lang == "ru" else "ru")
        await call.message.edit_text(
            _R.render_danger(mode, lang), parse_mode=ParseMode.HTML,
            reply_markup=_danger_kb(mode, lang), link_preview_options=_NO_PREVIEW)
        return True
    if screen == "exp" and action == "ask":
        fmt = arg if arg in {"csv", "json"} else "csv"
        await call.message.edit_text(
            _R.render_export_confirm(fmt, False, lang), parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text=_R.tr("btn_confirm", lang),
                                      callback_data=f"v1:exp:yes:{fmt}"),
                 InlineKeyboardButton(text=_R.tr("btn_enc", lang),
                                      callback_data=f"v1:enc:ask:{fmt}")],
                [InlineKeyboardButton(text=_R.tr("btn_back", lang),
                                      callback_data="v1:danger:show"),
                 InlineKeyboardButton(text=_R.tr("btn_home", lang),
                                      callback_data="v1:home:show")],
            ]), link_preview_options=_NO_PREVIEW)
        return True
    if screen == "exp" and action == "yes":
        fmt = arg if arg in {"csv", "json"} else "csv"
        ok, wait = _flood_ok(uid, "export")
        if not ok:
            await call.message.answer(_R.tr("cooldown", lang, n=wait))
            return True
        try:
            await call.message.answer("[...] Готовлю файл кошельков…")
            await send_wallets_file(call.message.bot, uid or 0, daemon, fmt)
            logger.info("[AUDIT] export user=%s fmt=%s via=v1", uid, fmt)
        except Exception:  # noqa: BLE001
            logger.warning("Экспорт кошельков (v1) упал", exc_info=True)
            await call.message.answer("[err] Не удалось сформировать файл. Код: EXP-FAIL")
        return True
    if screen == "enc" and action == "ask":
        fmt = arg if arg in {"csv", "json"} else "csv"
        if state is None:
            await call.message.answer(_R.render_enc_ask(lang), parse_mode=ParseMode.HTML)
            return True
        await state.clear()
        await state.update_data(enc_fmt=fmt)
        await state.set_state(EncWiz.password)
        await call.message.answer(
            _R.render_enc_ask(lang), parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text=_R.tr("btn_cancel", lang),
                                     callback_data="v1:enc:cancel")]]))
        return True
    if screen == "enc" and action == "cancel":
        if state is not None:
            try:
                await state.clear()
            except Exception:  # noqa: BLE001
                pass
        await call.message.edit_text(
            _R.render_danger(mode, lang), parse_mode=ParseMode.HTML,
            reply_markup=_danger_kb(mode, lang), link_preview_options=_NO_PREVIEW)
        return True
    if screen == "master" and action in {"cmd", "cfg"}:
        if state is None:
            await call.message.answer(_bot_ops.CMD_USAGE, parse_mode=ParseMode.HTML)
            return True
        await state.clear()
        await state.update_data(m_kind=action, m_started=time.monotonic())
        await call.message.answer(
            "<b>harvest // МАСТЕР</b>\nШаг 1/3 — сеть:",
            parse_mode=ParseMode.HTML,
            reply_markup=await _master_nets_kb(lang),
        )
        return True
    if screen == "master" and action == "net":
        slug = (arg or "").strip().lower()
        if slug == "vibe":
            slug = "vibevibe"
        from portal.networks import NETWORKS as _NETS

        if slug not in {n["slug"] for n in _NETS}:
            await call.message.answer("[err] Неизвестная сеть.")
            return True
        if state is None:
            return True
        if _master_expired(await state.get_data()):
            await state.clear()
            await call.message.answer(_R.tr("stale", lang))
            return True
        await state.update_data(m_net=slug)
        await call.message.answer(
            "Шаг 2/3 — кошельки (или «своё» числом 1..5000):",
            reply_markup=_master_num_kb("w", ["10", "50", "200"], lang),
        )
        return True
    if screen == "master" and action == "w":
        if state is None:
            return True
        try:
            n = int(arg)
        except (TypeError, ValueError):
            return True
        if not 1 <= n <= 5000:
            await call.message.answer("[err] Нужно число 1..5000.")
            return True
        if _master_expired(await state.get_data()):
            await state.clear()
            await call.message.answer(_R.tr("stale", lang))
            return True
        await state.update_data(m_wallets=n)
        await call.message.answer(
            "Шаг 2/3 — циклов (0 — бесконечно) или кнопкой:",
            reply_markup=_master_num_kb("c", ["2", "5", "∞"], lang),
        )
        return True
    if screen == "master" and action == "wcustom":
        if state is None:
            return True
        await state.set_state(MasterWiz.wallets)
        await call.message.answer("Введи число кошельков 1..5000. Или /cancel.")
        return True
    if screen == "master" and action == "c":
        if state is None:
            return True
        n_cycles: int | None = 0 if arg == "∞" else None
        if n_cycles is None:
            try:
                n_cycles = int(arg)
            except (TypeError, ValueError):
                return True
        n = n_cycles
        if not 0 <= n <= 100000:
            await call.message.answer("[err] Нужно число 0..100000.")
            return True
        if _master_expired(await state.get_data()):
            await state.clear()
            await call.message.answer(_R.tr("stale", lang))
            return True
        await state.update_data(m_cycles=n)
        await call.message.answer(
            "Шаг 3/3 — профиль (быстрый = --fast: меньше задержек, заметнее).\n"
            "Ритмы флота: выходные активнее, у каждого своё окно часов — детали в [Профили]:",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="[Тихий]", callback_data="v1:master:profile:quiet"),
                 InlineKeyboardButton(text="[Обычный]", callback_data="v1:master:profile:normal"),
                 InlineKeyboardButton(text="[Быстрый]", callback_data="v1:master:profile:fast")],
                [InlineKeyboardButton(text=_R.tr("btn_cancel", lang),
                                      callback_data="v1:master:cancel")],
            ]),
        )
        return True
    if screen == "master" and action == "ccustom":
        if state is None:
            return True
        await state.set_state(MasterWiz.cycles)
        await call.message.answer("Введи число циклов 0..100000 (0 — бесконечно). Или /cancel.")
        return True
    if screen == "master" and action == "profile":
        if state is None or arg not in _bot_ops.PROFILE_FLAGS:
            return True
        if _master_expired(await state.get_data()):
            await state.clear()
            await call.message.answer(_R.tr("stale", lang))
            return True
        await state.update_data(m_profile=arg)
        await call.message.answer(
            f"Профиль: {_PROFILE_LABEL[arg]}. Кран:",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="[С краном]", callback_data="v1:master:faucet:1"),
                 InlineKeyboardButton(text="[Без крана]", callback_data="v1:master:faucet:0")],
                [InlineKeyboardButton(text=_R.tr("btn_cancel", lang),
                                      callback_data="v1:master:cancel")],
            ]),
        )
        return True
    if screen == "master" and action == "faucet":
        if state is None or arg not in {"0", "1"}:
            return True
        if _master_expired(await state.get_data()):
            await state.clear()
            await call.message.answer(_R.tr("stale", lang))
            return True
        await state.update_data(m_faucet=(arg == "1"))
        await _master_finish(call.message, state)
        return True
    if screen == "master" and action == "yaml":
        if state is None:
            return True
        data = await state.get_data()
        if _master_expired(data) or not data.get("m_yaml") or not data.get("m_cmd"):
            await state.clear()
            await call.message.answer(_R.tr("stale", lang))
            return True
        try:
            filename, content = _bot_ops.build_config_yaml(data["m_net"], data["m_wallets"])
        except ValueError as e:
            await call.message.answer(f"[err] {_esc(e)}")
            return True
        await call.message.bot.send_document(
            chat_id=call.from_user.id,
            document=BufferedInputFile(content.encode("utf-8"), filename=filename),
            caption=f"<code>{_esc(data['m_cmd'])}</code>",
            parse_mode=ParseMode.HTML,
        )
        return True
    if screen == "master" and action == "verify":
        if state is None:
            return True
        data = await state.get_data()
        if _master_expired(data) or not data.get("m_yaml"):
            await state.clear()
            await call.message.answer(_R.tr("stale", lang))
            return True
        _ok, lines = _check_config_text(data["m_yaml"])
        await call.message.answer(
            "<b>harvest // ПРОВЕРКА</b>\n" + "\n".join(lines), parse_mode=ParseMode.HTML)
        return True
    if screen == "master" and action == "restart":
        if state is not None:
            await state.clear()
            await state.update_data(m_started=time.monotonic())
        await call.message.answer(
            "<b>harvest // МАСТЕР</b>\nШаг 1/3 — сеть:",
            parse_mode=ParseMode.HTML,
            reply_markup=await _master_nets_kb(lang),
        )
        return True
    if screen == "master" and action == "cancel":
        if state is not None:
            try:
                await state.clear()
            except Exception:  # noqa: BLE001
                pass
        await call.message.answer("[ОТМЕНА] Мастер закрыт.")
        return True
    # Неизвестный v1-экран новой версии — как устаревший: домой без трейсбэка.
    await _stale_home(call, daemon)
    return True


def _toggle_monitor(
    bot: Bot,
    tg_id: int,
    daemon: FarmDaemon,
    monitor: LiveMonitor,
    answer,
) -> None:
    """Включает/выключает живой мониторинг (команда /monitor и кнопка).

    answer — awaitable (message.answer / call.message.answer).
    """
    import asyncio as _asyncio

    if monitor.bot is None:
        # Ленивая инициализация: привязываем к реальному боту и владельцу.
        monitor.bot = bot
        monitor.tg_id = tg_id
        monitor.daemon = daemon

    async def _act() -> None:
        if monitor.running:
            await monitor.stop()
            await answer("Мониторинг выключен.")
        else:
            await monitor.start()
            await answer(
                "Мониторинг включён.\n"
                f"Статус буду присылать каждые {monitor.interval_s:.0f} с, "
                "при остановке фермы — сразу."
            )

    _asyncio.create_task(_act())


def _bot_ops_check(text: str) -> str:
    """Ответ на /check: YAML идёт следующими строками после команды."""
    _, _, rest = text.partition("\n")
    if not rest.strip():
        return (
            "<b>harvest // ПРОВЕРКА</b>\n"
            "Вставьте YAML следующими строками:\n"
            "<code>/check</code> + перенос строки + конфиг.\n"
            "Пример — вкладка «Про → Проверка» на сайте."
        )
    _ok, lines = _check_config_text(rest)
    return "<b>harvest // ПРОВЕРКА</b>\n" + "\n".join(lines)


async def _answer_cfg(message: Message, text: str) -> None:
    """Ответ на /cfg: готовый YAML документом + команда в подписи."""
    from aiogram.types import BufferedInputFile

    parts = text.split()
    if len(parts) < 2:
        await message.answer(_bot_ops.CFG_USAGE, parse_mode=ParseMode.HTML)
        return
    try:
        n = int(parts[2]) if len(parts) > 2 else 50
    except ValueError:
        await message.answer(
            "[err] кошельки — целое число 1..5000.\n" + _bot_ops.CFG_USAGE,
            parse_mode=ParseMode.HTML,
        )
        return
    try:
        filename, content = _bot_ops.build_config_yaml(parts[1], n)
        cmd = _bot_ops.build_command(parts[1], n, 5)
    except ValueError as e:
        await message.answer(f"[err] {_esc(e)}\n" + _bot_ops.CFG_USAGE, parse_mode=ParseMode.HTML)
        return
    await message.bot.send_document(
        chat_id=message.from_user.id,
        document=BufferedInputFile(content.encode("utf-8"), filename=filename),
        caption=f"<code>{_esc(cmd)}</code>",
        parse_mode=ParseMode.HTML,
    )


def build_dispatcher(cfg: PortalConfig, daemon: FarmDaemon, monitor: LiveMonitor | None = None) -> Dispatcher:
    install_token_redaction()
    dp = Dispatcher(storage=MemoryStorage())
    dp.conflict = _conflict  # трекер 409 для /doctor и тестов (п.7 ТЗ)
    # Монитор можно передать снаружи (для тестов/перезапуска). Если нет — создаём
    # лениво при первом /monitor: без токена бота у него не будет, но dispatcher
    # соберётся всегда.
    if monitor is None:
        monitor = LiveMonitor.__new__(LiveMonitor)
        monitor.bot = None
        monitor.tg_id = 0
        monitor.daemon = daemon
        monitor.interval_s = _DEFAULT_MONITOR_INTERVAL_S
        monitor._task = None
        monitor._stop = asyncio.Event()
        monitor._was_running = None
    dp.monitor = monitor  # доступен извне (тесты) и в обработчиках

    def _menu() -> InlineKeyboardMarkup:
        return _menu_kb(cfg, monitor_on=monitor.running)

    @dp.message(CommandStart())
    async def on_start(message: Message, state: FSMContext | None = None) -> None:
        if not _allowed(cfg, message.from_user.id if message.from_user else None):
            await message.answer("⛔ Нет доступа.")
            return
        if state is not None:
            try:
                await state.clear()  # сброс мастера по /start (п.4 ТЗ)
            except Exception:  # noqa: BLE001
                pass
        await _show_home_answer(message, daemon)

    @dp.message()
    async def on_help(message: Message, state: FSMContext | None = None) -> None:
        # Default-deny для ЛЮБЫХ сообщений, включая неизвестные команды:
        # посторонним бот не должен даже подтверждать, что он существует.
        if not _allowed(cfg, message.from_user.id if message.from_user else None):
            return
        # YAML-файл обрабатывает отдельный хендлер ниже (регистрирован позже,
        # сюда попадаем первыми — пропускаем дальше).
        if getattr(message, "document", None):
            return
        text = (message.text or "").strip()
        # Свободный ввод активных мастеров (числа, пароль) — до разбора команд.
        if state is not None:
            try:
                if text == "/cancel":
                    await state.clear()
                    await message.answer("[ОТМЕНА] Мастер закрыт.")
                    return
                if await _enc_handle_text(message, state, daemon):
                    return
                if await _master_handle_text(message, state):
                    return
                if await _wiz_handle_text(message, state):
                    return
            except Exception:  # noqa: BLE001
                logger.warning("мастер: сбой обработки ввода", exc_info=True)
                return
        if text == "/help":
            await message.answer(_HELP_TEXT, parse_mode=ParseMode.HTML, reply_markup=_menu())
        elif text == "/doctor":
            ok, wait = _flood_ok(message.from_user.id if message.from_user else None, "doctor")
            if not ok:
                await message.answer(f"[жди {wait}с] Диагностика — антифлуд 5с.")
                return
            await message.answer(
                _fmt_doctor(await daemon.statistics()) + f"\nbot: {bot_conflict_status()}",
                parse_mode=ParseMode.HTML,
                reply_markup=_menu(),
            )
        elif text == "/diag":
            ok, wait = _flood_ok(message.from_user.id if message.from_user else None, "doctor")
            if not ok:
                await message.answer(f"[жди {wait}с] Диагностика — антифлуд 5с.")
                return
            await message.answer(
                _fmt_doctor(await daemon.statistics()) + f"\nbot: {bot_conflict_status()}",
                parse_mode=ParseMode.HTML,
            )
        elif text == "/pnl":
            ok, wait = _flood_ok(message.from_user.id if message.from_user else None, "pnl")
            if not ok:
                await message.answer(f"[жди {wait}с] PnL — антифлуд 5с.")
                return
            await message.answer(_fmt_pnl(await daemon.pnl_snapshot()), parse_mode=ParseMode.HTML)
        elif text == "/history":
            ok, wait = _flood_ok(message.from_user.id if message.from_user else None, "history")
            if not ok:
                await message.answer(f"[жди {wait}с] История — антифлуд 5с.")
                return
            await message.answer(_fmt_history(await daemon.cycle_history()), parse_mode=ParseMode.HTML)
        elif text == "/status":
            ok, wait = _flood_ok(message.from_user.id if message.from_user else None, "status")
            if not ok:
                await message.answer(f"[жди {wait}с] Статус — антифлуд 5с.")
                return
            await message.answer(_fmt_status(await daemon.statistics()), parse_mode=ParseMode.HTML)
        elif text.startswith("/wallets"):
            ok, wait = _flood_ok(message.from_user.id if message.from_user else None, "wallets")
            if not ok:
                await message.answer(f"[жди {wait}с] Тяжёлая операция — антифлуд.")
                return
            parts = text.split()
            page = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 1
            wallets = await daemon.all_wallets()
            content, pages = _fmt_wallets_page(wallets, page)
            kb = _wallets_kb(page, pages) if wallets else _wallets_empty_kb()
            await message.answer(content, parse_mode=ParseMode.HTML, reply_markup=kb)
        elif text.startswith("/monitor"):
            _toggle_monitor(message.bot, message.from_user.id, daemon, monitor, message.answer)
        elif text.startswith("/export"):
            ok, wait = _flood_ok(message.from_user.id if message.from_user else None, "export")
            if not ok:
                await message.answer(f"[жди {wait}с] Тяжёлая операция — антифлуд.")
                return
            parts = text.split()
            fmt = parts[1].lower() if len(parts) > 1 and parts[1].lower() in {"json"} else "csv"
            try:
                await send_wallets_file(message.bot, message.from_user.id, daemon, fmt)
                logger.info(
                    "[AUDIT] export user=%s fmt=%s via=/export",
                    message.from_user.id if message.from_user else None,
                    fmt,
                )
            except Exception:  # noqa: BLE001
                logger.warning("Экспорт кошельков через /export упал", exc_info=True)
                await message.answer("[err] Не удалось сформировать файл кошельков.")
        elif text == "/network":
            answer, kb = _networks_view(daemon)
            await message.answer(answer, parse_mode=ParseMode.HTML, reply_markup=kb)
        elif text == "/profiles":
            try:
                wallets = await daemon.all_wallets()
            except Exception:  # noqa: BLE001
                wallets = []
            if not wallets:
                await message.answer(
                    "Профилей пока нет — флот пуст.\n"
                    "Создай кошельки: портал → Кошельки → Создать, или /wallets → [+ СОЗДАТЬ]."
                )
                return
            cfgd = getattr(daemon, "config", None) or {}
            beh = (cfgd.get("behavior") or {}) if isinstance(cfgd, dict) else {}
            summary = _bot_ops.profiles_summary(
                [w.get("address", "") for w in wallets if isinstance(w, dict)], beh)
            _mode, lang = await _mode_lang(daemon, message.from_user.id if message.from_user else None)
            await message.answer(_R.render_profiles(summary, lang), parse_mode=ParseMode.HTML)
        elif text == "/menu":
            _mode, lang = await _mode_lang(daemon, message.from_user.id if message.from_user else None)
            uid = message.from_user.id if message.from_user else None
            live_on = bool(uid and _live.get(uid) is not None and not _live[uid].done())
            await message.answer(
                "<b>harvest // МЕНЮ</b>" if lang == "ru" else "<b>harvest // MENU</b>",
                parse_mode=ParseMode.HTML,
                reply_markup=_menu_screen_kb(lang, live_on),
            )
        elif text == "/balance":
            ok, wait = _flood_ok(message.from_user.id if message.from_user else None, "balance")
            if not ok:
                await message.answer(f"[жди {wait}с] Балансы — антифлуд 10с.")
                return
            try:
                bals = await daemon.wallet_balances()
            except RuntimeError:
                await message.answer("[err] Прогон не запущен.")
                return
            except Exception:  # noqa: BLE001
                await message.answer("[err] Не удалось прочитать балансы.")
                return
            if not bals:
                await message.answer("Кошельков пока нет — создайте через /wallets или портал.")
                return
            await message.answer(_fmt_balances(bals), parse_mode=ParseMode.HTML)
        elif text == "/pause":
            ok = await daemon.pause()
            await message.answer("[ПАУЗА] Прогон на паузе." if ok else "[err] Нечего ставить на паузу.")
        elif text == "/resume":
            ok = await daemon.resume()
            await message.answer("[ОК] Прогон продолжен." if ok else "[err] Нечего продолжать.")
        elif text == "/stop":
            ok = await daemon.stop()
            await message.answer("[СТОП] Прогон остановлен." if ok else "[err] Прогон не запущен.")
        elif text == "/faucet":
            ok, wait = _flood_ok(message.from_user.id if message.from_user else None, "faucet")
            if not ok:
                await message.answer(f"[жди {wait}с] Кран — антифлуд 15с.")
                return
            try:
                report = await daemon.faucet_report()
            except Exception:  # noqa: BLE001
                report = []
            live = sum(1 for p in report if isinstance(p, dict) and p.get("alive"))
            lines = ["<b>harvest // КРАН</b>"]
            if not report:
                lines.append("проба не удалась — смотри логи прогона.")
            for p in report[:6]:
                mark = "[ok]" if p.get("alive") else "[err]"
                lines.append(
                    f"{mark} {p.get('type')}: {html.escape(str(p.get('reason', ''))[:160])}"
                )
            if live > 0:
                lines.append(f"Итого живых: {live}.")
            else:
                lines.append(
                    "Итого: 0 живых — задай CHAINSTACK_API_KEY в окружении "
                    "машины прогона и перезапусти задачу."
                )
            await message.answer("\n".join(lines), parse_mode=ParseMode.HTML)
        elif text == "/rpc":
            ok, wait = _flood_ok(message.from_user.id if message.from_user else None, "rpc")
            if not ok:
                await message.answer(f"[жди {wait}с] RPC — антифлуд 15с.")
                return
            try:
                health = await daemon.rpc_health()
            except Exception:  # noqa: BLE001
                await message.answer("[err] Не удалось опросить RPC.")
                return
            await message.answer(_fmt_rpc(health), parse_mode=ParseMode.HTML)
        elif text.startswith("/scheduler") or text.startswith("/schedule"):
            ok, wait = _flood_ok(message.from_user.id if message.from_user else None, "scheduler")
            if not ok:
                await message.answer(f"[жди {wait}с] Планировщик — антифлуд 10с.")
                return
            await message.answer(_fmt_scheduler(daemon.scheduler_state()), parse_mode=ParseMode.HTML)
        elif text.startswith("/alerts"):
            parts = text.split()
            if len(parts) >= 2 and parts[1].lower() in {"off", "0", "disable"}:
                try:
                    await daemon.set_balance_alerts(False, 0)
                    await message.answer("[ok] Алерты выкл.")
                except Exception:  # noqa: BLE001
                    await message.answer("[err] Не удалось выключить алерты.")
                return
            if len(parts) >= 2:
                try:
                    threshold = float(parts[1])
                    if not 0 <= threshold <= 100:
                        raise ValueError
                    res = await daemon.set_balance_alerts(True, threshold)
                    await message.answer(f"[ok] Алерты вкл, порог {res.get('threshold')}.")
                    return
                except (ValueError, TypeError):
                    await message.answer("[err] /alerts [порог 0..100] или /alerts off")
                    return
                except Exception:  # noqa: BLE001
                    await message.answer("[err] Не удалось сохранить алерты.")
                    return
                try:
                    await daemon.set_balance_alerts(False, 0)
                    await message.answer("[ok] Алерты выкл.")
                except Exception:  # noqa: BLE001
                    await message.answer("[err] Не удалось выключить алерты.")
                return
            try:
                st = daemon.balance_alerts_state()
            except Exception:  # noqa: BLE001
                await message.answer("[err] Не удалось прочитать алерты.")
                return
            on = "вкл" if st.get("enabled") else "выкл"
            await message.answer(
                f"Алерты: {on}, порог {st.get('threshold')}.\n"
                "/alerts 0.01 — вкл с порогом, /alerts off — выкл."
            )
        elif text == "/cancel":
            await message.answer("[ОТМЕНА] Мастер закрыт.")
        elif text.startswith("/cmd"):
            await message.answer(
                _bot_ops_parse_cmd(text), parse_mode=ParseMode.HTML, reply_markup=_menu()
            )
        elif text.startswith("/cfg"):
            await _answer_cfg(message, text)
        elif text.startswith("/check"):
            await message.answer(_bot_ops_check(text), parse_mode=ParseMode.HTML)
        elif text == "/ref":
            await message.answer(_bot_ops.REF_TEXT, parse_mode=ParseMode.HTML)
        elif text.startswith("/"):
            await message.answer("Неизвестная команда. /help — список возможностей.")

    @dp.message(F.document)
    async def on_document(message: Message) -> None:
        # YAML-файл на проверку (текстом — через /check). Лимит 16 КБ.
        if not _allowed(cfg, message.from_user.id if message.from_user else None):
            return
        doc = message.document
        name = (doc.file_name or "").lower()
        if not name.endswith((".yaml", ".yml", ".txt")):
            await message.answer("[err] Пришли YAML (.yaml/.yml). Текстом — через /check.")
            return
        if (doc.file_size or 0) > 16 * 1024:
            await message.answer("[err] Файл больше 16 КБ.")
            return
        try:
            from io import BytesIO

            buf = BytesIO()
            await message.bot.download(doc, buf)
            _ok, lines = _check_config_text(buf.getvalue().decode("utf-8", "ignore"))
        except Exception:  # noqa: BLE001
            logger.warning("проверка YAML-файла упала", exc_info=True)
            await message.answer("[err] Не удалось прочитать файл.")
            return
        await message.answer(
            "<b>harvest // ПРОВЕРКА</b>\n" + "\n".join(lines), parse_mode=ParseMode.HTML
        )

    @dp.callback_query()
    async def on_callback(call: CallbackQuery, state: FSMContext | None = None) -> None:
        if not _allowed(cfg, call.from_user.id if call.from_user else None):
            await call.answer("⛔ Нет доступа.", show_alert=True)
            return
        data = call.data or ""
        try:
            await call.answer()
        except Exception:  # noqa: BLE001
            pass

        try:
            if await _handle_v1(call, state, cfg, daemon):
                return
            if data == "cmd:run":
                if state is None:
                    await call.message.answer(_bot_ops.CMD_USAGE, parse_mode=ParseMode.HTML)
                else:
                    await _wiz_start_cmd(call.message, state)
                return

            if data == "cfg:run":
                if state is None:
                    await call.message.answer(_bot_ops.CFG_USAGE, parse_mode=ParseMode.HTML)
                else:
                    await _wiz_start_cfg(call.message, state)
                return

            if data == "wiz:cancel":
                if state is not None:
                    try:
                        await state.clear()
                    except Exception:  # noqa: BLE001
                        pass
                await call.message.answer("[ОТМЕНА] Мастер закрыт.")
                return

            if data.startswith("wiz:cmd:net:") or data.startswith("wiz:cfg:net:"):
                if state is None:
                    return
                slug = data.rsplit(":net:", 1)[1]
                if not _bot_ops.known_slug(slug):
                    await call.message.answer("[err] Неизвестная сеть.")
                    return
                slug = "vibevibe" if slug == "vibe" else slug
                if data.startswith("wiz:cmd:"):
                    await state.update_data(net=slug)
                    await state.set_state(CmdWizard.wallets)
                    await call.message.answer(
                        "Шаг 2/4 — кошельков (или отправь число 1..5000):",
                        reply_markup=_wiz_num_kb("wiz:cmd:w", ["3", "10", "50", "200"]),
                    )
                else:
                    await state.update_data(net=slug)
                    await state.set_state(CfgWizard.wallets)
                    await call.message.answer(
                        "Шаг 2/2 — кошельков (или отправь число 1..5000):",
                        reply_markup=_wiz_num_kb("wiz:cfg:w", ["3", "10", "50", "200"]),
                    )
                return

            if data.startswith("wiz:cmd:w:") or data.startswith("wiz:cfg:w:"):
                if state is None:
                    return
                try:
                    n = int(data.rsplit(":w:", 1)[1])
                except ValueError:
                    return
                if not 1 <= n <= 5000:
                    await call.message.answer("[err] Нужно число 1..5000.")
                    return
                await state.update_data(wallets=n)
                if data.startswith("wiz:cmd:"):
                    await state.set_state(CmdWizard.cycles)
                    await call.message.answer(
                        "Шаг 3/4 — кругов (0 — бесконечно):",
                        reply_markup=_wiz_num_kb("wiz:cmd:c", ["1", "2", "5", "0"]),
                    )
                else:
                    await _wiz_finish_cfg(call.message, state)
                return

            if data.startswith("wiz:cmd:c:"):
                if state is None:
                    return
                try:
                    n = int(data.rsplit(":c:", 1)[1])
                except ValueError:
                    return
                if not 0 <= n <= 100000:
                    await call.message.answer("[err] Нужно число 0..100000.")
                    return
                await state.update_data(cycles=n)
                await state.set_state(CmdWizard.faucet)
                await call.message.answer(
                    "Шаг 4/4 — кран:",
                    reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                        InlineKeyboardButton(text="[С КРАНОМ]", callback_data="wiz:cmd:f:1"),
                        InlineKeyboardButton(text="[БЕЗ КРАНА]", callback_data="wiz:cmd:f:0"),
                    ], [InlineKeyboardButton(text="[ОТМЕНА]", callback_data="wiz:cancel")]]),
                )
                return

            if data.startswith("wiz:cmd:f:"):
                if state is None:
                    return
                await _wiz_finish_cmd(call.message, state, data.endswith(":0"))
                return

            if data == "stats":
                answer = _fmt_stats(await daemon.statistics())
                kb = _refresh_home_kb("stats")
                await call.message.edit_text(
                    answer,
                    parse_mode=ParseMode.HTML,
                    reply_markup=kb,
                    link_preview_options=_NO_PREVIEW,
                )
                return

            if data == "status":
                answer = _fmt_status(await daemon.statistics())
                kb = _refresh_home_kb("status")
                await call.message.edit_text(
                    answer,
                    parse_mode=ParseMode.HTML,
                    reply_markup=kb,
                    link_preview_options=_NO_PREVIEW,
                )
                return

            if data == "monitor:toggle":
                _toggle_monitor(
                    call.message.bot, call.from_user.id, daemon, monitor, call.message.answer
                )
                return

            if data.startswith("wallets:page:"):
                page = int(data.split(":")[2])
                wallets = await daemon.all_wallets()
                content, pages = _fmt_wallets_page(wallets, page)
                kb = _wallets_kb(page, pages) if wallets else _wallets_empty_kb()
                await call.message.edit_text(content, parse_mode=ParseMode.HTML, reply_markup=kb)
                return

            if data == "wallets:create":
                ok, wait = _flood_ok(call.from_user.id if call.from_user else None, "create")
                if not ok:
                    await call.message.answer(f"[жди {wait}с] Тяжёлая операция — антифлуд.")
                    return
                try:
                    n = int(((daemon.config or {}).get("wallets") or {}).get("count", 3))
                except (TypeError, ValueError, AttributeError):
                    n = 3
                try:
                    created = await daemon.create_wallets(n)
                except RuntimeError:
                    await call.message.answer("[err] БД фермы не подключена.")
                    return
                await call.message.answer(f"[ok] Создано кошельков: {len(created)}")
                wallets = await daemon.all_wallets()
                content, pages = _fmt_wallets_page(wallets, 1)
                kb = _wallets_kb(1, pages) if wallets else _wallets_empty_kb()
                await call.message.edit_text(content, parse_mode=ParseMode.HTML, reply_markup=kb)
                return

            if data == "links":
                links = load_links(cfg.links_path)
                title = html.escape(str(links.get("title", "Наши ссылки"))[:200])
                lines = [f"<b>harvest // ССЫЛКИ — {title}</b>", ""]
                # Ограничение размера: сообщение >4096 символов Telegram отвергнет.
                for item in links.get("items", [])[:15]:
                    url = html.escape(str(item["url"])[:300], quote=True)
                    label = html.escape(str(item.get("label", item["url"]))[:200])
                    lines.append(f'• <a href="{url}">{label}</a>')
                if len(links.get("items", [])) > 15:
                    lines.append(f"… и ещё {len(links.get('items', [])) - 15}")
                await call.message.edit_text(
                    "\n".join(lines),
                    parse_mode=ParseMode.HTML,
                    reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                        [InlineKeyboardButton(text="[ДОМОЙ]", callback_data="menu:home")],
                    ]),
                    link_preview_options=_NO_PREVIEW,
                )
                return

            if data == "doctor":
                answer = _fmt_doctor(await daemon.statistics()) + f"\nbot: {bot_conflict_status()}"
                kb = _refresh_home_kb("doctor")
                await call.message.edit_text(
                    answer,
                    parse_mode=ParseMode.HTML,
                    reply_markup=kb,
                    link_preview_options=_NO_PREVIEW,
                )
                return

            if data == "history":
                answer = _fmt_history(await daemon.cycle_history())
                kb = _refresh_home_kb("history")
                await call.message.edit_text(
                    answer,
                    parse_mode=ParseMode.HTML,
                    reply_markup=kb,
                    link_preview_options=_NO_PREVIEW,
                )
                return

            if data in ("export:csv", "export:json"):
                fmt = data.split(":")[1]
                await call.message.edit_text(
                    "<b>harvest // ЭКСПОРТ — подтверждение</b>\n"
                    f"Файл ({fmt.upper()}) содержит <b>все приватные ключи</b>.\n"
                    "Пересылка запрещена, сообщение удалится через 60 с.\n"
                    "Продолжить?",
                    parse_mode=ParseMode.HTML,
                    reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                        InlineKeyboardButton(text="[ДА, ВЫСЛАТЬ]", callback_data=f"export:yes:{fmt}"),
                        InlineKeyboardButton(text="[ДОМОЙ]", callback_data="menu:home"),
                    ]]),
                    link_preview_options=_NO_PREVIEW,
                )
                return

            if data.startswith("export:yes:"):
                fmt = data.split(":")[2]
                if fmt not in {"csv", "json"}:
                    return
                ok, wait = _flood_ok(call.from_user.id if call.from_user else None, "export")
                if not ok:
                    await call.message.answer(f"[жди {wait}с] Тяжёлая операция — антифлуд.")
                    return
                try:
                    await call.message.answer("[...] Готовлю файл кошельков…")
                    await send_wallets_file(call.message.bot, call.from_user.id, daemon, fmt)
                    logger.info(
                        "[AUDIT] export user=%s fmt=%s via=button",
                        call.from_user.id if call.from_user else None,
                        fmt,
                    )
                except Exception:  # noqa: BLE001
                    logger.warning("Экспорт кошельков (кнопка) упал", exc_info=True)
                    await call.message.answer("[err] Не удалось сформировать файл кошельков.")
                return

            if data in ("cmd:usage", "cfg:usage"):
                await call.message.answer(
                    _bot_ops.CMD_USAGE if data == "cmd:usage" else _bot_ops.CFG_USAGE,
                    parse_mode=ParseMode.HTML,
                )
                return

            if data == "ref":
                await call.message.edit_text(
                    _bot_ops.REF_TEXT,
                    parse_mode=ParseMode.HTML,
                    reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                        [InlineKeyboardButton(text="[ОБНОВИТЬ]", callback_data="ref")],
                        [InlineKeyboardButton(text="[ДОМОЙ]", callback_data="menu:home")],
                    ]),
                    link_preview_options=_NO_PREVIEW,
                )
                return

            if data == "menu:home":
                # Legacy-кнопки ведут на новое Поле (п.2 ТЗ); текст обновлён.
                await _show_home_edit(call, daemon)
                return

            if data == "network:list":
                answer, kb = _networks_view(daemon)
                await call.message.edit_text(
                    answer,
                    parse_mode=ParseMode.HTML,
                    reply_markup=kb,
                    link_preview_options=_NO_PREVIEW,
                )
                return

            if data.startswith("network:start:"):
                slug = data.split(":", 2)[2]
                switched = await daemon.switch_to(slug)
                if not switched:
                    await call.message.answer("[err] Неизвестная сеть.")
                    return
                ok = await daemon.start()
                await call.message.answer(
                    f"[{'ok' if ok else 'err'}] Прогон {_esc(slug)}: {'запущен' if ok else 'не запустился'}"
                )
                answer, kb = _networks_view(daemon)
                await call.message.edit_text(
                    answer,
                    parse_mode=ParseMode.HTML,
                    reply_markup=kb,
                    link_preview_options=_NO_PREVIEW,
                )
                return

            if data == "network:stop":
                ok = await daemon.stop()
                await call.message.answer("[ok] Прогон остановлен." if ok else "[err] Остановить не удалось.")
                answer, kb = _networks_view(daemon)
                await call.message.edit_text(
                    answer,
                    parse_mode=ParseMode.HTML,
                    reply_markup=kb,
                    link_preview_options=_NO_PREVIEW,
                )
                return

            actions = {
                "farm:start": daemon.start,
                "farm:stop": daemon.stop,
                "farm:pause": daemon.pause,
                "farm:resume": daemon.resume,
            }
            if data in actions:
                ok = await actions[data]()
                label = data.split(":")[1]
                done = "выполнено" if ok else "уже в этой позиции / стенд недоступен"
                await call.message.answer(f"[{'ok' if ok else 'err'}] {label}: {done}")
                return

            await call.message.answer("Неизвестная команда.", reply_markup=_menu_kb(cfg))
        except Exception:  # noqa: BLE001
            # Сбой daemon/БД не должен молча глотаться aiogram-диспетчером:
            # пишем в журнал и отвечаем пользователю generic-текстом (без деталей).
            logger.warning("callback %r упал", data, exc_info=True)
            try:
                await call.message.answer("[err] Не удалось выполнить команду. Попробуйте позже.")
            except Exception:  # noqa: BLE001
                pass

    return dp


async def run_bot(cfg: PortalConfig, daemon: FarmDaemon) -> None:
    install_token_redaction()
    dlg = logging.getLogger("aiogram.dispatcher")
    if not any(isinstance(f, _ConflictLogFilter) for f in dlg.filters):
        dlg.addFilter(_ConflictLogFilter())
    bot = Bot(token=cfg.telegram_token, default=_BOT_DEFAULTS)
    # Первый разрешённый владелец — получатель живого мониторинга по умолчанию.
    default_tg = cfg.telegram_allow_ids[0] if cfg.telegram_allow_ids else 0
    monitor = LiveMonitor(bot, default_tg, daemon)
    dp = build_dispatcher(cfg, daemon, monitor)
    logger.info("Telegram-бот запущен (long-polling)")
    await setup_bot_commands(bot)
    # При каждом запуске шлём владельцу актуальный адрес портала,
    # чтобы смена адреса туннеля не терялась.
    if cfg.public_base_url:
        try:
            await notify_owner(
                cfg,
                "harvest запущен.\nСайт: " + cfg.public_base_url,
            )
        except Exception:  # noqa: BLE001
            logger.warning("Не удалось отправить адрес владельцу", exc_info=True)
    try:
        await dp.start_polling(bot)
    except Exception as e:  # noqa: BLE001
        # Конфликт 409 (п.7 ТЗ): другой экземпляр опрашивает токен — не падать,
        # один понятный ряд + одно уведомление владельцу, дальше — наверх
        # (перезапуск — делом супервизора) с отредактированным текстом.
        from aiogram.exceptions import TelegramConflictError

        from portal.bot_render import redact_token

        if isinstance(e, TelegramConflictError):
            delay = _conflict.hit()
            if _conflict.needs_notify():
                try:
                    await notify_owner(cfg, "Другой экземпляр опрашивает этот токен (409).")
                except Exception:  # noqa: BLE001
                    pass
            logger.warning("Telegram 409, backoff %.0fс: %s", delay, redact_token(e))
            await asyncio.sleep(delay)
        raise
    else:
        _conflict.resolve()
