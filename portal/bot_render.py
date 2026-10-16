"""HARVEST // BOT-RENDER — единый рендер текстов Telegram-бота (п.1 ТЗ, этап 2).

Все экраны строятся здесь; хендлеры только вызывают render_* и отправляют.
parse_mode HTML, динамика экранирована, лимит 4096 с усечением «…».

Метки: [RUN]/[PAUSE]/[ERR]/[STOP]/[ok]/[warn] — без emoji.
Спарклайны ▁▂▃▄▅▆▇█, прогресс ▓/░, адреса 0xb3E4…B029E.
Сетка поля: · idle, ■ working, ▣ ok, x err, ~ rest (10 в ряд, макс. 100 + «+N»).

Редакция токена (п.8 ТЗ): шаблон bot<id>:<hash> из URL запросов к Telegram
часто попадает в тексты исключений HTTP-клиента — redact_token() чистит
логи и тексты ошибок, включая трейсбэки.
"""

from __future__ import annotations

import html
import re
from datetime import UTC, datetime

MAX_TEXT_LEN = 4096

# Метки и разделители — в одном месте (п.1 ТЗ).
LABELS = {
    "run": "[RUN]",
    "pause": "[PAUSE]",
    "err": "[ERR]",
    "stop": "[STOP]",
    "ok": "[ok]",
    "warn": "[warn]",
    "sep": "────────────────────────",
}

# Токен бота в URL запросов: bot123456:ABC-DEF1234ghI... (п.8 ТЗ).
_TOKEN_RE = re.compile(r"bot\d+:[A-Za-z0-9_-]+")
_TOKEN_STUB = "bot<TOKEN>"  # nosec B105 — не пароль, а метка-заглушка для редакции


def redact_token(text: object) -> str:
    """Вычищает шаблон bot<id>:<secret> из любого текста (логи, трейсбэки)."""
    return _TOKEN_RE.sub(_TOKEN_STUB, str(text))


def esc(value: object) -> str:
    """HTML-экранирование динамических значений."""
    return html.escape(str(value), quote=True)


def ensure_limit(text: str, limit: int = MAX_TEXT_LEN) -> str:
    """Усечение до лимита Telegram с «…» (п.1 ТЗ)."""
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 1)] + "…"


def short_addr(addr: object) -> str:
    """Сокращение адреса: 0xb3E4…B029E (п.1 ТЗ)."""
    s = str(addr or "")
    if len(s) <= 12:
        return s
    return f"{s[:6]}…{s[-5:]}"


_SPARK = "▁▂▃▄▅▆▇█"


def sparkline(values: list[float]) -> str:
    """Спарклайн ▁▂▄▆█ для истории (п.1 ТЗ). Пусто → «—»."""
    if not values:
        return "—"
    try:
        nums = [float(v) for v in values]
    except (TypeError, ValueError):
        return "—"
    lo, hi = min(nums), max(nums)
    if hi <= lo:
        return _SPARK[0] * len(nums)
    out = []
    for v in nums:
        idx = int((v - lo) / (hi - lo) * (len(_SPARK) - 1))
        out.append(_SPARK[max(0, min(idx, len(_SPARK) - 1))])
    return "".join(out)


def progress_cells(done: object, total: object, width: int = 10) -> str:
    """Прогресс ▓▓▓▓░░░░ 62% (п.1 ТЗ)."""
    try:
        t = int(str(total or 0))
    except (TypeError, ValueError):
        t = 0
    try:
        d = int(str(done or 0))
    except (TypeError, ValueError):
        d = 0
    pct = (d / t * 100) if t else 0
    filled = max(0, min(width, round(pct / 100 * width)))
    return f"{'▓' * filled}{'░' * (width - filled)} {pct:.0f}% ({d}/{t})"


# callback_data: версионирован, короткий, ≤64 байт (п.3 ТЗ).
_CB_MAX_BYTES = 64
_CB_VERSION = "v1"


def cb(screen: str, action: str, arg: str = "") -> str:
    """Новый callback: 'v1:<экран>:<действие>[:арг]'."""
    parts = [_CB_VERSION, screen, action] + ([arg] if arg else [])
    data = ":".join(parts)
    if len(data.encode("utf-8")) > _CB_MAX_BYTES:
        raise ValueError(f"callback_data длиннее {_CB_MAX_BYTES} байт: {screen}/{action}")
    return data


def parse_cb(data: str) -> tuple[str, str, str] | None:
    """Разбор callback: новый 'v1:...' либо legacy (без версии).

    Возвращает (screen, action, arg). Устаревшая версия (не v1) → None,
    вызывающий перерисовывает «Поле» с «Экран устарел» (п.3 ТЗ).
    Legacy без версии маппится как есть: screen=data — для совместимости
    со старыми сообщениями.
    """
    raw = str(data or "")
    if raw.startswith(_CB_VERSION + ":"):
        parts = raw.split(":")
        if len(parts) < 3:
            return None
        return parts[1], parts[2], ":".join(parts[3:])
    if re.match(r"^v\d+:", raw):
        # Похоже на версию, но не текущая → устарело (п.3 ТЗ).
        return None
    if not raw:
        return None
    # Legacy: целое поле — screen, без action.
    return raw, "", ""


def render_home(
    state: dict,
    mode: str = "novice",
    cells: str = "",
    cells_total: int = 0,
    updated: str = "",
) -> str:
    """Домашний экран «ПОЛЕ» (п.2 ТЗ). Одно сообщение, правка на месте.

    state: running/paused/network/chain_id/wallet_count/cycle/cycles_total/
      errors/health_factor. cells: строка формата iwoer (см. wallet_cells).
    """
    from portal.wallet_cells import render_grid

    running = bool(state.get("running"))
    paused = bool(state.get("paused"))
    mark = LABELS["pause"] if running and paused else (LABELS["run"] if running else LABELS["stop"])
    net = esc(state.get("network", "—"))
    chain = esc(state.get("chain_id", "—"))
    wallets = esc(state.get("wallet_count", "—"))
    cycle = esc(state.get("cycle", "—"))
    total = state.get("cycles_total", "∞")
    total_s = "∞" if total in (0, "0", None, "∞") else esc(total)
    errors = esc(state.get("errors", 0))
    try:
        health = float(state.get("health_factor", 0.0) or 0.0)
    except (TypeError, ValueError):
        health = 0.0
    stamp = esc(updated or datetime.now(UTC).strftime("%H:%M UTC"))
    grid = render_grid(cells, cells_total)
    lines = [
        "<b>HARVEST // ПОЛЕ</b>",
        f"{mark}  {net} · {chain}",
        LABELS["sep"],
        f"кошельки  {wallets}      цикл {cycle} / {total_s}",
        f"ошибки    {errors}       health {health:.2f}",
        grid,
        f"обновлено {stamp}",
    ]
    if mode == "pro":
        try:
            workers = int(state.get("workers", 0) or 0)
        except (TypeError, ValueError):
            workers = 0
        lines.append(
            f"workers {esc(workers)} · rpc {esc(state.get('rpc_ok', '—'))} · "
            f"gas {esc(state.get('gas_limit', '—'))}"
        )
        if state.get("cmd"):
            lines.append(f"<pre>{esc(state['cmd'])}</pre>")
    else:
        lines.append("кошельки — ваши адреса · цикл — текущий проход · health — живость сети")
    return ensure_limit("\n".join(lines))


def render_alert(text: str) -> str:
    """Алерты владельцу через единый рендер (п.7 ТЗ). Без секретов."""
    return ensure_limit(f"<b>HARVEST // АЛЕРТ</b>\n{esc(redact_token(text))}")


# ---------------- строки RU/EN и новые экраны (п.2-6 ТЗ) ----------------

LANGS = ("ru", "en")

_STR: dict[str, dict[str, str]] = {
    "updated": {"ru": "обновлено", "en": "updated"},
    "hint_novice": {
        "ru": "кошельки — ваши адреса · цикл — текущий проход · health — живость сети",
        "en": "wallets — your addresses · cycle — current pass · health — net liveness",
    },
    "btn_start": {"ru": "[ Запуск ]", "en": "[ Start ]"},
    "btn_pause": {"ru": "[ Пауза ]", "en": "[ Pause ]"},
    "btn_resume": {"ru": "[ Резюм ]", "en": "[ Resume ]"},
    "btn_stop": {"ru": "[ Стоп ]", "en": "[ Stop ]"},
    "btn_net": {"ru": "[ Сеть ]", "en": "[ Network ]"},
    "btn_scale": {"ru": "[ Масштаб ]", "en": "[ Scale ]"},
    "btn_wallets": {"ru": "[ Кошельки ]", "en": "[ Wallets ]"},
    "btn_history": {"ru": "[ История ]", "en": "[ History ]"},
    "btn_builder": {"ru": "[ Конструктор ]", "en": "[ Builder ]"},
    "btn_ref": {"ru": "[ Справочник ]", "en": "[ Manual ]"},
    "btn_diag": {"ru": "[ Диагностика ]", "en": "[ Diagnostics ]"},
    "btn_danger": {"ru": "[ Опасная зона ]", "en": "[ Danger zone ]"},
    "btn_refresh": {"ru": "[ Обновить ]", "en": "[ Refresh ]"},
    "btn_live_on": {"ru": "[ Live 10 мин ]", "en": "[ Live 10 min ]"},
    "btn_live_off": {"ru": "[ Live выкл ]", "en": "[ Live off ]"},
    "btn_back": {"ru": "[ ← Назад ]", "en": "[ ← Back ]"},
    "btn_home": {"ru": "[ Поле ]", "en": "[ Field ]"},
    "btn_confirm": {"ru": "[ Подтвердить ]", "en": "[ Confirm ]"},
    "btn_cancel": {"ru": "[ Отмена ]", "en": "[ Cancel ]"},
    "btn_yes_stop": {"ru": "[ Да, остановить ]", "en": "[ Yes, stop ]"},
    "btn_csv": {"ru": "[ CSV ]", "en": "[ CSV ]"},
    "btn_json": {"ru": "[ JSON ]", "en": "[ JSON ]"},
    "btn_enc": {"ru": "[ Зашифрованный ]", "en": "[ Encrypted ]"},
    "btn_keys": {"ru": "[ Ключи ]", "en": "[ Keys ]"},
    "btn_show": {"ru": "[ Показать ]", "en": "[ Show ]"},
    "btn_cmd": {"ru": "[ CMD ]", "en": "[ CMD ]"},
    "btn_cfg": {"ru": "[ CFG ]", "en": "[ CFG ]"},
    "btn_restart": {"ru": "[ Начать заново ]", "en": "[ Restart ]"},
    "btn_yaml": {"ru": "[ YAML файлом ]", "en": "[ YAML file ]"},
    "btn_verify": {"ru": "[ Проверить ]", "en": "[ Verify ]"},
    "btn_add": {"ru": "[ +{n} ]", "en": "[ +{n} ]"},
    "mode_novice": {"ru": "Новичок", "en": "Novice"},
    "mode_pro": {"ru": "Про", "en": "Pro"},
    "stale": {
        "ru": "Экран устарел — показываю Поле.",
        "en": "Stale screen — showing Field.",
    },
    "cooldown": {"ru": "[жди {n}с]", "en": "[wait {n}s]"},
}


def tr(key: str, lang: str = "ru", **kw: object) -> str:
    """Строка по ключу на языке (по умолчанию RU)."""
    lang = lang if lang in LANGS else "ru"
    return _STR.get(key, {}).get(lang, _STR.get(key, {}).get("ru", key)).format(**kw)


def render_stop_confirm(state: dict, lang: str = "ru") -> str:
    net = esc(state.get("network", "—"))
    return ensure_limit(
        "<b>HARVEST // СТОП</b>\n"
        f"Остановить фарм ({net})?\n"
        "Текущий цикл прервётся, состояние сохранится."
    )


def render_danger(mode: str, lang: str, enc_available: bool = True) -> str:
    m = tr("mode_pro" if mode == "pro" else "mode_novice", lang)
    enc = (
        "Шифрованный .enc (AES-256-CBC v2, пароль спрошу следующим сообщением)."
        if lang == "ru"
        else "Encrypted .enc (AES-256-CBC v2, I will ask the password next)."
    )
    if not enc_available:
        enc = "Шифрование недоступно." if lang == "ru" else "Encryption unavailable."
    warn = (
        "Файл содержит приватные ключи. Только тестнет. "
        "Не пересылай и не храни в чате."
        if lang == "ru"
        else "The file contains private keys. Testnet only. Do not forward or keep in chat."
    )
    restart = (
        "Сообщение с файлом удаляется через 60 с (best-effort, при рестарте не гарантируется)."
        if lang == "ru"
        else "The file message is deleted after 60 s (best-effort, not guaranteed across restarts)."
    )
    head = "HARVEST // ОПАСНАЯ ЗОНА" if lang == "ru" else "HARVEST // DANGER ZONE"
    return ensure_limit(
        f"<b>{head}</b>\n{warn}\n{restart}\n\n"
        f"{'Режим' if lang == 'ru' else 'Mode'}: <b>{m}</b> · "
        f"{'Язык' if lang == 'ru' else 'Language'}: <b>{lang.upper()}</b>\n{enc}"
    )


def render_export_confirm(fmt: str, enc: bool = False, lang: str = "ru") -> str:
    kind = "(.enc, шифрованный)" if enc and lang == "ru" else (
        "(.enc, encrypted)" if enc else f"({fmt.upper()})"
    )
    warn = (
        "Файл содержит <b>все приватные ключи</b>. "
        "Пересылка запрещена, сообщение удалится через 60 с."
        if lang == "ru"
        else "The file contains <b>all private keys</b>. "
        "Forwarding is blocked, the message deletes itself in 60 s."
    )
    head = "HARVEST // ЭКСПОРТ — подтверждение"
    return ensure_limit(f"<b>{head}</b>\nФайл {kind} {warn}\nПродолжить?")


def render_keys_confirm(page: int, lang: str = "ru") -> str:
    warn = (
        f"Показать приватные ключи (стр. {page})? "
        "Сообщение защищено от пересылки и удалится через 60 с."
        if lang == "ru"
        else f"Reveal private keys (p. {page})? "
        "The message is forward-protected and deletes itself in 60 s."
    )
    return ensure_limit("<b>HARVEST // КЛЮЧИ</b>\n" + warn)


def render_enc_ask(lang: str = "ru") -> str:
    return ensure_limit(
        "<b>HARVEST // ПАРОЛЬ</b>\n"
        + (
            "Пришли пароль для .enc одной строкой — сообщение с паролем "
            "потру удалить, но лучше удали его сам(а). /cancel — отмена."
            if lang == "ru"
            else "Send the .enc password in one line — I will try to delete it, "
            "but better delete it yourself. /cancel aborts."
        )
    )


def render_scale(wallet_count: object, lang: str = "ru") -> str:
    head = "HARVEST // МАСШТАБ" if lang == "ru" else "HARVEST // SCALE"
    body = (
        f"кошельков: <b>{esc(wallet_count)}</b>\n"
        "Создание — до 50 за вызов (защита HTTP/RPC)."
        if lang == "ru"
        else f"wallets: <b>{esc(wallet_count)}</b>\nCreation — up to 50 per call (HTTP/RPC guard)."
    )
    return ensure_limit(f"<b>{head}</b>\n{body}")


def render_builder(lang: str = "ru") -> str:
    head = "HARVEST // КОНСТРУКТОР" if lang == "ru" else "HARVEST // BUILDER"
    body = (
        "CMD — готовая команда auto.py. CFG — готовый YAML файлом.\n"
        "Ручной ввод тоже работает: /cmd vibe 50 5"
        if lang == "ru"
        else "CMD — ready auto.py command. CFG — ready YAML file.\n"
        "Manual input works too: /cmd vibe 50 5"
    )
    return ensure_limit(f"<b>{head}</b>\n{body}")


def render_master_done(cmd: str, lang: str = "ru") -> str:
    head = "HARVEST // ГОТОВО" if lang == "ru" else "HARVEST // DONE"
    return ensure_limit(f"<b>{head}</b>\n<pre>{esc(cmd)}</pre>")


def render_wallets_list(
    wallets: list[dict], page: int = 1, per_page: int = 4, lang: str = "ru"
) -> tuple[str, int]:
    """Список БЕЗ ключей: адреса сокращены (п.6 ТЗ). Возвращает (текст, страниц)."""
    total = len(wallets)
    pages = max(1, (total + per_page - 1) // per_page)
    page = max(1, min(page, pages))
    chunk = wallets[(page - 1) * per_page : (page - 1) * per_page + per_page]
    head = "HARVEST // КОШЕЛЬКИ" if lang == "ru" else "HARVEST // WALLETS"
    lines = [f"<b>{head}</b> · {total} · {page}/{pages}", ""]
    if not chunk:
        lines.append(
            "Кошельков пока нет — создайте кнопкой ниже."
            if lang == "ru" else "No wallets yet — create with the button below."
        )
    for i, w in enumerate(chunk, (page - 1) * per_page + 1):
        addr = esc(short_addr(w.get("address", "")))
        acts = esc(w.get("total_actions", 0))
        lines.append(f"<b>#{i}</b> · <code>{addr}</code> · {acts}")
    lines.append(
        "Ключи скрыты — кнопка «Ключи» покажет их с подтверждением."
        if lang == "ru" else "Keys hidden — the Keys button reveals them with confirmation."
    )
    return ensure_limit("\n".join(lines)), pages
