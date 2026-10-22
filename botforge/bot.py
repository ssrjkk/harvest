"""harvest BOT FORGE — автономное поле фермы (24/7 в облаке, без ПК).

Читает state.json с GitHub Pages (метрики фермы, которые публикует
publish_dash.py с твоего ПК). Кошельки с ключами и сид-фразами читает
из шифрованного `wallets.enc.json` там же (собирает publish_dash.py) —
поэтому /wallets работает даже когда ПК выключен. На публичном хосте
лежит только шифротекст; ключ никуда не пишется, только в Secrets.

Переменные окружения:
  BOT_TOKEN           — токен от @BotFather (обязательно)
  STATE_URL           — URL state.json (по умолчанию https://ssrjkk.github.io/harvest/state.json)
  WALLETS_URL         — URL wallets.enc.json (по умолчанию рядом с STATE_URL)
  WALLETS_KEY         — 64 hex, ключ шифрования блоба (опционально, но без
                        него /wallets недоступен; тот же ключ в env на ПК)
  TELEGRAM_ALLOW_IDS  — разрешённые Telegram user_id через запятую (default-deny!)
  TELEGRAM_OPEN_ACCESS=1 — сознательно открыть доступ всем (не рекомендуется)
"""

import asyncio
import os
import sys
from datetime import datetime, timezone

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
WALLETS_URL = os.environ.get(
    "WALLETS_URL", "https://ssrjkk.github.io/harvest/wallets.enc.json"
)
# Ключ блоба: строго env, 64 hex. Формат блоба — tools/sync_wallets_blob.py:
# {"v":1,"alg":"AES-256-GCM","nonce":hex,"ct":hex}, AAD b"wallets-blob-v1".
_WALLETS_AAD = b"wallets-blob-v1"
_WALLETS_TTL = 300  # секунд: кэш расшифрованных кошельков в памяти бота
_WALLETS_CACHE: dict = {"at": 0.0, "etag": None, "rows": None}
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
    ("pnl", "PnL последнего прогона"),
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


def _wallets_key() -> bytes | None:
    """Ключ из env или None (тогда /wallets недоступен, как и раньше без токена)."""
    s = os.environ.get("WALLETS_KEY", "").strip()
    if s[:2].lower() == "0x":
        s = s[2:]
    try:
        key = bytes.fromhex(s)
    except ValueError:
        return None
    return key if len(key) == 32 else None


def _decrypt_blob(blob: dict, key: bytes) -> list:
    from cryptography.exceptions import InvalidTag
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    if not isinstance(blob, dict) or blob.get("v") != 1:
        raise ValueError("bad wallets blob version")
    try:
        nonce = bytes.fromhex(blob["nonce"])
        ct = bytes.fromhex(blob["ct"])
        pt = AESGCM(key).decrypt(nonce, ct, _WALLETS_AAD)
        rows = __import__("json").loads(pt.decode("utf-8"))
    except (ValueError, InvalidTag) as e:
        raise ValueError(f"bad wallets blob: {e}") from e
    if not isinstance(rows, list):
        raise ValueError("bad wallets blob shape")
    return rows


async def fetch_wallets():
    """Кошельки из шифрованного wallets.enc.json (gh-pages). None если нет ключа.

    Кэш в памяти на _WALLETS_TTL: листание /wallets на 1000 кошельков
    (250 страниц) не качает блоб заново на каждую страницу.
    """
    import time as _time

    key = _wallets_key()
    if key is None:
        return None
    now = _time.monotonic()
    if _WALLETS_CACHE["rows"] is not None and now - _WALLETS_CACHE["at"] < _WALLETS_TTL:
        return _WALLETS_CACHE["rows"]
    headers = {}
    if _WALLETS_CACHE["etag"]:
        headers["If-None-Match"] = _WALLETS_CACHE["etag"]
    async with aiohttp.ClientSession() as session:
        async with session.get(WALLETS_URL, headers=headers,
                               timeout=aiohttp.ClientTimeout(total=20)) as r:
            if r.status == 304 and _WALLETS_CACHE["rows"] is not None:
                _WALLETS_CACHE["at"] = now
                return _WALLETS_CACHE["rows"]
            if r.status == 404:
                raise RuntimeError("wallets blob not published yet (404)")
            if r.status != 200:
                raise RuntimeError(f"wallets blob HTTP {r.status}")
            try:
                blob = await r.json(content_type=None)
            except Exception as e:
                raise RuntimeError(f"wallets blob bad JSON: {e}") from e
    rows = _decrypt_blob(blob, key)
    rows = [w for w in rows
            if isinstance(w, dict) and isinstance(w.get("address"), str) and w.get("address")]
    rows.sort(key=lambda w: w.get("total_actions", 0) or 0, reverse=True)
    _WALLETS_CACHE.update(at=now, etag=r.headers.get("ETag"), rows=rows,
                          built_at=blob.get("built_at") if isinstance(blob, dict) else None)
    return [
        {"address": w.get("address", ""), "private_key": w.get("private_key", ""),
         "mnemonic": w.get("mnemonic"), "total_actions": w.get("total_actions", 0)}
        for w in rows
    ]


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


def _hf_text(s) -> str:
    """health для витрины: число — с двумя знаками, неизвестно — «—»."""
    try:
        v = s.get("health_factor")
        return "—" if v is None else f"{float(v):.2f}"
    except (TypeError, ValueError):
        return "—"


def _snapshot_age_min(updated_at) -> float | None:
    """Возраст снимка state.json в минутах. None — не распарсили.

    Принимает и полный штамп SQLite («… HH:MM:SS»), и короткий stamp
    публикации («… HH:MM UTC», секунд нет).
    """
    raw = str(updated_at or "").strip()
    for suf in (" UTC", "UTC", " GMT", "GMT"):
        if raw.endswith(suf):
            raw = raw[: -len(suf)].strip()
            break
    ts = None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
        try:
            ts = datetime.strptime(raw, fmt).replace(tzinfo=timezone.utc)
            break
        except (TypeError, ValueError):
            continue
    if ts is None:
        return None
    return (datetime.now(timezone.utc) - ts).total_seconds() / 60


def _freshness_note(s) -> str:
    """Свежесть снимка для статусов: «5 мин назад» + флаг протухания.

    Порог тот же, что на сайте (45 мин): старше — честное «ферма молчит»,
    а не вид живой витрины.
    """
    age = _snapshot_age_min(s.get("updated_at"))
    if age is None or age < 0:
        return ""
    if age < 1:
        ago = "только что"
    elif age < 60:
        ago = f"{int(age)} мин назад"
    else:
        ago = f"{int(age // 60)} ч {int(age % 60):02d} мин назад"
    stale = " · ⚠️ снимок старый, ферма молчит" if age > 45 else ""
    return f"({ago}{stale})"


def fmt_status(s):
    pool = s.get("pool", {}) if isinstance(s, dict) else {}
    state = "▶ работает" if s.get("running") else "⏹ остановлена"
    if pool.get("paused"):
        state += " ⏸"
    hf = _hf_text(s)
    return (
        "📊 <b>harvest — статус</b>\n\n"
        f"Ферма: <b>{state}</b>\n"
        f"• Действий: <b>{pool.get('actions', 0)}</b> · ошибок: <b>{pool.get('errors', 0)}</b>\n"
        f"• Обработано: <b>{pool.get('processed', 0)}</b> · циклов: <b>{pool.get('cycles', 0)}</b>\n"
        f"• Кошельков: <b>{s.get('wallet_count', '-')}</b> · health: <b>{hf}</b>\n"
        f"Прогресс: <b>{progress_bar(pool.get('processed', 0), s.get('wallet_count', 0))}</b>\n"
        f"• Обновлено: <b>{esc(s.get('updated_at', '-'))}</b> {esc(_freshness_note(s))}\n\n"
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
    hf = _hf_text(s)
    lines = ["<b>harvest // диагностика</b>"]
    lines.append(f"ферма: {'RUN' if s.get('running') else 'STOP'}{' + PAUSE' if pool.get('paused') else ''}")
    lines.append(f"ошибок: {errs} · health: {hf}")
    lines.append(f"кошельков: {s.get('wallet_count', '-')}")
    lines.append(f"обновлено: {esc(s.get('updated_at', '-'))} {esc(_freshness_note(s))}")
    if errs and hf != "—":
        try:
            bad = float(hf) < 0.5
        except ValueError:
            bad = False
        if bad:
            lines.append("совет: /status + проверь RPC и кран")
    return "\n".join(lines)


def fmt_pnl(s):
    """PnL последнего прогона: агрегаты из state.json (без адресов)."""
    p = s.get("pnl") if isinstance(s, dict) else None
    if not isinstance(p, dict) or not p:
        return "PnL пока нет — прогон ещё не публиковал отчёт."
    ok = p.get("actions_ok", 0)
    fail = p.get("actions_fail", 0)
    total = ok + fail
    rate = (ok / total * 100) if total else 0

    def _num(v):
        try:
            return float(v)
        except (TypeError, ValueError):
            return None

    farm = _num(p.get("farm_delta"))
    faucet = _num(p.get("faucet_delta"))
    lines = ["<b>harvest // PnL прогона</b>"]
    lines.append(f"• Успешных действий: <b>{esc(ok)}</b> · отказов: <b>{esc(fail)}</b> ({rate:.1f}%)")
    lines.append(f"• Δ фарма: <b>{farm:+.6f}</b>" if farm is not None else "• Δ фарма: <b>—</b>")
    lines.append(f"• Δ крана: <b>{faucet:+.6f}</b>" if faucet is not None else "• Δ крана: <b>—</b>")
    lines.append(f"• Выборка: {esc(p.get('measured', 0))}/{esc(p.get('total_wallets', 0))} кошельков")
    reasons = p.get("by_reason") or []
    if reasons:
        lines.append("")
        lines.append("<b>Причины отказов</b>")
        for r in reasons[:5]:
            if not isinstance(r, dict):
                continue
            lines.append(f"• {esc(r.get('reason', '?'))} — {esc(r.get('count', 0))}")
    lines.append("")
    lines.append(f"• Обновлено: {esc(p.get('created_at', '-'))}")
    return "\n".join(lines)


HELP_TEXT = (
    "Возможности:\n"
    "• /status — состояние фермы + прогресс\n"
    "• /pnl — PnL последнего прогона\n"
    "• /history — последние циклы\n"
    "• /doctor — диагностика\n"
    "• /wallets — кошельки (ключи и сид-фразы)\n"
    "• /start — меню\n\n"
    "Кнопки под сообщениями — переход между разделами без набора команд.\n\n" + AUTHOR
)


def nav_kb(current: str = "status") -> InlineKeyboardMarkup:
    """Быстрая навигация по панелям. Тап по открытой — обновить её же."""
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⟳ Обновить", callback_data=f"pg:refresh:{current}")],
        [InlineKeyboardButton(text="Статус", callback_data="pg:status"),
         InlineKeyboardButton(text="PnL", callback_data="pg:pnl")],
        [InlineKeyboardButton(text="История", callback_data="pg:history"),
         InlineKeyboardButton(text="Доктор", callback_data="pg:doctor")],
    ])


_PANELS = {
    "status": fmt_status,
    "pnl": fmt_pnl,
    "history": fmt_history,
    "doctor": fmt_doctor,
}


def panel_kind(data) -> str | None:
    """Какой раздел просит кнопка `pg:*`. None — не наша кнопка.

    Вынесено из обработчика колбэков, чтобы молчаливый игнор
    (кнопка ничего не делает) ловился тестом, а не пользователями.
    """
    if not isinstance(data, str) or not data.startswith("pg:"):
        return None
    kind = data.split(":", 1)[1]
    if kind == "refresh":
        return "status"
    if kind.startswith("refresh:"):
        kind = kind.split(":", 1)[1]
    return kind if kind in _PANELS else None


async def render_panel(kind: str):
    """Готовый ответ панели (текст, клавиатура) или (None, None) при сбое."""
    kind = panel_kind(kind if kind.startswith("pg:") else f"pg:{kind}")
    if kind is None:
        return None, None
    try:
        return _PANELS[kind](await fetch_state()), nav_kb(kind)
    except Exception:  # noqa: BLE001 — сеть легла: ответит вызыватель
        return None, None


def fmt_wallets_page(wallets, page=1, built_at=None):
    total = len(wallets)
    pages = max(1, (total + PER_PAGE - 1) // PER_PAGE)
    page = max(1, min(page, pages))
    start = (page - 1) * PER_PAGE
    chunk = wallets[start : start + PER_PAGE]
    head = f"💼 <b>Кошельки</b> · всего {total} · стр. {page}/{pages}"
    if built_at:
        head += f" · <i>данные на {esc(str(built_at)[:16].replace('T', ' '))}</i>"
    lines = [head, ""]
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
    """Пагинация: края + шаг. При 1000 кошельков (250 стр.) листать по одной — боль."""
    row = []
    if page > 1:
        row.append(InlineKeyboardButton(text="⏮", callback_data="wl:1"))
        row.append(InlineKeyboardButton(text="◀", callback_data=f"wl:{page - 1}"))
    if page < pages:
        row.append(InlineKeyboardButton(text="▶", callback_data=f"wl:{page + 1}"))
        row.append(InlineKeyboardButton(text="⏭", callback_data=f"wl:{pages}"))
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
            "• /pnl — PnL последнего прогона\n"
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
                await m.answer(fmt_status(await fetch_state()), parse_mode=ParseMode.HTML,
                               reply_markup=nav_kb("status"))
            except Exception:  # noqa: BLE001
                await m.answer("⚠️ Не удалось получить состояние фермы.", parse_mode=ParseMode.HTML)
        elif text == "/pnl":
            try:
                await m.answer(fmt_pnl(await fetch_state()), parse_mode=ParseMode.HTML,
                               reply_markup=nav_kb("pnl"))
            except Exception:  # noqa: BLE001
                await m.answer("⚠️ Не удалось получить PnL.", parse_mode=ParseMode.HTML)
        elif text == "/history":
            try:
                await m.answer(fmt_history(await fetch_state()), parse_mode=ParseMode.HTML,
                               reply_markup=nav_kb("history"))
            except Exception:  # noqa: BLE001
                await m.answer("⚠️ Не удалось получить историю.", parse_mode=ParseMode.HTML)
        elif text == "/doctor":
            try:
                await m.answer(fmt_doctor(await fetch_state()), parse_mode=ParseMode.HTML,
                               reply_markup=nav_kb("doctor"))
            except Exception:  # noqa: BLE001
                await m.answer("⚠️ Не удалось получить диагностику.", parse_mode=ParseMode.HTML)
        elif text == "/wallets":
            try:
                wallets = await fetch_wallets()
            except Exception as e:  # noqa: BLE001
                await m.answer(f"⚠️ Кошельки недоступны: {esc(str(e))[:120]}",
                               parse_mode=ParseMode.HTML)
                return
            if wallets is None:
                await m.answer(
                    "💼 Кошельки появятся, когда ПК опубликует шифрованный блоб.\n"
                    "Добавь один и тот же WALLETS_KEY (64 hex) в env на ПК и в Secrets бота.\n\n" + AUTHOR,
                    parse_mode=ParseMode.HTML,
                )
                return
            content, pages = fmt_wallets_page(wallets, 1, _WALLETS_CACHE.get("built_at"))
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
        if cb.data and cb.data.startswith("pg:"):
            text, kb = await render_panel(cb.data)
            if text is None:
                await cb.answer("Не удалось обновить", show_alert=True)
                return
            try:
                await cb.message.edit_text(text, parse_mode=ParseMode.HTML, reply_markup=kb)
            except Exception:  # noqa: BLE001 — тот же текст: нечего обновлять
                await cb.answer("Уже свежо")
                return
            await cb.answer()
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
        content, pages = fmt_wallets_page(wallets, page, _WALLETS_CACHE.get("built_at"))
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
