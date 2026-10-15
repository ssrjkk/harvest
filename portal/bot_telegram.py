"""Telegram-бот HARVEST PORTAL: статистика, старт/стоп, пауза, ссылки,
кнопка Mini App, живой мониторинг с алертами. Long-polling (без открытых
портов — дружит с любым хостингом).

Стиль сообщений — ops-board сайта (раздел 05): секции `HARVEST // X`,
маркеры `[RUN]`/`[STOP]`, `[ok]`/`[warn]`, таблицы в <pre>, без эмодзи.
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
    check_config_text as _check_config_text,
    fmt_balances as _fmt_balances,
    fmt_doctor as _fmt_doctor,
    fmt_history as _fmt_history,
    fmt_networks as _fmt_networks,
    fmt_stats as _fmt_stats,
    fmt_status as _fmt_status,
    parse_cmd_args as _bot_ops_parse_cmd,
    parse_count as _parse_count,
    parse_cycles as _parse_cycles,
)
from portal.config import PortalConfig, load_links
from portal.farm import FarmDaemon

# Реэкспорт ops-форматтеров: тесты и внешний код используют имена
# portal.bot_telegram (реализация — portal/bot_ops.py).
__all__ = [
    "_fmt_balances", "_fmt_doctor", "_fmt_history", "_fmt_networks",
    "_fmt_stats", "_fmt_status", "_fmt_wallets_page", "_progress_bar",
    "build_dispatcher", "run_bot", "notify_owner", "send_wallets_file",
    "LiveMonitor", "CmdWizard", "CfgWizard",
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
        "<b>HARVEST // МАСТЕР</b>\nШаг 1/4 — сеть:",
        parse_mode=ParseMode.HTML,
        reply_markup=_wiz_nets_kb("wiz:cmd"),
    )


async def _wiz_start_cfg(message: Message, state: FSMContext) -> None:
    await state.clear()
    await state.set_state(CfgWizard.net)
    await message.answer(
        "<b>HARVEST // МАСТЕР</b>\nШаг 1/2 — сеть для YAML:",
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
        f"<b>HARVEST // ГОТОВО</b>\n<code>{_esc(cmd)}</code>\n{_FOOT}",
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


async def notify_owner(cfg: PortalConfig, text: str) -> None:
    """Пуш-алерт владельцу (Telegram). Без токена/разрешённых — тихий no-op."""
    if not cfg.telegram_token or not cfg.telegram_allow_ids:
        return
    bot = Bot(token=cfg.telegram_token, default=_BOT_DEFAULTS)
    async with bot:
        for tg_id in cfg.telegram_allow_ids:
            try:
                # Алерты слаем plain-text: там могут быть символы типа &, которые
                # HTML-парсер Telegram не примет.
                await bot.send_message(chat_id=tg_id, text=text, parse_mode=None)
            except Exception:  # noqa: BLE001
                logger.warning("Не удалось отправить алерт %s", tg_id, exc_info=True)


def _wallets_kb(page: int, pages: int) -> InlineKeyboardMarkup:
    row = []
    if page > 1:
        row.append(InlineKeyboardButton(text="◀", callback_data=f"wallets:page:{page-1}"))
    if page < pages:
        row.append(InlineKeyboardButton(text="▶", callback_data=f"wallets:page:{page+1}"))
    return InlineKeyboardMarkup(inline_keyboard=[row]) if row else InlineKeyboardMarkup(inline_keyboard=[])


def _wallets_empty_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="[+ СОЗДАТЬ]", callback_data="wallets:create")]]
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
                except asyncio.TimeoutError:
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
            text = "[АЛЕРТ] Ферма остановилась.\n/status — детали"
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
    """Отправляет владельцу файл со всеми кошельками (CSV/JSON)."""
    from aiogram.types import BufferedInputFile

    content, filename = await daemon.export_wallets(fmt)
    await bot.send_document(
        chat_id=tg_id,
        document=BufferedInputFile(content.encode("utf-8-sig"), filename=filename),
        caption=f"Кошельки ({fmt.upper()}) — {len(content):,} байт",
    )


def _menu_kb(cfg: PortalConfig, monitor_on: bool = False) -> InlineKeyboardMarkup:
    rows = []
    if cfg.public_base_url:
        rows.append([InlineKeyboardButton(text="[MINI APP]", web_app=WebAppInfo(url=cfg.public_base_url))])
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
                InlineKeyboardButton(text="[ПУЛЬТ]", callback_data="status"),
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
    "<b>HARVEST PORTAL</b> — единый пульт фермы.\n\n"
    "Пульт и диагностика:\n"
    "• /status — живой статус (успех %, средние, health)\n"
    "• /monitor — живой мониторинг с алертами\n"
    "• /doctor — диагностика, /history — история циклов\n"
    "• /balance — балансы кошельков\n\n"
    "Управление:\n"
    "• Старт / Стоп / Пауза / Резюм\n"
    "• /network — переключение тестнет-сети\n\n"
    "Конструкторы (как вкладки Про на сайте):\n"
    "• /cmd сеть [кошельки] [циклы] — готовая команда\n"
    "• Кнопка [CMD] — мастер: сеть → кошельки → циклы → кран\n"
    "• /cfg сеть [кошельки] — готовый YAML файлом\n"
    "• Кнопка [CFG] — мастер: сеть → кошельки\n"
    "• /check + YAML следующими строками — проверка валидатором\n"
    "• YAML можно прислать файлом (.yaml) — тоже проверю\n"
    "• /cancel — закрыть мастер\n"
    "• /ref — сжатый справочник параметров\n\n"
    "Кошельки:\n"
    "• /wallets — список (ключи под спойлером)\n"
    "• /export [json] — скачать все кошельки (CSV)\n\n"
    "/start — главное меню\n/help — эта справка\n\n"
    "ssrjkk · t.me/ssrjkk_bot · MIT · v2.3.0"
)


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
        ]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _networks_view(daemon: FarmDaemon) -> tuple[str, InlineKeyboardMarkup]:
    networks = daemon.networks_info()
    return _fmt_networks(networks), _networks_kb(networks)


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
            "<b>HARVEST // ПРОВЕРКА</b>\n"
            "Вставьте YAML следующими строками:\n"
            "<code>/check</code> + перенос строки + конфиг.\n"
            "Пример — вкладка «Про → Проверка» на сайте."
        )
    _ok, lines = _check_config_text(rest)
    return "<b>HARVEST // ПРОВЕРКА</b>\n" + "\n".join(lines)


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
    dp = Dispatcher(storage=MemoryStorage())
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
    async def on_start(message: Message) -> None:
        if not _allowed(cfg, message.from_user.id if message.from_user else None):
            await message.answer("⛔ Нет доступа.")
            return
        await message.answer(
            "Привет! Это <b>HARVEST PORTAL</b> — единый пульт фермы.\n"
            "Фарм, пульт, конструкторы команд и конфигов — всё здесь.\n\n"
            "ssrjkk · t.me/ssrjkk_bot · MIT · v2.3.0",
            parse_mode=ParseMode.HTML,
            reply_markup=_menu(),
        )

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
        # Свободный ввод активного мастера (числа) — до разбора команд.
        if state is not None:
            try:
                if await _wiz_handle_text(message, state):
                    return
            except Exception:  # noqa: BLE001
                logger.warning("мастер: сбой обработки ввода", exc_info=True)
                return
        text = (message.text or "").strip()
        if text == "/help":
            await message.answer(_HELP_TEXT, parse_mode=ParseMode.HTML, reply_markup=_menu())
        elif text == "/doctor":
            await message.answer(
                _fmt_doctor(await daemon.statistics()),
                parse_mode=ParseMode.HTML,
                reply_markup=_menu(),
            )
        elif text == "/history":
            await message.answer(_fmt_history(await daemon.cycle_history()), parse_mode=ParseMode.HTML)
        elif text == "/status":
            await message.answer(_fmt_status(await daemon.statistics()), parse_mode=ParseMode.HTML)
        elif text.startswith("/wallets"):
            parts = text.split()
            page = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 1
            wallets = await daemon.all_wallets()
            content, pages = _fmt_wallets_page(wallets, page)
            kb = _wallets_kb(page, pages) if wallets else _wallets_empty_kb()
            await message.answer(content, parse_mode=ParseMode.HTML, reply_markup=kb)
        elif text.startswith("/monitor"):
            _toggle_monitor(message.bot, message.from_user.id, daemon, monitor, message.answer)
        elif text.startswith("/export"):
            parts = text.split()
            fmt = parts[1].lower() if len(parts) > 1 and parts[1].lower() in {"json"} else "csv"
            try:
                await send_wallets_file(message.bot, message.from_user.id, daemon, fmt)
            except Exception:  # noqa: BLE001
                logger.warning("Экспорт кошельков через /export упал", exc_info=True)
                await message.answer("[err] Не удалось сформировать файл кошельков.")
        elif text == "/network":
            answer, kb = _networks_view(daemon)
            await message.answer(answer, parse_mode=ParseMode.HTML, reply_markup=kb)
        elif text == "/balance":
            try:
                bals = await daemon.wallet_balances()
            except RuntimeError:
                await message.answer("[err] Ферма не запущена.")
                return
            await message.answer(_fmt_balances(bals), parse_mode=ParseMode.HTML)
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
            "<b>HARVEST // ПРОВЕРКА</b>\n" + "\n".join(lines), parse_mode=ParseMode.HTML
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
                kb = InlineKeyboardMarkup(
                    inline_keyboard=[[InlineKeyboardButton(text="[ОБНОВИТЬ]", callback_data="stats")]]
                )
                await call.message.edit_text(
                    answer,
                    parse_mode=ParseMode.HTML,
                    reply_markup=kb,
                    link_preview_options=_NO_PREVIEW,
                )
                return

            if data == "status":
                answer = _fmt_status(await daemon.statistics())
                kb = InlineKeyboardMarkup(
                    inline_keyboard=[[InlineKeyboardButton(text="[ОБНОВИТЬ]", callback_data="status")]]
                )
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
                lines = [f"<b>HARVEST // ССЫЛКИ — {title}</b>", ""]
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
                    link_preview_options=_NO_PREVIEW,
                )
                return

            if data == "doctor":
                answer = _fmt_doctor(await daemon.statistics())
                kb = InlineKeyboardMarkup(
                    inline_keyboard=[[InlineKeyboardButton(text="[ОБНОВИТЬ]", callback_data="doctor")]]
                )
                await call.message.edit_text(
                    answer,
                    parse_mode=ParseMode.HTML,
                    reply_markup=kb,
                    link_preview_options=_NO_PREVIEW,
                )
                return

            if data == "history":
                answer = _fmt_history(await daemon.cycle_history())
                kb = InlineKeyboardMarkup(
                    inline_keyboard=[[InlineKeyboardButton(text="[ОБНОВИТЬ]", callback_data="history")]]
                )
                await call.message.edit_text(
                    answer,
                    parse_mode=ParseMode.HTML,
                    reply_markup=kb,
                    link_preview_options=_NO_PREVIEW,
                )
                return

            if data in ("export:csv", "export:json"):
                fmt = data.split(":")[1]
                try:
                    await call.message.answer("[...] Готовлю файл кошельков…")
                    await send_wallets_file(call.message.bot, call.from_user.id, daemon, fmt)
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
                    reply_markup=InlineKeyboardMarkup(
                        inline_keyboard=[[InlineKeyboardButton(text="[ОБНОВИТЬ]", callback_data="ref")]]
                    ),
                    link_preview_options=_NO_PREVIEW,
                )
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
                    f"[{'ok' if ok else 'err'}] Фарм {_esc(slug)}: {'запущен' if ok else 'не запустился'}"
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
                await call.message.answer("[ok] Ферма остановлена." if ok else "[err] Остановить не удалось.")
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
                done = "выполнено" if ok else "уже в этой позиции / ферма недоступна"
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
    bot = Bot(token=cfg.telegram_token, default=_BOT_DEFAULTS)
    # Первый разрешённый владелец — получатель живого мониторинга по умолчанию.
    default_tg = cfg.telegram_allow_ids[0] if cfg.telegram_allow_ids else 0
    monitor = LiveMonitor(bot, default_tg, daemon)
    dp = build_dispatcher(cfg, daemon, monitor)
    logger.info("Telegram-бот запущен (long-polling)")
    # При каждом запуске шлём владельцу актуальный адрес портала,
    # чтобы смена адреса туннеля не терялась.
    if cfg.public_base_url:
        try:
            await notify_owner(
                cfg,
                "HARVEST запущен.\nСайт: " + cfg.public_base_url,
            )
        except Exception:  # noqa: BLE001
            logger.warning("Не удалось отправить адрес владельцу", exc_info=True)
    await dp.start_polling(bot)
