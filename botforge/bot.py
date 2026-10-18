"""harvest BOT FORGE — автономное поле фермы (24/7 в облаке, без ПК).

Читает state.json с GitHub Pages (метрики фермы, которые публикует
publish_dash.py с твоего ПК). Кошельки с ключами и сид-фразами читает
из облачной БД MotherDuck (синхронизирует sync_motherduck.py) —
поэтому /wallets работает даже когда ПК выключен.

Переменные окружения:
  BOT_TOKEN           — токен от @BotFather (обязательно)
  STATE_URL           — URL state.json (по умолчанию https://ssrjkk.github.io/harvest/state.json)
  MOTHERDUCK_TOKEN    — токен MotherDuck для чтения кошельков (опционально)
  TELEGRAM_ALLOW_IDS  — разрешённые Telegram user_id через запятую (default-deny!)
  TELEGRAM_OPEN_ACCESS=1 — сознательно открыть доступ всем (не рекомендуется)
"""

import asyncio
import os
import sys

import aiohttp
from aiogram import Bot, Dispatcher
from aiogram.enums import ParseMode
from aiogram.filters import CommandStart
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, Message

_TOKEN = os.environ.get("BOT_TOKEN", "").strip()
if not _TOKEN:
    print("BOT_TOKEN is required (see botforge/README.md)", file=sys.stderr)
    raise SystemExit(2)
TOKEN = _TOKEN
STATE_URL = os.environ.get(
    "STATE_URL", "https://ssrjkk.github.io/harvest/state.json"
)
AUTHOR = "👤 Автор: <a href='https://t.me/ssrjkk_bot'>@ssrjkk_bot</a>"
PER_PAGE = 4

# Default-deny: доступ только перечисленным user_id. Без списка (и без явного
# TELEGRAM_OPEN_ACCESS=1) бот отказывает всем — /wallets отдаёт приватные ключи,
# поэтому открытый доступ недопустим.
# Сохраняем знак: "-100..." (каналы) — валидный ID.
ALLOW_IDS = set()
for _raw in os.environ.get("TELEGRAM_ALLOW_IDS", "").split(","):
    _s = _raw.strip()
    if not _s:
        continue
    _num = _s[1:] if _s[:1] in {"+", "-"} else _s
    if _num.isdigit():
        try:
            ALLOW_IDS.add(int(_s))
        except ValueError:
            continue
OPEN_ACCESS = os.environ.get("TELEGRAM_OPEN_ACCESS", "").strip().lower() in {"1", "true", "yes"}

DENY_MSG = "⛔ Нет доступа. Напиши автору: https://t.me/ssrjkk_bot"

COMMANDS = [
    ("start", "Поле — меню"),
    ("status", "Статус фермы"),
    ("history", "История циклов"),
    ("doctor", "Диагностика"),
    ("wallets", "Кошельки"),
    ("help", "Справка"),
]


def allowed(user_id) -> bool:
    if user_id is None:
        return False
    if OPEN_ACCESS:
        return True
    return user_id in ALLOW_IDS


def esc(v):
    return str(v or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


async def fetch_state():
    async with aiohttp.ClientSession() as session:
        async with session.get(STATE_URL, timeout=aiohttp.ClientTimeout(total=20)) as r:
            if r.status != 200:
                raise RuntimeError(f"state.json HTTP {r.status}")
            try:
                data = await r.json(content_type=None)
            except Exception as e:
                raise RuntimeError(f"state.json bad JSON: {e}") from e
            if not isinstance(data, dict):
                raise RuntimeError("state.json bad shape")
            return data


def _fetch_wallets_sync():
    """Синхронная версия: duckdb не async, поэтому вызываем через to_thread."""
    token = os.environ.get("MOTHERDUCK_TOKEN", "").strip()
    if not token:
        return None
    import duckdb

    con = None
    try:
        # Токен передаём через config, чтобы не светить в URL/ошибках.
        con = duckdb.connect("md:harvest", config={"motherduck_token": token})
        rows = con.execute(
            "SELECT address, private_key, mnemonic, total_actions "
            "FROM wallets ORDER BY total_actions DESC"
        ).fetchall()
    finally:
        try:
            if con is not None:
                con.close()
        except Exception:
            pass
    return [
        {"address": r[0], "private_key": r[1], "mnemonic": r[2], "total_actions": r[3]}
        for r in rows
    ]


async def fetch_wallets():
    """Кошельки из MotherDuck (облако). None если токена нет."""
    if not os.environ.get("MOTHERDUCK_TOKEN"):
        return None
    return await asyncio.to_thread(_fetch_wallets_sync)


def progress_bar(done, total, width=10):
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
    return f"{bar} {pct:.0f}% ({done}/{total})"


def fmt_status(s):
    pool = s.get("pool", {}) if isinstance(s, dict) else {}
    state = "▶ работает" if s.get("running") else "⏹ остановлена"
    if pool.get("paused"):
        state += " ⏸"
    try:
        hf = float(s.get("health_factor", 1.0))
    except (TypeError, ValueError):
        hf = 1.0
    return (
        "📊 <b>harvest — статус</b>\n\n"
        f"Ферма: <b>{state}</b>\n"
        f"• Действий: <b>{pool.get('actions', 0)}</b> · ошибок: <b>{pool.get('errors', 0)}</b>\n"
        f"• Обработано: <b>{pool.get('processed', 0)}</b> · циклов: <b>{pool.get('cycles', 0)}</b>\n"
        f"• Кошельков: <b>{s.get('wallet_count', '-')}</b> · health: <b>{hf:.2f}</b>\n"
        f"Прогресс: <b>{progress_bar(pool.get('processed', 0), s.get('wallet_count', 0))}</b>\n"
        f"• Обновлено: <b>{esc(s.get('updated_at', '-'))}</b>\n\n"
        f"{AUTHOR}"
    )


def fmt_history(s):
    rows = s.get("history", []) if isinstance(s, dict) else []
    if not rows:
        return "История пуста — циклов пока не было."
    lines = ["<b>harvest // история</b> (последние 10)"]
    for c in rows[:10]:
        if not isinstance(c, dict):
            continue
        lines.append(
            f"#{esc(c.get('id', '?'))} · ok {esc(c.get('actions_ok', 0))} · "
            f"err {esc(c.get('errors', 0))} · wal {esc(c.get('wallets', 0))}"
        )
    return "\n".join(lines)


def fmt_doctor(s):
    pool = s.get("pool", {}) if isinstance(s, dict) else {}
    errs = pool.get("errors", 0)
    try:
        hf = float(s.get("health_factor", 1.0))
    except (TypeError, ValueError):
        hf = 1.0
    lines = ["<b>harvest // диагностика</b>"]
    lines.append(f"ферма: {'RUN' if s.get('running') else 'STOP'}{' + PAUSE' if pool.get('paused') else ''}")
    lines.append(f"ошибок: {errs} · health: {hf:.2f}")
    lines.append(f"кошельков: {s.get('wallet_count', '-')}")
    lines.append(f"обновлено: {esc(s.get('updated_at', '-'))}")
    if errs and hf < 0.5:
        lines.append("совет: /status + проверь RPC и кран")
    return "\n".join(lines)


HELP_TEXT = (
    "Возможности:\n"
    "• /status — состояние фермы + прогресс\n"
    "• /history — последние циклы\n"
    "• /doctor — диагностика\n"
    "• /wallets — кошельки (ключи и сид-фразы)\n"
    "• /start — меню\n\n" + AUTHOR
)


def fmt_wallets_page(wallets, page=1):
    total = len(wallets)
    pages = max(1, (total + PER_PAGE - 1) // PER_PAGE)
    page = max(1, min(page, pages))
    start = (page - 1) * PER_PAGE
    chunk = wallets[start : start + PER_PAGE]
    lines = [f"💼 <b>Кошельки</b> · всего {total} · стр. {page}/{pages}", ""]
    if not chunk:
        lines.append("Кошельков пока нет — запусти фарм.")
    for i, w in enumerate(chunk, start + 1):
        addr = esc(w.get("address", ""))
        acts = esc(w.get("total_actions", 0))
        key = esc(w.get("private_key", ""))
        mn = esc(w.get("mnemonic", "") or "—")
        lines.append(f"<b>#{i}</b> · <code>{addr}</code> · <i>{acts} действ.</i>")
        lines.append(f"🔑 Ключ: <spoiler>{key}</spoiler>")
        lines.append(f"🌱 Сид: <spoiler>{mn}</spoiler>")
        lines.append("")
    lines.append("Нажми на 🔑/🌱, чтобы открыть. Секреты видны только вам.")
    return "\n".join(lines), pages


def wallets_kb(page, pages):
    row = []
    if page > 1:
        row.append(InlineKeyboardButton(text="◀", callback_data=f"wl:{page - 1}"))
    if page < pages:
        row.append(InlineKeyboardButton(text="▶", callback_data=f"wl:{page + 1}"))
    return InlineKeyboardMarkup(inline_keyboard=[row]) if row else None


async def main():
    from aiogram.client.default import DefaultBotProperties

    bot = Bot(token=TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher()

    async def _setup_commands() -> None:
        try:
            from aiogram.types import BotCommand, BotCommandScopeAllPrivateChats

            await bot.set_my_commands(
                [BotCommand(command=c, description=d) for c, d in COMMANDS],
                scope=BotCommandScopeAllPrivateChats(),
            )
        except Exception:  # noqa: BLE001
            pass

    @dp.message(CommandStart())
    async def on_start(m: Message) -> None:
        if not allowed(m.from_user.id if m.from_user else None):
            await m.answer(DENY_MSG, parse_mode=ParseMode.HTML)
            return
        await m.answer(
            "Привет! Это <b>harvest</b> — поле фермы.\n"
            "• /status — текущее состояние + прогресс\n"
            "• /history — последние циклы\n"
            "• /doctor — диагностика\n"
            "• /wallets — кошельки с ключами и сид-фразами\n"
            "• /help — справка\n\n"
            + AUTHOR,
            parse_mode=ParseMode.HTML,
        )

    @dp.message()
    async def on_msg(m: Message) -> None:
        if not allowed(m.from_user.id if m.from_user else None):
            await m.answer(DENY_MSG, parse_mode=ParseMode.HTML)
            return
        text = (m.text or "").strip()
        if text == "/status":
            try:
                await m.answer(fmt_status(await fetch_state()), parse_mode=ParseMode.HTML)
            except Exception:  # noqa: BLE001
                await m.answer("⚠️ Не удалось получить состояние фермы.", parse_mode=ParseMode.HTML)
        elif text == "/history":
            try:
                await m.answer(fmt_history(await fetch_state()), parse_mode=ParseMode.HTML)
            except Exception:  # noqa: BLE001
                await m.answer("⚠️ Не удалось получить историю.", parse_mode=ParseMode.HTML)
        elif text == "/doctor":
            try:
                await m.answer(fmt_doctor(await fetch_state()), parse_mode=ParseMode.HTML)
            except Exception:  # noqa: BLE001
                await m.answer("⚠️ Не удалось получить диагностику.", parse_mode=ParseMode.HTML)
        elif text == "/wallets":
            try:
                wallets = await fetch_wallets()
            except Exception:  # noqa: BLE001
                await m.answer("⚠️ Не удалось прочитать кошельки из облака.", parse_mode=ParseMode.HTML)
                return
            if wallets is None:
                await m.answer(
                    "💼 Кошельки доступны из облака, когда ПК включён.\n"
                    "Чтобы видеть их всегда — добавь MOTHERDUCK_TOKEN в Secrets.\n\n" + AUTHOR,
                    parse_mode=ParseMode.HTML,
                )
                return
            content, pages = fmt_wallets_page(wallets, 1)
            await m.answer(
                content, parse_mode=ParseMode.HTML, reply_markup=wallets_kb(1, pages)
            )
        elif text == "/help":
            await m.answer(HELP_TEXT, parse_mode=ParseMode.HTML)

    @dp.callback_query()
    async def on_cb(cb) -> None:
        if not allowed(cb.from_user.id if cb.from_user else None):
            await cb.answer(DENY_MSG, show_alert=True)
            return
        if not cb.data or not cb.data.startswith("wl:"):
            return
        try:
            page = int((cb.data.split(":") + [""])[1])
        except (TypeError, ValueError):
            await cb.answer("bad page", show_alert=True)
            return
        try:
            wallets = await fetch_wallets()
        except Exception:  # noqa: BLE001
            await cb.answer("Кошельки недоступны", show_alert=True)
            return
        if wallets is None:
            await cb.answer("Кошельки недоступны", show_alert=True)
            return
        content, pages = fmt_wallets_page(wallets, page)
        await cb.message.edit_text(
            content, parse_mode=ParseMode.HTML, reply_markup=wallets_kb(page, pages)
        )
        await cb.answer()

    print("harvest BotForge started, polling...")
    await _setup_commands()
    await dp.start_polling(bot)


if __name__ == "__main__":
    import asyncio

    asyncio.run(main())
