"""harvest // BOT-RENDER — единый рендер текстов Telegram-бота (п.1 ТЗ, этап 2).

Все экраны строятся здесь; хендлеры только вызывают render_* и отправляют.
parse_mode HTML, динамика экранирована, лимит 4096 с усечением «…».

Метки: [RUN]/[PAUSE]/[ERR]/[STOP]/[ok]/[warn]; эмодзи — только home-статус.
Спарклайны ▁▂▃▄▅▆▇█, прогресс ▓/░, адреса 0xb3E4…B029E.
Сетка поля: · idle, ■ working, ▣ ok, x err, ~ rest (10 в ряд, макс. 100 + «+N»).

Редакция токена (п.8 ТЗ): шаблон bot<id>:<hash> из URL запросов к Telegram
часто попадает в тексты исключений HTTP-клиента — redact_token() чистит
логи и тексты ошибок, включая трейсбэки.
"""

from __future__ import annotations

import html
import re

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

# Токен бота в URL запросов: bot123456:ABC-DEF1234ghI... (п.8 ТЗ),
# плюс голый вид 123456:ABC... (env-дампы, трейсбэки без префикса bot).
_TOKEN_RE = re.compile(r"bot\d+:[A-Za-z0-9_-]+")
_BARE_TOKEN_RE = re.compile(r"(?<!\w)\d{5,}:[A-Za-z0-9_-]{20,}")
_TOKEN_STUB = "bot<TOKEN>"  # nosec B105 — не пароль, а метка-заглушка для редакции


def redact_token(text: object) -> str:
    """Вычищает токен из любого текста (логи, трейсбэки): с префиксом и голый."""
    s = _TOKEN_RE.sub(_TOKEN_STUB, str(text))
    return _BARE_TOKEN_RE.sub(_TOKEN_STUB, s)


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
    lang: str = "ru",
) -> str:
    """Домашний экран «ПОЛЕ» (п.2 ТЗ). Одно сообщение, правка на месте.

    state: running/paused/network/chain_id/wallet_count/cycle/cycles_total/
      errors/health_factor/uptime_s. cells: строка формата iwoer.
    Формат — чистый статус по видению v3.0: заголовок, сеть+health,
    полоса флота ▰▱, прогресс цикла, успех/ошибки, аптайм. Только реальные
    цифры из state/cells — никаких нарисованных балансов и стримов.
    """
    running = bool(state.get("running"))
    paused = bool(state.get("paused"))
    en = lang == "en"
    if running and paused:
        mark, dot = ("Пауза", "⏸") if not en else ("Paused", "⏸")
    elif running:
        mark, dot = ("Активен", "🟢") if not en else ("Active", "🟢")
    else:
        mark, dot = ("Остановлен", "🔴") if not en else ("Stopped", "🔴")
    mark_en = "Active" if running and not paused else mark
    net = esc(state.get("network", "—"))
    chain = esc(state.get("chain_id", "—"))
    try:
        health_pct = round(float(state.get("health_factor", 0.0) or 0.0) * 100)
    except (TypeError, ValueError):
        health_pct = 0
    try:
        total_n = int(cells_total or state.get("wallet_count") or 0)
    except (TypeError, ValueError):
        total_n = 0
    dist = {c: 0 for c in "iwoer"}
    for ch in str(cells or ""):
        if ch in dist:
            dist[ch] += 1
    active = dist["w"] + dist["o"]
    pct = round(active / total_n * 100) if total_n else 0
    filled = max(0, min(10, round(pct / 10)))
    bar = "▰" * filled + "▱" * (10 - filled)
    try:
        cycle_n = int(state.get("cycle") or 0)
    except (TypeError, ValueError):
        cycle_n = 0
    try:
        ok_n = int((state.get("pool") or {}).get("actions", state.get("actions", 0)) or 0)
    except (TypeError, ValueError):
        ok_n = 0
    try:
        err_n = int((state.get("pool") or {}).get("errors", state.get("errors", 0)) or 0)
    except (TypeError, ValueError):
        err_n = 0
    def f(n: int) -> str:
        return f"{n:,}".replace(",", " " if not en else ",")

    up = fmt_uptime(state.get("uptime_s"))
    if en:
        head = f"🌾 <b>harvest // {mark_en}</b> ({dot})"
        lines = [
            head,
            f"Network: {net} ({chain}) · RPC health: {health_pct}% ⚡️",
            "",
            f"📊 Fleet status ({f(total_n)} wallets):",
            f"{bar} {pct}% ({f(active)} working / {f(total_n - active)} waiting)",
            "",
            f"🔄 Progress: cycle {cycle_n or '—'} of ∞",
            f"✨ Success: {f(ok_n)} tx  |  ⚠️ Errors: {f(err_n)}",
            f"⏱ Uptime: {up}",
        ]
    else:
        lines = [
            f"🌾 <b>harvest // {mark}</b> ({dot})",
            f"Сеть: {net} ({chain}) · RPC health: {health_pct}% ⚡️",
            "",
            f"📊 Статус флота ({f(total_n)} кошельков):",
            f"{bar} {pct}% ({f(active)} в работе / {f(total_n - active)} ожидают)",
            "",
            f"🔄 Прогресс: цикл {cycle_n or '—'} из ∞",
            f"✨ Успешно: {f(ok_n)} tx  |  ⚠️ Ошибки: {f(err_n)}",
            f"⏱ Аптайм: {up}",
        ]
    return ensure_limit("\n".join(lines))


def fmt_uptime(seconds: object) -> str:
    """Аптайм демона человеческим языком: 4ч 12м / 12м / 3д 2ч. Нет данных — «—»."""
    if not isinstance(seconds, (str, int, float)):
        return "—"
    try:
        s = int(float(seconds or 0))
    except (TypeError, ValueError):
        return "—"
    if s <= 0:
        return "—"
    d, s = divmod(s, 86400)
    h, s = divmod(s, 3600)
    m = s // 60
    if d:
        return f"{d}д {h}ч"
    if h:
        return f"{h}ч {m:02d}м"
    return f"{m}м"


def render_alert(text: str) -> str:
    """Алерты владельцу через единый рендер (п.7 ТЗ). Без секретов."""
    return ensure_limit(f"<b>harvest // АЛЕРТ</b>\n{esc(redact_token(text))}")


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
    "btn_menu": {"ru": "[ Меню ]", "en": "[ Menu ]"},
    "btn_terminal": {"ru": "[ Открыть терминал ]", "en": "[ Open terminal ]"},
    "btn_profiles": {"ru": "[ Профили ]", "en": "[ Profiles ]"},
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
        "<b>harvest // СТОП</b>\n"
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
    head = "harvest // ОПАСНАЯ ЗОНА" if lang == "ru" else "harvest // DANGER ZONE"
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
    head = "harvest // ЭКСПОРТ — подтверждение"
    return ensure_limit(f"<b>{head}</b>\nФайл {kind} {warn}\nПродолжить?")


def render_keys_confirm(page: int, lang: str = "ru") -> str:
    warn = (
        f"Показать приватные ключи (стр. {page})? "
        "Сообщение защищено от пересылки и удалится через 60 с."
        if lang == "ru"
        else f"Reveal private keys (p. {page})? "
        "The message is forward-protected and deletes itself in 60 s."
    )
    return ensure_limit("<b>harvest // КЛЮЧИ</b>\n" + warn)


def render_enc_ask(lang: str = "ru") -> str:
    return ensure_limit(
        "<b>harvest // ПАРОЛЬ</b>\n"
        + (
            "Пришли пароль для .enc одной строкой — сообщение с паролем "
            "потру удалить, но лучше удали его сам(а). /cancel — отмена."
            if lang == "ru"
            else "Send the .enc password in one line — I will try to delete it, "
            "but better delete it yourself. /cancel aborts."
        )
    )


def render_scale(wallet_count: object, lang: str = "ru") -> str:
    head = "harvest // МАСШТАБ" if lang == "ru" else "harvest // SCALE"
    body = (
        f"кошельков: <b>{esc(wallet_count)}</b>\n"
        "Создание — до 50 за вызов (защита HTTP/RPC)."
        if lang == "ru"
        else f"wallets: <b>{esc(wallet_count)}</b>\nCreation — up to 50 per call (HTTP/RPC guard)."
    )
    return ensure_limit(f"<b>{head}</b>\n{body}")


def render_profiles(summary: dict, lang: str = "ru") -> str:
    """Экран разнообразия профилей флота (бот): только агрегаты, без адресов."""
    n = summary.get("n", 0)
    head = "harvest // ПРОФИЛИ" if lang == "ru" else "harvest // PROFILES"
    if lang == "ru":
        body = (
            f"кошельков: <b>{esc(n)}</b>\n"
            f"активность {esc(summary.get('activity_lo', 0))}–{esc(summary.get('activity_hi', 0))} "
            f"· коридоров {esc(summary.get('corridors', 0))}\n"
            f"отдых {esc(summary.get('rest', 0))} · всплески {esc(summary.get('burst', 0))} "
            f"· выходные {esc(summary.get('weekend', 0))}\n"
            f"окон часов: {esc(summary.get('windows', 0))}"
        )
    else:
        body = (
            f"wallets: <b>{esc(n)}</b>\n"
            f"activity {esc(summary.get('activity_lo', 0))}–{esc(summary.get('activity_hi', 0))} "
            f"· corridors {esc(summary.get('corridors', 0))}\n"
            f"rest {esc(summary.get('rest', 0))} · bursts {esc(summary.get('burst', 0))} "
            f"· weekends {esc(summary.get('weekend', 0))}\n"
            f"hour windows: {esc(summary.get('windows', 0))}"
        )
    return ensure_limit(f"<b>{head}</b>\n{body}")


def render_builder(lang: str = "ru") -> str:
    head = "harvest // КОНСТРУКТОР" if lang == "ru" else "harvest // BUILDER"
    body = (
        "CMD — готовая команда auto.py. CFG — готовый YAML файлом.\n"
        "Ручной ввод тоже работает: /cmd vibe 50 5"
        if lang == "ru"
        else "CMD — ready auto.py command. CFG — ready YAML file.\n"
        "Manual input works too: /cmd vibe 50 5"
    )
    return ensure_limit(f"<b>{head}</b>\n{body}")


def render_master_done(cmd: str, lang: str = "ru") -> str:
    head = "harvest // ГОТОВО" if lang == "ru" else "harvest // DONE"
    return ensure_limit(f"<b>{head}</b>\n<pre>{esc(cmd)}</pre>")


def render_wallets_list(
    wallets: list[dict], page: int = 1, per_page: int = 4, lang: str = "ru"
) -> tuple[str, int]:
    """Список БЕЗ ключей: адреса сокращены (п.6 ТЗ). Возвращает (текст, страниц)."""
    total = len(wallets)
    pages = max(1, (total + per_page - 1) // per_page)
    page = max(1, min(page, pages))
    chunk = wallets[(page - 1) * per_page : (page - 1) * per_page + per_page]
    head = "harvest // КОШЕЛЬКИ" if lang == "ru" else "harvest // WALLETS"
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
