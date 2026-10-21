"""harvest // OPS — логика Telegram-бота в дизайне сайта (эмодзи — только статус home).

Чистые функции без зависимости от aiogram-хендлеров: форматирование
поля/статистики/истории/балансов/сетей, конструктор команды `/cmd`,
генератор конфига `/cfg` из shipped YAML, проверка вставленного YAML
`/check` настоящим валидатором и сжатый справочник `/ref`.

Стиль сообщений — как ops-board сайта: секции `harvest // X`,
маркеры `[RUN]` / `[STOP]` / `[PAUSE]`, `[ok]` / `[warn]` / `[err]`,
таблицы в `<pre>`, значения экранированы. Секреты никогда не печатаются:
проверка сообщает только итоги, входной текст не цитируется.
"""

from __future__ import annotations

import html
import os
import re

import yaml

from core.config_validate import ConfigError, config_warnings, validate_config
from portal.networks import NETWORKS

_FOOT = "ssrjkk · t.me/ssrjkk_bot · MIT · v2.3.0"

# Лимит входного YAML для /check (как лимит initData): защита от спама.
_CHECK_MAX_BYTES = 16 * 1024

# Слаг -> shipped config. `simple` нет в portal.networks — пресет трансферов.
_CMD_NETS: dict[str, str] = {str(n["slug"]): str(n["config"]) for n in NETWORKS}
_CMD_NETS["simple"] = "config.simple.yaml"


def known_slug(slug: str) -> bool:
    """Известный слаг сети (с учётом алиаса vibe)."""
    s = (slug or "").strip().lower()
    return s == "vibe" or s in _CMD_NETS


_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _esc(value: object) -> str:
    """HTML-экранирование значений (defense-in-depth, как в bot_telegram)."""
    return html.escape(str(value), quote=True)


def _redact_line(line: str) -> str:
    """Вычищает credentials/query-токены из строк validator'а перед показом."""
    line = re.sub(r"(https?://)[^/\s@]+@", r"\1", line)
    return re.sub(r"([?&#])[^?\s&#]*?(api[_-]?key|token|secret|pass)[^?\s&#]*", r"\1…", line, flags=re.I)


def _state_mark(running: bool, paused: bool = False) -> str:
    if running and paused:
        return "[RUN+PAUSE]"
    return "[RUN]" if running else "[STOP]"


def fmt_status(data: dict) -> str:
    """Живой статус: успех %, средние, health — как блок «Поле» на сайте."""
    pool = data.get("pool", {}) or {}
    db = data.get("db", {}) or {}
    actions = int(pool.get("actions", 0) or 0)
    errors = int(pool.get("errors", 0) or 0)
    cycles = int(pool.get("cycles", 0) or 0)
    total = actions + errors
    rate = f"{actions / total * 100:.1f}%" if total else "—"
    per_cycle = f"{actions / cycles:.1f}" if cycles else "—"
    state = _state_mark(bool(data.get("running")), bool(pool.get("paused")))
    lines = [
        "<b>harvest // ПОЛЕ</b>",
        f"состояние: <b>{state}</b> · health: <b>{float(data.get('health_factor', 0.0)):.2f}</b>",
        "<pre>"
        f"кошельки  {_esc(data.get('wallet_count', '-'))}\n"
        f"действия  {_esc(actions)} · ошибки {_esc(errors)}\n"
        f"успех     {_esc(rate)} · сред/цикл {_esc(per_cycle)}\n"
        f"циклы     {_esc(cycles)} · история {_esc(db.get('cycles', '-'))}\n"
        f"воркеры   {_esc(pool.get('dyn_workers', '-'))}"
        "</pre>",
        _progress_bar(pool.get("processed", 0), data.get("wallet_count", 0)),
        _FOOT,
    ]
    return "\n".join(lines)


def fmt_stats(data: dict) -> str:
    """Компактная статистика (кнопка СТАТА)."""
    pool = data.get("pool", {}) or {}
    db = data.get("db", {}) or {}
    state = _state_mark(bool(data.get("running")), bool(pool.get("paused")))
    lines = [
        "<b>harvest // СТАТА</b>",
        f"состояние: <b>{state}</b>",
        "<pre>"
        f"кошельки  {_esc(data.get('wallet_count', '-'))}\n"
        f"циклы     {_esc(pool.get('cycles', 0))}\n"
        f"действия  {_esc(pool.get('actions', 0))}\n"
        f"ошибки    {_esc(pool.get('errors', 0))}\n"
        f"дропы     {_esc(pool.get('dropped', 0))}\n"
        f"воркеры   {_esc(pool.get('dyn_workers', '-'))}\n"
        f"история   {_esc(db.get('cycles', '-'))}"
        "</pre>",
        _progress_bar(pool.get("processed", 0), data.get("wallet_count", 0)),
        _FOOT,
    ]
    return "\n".join(lines)


def fmt_doctor(data: dict) -> str:
    pool = data.get("pool", {}) or {}
    db = data.get("db", {}) or {}
    health = float(data.get("health_factor", 0.0) or 0.0)
    problems: list[str] = []
    if not data.get("running"):
        problems.append("ферма не запущена")
    if not pool:
        problems.append("ядро фермы не подключено")
    if pool:
        if health and health < 0.5:
            problems.append(f"сеть деградировала (health {health:.2f})")
        errors = int(pool.get("errors", 0) or 0)
        actions = int(pool.get("actions", 0) or 0)
        if errors and errors > actions * 0.5 + 50:
            problems.append("накоплено много ошибок")
    lines = [
        "<b>harvest // ДОКТОР</b>",
        f"ферма: {_state_mark(bool(data.get('running')))} · "
        f"ядро: {'[ok] подключено' if pool else '[err] НЕ ПОДКЛЮЧЕНО'}",
        f"health: {health:.2f}",
    ]
    if pool:
        lines.append(
            f"циклы: <b>{_esc(pool.get('cycles', 0))}</b> · "
            f"действия: <b>{_esc(pool.get('actions', 0))}</b> · "
            f"ошибки: <b>{_esc(pool.get('errors', 0))}</b>"
        )
    lines.append(f"история БД: <b>{_esc(db.get('cycles', 0))}</b> циклов")
    lines.append("")
    if problems:
        lines.append("[warn] замечания:")
        lines.extend(f"— {_esc(p)}" for p in problems)
    else:
        lines.append("[ok] всё штатно.")
    lines.append(_FOOT)
    return "\n".join(lines)


def fmt_history(rows: list[dict]) -> str:
    lines = ["<b>harvest // ИСТОРИЯ</b>", ""]
    if not rows:
        lines.append("Пусто — циклы ещё не завершались.")
        lines.append(_FOOT)
        return "\n".join(lines)
    for row in rows[:8]:
        errs = int(row.get("errors") or 0)
        mark = "[ok]" if errs == 0 else f"[err {errs}]"
        started = str(row.get("started_at") or "-")[:19]
        lines.append(
            f"{mark} #{_esc(row.get('id'))} · {_esc(started)} · "
            f"{float(row.get('duration_s') or 0):.0f}с · "
            f"кошельков {_esc(row.get('wallets'))} · "
            f"действий {_esc(row.get('actions_ok'))} · ошибок {_esc(errs)}"
        )
    lines.append(_FOOT)
    return "\n".join(lines)


def fmt_pnl(snap: dict | None) -> str:
    """PnL последнего прогона: агрегаты, без адресов кошельков."""
    lines = ["<b>harvest // PnL ПРОГОНА</b>", ""]
    if not snap:
        lines.append("Пусто — прогона с отчётом ещё не было.")
        lines.append(_FOOT)
        return "\n".join(lines)
    ok = int(snap.get("actions_ok") or 0)
    fail = int(snap.get("actions_fail") or 0)
    total = ok + fail
    rate = (ok / total * 100) if total else 0.0
    lines.append(f"Успешных действий: {_esc(ok)} · отказов: {_esc(fail)} ({rate:.1f}%)")

    def _delta(v):
        try:
            return f"{float(v):+.6f}"
        except (TypeError, ValueError):
            return "—"

    lines.append(f"Δ фарма: {_esc(_delta(snap.get('farm_delta')))}")
    lines.append(f"Δ крана: {_esc(_delta(snap.get('faucet_delta')))}")
    lines.append(
        f"Выборка: {_esc(snap.get('measured'))}/{_esc(snap.get('total_wallets'))} кошельков · "
        f"окно: {_esc(snap.get('window_actions'))} действий"
    )
    reasons = snap.get("by_reason") or []
    if reasons:
        lines.append("")
        lines.append("<b>Причины отказов</b>")
        for r in reasons[:5]:
            if not isinstance(r, dict):
                continue
            lines.append(f"· {_esc(r.get('reason', '?'))} — {_esc(r.get('count', 0))}")
    lines.append("")
    lines.append(f"Обновлено: {_esc(str(snap.get('created_at') or '-')[:19])}")
    lines.append(_FOOT)
    return "\n".join(lines)


def fmt_balances(bals: list[dict]) -> str:
    if not bals:
        return "<b>harvest // БАЛАНСЫ</b>\n\nКошельков пока нет — создайте кнопкой [+ СОЗДАТЬ] в /wallets."
    lines = ["<b>harvest // БАЛАНСЫ</b>", ""]
    total = 0.0
    unknown = 0
    for b in bals:
        bal = b.get("balance")
        addr = _esc(b.get("address", ""))
        if bal is None:
            unknown += 1
            lines.append(f"• <code>{addr}</code> — ?")
        else:
            total += float(bal)
            lines.append(f"• <code>{addr}</code> — {float(bal):.4f} ETH")
    lines += ["", f"<b>Итого: {total:.4f} ETH</b>"]
    if unknown:
        lines.append(f"({unknown} — не прочитались, RPC недоступен)")
    lines.append(_FOOT)
    return "\n".join(lines)


def fmt_networks(networks: list[dict]) -> str:
    lines = ["<b>harvest // СЕТИ</b>", ""]
    for n in networks or []:
        if n.get("running"):
            mark = "[RUN]"
        elif n.get("current"):
            mark = "[CUR]"
        else:
            mark = "[···]"
        name = _esc(n.get("name") or n.get("slug", "?"))
        tagline = _esc(n.get("tagline") or "")
        line = f"{mark} <b>{name}</b> — {tagline}" if tagline else f"{mark} <b>{name}</b>"
        lines.append(line)
    lines += ["", "Кнопка под сетью — переключить и запустить фарм.", _FOOT]
    return "\n".join(lines)


def _progress_bar(done: int, total: int, label: str = "", width: int = 10) -> str:
    """Прогресс-бар: ▰▰▰▱▱ 60% (12/20)."""
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
    bar = f"{'▰' * filled}{'▱' * (width - filled)} {pct:.0f}% ({done}/{total})"
    return f"<b>{label}</b>\n{bar}" if label else bar


def _fmt_wallets_page(wallets: list[dict], page: int = 1, per_page: int = 4) -> tuple[str, int]:
    """Кошельки с ключами (спойлер). Возвращает (текст, страниц)."""
    total = len(wallets)
    pages = max(1, (total + per_page - 1) // per_page)
    page = max(1, min(page, pages))
    start = (page - 1) * per_page
    chunk = wallets[start : start + per_page]
    lines = [f"<b>harvest // КОШЕЛЬКИ</b> · всего {total} · стр. {page}/{pages}", ""]
    if not chunk:
        lines.append("Кошельков пока нет — создайте кнопкой [+ СОЗДАТЬ] ниже.")
    for i, w in enumerate(chunk, start + 1):
        addr = _esc(w.get("address", ""))
        acts = _esc(w.get("total_actions", 0))
        key = _esc(w.get("private_key", ""))
        mn = _esc(w.get("mnemonic", "") or "—")
        lines.append(f"<b>#{i}</b> · <code>{addr}</code> · {acts} действ.")
        lines.append(f"ключ: <spoiler>{key}</spoiler>")
        lines.append(f"сид: <spoiler>{mn}</spoiler>")
        lines.append("")
    lines.append("Нажми на спойлер, чтобы открыть. Секреты видны только вам.")
    lines.append(_FOOT)
    return "\n".join(lines), pages


# ---------------- общие парсеры мастера ----------------

def parse_count(text: str) -> int | None:
    """Число кошельков 1..5000 из свободного ввода. None = переспросить."""
    try:
        n = int((text or "").strip())
    except (TypeError, ValueError):
        return None
    return n if 1 <= n <= 5000 else None


def parse_cycles(text: str) -> int | None:
    """Число циклов 0..100000 (0 — бесконечно). None = переспросить."""
    try:
        n = int((text or "").strip())
    except (TypeError, ValueError):
        return None
    return n if 0 <= n <= 100000 else None


# ---------------- /cmd ----------------

CMD_USAGE = (
    "<b>harvest // КОМАНДА</b>\n"
    "Собирает готовую команду auto.py. Формат:\n"
    "<code>/cmd &lt;сеть&gt; [кошельки] [циклы] [nofaucet]</code>\n"
    "сети: <code>vibevibe</code> <code>robinhood</code> <code>flop</code> "
    "<code>arc</code> <code>simple</code>\n"
    "пример: <code>/cmd vibe 50 5</code> (/cfg даст готовый YAML)"
)


def build_command(slug: str, wallets: int = 50, cycles: int = 5,
                  skip_faucet: bool = False) -> str:
    """Готовая команда auto.py. Raises ValueError с причиной при плохих аргументах."""
    slug = (slug or "").strip().lower()
    if slug == "vibe":
        slug = "vibevibe"
    if slug not in _CMD_NETS:
        raise ValueError(f"неизвестная сеть '{slug}': vibe/robinhood/flop/arc/simple")
    if not 1 <= wallets <= 5000:
        raise ValueError(f"кошельков должно быть 1..5000, дано {wallets}")
    if not 0 <= cycles <= 100000:
        raise ValueError(f"циклов должно быть 0..100000 (0 — бесконечно), дано {cycles}")
    cmd = f"python auto.py --config {_CMD_NETS[slug]} --wallets {wallets} --cycles {cycles}"
    if skip_faucet:
        cmd += " --skip-faucet"
    return cmd


def parse_cmd_args(text: str) -> str:
    """Разбирает `/cmd ...` в команду или usage. Никогда не бросает исключений."""
    parts = (text or "").split()
    if len(parts) < 2:
        return CMD_USAGE
    slug = parts[1]
    try:
        wallets = int(parts[2]) if len(parts) > 2 else 50
        cycles = int(parts[3]) if len(parts) > 3 else 5
    except ValueError:
        return "[err] кошельки и циклы — целые числа.\n" + CMD_USAGE
    skip = len(parts) > 4 and parts[4].lower() in {"nofaucet", "skip-faucet", "без-крана"}
    try:
        cmd = build_command(slug, wallets, cycles, skip)
    except ValueError as e:
        return f"[err] {_esc(e)}\n" + CMD_USAGE
    return f"<b>harvest // КОМАНДА</b>\n<code>{_esc(cmd)}</code>\n{_FOOT}"


# ---------------- /cfg ----------------

CFG_USAGE = (
    "<b>harvest // КОНФИГ</b>\n"
    "Присылает готовый YAML под сеть (без секретов). Формат:\n"
    "<code>/cfg &lt;сеть&gt; [кошельки]</code>\n"
    "сети: <code>vibevibe</code> <code>robinhood</code> <code>flop</code> "
    "<code>arc</code> <code>simple</code>\n"
    "пример: <code>/cfg vibe 50</code>"
)


def build_config_yaml(slug: str, wallets: int = 50) -> tuple[str, str]:
    """YAML shipped-конфига с подставленным wallets.count.

    Возвращает (имя файла, текст). Перед отдачей прогоняет настоящий
    валидатор — битый конфиг не уйдёт. Raises ValueError.
    """
    slug = (slug or "").strip().lower()
    if slug == "vibe":
        slug = "vibevibe"
    if slug not in _CMD_NETS:
        raise ValueError(f"неизвестная сеть '{slug}': vibe/robinhood/flop/arc/simple")
    if not 1 <= wallets <= 5000:
        raise ValueError(f"кошельков должно быть 1..5000, дано {wallets}")
    filename = _CMD_NETS[slug]
    path = os.path.join(_ROOT, filename)
    if not os.path.exists(path):
        raise ValueError(f"файл {filename} отсутствует в репозитории")
    with open(path, encoding="utf-8") as f:
        cfg = yaml.safe_load(f)
    if not isinstance(cfg, dict):
        raise ValueError(f"{filename}: не словарь верхнего уровня")
    wal = cfg.get("wallets")
    if not isinstance(wal, dict):
        raise ValueError(f"{filename}: нет секции wallets")
    wal["count"] = wallets
    try:
        validate_config(cfg)
    except ConfigError as e:
        raise ValueError(f"{filename}: не прошёл валидацию: {e}") from e
    warns = config_warnings(cfg)
    head = "".join(f"# замечание валидатора: {w}\n" for w in warns[:5])
    head += f"# harvest — {filename} (собрано ботом, wallets.count={wallets})\n"
    return filename, head + yaml.safe_dump(cfg, sort_keys=False, allow_unicode=True)


# ---------------- профили конструктора (этап 2–3: бот-мастер и Mini App) ----------------

# Флагов тихий/обычный/быстрый в auto.py нет; отображение интерпретирующее:
# тихий = холостой прогон (--dry-run), быстрый = минимальные задержки (--fast).
# Утверждено владельцем: подпись в интерфейсе предупреждает, что быстрый
# ослабляет «человеческое» поведение (задержки/джиттер — защита проекта).
# Единый источник для бота и Mini App (никакой второй копии логики).
PROFILE_FLAGS: dict[str, list[str]] = {"quiet": ["--dry-run"], "normal": [], "fast": ["--fast"]}
PROFILE_LABEL = {"quiet": "тихий", "normal": "обычный", "fast": "быстрый"}


def apply_profile_flags(cmd: str, profile: str) -> str:
    """Добавляет флаги профиля к готовой команде."""
    for flag in PROFILE_FLAGS.get(profile or "normal", []):
        cmd += f" {flag}"
    return cmd


def profiles_summary(addresses: list[str], beh: dict | None = None) -> dict:
    """Сводка разнообразия профилей флота для экрана [Профили] (бот).

    Чистая функция: адреса + behavior-секция конфига → счётчики.
    """
    from core.behavior import profile_for

    cfg = {"behavior": beh or {}}
    addrs = [str(a) for a in (addresses or []) if a]
    profs = [profile_for(a, cfg) for a in addrs]
    acts = [p.activity for p in profs]
    corridors = {(p.actions_lo, p.actions_width) for p in profs}
    return {
        "n": len(addrs),
        "activity_lo": round(min(acts), 2) if acts else 0.0,
        "activity_hi": round(max(acts), 2) if acts else 0.0,
        "corridors": len(corridors),
        "rest": sum(1 for p in profs if p.rest_prob > 0),
        "burst": sum(1 for p in profs if p.burst_prob > 0),
        "weekend": sum(1 for p in profs if p.weekend_strength > 0),
        "windows": len({round(p.day_start) for p in profs}),
    }


# ---------------- /check ----------------

def check_config_text(text: str) -> tuple[bool, list[str]]:
    """Проверяет вставленный YAML настоящим валидатором.

    Возвращает (ok, строки). Входной текст никогда не цитируется,
    секреты из сообщений валидатора вычищаются.
    """
    raw = (text or "").encode("utf-8", "ignore")
    if len(raw) > _CHECK_MAX_BYTES:
        return False, ["[err] слишком длинный текст (>16 КБ). Пришлите файл."]
    try:
        cfg = yaml.safe_load(raw.decode("utf-8", "ignore"))
    except yaml.YAMLError as e:
        return False, [f"[err] YAML не парсится: {_esc(str(e)[:200])}"]
    if not isinstance(cfg, dict):
        return False, ["[err] корень YAML — не словарь (нужны секции network/wallets/...)"]
    try:
        validate_config(cfg)
    except ConfigError as e:
        out = [f"[err] {_redact_line(_esc(line))}" for line in str(e).splitlines()[:10]]
        return False, out or ["[err] ошибки конфигурации"]
    warns = config_warnings(cfg)
    out = ["[ok] конфиг валиден."]
    out.extend(f"[warn] {_redact_line(_esc(w))}" for w in warns[:5])
    return True, out


# ---------------- /ref ----------------

REF_TEXT = """<b>harvest // СПРАВКА</b>
<pre>network.rpc_url   https URL или список
network.chain_id  ≥1 (99999 = заглушка flop)
wallets.count     ≥0
faucet.min→target min ≤ target
faucet.retries    1..20 · concur 1..64
actions[].weight  ≥0 (0 = выкл)
transfer.target   адрес | random_wallet
contract-действия contract+method (0x0 = заглушка→transfer)
gas_limit         ≥1 · ≥50000 с реал. контрактами
threading.workers 1..1000
receipt_timeout   30с → None без отката nonce
license.grace     7 дней офлайн
schedule          ≥0.05 ч</pre>
ENV: FARMER_RPC_URL · FARMER_CHAIN_ID · FARMER_WORKERS ·
FARMER_MIN/ TARGET_BALANCE · FARMER_MASTER_KEY ·
FARMER_VIBE_SIGNER_KEY · CHAINSTACK_API_KEY
Полный справочник — вкладка «Про → Справочник» на сайте.
""" + _FOOT
