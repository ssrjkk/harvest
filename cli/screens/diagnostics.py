"""Экраны диагностики: doctor и RPC-зонд."""

from core import ui
from core.database import Database, _redact_rpc_url
from core.doctor import doctor
from core.network import NetworkManager


async def screen_doctor(u: ui.UI, config: dict) -> None:
    """Полная самодиагностика окружения."""
    u.panel(
        "ДИАГНОСТИКА",
        "  Проверяю конфиг, master-ключ, БД, сеть, кран и licence-гейт...",
        color="bright_blue",
    )
    await u.spinner("Диагностика...", doctor(u, config))
    u.toast("Самодиагностика завершена", kind="info")
    u.hint("  Всё работает — можно запускать фарм. Красные пункты — подсказка, что чинить.")


async def screen_rpc_diag(u: ui.UI, config: dict, db: Database) -> None:
    print(f"  Зонд RPC: {_redact_rpc_url(config['network']['rpc_url']) or ''}...")
    net = NetworkManager(config, db)
    try:
        probes = await net.probe_all()
        u.table(
            "ДИАГНОСТИКА RPC",
            ["Эндпоинт", "Задержка", "Статус"],
            [
                [
                    _redact_rpc_url(url) or url,
                    f"{ms:.0f} мс" if ms is not None else "—",
                    f"{u.dot(ms)} {'OK' if ms is not None else 'FAIL'}",
                ]
                for url, ms in probes
            ],
        )
        d = net.diagnostics([ms for _, ms in probes])
        cb = "ОТКРЫТ (блокирует вызовы)" if d["cb_state"] == "OPEN" else d["cb_state"]
        u.table(
            "МЕТРИКИ",
            ["Параметр", "Значение"],
            [
                ["Активный RPC", d["active"]],
                ["Chain ID", d["chain_id"]],
                [
                    "Вызовов / сессия",
                    f"{d['calls']} (среднее {d['avg_ms']:.0f} мс)" if d["avg_ms"] else str(d["calls"]),
                ],
                ["Ошибок", d["errors"]],
                ["Circuit breaker", cb],
                ["Сбоев подряд", d["cb_failures"]],
            ],
        )
    finally:
        await net.close()
