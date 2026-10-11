"""Экраны статистики и истории циклов."""

from core import ui
from core.database import Database, _redact_rpc_url
from core.utils import truncate_address


async def screen_stats(u: ui.UI, db: Database) -> None:
    stats = await db.get_stats()
    u.print()
    u.table(
        "СТАТИСТИКА",
        ["Показатель", "Значение"],
        [
            ["Кошельков", stats["total_wallets"]],
            ["Всего действий", stats["total_actions"]],
            ["Успешных", stats["success_actions"]],
            [
                "Процент успеха",
                f"{stats['success_actions'] / max(stats['total_actions'], 1) * 100:.1f}%",
            ],
            ["Записей в логе", stats["total_log_entries"]],
        ],
    )
    top = await db.get_top_wallets(10)
    if top:
        u.table(
            "ТОП-АКТИВНОСТЬ (по actions)",
            ["#", "Адрес", "Действий"],
            [[i, truncate_address(w["address"]), w["total_actions"]] for i, w in enumerate(top[:10], 1)],
        )
        u.toast(
            f"Лидер: {truncate_address(top[0]['address'])} — {top[0]['total_actions']} действий",
            kind="info",
        )
    else:
        u.hint("  Кошельков пока нет — создайте через пункт 1.")

    cycles = await db.get_cycle_stats()
    u.table(
        "ЦИКЛЫ ФАРМА (журнал)",
        ["Показатель", "Значение"],
        [
            ["Завершено циклов", cycles["cycles"]],
            ["Кошельков обработано", cycles["wallets"]],
            ["Действий за циклы", cycles["actions"]],
            ["Время фарма", f"{cycles['duration_s'] / 3600:.1f} ч"],
            ["Последний цикл", str(cycles["last_started_at"] or "—").replace("T", " ")[:19]],
        ],
    )


async def screen_history(u: ui.UI, db: Database) -> None:
    rows = await db.get_cycle_history(20)
    if not rows:
        u.print("  Циклов фарма ещё нет — запустите фарм (пункт 2 или 3).", style="yellow")
        return
    u.print()
    table_rows = []
    for i, r in enumerate(rows, 1):
        rpc_detail = (_redact_rpc_url(r["rpc_url"]) or "")[:28]
        if r.get("rpc_calls"):
            rate = (r["rpc_errors"] / max(r["rpc_calls"], 1)) * 100
            rpc_detail += f" | {r.get('rpc_latency_ms', 0):.0f}ms/{rate:.0f}%err"
        table_rows.append(
            [
                i,
                str(r["started_at"] or "").replace("T", " ")[:19],
                f"{r['duration_s']:.0f}",
                r["wallets"],
                r["wallets_ok"],
                r["actions_ok"],
                r["errors"],
                rpc_detail,
            ]
        )
    u.table(
        "ИСТОРИЯ ЦИКЛОВ (последние 20)",
        ["#", "Старт", "Длит(с)", "Кошельков", "ОК", "Действий", "Ошибки", "RPC"],
        table_rows,
    )
