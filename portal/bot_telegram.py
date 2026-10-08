"""Telegram-бот HARVEST PORTAL: статистика, старт/стоп, пауза, ссылки,
кнопка Mini App, живой мониторинг с алертами. Long-polling (без открытых
портов — дружит с любым хостингом).

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

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import CommandStart
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    LinkPreviewOptions,
    Message,
    WebAppInfo,
)

from portal.config import PortalConfig, load_links
from portal.farm import FarmDaemon

logger = logging.getLogger(__name__)

# Отключает превью ссылок (t.me/ssrjkk_bot) во всех сообщениях бота.
_NO_PREVIEW = LinkPreviewOptions(is_disabled=True)
_BOT_DEFAULTS = DefaultBotProperties(parse_mode=ParseMode.HTML, link_preview_is_disabled=True)

# Интервал пульса монитора (сек) и его можно переопределить env.
_DEFAULT_MONITOR_INTERVAL_S = 600.0

# Ссылка на бота автора: добавляется в стартовое сообщение, help и статусы.
_AUTHOR_LINK = "👤 Автор: @ssrjkk_bot · <a href='https://t.me/ssrjkk_bot'>t.me/ssrjkk_bot</a>"


def _allowed(cfg: PortalConfig, tg_id: int | None) -> bool:
    # Default-deny: без TELEGRAM_ALLOW_IDS бот никому не отвечает (сознательное
    # исключение — PORTAL_OPEN_ACCESS). Раньше пустой список открывал бота ЛЮБОМУ.
    if not tg_id:
        return False
    return cfg.telegram_user_allowed(tg_id)


def _esc(value: object) -> str:
    """HTML-экранирование значений из daemon.statistics()/live_stats (defense-in-depth)."""
    return html.escape(str(value), quote=True)


def _fmt_doctor(data: dict) -> str:
    pool = data.get("pool", {})
    db = data.get("db", {})
    health = data.get("health_factor", 0.0)
    problems = []
    if not data.get("running"):
        problems.append("ферма не запущена")
    if not pool:
        problems.append("ядро фермы не подключено")
    if pool:
        if health and health < 0.5:
            problems.append(f"сеть деградировала (health {health:.2f})")
        errors = pool.get("errors", 0)
        actions = pool.get("actions", 0)
        if errors and errors > actions * 0.5 + 50:
            problems.append("накоплено много ошибок")
    lines = [
        "🔧 <b>HARVEST — диагностика</b>",
        "",
        "Ферма: " + ("▶ работает" if data.get("running") else "⏹ остановлена"),
        "Ядро: " + ("подключено" if pool else "НЕ ПОДКЛЮЧЕНО"),
        f"Health сети: {health:.2f}" + ("" if not health or health >= 0.5 else " ⚠️"),
    ]
    if pool:
        lines.append(
            f"Циклов: <b>{_esc(pool.get('cycles', 0))}</b> · "
            f"действий: <b>{_esc(pool.get('actions', 0))}</b> · "
            f"ошибок: <b>{_esc(pool.get('errors', 0))}</b>"
        )
        lines.append(
            f"Обработано: {_esc(pool.get('processed', 0))} · "
            f"воркеры: {_esc(pool.get('dyn_workers', '-'))}"
        )
    lines.append(f"БД: циклов в истории: <b>{_esc(db.get('cycles', 0))}</b>")
    lines.append("")
    if problems:
        lines.append("⚠️ Замечания:")
        lines.extend(f"• {p}" for p in problems)
    else:
        lines.append("✅ Всё штатно.")
    lines.append("")
    lines.append(_AUTHOR_LINK)
    return "\n".join(lines)


def _fmt_history(rows: list[dict]) -> str:
    lines = ["🕓 <b>История циклов</b>", ""]
    if not rows:
        lines.append("Пусто — циклы ещё не завершались.")
        lines.append("")
        lines.append(_AUTHOR_LINK)
        return "\n".join(lines)
    for row in rows[:8]:
        errs = row.get("errors") or 0
        mark = "✅" if errs == 0 else "⚠️"
        started = str(row.get("started_at") or "-")[:19]
        lines.append(
            f"{mark} #{_esc(row.get('id'))} · {_esc(started)} · "
            f"{float(row.get('duration_s') or 0):.0f}с · "
            f"кошельков {_esc(row.get('wallets'))} (ок {_esc(row.get('wallets_ok'))}) · "
            f"действий {_esc(row.get('actions_ok'))} · ошибок {_esc(errs)}"
        )
    lines.append("")
    lines.append(_AUTHOR_LINK)
    return "\n".join(lines)


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


def _fmt_stats(data: dict) -> str:
    pool = data.get("pool", {})
    db = data.get("db", {})
    lines = [
        "📊 <b>HARVEST — статус</b>",
        "",
        f"Ферма: <b>{'▶ РАБОТАЕТ' if data['running'] else '⏹ ОСТАНОВЛЕНА'}</b>",
        f"Пауза: <b>{'⏸ да' if pool.get('paused') else '✅ нет'}</b>",
        "",
        f"Кошельков: <b>{_esc(data.get('wallet_count', '-'))}</b>",
        f"Циклов: <b>{_esc(pool.get('cycles', 0))}</b>",
        f"Действий: <b>{_esc(pool.get('actions', 0))}</b>",
        f"Ошибок: <b>{_esc(pool.get('errors', 0))}</b>",
        f"Обработано: <b>{_esc(pool.get('processed', 0))}</b>",
        f"Дропов: <b>{_esc(pool.get('dropped', 0))}</b>",
        "",
        f"Воркеры: <b>{_esc(pool.get('dyn_workers', '-'))}</b> (health {pool.get('health', 1.0):.2f})",
        f"Успешных сборок БД: <b>{_esc(db.get('cycles', '-'))}</b>",
        "",
        _progress_bar(pool.get("processed", 0), data.get("wallet_count", 0), "обработано кошельков"),
        "",
        _AUTHOR_LINK,
    ]
    return "\n".join(lines)


def _fmt_status(data: dict) -> str:
    """Богатый статус как на сайте: успех %, средние за цикл, health, кошельки."""
    pool = data.get("pool", {})
    db = data.get("db", {})
    actions = int(pool.get("actions", 0) or 0)
    errors = int(pool.get("errors", 0) or 0)
    cycles = int(pool.get("cycles", 0) or 0)
    total = actions + errors
    rate = f"{actions / total * 100:.1f}%" if total else "—"
    per_cycle = f"{actions / cycles:.1f}" if cycles else "—"
    state = "▶ РАБОТАЕТ" if data.get("running") else "⏹ ОСТАНОВЛЕНА"
    if pool.get("paused"):
        state += " ⏸"
    lines = [
        "📈 <b>HARVEST — живой статус</b>",
        "",
        f"Ферма: <b>{state}</b>",
        f"Health сети: <b>{data.get('health_factor', 0.0):.2f}</b>",
        "",
        f"• Кошельков в БД: <b>{_esc(data.get('wallet_count', '-'))}</b>",
        f"• Действий: <b>{_esc(actions)}</b> · ошибок: <b>{_esc(errors)}</b>",
        f"• Успешность: <b>{_esc(rate)}</b>",
        f"• Обработано: <b>{_esc(pool.get('processed', 0))}</b>",
        "",
        f"• Циклов: <b>{_esc(cycles)}</b> · действий/цикл: <b>{_esc(per_cycle)}</b>",
        f"• Воркеры: <b>{_esc(pool.get('dyn_workers', '-'))}</b>",
        f"• Циклов в истории: <b>{_esc(db.get('cycles', '-'))}</b>",
        "",
        _progress_bar(pool.get("processed", 0), data.get("wallet_count", 0), "обработано кошельков"),
        "",
        _AUTHOR_LINK,
    ]
    return "\n".join(lines)


def _progress_bar(done: int, total: int, label: str, width: int = 10) -> str:
    """Прогресс-бар: ▰▰▰▱▱ 60% (12/20). label — подпись."""
    try:
        total = int(total or 0)
    except (TypeError, ValueError):
        total = 0
    try:
        done = int(done or 0)
    except (TypeError, ValueError):
        done = 0
    pct = (done / total * 100) if total else 0
    filled = round(pct / 100 * width)
    bar = "▰" * filled + "▱" * (width - filled)
    return f"<b>{label}</b>\n{bar} {pct:.0f}% ({done}/{total})"


def _fmt_wallets_page(wallets: list[dict], page: int = 1, per_page: int = 4) -> tuple[str, int]:
    """Кошельки с ключами и сид-фразами (скрыты спойлером). Возвращает (текст, страниц)."""
    total = len(wallets)
    pages = max(1, (total + per_page - 1) // per_page)
    page = max(1, min(page, pages))
    start = (page - 1) * per_page
    chunk = wallets[start : start + per_page]
    lines = [f"💼 <b>Кошельки</b> · всего {total} · стр. {page}/{pages}", ""]
    if not chunk:
        lines.append("Кошельков пока нет — запусти фарм в боте, он создаст.")
    for i, w in enumerate(chunk, start + 1):
        addr = _esc(w.get("address", ""))
        acts = _esc(w.get("total_actions", 0))
        key = _esc(w.get("private_key", ""))
        mn = _esc(w.get("mnemonic", "") or "—")
        lines.append(f"<b>#{i}</b> · <code>{addr}</code> · <i>{acts} действ.</i>")
        lines.append(f"🔑 Ключ: <spoiler>{key}</spoiler>")
        lines.append(f"🌱 Сид: <spoiler>{mn}</spoiler>")
        lines.append("")
    lines.append("Нажми на 🔑/🌱, чтобы открыть. Секреты видны только вам.")
    return "\n".join(lines), pages


def _wallets_kb(page: int, pages: int) -> InlineKeyboardMarkup:
    row = []
    if page > 1:
        row.append(InlineKeyboardButton(text="◀", callback_data=f"wallets:page:{page-1}"))
    if page < pages:
        row.append(InlineKeyboardButton(text="▶", callback_data=f"wallets:page:{page+1}"))
    return InlineKeyboardMarkup(inline_keyboard=[row]) if row else InlineKeyboardMarkup(inline_keyboard=[])


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
            text = "⛔ <b>Ферма остановилась.</b>\n/status — детали"
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
        caption=f"💼 Кошельки ({fmt.upper()}) — {len(content):,} байт",
    )


def _menu_kb(cfg: PortalConfig, monitor_on: bool = False) -> InlineKeyboardMarkup:
    rows = []
    if cfg.public_base_url:
        rows.append([InlineKeyboardButton(text="🚀 Открыть Mini App", web_app=WebAppInfo(url=cfg.public_base_url))])
    rows.extend(
        [
            [
                InlineKeyboardButton(text="▶️ Старт", callback_data="farm:start"),
                InlineKeyboardButton(text="⏹ Стоп", callback_data="farm:stop"),
            ],
            [
                InlineKeyboardButton(text="⏸ Пауза", callback_data="farm:pause"),
                InlineKeyboardButton(text="▶️ Резюм", callback_data="farm:resume"),
            ],
            [
                InlineKeyboardButton(text="📊 Статистика", callback_data="stats"),
                InlineKeyboardButton(text="📈 Живой статус", callback_data="status"),
            ],
            [
                InlineKeyboardButton(text="🔧 Doctor", callback_data="doctor"),
                InlineKeyboardButton(text="🕓 История", callback_data="history"),
            ],
            [
                InlineKeyboardButton(text="💼 Кошельки (CSV)", callback_data="export:csv"),
                InlineKeyboardButton(text="💼 Кошельки (JSON)", callback_data="export:json"),
            ],
            [
                InlineKeyboardButton(text="💼 Список кошельков", callback_data="wallets:page:1"),
            ],
            [
                InlineKeyboardButton(
                    text="🔔 Мониторинг: ВКЛ" if monitor_on else "🔕 Мониторинг: ВЫКЛ",
                    callback_data="monitor:toggle",
                ),
                InlineKeyboardButton(text="🔗 Ссылки", callback_data="links"),
            ],
        ]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)


_HELP_TEXT = (
    "<b>HARVEST PORTAL</b> — единый пульт фермы.\n\n"
    "Возможности:\n"
    "• Статистика в реальном времени\n"
    "• /status — развёрнутый статус (успех %, средние, health)\n"
    "• /monitor — живой мониторинг с алертами (периодический статус + алерт об остановке)\n"
    "• Старт / Стоп фермы\n"
    "• Пауза / Резюм\n"
    "• Кошельки — экспорт всех кошельков с ключами (CSV/JSON)\n"
    "• /wallets — список кошельков с ключами и сид-фразами\n"
    "• Ссылки\n"
    "• /doctor — диагностика\n"
    "• /history — история циклов\n"
    "• /export — скачать все кошельки (CSV)\n"
    "• Mini App (кнопка под меню)\n\n"
    "/start — главное меню\n/help — эта справка\n\n"
    + _AUTHOR_LINK
)


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
            await answer("🔕 Живой мониторинг выключен.")
        else:
            await monitor.start()
            await answer(
                "🔔 Живой мониторинг включён.\n"
                f"Статус буду присылать каждые {monitor.interval_s:.0f} с, "
                "при остановке фермы — сразу."
            )

    _asyncio.create_task(_act())


def build_dispatcher(cfg: PortalConfig, daemon: FarmDaemon, monitor: LiveMonitor | None = None) -> Dispatcher:
    dp = Dispatcher()
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
            "Управляй фармом, смотри статистику, включай живой мониторинг.\n\n"
            + _AUTHOR_LINK,
            parse_mode=ParseMode.HTML,
            reply_markup=_menu(),
        )

    @dp.message()
    async def on_help(message: Message) -> None:
        # Default-deny для ЛЮБЫХ сообщений, включая неизвестные команды:
        # посторонним бот не должен даже подтверждать, что он существует.
        if not _allowed(cfg, message.from_user.id if message.from_user else None):
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
            await message.answer(content, parse_mode=ParseMode.HTML, reply_markup=_wallets_kb(page, pages))
        elif text.startswith("/monitor"):
            _toggle_monitor(message.bot, message.from_user.id, daemon, monitor, message.answer)
        elif text.startswith("/export"):
            parts = text.split()
            fmt = parts[1].lower() if len(parts) > 1 and parts[1].lower() in {"json"} else "csv"
            try:
                await send_wallets_file(message.bot, message.from_user.id, daemon, fmt)
            except Exception:  # noqa: BLE001
                logger.warning("Экспорт кошельков через /export упал", exc_info=True)
                await message.answer("⚠️ Не удалось сформировать файл кошельков.")
        elif text.startswith("/"):
            await message.answer("Неизвестная команда. /help — список возможностей.")

    @dp.callback_query()
    async def on_callback(call: CallbackQuery) -> None:
        if not _allowed(cfg, call.from_user.id if call.from_user else None):
            await call.answer("⛔ Нет доступа.", show_alert=True)
            return
        data = call.data or ""
        try:
            await call.answer()
        except Exception:  # noqa: BLE001
            pass

        try:
            if data == "stats":
                answer = _fmt_stats(await daemon.statistics())
                kb = InlineKeyboardMarkup(
                    inline_keyboard=[[InlineKeyboardButton(text="📊 Обновить", callback_data="stats")]]
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
                    inline_keyboard=[[InlineKeyboardButton(text="📈 Обновить", callback_data="status")]]
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
                kb = _wallets_kb(page, pages)
                await call.message.edit_text(content, parse_mode=ParseMode.HTML, reply_markup=kb)
                return

            if data == "links":
                links = load_links(cfg.links_path)
                title = html.escape(str(links.get("title", "Наши ссылки"))[:200])
                lines = [f"🔗 <b>{title}</b>", ""]
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
                    inline_keyboard=[[InlineKeyboardButton(text="🔧 Обновить", callback_data="doctor")]]
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
                    inline_keyboard=[[InlineKeyboardButton(text="🕓 Обновить", callback_data="history")]]
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
                    await call.message.answer("📦 Готовлю файл кошельков…")
                    await send_wallets_file(call.message.bot, call.from_user.id, daemon, fmt)
                except Exception:  # noqa: BLE001
                    logger.warning("Экспорт кошельков (кнопка) упал", exc_info=True)
                    await call.message.answer("⚠️ Не удалось сформировать файл кошельков.")
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
                await call.message.answer(
                    f"{'✅' if ok else '⚠️'} {label}: {'выполнено' if ok else 'уже в этой позиции / ферма недоступна'}"
                )
                return

            await call.message.answer("Неизвестная команда.", reply_markup=_menu_kb(cfg))
        except Exception:  # noqa: BLE001
            # Сбой daemon/БД не должен молча глотаться aiogram-диспетчером:
            # пишем в журнал и отвечаем пользователю generic-текстом (без деталей).
            logger.warning("callback %r упал", data, exc_info=True)
            try:
                await call.message.answer("⚠️ Не удалось выполнить команду. Попробуйте позже.")
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
                "🌾 HARVEST запущен.\nСайт: " + cfg.public_base_url + "\nЛогин: мастер-ключ (FARMER_MASTER_KEY)",
            )
        except Exception:  # noqa: BLE001
            logger.warning("Не удалось отправить адрес владельцу", exc_info=True)
    await dp.start_polling(bot)
