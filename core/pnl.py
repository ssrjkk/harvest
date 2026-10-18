"""PnL-отчёт по прогону фермы: HTML со ссылками на эксплорер.

На тестнетах «PnL» — не прибыль в деньгах, а движение нативного баланса:
сколько газа съела ферма и что осталось на кошельках. Источники:

* ``actions_log`` — что, с каким исходом и под каким tx_hash отправляли;
* снимки баланса через :meth:`NetworkManager.get_balance` в ключевых точках
  прогона (до крана, перед фармом, после всего).

Точный ``gasUsed`` недоступен: ``send_transfer`` отдаёт наружу только хеш,
ресипт наружу не пробрасывается, и в БД газа нет. Поэтому раздел «газ» —
оценка ``gas_limit × gas_price`` и помечен как оценка. На сам Δ баланса
оценка не влияет: газ в него уже включён.

Балансы снимаются по выборке, а не по всем кошелькам: 1000 кошельков × 2
снимка — это 2000 RPC-вызовов и минуты ожидания ради отчёта. Размер
выборки настраивается (``--pnl-sample``, 0 = все кошельки).
"""

from __future__ import annotations

import asyncio
import html
import json
import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# Ток округления для балансов: 12 знаков — как в wei→eth (1e18).
_ETH_DECIMALS = 12


# Эксплореры по chain_id: в минимальных конфигах (например vibevibe) network
# задаёт только rpc_url + chain_id, а без эксплорера ссылки в отчёте мертвы.
_EXPLORERS: dict[int, str] = {
    46630: "https://explorer.testnet.chain.robinhood.com",
    5042002: "https://explorer.testnet.arc.network",
}


def resolve_network_meta(net_cfg: dict) -> dict:
    """Нормализует секцию config['network'] под шапку отчёта."""
    chain_id = net_cfg.get("chain_id")
    return {
        "name": net_cfg.get("name") or f"Chain {chain_id}",
        "slug": net_cfg.get("slug", ""),
        "chain_id": chain_id,
        "currency": net_cfg.get("currency") or "ETH",
        "explorer": net_cfg.get("explorer") or _EXPLORERS.get(chain_id, ""),
    }


def pick_sample(addresses: list[str], n: int) -> list[str]:
    """Равномерная выборка кошельков.

    Равномерная, а не «топ-N по активности»: иначе отчёт покажет PnL
    активных кошельков и скроет спящие, а это и есть картина прогона.
    """
    if n <= 0 or n >= len(addresses):
        return list(addresses)
    step = len(addresses) / n
    return [addresses[int(i * step)] for i in range(n)]


async def snapshot(
    network: Any, addresses: list[str], *, concurrency: int = 8
) -> dict[str, float | None]:
    """Балансы кошельков. Ошибка по одному адресу не роняет весь снимок."""
    if not addresses:
        return {}
    sem = asyncio.Semaphore(max(1, concurrency))

    async def one(addr: str) -> float | None:
        async with sem:
            try:
                return float(await network.get_balance(addr, refresh=True))
            except Exception as e:  # noqa: BLE001 — адрес не прочитали, идём дальше
                logger.debug("PnL: баланс %s не прочитан (%s)", addr[:10], e)
                return None

    values = await asyncio.gather(*(one(a) for a in addresses))
    return dict(zip(addresses, values, strict=False))


def _delta(a: float | None, b: float | None) -> float | None:
    if a is None or b is None:
        return None
    return b - a


def _fmt_eth(v: float | None, places: int = 6) -> str:
    if v is None:
        return "—"
    return f"{v:.{places}f}"


def summarize_actions(actions: list[dict], *, sample: set[str], top_n: int = 8) -> dict:
    """Сводка по actions_log: по типам действий, по кошелькам и свежие tx.

    ``sample`` — адреса выборки: в разбивке по кошелькам показываем только их,
    иначе таблица разрастётся до тысячи строк.
    """
    by_type: dict[str, dict[str, int]] = {}
    per_wallet: dict[str, dict[str, int]] = {}
    txs: list[dict] = []
    for a in actions:
        t = a.get("action_type") or "?"
        slot = by_type.setdefault(t, {"ok": 0, "fail": 0})
        slot["ok" if a.get("success") else "fail"] += 1
        if a.get("address") in sample:
            w = per_wallet.setdefault(a["address"], {"ok": 0, "fail": 0})
            w["ok" if a.get("success") else "fail"] += 1
        if a.get("tx_hash") and a.get("success"):
            txs.append(a)
    ranked = sorted(per_wallet.items(), key=lambda kv: -(kv[1]["ok"] + kv[1]["fail"]))
    return {
        "by_type": by_type,
        "per_wallet": ranked[:top_n],
        "recent_txs": txs[:10],
        "total_ok": sum(v["ok"] for v in by_type.values()),
        "total_fail": sum(v["fail"] for v in by_type.values()),
    }


class PnlTracker:
    """Снимает балансы в ключевых точках прогона и считает дельты."""

    def __init__(self, addresses: list[str], *, sample: int = 25, concurrency: int = 8) -> None:
        self.total_wallets = len(addresses)
        self.sample_size = sample
        self.addresses = pick_sample(addresses, sample)
        self.concurrency = concurrency
        self.marks: dict[str, dict[str, float | None]] = {}
        self.order: list[str] = []

    async def mark(self, network: Any, label: str) -> None:
        """Снимок балансов выборки под именем label (перезаписывает при повторе)."""
        self.marks[label] = await snapshot(network, self.addresses, concurrency=self.concurrency)
        if label not in self.order:
            self.order.append(label)

    def section(self, before: str | None, after: str | None) -> dict:
        """Дельта между двумя метками + агрегаты по выборке."""
        rows: list[dict] = []
        if not before or not after:
            return {"rows": rows, "sum_delta": None, "measured": 0}
        b, a = self.marks.get(before, {}), self.marks.get(after, {})
        total = 0.0
        measured = 0
        for addr in self.addresses:
            d = _delta(b.get(addr), a.get(addr))
            if d is not None:
                total += d
                measured += 1
            rows.append({
                "address": addr,
                "before": b.get(addr),
                "after": a.get(addr),
                "delta": d,
            })
        return {"rows": rows, "sum_delta": total if measured else None, "measured": measured}


_CSS = """
*{box-sizing:border-box}
body{margin:0;padding:28px 18px 60px;background:#14110A;color:#EDE8DC;
 font:14px/1.6 Consolas,"Courier New",monospace}
.wrap{max-width:1080px;margin:0 auto}
h1{font-size:26px;margin:0 0 4px;letter-spacing:.02em;color:#D4A72C}
h2{font-size:15px;margin:30px 0 10px;letter-spacing:.14em;text-transform:uppercase;color:#9A8F73}
.sub{color:#9A8F73;font-size:12.5px;margin:0 0 6px}
.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(170px,1fr));gap:10px;margin:16px 0}
.card{border:2px solid #2A2418;padding:12px 14px;background:#1B1710;box-shadow:4px 4px 0 #0D0B06}
.card .k{font-size:10.5px;letter-spacing:.12em;text-transform:uppercase;color:#9A8F73}
.card .v{font-size:26px;margin-top:3px}
.pos{color:#6FAF5A}.neg{color:#C4553D}.mut{color:#9A8F73}
table{width:100%;border-collapse:collapse;font-size:12.5px;background:#1B1710}
th{background:#2A2418;color:#EDE8DC;text-align:left;padding:8px 9px;font-size:11px;
 letter-spacing:.1em;text-transform:uppercase}
td{padding:7px 9px;border-bottom:1px solid #262016}
tr:last-child td{border-bottom:0}
a{color:#D4A72C}
.note{border-left:4px solid #D4A72C;background:#1B1710;padding:10px 13px;margin:14px 0;
 font-size:12.5px;color:#C9C1AC}
.est{border-left-color:#9A8F73}
.foot{margin-top:34px;padding-top:12px;border-top:1px solid #262016;color:#6F6A5C;font-size:11.5px}
code{color:#D4A72C}
@media print{body{background:#fff;color:#000}}
"""


def render_html(
    *,
    tracker: PnlTracker,
    faucet_delta: float | None,
    farm_delta: float | None,
    actions: dict,
    network: dict,
    explorer: str,
    currency: str,
    out_path: str,
) -> str:
    """Собирает самодостаточный HTML (без внешних файлов — открывается с диска)."""
    esc = html.escape
    gen = datetime.now(UTC).strftime("%Y-%m-%d %H:%M UTC")

    def addr_link(a: str) -> str:
        if not a:
            return '<span class="mut">—</span>'
        short = a[:10] + "…" + a[-6:]
        if not explorer:
            return f"<span>{esc(short)}</span>"
        return (f'<a href="{esc(explorer.rstrip("/"))}/address/{esc(a)}" '
                f'target="_blank" rel="noopener">{esc(short)}</a>')

    def tx_link(h: str) -> str:
        if not h:
            return '<span class="mut">—</span>'
        short = h[:18] + "…"
        if not explorer:
            return f"<code>{esc(short)}</code>"
        return (f'<a href="{esc(explorer.rstrip("/"))}/tx/{esc(h)}" '
                f'target="_blank" rel="noopener"><code>{esc(short)}</code></a>')

    def delta_cell(v: float | None) -> str:
        if v is None:
            return '<span class="mut">—</span>'
        cls = "pos" if v > 0 else ("neg" if v < 0 else "mut")
        return f'<span class="{cls}">{v:+.{_ETH_DECIMALS}f}</span>'

    # --- карточки ---
    ok, fail = actions["total_ok"], actions["total_fail"]
    rate = (ok / (ok + fail) * 100) if (ok + fail) else 0.0
    cards = [
        ("Успешных действий", str(ok), ""),
        ("Ошибок", str(fail), "neg" if fail else ""),
        ("Success rate", f"{rate:.1f}%", ""),
        ("Δ фарма (выборка)", _fmt_eth(farm_delta), "neg" if (farm_delta or 0) < 0 else ""),
        ("Δ крана (выборка)", _fmt_eth(faucet_delta), "pos" if (faucet_delta or 0) > 0 else ""),
        ("Кошельков всего", str(tracker.total_wallets), ""),
    ]
    cards_html = "".join(
        f'<div class="card"><div class="k">{esc(k)}</div>'
        f'<div class="v {cls}">{esc(v)}</div></div>'
        for k, v, cls in cards
    )

    # --- по типам действий ---
    type_rows = "".join(
        f"<tr><td>{esc(t)}</td><td class='pos'>{d['ok']}</td>"
        f"<td class='neg'>{d['fail']}</td>"
        f"<td>{(d['ok'] / (d['ok'] + d['fail']) * 100 if (d['ok'] + d['fail']) else 0):.1f}%</td></tr>"
        for t, d in sorted(actions["by_type"].items(), key=lambda kv: -(kv[1]["ok"]))
    ) or '<tr><td colspan="4" class="mut">нет данных</td></tr>'

    # --- по кошелькам выборки ---
    pw = dict(actions["per_wallet"])
    farm = tracker.section("pre_farm", "post_run")
    wallet_rows = []
    for r in farm["rows"]:
        w = pw.get(r["address"], {"ok": 0, "fail": 0})
        wallet_rows.append(
            f"<tr><td>{addr_link(r['address'])}</td>"
            f"<td class='pos'>{w['ok']}</td><td class='neg'>{w['fail']}</td>"
            f"<td>{_fmt_eth(r['before'], 8)}</td><td>{_fmt_eth(r['after'], 8)}</td>"
            f"<td>{delta_cell(r['delta'])}</td></tr>"
        )
    wallet_rows_html = "".join(wallet_rows) or \
        '<tr><td colspan="6" class="mut">нет данных</td></tr>'

    # --- свежие транзакции ---
    tx_rows = "".join(
        f"<tr><td>{tx_link(t['tx_hash'])}</td>"
        f"<td>{esc(t['action_type'])}</td><td>{addr_link(t['address'])}</td></tr>"
        for t in actions["recent_txs"]
    ) or '<tr><td colspan="3" class="mut">нет данных</td></tr>'

    measured_note = (
        f"Балансы сняты по выборке {len(tracker.addresses)} из "
        f"{tracker.total_wallets} кошельков. "
        + (f"Успешно прочитано {farm['measured']}." if farm["measured"] else "Прочитать не удалось.")
    )

    return f"""<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>PnL — {esc(network.get('name', 'harvest'))} · {esc(gen)}</title>
<style>{_CSS}</style>
</head>
<body>
<div class="wrap">
  <h1>PnL — {esc(network.get('name', 'harvest'))}</h1>
  <p class="sub">{esc(network.get('slug', ''))} · chain_id {esc(str(network.get('chain_id', '—')))} ·
     {esc(currency)} · сгенерировано {esc(gen)}</p>
  <p class="sub">Прогон: <code>{esc(out_path)}</code></p>

  <div class="cards">{cards_html}</div>

  <h2>Дельта баланса по фазам</h2>
  <table>
    <tr><th>Фаза</th><th>Δ {esc(currency)} (выборка)</th><th>Что значит</th></tr>
    <tr><td>Кран</td><td>{delta_cell(faucet_delta)}</td>
        <td class="mut">пополнение до целевого баланса</td></tr>
    <tr><td>Фарм</td><td>{delta_cell(farm_delta)}</td>
        <td class="mut">газ на транзакции, минус всё, что ушло в переводы</td></tr>
  </table>
  <p class="note">{esc(measured_note)}</p>
  <p class="note est">Раздел про газ — оценка: <code>send_transfer</code> отдаёт только tx_hash,
     ресипт наружу не пробрасывается, поэтому точный <code>gasUsed</code> не сохраняется.
     На <code>Δ баланса</code> это не влияет — газ в него уже включён.</p>

  <h2>По типам действий</h2>
  <table>
    <tr><th>Действие</th><th>Успешно</th><th>Ошибок</th><th>Success rate</th></tr>
    {type_rows}
  </table>

  <h2>Кошельки выборки</h2>
  <table>
    <tr><th>Кошелёк</th><th>OK</th><th>Ошибок</th><th>До</th><th>После</th><th>Δ</th></tr>
    {wallet_rows_html}
  </table>

  <h2>Свежие транзакции</h2>
  <table>
    <tr><th>Tx</th><th>Действие</th><th>Кошелёк</th></tr>
    {tx_rows}
  </table>

  <div class="foot">
    harvest · PnL-отчёт по прогону фермы · {esc(gen)}<br>
    Только тестные сети. Значения в нативной валюте сети, без курса к фиату.
    {"Эксплорер: " + esc(explorer) if explorer else "Для этой сети эксплорер не задан — ссылок нет."}
  </div>
</div>
</body>
</html>
"""


async def build_report(
    *,
    tracker: PnlTracker,
    db: Any,
    network: dict,
    currency: str,
    explorer: str,
    out_path: str,
) -> dict:
    """Собирает цифры и пишет HTML. Возвращает сводку для CLI."""
    faucet = tracker.section("pre_faucet", "pre_farm")
    farm = tracker.section("pre_farm", "post_run")
    actions = summarize_actions(
        await db.get_actions_since(0, limit=5000), sample=set(tracker.addresses)
    )
    doc = render_html(
        tracker=tracker,
        faucet_delta=faucet["sum_delta"],
        farm_delta=farm["sum_delta"],
        actions=actions,
        network=network,
        explorer=explorer,
        currency=currency,
        out_path=out_path,
    )
    Path(out_path).write_text(doc, encoding="utf-8")
    return {
        "path": out_path,
        "faucet_delta": faucet["sum_delta"],
        "farm_delta": farm["sum_delta"],
        "measured": farm["measured"],
        "sample": len(tracker.addresses),
        "total": tracker.total_wallets,
        "ok": actions["total_ok"],
        "fail": actions["total_fail"],
    }


def describe(summary: dict) -> str:
    """Одна строка для CLI: путь, дельты, охват."""
    d = lambda v: _fmt_eth(v)  # noqa: E731
    return json.dumps(
        {
            "path": summary["path"],
            "sample": f"{summary['sample']}/{summary['total']}",
            "measured": summary["measured"],
            "farm_delta": d(summary["farm_delta"]),
            "faucet_delta": d(summary["faucet_delta"]),
            "ok": summary["ok"],
            "fail": summary["fail"],
        },
        ensure_ascii=False,
    )
