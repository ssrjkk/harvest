"""Telegram-бот HARVEST PORTAL: статистика, старт/стоп, пауза, ссылки,
кнопка Mini App. Long-polling (без открытых портов — дружит с любым хостингом).

Mini App открывается через WebAppInfo; Telegram сам дописывает initData к URL,
портальный фронтенд шлёт его в /api/tg/init и получает сессию.
"""

from __future__ import annotations

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
    Message,
    WebAppInfo,
)

from portal.config import PortalConfig, load_links
from portal.farm import FarmDaemon

logger = logging.getLogger(__name__)


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
    return "\n".join(lines)


def _fmt_history(rows: list[dict]) -> str:
    lines = ["🕓 <b>История циклов</b>", ""]
    if not rows:
        lines.append("Пусто — циклы ещё не завершались.")
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
    return "\n".join(lines)


async def notify_owner(cfg: PortalConfig, text: str) -> None:
    """Пуш-алерт владельцу (Telegram). Без токена/разрешённых — тихий no-op."""
    if not cfg.telegram_token or not cfg.telegram_allow_ids:
        return
    bot = Bot(token=cfg.telegram_token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
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
        f"Циклов: <b>{_esc(pool.get('cycles', 0))}</b>",
        f"Действий: <b>{_esc(pool.get('actions', 0))}</b>",
        f"Ошибок: <b>{_esc(pool.get('errors', 0))}</b>",
        f"Обработано: <b>{_esc(pool.get('processed', 0))}</b>",
        f"Дропов: <b>{_esc(pool.get('dropped', 0))}</b>",
        "",
        f"Воркеры: <b>{_esc(pool.get('dyn_workers', '-'))}</b> (health {pool.get('health', 1.0):.2f})",
        f"Успешных сборок БД: <b>{_esc(db.get('cycles', '-'))}</b>",
    ]
    return "\n".join(lines)


def _menu_kb(cfg: PortalConfig) -> InlineKeyboardMarkup:
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
                InlineKeyboardButton(text="🔗 Ссылки", callback_data="links"),
            ],
            [
                InlineKeyboardButton(text="🔧 Doctor", callback_data="doctor"),
                InlineKeyboardButton(text="🕓 История", callback_data="history"),
            ],
        ]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)


_HELP_TEXT = (
    "<b>HARVEST PORTAL</b> — единый пульт фермы.\n\n"
    "Возможности:\n"
    "• Статистика в реальном времени\n"
    "• Старт / Стоп фермы\n"
    "• Пауза / Резюм\n"
    "• Ссылки\n"
    "• /doctor — диагностика\n"
    "• /history — история циклов\n"
    "• Mini App (кнопка под меню)\n\n"
    "/start — главное меню\n/help — эта справка"
)


def build_dispatcher(cfg: PortalConfig, daemon: FarmDaemon) -> Dispatcher:
    dp = Dispatcher()

    @dp.message(CommandStart())
    async def on_start(message: Message) -> None:
        if not _allowed(cfg, message.from_user.id if message.from_user else None):
            await message.answer("⛔ Нет доступа.")
            return
        await message.answer(
            "Привет! Это <b>HARVEST PORTAL</b> — единый пульт фермы.\n"
            "Управляй фармом, смотри статистику и открывай Mini App.",
            parse_mode=ParseMode.HTML,
            reply_markup=_menu_kb(cfg),
        )

    @dp.message()
    async def on_help(message: Message) -> None:
        # Default-deny для ЛЮБЫХ сообщений, включая неизвестные команды:
        # посторонним бот не должен даже подтверждать, что он существует.
        if not _allowed(cfg, message.from_user.id if message.from_user else None):
            return
        if message.text and message.text.strip() == "/help":
            await message.answer(_HELP_TEXT, parse_mode=ParseMode.HTML, reply_markup=_menu_kb(cfg))
        elif message.text and message.text.strip() == "/doctor":
            await message.answer(
                _fmt_doctor(await daemon.statistics()),
                parse_mode=ParseMode.HTML,
                reply_markup=_menu_kb(cfg),
            )
        elif message.text and message.text.strip() == "/history":
            await message.answer(_fmt_history(await daemon.cycle_history()), parse_mode=ParseMode.HTML)
        elif message.text and message.text.startswith("/"):
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
                await call.message.edit_text(answer, parse_mode=ParseMode.HTML, reply_markup=kb)
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
                await call.message.edit_text("\n".join(lines), parse_mode=ParseMode.HTML)
                return

            if data == "doctor":
                answer = _fmt_doctor(await daemon.statistics())
                kb = InlineKeyboardMarkup(
                    inline_keyboard=[[InlineKeyboardButton(text="🔧 Обновить", callback_data="doctor")]]
                )
                await call.message.edit_text(answer, parse_mode=ParseMode.HTML, reply_markup=kb)
                return

            if data == "history":
                answer = _fmt_history(await daemon.cycle_history())
                kb = InlineKeyboardMarkup(
                    inline_keyboard=[[InlineKeyboardButton(text="🕓 Обновить", callback_data="history")]]
                )
                await call.message.edit_text(answer, parse_mode=ParseMode.HTML, reply_markup=kb)
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
    bot = Bot(token=cfg.telegram_token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = build_dispatcher(cfg, daemon)
    logger.info("Telegram-бот запущен (long-polling)")
    await dp.start_polling(bot)
