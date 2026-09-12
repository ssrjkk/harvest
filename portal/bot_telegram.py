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
