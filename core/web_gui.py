"""Веб-интерфейс для фармера HARVEST.

FastAPI backend + статический frontend. Управление через браузер:
старт/стоп фарм, создание кошельков, кран, статистика, мониторинг,
планировщик, группы кошельков, RPC-дашборд, мульти-чейн.
"""

import asyncio
import json
import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from core.config import load_config
from core.crypto import resolve_master_key
from core.database import Database
from core.faucet import Faucet
from core.license import enforce_license_async
from core.network import NetworkManager
from core.pool import FarmerPool
from core.scheduler import Scheduler
from core.wallet import WalletManager

logger = logging.getLogger(__name__)

_state: dict[str, Any] = {
    "pool": None,
    "config": None,
    "db": None,
    "network": None,
    "wallet_manager": None,
    "faucet": None,
    "farming_task": None,
    "is_farming": False,
    "clients": [],
    "logs": [],
    "scheduler": None,
    "group_manager": None,
    "balance_alerts_task": None,
    "balance_alerts_enabled": False,
    "balance_threshold": 0.0,
    "current_network": "default",
    "available_networks": [],
}

MAX_LOGS = 200


class WebSocketLogHandler(logging.Handler):
    def emit(self, record):
        try:
            msg = self.format(record)
            _state["logs"].append(msg)
            if len(_state["logs"]) > MAX_LOGS:
                _state["logs"] = _state["logs"][-MAX_LOGS:]
            for client in _state["clients"][:]:
                try:
                    asyncio.create_task(
                        client.send_text(
                            json.dumps({"type": "log", "message": msg, "level": record.levelname})
                        )
                    )
                except Exception:
                    pass
        except Exception:
            pass


async def broadcast_status():
    if not _state["clients"]:
        return
    stats = await _state["db"].get_stats() if _state["db"] else {}
    status = {
        "type": "status",
        "is_farming": _state["is_farming"],
        "wallets": stats.get("total_wallets", 0),
        "actions": stats.get("total_actions", 0),
        "success_rate": (
            f"{stats['success_actions'] / max(stats['total_actions'], 1) * 100:.1f}%"
            if stats.get("total_actions", 0) > 0
            else "0%"
        ),
        "network": _state["current_network"],
        "scheduler_running": _state["scheduler"].is_running if _state["scheduler"] else False,
        "balance_alerts": _state["balance_alerts_enabled"],
    }
    for client in _state["clients"][:]:
        try:
            await client.send_text(json.dumps(status))
        except Exception:
            pass


async def broadcast_event(event_type: str, data: dict):
    msg = json.dumps({"type": event_type, **data})
    for client in _state["clients"][:]:
        try:
            await client.send_text(msg)
        except Exception:
            pass


@asynccontextmanager
async def lifespan(app: FastAPI):
    handler = WebSocketLogHandler()
    handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", "%H:%M:%S"))
    logging.getLogger().addHandler(handler)
    _scan_networks()
    yield
    if _state["scheduler"]:
        await _state["scheduler"].stop()
    if _state["balance_alerts_task"]:
        _state["balance_alerts_task"].cancel()
    if _state["farming_task"]:
        _state["farming_task"].cancel()
    if _state["network"]:
        await _state["network"].close()
    if _state["db"]:
        await _state["db"].close()


app = FastAPI(title="HARVEST GUI", lifespan=lifespan)

static_dir = Path(__file__).parent / "static"
if static_dir.exists():
    app.mount("/static", StaticFiles(directory=static_dir), name="static")


def _scan_networks() -> list[str]:
    configs = sorted(Path(".").glob("config*.yaml"))
    names = []
    for c in configs:
        name = c.stem.replace("config", "").strip("_") or "default"
        names.append(name)
    _state["available_networks"] = names
    return names


class CreateWalletsRequest(BaseModel):
    count: int = 10
    group_id: int | None = None


class GroupCreateRequest(BaseModel):
    name: str
    color: str = "#007bff"


class GroupAssignRequest(BaseModel):
    addresses: list[str]


class ScheduleCreateRequest(BaseModel):
    task_id: str
    name: str = ""
    interval_hours: float = 0
    interval_minutes: float = 0
    run_at_time: str | None = None
    cycles: int = 1


class ScheduleUpdateRequest(BaseModel):
    name: str | None = None
    enabled: bool | None = None
    interval_hours: float | None = None
    interval_minutes: float | None = None
    run_at_time: str | None = None
    cycles: int | None = None


class SwitchNetworkRequest(BaseModel):
    network: str


class BalanceAlertsRequest(BaseModel):
    enabled: bool
    threshold: float = 0.0


@app.get("/", response_class=HTMLResponse)
async def root():
    html_path = static_dir / "index.html"
    if html_path.exists():
        return html_path.read_text(encoding="utf-8")
    return "<h1>HARVEST GUI</h1><p>Frontend not found</p>"


@app.get("/api/status")
async def get_status():
    stats = await _state["db"].get_stats() if _state["db"] else {}
    return {
        "is_farming": _state["is_farming"],
        "wallets": stats.get("total_wallets", 0),
        "actions": stats.get("total_actions", 0),
        "success_actions": stats.get("success_actions", 0),
        "success_rate": (
            f"{stats['success_actions'] / max(stats['total_actions'], 1) * 100:.1f}%"
            if stats.get("total_actions", 0) > 0
            else "0%"
        ),
        "config_loaded": _state["config"] is not None,
        "network": _state["current_network"],
        "scheduler_running": _state["scheduler"].is_running if _state["scheduler"] else False,
        "balance_alerts": _state["balance_alerts_enabled"],
    }


@app.post("/api/init")
async def init_system():
    try:
        if _state["config"] is not None:
            return {"ok": True, "message": "Already initialized"}

        config = load_config()
        _state["config"] = config
        await enforce_license_async(config)

        try:
            resolve_master_key(config)
        except Exception as e:
            return {"ok": False, "message": f"Master key error: {e}"}

        db = Database(config["database"]["path"])
        await db.init()
        _state["db"] = db

        gm = db.get_group_manager()
        conn = await db._connect()
        gm.set_db(conn)
        _state["group_manager"] = gm

        network = NetworkManager(config)
        await network.init()
        _state["network"] = network

        _state["wallet_manager"] = WalletManager(config, db, network)

        if config.get("faucet", {}).get("enabled", False):
            _state["faucet"] = Faucet(config)

        scheduler = Scheduler()
        await scheduler.load()
        scheduler.set_run_callback(_scheduled_farm)
        await scheduler.start()
        _state["scheduler"] = scheduler

        logger.info("System initialized")
        await broadcast_status()
        return {"ok": True, "message": "System initialized"}
    except Exception as e:
        logger.error(f"Init error: {e}")
        return {"ok": False, "message": str(e)}


@app.post("/api/wallets/create")
async def create_wallets(req: CreateWalletsRequest):
    try:
        if _state["wallet_manager"] is None:
            return {"ok": False, "message": "Not initialized"}
        created = await _state["wallet_manager"].create_wallets(req.count)
        if req.group_id and _state["group_manager"]:
            addresses = [w["address"] for w in created]
            await _state["group_manager"].add_bulk_to_group(addresses, req.group_id)
        logger.info(f"Created {len(created)} wallets")
        await broadcast_status()
        return {"ok": True, "created": len(created)}
    except Exception as e:
        logger.error(f"Create wallets error: {e}")
        return {"ok": False, "message": str(e)}


@app.post("/api/faucet/request")
async def request_faucet():
    try:
        if _state["faucet"] is None:
            return {"ok": False, "message": "Faucet not configured"}
        if _state["network"] is None:
            return {"ok": False, "message": "Network not initialized"}
        wallets = await _state["db"].get_all_wallets()
        if not wallets:
            return {"ok": False, "message": "No wallets"}
        success = 0
        for wallet in wallets:
            try:
                if await _state["faucet"].ensure_balance(_state["network"], wallet["address"]):
                    success += 1
            except Exception as e:
                logger.warning(f"Faucet error for {wallet['address'][:10]}: {e}")
        logger.info(f"Faucet: {success}/{len(wallets)} wallets refilled")
        await broadcast_status()
        return {"ok": True, "success": success, "total": len(wallets)}
    except Exception as e:
        logger.error(f"Faucet error: {e}")
        return {"ok": False, "message": str(e)}


@app.post("/api/farming/start")
async def start_farming():
    try:
        if _state["is_farming"]:
            return {"ok": False, "message": "Already farming"}
        if _state["db"] is None or _state["network"] is None:
            return {"ok": False, "message": "Not initialized"}
        wallets = await _state["db"].get_all_wallets()
        if not wallets:
            return {"ok": False, "message": "No wallets"}

        pool = FarmerPool(_state["config"], _state["db"], _state["network"])
        _state["pool"] = pool

        async def farming_loop():
            try:
                _state["is_farming"] = True
                await broadcast_status()
                logger.info("Farming started")
                while _state["is_farming"]:
                    await pool.run_cycle()
                    await broadcast_status()
                    await asyncio.sleep(1)
            except asyncio.CancelledError:
                logger.info("Farming cancelled")
            except Exception as e:
                logger.error(f"Farming error: {e}")
            finally:
                _state["is_farming"] = False
                await broadcast_status()

        _state["farming_task"] = asyncio.create_task(farming_loop())
        return {"ok": True, "message": "Farming started"}
    except Exception as e:
        logger.error(f"Start farming error: {e}")
        return {"ok": False, "message": str(e)}


@app.post("/api/farming/stop")
async def stop_farming():
    try:
        if not _state["is_farming"]:
            return {"ok": False, "message": "Not farming"}
        _state["is_farming"] = False
        if _state["farming_task"]:
            _state["farming_task"].cancel()
            try:
                await _state["farming_task"]
            except asyncio.CancelledError:
                pass
            _state["farming_task"] = None
        logger.info("Farming stopped")
        await broadcast_status()
        return {"ok": True, "message": "Farming stopped"}
    except Exception as e:
        logger.error(f"Stop farming error: {e}")
        return {"ok": False, "message": str(e)}


@app.get("/api/stats")
async def get_stats():
    try:
        if _state["db"] is None:
            return {"ok": False, "message": "DB not initialized"}
        stats = await _state["db"].get_stats()
        top = await _state["db"].get_top_wallets(10)
        cycles = await _state["db"].get_cycle_stats()
        return {"ok": True, "stats": stats, "top_wallets": top, "cycles": cycles}
    except Exception as e:
        logger.error(f"Stats error: {e}")
        return {"ok": False, "message": str(e)}


@app.get("/api/rpc/health")
async def rpc_health():
    try:
        if _state["network"] is None:
            return {"ok": False, "message": "Network not initialized"}
        diag = _state["network"].diagnostics()
        return {
            "ok": True,
            "rpc": {
                "urls": diag.get("rpc_urls", []),
                "active": diag.get("active", ""),
                "chain_id": diag.get("chain_id"),
                "avg_latency_ms": diag.get("avg_ms"),
                "total_calls": diag.get("calls", 0),
                "total_errors": diag.get("errors", 0),
                "error_rate": (
                    f"{diag['errors'] / max(diag['calls'], 1) * 100:.1f}%"
                    if diag.get("calls", 0) > 0
                    else "0%"
                ),
                "circuit_breaker": {
                    "state": diag.get("cb_state", "CLOSED"),
                    "failures": diag.get("cb_failures", 0),
                },
                "rpc_rate": diag.get("rpc_rate", 0),
                "nodes": diag.get("nodes", []),
            },
        }
    except Exception as e:
        logger.error(f"RPC health error: {e}")
        return {"ok": False, "message": str(e)}


@app.get("/api/networks")
async def list_networks():
    return {
        "ok": True,
        "current": _state["current_network"],
        "available": _state["available_networks"],
    }


@app.post("/api/networks/switch")
async def switch_network(req: SwitchNetworkRequest):
    try:
        if _state["is_farming"]:
            return {"ok": False, "message": "Stop farming before switching network"}

        config_path = f"config_{req.network}.yaml" if req.network != "default" else "config.yaml"
        if not Path(config_path).exists():
            config_path = f"config_{req.network}.yaml"
        if not Path(config_path).exists():
            return {"ok": False, "message": f"Config not found: {config_path}"}

        if _state["network"]:
            await _state["network"].close()
        if _state["db"]:
            await _state["db"].close()

        _state["config"] = None
        _state["network"] = None
        _state["db"] = None
        _state["wallet_manager"] = None
        _state["faucet"] = None
        _state["group_manager"] = None

        os.environ["HARVEST_NETWORK"] = req.network
        result = await init_system()
        if result.get("ok"):
            _state["current_network"] = req.network
            logger.info(f"Switched to network: {req.network}")
            await broadcast_event("network_switched", {"network": req.network})
        return result
    except Exception as e:
        logger.error(f"Switch network error: {e}")
        return {"ok": False, "message": str(e)}


@app.get("/api/scheduler/tasks")
async def list_schedule_tasks():
    if not _state["scheduler"]:
        return {"ok": False, "message": "Scheduler not initialized"}
    return {"ok": True, "tasks": _state["scheduler"].get_all_tasks()}


@app.post("/api/scheduler/tasks")
async def create_schedule_task(req: ScheduleCreateRequest):
    if not _state["scheduler"]:
        return {"ok": False, "message": "Scheduler not initialized"}
    try:
        task = _state["scheduler"].add_task(
            req.task_id,
            name=req.name,
            interval_hours=req.interval_hours,
            interval_minutes=req.interval_minutes,
            run_at_time=req.run_at_time,
            cycles=req.cycles,
        )
        await broadcast_event("scheduler_updated", {})
        return {"ok": True, "task": task.to_dict()}
    except Exception as e:
        return {"ok": False, "message": str(e)}


@app.put("/api/scheduler/tasks/{task_id}")
async def update_schedule_task(task_id: str, req: ScheduleUpdateRequest):
    if not _state["scheduler"]:
        return {"ok": False, "message": "Scheduler not initialized"}
    updates = {k: v for k, v in req.model_dump().items() if v is not None}
    task = _state["scheduler"].update_task(task_id, **updates)
    if not task:
        return {"ok": False, "message": "Task not found"}
    await broadcast_event("scheduler_updated", {})
    return {"ok": True, "task": task.to_dict()}


@app.delete("/api/scheduler/tasks/{task_id}")
async def delete_schedule_task(task_id: str):
    if not _state["scheduler"]:
        return {"ok": False, "message": "Scheduler not initialized"}
    ok = _state["scheduler"].remove_task(task_id)
    if not ok:
        return {"ok": False, "message": "Task not found"}
    await broadcast_event("scheduler_updated", {})
    return {"ok": True}


@app.post("/api/scheduler/start")
async def start_scheduler():
    if not _state["scheduler"]:
        return {"ok": False, "message": "Scheduler not initialized"}
    await _state["scheduler"].start()
    await broadcast_status()
    return {"ok": True, "message": "Scheduler started"}


@app.post("/api/scheduler/stop")
async def stop_scheduler():
    if not _state["scheduler"]:
        return {"ok": False, "message": "Scheduler not initialized"}
    await _state["scheduler"].stop()
    await broadcast_status()
    return {"ok": True, "message": "Scheduler stopped"}


async def _scheduled_farm(cycles: int):
    if _state["is_farming"]:
        logger.info("Scheduled farm skipped: already farming")
        return
    if _state["db"] is None or _state["network"] is None:
        logger.warning("Scheduled farm skipped: not initialized")
        return
    pool = FarmerPool(_state["config"], _state["db"], _state["network"])
    _state["pool"] = pool
    _state["is_farming"] = True
    await broadcast_status()
    try:
        for i in range(cycles):
            if not _state["is_farming"]:
                break
            logger.info("Scheduled cycle %d/%d", i + 1, cycles)
            await pool.run_cycle()
            await broadcast_status()
    except Exception as e:
        logger.error("Scheduled farm error: %s", e)
    finally:
        _state["is_farming"] = False
        await broadcast_status()


@app.get("/api/balance-alerts")
async def get_balance_alerts():
    return {
        "ok": True,
        "enabled": _state["balance_alerts_enabled"],
        "threshold": _state["balance_threshold"],
    }


@app.post("/api/balance-alerts")
async def set_balance_alerts(req: BalanceAlertsRequest):
    _state["balance_alerts_enabled"] = req.enabled
    _state["balance_threshold"] = req.threshold
    if req.enabled and not _state["balance_alerts_task"]:
        _state["balance_alerts_task"] = asyncio.create_task(_balance_monitor_loop())
    elif not req.enabled and _state["balance_alerts_task"]:
        _state["balance_alerts_task"].cancel()
        _state["balance_alerts_task"] = None
    await broadcast_status()
    return {"ok": True, "enabled": req.enabled, "threshold": req.threshold}


async def _balance_monitor_loop():
    logger.info("Balance monitor started")
    while _state["balance_alerts_enabled"]:
        try:
            if _state["network"] and _state["db"] and _state["faucet"]:
                addresses = await _state["db"].get_all_addresses()
                low_balance = []
                for addr in addresses:
                    try:
                        bal = await _state["network"].get_balance(addr)
                        if bal < _state["balance_threshold"]:
                            low_balance.append({"address": addr, "balance": bal})
                    except Exception:
                        pass
                if low_balance:
                    await broadcast_event(
                        "balance_alert",
                        {
                            "threshold": _state["balance_threshold"],
                            "low_balance_wallets": low_balance[:20],
                            "total_low": len(low_balance),
                        },
                    )
                    if _state["faucet"]:
                        for w in low_balance:
                            try:
                                await _state["faucet"].ensure_balance(
                                    _state["network"], w["address"]
                                )
                            except Exception as e:
                                logger.warning("Auto-faucet failed for %s: %s", w["address"][:10], e)
            await asyncio.sleep(30)
        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.error("Balance monitor error: %s", e)
            await asyncio.sleep(15)
    logger.info("Balance monitor stopped")


@app.get("/api/groups")
async def list_groups():
    if not _state["group_manager"]:
        return {"ok": False, "message": "Not initialized"}
    groups = await _state["group_manager"].get_all_groups()
    return {"ok": True, "groups": groups}


@app.post("/api/groups")
async def create_group(req: GroupCreateRequest):
    if not _state["group_manager"]:
        return {"ok": False, "message": "Not initialized"}
    try:
        group = await _state["group_manager"].create_group(req.name, req.color)
        return {"ok": True, "group": group}
    except ValueError as e:
        return {"ok": False, "message": str(e)}
    except Exception as e:
        return {"ok": False, "message": str(e)}


@app.delete("/api/groups/{group_id}")
async def delete_group(group_id: int):
    if not _state["group_manager"]:
        return {"ok": False, "message": "Not initialized"}
    ok = await _state["group_manager"].delete_group(group_id)
    return {"ok": ok, "message": "Deleted" if ok else "Not found"}


@app.post("/api/groups/{group_id}/wallets")
async def add_wallets_to_group(group_id: int, req: GroupAssignRequest):
    if not _state["group_manager"]:
        return {"ok": False, "message": "Not initialized"}
    count = await _state["group_manager"].add_bulk_to_group(req.addresses, group_id)
    return {"ok": True, "added": count}


@app.delete("/api/groups/{group_id}/wallets")
async def remove_wallets_from_group(group_id: int, req: GroupAssignRequest):
    if not _state["group_manager"]:
        return {"ok": False, "message": "Not initialized"}
    removed = 0
    for addr in req.addresses:
        if await _state["group_manager"].remove_wallet_from_group(addr, group_id):
            removed += 1
    return {"ok": True, "removed": removed}


@app.get("/api/groups/{group_id}/wallets")
async def get_group_wallets(group_id: int):
    if not _state["group_manager"]:
        return {"ok": False, "message": "Not initialized"}
    addresses = await _state["group_manager"].get_group_addresses(group_id)
    return {"ok": True, "addresses": addresses}


@app.get("/api/wallets/{address}/groups")
async def get_wallet_groups(address: str):
    if not _state["group_manager"]:
        return {"ok": False, "message": "Not initialized"}
    groups = await _state["group_manager"].get_wallet_groups(address)
    return {"ok": True, "groups": groups}


@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await websocket.accept()
    _state["clients"].append(websocket)
    try:
        status = await get_status()
        await websocket.send_text(json.dumps({"type": "status", **status}))
        for log in _state["logs"][-20:]:
            await websocket.send_text(json.dumps({"type": "log", "message": log, "level": "INFO"}))
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        pass
    except Exception as e:
        logger.error(f"WebSocket error: {e}")
    finally:
        if websocket in _state["clients"]:
            _state["clients"].remove(websocket)


def start_gui(host: str = "127.0.0.1", port: int = 8080):
    import uvicorn

    logger.info(f"Starting GUI on http://{host}:{port}")
    uvicorn.run(app, host=host, port=port, log_level="info")
