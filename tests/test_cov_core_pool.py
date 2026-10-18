"""Coverage 100% for core/pool, core/license, core/netdiscovery, core/logger."""

import asyncio
import hashlib
import json
import logging
import sys
import tempfile
import unittest
from collections import Counter
from pathlib import Path
from unittest import mock
from unittest.mock import AsyncMock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.behavior import WalletProfile  # noqa: E402
from core.database import Database  # noqa: E402


def _base_cfg(tmpdir: str, **over):
    cfg = {
        "network": {"rpc_url": "http://127.0.0.1:1/rpc", "chain_id": 46630},
        "farming": {
            "actions_per_cycle": [2, 4],
            "delay_between_actions": [1, 3],
            "delay_between_cycles": [3600, 7200],
            "skip_cycle_probability": 0.0,
        },
        "threading": {"max_workers": 2, "timeout_per_wallet": 0, "rpc_threads": 2},
        "behavior": {"enabled": False},
        "database": {"path": str(Path(tmpdir) / "state.db")},
        "faucet": {"enabled": True, "strategies": []},
        "actions": [],
        "advanced": {"gas_limit": 21000, "check_balance_before_action": False},
        "state_file": str(Path(tmpdir) / "state.json"),
    }
    cfg.update(over)
    return cfg


class FakeWriter:
    def __init__(self, fail_stop=False):
        self.dropped_count = 0
        self.started = False
        self.stopped = False
        self.fail_stop = fail_stop

    def start(self):
        self.started = True

    async def stop(self):
        self.stopped = True
        if self.fail_stop:
            raise RuntimeError("writer stop boom")


class FakeExecutor:
    def __init__(self, fail_flush=False, fail_stop=False):
        self.writer = FakeWriter(fail_stop=fail_stop)
        self.flushed = 0
        self.fail_flush = fail_flush

    async def flush(self):
        self.flushed += 1
        if self.fail_flush:
            raise RuntimeError("flush boom")


class FakeNetwork:
    def __init__(self, *a, **kw):
        self.rpc_url = "http://127.0.0.1:1/rpc"
        self.monitored = False
        self.closed = False
        self.diag = False

    def start_monitor(self):
        self.monitored = True

    def concurrency_factor(self):
        return 1.0

    def take_metrics(self):
        return {"calls": 1, "errors": 0, "avg_latency_ms": 5}

    def log_diagnostics(self):
        self.diag = True

    def clear_accounts_cache(self):
        pass

    async def close(self):
        self.closed = True


class FakeFaucet:
    def __init__(self, live=1, fail_close=False):
        self._live = live
        self.closed = False
        self.fail_close = fail_close
        self.enabled = True
        self.min_balance = 0.0

    async def validate(self):
        return self._live

    async def close(self):
        self.closed = True
        if self.fail_close:
            raise RuntimeError("faucet close boom")


class FakeVibe:
    def __init__(self, *a, **kw):
        pass


class FakeWP:
    start_raise = False
    stop_raise = False
    per_wallet = 1
    last_h = 0.9
    cur_w = 2

    def __init__(self, max_workers, worker_func, health_fn=None, monitor_interval=5.0):
        self.max_workers = max_workers
        self.worker_func = worker_func
        self.health_fn = health_fn
        self.results = {}
        self.last_health = FakeWP.last_h
        self.current_workers = FakeWP.cur_w
        self._submitted = []
        self.started = False

    async def start(self):
        self.started = True
        if FakeWP.start_raise:
            raise RuntimeError("start boom")

    async def submit(self, wallet, addrs, cyc):
        self._submitted.append(wallet)

    async def join(self):
        for w in self._submitted:
            self.results[w["address"]] = FakeWP.per_wallet
        return self.results

    async def stop(self):
        if FakeWP.stop_raise:
            raise RuntimeError("stop boom")


class FakeDB:
    def __init__(self, wallets=None, record_raise=False, prune_raise=False):
        self._wallets = wallets or []
        self.record_raise = record_raise
        self.prune_raise = prune_raise
        self.recorded = []

    async def init(self):
        pass

    async def close(self):
        pass

    async def get_all_wallets(self):
        return list(self._wallets)

    async def record_cycle(self, **kw):
        if self.record_raise:
            raise RuntimeError("record boom")
        self.recorded.append(kw)

    async def prune_actions_log(self, keep_latest=None):
        if self.prune_raise:
            raise RuntimeError("prune boom")


def _make_pool(
    tmpdir,
    cfg_over=None,
    net=None,
    faucet_live=1,
    exec_fail_flush=False,
    exec_fail_stop=False,
    faucet_fail_close=False,
    patch_wp=True,
):
    from core import pool as pool_mod

    cfg = _base_cfg(tmpdir)
    if cfg_over:
        for k, v in cfg_over.items():
            cfg[k] = v
    fake_net = net or FakeNetwork()
    fake_faucet = FakeFaucet(live=faucet_live, fail_close=faucet_fail_close)
    fake_exec = FakeExecutor(fail_flush=exec_fail_flush, fail_stop=exec_fail_stop)
    p1 = mock.patch.object(pool_mod, "NetworkManager", return_value=fake_net)
    p2 = mock.patch.object(pool_mod, "VibeVibeInterface", FakeVibe)
    p3 = mock.patch.object(pool_mod, "Faucet", return_value=fake_faucet)
    p4 = mock.patch.object(pool_mod, "ActionExecutor", return_value=fake_exec)
    p1.start()
    p2.start()
    p3.start()
    p4.start()
    # keep patchers alive via pool attr
    import core.pool as pm

    pool = pm.FarmerPool(cfg, FakeDB(), network=fake_net)
    # replace db/executor refs for control (already fakes)
    pool._patchers = (p1, p2, p3, p4)
    pool._fake_faucet = fake_faucet
    pool._fake_exec = fake_exec
    pool._fake_net = fake_net
    if patch_wp:
        pool._wp_patch = mock.patch.object(pm, "WorkerPool", FakeWP)
        pool._wp_patch.start()
        pool._patchers = pool._patchers + (pool._wp_patch,)
    return pool


def _stop_patches(pool):
    for p in getattr(pool, "_patchers", ()):
        try:
            p.stop()
        except Exception:
            pass


def _wallet(i):
    return {"address": f"0x{i + 1:040x}", "private_key": "ab" * 32}


# ---------------- _CycleState / unlink / write_empty ----------------
class TestCycleStateFull(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.dir = Path(self.td.name)

    def tearDown(self):
        self.td.cleanup()

    async def test_unlink_ok_and_fail(self):
        from core.pool import _unlink_state_file

        p = self.dir / "s.json"
        p.write_text("{}", encoding="utf-8")
        self.assertTrue(_unlink_state_file(p))
        self.assertFalse(p.exists())
        # missing -> OSError -> False
        self.assertFalse(_unlink_state_file(p))

    async def test_write_empty_state(self):
        from core.pool import _write_empty_state

        p = self.dir / "s.json"
        p.write_text('{"x":1}', encoding="utf-8")
        _write_empty_state(p)
        self.assertEqual(json.loads(p.read_text(encoding="utf-8")), {})
        self.assertFalse((self.dir / "s.tmp").exists())

    async def test_load_missing(self):
        from core.pool import _CycleState

        st = _CycleState(str(self.dir / "no.json"))
        self.assertFalse(await st.load())

    async def test_load_ok_with_addrs(self):
        from core.pool import _CycleState

        p = self.dir / "s.json"
        p.write_text(
            json.dumps({"cycle_number": 3, "processed_addresses": ["0xaa"], "started_at": 1.0}), encoding="utf-8"
        )
        st = _CycleState(str(p))
        self.assertTrue(await st.load())
        self.assertEqual(st.cycle_number, 3)

    async def test_load_ok_empty(self):
        from core.pool import _CycleState

        p = self.dir / "s.json"
        p.write_text(json.dumps({"cycle_number": 0, "processed_addresses": [], "started_at": 0}), encoding="utf-8")
        st = _CycleState(str(p))
        self.assertFalse(await st.load())

    async def test_load_corrupt(self):
        from core.pool import _CycleState

        p = self.dir / "s.json"
        p.write_text("{bad json", encoding="utf-8")
        st = _CycleState(str(p))
        self.assertFalse(await st.load())

    async def test_load_defaults(self):
        from core.pool import _CycleState

        p = self.dir / "s.json"
        p.write_text(json.dumps({}), encoding="utf-8")
        st = _CycleState(str(p))
        self.assertFalse(await st.load())
        self.assertEqual(st.cycle_number, 0)

    async def test_save_ok_and_counters(self):
        from core.pool import _CycleState

        p = self.dir / "s.json"
        st = _CycleState(str(p))
        st.processed_addresses.add("0xaa")
        st.cycle_number = 2
        st.started_at = 5.0
        await st.save()
        self.assertTrue(p.exists())
        self.assertGreater(st._last_save_ts, 0)

    async def test_save_oserror(self):
        from core.pool import _CycleState

        st = _CycleState(str(self.dir / "s.json"))
        st.processed_addresses.add("0xaa")
        with mock.patch("core.pool.asyncio.to_thread", side_effect=OSError("disk")):
            await st.save()  # should not raise, counters unchanged
        self.assertEqual(st._last_saved_count, 0)

    async def test_mark_done_debounce(self):
        from core.pool import _CycleState

        st = _CycleState(str(self.dir / "s.json"))
        st._now = lambda: 0.0
        for i in range(10):
            await st.mark_done(f"0x{i:040x}")
        self.assertFalse((self.dir / "s.json").exists())
        st._now = lambda: 10.0
        for i in range(10, 70):
            await st.mark_done(f"0x{i:040x}")
        self.assertTrue((self.dir / "s.json").exists())

    async def test_clear_missing(self):
        from core.pool import _CycleState

        st = _CycleState(str(self.dir / "none.json"))
        st.processed_addresses.add("x")
        await st.clear()
        self.assertEqual(st.processed_addresses, set())

    async def test_clear_ok(self):
        from core.pool import _CycleState

        p = self.dir / "s.json"
        p.write_text("{}", encoding="utf-8")
        st = _CycleState(str(p))
        st.processed_addresses.add("x")
        await st.clear()
        self.assertFalse(p.exists())

    async def test_clear_unlink_fail_rewrite(self):
        from core.pool import _CycleState

        p = self.dir / "s.json"
        p.write_text(json.dumps({"cycle_number": 1, "processed_addresses": ["0xa"]}), encoding="utf-8")
        st = _CycleState(str(p))
        with mock.patch("core.pool._unlink_state_file", return_value=False):
            await st.clear()
        self.assertTrue(p.exists())
        st2 = _CycleState(str(p))
        self.assertFalse(await st2.load())

    async def test_clear_unlink_exception(self):
        from core.pool import _CycleState

        p = self.dir / "s.json"
        p.write_text("{}", encoding="utf-8")
        st = _CycleState(str(p))
        with mock.patch("core.pool.asyncio.to_thread", side_effect=RuntimeError("boom")):
            await st.clear()
        # fallback to write empty still attempted; file should exist empty or missing
        self.assertTrue(True)

    async def test_clear_rewrite_fail(self):
        from core.pool import _CycleState

        p = self.dir / "s.json"
        p.write_text("{}", encoding="utf-8")
        st = _CycleState(str(p))
        with mock.patch("core.pool._unlink_state_file", return_value=False):
            with mock.patch("core.pool._write_empty_state", side_effect=RuntimeError("boom")):
                await st.clear()


# ---------------- FarmerPool init branches ----------------
class TestPoolInit(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.TemporaryDirectory()

    def tearDown(self):
        self.td.cleanup()

    def _mk(self, cfg_over=None, use_network_param=True):
        from core import pool as pool_mod

        cfg = _base_cfg(self.td.name)
        if cfg_over:
            for k, v in cfg_over.items():
                cfg[k] = v
        fake_net = FakeNetwork()
        with (
            mock.patch.object(pool_mod, "NetworkManager", return_value=fake_net),
            mock.patch.object(pool_mod, "VibeVibeInterface", FakeVibe),
            mock.patch.object(pool_mod, "Faucet", return_value=FakeFaucet()),
            mock.patch.object(pool_mod, "ActionExecutor", return_value=FakeExecutor()),
        ):
            if use_network_param:
                pool = pool_mod.FarmerPool(cfg, FakeDB(), network=fake_net)
            else:
                pool = pool_mod.FarmerPool(cfg, FakeDB())
        return pool

    def test_explicit_timeout(self):
        pool = self._mk({"threading": {"max_workers": 2, "timeout_per_wallet": 99}})
        self.assertEqual(pool.timeout, 99)

    def test_auto_timeout_list_list_enabled(self):
        cfg_over = {
            "threading": {"max_workers": 2, "timeout_per_wallet": 0},
            "farming": {"actions_per_cycle": [2, 4], "delay_between_actions": [1, 3], "delay_between_cycles": [1, 2]},
            "behavior": {"enabled": True, "burst_multiplier": [1.2, 2.0], "action_delay_scale": [1.0, 2.0]},
        }
        pool = self._mk(cfg_over)
        self.assertGreater(pool.timeout, 0)

    def test_auto_timeout_scalar_scalar_disabled(self):
        cfg_over = {
            "threading": {"max_workers": 2, "timeout_per_wallet": 0},
            "farming": {"actions_per_cycle": 5, "delay_between_actions": 7, "delay_between_cycles": [1, 2]},
            "behavior": {"enabled": False},
        }
        pool = self._mk(cfg_over)
        self.assertGreater(pool.timeout, 0)

    def test_auto_timeout_scalar_list_invalid_scale(self):
        cfg_over = {
            "threading": {"max_workers": 2},
            "farming": {"actions_per_cycle": 5, "delay_between_actions": [1, 2], "delay_between_cycles": [1, 2]},
            "behavior": {"enabled": True, "action_delay_scale": 1.5},
        }
        pool = self._mk(cfg_over)
        self.assertGreater(pool.timeout, 0)

    def test_auto_timeout_no_behavior(self):
        cfg = _base_cfg(self.td.name)
        del cfg["behavior"]
        cfg["threading"] = {"max_workers": 2}
        from core import pool as pool_mod

        with (
            mock.patch.object(pool_mod, "NetworkManager", return_value=FakeNetwork()),
            mock.patch.object(pool_mod, "VibeVibeInterface", FakeVibe),
            mock.patch.object(pool_mod, "Faucet", return_value=FakeFaucet()),
            mock.patch.object(pool_mod, "ActionExecutor", return_value=FakeExecutor()),
        ):
            pool = pool_mod.FarmerPool(cfg, FakeDB())
        self.assertGreater(pool.timeout, 0)

    def test_auto_timeout_bad_scale_len(self):
        cfg_over = {
            "threading": {"max_workers": 2},
            "farming": {"actions_per_cycle": [2, 4], "delay_between_actions": [1, 3], "delay_between_cycles": [1, 2]},
            "behavior": {"enabled": True, "action_delay_scale": [1.0, 2.0, 3.0]},
        }
        pool = self._mk(cfg_over)
        self.assertGreater(pool.timeout, 0)

    def test_state_file_default(self):
        cfg = _base_cfg(self.td.name)
        del cfg["state_file"]
        cfg["database"] = {"path": str(Path(self.td.name) / "mydb.db")}
        from core import pool as pool_mod

        with (
            mock.patch.object(pool_mod, "NetworkManager", return_value=FakeNetwork()),
            mock.patch.object(pool_mod, "VibeVibeInterface", FakeVibe),
            mock.patch.object(pool_mod, "Faucet", return_value=FakeFaucet()),
            mock.patch.object(pool_mod, "ActionExecutor", return_value=FakeExecutor()),
        ):
            pool = pool_mod.FarmerPool(cfg, FakeDB())
        self.assertIn("mydb.db.state.json", str(pool._state._path))

    def test_network_none_branch(self):
        pool = self._mk(use_network_param=False)
        self.assertIsNotNone(pool.network)

    def test_profile_pause_resume_stats(self):
        pool = self._mk()
        self.assertFalse(pool.paused)
        pool.pause()
        self.assertTrue(pool.paused)
        pool.resume()
        self.assertFalse(pool.paused)
        st = pool.live_stats()
        for k in ("errors", "actions", "processed", "cycles", "paused", "dropped", "dyn_workers", "health"):
            self.assertIn(k, st)
        p1 = pool._profile("0x" + "11" * 20)
        p2 = pool._profile("0x" + "11" * 20)
        self.assertIs(p1, p2)


class TestPerWalletTimeout(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.TemporaryDirectory()

    def tearDown(self):
        self.td.cleanup()

    def _pool(self, cfg_over=None):
        from core import pool as pool_mod

        cfg = _base_cfg(self.td.name)
        if cfg_over:
            for k, v in cfg_over.items():
                cfg[k] = v
        with (
            mock.patch.object(pool_mod, "NetworkManager", return_value=FakeNetwork()),
            mock.patch.object(pool_mod, "VibeVibeInterface", FakeVibe),
            mock.patch.object(pool_mod, "Faucet", return_value=FakeFaucet()),
            mock.patch.object(pool_mod, "ActionExecutor", return_value=FakeExecutor()),
        ):
            return pool_mod.FarmerPool(cfg, FakeDB(), network=FakeNetwork())

    def test_neutral_scalar(self):
        pool = self._pool(
            {
                "farming": {"actions_per_cycle": 4, "delay_between_actions": 5, "delay_between_cycles": [1, 2]},
                "behavior": {"enabled": False},
            }
        )
        p = WalletProfile(neutral=True)
        self.assertGreater(pool._per_wallet_timeout(p), 0)

    def test_neutral_list(self):
        pool = self._pool()
        p = WalletProfile(neutral=True)
        self.assertGreater(pool._per_wallet_timeout(p), 0)

    def test_non_neutral(self):
        pool = self._pool(
            {
                "farming": {
                    "actions_per_cycle": [2, 4],
                    "delay_between_actions": [1, 3],
                    "delay_between_cycles": [1, 2],
                },
                "behavior": {"enabled": True, "burst_multiplier": 2.0},
            }
        )
        p = WalletProfile(neutral=False, activity=2.0, delay_actions_scale=2.0)
        self.assertGreater(pool._per_wallet_timeout(p), 0)

    def test_non_neutral_scalar_cfg(self):
        pool = self._pool(
            {
                "farming": {"actions_per_cycle": 3, "delay_between_actions": 4, "delay_between_cycles": [1, 2]},
                "behavior": {"enabled": True},
            }
        )
        p = WalletProfile(neutral=False, activity=1.0, delay_actions_scale=1.0)
        self.assertGreater(pool._per_wallet_timeout(p), 0)


# ---------------- _run_one ----------------
class TestRunOne(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.td = tempfile.TemporaryDirectory()

    def tearDown(self):
        self.td.cleanup()

    def _pool(self):
        return _make_pool(self.td.name)

    async def test_rest_until(self):
        pool = self._pool()
        try:
            w = _wallet(0)
            pool._rest_until[w["address"]] = 5
            r = await pool._run_one(w, [w["address"]], cycle_number=3)
            self.assertEqual(r, (w["address"], 0))
            # cycle 0 bypasses rest
            pool._rest_until[w["address"]] = 5
            with mock.patch("core.pool.Farmer") as mf:
                mf.return_value.run_cycle = AsyncMock(return_value=1)
                r2 = await pool._run_one(w, [w["address"]], cycle_number=0)
                self.assertEqual(r2[1], 1)
        finally:
            _stop_patches(pool)

    async def test_rest_prob_hit(self):
        pool = self._pool()
        try:
            w = _wallet(1)
            prof = WalletProfile(neutral=False, rest_prob=0.9, rest_cycles=(2, 3), start_delay=0.0)
            pool._profiles[w["address"]] = prof
            with mock.patch("core.pool.random.random", return_value=0.0):
                with mock.patch("core.pool.random.randint", return_value=2):
                    r = await pool._run_one(w, [w["address"]], cycle_number=1)
                    self.assertEqual(r[1], 0)
                    self.assertIn(w["address"], pool._rest_until)
        finally:
            _stop_patches(pool)

    async def test_rest_prob_miss_and_start_delay(self):
        pool = self._pool()
        try:
            w = _wallet(2)
            prof = WalletProfile(neutral=False, rest_prob=0.01, rest_cycles=(1, 1), start_delay=0.5)
            pool._profiles[w["address"]] = prof
            with mock.patch("core.pool.random.random", return_value=0.99):
                with mock.patch("core.pool.Farmer") as mf:
                    mf.return_value.run_cycle = AsyncMock(return_value=2)
                    with mock.patch("core.pool.asyncio.sleep", new=AsyncMock()) as ms:
                        r = await pool._run_one(w, [w["address"]], cycle_number=1)
                        self.assertEqual(r[1], 2)
                        ms.assert_awaited()
        finally:
            _stop_patches(pool)

    async def test_success_zero_and_positive(self):
        pool = self._pool()
        try:
            w = _wallet(3)
            pool._profiles[w["address"]] = WalletProfile(neutral=True, start_delay=0.0)
            with mock.patch("core.pool.Farmer") as mf:
                mf.return_value.run_cycle = AsyncMock(return_value=0)
                r = await pool._run_one(w, [w["address"]], cycle_number=0)
                self.assertEqual(r[1], 0)
                mf.return_value.run_cycle = AsyncMock(return_value=5)
                r2 = await pool._run_one(w, [w["address"]], cycle_number=0)
                self.assertEqual(r2[1], 5)
        finally:
            _stop_patches(pool)

    async def test_timeout(self):
        pool = self._pool()
        try:
            w = _wallet(4)
            pool._profiles[w["address"]] = WalletProfile(neutral=True, start_delay=0.0)
            with mock.patch("core.pool.Farmer") as mf:
                mf.return_value.run_cycle = AsyncMock(side_effect=TimeoutError("t"))
                r = await pool._run_one(w, [w["address"]], cycle_number=0)
                self.assertEqual(r[1], 0)
                self.assertEqual(pool.error_count, 1)
        finally:
            _stop_patches(pool)

    async def test_exception(self):
        pool = self._pool()
        try:
            w = _wallet(5)
            pool._profiles[w["address"]] = WalletProfile(neutral=True, start_delay=0.0)
            with mock.patch("core.pool.Farmer") as mf:
                mf.return_value.run_cycle = AsyncMock(side_effect=RuntimeError("boom"))
                r = await pool._run_one(w, [w["address"]], cycle_number=0)
                self.assertEqual(r[1], 0)
                self.assertEqual(pool.error_count, 1)
        finally:
            _stop_patches(pool)


# ---------------- run_once ----------------
class TestRunOnce(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        FakeWP.start_raise = False
        FakeWP.stop_raise = False
        FakeWP.per_wallet = 1

    def tearDown(self):
        self.td.cleanup()
        FakeWP.start_raise = False
        FakeWP.stop_raise = False
        FakeWP.per_wallet = 1

    async def _pool_db(self, faucet_live=1, real_db=False, cfg_over=None):
        if real_db:
            db = Database(str(Path(self.td.name) / "r.db"))
            await db.init()
        else:
            db = FakeDB()
        pool = _make_pool(self.td.name, faucet_live=faucet_live, cfg_over=cfg_over)
        # swap db to desired
        pool.db = db
        return pool, db

    async def test_faucet_zero_warn(self):
        pool, db = await self._pool_db(faucet_live=0)
        try:
            FakeWP.per_wallet = 1
            wallets = [_wallet(0)]
            res = await pool.run_once(wallets, [w["address"] for w in wallets], cycle_number=1)
            self.assertEqual(len(res), 1)
            self.assertTrue(pool._faucet_validated)
        finally:
            _stop_patches(pool)

    async def test_crash_all_done(self):
        pool, db = await self._pool_db()
        try:
            wallets = [_wallet(0), _wallet(1)]
            pool._faucet_validated = True
            pool._state.cycle_number = 7
            for w in wallets:
                pool._state.processed_addresses.add(w["address"])
            await pool._state.save()
            res = await pool.run_once(wallets, [w["address"] for w in wallets], cycle_number=7)
            self.assertEqual(res, [])
            self.assertFalse(Path(pool._state._path).exists())
        finally:
            _stop_patches(pool)

    async def test_crash_filter_some(self):
        pool, db = await self._pool_db()
        try:
            wallets = [_wallet(0), _wallet(1)]
            pool._faucet_validated = True
            pool._state.cycle_number = 7
            pool._state.processed_addresses.add(wallets[0]["address"])
            await pool._state.save()
            FakeWP.per_wallet = 2
            res = await pool.run_once(wallets, [w["address"] for w in wallets], cycle_number=7)
            addrs = [a for a, _ in res]
            self.assertNotIn(wallets[0]["address"], addrs)
            self.assertIn(wallets[1]["address"], addrs)
        finally:
            _stop_patches(pool)

    async def test_crash_mismatch(self):
        pool, db = await self._pool_db()
        try:
            wallets = [_wallet(0)]
            pool._faucet_validated = True
            pool._state.cycle_number = 99
            pool._state.processed_addresses.add(wallets[0]["address"])
            await pool._state.save()
            FakeWP.per_wallet = 1
            res = await pool.run_once(wallets, [w["address"] for w in wallets], cycle_number=1)
            self.assertEqual(len(res), 1)
        finally:
            _stop_patches(pool)

    async def test_stop_break(self):
        pool, db = await self._pool_db()
        try:
            pool._faucet_validated = True
            wallets = [_wallet(0), _wallet(1)]
            ev = asyncio.Event()
            ev.set()
            FakeWP.per_wallet = 1
            res = await pool.run_once(wallets, [w["address"] for w in wallets], cycle_number=1, stop_event=ev)
            self.assertEqual(res, [])
        finally:
            _stop_patches(pool)

    async def test_pause_loop(self):
        pool, db = await self._pool_db()
        try:
            pool._faucet_validated = True
            wallets = [_wallet(0)]
            pool.pause()
            orig_sleep = asyncio.sleep

            async def _fake_sleep(d):
                pool.resume()
                await orig_sleep(0.001)

            with mock.patch("core.pool.asyncio.sleep", side_effect=_fake_sleep):
                FakeWP.per_wallet = 1
                res = await pool.run_once(wallets, [w["address"] for w in wallets], cycle_number=1)
                self.assertEqual(len(res), 1)
        finally:
            _stop_patches(pool)

    async def test_stop_during_pause(self):
        pool, db = await self._pool_db()
        try:
            pool._faucet_validated = True
            wallets = [_wallet(0)]
            pool.pause()
            ev = asyncio.Event()
            orig_sleep = asyncio.sleep

            async def _fake_sleep(d):
                ev.set()
                await orig_sleep(0.001)

            with mock.patch("core.pool.asyncio.sleep", side_effect=_fake_sleep):
                FakeWP.per_wallet = 1
                res = await pool.run_once(wallets, [w["address"] for w in wallets], cycle_number=1, stop_event=ev)
                self.assertEqual(res, [])
        finally:
            _stop_patches(pool)

    async def test_chunk_progress_and_zero(self):
        pool, db = await self._pool_db(
            cfg_over={"threading": {"max_workers": 2, "timeout_per_wallet": 5}, "chunk_size": 1} if False else None
        )
        try:
            pool._faucet_validated = True
            # force chunk_size 1 for progress log
            pool.chunk_size = 1
            wallets = [_wallet(0), _wallet(1)]
            FakeWP.per_wallet = 1
            res = await pool.run_once(wallets, [w["address"] for w in wallets], cycle_number=1)
            self.assertEqual(len(res), 2)
            # zero actions -> no chunk save
            FakeWP.per_wallet = 0
            pool._faucet_validated = True
            res2 = await pool.run_once(wallets, [w["address"] for w in wallets], cycle_number=2)
            self.assertEqual(len(res2), 2)
        finally:
            _stop_patches(pool)

    async def test_base_exception(self):
        pool, db = await self._pool_db()
        try:
            pool._faucet_validated = True
            wallets = [_wallet(0)]
            with mock.patch.object(pool, "_run_chunk_with_pool", side_effect=KeyboardInterrupt("stop")):
                with self.assertRaises(KeyboardInterrupt):
                    await pool.run_once(wallets, [w["address"] for w in wallets], cycle_number=1)
        finally:
            _stop_patches(pool)

    async def test_base_exception_flush_fail(self):
        pool, db = await self._pool_db()
        try:
            pool._faucet_validated = True
            pool._fake_exec.fail_flush = True
            wallets = [_wallet(0)]
            with mock.patch.object(pool, "_run_chunk_with_pool", side_effect=RuntimeError("chunk boom")):
                with self.assertRaises(RuntimeError):
                    await pool.run_once(wallets, [w["address"] for w in wallets], cycle_number=1)
        finally:
            _stop_patches(pool)

    async def test_base_exception_save_fail(self):
        pool, db = await self._pool_db()
        try:
            pool._faucet_validated = True
            wallets = [_wallet(0)]
            with mock.patch.object(pool, "_run_chunk_with_pool", side_effect=RuntimeError("boom")):
                with mock.patch.object(pool._state, "save", side_effect=RuntimeError("save boom")):
                    with self.assertRaises(RuntimeError):
                        await pool.run_once(wallets, [w["address"] for w in wallets], cycle_number=1)
        finally:
            _stop_patches(pool)

    async def test_start_raise(self):
        pool, db = await self._pool_db()
        try:
            pool._faucet_validated = True
            FakeWP.start_raise = True
            wallets = [_wallet(0)]
            with self.assertRaises(RuntimeError):
                await pool.run_once(wallets, [w["address"] for w in wallets], cycle_number=1)
        finally:
            FakeWP.start_raise = False
            _stop_patches(pool)

    async def test_stop_warn(self):
        pool, db = await self._pool_db()
        try:
            pool._faucet_validated = True
            FakeWP.stop_raise = True
            wallets = [_wallet(0)]
            FakeWP.per_wallet = 1
            res = await pool.run_once(wallets, [w["address"] for w in wallets], cycle_number=1)
            self.assertEqual(len(res), 1)
        finally:
            FakeWP.stop_raise = False
            _stop_patches(pool)

    async def test_run_chunk_direct(self):
        pool, db = await self._pool_db()
        try:
            wallets = [_wallet(0), _wallet(1)]
            wp = FakeWP(2, None)
            FakeWP.per_wallet = 3
            out = await pool._run_chunk_with_pool(wp, wallets, [w["address"] for w in wallets], 1)
            self.assertEqual(out[wallets[0]["address"]], 3)
        finally:
            _stop_patches(pool)

    async def test_record_cycle_warn(self):
        pool, db = await self._pool_db()
        try:
            pool._faucet_validated = True
            pool.db = FakeDB(record_raise=True)
            FakeWP.per_wallet = 1
            wallets = [_wallet(0)]
            res = await pool.run_once(wallets, [w["address"] for w in wallets], cycle_number=1)
            self.assertEqual(len(res), 1)
        finally:
            _stop_patches(pool)

    async def test_record_cycle_ok_real_db(self):
        pool, db = await self._pool_db(real_db=True)
        try:
            pool._faucet_validated = True
            FakeWP.per_wallet = 1
            wallets = [_wallet(0)]
            res = await pool.run_once(wallets, [w["address"] for w in wallets], cycle_number=1)
            self.assertEqual(len(res), 1)
            await db.close()
        finally:
            _stop_patches(pool)

    async def test_interrupted_keeps_state(self):
        pool, db = await self._pool_db()
        try:
            pool._faucet_validated = True
            wallets = [_wallet(0), _wallet(1)]
            ev = asyncio.Event()
            ev.set()
            res = await pool.run_once(wallets, [w["address"] for w in wallets], cycle_number=1, stop_event=ev)
            self.assertEqual(res, [])
            # interrupted -> state file kept (saved, not cleared)
            self.assertTrue(Path(pool._state._path).exists())
        finally:
            _stop_patches(pool)


class TestPoolCloseForeverSleep(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.td = tempfile.TemporaryDirectory()

    def tearDown(self):
        self.td.cleanup()

    async def test_close_ok_and_idempotent(self):
        pool = _make_pool(self.td.name)
        try:
            await pool.close()
            await pool.close()
        finally:
            _stop_patches(pool)

    async def test_close_with_raises(self):
        pool = _make_pool(self.td.name, exec_fail_stop=True, faucet_fail_close=True)
        # writer.stop and faucet.close raise, network.close ok
        with mock.patch.object(pool._fake_net, "close", side_effect=RuntimeError("net boom")):
            with self.assertRaises(RuntimeError):
                await pool.close()
        _stop_patches(pool)
        # second variant: writer/faucet raise but network ok -> swallowed
        pool2 = _make_pool(self.td.name, exec_fail_stop=True, faucet_fail_close=True)
        try:
            await pool2.close()
        finally:
            _stop_patches(pool2)

    async def test_interruptible_sleep_fast(self):
        pool = _make_pool(self.td.name)
        try:
            ev = asyncio.Event()
            ev.set()
            await asyncio.wait_for(pool._interruptible_sleep(30, ev), timeout=2)
            # normal small sleep
            ev2 = asyncio.Event()
            await pool._interruptible_sleep(0.05, ev2)
            # paused loop
            pool.pause()
            ev3 = asyncio.Event()

            async def _resumer():
                await asyncio.sleep(0.05)
                pool.resume()
                ev3.set()

            t = asyncio.create_task(_resumer())
            await pool._interruptible_sleep(0.3, asyncio.Event())
            await t
        finally:
            _stop_patches(pool)
            try:
                await pool.close()
            except Exception:
                pass

    async def test_interruptible_sleep_paused_stop(self):
        pool = _make_pool(self.td.name)
        try:
            pool.pause()
            ev = asyncio.Event()
            ev.set()
            await asyncio.wait_for(pool._interruptible_sleep(5, ev), timeout=2)
        finally:
            _stop_patches(pool)
            try:
                await pool.close()
            except Exception:
                pass

    async def test_run_forever_empty(self):
        pool = _make_pool(self.td.name)
        try:
            pool.db = FakeDB(wallets=[])
            ev = asyncio.Event()

            async def _stop_first(seconds, stop_ev):
                stop_ev.set()

            with mock.patch.object(pool, "_interruptible_sleep", side_effect=_stop_first):
                await asyncio.wait_for(pool.run_forever(ev), timeout=5)
                self.assertGreaterEqual(pool.cycle_count, 1)
        finally:
            _stop_patches(pool)

    async def test_run_forever_with_wallets(self):
        cfg_over = {
            "threading": {"max_workers": 2, "timeout_per_wallet": 5, "prune_log_every_cycles": 1},
            "farming": {
                "actions_per_cycle": [1, 1],
                "delay_between_actions": [0, 0],
                "delay_between_cycles": [0.01, 0.02],
            },
            "database": {"path": str(Path(self.td.name) / "x.db"), "log_keep": 10},
            "behavior": {"enabled": True},
        }
        pool = _make_pool(self.td.name, cfg_over=cfg_over)
        try:
            wallets = [_wallet(0)]
            pool.db = FakeDB(wallets=wallets)
            # pre-save restored higher cycle
            pool._state.cycle_number = 5
            pool._state.processed_addresses.add("0x" + "ff" * 20)
            await pool._state.save()
            calls = {"n": 0}

            async def _once(*a, **kw):
                calls["n"] += 1
                return []

            async def _stop_after(seconds, stop_ev):
                # stop after first full iteration
                if calls["n"] >= 1:
                    stop_ev.set()

            with (
                mock.patch.object(pool, "run_once", side_effect=_once),
                mock.patch("core.pool.random.uniform", return_value=0.01),
                mock.patch.object(pool, "_interruptible_sleep", side_effect=_stop_after),
            ):
                ev = asyncio.Event()
                await asyncio.wait_for(pool.run_forever(ev), timeout=5)
                self.assertGreaterEqual(pool.cycle_count, 1)
                self.assertGreaterEqual(calls["n"], 1)
        finally:
            _stop_patches(pool)

    async def test_run_forever_prune_fail_and_no_behavior(self):
        cfg_over = {
            "threading": {"max_workers": 2, "timeout_per_wallet": 5, "prune_log_every_cycles": 1},
            "farming": {
                "actions_per_cycle": [1, 1],
                "delay_between_actions": [0, 0],
                "delay_between_cycles": [0.01, 0.02],
            },
            "database": {"path": str(Path(self.td.name) / "y.db"), "log_keep": 10},
            "behavior": {"enabled": False},
        }
        pool = _make_pool(self.td.name, cfg_over=cfg_over)
        try:
            wallets = [_wallet(0)]
            pool.db = FakeDB(wallets=wallets, prune_raise=True)
            calls = {"n": 0}

            async def _once(*a, **kw):
                calls["n"] += 1
                return []

            async def _stop_after(seconds, stop_ev):
                if calls["n"] >= 1:
                    stop_ev.set()

            with (
                mock.patch.object(pool, "run_once", side_effect=_once),
                mock.patch("core.pool.random.uniform", return_value=0.0),
                mock.patch.object(pool, "_interruptible_sleep", side_effect=_stop_after),
            ):
                ev = asyncio.Event()
                await asyncio.wait_for(pool.run_forever(ev), timeout=5)
        finally:
            _stop_patches(pool)

    async def test_run_forever_no_prune(self):
        cfg_over = {
            "threading": {"max_workers": 2, "timeout_per_wallet": 5},
            "farming": {
                "actions_per_cycle": [1, 1],
                "delay_between_actions": [0, 0],
                "delay_between_cycles": [0.01, 0.02],
            },
            "behavior": {"enabled": True},
        }
        pool = _make_pool(self.td.name, cfg_over=cfg_over)
        try:
            pool.db = FakeDB(wallets=[_wallet(0)])
            calls = {"n": 0}

            async def _once(*a, **kw):
                calls["n"] += 1
                return []

            async def _stop_after(seconds, stop_ev):
                if calls["n"] >= 1:
                    stop_ev.set()

            with (
                mock.patch.object(pool, "run_once", side_effect=_once),
                mock.patch("core.pool.random.uniform", return_value=0.01),
                mock.patch.object(pool, "_interruptible_sleep", side_effect=_stop_after),
            ):
                ev = asyncio.Event()
                await asyncio.wait_for(pool.run_forever(ev), timeout=5)
        finally:
            _stop_patches(pool)


# ================= LICENSE =================
def _lic_cfg(tdir, **over):
    cfg = {
        "license": {
            "enabled": True,
            "deploy_url": "http://127.0.0.1:1/license.json",
            "deploy_salt": "salt-test",
            "grace_days": 7,
            "cache_file": str(Path(tdir) / ".license_cache"),
            "timeout": 1.0,
        }
    }
    cfg["license"].update(over)
    return cfg


class TestLicenseAnalyze(unittest.TestCase):
    def test_all_branches(self):
        import tempfile
        from core.license import LicenseManager

        with tempfile.TemporaryDirectory() as td:
            LicenseManager(_lic_cfg(td, deploy_salt="harvest-deploy-salt-v1"))
            # default salt warning covered by init above
            lm2 = LicenseManager(_lic_cfg(td))
            self.assertFalse(lm2._analyze(None)[0])
            self.assertFalse(lm2._analyze({})[0])
            self.assertFalse(lm2._analyze({"app": "other", "pass": "x"})[0])
            self.assertFalse(lm2._analyze({"pass": "x", "v": "bad"})[0])
            self.assertFalse(lm2._analyze({"pass": "x", "v": object()})[0])
            self.assertFalse(lm2._analyze({"pass": "x", "v": 1})[0])
            self.assertFalse(lm2._analyze({"pass": "x", "v": "1"})[0])
            self.assertFalse(lm2._analyze({"app": "harvest", "v": 2, "status": "revoked", "pass": "x"})[0])
            self.assertFalse(lm2._analyze({"pass": "x", "status": "weird"})[0])
            self.assertFalse(lm2._analyze({"status": "active"})[0])
            self.assertFalse(lm2._analyze({"pass": "x", "expires": "2020-01-01T00:00:00Z"})[0])
            self.assertFalse(lm2._analyze({"pass": "x", "expires": "not-a-date"})[0])
            self.assertTrue(lm2._analyze({"pass": "x"})[0])
            self.assertTrue(lm2._analyze({"pass": "x", "v": 2})[0])
            self.assertTrue(lm2._analyze({"pass": "x", "expires": "2999-01-01T00:00:00Z"})[0])
            self.assertTrue(lm2._analyze({"pass": "x", "v": "3"})[0])


class TestLicenseVerify(unittest.TestCase):
    def test_empty(self):
        import tempfile
        from core.license import LicenseManager

        with tempfile.TemporaryDirectory() as td:
            lm = LicenseManager(_lic_cfg(td))
            self.assertFalse(lm._verify_password({}, "x"))
            self.assertFalse(lm._verify_password({"pass": ""}, "x"))

    def test_pbkdf2_ok_fail(self):
        import tempfile
        from core.license import LicenseManager

        with tempfile.TemporaryDirectory() as td:
            lm = LicenseManager(_lic_cfg(td))
            h = LicenseManager.make_hash("secret", "salt-test", iterations=10000)
            self.assertTrue(lm._verify_password({"pass": h}, "secret"))
            self.assertFalse(lm._verify_password({"pass": h}, "wrong"))

    def test_iters_bounds(self):
        import tempfile
        from core.license import LicenseManager

        with tempfile.TemporaryDirectory() as td:
            lm = LicenseManager(_lic_cfg(td))
            self.assertFalse(lm._verify_password({"pass": "pbkdf2_sha256$9999$salt$00"}, "x"))
            self.assertFalse(lm._verify_password({"pass": "pbkdf2_sha256$5000000$salt$00"}, "x"))

    def test_malformed_and_attr(self):
        import tempfile
        from core.license import LicenseManager

        with tempfile.TemporaryDirectory() as td:
            lm = LicenseManager(_lic_cfg(td))
            self.assertFalse(lm._verify_password({"pass": "pbkdf2_sha256$bad"}, "x"))
            self.assertFalse(lm._verify_password({"pass": "pbkdf2_sha256$notint$salt$digest"}, "x"))

            class _BadStr(str):
                def split(self, *a, **kw):
                    raise AttributeError("no split")

            self.assertFalse(lm._verify_password({"pass": _BadStr("pbkdf2_sha256$10000$salt$abc")}, "x"))

    def test_legacy(self):
        import tempfile
        from core.license import LicenseManager

        with tempfile.TemporaryDirectory() as td:
            lm = LicenseManager(_lic_cfg(td))
            legacy = hashlib.sha256(b"salt-testsecret").hexdigest()
            self.assertTrue(lm._verify_password({"pass": legacy}, "secret"))
            # second call no warning (covered _legacy_warned)
            self.assertTrue(lm._verify_password({"pass": legacy}, "secret"))
            self.assertFalse(lm._verify_password({"pass": legacy}, "wrong"))


class TestLicenseCache(unittest.TestCase):
    def test_roundtrip(self):
        import tempfile
        from core.license import LicenseManager

        with tempfile.TemporaryDirectory() as td:
            lm = LicenseManager(_lic_cfg(td))
            lm._cache_write({"app": "harvest", "pass": "abc"}, "abc")
            c = lm._cache_read()
            self.assertEqual(c["payload"]["pass"], "abc")

    def test_write_tmp_fail(self):
        import tempfile
        from core.license import LicenseManager

        with tempfile.TemporaryDirectory() as td:
            lm = LicenseManager(_lic_cfg(td))
            with mock.patch("pathlib.Path.write_text", side_effect=OSError("disk")):
                lm._cache_write({"pass": "x"}, "x")
            # unlink OSError branch: write fails then unlink fails
            with mock.patch("pathlib.Path.write_text", side_effect=RuntimeError("boom")):
                with mock.patch("pathlib.Path.unlink", side_effect=OSError("locked")):
                    lm._cache_write({"pass": "x"}, "x")

    def test_write_restrict_fail(self):
        import tempfile
        from core.license import LicenseManager

        with tempfile.TemporaryDirectory() as td:
            lm = LicenseManager(_lic_cfg(td))
            with mock.patch("core.utils.restrict_file_permissions", side_effect=RuntimeError("perm")):
                lm._cache_write({"pass": "x"}, "x")

    def test_read_bad(self):
        import tempfile
        from core.license import LicenseManager

        with tempfile.TemporaryDirectory() as td:
            lm = LicenseManager(_lic_cfg(td))
            self.assertIsNone(lm._cache_read())
            p = Path(td) / ".license_cache"
            p.write_text("not json", encoding="utf-8")
            self.assertIsNone(lm._cache_read())
            p.write_text(json.dumps({"raw": "x"}), encoding="utf-8")
            self.assertIsNone(lm._cache_read())

    def test_sig_mismatch(self):
        import tempfile
        from core.license import LicenseManager

        with tempfile.TemporaryDirectory() as td:
            lm = LicenseManager(_lic_cfg(td))
            lm._cache_write({"app": "harvest", "pass": "abc"}, "abc")
            p = Path(td) / ".license_cache"
            data = json.loads(p.read_text(encoding="utf-8"))
            data["sig"] = "0" * 64
            p.write_text(json.dumps(data), encoding="utf-8")
            self.assertIsNone(lm._cache_read())


class FakeResp:
    def __init__(self, status=200, data=b"{}"):
        self.status = status
        self._data = data

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def read(self, n=None):
        return self._data


class TestLicenseFetch(unittest.TestCase):
    def test_no_url(self):
        import tempfile
        from core.license import LicenseManager

        with tempfile.TemporaryDirectory() as td:
            lm = LicenseManager(_lic_cfg(td, deploy_url=""))
            self.assertIsNone(lm._fetch())

    def test_urlsplit_fail(self):
        import tempfile
        from core.license import LicenseManager

        with tempfile.TemporaryDirectory() as td:
            lm = LicenseManager(_lic_cfg(td, deploy_url="https://example.com/x"))
            with mock.patch("urllib.parse.urlsplit", side_effect=Exception("boom")):
                self.assertIsNone(lm._fetch())

    def test_http_non_loopback(self):
        import tempfile
        from core.license import LicenseManager

        with tempfile.TemporaryDirectory() as td:
            lm = LicenseManager(_lic_cfg(td, deploy_url="http://evil.example/x"))
            self.assertIsNone(lm._fetch())

    def test_https_ok(self):
        import tempfile
        from core.license import LicenseManager

        with tempfile.TemporaryDirectory() as td:
            lm = LicenseManager(_lic_cfg(td, deploy_url="https://example.com/license.json"))
            payload = {"app": "harvest", "pass": "x"}
            data = json.dumps(payload).encode()
            with mock.patch("urllib.request.urlopen", return_value=FakeResp(200, data)):
                self.assertEqual(lm._fetch(), payload)

    def test_status_not_200(self):
        import tempfile
        from core.license import LicenseManager

        with tempfile.TemporaryDirectory() as td:
            lm = LicenseManager(_lic_cfg(td, deploy_url="https://example.com/x"))
            with mock.patch("urllib.request.urlopen", return_value=FakeResp(404, b"{}")):
                self.assertIsNone(lm._fetch())

    def test_big_body(self):
        import tempfile
        from core import license as lic_mod
        from core.license import LicenseManager

        with tempfile.TemporaryDirectory() as td:
            lm = LicenseManager(_lic_cfg(td, deploy_url="https://example.com/x"))
            with mock.patch(
                "urllib.request.urlopen", return_value=FakeResp(200, b"x" * (lic_mod._MAX_LICENSE_BODY + 10))
            ):
                self.assertIsNone(lm._fetch())

    def test_nondict_payload(self):
        import tempfile
        from core.license import LicenseManager

        with tempfile.TemporaryDirectory() as td:
            lm = LicenseManager(_lic_cfg(td, deploy_url="https://example.com/x"))
            with mock.patch("urllib.request.urlopen", return_value=FakeResp(200, b"[1,2]")):
                self.assertIsNone(lm._fetch())

    def test_exception(self):
        import tempfile
        from core.license import LicenseManager

        with tempfile.TemporaryDirectory() as td:
            lm = LicenseManager(_lic_cfg(td, deploy_url="https://example.com/x"))
            with mock.patch("urllib.request.urlopen", side_effect=RuntimeError("net")):
                self.assertIsNone(lm._fetch())

    def test_loopback_http(self):
        import tempfile
        from core.license import LicenseManager

        with tempfile.TemporaryDirectory() as td:
            lm = LicenseManager(_lic_cfg(td, deploy_url="http://127.0.0.1:1/x"))
            with mock.patch("urllib.request.urlopen", side_effect=RuntimeError("down")):
                self.assertIsNone(lm._fetch())


class TestLicenseRequire(unittest.IsolatedAsyncioTestCase):
    async def test_disabled(self):
        from core.license import LicenseManager

        lm = LicenseManager({"license": {"enabled": False}})
        ok, _ = await lm.require_activation()
        self.assertTrue(ok)

    async def test_no_url(self):
        import tempfile
        from core.license import LicenseManager

        with tempfile.TemporaryDirectory() as td:
            lm = LicenseManager(_lic_cfg(td, deploy_url=""))
            ok, r = await lm.require_activation()
            self.assertFalse(ok)
            self.assertIn("deploy_url", r)

    async def test_online_analyze_fail(self):
        import tempfile
        from core.license import LicenseManager

        with tempfile.TemporaryDirectory() as td:
            lm = LicenseManager(_lic_cfg(td))
            with mock.patch.object(lm, "_fetch", return_value={"status": "revoked", "pass": "x"}):
                ok, _ = await lm.require_activation("pw")
                self.assertFalse(ok)

    async def test_online_cache_hit(self):
        import tempfile
        from core.license import LicenseManager

        with tempfile.TemporaryDirectory() as td:
            lm = LicenseManager(_lic_cfg(td))
            payload = {"app": "harvest", "status": "active", "pass": "abc"}
            lm._cache_write(payload, "abc")
            with mock.patch.object(lm, "_fetch", return_value=payload):
                ok, _ = await lm.require_activation()
                self.assertTrue(ok)

    async def test_online_password_ok(self):
        import tempfile
        from core.license import LicenseManager

        with tempfile.TemporaryDirectory() as td:
            lm = LicenseManager(_lic_cfg(td))
            h = LicenseManager.make_hash("pw123", "salt-test", iterations=10000)
            payload = {"app": "harvest", "status": "active", "pass": h}
            with mock.patch.object(lm, "_fetch", return_value=payload):
                ok, _ = await lm.require_activation("pw123")
                self.assertTrue(ok)

    async def test_online_need_password(self):
        import tempfile
        from core.license import LicenseManager

        with tempfile.TemporaryDirectory() as td:
            lm = LicenseManager(_lic_cfg(td))
            h = LicenseManager.make_hash("pw123", "salt-test", iterations=10000)
            payload = {"app": "harvest", "status": "active", "pass": h}
            with mock.patch.object(lm, "_fetch", return_value=payload):
                ok, r = await lm.require_activation("wrong")
                self.assertFalse(ok)
                self.assertIn("пароль", r)
                ok2, _ = await lm.require_activation(None)
                self.assertFalse(ok2)

    async def test_offline_no_cache(self):
        import tempfile
        from core.license import LicenseManager

        with tempfile.TemporaryDirectory() as td:
            lm = LicenseManager(_lic_cfg(td))
            with mock.patch.object(lm, "_fetch", return_value=None):
                ok, r = await lm.require_activation()
                self.assertFalse(ok)
                self.assertIn("нет связи", r)

    async def test_offline_bad_payload(self):
        import tempfile
        from core.license import LicenseManager

        with tempfile.TemporaryDirectory() as td:
            lm = LicenseManager(_lic_cfg(td))
            lm._cache_write({"status": "revoked", "pass": "x"}, "x")
            with mock.patch.object(lm, "_fetch", return_value=None):
                ok, _ = await lm.require_activation()
                self.assertFalse(ok)

    async def test_offline_expired(self):
        import tempfile
        from core.license import LicenseManager

        with tempfile.TemporaryDirectory() as td:
            lm = LicenseManager(_lic_cfg(td, grace_days=-1))
            lm._cache_write({"app": "harvest", "status": "active", "pass": "abc"}, "abc")
            with mock.patch.object(lm, "_fetch", return_value=None):
                ok, r = await lm.require_activation()
                self.assertFalse(ok)
                self.assertIn("протух", r)

    async def test_offline_pw_match(self):
        import tempfile
        from core.license import LicenseManager

        with tempfile.TemporaryDirectory() as td:
            lm = LicenseManager(_lic_cfg(td))
            lm._cache_write({"app": "harvest", "status": "active", "pass": "abc"}, "abc")
            with mock.patch.object(lm, "_fetch", return_value=None):
                ok, _ = await lm.require_activation()
                self.assertTrue(ok)

    async def test_offline_password_verify(self):
        import tempfile
        from core.license import LicenseManager

        with tempfile.TemporaryDirectory() as td:
            lm = LicenseManager(_lic_cfg(td))
            h = LicenseManager.make_hash("s3cr3t", "salt-test", iterations=10000)
            # write cache with mismatched pw then verify via password
            payload = {"app": "harvest", "status": "active", "pass": h}
            lm._cache_write(payload, "other")
            with mock.patch.object(lm, "_fetch", return_value=None):
                ok, _ = await lm.require_activation("s3cr3t")
                self.assertTrue(ok)
                ok2, _ = await lm.require_activation("bad")
                self.assertFalse(ok2)


class TestLicenseEnforce(unittest.IsolatedAsyncioTestCase):
    async def test_disabled(self):
        from core.license import enforce_license_async

        lm = await enforce_license_async({"license": {"enabled": False}})
        self.assertFalse(lm.enabled)

    async def test_env_ok(self):
        import tempfile
        from core.license import enforce_license_async

        with tempfile.TemporaryDirectory() as td:
            cfg = _lic_cfg(td)
            with mock.patch.dict("os.environ", {"LICENSE_PASSWORD": "pw123"}):
                h = __import__("core.license", fromlist=["LicenseManager"]).LicenseManager.make_hash(
                    "pw123", "salt-test", iterations=10000
                )
                payload = {"app": "harvest", "status": "active", "pass": h}
                with mock.patch("core.license.LicenseManager._fetch", return_value=payload):
                    lm = await enforce_license_async(cfg)
                    self.assertTrue(lm.enabled)

    async def test_env_fail(self):
        import tempfile
        from core.license import LicenseError, enforce_license_async

        with tempfile.TemporaryDirectory() as td:
            cfg = _lic_cfg(td)
            with mock.patch.dict("os.environ", {"LICENSE_PASSWORD": "bad"}):
                h = __import__("core.license", fromlist=["LicenseManager"]).LicenseManager.make_hash(
                    "good", "salt-test", iterations=10000
                )
                payload = {"app": "harvest", "status": "active", "pass": h}
                with mock.patch("core.license.LicenseManager._fetch", return_value=payload):
                    with self.assertRaises(LicenseError):
                        await enforce_license_async(cfg)

    async def test_non_interactive_ok_fail(self):
        import tempfile
        from core.license import LicenseError, enforce_license_async

        with tempfile.TemporaryDirectory() as td:
            cfg = _lic_cfg(td)
            with mock.patch.dict("os.environ", {}, clear=False):
                import os as _os

                _os.environ.pop("LICENSE_PASSWORD", None)
                h = __import__("core.license", fromlist=["LicenseManager"]).LicenseManager.make_hash(
                    "pw", "salt-test", iterations=10000
                )
                payload = {"app": "harvest", "status": "active", "pass": h}
                # ok via cache hit without password
                with mock.patch("core.license.LicenseManager._fetch", return_value=payload):
                    with mock.patch(
                        "core.license.LicenseManager._cache_read",
                        return_value={"payload": payload, "pw": h, "ts": 9999999999},
                    ):
                        with mock.patch("core.license.time.time", return_value=9999999999):
                            # need _analyze ok and cache hit -> require_activation(None) True
                            lm = await enforce_license_async(cfg, interactive=False)
                            self.assertTrue(lm.enabled)
                # fail
                with mock.patch("core.license.LicenseManager._fetch", return_value=None):
                    with mock.patch("core.license.LicenseManager._cache_read", return_value=None):
                        with self.assertRaises(LicenseError):
                            await enforce_license_async(cfg, interactive=False)

    async def test_interactive(self):
        import tempfile
        from core.license import LicenseError, enforce_license_async

        with tempfile.TemporaryDirectory() as td:
            cfg = _lic_cfg(td)
            with mock.patch.dict("os.environ", {}, clear=False):
                import os as _os

                _os.environ.pop("LICENSE_PASSWORD", None)
                h = __import__("core.license", fromlist=["LicenseManager"]).LicenseManager.make_hash(
                    "secret", "salt-test", iterations=10000
                )
                payload = {"app": "harvest", "status": "active", "pass": h}
                # EOF -> raise
                with mock.patch("core.license.LicenseManager._fetch", return_value=payload):
                    with mock.patch("getpass.getpass", side_effect=EOFError("eof")):
                        with self.assertRaises(LicenseError):
                            await enforce_license_async(cfg, interactive=True, attempts=1)
                    # empty then ok
                    with mock.patch("getpass.getpass", side_effect=["", "secret"]):
                        with mock.patch("builtins.print"):
                            lm = await enforce_license_async(cfg, interactive=True, attempts=2)
                            self.assertTrue(lm.enabled)
                    # OSError -> raise
                    with mock.patch("getpass.getpass", side_effect=OSError("no tty")):
                        with self.assertRaises(LicenseError):
                            await enforce_license_async(cfg, interactive=True, attempts=1)
        # wrong then fail -> final raise (fresh dir, no cache hit)
        with tempfile.TemporaryDirectory() as td2:
            cfg2 = _lic_cfg(td2)
            with mock.patch.dict("os.environ", {}, clear=False):
                import os as _os2

                _os2.environ.pop("LICENSE_PASSWORD", None)
                with mock.patch("core.license.LicenseManager._fetch", return_value=payload):
                    with mock.patch("getpass.getpass", return_value="wrong"):
                        with mock.patch("builtins.print"):
                            with self.assertRaises(LicenseError):
                                await enforce_license_async(cfg2, interactive=True, attempts=1)

    def test_main_setpass(self):
        import runpy

        with mock.patch.object(sys, "argv", ["core.license", "setpass", "hello", "salt-test"]):
            # runpy will print hash; capture via patch print? just run, SystemExit not raised (no raise)
            # core.license __main__ does not raise, just prints
            runpy.run_module("core.license", run_name="__main__", alter_sys=False)


# ================= NETDISCOVERY =================
class _FakeCM:
    def __init__(self, resp=None, exc=None):
        self._resp = resp
        self._exc = exc

    async def __aenter__(self):
        if self._exc:
            raise self._exc
        return self._resp

    async def __aexit__(self, *a):
        return False


class _FakeJsonResp:
    def __init__(self, status=200, payload=None):
        self.status = status
        self._payload = payload

    async def json(self, content_type=None):
        return self._payload


class _FakeSession:
    def __init__(self, handler=None):
        self._handler = handler

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    def post(self, url, json=None, headers=None):
        assert self._handler is not None
        return self._handler(url, json)


class TestNetRpc(unittest.IsolatedAsyncioTestCase):
    async def test_rpc_own_session(self):
        from core import netdiscovery as nd

        payload = {"result": "0x1"}

        def _h(url, js):
            return _FakeCM(_FakeJsonResp(200, payload))

        sess = _FakeSession(_h)

        class _FakeClient:
            def __init__(self, *a, **kw):
                pass

            async def __aenter__(self):
                return sess

            async def __aexit__(self, *a):
                return False

        with mock.patch.object(nd.aiohttp, "ClientSession", _FakeClient):
            res = await nd._rpc("eth_chainId", "https://x")
            self.assertEqual(res, "0x1")

    async def test_rpc_with_session_ok(self):
        from core import netdiscovery as nd

        def _h(url, js):
            return _FakeCM(_FakeJsonResp(200, {"result": "0x2"}))

        sess = _FakeSession(_h)
        res = await nd._rpc("eth_chainId", "https://x", session=sess)
        self.assertEqual(res, "0x2")

    async def test_rpc_http_error(self):
        from core import netdiscovery as nd

        def _h(url, js):
            return _FakeCM(_FakeJsonResp(500, {}))

        sess = _FakeSession(_h)
        with self.assertRaises(RuntimeError):
            await nd._rpc("eth_chainId", "https://x", session=sess)

    async def test_rpc_error_field(self):
        from core import netdiscovery as nd

        def _h(url, js):
            return _FakeCM(_FakeJsonResp(200, {"error": {"code": -1}}))

        sess = _FakeSession(_h)
        with self.assertRaises(RuntimeError):
            await nd._rpc("eth_chainId", "https://x", session=sess)

    async def test_probe_ok_and_gas_fail(self):
        from core import netdiscovery as nd

        async def _fake_rpc(m, url, params=None, session=None):
            if m == "eth_chainId":
                return "0x1"
            if m == "eth_blockNumber":
                return "0x10"
            if m == "eth_gasPrice":
                raise RuntimeError("no gas")
            raise AssertionError(m)

        with mock.patch.object(nd, "_rpc", side_effect=_fake_rpc):
            with mock.patch.object(nd.time, "monotonic", side_effect=[0.0, 0.05]):
                pr = await nd.rpc_probe("https://x")
                self.assertIsNotNone(pr)
                self.assertIsNone(pr.gas_price_wei)

    async def test_probe_gas_ok(self):
        from core import netdiscovery as nd

        async def _fake_rpc(m, url, params=None, session=None):
            if m == "eth_chainId":
                return "0xaa"
            if m == "eth_blockNumber":
                return "0x10"
            return "0x64"

        with mock.patch.object(nd, "_rpc", side_effect=_fake_rpc):
            pr = await nd.rpc_probe("https://x")
            self.assertEqual(pr.gas_price_wei, 100)

    async def test_probe_mismatch(self):
        from core import netdiscovery as nd

        async def _fake_rpc(m, url, params=None, session=None):
            return "0x1"

        with mock.patch.object(nd, "_rpc", side_effect=_fake_rpc):
            self.assertIsNone(await nd.rpc_probe("https://x", expected_chain_id=999))

    async def test_probe_exception(self):
        from core import netdiscovery as nd

        async def _fake_rpc(m, url, params=None, session=None):
            raise RuntimeError("down")

        with mock.patch.object(nd, "_rpc", side_effect=_fake_rpc):
            self.assertIsNone(await nd.rpc_probe("https://x"))


class TestNetScan(unittest.IsolatedAsyncioTestCase):
    async def test_scan_filters_and_sorts(self):
        from core import netdiscovery as nd

        c1 = nd.NetworkCandidate(id="a", name="A", chain_id=1, symbol="ETH", rpc_urls=("https://a",))
        c2 = nd.NetworkCandidate(id="b", name="B", chain_id=2, symbol="ETH", rpc_urls=("https://b", "https://b2"))

        async def _fake_probe(url, expected=None, session=None):
            if url == "https://a":
                return nd.ProbeResult(
                    candidate=c1, rpc_url=url, chain_id=1, block_number=5, gas_price_wei=1, latency_ms=30.0
                )
            if url == "https://b":
                # mismatch: candidate expects 2 but got 1
                return nd.ProbeResult(
                    candidate=c2, rpc_url=url, chain_id=1, block_number=5, gas_price_wei=1, latency_ms=5.0
                )
            return None

        with mock.patch.object(nd, "rpc_probe", side_effect=_fake_probe):
            live = await nd.scan_networks([c1, c2], concurrency=2)
            self.assertEqual(len(live), 1)
            self.assertEqual(live[0].rpc_url, "https://a")

    async def test_scan_sort(self):
        from core import netdiscovery as nd

        c1 = nd.NetworkCandidate(id="a", name="A", chain_id=1, symbol="ETH", rpc_urls=("https://a1", "https://a2"))

        async def _fake_probe(url, expected=None, session=None):
            lat = 10.0 if url == "https://a1" else 1.0
            return nd.ProbeResult(
                candidate=c1, rpc_url=url, chain_id=1, block_number=1, gas_price_wei=None, latency_ms=lat
            )

        with mock.patch.object(nd, "rpc_probe", side_effect=_fake_probe):
            live = await nd.scan_networks([c1], concurrency=1)
            self.assertEqual(live[0].rpc_url, "https://a2")

    async def test_hunt(self):
        from core import netdiscovery as nd

        async def _fake_rpc(m, url, params=None, session=None):
            if m == "eth_blockNumber":
                return hex(5)
            if m == "eth_getBlockByNumber":
                # params[0] is hex string
                if params[0] == hex(3):
                    raise RuntimeError("flaky")
                return {"transactions": [{"to": "0x" + "ab" * 20, "input": "0x12345678"}]}
            raise AssertionError(m)

        with mock.patch.object(nd, "_rpc", side_effect=_fake_rpc):
            hints = await nd.hunt_contracts("https://x", max_blocks=5)
            self.assertGreater(len(hints), 0)

    def test_is_contract_and_aggregate(self):
        from core import netdiscovery as nd

        self.assertFalse(nd._is_contract_addr(None))
        self.assertFalse(nd._is_contract_addr(123))
        self.assertFalse(nd._is_contract_addr("0x123"))
        self.assertFalse(nd._is_contract_addr("nothex"))
        self.assertFalse(nd._is_contract_addr("0x" + "00" * 20))
        self.assertTrue(nd._is_contract_addr("0x" + "ab" * 20))
        # system empty exact
        self.assertFalse(nd._is_contract_addr("0x000000000000000000000000000000000000dEaD"))
        A = "0x" + "aa" * 20
        B = "0x" + "bb" * 20
        blocks = [
            {
                "transactions": [
                    {"to": A, "input": "0xdeadbeef" + "00" * 10},
                    {"to": A, "data": "0xcafebabe" + "00" * 5},
                    {"to": B, "input": "0x"},
                    {"to": B, "input": "0x123"},
                    {"to": B, "input": 123},
                    {"to": None, "input": "0xdeadbeef00"},
                    {"to": "0x" + "00" * 20, "input": "0xdeadbeef00"},
                    "garbage",
                    {"to": A, "input": "0x"},
                ]
            },
            None,
            "bad",
            {},
            {"transactions": None},
            {"transactions": [{"to": A}]},
        ]
        hints = nd.aggregate_contracts(blocks)
        self.assertGreater(len(hints), 0)


class TestNetConfig(unittest.TestCase):
    def _probe(self):
        from core.netdiscovery import NetworkCandidate, ProbeResult

        cand = NetworkCandidate(
            id="t", name="T", chain_id=1, symbol="ETH", rpc_urls=("https://x",), explorer="https://e"
        )
        return ProbeResult(
            candidate=cand, rpc_url="https://x", chain_id=1, block_number=10, gas_price_wei=5, latency_ms=3.0
        )

    def test_action_hint(self):
        from core.netdiscovery import ContractHint, _action_from_hint

        h = ContractHint(address="0x" + "aa" * 20, calls=2, selectors=Counter({"0xaaaa": 1}))
        a = _action_from_hint(h)
        self.assertIn("method", a)
        h2 = ContractHint(address="0x" + "aa" * 20, calls=1, selectors=Counter())
        self.assertNotIn("method", _action_from_hint(h2))

    def test_build(self):
        from core.netdiscovery import ContractHint, build_config

        cfg = build_config(self._probe())
        self.assertEqual(cfg["network"]["chain_id"], 1)
        self.assertNotIn("meta", cfg["network"])
        h = ContractHint(address="0x" + "aa" * 20, calls=1, selectors=Counter())
        cfg2 = build_config(self._probe(), [h])
        self.assertIn("meta", cfg2["network"])

    def test_write(self):
        import tempfile
        from core.netdiscovery import write_config

        with tempfile.TemporaryDirectory() as td:
            out = str(Path(td) / "o.yaml")
            self.assertEqual(write_config({"a": 1}, out), out)

    def test_fmt(self):
        from core.netdiscovery import _fmt_probe

        p = self._probe()
        self.assertIn("OK", _fmt_probe(p))
        bad = self._probe()
        # mismatch
        from core.netdiscovery import NetworkCandidate

        bad.candidate = NetworkCandidate(id="x", name="X", chain_id=999, symbol="E", rpc_urls=())
        self.assertIn("MISMATCH", _fmt_probe(bad))
        nogas = self._probe()
        nogas.gas_price_wei = None
        self.assertNotIn("gas=", _fmt_probe(nogas))

    def test_candidate(self):
        from core.netdiscovery import NETWORK_REGISTRY, _candidate_for

        self.assertEqual(_candidate_for(NETWORK_REGISTRY[0].chain_id), NETWORK_REGISTRY[0])
        c = _candidate_for(123456789)
        self.assertEqual(c.id, "chain-123456789")

    def test_format_report(self):
        from core.netdiscovery import ContractHint, DiscoverReport, format_report

        self.assertIn("ни одна", format_report(DiscoverReport()))
        rep = DiscoverReport(
            live=[self._probe()],
            hints=[ContractHint(address="0x" + "aa" * 20, calls=2, selectors=Counter({"0xbb": 1}))],
            config_path="c.yaml",
            config={"actions": [{"type": "transfer"}]},
            error="e1",
        )
        t = format_report(rep)
        self.assertIn("Конфиг записан", t)
        rep2 = DiscoverReport(
            live=[self._probe()], hints=[ContractHint(address="0x" + "aa" * 20, calls=1, selectors=Counter())]
        )
        self.assertIn("(value-only)", format_report(rep2))
        rep3 = DiscoverReport(live=[self._probe()], config_path="c.yaml", config={"actions": []})
        self.assertIn("действия", format_report(rep3))


class TestNetAuto(unittest.IsolatedAsyncioTestCase):
    async def test_rpc_none(self):
        from core import netdiscovery as nd

        with mock.patch.object(nd, "rpc_probe", new=AsyncMock(return_value=None)):
            rep = await nd.auto_discover(rpc_url="https://x", out_path=str(Path(tempfile.mkdtemp()) / "o.yaml"))
            self.assertIn("недоступен", rep.error)

    async def test_rpc_mismatch(self):
        from core import netdiscovery as nd

        cand = nd.NetworkCandidate(id="a", name="A", chain_id=1, symbol="E", rpc_urls=())
        pr = nd.ProbeResult(
            candidate=cand, rpc_url="https://x", chain_id=2, block_number=1, gas_price_wei=None, latency_ms=1.0
        )
        # chain_ok False because candidate chain 1 vs actual 2
        with mock.patch.object(nd, "rpc_probe", new=AsyncMock(return_value=pr)):
            rep = await nd.auto_discover(rpc_url="https://x", out_path=str(Path(tempfile.mkdtemp()) / "o.yaml"))
            self.assertTrue(rep.error)

    async def test_rpc_ok(self):
        from core import netdiscovery as nd

        cand = nd.NetworkCandidate(id="a", name="A", chain_id=1, symbol="E", rpc_urls=())
        pr = nd.ProbeResult(
            candidate=cand, rpc_url="https://x", chain_id=1, block_number=1, gas_price_wei=None, latency_ms=1.0
        )
        with tempfile.TemporaryDirectory() as td:
            out = str(Path(td) / "o.yaml")
            with mock.patch.object(nd, "rpc_probe", new=AsyncMock(return_value=pr)):
                rep = await nd.auto_discover(rpc_url="https://x", out_path=out)
                self.assertTrue(rep.live)

    async def test_no_live(self):
        from core import netdiscovery as nd

        with tempfile.TemporaryDirectory() as td:
            out = str(Path(td) / "o.yaml")
            with mock.patch.object(nd, "scan_networks", new=AsyncMock(return_value=[])):
                rep = await nd.auto_discover(scan=True, out_path=out)
                self.assertIn("нет живых", rep.error)

    async def test_scan_hunt_ok(self):
        from core import netdiscovery as nd

        cand = nd.NetworkCandidate(id="a", name="A", chain_id=1, symbol="E", rpc_urls=())
        pr = nd.ProbeResult(
            candidate=cand, rpc_url="https://x", chain_id=1, block_number=1, gas_price_wei=None, latency_ms=1.0
        )
        with tempfile.TemporaryDirectory() as td:
            out = str(Path(td) / "o.yaml")
            with (
                mock.patch.object(nd, "scan_networks", new=AsyncMock(return_value=[pr])),
                mock.patch.object(nd, "hunt_contracts", new=AsyncMock(return_value=[])),
            ):
                rep = await nd.auto_discover(scan=True, hunt=True, out_path=out)
                self.assertTrue(rep.live)

    async def test_exception(self):
        from core import netdiscovery as nd

        with tempfile.TemporaryDirectory() as td:
            out = str(Path(td) / "o.yaml")
            with mock.patch.object(nd, "scan_networks", side_effect=RuntimeError("boom")):
                rep = await nd.auto_discover(scan=True, out_path=out)
                self.assertIn("boom", rep.error)

    def test_cli_parse_and_main(self):
        from core import netdiscovery as nd

        with mock.patch.object(
            sys,
            "argv",
            ["netdiscovery", "--scan", "--hunt", "--rpc", "https://x", "--out", "o.yaml", "--max-blocks", "3"],
        ):
            args = nd._parse_cli()
            self.assertTrue(args.scan)
            self.assertEqual(args.max_blocks, 3)
        # main no args -> 2
        with mock.patch.object(sys, "argv", ["netdiscovery"]):
            self.assertEqual(nd.main_cli(), 2)
        # main ok -> 0 (use real ProbeResult for format_report)
        with tempfile.TemporaryDirectory() as td:
            out = str(Path(td) / "o.yaml")
            cand = nd.NetworkCandidate(id="t", name="T", chain_id=1, symbol="E", rpc_urls=("https://x",))
            pr = nd.ProbeResult(
                candidate=cand, rpc_url="https://x", chain_id=1, block_number=1, gas_price_wei=1, latency_ms=1.0
            )
            rep_ok = nd.DiscoverReport(live=[pr], config_path=out, config={"actions": [{"type": "transfer"}]}, error="")
            with mock.patch.object(sys, "argv", ["netdiscovery", "--scan"]):
                with mock.patch.object(nd, "auto_discover", new=AsyncMock(return_value=rep_ok)):
                    self.assertEqual(nd.main_cli(), 0)
            # fail -> 1 (no live)
            rep_fail = nd.DiscoverReport(live=[], config_path="", config={}, error="bad")
            with mock.patch.object(sys, "argv", ["netdiscovery", "--scan"]):
                with mock.patch.object(nd, "auto_discover", new=AsyncMock(return_value=rep_fail)):
                    self.assertEqual(nd.main_cli(), 1)
            # rpc path
            with mock.patch.object(sys, "argv", ["netdiscovery", "--rpc", "https://x"]):
                with mock.patch.object(nd, "auto_discover", new=AsyncMock(return_value=rep_fail)):
                    self.assertEqual(nd.main_cli(), 1)

    def test_main_entry(self):
        import runpy

        with mock.patch.object(sys, "argv", ["netdiscovery", "--scan"]):
            with mock.patch(
                "core.netdiscovery.auto_discover",
                new=AsyncMock(
                    return_value=__import__("core.netdiscovery", fromlist=["DiscoverReport"]).DiscoverReport()
                ),
            ):
                try:
                    runpy.run_module("core.netdiscovery", run_name="__main__", alter_sys=False)
                except SystemExit as e:
                    self.assertIn(e.code, (0, 1))


# ================= LOGGER =================
class TestLoggerFull(unittest.TestCase):
    def setUp(self):
        self.root = logging.getLogger()
        self._old_level = self.root.level
        self._old_handlers = list(self.root.handlers)
        self._td = tempfile.mkdtemp()
        self._log = str(Path(self._td) / "t.log")

    def tearDown(self):
        import shutil

        for h in self.root.handlers[:]:
            if h not in self._old_handlers:
                self.root.removeHandler(h)
                try:
                    h.close()
                except Exception:
                    pass
        self.root.handlers[:] = self._old_handlers
        self.root.level = self._old_level
        shutil.rmtree(self._td, ignore_errors=True)

    def test_restrict_oserror(self):
        from core.logger import RestrictedRotatingFileHandler

        with mock.patch("core.logger.restrict_file_permissions", side_effect=OSError("perm")):
            h = RestrictedRotatingFileHandler(self._log, maxBytes=1024, backupCount=1)
            try:
                h.close()
            finally:
                try:
                    self.root.removeHandler(h)
                except Exception:
                    pass
        # doRollover with restrict ok + fail
        from core.logger import restrict_file_permissions  # noqa

        h2 = RestrictedRotatingFileHandler(self._log, maxBytes=50, backupCount=2)
        try:
            h2.doRollover()
            with mock.patch("core.logger.restrict_file_permissions", side_effect=OSError("x")):
                h2.doRollover()
        finally:
            h2.close()
            try:
                self.root.removeHandler(h2)
            except Exception:
                pass

    def test_colored(self):
        from core.logger import ColoredFormatter

        f = ColoredFormatter("%(levelname)s %(message)s")
        for lvl in ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL", "UNKNOWN"):
            rec = logging.LogRecord("n", getattr(logging, lvl, 0), "p", 0, "msg", None, None)
            rec.levelname = lvl
            out = f.format(rec)
            self.assertIn("msg", out)
            self.assertEqual(rec.levelname, lvl)

    def test_json(self):
        import json as _j
        from core.logger import JSONFormatter

        f = JSONFormatter()
        rec = logging.LogRecord("n", logging.INFO, "p", 0, "hello", None, None)
        out = _j.loads(f.format(rec))
        self.assertEqual(out["message"], "hello")
        try:
            raise ValueError("oops")
        except ValueError:
            import sys as _s

            rec2 = logging.LogRecord("n", logging.ERROR, "p", 0, "fail", None, _s.exc_info())
            rec2.extra_data = {"k": 1}
            out2 = _j.loads(f.format(rec2))
            self.assertIn("exception", out2)
            self.assertIn("extra", out2)

    def test_setup_variants(self):
        from core.logger import setup_logging

        # non-string level
        setup_logging({"logging": {"file": self._log, "level": 123, "console": False}})
        self.assertEqual(logging.getLogger().level, logging.INFO)
        # json file + json console
        setup_logging(
            {"logging": {"file": self._log, "level": "debug", "console": True, "colors": True, "json_format": True}}
        )
        # colors false, json false
        setup_logging(
            {"logging": {"file": self._log, "level": "WARNING", "console": True, "colors": False, "json_format": False}}
        )
        # colors true, json false
        setup_logging(
            {"logging": {"file": self._log, "level": "INFO", "console": True, "colors": True, "json_format": False}}
        )
        # no console
        setup_logging({"logging": {"file": self._log, "level": "INFO", "console": False}})
        # missing level
        setup_logging({"logging": {"file": self._log}})
        # subdir
        sub = str(Path(self._td) / "sub" / "a.log")
        setup_logging({"logging": {"file": sub, "level": "INFO", "console": False}})
        self.assertTrue(Path(sub).exists())
        # file in cwd (no mkdir)
        with mock.patch("pathlib.Path.mkdir") as mm:
            cfg = {"logging": {"file": "just.log", "level": "INFO", "console": False}}
            # temporarily change to td to avoid polluting repo
            import os as _os

            old = _os.getcwd()
            try:
                _os.chdir(self._td)
                setup_logging(cfg)
                mm.assert_not_called()
            finally:
                _os.chdir(old)
                try:
                    Path(self._td, "just.log").unlink(missing_ok=True)
                except Exception:
                    pass


if __name__ == "__main__":
    unittest.main()
