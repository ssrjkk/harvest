"""Coverage-тесты: doctor.py, faucet.py, vibevibe.py → 100 % stmts."""

import asyncio
import json
import sys
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, mock_open, patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from web3 import Web3


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _config():
    return {
        "database": {"path": "db.sqlite"},
        "network": {"rpc_url": "http://localhost:8545", "chain_id": 1},
        "faucet": {"enabled": True, "strategies": [{"type": "web", "url": "http://faucet.test"}]},
        "threading": {"max_workers": 4},
    }


_ADDR = "0x" + "1" * 40
_ADDR_CS = Web3.to_checksum_address(_ADDR)


def _make_vi():
    from core.vibevibe import VibeVibeInterface
    vi = VibeVibeInterface.__new__(VibeVibeInterface)
    vi.network = MagicMock()
    vi.network.w3 = MagicMock()
    vi.abi = [
        {"name": "swap", "inputs": [{"name": "amount", "type": "uint256"}],
         "outputs": [], "stateMutability": "nonpayable", "type": "function"},
        {"name": "mint", "inputs": [], "outputs": [],
         "stateMutability": "nonpayable", "type": "function"},
    ]
    vi._contracts = {}
    vi._gas_limit = 300000
    return vi


# ─── doctor.py ────────────────────────────────────────────────────────

class TestDoctor(unittest.TestCase):

    def _base_mocks(self, **overrides):
        """Return dict of mock values for a successful doctor run."""
        d = {
            "validate_config": None,
            "resolve_master_key": b"\x00" * 32,
            "summarize": {"cpu_cores": 1, "max_workers": 1,
                          "rpc_threads": 1, "gen_workers": 1, "chunk_size": 1},
            "LicenseManager": False,
            "db_init": None,
            "db_stats": {"total_wallets": 0, "total_actions": 0},
            "db_close": None,
            "latency": 0.1,
            "chain_id": 1,
            "net_close": None,
            "faucet_validate": 1,
            "faucet_close": None,
        }
        d.update(overrides)
        return d

    def _run_doctor(self, ui, cfg, dm, *, db_class_side=None):
        from core.doctor import doctor
        # Patch all local imports inside doctor()
        with patch("core.config_validate.validate_config", side_effect=dm["validate_config"]), \
             patch("core.crypto.resolve_master_key", side_effect=dm["resolve_master_key"]), \
             patch("core.performance.summarize", return_value=dm["summarize"]), \
             patch("core.license.LicenseManager") as lm_cls, \
             patch("core.database.Database", side_effect=db_class_side) as db_cls, \
             patch("core.database._redact_rpc_url", return_value="http://x"), \
             patch("core.faucet.Faucet") as fa_cls, \
             patch("core.network.NetworkManager") as nm_cls, \
             patch("core.ui._safe", side_effect=lambda s: s):

            lm_inst = lm_cls.return_value
            lm_inst.enabled = dm["LicenseManager"]

            if db_class_side is None:
                db_inst = db_cls.return_value
                db_inst.init = AsyncMock(side_effect=dm["db_init"])
                db_inst.get_stats = AsyncMock(return_value=dm["db_stats"])
                db_inst.close = AsyncMock(side_effect=dm["db_close"])
            # else: db_cls side_effect set to raise

            net_inst = nm_cls.return_value
            net_inst.latency_probe = AsyncMock(return_value=dm["latency"])
            if dm["chain_id"] is None:
                net_inst.run_in_executor = AsyncMock(side_effect=RuntimeError("chain_id err"))
            else:
                net_inst.run_in_executor = AsyncMock(return_value=dm["chain_id"])
            net_inst.w3 = MagicMock()
            net_inst.close = AsyncMock(side_effect=dm["net_close"])

            fa_inst = fa_cls.return_value
            fa_inst.validate = AsyncMock(return_value=dm["faucet_validate"])
            fa_inst.close = AsyncMock(side_effect=dm["faucet_close"])

            result = _run(doctor(ui, cfg))
        return result

    def test_config_fail(self):
        self.assertFalse(self._run_doctor(None, _config(),
            self._base_mocks(validate_config=ValueError("bad cfg"))))

    def test_perf_fail(self):
        self.assertFalse(self._run_doctor(None, _config(),
            self._base_mocks(summarize=RuntimeError("perf err"))))

    def test_master_key_error(self):
        from core.crypto import MasterKeyError
        self.assertFalse(self._run_doctor(None, _config(),
            self._base_mocks(resolve_master_key=MasterKeyError("no key"))))

    def test_licence_enabled(self):
        self._run_doctor(None, _config(),
            self._base_mocks(LicenseManager=True))

    def test_licence_disabled(self):
        self._run_doctor(None, _config(),
            self._base_mocks(LicenseManager=False))

    def test_db_fail(self):
        self.assertFalse(self._run_doctor(None, _config(),
            self._base_mocks(db_init=RuntimeError("db err"))))

    def test_latency_none(self):
        self.assertFalse(self._run_doctor(None, _config(),
            self._base_mocks(latency=None)))

    def test_chain_id_raise(self):
        self.assertFalse(self._run_doctor(None, _config(),
            self._base_mocks(chain_id=None)))

    def test_chain_mismatch(self):
        self.assertFalse(self._run_doctor(None, _config(),
            self._base_mocks(chain_id=999)))

    def test_rpc_ok_ui(self):
        cfg = _config()
        cfg["network"]["chain_id"] = None  # no chain_id check → always OK
        ui = MagicMock()
        self.assertTrue(self._run_doctor(ui, cfg,
            self._base_mocks(db_stats={"total_wallets": 1, "total_actions": 2})))

    def test_faucet_validate_zero(self):
        self.assertFalse(self._run_doctor(None, _config(),
            self._base_mocks(faucet_validate=0)))

    def test_faucet_disabled_config(self):
        cfg = _config()
        cfg["faucet"]["enabled"] = False
        self.assertTrue(self._run_doctor(None, cfg, self._base_mocks()))

    def test_faucet_exception(self):
        self.assertFalse(self._run_doctor(None, _config(),
            self._base_mocks(), db_class_side=RuntimeError("fail")))

    def test_closer_none_and_close_raise(self):
        self._run_doctor(None, _config(), self._base_mocks(
            db_init=RuntimeError("init fail"),
            db_close=RuntimeError("close err"),
            net_close=RuntimeError("net close"),
        ), db_class_side=RuntimeError("db fail"))

    def test_rpc_exception(self):
        self.assertFalse(self._run_doctor(None, _config(),
            self._base_mocks(), db_class_side=RuntimeError("fail")))


# ─── faucet.py ────────────────────────────────────────────────────────

class TestFaucetInit(unittest.TestCase):

    def test_invalid_delay_range(self):
        from core.faucet import Faucet
        f = Faucet({"faucet": {"delay_between_requests": "bad"}, "proxy": {}})
        self.assertEqual(f.delay_range, [5, 15])

    def test_delay_range_too_short(self):
        from core.faucet import Faucet
        f = Faucet({"faucet": {"delay_between_requests": [5]}, "proxy": {}})
        self.assertEqual(f.delay_range, [5, 15])

    def test_delay_range_negative(self):
        from core.faucet import Faucet
        f = Faucet({"faucet": {"delay_between_requests": [-1, 5]}, "proxy": {}})
        self.assertEqual(f.delay_range, [5, 15])

    def test_delay_range_start_gt_end(self):
        from core.faucet import Faucet
        f = Faucet({"faucet": {"delay_between_requests": [20, 5]}, "proxy": {}})
        self.assertEqual(f.delay_range, [5, 15])

    def test_retries_type_error(self):
        from core.faucet import Faucet
        f = Faucet({"faucet": {"retries": "abc"}, "proxy": {}})
        self.assertEqual(f.retries, 3)

    def test_retries_value_error(self):
        from core.faucet import Faucet
        f = Faucet({"faucet": {"retries": ""}, "proxy": {}})
        self.assertEqual(f.retries, 3)

    def test_max_concurrent_type_error(self):
        from core.faucet import Faucet
        f = Faucet({"faucet": {"max_concurrent": "abc"}, "proxy": {}})
        self.assertEqual(f.max_concurrent, 8)

    def test_max_concurrent_value_error(self):
        from core.faucet import Faucet
        f = Faucet({"faucet": {"max_concurrent": ""}, "proxy": {}})
        self.assertEqual(f.max_concurrent, 8)

    def test_retries_floor(self):
        from core.faucet import Faucet
        f = Faucet({"faucet": {"retries": 0}, "proxy": {}})
        self.assertEqual(f.retries, 1)

    def test_max_concurrent_floor(self):
        from core.faucet import Faucet
        f = Faucet({"faucet": {"max_concurrent": 0}, "proxy": {}})
        self.assertEqual(f.max_concurrent, 1)

    def test_faucet_only_config(self):
        from core.faucet import Faucet
        f = Faucet({"strategies": [], "proxy": {}})
        self.assertEqual(f.strategies, [])

    def test_non_dict_strategy_filtered(self):
        from core.faucet import Faucet
        f = Faucet({"faucet": {"strategies": [{"type": "a"}, "bad", 123]}, "proxy": {}})
        self.assertEqual(len(f.strategies), 1)


class TestFaucetProxies(unittest.TestCase):

    def test_load_proxies_list(self):
        from core.faucet import Faucet
        result = Faucet._load_proxies({"list": ["http://p1", "http://p2"]})
        self.assertEqual(result, ["http://p1", "http://p2"])

    def test_load_proxies_list_filter_non_str(self):
        from core.faucet import Faucet
        result = Faucet._load_proxies({"list": ["http://p1", 123, None, "http://p2"]})
        self.assertEqual(result, ["http://p1", "http://p2"])

    def test_load_proxies_from_file(self):
        from core.faucet import Faucet
        content = "http://proxy1\n# comment\nhttp://proxy2\n\n"
        m = mock_open(read_data=content)
        with patch("builtins.open", m):
            result = Faucet._load_proxies({"list": "proxies.txt"})
        self.assertEqual(result, ["http://proxy1", "http://proxy2"])

    def test_load_proxies_file_oserror(self):
        from core.faucet import Faucet
        with patch("builtins.open", side_effect=OSError("file not found")):
            result = Faucet._load_proxies({"list": "missing.txt"})
        self.assertEqual(result, [])

    def test_load_proxies_empty(self):
        from core.faucet import Faucet
        result = Faucet._load_proxies({})
        self.assertEqual(result, [])


class TestFaucetPickProxy(unittest.TestCase):

    def test_pick_proxy_disabled(self):
        from core.faucet import Faucet
        f = Faucet({"faucet": {}, "proxy": {"enabled": False}})
        self.assertIsNone(f._pick_proxy())

    def test_pick_proxy_no_list(self):
        from core.faucet import Faucet
        f = Faucet({"faucet": {}, "proxy": {"enabled": True}})
        self.assertIsNone(f._pick_proxy())

    def test_pick_proxy_random(self):
        from core.faucet import Faucet
        f = Faucet({"faucet": {}, "proxy": {"enabled": True, "list": ["p1", "p2"], "rotate": "random"}})
        result = f._pick_proxy()
        self.assertIn(result, ["p1", "p2"])

    def test_pick_proxy_sequential(self):
        from core.faucet import Faucet
        f = Faucet({"faucet": {}, "proxy": {"enabled": True, "list": ["p1", "p2"], "rotate": "sequential"}})
        self.assertEqual(f._pick_proxy(), "p1")
        self.assertEqual(f._pick_proxy(), "p2")
        self.assertEqual(f._pick_proxy(), "p1")


class TestFaucetSession(unittest.TestCase):

    def test_get_session_creates(self):
        from core.faucet import Faucet
        f = Faucet({"faucet": {}, "proxy": {}})
        session = _run(f._get_session())
        self.assertIsNotNone(session)
        _run(f.close())

    def test_get_session_reuse(self):
        from core.faucet import Faucet
        f = Faucet({"faucet": {}, "proxy": {}})
        s1 = _run(f._get_session())
        s2 = _run(f._get_session())
        self.assertIs(s1, s2)
        _run(f.close())

    def test_close_already_closed(self):
        from core.faucet import Faucet
        f = Faucet({"faucet": {}, "proxy": {}})
        _run(f._get_session())
        _run(f.close())
        _run(f.close())

    def test_close_no_session(self):
        from core.faucet import Faucet
        f = Faucet({"faucet": {}, "proxy": {}})
        _run(f.close())


class TestFaucetValidate(unittest.TestCase):

    def test_validate_disabled(self):
        from core.faucet import Faucet
        f = Faucet({"faucet": {"enabled": False}, "proxy": {}})
        self.assertEqual(_run(f.validate()), 0)

    def test_validate_no_strategies(self):
        from core.faucet import Faucet
        f = Faucet({"faucet": {"strategies": []}, "proxy": {}})
        self.assertEqual(_run(f.validate()), 0)

    def test_validate_http_ok(self):
        from core.faucet import Faucet
        f = Faucet({"faucet": {"strategies": [{"type": "web", "url": "http://ok.test"}]}, "proxy": {}})
        mock_resp = AsyncMock()
        mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_resp.__aexit__ = AsyncMock(return_value=False)
        mock_resp.status = 200
        mock_session = AsyncMock()
        mock_session.get = MagicMock(return_value=mock_resp)
        with patch.object(f, "_get_session", new_callable=AsyncMock, return_value=mock_session):
            result = _run(f.validate())
        self.assertEqual(result, 1)
        self.assertEqual(f._reachable, [True])

    def test_validate_url_none(self):
        from core.faucet import Faucet
        f = Faucet({"faucet": {"strategies": [{"type": "web"}]}, "proxy": {}})
        mock_session = AsyncMock()
        with patch.object(f, "_get_session", new_callable=AsyncMock, return_value=mock_session):
            result = _run(f.validate())
        self.assertEqual(result, 0)
        self.assertEqual(f._reachable, [False])

    def test_validate_http_error(self):
        from core.faucet import Faucet
        f = Faucet({"faucet": {"strategies": [{"type": "web", "url": "http://err.test"}]}, "proxy": {}})
        mock_session = AsyncMock()
        mock_session.get = MagicMock(side_effect=RuntimeError("conn err"))
        with patch.object(f, "_get_session", new_callable=AsyncMock, return_value=mock_session):
            result = _run(f.validate())
        self.assertEqual(result, 0)

    def test_validate_multiple_mixed(self):
        from core.faucet import Faucet
        f = Faucet({"faucet": {"strategies": [
            {"type": "web", "url": "http://ok.test"},
            {"type": "web"},  # url None
            {"type": "web", "url": "http://err.test"},
        ]}, "proxy": {}})

        call_log = []

        class _Ctx:
            def __init__(self, url):
                self._url = url

            async def __aenter__(self):
                call_log.append(self._url)
                if "ok" in (self._url or ""):
                    self.status = 200
                    return self
                raise RuntimeError("conn err")

            async def __aexit__(self, *a):
                return False

        class _Sess:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

            def get(self, url, **kw):
                return _Ctx(url)

        with patch.object(f, "_get_session", new_callable=AsyncMock, return_value=_Sess()):
            result = _run(f.validate())
        self.assertEqual(result, 1)
        self.assertEqual(f._reachable, [True, False, False])

    def test_validate_probe_none_url(self):
        from core.faucet import Faucet
        f = Faucet({"faucet": {"strategies": [{"type": "web", "url": None}]}, "proxy": {}})
        mock_session = AsyncMock()
        with patch.object(f, "_get_session", new_callable=AsyncMock, return_value=mock_session):
            result = _run(f.validate())
        self.assertEqual(result, 0)


class TestFaucetLiveStrategies(unittest.TestCase):

    def test_live_strategies_no_validate(self):
        from core.faucet import Faucet
        f = Faucet({"faucet": {"strategies": [{"type": "a"}, {"type": "b"}]}, "proxy": {}})
        self.assertEqual(f._live_strategies(), [{"type": "a"}, {"type": "b"}])

    def test_live_strategies_after_validate(self):
        from core.faucet import Faucet
        f = Faucet({"faucet": {"strategies": [{"type": "a"}, {"type": "b"}, {"type": "c"}]}, "proxy": {}})
        f._reachable = [True, False, True]
        self.assertEqual(f._live_strategies(), [{"type": "a"}, {"type": "c"}])


class TestFaucetRequestStrategy(unittest.TestCase):

    def test_no_url(self):
        from core.faucet import Faucet
        f = Faucet({"faucet": {}, "proxy": {}})
        session = AsyncMock()
        self.assertFalse(_run(f._request_strategy(session, {"type": "web"}, None, _ADDR)))

    def test_chainstack_payload(self):
        from core.faucet import Faucet
        f = Faucet({"faucet": {}, "proxy": {}})
        mock_resp = AsyncMock()
        mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_resp.__aexit__ = AsyncMock(return_value=False)
        mock_resp.status = 200
        session = AsyncMock()
        session.post = MagicMock(return_value=mock_resp)
        strategy = {"type": "chainstack", "url": "http://cs.test", "network_param": "eth"}
        self.assertTrue(_run(f._request_strategy(session, strategy, None, _ADDR)))
        call_kwargs = session.post.call_args
        self.assertEqual(call_kwargs[1]["json"]["network"], "eth")

    def test_regular_payload(self):
        from core.faucet import Faucet
        f = Faucet({"faucet": {}, "proxy": {}})
        mock_resp = AsyncMock()
        mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_resp.__aexit__ = AsyncMock(return_value=False)
        mock_resp.status = 201
        session = AsyncMock()
        session.post = MagicMock(return_value=mock_resp)
        strategy = {"type": "web", "url": "http://ok.test"}
        self.assertTrue(_run(f._request_strategy(session, strategy, None, _ADDR)))

    def test_status_202(self):
        from core.faucet import Faucet
        f = Faucet({"faucet": {}, "proxy": {}})
        mock_resp = AsyncMock()
        mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_resp.__aexit__ = AsyncMock(return_value=False)
        mock_resp.status = 202
        session = AsyncMock()
        session.post = MagicMock(return_value=mock_resp)
        self.assertTrue(_run(f._request_strategy(session, {"type": "w", "url": "http://x"}, None, _ADDR)))

    def test_bad_status(self):
        from core.faucet import Faucet
        f = Faucet({"faucet": {}, "proxy": {}})
        mock_resp = AsyncMock()
        mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_resp.__aexit__ = AsyncMock(return_value=False)
        mock_resp.status = 500
        mock_resp.text = AsyncMock(return_value="err")
        session = AsyncMock()
        session.post = MagicMock(return_value=mock_resp)
        self.assertFalse(_run(f._request_strategy(session, {"type": "w", "url": "http://x"}, None, _ADDR)))

    def test_timeout_error(self):
        from core.faucet import Faucet
        f = Faucet({"faucet": {}, "proxy": {}})
        session = AsyncMock()
        session.post = MagicMock(side_effect=TimeoutError("timeout"))
        self.assertFalse(_run(f._request_strategy(session, {"type": "w", "url": "http://x"}, None, _ADDR)))

    def test_client_error(self):
        import aiohttp

        from core.faucet import Faucet
        f = Faucet({"faucet": {}, "proxy": {}})
        session = AsyncMock()
        session.post = MagicMock(side_effect=aiohttp.ClientError("http err"))
        self.assertFalse(_run(f._request_strategy(session, {"type": "w", "url": "http://x"}, None, _ADDR)))

    def test_general_exception(self):
        from core.faucet import Faucet
        f = Faucet({"faucet": {}, "proxy": {}})
        session = AsyncMock()
        session.post = MagicMock(side_effect=RuntimeError("general"))
        self.assertFalse(_run(f._request_strategy(session, {"type": "w", "url": "http://x"}, None, _ADDR)))


class TestFaucetRequestTokens(unittest.TestCase):

    def test_disabled(self):
        from core.faucet import Faucet
        f = Faucet({"faucet": {"enabled": False}, "proxy": {}})
        self.assertFalse(_run(f.request_tokens(_ADDR)))

    def test_no_live_strategies(self):
        from core.faucet import Faucet
        f = Faucet({"faucet": {"strategies": []}, "proxy": {}})
        self.assertFalse(_run(f.request_tokens(_ADDR)))

    def test_success_first_try(self):
        from core.faucet import Faucet
        f = Faucet({"faucet": {"strategies": [{"type": "web", "url": "http://ok.test"}], "retries": 2}, "proxy": {}})
        f._reachable = [True]
        mock_resp = AsyncMock()
        mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_resp.__aexit__ = AsyncMock(return_value=False)
        mock_resp.status = 200
        session = AsyncMock()
        session.post = MagicMock(return_value=mock_resp)
        with patch.object(f, "_get_session", new_callable=AsyncMock, return_value=session), \
             patch("core.faucet.asyncio.sleep", new_callable=AsyncMock):
            self.assertTrue(_run(f.request_tokens(_ADDR)))

    def test_exhausted(self):
        from core.faucet import Faucet
        f = Faucet({"faucet": {"strategies": [{"type": "web", "url": "http://fail.test"}], "retries": 1}, "proxy": {}})
        f._reachable = [True]
        mock_resp = AsyncMock()
        mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_resp.__aexit__ = AsyncMock(return_value=False)
        mock_resp.status = 500
        mock_resp.text = AsyncMock(return_value="err")
        session = AsyncMock()
        session.post = MagicMock(return_value=mock_resp)
        with patch.object(f, "_get_session", new_callable=AsyncMock, return_value=session), \
             patch("core.faucet.asyncio.sleep", new_callable=AsyncMock):
            self.assertFalse(_run(f.request_tokens(_ADDR, retries=1)))

    def test_with_proxy(self):
        from core.faucet import Faucet
        f = Faucet({"faucet": {"strategies": [{"type": "web", "url": "http://ok.test"}], "retries": 1}, "proxy": {}})
        f._reachable = [True]
        mock_resp = AsyncMock()
        mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_resp.__aexit__ = AsyncMock(return_value=False)
        mock_resp.status = 200
        session = AsyncMock()
        session.post = MagicMock(return_value=mock_resp)
        with patch.object(f, "_get_session", new_callable=AsyncMock, return_value=session), \
             patch("core.faucet.asyncio.sleep", new_callable=AsyncMock):
            self.assertTrue(_run(f.request_tokens(_ADDR, proxy="http://proxy")))


class TestFaucetEnsureBalance(unittest.TestCase):

    def test_balance_geq_goal(self):
        from core.faucet import Faucet
        f = Faucet({"faucet": {}, "proxy": {}})
        net = AsyncMock()
        net.get_balance = AsyncMock(return_value=1.0)
        self.assertTrue(_run(f.ensure_balance(net, _ADDR)))

    def test_target_balance_loop_success(self):
        from core.faucet import Faucet
        f = Faucet({"faucet": {"target_balance": 0.1, "retries": 2}, "proxy": {}})
        net = AsyncMock()
        balances = iter([0.0, 0.0, 0.2])
        net.get_balance = AsyncMock(side_effect=lambda addr, refresh: next(balances))
        with patch.object(f, "request_tokens", new_callable=AsyncMock, return_value=True):
            self.assertTrue(_run(f.ensure_balance(net, _ADDR)))

    def test_target_balance_loop_fail(self):
        from core.faucet import Faucet
        f = Faucet({"faucet": {"target_balance": 0.1, "retries": 1}, "proxy": {}})
        net = AsyncMock()
        net.get_balance = AsyncMock(return_value=0.0)
        with patch.object(f, "request_tokens", new_callable=AsyncMock, return_value=True):
            self.assertFalse(_run(f.ensure_balance(net, _ADDR)))

    def test_no_target_balance(self):
        from core.faucet import Faucet
        f = Faucet({"faucet": {"target_balance": 0, "min_balance": 0.005}, "proxy": {}})
        net = AsyncMock()
        net.get_balance = AsyncMock(return_value=0.0)
        with patch.object(f, "request_tokens", new_callable=AsyncMock, return_value=False):
            self.assertFalse(_run(f.ensure_balance(net, _ADDR)))

    def test_target_balance_geq_min_after_loop(self):
        from core.faucet import Faucet
        f = Faucet({"faucet": {"target_balance": 1.0, "retries": 2, "min_balance": 0.005}, "proxy": {}})
        net = AsyncMock()
        net.get_balance = AsyncMock(return_value=0.01)
        with patch.object(f, "request_tokens", new_callable=AsyncMock, return_value=True):
            self.assertTrue(_run(f.ensure_balance(net, _ADDR)))


class TestFaucetConcurrentBatch(unittest.TestCase):

    def test_concurrent_batch(self):
        from core.faucet import Faucet
        f = Faucet({"faucet": {"max_concurrent": 3}, "proxy": {}})
        self.assertEqual(f.concurrent_batch(10), 3)
        self.assertEqual(f.concurrent_batch(2), 2)
        self.assertEqual(f.concurrent_batch(0), 1)


class TestFaucetRequestBatch(unittest.TestCase):

    def test_batch_with_network(self):
        from core.faucet import Faucet
        f = Faucet({"faucet": {"strategies": []}, "proxy": {}})
        net = AsyncMock()
        with patch.object(f, "ensure_balance", new_callable=AsyncMock, return_value=True):
            ok, fail = _run(f.request_batch(["a", "b"], 2, network=net))
        self.assertEqual(ok, 2)
        self.assertEqual(fail, 0)

    def test_batch_without_network(self):
        from core.faucet import Faucet
        f = Faucet({"faucet": {"strategies": []}, "proxy": {}})
        with patch.object(f, "request_tokens", new_callable=AsyncMock, return_value=True):
            ok, fail = _run(f.request_batch(["a", "b"], 2, network=None))
        self.assertEqual(ok, 2)
        self.assertEqual(fail, 0)

    def test_batch_exception_in_one(self):
        from core.faucet import Faucet
        f = Faucet({"faucet": {"strategies": []}, "proxy": {}})
        with patch.object(f, "request_tokens", new_callable=AsyncMock, side_effect=RuntimeError("boom")):
            ok, fail = _run(f.request_batch(["a"], 2, network=None))
        self.assertEqual(ok, 0)
        self.assertEqual(fail, 1)

    def test_batch_with_progress_and_pause(self):
        from core.faucet import Faucet
        f = Faucet({"faucet": {"strategies": []}, "proxy": {}})
        progress = MagicMock()
        with patch.object(f, "request_tokens", new_callable=AsyncMock, return_value=True), \
             patch("core.faucet.asyncio.sleep", new_callable=AsyncMock):
            ok, fail = _run(f.request_batch(["a", "b"], 1, progress=progress, pause=0.1))
        self.assertEqual(ok, 2)
        progress.assert_called()

    def test_batch_partial_fail(self):
        from core.faucet import Faucet
        f = Faucet({"faucet": {"strategies": []}, "proxy": {}})
        call_count = 0

        async def fake_tokens(addr, **kw):
            nonlocal call_count
            call_count += 1
            return call_count != 2

        with patch.object(f, "request_tokens", side_effect=fake_tokens):
            ok, fail = _run(f.request_batch(["a", "b", "c"], 1, network=None))
        self.assertEqual(ok, 2)
        self.assertEqual(fail, 1)

    def test_batch_network_ensure_balance_fail(self):
        from core.faucet import Faucet
        f = Faucet({"faucet": {"strategies": []}, "proxy": {}})
        net = AsyncMock()
        with patch.object(f, "ensure_balance", new_callable=AsyncMock, side_effect=RuntimeError("err")):
            ok, fail = _run(f.request_batch(["a"], 2, network=net))
        self.assertEqual(ok, 0)
        self.assertEqual(fail, 1)


# ─── vibevibe.py ──────────────────────────────────────────────────────

class TestVibeVibeABI(unittest.TestCase):

    def _make(self, abi_data=None):
        from core.vibevibe import VibeVibeInterface
        net = MagicMock()
        net.w3 = MagicMock()
        with patch("core.vibevibe.resolve_bundled", return_value=Path("abi.json")), \
             patch("builtins.open", mock_open(read_data=json.dumps(abi_data) if abi_data is not None else "")):
            return VibeVibeInterface(net, {"network": {}})

    def test_load_abi_valid(self):
        vi = self._make({"abi": [{"name": "swap"}]})
        self.assertEqual(vi.abi, [{"name": "swap"}])

    def test_load_abi_empty_dict(self):
        vi = self._make({})
        from core.vibevibe import _PLACEHOLDER_ABI
        self.assertEqual(vi.abi, _PLACEHOLDER_ABI)

    def test_load_abi_list(self):
        vi = self._make([{"name": "swap"}])
        self.assertEqual(vi.abi, [{"name": "swap"}])

    def test_load_abi_not_found(self):
        from core.vibevibe import _PLACEHOLDER_ABI, VibeVibeInterface
        net = MagicMock()
        with patch("core.vibevibe.resolve_bundled", return_value=Path("missing.json")), \
             patch("builtins.open", side_effect=FileNotFoundError):
            vi = VibeVibeInterface(net, {"network": {}})
        self.assertEqual(vi.abi, _PLACEHOLDER_ABI)

    def test_custom_abi_path(self):
        from core.vibevibe import VibeVibeInterface
        net = MagicMock()
        net.w3 = MagicMock()
        with patch("core.vibevibe.resolve_bundled", return_value=Path("custom.json")) as rb, \
             patch("builtins.open", mock_open(read_data=json.dumps({"abi": [{"name": "foo"}]}))):
            vi = VibeVibeInterface(net, {"network": {"abi_path": "custom.json"}})
        rb.assert_called_with("custom.json")
        self.assertEqual(vi.abi, [{"name": "foo"}])

    def test_gas_limit_from_config(self):
        from core.vibevibe import VibeVibeInterface
        net = MagicMock()
        net.w3 = MagicMock()
        with patch("core.vibevibe.resolve_bundled", return_value=Path("abi.json")), \
             patch("builtins.open", mock_open(read_data="[]")):
            vi = VibeVibeInterface(net, {"network": {}, "advanced": {"gas_limit": 500000}})
        self.assertEqual(vi._gas_limit, 500000)


class TestVibeVibeContract(unittest.TestCase):

    def _make_vi(self):
        vi = _make_vi()
        vi.network.w3 = MagicMock()
        vi.network._close_w3_provider = AsyncMock()
        return vi

    def test_get_contract_new(self):
        vi = self._make_vi()
        contract = vi._get_contract(_ADDR)
        self.assertIsNotNone(contract)
        self.assertIn(_ADDR_CS, vi._contracts)

    def test_get_contract_reuse_same_w3(self):
        vi = self._make_vi()
        c1 = vi._get_contract(_ADDR)
        c2 = vi._get_contract(_ADDR)
        self.assertIs(c1, c2)

    def test_get_contract_failover(self):
        vi = self._make_vi()
        old_w3 = MagicMock()
        vi._contracts[_ADDR_CS] = (old_w3, MagicMock())
        with patch("asyncio.ensure_future") as mock_ef:
            mock_task = MagicMock()
            mock_task.cancelled = MagicMock(return_value=False)
            mock_task.exception = MagicMock(return_value=None)
            mock_ef.return_value = mock_task
            c = vi._get_contract(_ADDR)
        self.assertIsNotNone(c)
        self.assertIn(_ADDR_CS, vi._contracts)

    def test_get_contract_failover_done_callback(self):
        vi = self._make_vi()
        old_w3 = MagicMock()
        vi._contracts[_ADDR_CS] = (old_w3, MagicMock())
        captured_cb = {}

        def fake_ensure_future(coro):
            task = MagicMock()
            task.add_done_callback = lambda cb: captured_cb.__setitem__("cb", cb)
            task.cancelled = MagicMock(return_value=False)
            task.exception = MagicMock(return_value=None)
            captured_cb["task"] = task
            return task

        with patch("asyncio.ensure_future", side_effect=fake_ensure_future):
            vi._get_contract(_ADDR)
        # simulate task done
        captured_cb["cb"](captured_cb["task"])

    def test_get_contract_failover_task_cancelled(self):
        vi = self._make_vi()
        old_w3 = MagicMock()
        vi._contracts[_ADDR_CS] = (old_w3, MagicMock())
        captured_cb = {}

        def fake_ensure_future(coro):
            task = MagicMock()
            task.add_done_callback = lambda cb: captured_cb.__setitem__("cb", cb)
            task.cancelled = MagicMock(return_value=True)
            task.exception = MagicMock()
            captured_cb["task"] = task
            return task

        with patch("asyncio.ensure_future", side_effect=fake_ensure_future):
            vi._get_contract(_ADDR)
        captured_cb["cb"](captured_cb["task"])


class TestVibeVibeCallMethod(unittest.TestCase):

    def _make_vi(self):
        vi = _make_vi()
        vi.network.w3 = MagicMock()
        vi.network._close_w3_provider = AsyncMock()
        vi.network.invalidate_balance = MagicMock()
        vi.network.release_nonce = AsyncMock()
        vi.network.rollback_nonce_if_free = AsyncMock()
        return vi

    def _setup_vi(self):
        vi = _make_vi()
        vi.network.w3 = MagicMock()
        vi.network.get_account = MagicMock(return_value=MagicMock(address=_ADDR_CS))
        vi.network.claim_nonce = AsyncMock(return_value=0)
        vi.network.get_gas_price = AsyncMock(return_value=1000000000)
        vi.network.get_fee_basis = AsyncMock(return_value=None)
        vi.network.chain_id = 1
        vi.network.run_retry = AsyncMock(side_effect=lambda fn: fn())
        vi.network.send_raw_transaction = AsyncMock(return_value=MagicMock(hex=lambda: "0xhash"))
        vi.network.wait_for_receipt = AsyncMock(return_value={"status": 1})
        vi.network.invalidate_balance = MagicMock()
        vi.network.release_nonce = AsyncMock()
        vi.network.rollback_nonce_if_free = AsyncMock()
        return vi

    def _make_mock_contract(self, vi, method="swap"):
        mock_contract = MagicMock()
        mock_fn = MagicMock()
        mock_fn.__call__ = MagicMock(return_value=mock_fn)
        mock_fn.build_transaction = MagicMock(return_value={"gas": 100000})
        mock_fn.estimate_gas = MagicMock(return_value=50000)
        mock_contract.functions = MagicMock()
        setattr(mock_contract.functions, method, mock_fn)
        vi._contracts[_ADDR_CS] = (vi.network.w3, mock_contract)
        return mock_fn

    def test_method_not_found(self):
        vi = self._make_vi()
        vi.network.get_account = MagicMock(return_value=MagicMock(address=_ADDR_CS))
        mock_contract = MagicMock()
        # getattr(mock_contract.functions, 'nonexistent', None) → None
        mock_contract.functions = MagicMock(spec=[])
        mock_contract.functions.nonexistent = None
        vi._contracts[_ADDR_CS] = (vi.network.w3, mock_contract)
        result = _run(vi.call_method(_ADDR_CS, "nonexistent", "0x" + "ab" * 32))
        self.assertIsNone(result)

    def test_success_swap(self):
        vi = self._setup_vi()
        self._make_mock_contract(vi, "swap")
        vi.network.get_account.return_value.sign_transaction = MagicMock(return_value=MagicMock(raw_transaction=b"\x00"))
        result = _run(vi.call_method(_ADDR_CS, "swap", "0x" + "ab" * 32, amount_wei=1000))
        self.assertIsNotNone(result)

    def test_success_mint(self):
        vi = self._setup_vi()
        self._make_mock_contract(vi, "mint")
        vi.network.get_account.return_value.sign_transaction = MagicMock(return_value=MagicMock(raw_transaction=b"\x00"))
        result = _run(vi.call_method(_ADDR_CS, "mint", "0x" + "ab" * 32))
        self.assertIsNotNone(result)

    def test_receipt_none(self):
        vi = self._setup_vi()
        vi.network.wait_for_receipt = AsyncMock(return_value=None)
        self._make_mock_contract(vi, "swap")
        vi.network.get_account.return_value.sign_transaction = MagicMock(return_value=MagicMock(raw_transaction=b"\x00"))
        result = _run(vi.call_method(_ADDR_CS, "swap", "0x" + "ab" * 32, amount_wei=1000))
        self.assertIsNone(result)

    def test_receipt_reverted(self):
        vi = self._setup_vi()
        vi.network.wait_for_receipt = AsyncMock(return_value={"status": 0})
        self._make_mock_contract(vi, "swap")
        vi.network.get_account.return_value.sign_transaction = MagicMock(return_value=MagicMock(raw_transaction=b"\x00"))
        result = _run(vi.call_method(_ADDR_CS, "swap", "0x" + "ab" * 32, amount_wei=1000))
        self.assertIsNone(result)

    def test_exception_rollback(self):
        vi = self._make_vi()
        vi.network.get_account = MagicMock(side_effect=RuntimeError("account err"))
        vi.network.claim_nonce = AsyncMock(return_value=5)
        result = _run(vi.call_method(_ADDR_CS, "swap", "0x" + "ab" * 32, amount_wei=1000))
        self.assertIsNone(result)

    def test_exception_rollback_fails(self):
        vi = self._make_vi()
        vi.network.get_account = MagicMock(side_effect=RuntimeError("account err"))
        vi.network.claim_nonce = AsyncMock(return_value=5)
        vi.network.rollback_nonce_if_free = AsyncMock(side_effect=RuntimeError("rollback fail"))
        result = _run(vi.call_method(_ADDR_CS, "swap", "0x" + "ab" * 32, amount_wei=1000))
        self.assertIsNone(result)

    def test_eip1559_fee(self):
        vi = self._setup_vi()
        vi.network.get_fee_basis = AsyncMock(return_value={
            "maxFeePerGas": 2000000000, "maxPriorityFeePerGas": 1000000000
        })
        self._make_mock_contract(vi, "swap")
        vi.network.get_account.return_value.sign_transaction = MagicMock(return_value=MagicMock(raw_transaction=b"\x00"))
        result = _run(vi.call_method(_ADDR_CS, "swap", "0x" + "ab" * 32, amount_wei=1000))
        self.assertIsNotNone(result)

    def test_estimate_gas_exception_fallback(self):
        vi = self._setup_vi()
        mock_fn = self._make_mock_contract(vi, "swap")
        mock_fn.estimate_gas = MagicMock(side_effect=RuntimeError("est gas fail"))
        vi.network.get_account.return_value.sign_transaction = MagicMock(return_value=MagicMock(raw_transaction=b"\x00"))
        result = _run(vi.call_method(_ADDR_CS, "swap", "0x" + "ab" * 32, amount_wei=1000, gas_mult=2.0))
        self.assertIsNotNone(result)


class TestVibeVibeW3Property(unittest.TestCase):

    def test_w3_property(self):
        from core.vibevibe import VibeVibeInterface
        net = MagicMock()
        net.w3 = "w3obj"
        vi = VibeVibeInterface.__new__(VibeVibeInterface)
        vi.network = net
        self.assertEqual(vi.w3, "w3obj")


if __name__ == "__main__":
    unittest.main()
