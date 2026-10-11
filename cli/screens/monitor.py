"""Экран живого монитора балансов."""

import asyncio
from typing import Any

from core import ui
from core.database import Database
from core.network import NetworkManager
from core.performance import effective_max_workers
from core.utils import truncate_address


async def screen_monitor(u: ui.UI, config: dict, db: Database, stop_event: asyncio.Event) -> None:
    """Живой монитор балансов (автообновление каждые 5 сек)."""
    net = NetworkManager(config, db)
    sem = asyncio.Semaphore(effective_max_workers(config))
    state: dict[str, Any] = {
        "rows": [],
        "total": 0.0,
        "funded": 0,
        "zero": 0,
        "latency": None,
        "paused": False,
    }

    async def _producer():
        while not stop_event.is_set():
            if state.get("paused"):
                await asyncio.sleep(0.5)
                continue
            addrs = await db.get_all_addresses()
            state["latency"] = await net.latency_probe()
            if not addrs:
                state["rows"] = [["—", "—", "нет кошельков"]]
            else:

                async def _one(addr: str):
                    async with sem:
                        bal = await net.get_balance(addr)
                        return truncate_address(addr), bal

                res = await asyncio.gather(*[_one(a) for a in addrs], return_exceptions=True)
                rows = []
                total = 0.0
                funded = 0
                for r in res:
                    if isinstance(r, BaseException):
                        rows.append([str(r).split(":")[0][:14], "ERR", "—"])
                        continue
                    addr, bal = r
                    total += bal if isinstance(bal, float) else 0
                    has = isinstance(bal, float) and bal > 0.001
                    funded += 1 if has else 0
                    if u.use_rich:
                        col = "green" if has else "red"
                        rows.append(
                            [
                                addr,
                                f"[{col}]{bal:.6f}[/{col}]",
                                f"[{col}]{'OK' if has else 'LOW'}[/{col}]",
                            ]
                        )
                    else:
                        rows.append([addr, f"{bal:.6f}", "OK" if has else "LOW"])
                state["rows"] = rows
                state["total"] = total
                state["funded"] = funded
                state["zero"] = len(addrs) - funded
            await asyncio.sleep(5.0)

    prod = asyncio.create_task(_producer())

    def _render():
        if not state["rows"]:
            return [["—", "—", "обновление..."]]
        return state["rows"]

    def _cap():
        for k in u.poll_keys():
            if k == "p":
                state["paused"] = not state["paused"]
                u.toast(
                    "Обновление остановлено" if state["paused"] else "Обновление продолжается",
                    kind="warn" if state["paused"] else "info",
                )
            elif k == "q":
                stop_event.set()
        dot = u.dot(state["latency"])
        lat_txt = f"{state['latency']:.0f} мс" if state["latency"] is not None else "—"
        pause_mark = " [PAUSE]" if state["paused"] else ""
        return (
            f"{u.frame()} баланс {state['total']:.6f} | заправлено {state['funded']} | "
            f"пусто {state['zero']} | RPC {dot} {lat_txt}{pause_mark}"
        )

    try:
        await u.live_table(
            "МОНИТОР КОШЕЛЬКОВ · P/Q · Ctrl+C",
            _render,
            ["Адрес", "Баланс", "Статус"],
            period=5.0,
            stop=lambda: stop_event.is_set(),
            caption=_cap,
        )
    finally:
        prod.cancel()
        await net.close()
