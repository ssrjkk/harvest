"""Тесты пайплайна auto.py: остановка при мёртвом кране и пустых кошельках.

Главное проверяемое поведение: если кран недоступен и на кошельках нет
средств, прогон НЕ должен уходить в фарм — каждый перевод тогда заведомо
упадёт («insufficient funds»), сеть сожжёт лимит, а лог заполнится
бесполезными отказами. Раньше ферма продолжала работу вхолостую
(3778 таких отказов в живой БД от 16.10).
"""

import argparse
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import auto  # noqa: E402


class _FakeNet:
    """Сеть с заданными балансами; часть адресов отдаёт ошибку."""

    def __init__(self, balances: dict[str, float], fail: set[str] | None = None):
        self.balances = dict(balances)
        self.fail = fail or set()
        self.calls: list[str] = []

    async def get_balance(self, addr: str, refresh: bool = False) -> float:
        self.calls.append(addr)
        if addr in self.fail:
            raise ConnectionError("rpc down")
        return self.balances.get(addr, 0.0)

    async def close(self) -> None:
        pass


def _wallets(n: int) -> list[dict]:
    return [{"address": f"0x{i:040x}", "private_key": "0x" + "11" * 32} for i in range(n)]


class TestProbeFunded(unittest.IsolatedAsyncioTestCase):
    async def test_funded_wallet_found(self):
        net = _FakeNet({"0x" + f"{0:040x}": 0.05, "0x" + f"{1:040x}": 0.0})
        res = await auto._probe_funded(net, _wallets(2), {"faucet": {"min_balance": 0.005}})
        self.assertEqual(res["funded"], 1)
        self.assertEqual(res["checked"], 2)

    async def test_none_funded(self):
        net = _FakeNet({})
        res = await auto._probe_funded(net, _wallets(3), {"faucet": {"min_balance": 0.005}})
        self.assertEqual(res["funded"], 0)
        self.assertEqual(res["checked"], 3)

    async def test_network_error_skipped(self):
        # Ошибка на одном адресе не должна ломать проверку: остальные считаются.
        addrs = _wallets(2)
        net = _FakeNet({addrs[1]["address"]: 0.05}, fail={addrs[0]["address"]})
        res = await auto._probe_funded(net, addrs, {"faucet": {"min_balance": 0.005}})
        self.assertEqual(res["funded"], 1)
        self.assertEqual(res["checked"], 1)

    async def test_sample_limited(self):
        # Проверяем не все кошельки: 1000 RPC-вызовов ради проверки не нужно.
        net = _FakeNet({})
        res = await auto._probe_funded(net, _wallets(100), {"faucet": {}}, sample=5)
        self.assertEqual(res["checked"], 5)
        self.assertEqual(len(net.calls), 5)

    async def test_min_balance_from_config(self):
        addrs = _wallets(1)
        net = _FakeNet({addrs[0]["address"]: 0.01})
        res = await auto._probe_funded(net, addrs, {"faucet": {"min_balance": 0.05}})
        self.assertEqual(res["funded"], 0)
        res2 = await auto._probe_funded(net, addrs, {"faucet": {"min_balance": 0.005}})
        self.assertEqual(res2["funded"], 1)


def _args(**over) -> argparse.Namespace:
    base = dict(
        config="config.yaml",
        wallets=0,
        cycles=1,
        skip_faucet=False,
        skip_farm=False,
        export="wallets_export",
        batch=50,
        workers=0,
        rpc_threads=0,
        fast=False,
        dry_run=True,
        encrypt=False,
        decrypt=None,
        version=False,
        history=None,
        doctor=False,
        schedule=0.0,
        pnl_sample=25,
        pnl_out="pnl_report.html",
        skip_pnl=True,
        force_farm=False,
    )
    base.update(over)
    return argparse.Namespace(**base)


def _config(tmp: str) -> dict:
    return {
        "network": {"name": "T", "rpc_url": "http://127.0.0.1:1/rpc", "chain_id": 1},
        "wallets": {"count": 2, "generate_if_missing": False},
        "database": {"path": str(Path(tmp) / "s.db")},
        "faucet": {"enabled": True, "min_balance": 0.005, "target_balance": 0.05, "strategies": [
            {"type": "direct", "url": "http://127.0.0.1:1/claim"},
        ]},
        "farming": {"actions_per_cycle": [1, 1], "delay_between_actions": [0, 0],
                    "delay_between_cycles": [0, 0]},
        "threading": {"max_workers": 1, "rpc_threads": 1},
        "behavior": {"enabled": False},
        "actions": [],
        "advanced": {},
        "state_file": str(Path(tmp) / "state.json"),
    }


class TestRunOnceStopsWithoutFunds(unittest.IsolatedAsyncioTestCase):
    """run_once обязан остановиться, когда фармить бессмысленно."""

    async def test_aborts_when_faucet_dead_and_no_funds(self):
        with tempfile.TemporaryDirectory() as d:
            cfg = _config(d)
            addrs = [w["address"] for w in _wallets(2)]
            net = _FakeNet({a: 0.0 for a in addrs})
            db = mock.AsyncMock()
            db.get_all_wallets = mock.AsyncMock(return_value=[{"address": a} for a in addrs])
            db.count_wallets = mock.AsyncMock(return_value=2)
            db.close = mock.AsyncMock()
            faucet = mock.AsyncMock()
            faucet.validate = mock.AsyncMock(return_value=0)
            faucet.probe_report = mock.Mock(return_value=[])
            faucet.close = mock.AsyncMock()
            faucet.concurrent_batch = mock.Mock(return_value=1)

            with mock.patch.object(auto, "load_config", return_value=cfg), \
                mock.patch.object(auto, "setup_logging"), \
                mock.patch.object(auto, "banner"), \
                mock.patch.object(auto, "enforce_license_async", new=mock.AsyncMock()), \
                mock.patch.object(auto, "SingleInstance") as guard_cls, \
                mock.patch.object(auto, "Database", return_value=db), \
                mock.patch.object(auto, "NetworkManager", return_value=net), \
                mock.patch.object(auto, "Faucet", return_value=faucet), \
                mock.patch.object(auto, "FarmerPool") as pool_cls:
                guard_cls.return_value.acquire = mock.Mock(return_value=True)
                guard_cls.return_value.release = mock.Mock()
                code = await auto.run_once(_args())

            self.assertEqual(code, 1)
            # до пула не дошли — ни одной транзакции не отправлено
            pool_cls.assert_not_called()

    async def test_force_farm_bypasses_stop(self):
        with tempfile.TemporaryDirectory() as d:
            cfg = _config(d)
            addrs = [w["address"] for w in _wallets(2)]
            net = _FakeNet({a: 0.0 for a in addrs})
            db = mock.AsyncMock()
            db.get_all_wallets = mock.AsyncMock(return_value=[{"address": a} for a in addrs])
            db.count_wallets = mock.AsyncMock(return_value=2)
            db.get_stats = mock.AsyncMock(return_value={
                "total_wallets": 2, "total_actions": 0, "success_actions": 0,
            })
            db.close = mock.AsyncMock()
            faucet = mock.AsyncMock()
            faucet.validate = mock.AsyncMock(return_value=0)
            faucet.probe_report = mock.Mock(return_value=[])
            faucet.close = mock.AsyncMock()
            faucet.concurrent_batch = mock.Mock(return_value=1)
            pool = mock.AsyncMock()
            pool.run_once = mock.AsyncMock(return_value=[])
            pool.close = mock.AsyncMock()
            pool.network = mock.MagicMock()
            pool.network.diagnostics = mock.Mock(return_value={})

            with mock.patch.object(auto, "load_config", return_value=cfg), \
                mock.patch.object(auto, "setup_logging"), \
                mock.patch.object(auto, "banner"), \
                mock.patch.object(auto, "enforce_license_async", new=mock.AsyncMock()), \
                mock.patch.object(auto, "SingleInstance") as guard_cls, \
                mock.patch.object(auto, "Database", return_value=db), \
                mock.patch.object(auto, "NetworkManager", return_value=net), \
                mock.patch.object(auto, "Faucet", return_value=faucet), \
                mock.patch.object(auto, "FarmerPool", return_value=pool), \
                mock.patch.object(auto, "export_csv"), \
                mock.patch.object(auto, "export_json"):
                guard_cls.return_value.acquire = mock.Mock(return_value=True)
                guard_cls.return_value.release = mock.Mock()
                code = await auto.run_once(_args(force_farm=True))

            self.assertEqual(code, 0)
            pool.run_once.assert_awaited()


if __name__ == "__main__":
    unittest.main()
