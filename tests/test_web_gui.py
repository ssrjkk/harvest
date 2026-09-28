"""Тесты веб-интерфейса (core/web_gui.py): API endpoints, WebSocket, scheduler/groups/alerts."""

import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi.testclient import TestClient

import core.web_gui as web_gui
from core.web_gui import app, _state


class TestWebGuiStatus(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(app)
        self._orig_state = dict(_state)

    def tearDown(self):
        for k in self._orig_state:
            _state[k] = self._orig_state[k]

    def test_root_returns_html(self):
        resp = self.client.get("/")
        self.assertEqual(resp.status_code, 200)
        self.assertIn("text/html", resp.headers["content-type"])

    def test_get_status_no_db(self):
        _state["db"] = None
        _state["is_farming"] = False
        _state["config"] = None
        _state["scheduler"] = None
        _state["current_network"] = "default"
        _state["balance_alerts_enabled"] = False
        resp = self.client.get("/api/status")
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertFalse(data["is_farming"])
        self.assertEqual(data["wallets"], 0)

    def test_get_status_with_db(self):
        mock_db = AsyncMock()
        mock_db.get_stats = AsyncMock(return_value={
            "total_wallets": 5,
            "total_actions": 100,
            "success_actions": 90,
        })
        _state["db"] = mock_db
        _state["is_farming"] = True
        _state["config"] = {"test": True}
        _state["scheduler"] = None
        _state["current_network"] = "testnet"
        _state["balance_alerts_enabled"] = True
        resp = self.client.get("/api/status")
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertTrue(data["is_farming"])
        self.assertEqual(data["wallets"], 5)
        self.assertEqual(data["actions"], 100)
        self.assertTrue(data["config_loaded"])

    def test_list_networks(self):
        _state["current_network"] = "default"
        _state["available_networks"] = ["default", "testnet"]
        resp = self.client.get("/api/networks")
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertTrue(data["ok"])
        self.assertEqual(data["current"], "default")
        self.assertIn("testnet", data["available"])


class TestWebGuiScheduler(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(app)
        self._orig_state = dict(_state)
        from core.scheduler import Scheduler
        self.scheduler = Scheduler(":memory:")
        _state["scheduler"] = self.scheduler

    def tearDown(self):
        for k in self._orig_state:
            _state[k] = self._orig_state[k]

    def test_list_schedule_tasks_empty(self):
        resp = self.client.get("/api/scheduler/tasks")
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertTrue(data["ok"])
        self.assertEqual(data["tasks"], [])

    def test_create_schedule_task(self):
        resp = self.client.post("/api/scheduler/tasks", json={
            "task_id": "t1",
            "name": "Test Task",
            "interval_hours": 2,
            "cycles": 3,
        })
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertTrue(data["ok"])
        self.assertEqual(data["task"]["task_id"], "t1")

    def test_update_schedule_task(self):
        self.scheduler.add_task("t1", name="Original", interval_hours=1)
        resp = self.client.put("/api/scheduler/tasks/t1", json={
            "name": "Updated",
            "enabled": False,
        })
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertTrue(data["ok"])
        self.assertEqual(data["task"]["name"], "Updated")

    def test_update_nonexistent_task(self):
        resp = self.client.put("/api/scheduler/tasks/nonexistent", json={"name": "X"})
        data = resp.json()
        self.assertFalse(data["ok"])

    def test_delete_schedule_task(self):
        self.scheduler.add_task("t1", name="Test")
        resp = self.client.delete("/api/scheduler/tasks/t1")
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertTrue(data["ok"])

    def test_delete_nonexistent_task(self):
        resp = self.client.delete("/api/scheduler/tasks/nonexistent")
        data = resp.json()
        self.assertFalse(data["ok"])

    def test_scheduler_not_initialized(self):
        _state["scheduler"] = None
        resp = self.client.get("/api/scheduler/tasks")
        data = resp.json()
        self.assertFalse(data["ok"])


class TestWebGuiGroups(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(app)
        self._orig_state = dict(_state)
        self.mock_gm = AsyncMock()
        _state["group_manager"] = self.mock_gm

    def tearDown(self):
        for k in self._orig_state:
            _state[k] = self._orig_state[k]

    def test_list_groups(self):
        self.mock_gm.get_all_groups = AsyncMock(return_value=[
            {"id": 1, "name": "Test", "color": "#000", "count": 5}
        ])
        resp = self.client.get("/api/groups")
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertTrue(data["ok"])
        self.assertEqual(len(data["groups"]), 1)

    def test_create_group(self):
        self.mock_gm.create_group = AsyncMock(return_value={
            "id": 1, "name": "New", "color": "#fff", "count": 0
        })
        resp = self.client.post("/api/groups", json={"name": "New", "color": "#fff"})
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertTrue(data["ok"])

    def test_create_group_duplicate(self):
        self.mock_gm.create_group = AsyncMock(side_effect=ValueError("already exists"))
        resp = self.client.post("/api/groups", json={"name": "Dup"})
        data = resp.json()
        self.assertFalse(data["ok"])

    def test_delete_group(self):
        self.mock_gm.delete_group = AsyncMock(return_value=True)
        resp = self.client.delete("/api/groups/1")
        data = resp.json()
        self.assertTrue(data["ok"])

    def test_add_wallets_to_group(self):
        self.mock_gm.add_bulk_to_group = AsyncMock(return_value=3)
        resp = self.client.post("/api/groups/1/wallets", json={
            "addresses": ["0x1", "0x2", "0x3"]
        })
        data = resp.json()
        self.assertTrue(data["ok"])
        self.assertEqual(data["added"], 3)

    def test_remove_wallets_from_group(self):
        self.mock_gm.remove_wallet_from_group = AsyncMock(return_value=True)
        resp = self.client.request(
            "DELETE",
            "/api/groups/1/wallets",
            json={"addresses": ["0x1", "0x2"]},
        )
        data = resp.json()
        self.assertTrue(data["ok"])
        self.assertEqual(data["removed"], 2)

    def test_get_group_wallets(self):
        self.mock_gm.get_group_addresses = AsyncMock(return_value=["0x1", "0x2"])
        resp = self.client.get("/api/groups/1/wallets")
        data = resp.json()
        self.assertTrue(data["ok"])
        self.assertEqual(len(data["addresses"]), 2)

    def test_get_wallet_groups(self):
        self.mock_gm.get_wallet_groups = AsyncMock(return_value=[
            {"id": 1, "name": "G1"}
        ])
        resp = self.client.get("/api/wallets/0xabc/groups")
        data = resp.json()
        self.assertTrue(data["ok"])
        self.assertEqual(len(data["groups"]), 1)

    def test_groups_not_initialized(self):
        _state["group_manager"] = None
        resp = self.client.get("/api/groups")
        data = resp.json()
        self.assertFalse(data["ok"])


class TestWebGuiBalanceAlerts(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(app)
        self._orig_state = dict(_state)
        _state["balance_alerts_enabled"] = False
        _state["balance_threshold"] = 0.0
        _state["balance_alerts_task"] = None

    def tearDown(self):
        if _state.get("balance_alerts_task"):
            _state["balance_alerts_task"].cancel()
        for k in self._orig_state:
            _state[k] = self._orig_state[k]

    def test_get_balance_alerts(self):
        resp = self.client.get("/api/balance-alerts")
        data = resp.json()
        self.assertTrue(data["ok"])
        self.assertFalse(data["enabled"])

    def test_set_balance_alerts(self):
        resp = self.client.post("/api/balance-alerts", json={
            "enabled": True,
            "threshold": 0.5,
        })
        data = resp.json()
        self.assertTrue(data["ok"])
        self.assertTrue(data["enabled"])
        self.assertEqual(data["threshold"], 0.5)


class TestWebGuiRpcHealth(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(app)
        self._orig_state = dict(_state)

    def tearDown(self):
        for k in self._orig_state:
            _state[k] = self._orig_state[k]

    def test_rpc_health_not_initialized(self):
        _state["network"] = None
        resp = self.client.get("/api/rpc/health")
        data = resp.json()
        self.assertFalse(data["ok"])

    def test_rpc_health_with_network(self):
        mock_network = MagicMock()
        mock_network.diagnostics = MagicMock(return_value={
            "rpc_urls": ["http://rpc1", "http://rpc2"],
            "active": "http://rpc1",
            "chain_id": 1,
            "avg_ms": 50.0,
            "calls": 100,
            "errors": 5,
            "cb_state": "CLOSED",
            "cb_failures": 0,
            "rpc_rate": 10.0,
            "nodes": [],
        })
        _state["network"] = mock_network
        resp = self.client.get("/api/rpc/health")
        data = resp.json()
        self.assertTrue(data["ok"])
        self.assertEqual(data["rpc"]["avg_latency_ms"], 50.0)


class TestWebGuiFarming(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(app)
        self._orig_state = dict(_state)
        _state["is_farming"] = False

    def tearDown(self):
        if _state.get("farming_task"):
            _state["farming_task"].cancel()
        for k in self._orig_state:
            _state[k] = self._orig_state[k]

    def test_start_farming_not_initialized(self):
        _state["db"] = None
        _state["network"] = None
        resp = self.client.post("/api/farming/start")
        data = resp.json()
        self.assertFalse(data["ok"])

    def test_stop_farming_not_farming(self):
        _state["is_farming"] = False
        resp = self.client.post("/api/farming/stop")
        data = resp.json()
        self.assertFalse(data["ok"])

    def test_start_farming_no_wallets(self):
        mock_db = AsyncMock()
        mock_db.get_all_wallets = AsyncMock(return_value=[])
        _state["db"] = mock_db
        _state["network"] = MagicMock()
        resp = self.client.post("/api/farming/start")
        data = resp.json()
        self.assertFalse(data["ok"])

    @patch("core.web_gui.FarmerPool")
    def test_start_farming_success(self, mock_pool_cls):
        mock_db = AsyncMock()
        mock_db.get_all_wallets = AsyncMock(return_value=[{"address": "0x1"}])
        mock_network = MagicMock()
        mock_pool = AsyncMock()
        mock_pool.run_cycle = AsyncMock()
        mock_pool_cls.return_value = mock_pool

        _state["db"] = mock_db
        _state["network"] = mock_network
        _state["config"] = {"test": True}
        _state["is_farming"] = False

        resp = self.client.post("/api/farming/start")
        data = resp.json()
        self.assertTrue(data["ok"])
        self.assertIn("Farming started", data["message"])


class TestWebGuiInit(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(app)
        self._orig_state = dict(_state)

    def tearDown(self):
        for k in self._orig_state:
            _state[k] = self._orig_state[k]

    def test_init_already_initialized(self):
        _state["config"] = {"test": True}
        resp = self.client.post("/api/init")
        data = resp.json()
        self.assertTrue(data["ok"])
        self.assertIn("Already", data["message"])

    @patch("core.web_gui.load_config")
    @patch("core.web_gui.enforce_license_async")
    @patch("core.web_gui.resolve_master_key")
    @patch("core.web_gui.Database")
    @patch("core.web_gui.NetworkManager")
    @patch("core.web_gui.WalletManager")
    @patch("core.web_gui.Scheduler")
    def test_init_success(self, mock_scheduler_cls, mock_wm, mock_nm, mock_db_cls,
                          mock_resolve, mock_license, mock_load_config):
        _state["config"] = None
        mock_load_config.return_value = {
            "database": {"path": ":memory:"},
            "faucet": {"enabled": False},
        }
        mock_db = AsyncMock()
        mock_db.init = AsyncMock()
        mock_db.get_group_manager = MagicMock(return_value=AsyncMock())
        mock_db_cls.return_value = mock_db

        mock_network = AsyncMock()
        mock_network.init = AsyncMock()
        mock_nm.return_value = mock_network

        mock_scheduler = AsyncMock()
        mock_scheduler.load = AsyncMock()
        mock_scheduler.start = AsyncMock()
        mock_scheduler.set_run_callback = MagicMock()
        mock_scheduler_cls.return_value = mock_scheduler

        resp = self.client.post("/api/init")
        data = resp.json()
        self.assertTrue(data["ok"])


class TestWebGuiFaucet(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(app)
        self._orig_state = dict(_state)

    def tearDown(self):
        for k in self._orig_state:
            _state[k] = self._orig_state[k]

    def test_faucet_not_configured(self):
        _state["faucet"] = None
        resp = self.client.post("/api/faucet/request")
        data = resp.json()
        self.assertFalse(data["ok"])

    def test_faucet_network_not_initialized(self):
        _state["faucet"] = MagicMock()
        _state["network"] = None
        resp = self.client.post("/api/faucet/request")
        data = resp.json()
        self.assertFalse(data["ok"])

    def test_faucet_no_wallets(self):
        mock_db = AsyncMock()
        mock_db.get_all_wallets = AsyncMock(return_value=[])
        _state["db"] = mock_db
        _state["faucet"] = MagicMock()
        _state["network"] = MagicMock()
        resp = self.client.post("/api/faucet/request")
        data = resp.json()
        self.assertFalse(data["ok"])

    def test_faucet_success(self):
        mock_db = AsyncMock()
        mock_db.get_all_wallets = AsyncMock(return_value=[{"address": "0x1"}])
        mock_faucet = AsyncMock()
        mock_faucet.ensure_balance = AsyncMock(return_value=True)
        _state["db"] = mock_db
        _state["faucet"] = mock_faucet
        _state["network"] = MagicMock()
        resp = self.client.post("/api/faucet/request")
        data = resp.json()
        self.assertTrue(data["ok"])
        self.assertEqual(data["success"], 1)


class TestWebGuiWallets(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(app)
        self._orig_state = dict(_state)

    def tearDown(self):
        for k in self._orig_state:
            _state[k] = self._orig_state[k]

    def test_create_wallets_not_initialized(self):
        _state["wallet_manager"] = None
        resp = self.client.post("/api/wallets/create", json={"count": 5})
        data = resp.json()
        self.assertFalse(data["ok"])

    def test_create_wallets_success(self):
        mock_wm = AsyncMock()
        mock_wm.create_wallets = AsyncMock(return_value=[
            {"address": "0x1"}, {"address": "0x2"}
        ])
        _state["wallet_manager"] = mock_wm
        _state["group_manager"] = None
        resp = self.client.post("/api/wallets/create", json={"count": 2})
        data = resp.json()
        self.assertTrue(data["ok"])
        self.assertEqual(data["created"], 2)

    def test_create_wallets_with_group(self):
        mock_wm = AsyncMock()
        mock_wm.create_wallets = AsyncMock(return_value=[{"address": "0x1"}])
        mock_gm = AsyncMock()
        mock_gm.add_bulk_to_group = AsyncMock(return_value=1)
        _state["wallet_manager"] = mock_wm
        _state["group_manager"] = mock_gm
        resp = self.client.post("/api/wallets/create", json={"count": 1, "group_id": 1})
        data = resp.json()
        self.assertTrue(data["ok"])
        mock_gm.add_bulk_to_group.assert_called_once()


class TestWebGuiStats(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(app)
        self._orig_state = dict(_state)

    def tearDown(self):
        for k in self._orig_state:
            _state[k] = self._orig_state[k]

    def test_stats_not_initialized(self):
        _state["db"] = None
        resp = self.client.get("/api/stats")
        data = resp.json()
        self.assertFalse(data["ok"])

    def test_stats_success(self):
        mock_db = AsyncMock()
        mock_db.get_stats = AsyncMock(return_value={
            "total_wallets": 10,
            "total_actions": 100,
            "success_actions": 95,
        })
        mock_db.get_top_wallets = AsyncMock(return_value=[
            {"address": "0x1", "actions": 50}
        ])
        mock_db.get_cycle_stats = AsyncMock(return_value={"cycles": 5})
        _state["db"] = mock_db
        resp = self.client.get("/api/stats")
        data = resp.json()
        self.assertTrue(data["ok"])
        self.assertEqual(data["stats"]["total_wallets"], 10)
        self.assertEqual(len(data["top_wallets"]), 1)


class TestBroadcastFunctions(unittest.TestCase):
    def setUp(self):
        self._orig_state = dict(_state)
        _state["clients"] = []

    def tearDown(self):
        for k in self._orig_state:
            _state[k] = self._orig_state[k]

    def test_broadcast_status_no_clients(self):
        import asyncio
        _state["db"] = None
        asyncio.run(web_gui.broadcast_status())

    def test_broadcast_event_no_clients(self):
        import asyncio
        asyncio.run(web_gui.broadcast_event("test", {"data": "value"}))

    def test_broadcast_status_with_clients(self):
        import asyncio
        mock_client = AsyncMock()
        _state["clients"] = [mock_client]
        _state["db"] = None
        _state["is_farming"] = False
        _state["scheduler"] = None
        _state["current_network"] = "default"
        _state["balance_alerts_enabled"] = False
        asyncio.run(web_gui.broadcast_status())
        mock_client.send_text.assert_called()

    def test_broadcast_event_with_clients(self):
        import asyncio
        mock_client = AsyncMock()
        _state["clients"] = [mock_client]
        asyncio.run(web_gui.broadcast_event("test_event", {"key": "value"}))
        mock_client.send_text.assert_called()


class TestBalanceMonitorLoop(unittest.TestCase):
    def setUp(self):
        self._orig_state = dict(_state)

    def tearDown(self):
        for k in self._orig_state:
            _state[k] = self._orig_state[k]

    def test_balance_monitor_disabled(self):
        import asyncio
        _state["balance_alerts_enabled"] = False
        asyncio.run(web_gui._balance_monitor_loop())

    @patch("core.web_gui.asyncio.sleep")
    def test_balance_monitor_one_iteration(self, mock_sleep):
        import asyncio
        mock_sleep.side_effect = [None, asyncio.CancelledError()]

        mock_db = AsyncMock()
        mock_db.get_all_addresses = AsyncMock(return_value=["0x1"])
        mock_network = AsyncMock()
        mock_network.get_balance = AsyncMock(return_value=0.1)
        mock_faucet = AsyncMock()
        mock_faucet.ensure_balance = AsyncMock(return_value=True)

        _state["balance_alerts_enabled"] = True
        _state["balance_threshold"] = 0.5
        _state["db"] = mock_db
        _state["network"] = mock_network
        _state["faucet"] = mock_faucet
        _state["clients"] = []

        try:
            asyncio.run(web_gui._balance_monitor_loop())
        except asyncio.CancelledError:
            pass


class TestWebSocketEndpoint(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(app)
        self._orig_state = dict(_state)
        _state["db"] = None
        _state["is_farming"] = False
        _state["config"] = None
        _state["scheduler"] = None
        _state["current_network"] = "default"
        _state["balance_alerts_enabled"] = False
        _state["logs"] = []

    def tearDown(self):
        for k in self._orig_state:
            _state[k] = self._orig_state[k]

    def test_websocket_connect(self):
        with self.client.websocket_connect("/ws") as websocket:
            data = websocket.receive_json()
            self.assertEqual(data["type"], "status")


class TestWebSocketLogHandler(unittest.TestCase):
    def setUp(self):
        self._orig_state = dict(_state)
        _state["logs"] = []
        _state["clients"] = []

    def tearDown(self):
        for k in self._orig_state:
            _state[k] = self._orig_state[k]

    def test_log_handler_emit(self):
        import logging
        handler = web_gui.WebSocketLogHandler()
        handler.setFormatter(logging.Formatter("%(message)s"))
        record = logging.LogRecord(
            name="test", level=logging.INFO, pathname="", lineno=0,
            msg="Test message", args=(), exc_info=None
        )
        handler.emit(record)
        self.assertEqual(len(_state["logs"]), 1)
        self.assertIn("Test message", _state["logs"][0])

    def test_log_handler_max_logs(self):
        import logging
        handler = web_gui.WebSocketLogHandler()
        handler.setFormatter(logging.Formatter("%(message)s"))
        for i in range(250):
            record = logging.LogRecord(
                name="test", level=logging.INFO, pathname="", lineno=0,
                msg=f"Message {i}", args=(), exc_info=None
            )
            handler.emit(record)
        self.assertLessEqual(len(_state["logs"]), web_gui.MAX_LOGS)


class TestLifespan(unittest.TestCase):
    def test_lifespan_context(self):
        import asyncio
        async def run_lifespan():
            async with web_gui.lifespan(app):
                pass
        asyncio.run(run_lifespan())


class TestScanNetworks(unittest.TestCase):
    def test_scan_networks(self):
        with tempfile.TemporaryDirectory() as td:
            original_cwd = Path.cwd()
            try:
                import os
                os.chdir(td)
                Path("config.yaml").touch()
                Path("config_testnet.yaml").touch()
                names = web_gui._scan_networks()
                self.assertIn("default", names)
                self.assertIn("testnet", names)
            finally:
                os.chdir(original_cwd)


class TestScheduledFarm(unittest.TestCase):
    def setUp(self):
        self._orig_state = dict(_state)

    def tearDown(self):
        for k in self._orig_state:
            _state[k] = self._orig_state[k]

    def test_scheduled_farm_already_farming(self):
        import asyncio
        _state["is_farming"] = True
        asyncio.run(web_gui._scheduled_farm(1))

    def test_scheduled_farm_not_initialized(self):
        import asyncio
        _state["is_farming"] = False
        _state["db"] = None
        _state["network"] = None
        asyncio.run(web_gui._scheduled_farm(1))

    @patch("core.web_gui.FarmerPool")
    def test_scheduled_farm_success(self, mock_pool_cls):
        import asyncio
        mock_pool = AsyncMock()
        mock_pool.run_cycle = AsyncMock()
        mock_pool_cls.return_value = mock_pool

        _state["is_farming"] = False
        _state["db"] = MagicMock()
        _state["network"] = MagicMock()
        _state["config"] = {"test": True}

        asyncio.run(web_gui._scheduled_farm(2))
        self.assertEqual(mock_pool.run_cycle.call_count, 2)


class TestNetworkSwitch(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(app)
        self._orig_state = dict(_state)

    def tearDown(self):
        for k in self._orig_state:
            _state[k] = self._orig_state[k]

    def test_switch_network_while_farming(self):
        _state["is_farming"] = True
        resp = self.client.post("/api/networks/switch", json={"network": "testnet"})
        data = resp.json()
        self.assertFalse(data["ok"])

    def test_switch_network_config_not_found(self):
        _state["is_farming"] = False
        _state["network"] = None
        _state["db"] = None
        resp = self.client.post("/api/networks/switch", json={"network": "nonexistent"})
        data = resp.json()
        self.assertFalse(data["ok"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
