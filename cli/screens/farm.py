"""Экраны фарминга: один цикл и live-консоль."""

import asyncio
import logging
import time
from typing import Any

from core import ui
from core.database import Database
from core.network import NetworkManager
from core.performance import effective_max_workers
from core.pool import FarmerPool
from core.single_instance import SingleInstance, default_lock_path
from core.utils import truncate_address

logger = logging.getLogger(__name__)


async def screen_farm_once(u: ui.UI, config: dict, db: Database) -> None:
    wallets = await db.get_all_wallets()
    if not wallets:
        u.print("  Нет кошельков. Создайте сначала (пункт 1).", style="red")
        return
    guard = SingleInstance(default_lock_path(config["database"]["path"]))
    if not guard.acquire():
        u.print(f"  Уже работает другой экземпляр фармера (pid={guard.holder_pid()}).", style="red")
        u.print("  На одной БД может работать только один пул — выходим.", style="yellow")
        return
    pool = FarmerPool(config, db)
    try:
        all_addresses = [w["address"] for w in wallets]
        u.typewrite(f"  Старт цикла: {len(wallets)} кошельков, workers={effective_max_workers(config)}")
        stop_event = asyncio.Event()
        results = await pool.run_once(wallets, all_addresses, stop_event=stop_event)
        ok = sum(1 for _, v in results if v > 0)
        acts = sum(v for _, v in results)
        u.out_ok(f"Цикл завершён: {ok}/{len(wallets)} кошельков, действий {acts}")
    finally:
        try:
            await pool.close()
        except Exception as e:
            logger.warning(f"Ошибка закрытия пула: {e}")
        try:
            guard.release()
        except Exception as e:
            logger.warning(f"Ошибка снятия single-instance lock: {e}")


async def screen_farm_live(u: ui.UI, config: dict, db: Database, stop_event: asyncio.Event) -> None:
    """Live-консоль фарма: обновляемая таблица кошельков в реальном времени."""
    guard = SingleInstance(default_lock_path(config["database"]["path"]))
    if not guard.acquire():
        u.print(f"  Уже работает другой экземпляр фармера (pid={guard.holder_pid()}).", style="red")
        u.print("  На одной БД может работать только один пул — выходим.", style="yellow")
        return
    net: NetworkManager | None = None
    pool: FarmerPool | None = None
    try:
        net = NetworkManager(config, db)
        pool = FarmerPool(config, db, network=net)
        task = asyncio.create_task(pool.run_forever(stop_event))
    except Exception:
        if pool is not None:
            await pool.close()
        elif net is not None:
            await net.close()
        try:
            guard.release()
        except Exception as e:
            logger.warning(f"Ошибка снятия single-instance lock: {e}")
        raise
    state: dict[str, Any] = {
        "wallets": [],
        "latency": None,
        "t0": time.monotonic(),
        "lt": 0.0,
        "pool_err": "",
        "fatal": False,
    }

    def _on_task_done(t) -> None:
        if t.cancelled() or state["fatal"]:
            return
        err = t.exception()
        if err is not None:
            state["fatal"] = True
            state["pool_err"] = str(err)[:120]
            u.toast(f"Ошибка фарма: {err}", kind="err")
            stop_event.set()

    task.add_done_callback(_on_task_done)

    async def _producer():
        while not (stop_event.is_set() or task.done()):
            try:
                state["wallets"] = await db.get_top_wallets(15)
                state["enc"] = await db.any_seed_encrypted()
                now = time.monotonic()
                if now - state["lt"] >= 10:
                    state["latency"] = await net.latency_probe()
                    state["lt"] = now
            except Exception:
                pass
            await asyncio.sleep(2.0)

    prod = asyncio.create_task(_producer())

    def _render():
        rows = []
        max_a = max((w.get("total_actions", 0) for w in state["wallets"]), default=1)
        for i, w in enumerate(state["wallets"], 1):
            prog = u.bar(w.get("total_actions", 0) / max_a, 10)
            action_txt = (
                f"[{'green' if w.get('total_actions', 0) > 0 else 'yellow'}]{w['total_actions']}[/]"
                if u.use_rich
                else str(w["total_actions"])
            )
            rows.append(
                [
                    i,
                    truncate_address(w["address"]),
                    action_txt,
                    prog,
                    "enc" if state.get("enc") else "key",
                ]
            )
        return rows

    def _cap():
        for k in u.poll_keys():
            if k == "p":
                if pool.paused:
                    pool.resume()
                    u.toast("Пул продолжает работу", kind="info")
                else:
                    pool.pause()
                    u.toast("Пул на паузе", kind="warn")
            elif k == "q":
                stop_event.set()
        elapsed = int(time.monotonic() - state["t0"])
        ps = pool.live_stats()
        err = state["pool_err"] or (f"ошибки {ps['errors']}" if ps["errors"] else "ошибки 0")
        per = f"{ps['actions'] / elapsed * 60:.1f} действ/мин" if elapsed > 0 else "0 действ/мин"
        dot = u.dot(state["latency"])
        lat_txt = f"{state['latency']:.0f} мс" if state["latency"] is not None else "—"
        pause_mark = " [PAUSE]" if pool.paused else ""
        ok = ps["processed"] - ps["errors"]
        dyn = f"воркеры {ps['dyn_workers']} (health {ps['health']:.2f})"
        return (
            f"{u.frame()} живых {len(state['wallets'])} | действий {ps['actions']} | "
            f"ок {ok} из {ps['processed']} | {err} | RPC {dot} {lat_txt} | {per}{pause_mark} | {dyn}"
        )

    headers = ["#", "Адрес", "Действий", "Прогресс", "Ключ"]
    title = "LIVE ФАРМ · P/Q · Ctrl+C"

    try:
        await u.live_table(
            title,
            _render,
            headers,
            period=1.5,
            stop=lambda: stop_event.is_set() or task.done(),
            caption=_cap,
        )
    finally:
        prod.cancel()
        if not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            except Exception as e:
                logger.warning(f"Фарм-таск завершился с ошибкой: {e}")
        try:
            if pool is not None:
                await pool.close()
        except Exception as e:
            logger.warning(f"Ошибка закрытия пула: {e}")
        try:
            guard.release()
        except Exception as e:
            logger.warning(f"Ошибка снятия single-instance lock: {e}")
        ps = pool.live_stats()
    if state["pool_err"]:
        u.toast(f"Фарм прерван: {state['pool_err']}", kind="err")
    else:
        u.toast(
            f"Фарм завершён: {ps['processed']} кошельков, {ps['actions']} действий, ошибки {ps['errors']}",
            kind="ok",
        )
