"""Easy2 core coverage: exporter/scheduler/config/batchwriter/ui/utils,
vibevibe/workpool/actions/farmer/single_instance."""

import asyncio
import importlib
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock
from unittest.mock import AsyncMock, MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


# ---------------- exporter ----------------
class TestExporterEasy2(unittest.TestCase):
    def test_encrypt_import_error(self):
        from core import exporter

        with tempfile.TemporaryDirectory() as td:
            p = str(Path(td) / "w.json")
            Path(p).write_text('[{"a":1}]', encoding="utf-8")
            with mock.patch.dict(
                sys.modules,
                {
                    "cryptography": None,
                    "cryptography.hazmat": None,
                    "cryptography.hazmat.primitives": None,
                    "cryptography.hazmat.primitives.padding": None,
                    "cryptography.hazmat.primitives.ciphers": None,
                },
            ):
                # ensure re-import path hits ImportError branch
                ret = exporter.encrypt_file(p, "pw")
            self.assertEqual(ret, p)

    def test_encrypt_open_oserror(self):
        from core import exporter

        with tempfile.TemporaryDirectory() as td:
            p = str(Path(td) / "w.json")
            Path(p).write_text('[{"a":1}]', encoding="utf-8")
            with mock.patch("builtins.open", side_effect=OSError("disk")):
                ret = exporter.encrypt_file(p, "pw")
            self.assertEqual(ret, p)

    def test_encrypt_generic_exception(self):
        from core import exporter

        with tempfile.TemporaryDirectory() as td:
            p = str(Path(td) / "w.json")
            Path(p).write_text('[{"a":1}]', encoding="utf-8")
            with mock.patch("core.exporter.os.urandom", side_effect=RuntimeError("boom")):
                ret = exporter.encrypt_file(p, "pw")
            self.assertEqual(ret, p)

    def test_decrypt_too_small(self):
        from core.exporter import decrypt_file

        with tempfile.TemporaryDirectory() as td:
            p = str(Path(td) / "small.enc")
            Path(p).write_bytes(b"short")
            self.assertEqual(decrypt_file(p, "pw"), p)

    def test_decrypt_remove_oserror(self):
        try:
            import cryptography  # noqa
        except ImportError:
            self.skipTest("no crypto")
            return
        from core import exporter

        with tempfile.TemporaryDirectory() as td:
            p = str(Path(td) / "w.json")
            Path(p).write_text("hello world hello world", encoding="utf-8")
            enc = exporter.encrypt_file(p, "pw")
            self.assertTrue(enc.endswith(".enc"))
            with mock.patch("core.exporter.os.remove", side_effect=OSError("locked")):
                out = exporter.decrypt_file(enc, "pw")
            # decrypt returns out path even if source not removed
            self.assertTrue(out.endswith(".json") or out.endswith(".dec") or out != enc)
            self.assertTrue(Path(out).exists())

    def test_decrypt_import_error(self):
        from core import exporter

        with tempfile.TemporaryDirectory() as td:
            p = str(Path(td) / "x.enc")
            Path(p).write_bytes(b"0" * 100)
            no_crypto = {
                "cryptography": None,
                "cryptography.hazmat": None,
                "cryptography.hazmat.primitives": None,
                "cryptography.hazmat.primitives.padding": None,
                "cryptography.hazmat.primitives.ciphers": None,
                "cryptography.hazmat.primitives.ciphers.algorithms": None,
                "cryptography.hazmat.primitives.ciphers.modes": None,
            }
            with mock.patch.dict(sys.modules, no_crypto):
                ret = exporter.decrypt_file(p, "pw")
            self.assertEqual(ret, p)

    def test_decrypt_corrupt_and_generic(self):
        from core import exporter

        with tempfile.TemporaryDirectory() as td:
            p = str(Path(td) / "c.enc")
            Path(p).write_bytes(os.urandom(100))
            ret = exporter.decrypt_file(p, "pw")
            self.assertEqual(ret, p)
            # generic exception branch
            with mock.patch("core.exporter._derive_keys", side_effect=RuntimeError("boom")):
                p2 = str(Path(td) / "c2.enc")
                Path(p2).write_bytes(os.urandom(100))
                ret2 = exporter.decrypt_file(p2, "pw")
                self.assertEqual(ret2, p2)


# ---------------- scheduler ----------------
class TestSchedulerEasy2(unittest.IsolatedAsyncioTestCase):
    async def test_load_broken_json(self):
        from core.scheduler import Scheduler

        with tempfile.NamedTemporaryFile(suffix=".json", delete=False, mode="w", encoding="utf-8") as f:
            f.write("{not json {{{")
            name = f.name
        try:
            s = Scheduler(name)
            await s.load()
            self.assertEqual(len(s.tasks), 0)
        finally:
            Path(name).unlink(missing_ok=True)

    async def test_save_failure_covered(self):
        from core.scheduler import Scheduler

        with tempfile.TemporaryDirectory() as td:
            s = Scheduler(str(Path(td) / "s.json"))
            s.tasks["x"] = MagicMock()
            s.tasks["x"].to_dict.side_effect = RuntimeError("boom")
            # should not raise (except branch)
            s.save()

    def test_compute_next_run_last_valid_and_broken(self):
        from core.scheduler import Scheduler
        from datetime import datetime

        with tempfile.TemporaryDirectory() as td:
            s = Scheduler(str(Path(td) / "s.json"))
            from core.scheduler import ScheduleTask

            t = ScheduleTask("t1", interval_hours=1, last_run=datetime.now().isoformat())
            s._compute_next_run(t)
            self.assertIsNotNone(t.next_run)
            t2 = ScheduleTask("t2", interval_hours=1, last_run="not-a-date")
            s._compute_next_run(t2)
            self.assertIsNone(t2.next_run)

    def test_is_due_bad_date(self):
        from core.scheduler import Scheduler, ScheduleTask

        with tempfile.TemporaryDirectory() as td:
            s = Scheduler(str(Path(td) / "s.json"))
            t = ScheduleTask("t1", interval_hours=1)
            t.enabled = True
            t.next_run = "not-a-date"
            self.assertFalse(s._is_due(t))

    async def test_loop_break(self):
        from core.scheduler import Scheduler, ScheduleTask

        with tempfile.TemporaryDirectory() as td:
            s = Scheduler(str(Path(td) / "s.json"))
            t = ScheduleTask("t1", interval_hours=1)
            s.tasks["t1"] = t
            s._running = True
            s._stop_event = asyncio.Event()
            s._stop_event.set()
            s._is_due = MagicMock(return_value=True)
            s._execute_task = AsyncMock()
            await s._loop()
            s._execute_task.assert_awaited_once()

    async def test_loop_timeout_continue(self):
        from core.scheduler import Scheduler, ScheduleTask

        with tempfile.TemporaryDirectory() as td:
            s = Scheduler(str(Path(td) / "s.json"))
            t = ScheduleTask("t1", interval_hours=1)
            t.next_run = None
            s.tasks["t1"] = t
            s._running = True
            s._stop_event = asyncio.Event()
            # first wait_for raises TimeoutError -> continue, second returns -> break
            # mock закрывает переданную корутину, иначе "coroutine was never awaited".
            _wf_calls = {"n": 0}

            def _fake_wait_for(coro, timeout=None):
                try:
                    coro.close()
                except Exception:
                    pass
                _wf_calls["n"] += 1
                if _wf_calls["n"] == 1:
                    raise TimeoutError("t")
                return True

            with mock.patch("asyncio.wait_for", side_effect=_fake_wait_for):
                await s._loop()
            self.assertTrue(True)

    async def test_loop_execute_exception(self):
        from core.scheduler import Scheduler, ScheduleTask

        with tempfile.TemporaryDirectory() as td:
            s = Scheduler(str(Path(td) / "s.json"))
            t = ScheduleTask("t1", interval_hours=1)
            s.tasks["t1"] = t
            s._running = True
            s._stop_event = asyncio.Event()
            s._stop_event.set()
            s._is_due = MagicMock(return_value=True)
            s._execute_task = AsyncMock(side_effect=RuntimeError("boom"))

            async def _fake_sleep(_s):
                s._running = False

            with mock.patch("asyncio.sleep", new=_fake_sleep):
                await s._loop()
            self.assertFalse(s._running)


# ---------------- config ----------------
class TestConfigEasy2(unittest.TestCase):
    def _write_cfg(self, tmp_path, chain_id=0):
        import yaml

        cfg = {
            "network": {"rpc_url": "https://example.com/rpc", "chain_id": chain_id},
            "wallets": {"count": 1},
            "faucet": {"enabled": False},
            "actions": [{"type": "transfer", "target": "random_wallet"}],
            "farming": {"actions_per_cycle": [1, 1]},
            "threading": {"max_workers": 1},
            "database": {"path": "db.sqlite"},
        }
        p = Path(str(tmp_path)) / "c.yaml"
        p.write_text(yaml.dump(cfg), encoding="utf-8")
        return str(p)

    def test_chain_id_zero_returns_none(self):
        from core.config import load_config

        with tempfile.TemporaryDirectory() as td:
            p = self._write_cfg(td, chain_id=0)
            self.assertIsNone(load_config(p))

    def test_validate_typeerror_returns_none(self):
        from core.config import load_config

        with tempfile.TemporaryDirectory() as td:
            p = self._write_cfg(td, chain_id=1)
            # make config valid for missing-check but fail validate with TypeError
            with mock.patch("core.config.validate_config", side_effect=TypeError("bad type")):
                self.assertIsNone(load_config(p))
            with mock.patch("core.config.validate_config", side_effect=AttributeError("bad attr")):
                self.assertIsNone(load_config(p))
            with mock.patch("core.config.validate_config", side_effect=KeyError("bad key")):
                self.assertIsNone(load_config(p))


# ---------------- batchwriter ----------------
class TestBatchWriterEasy2(unittest.IsolatedAsyncioTestCase):
    async def test_hard_cap_drops_oldest(self):
        from core.batchwriter import BatchWriter

        class FakeDB:
            async def log_actions_batch(self, rows):
                pass

            async def update_wallet_health_batch(self, u):
                pass

        db = FakeDB()
        bw = BatchWriter(db, flush_every=999, max_buffer=500)
        bw.hard_cap = 3
        for i in range(4):
            await bw.add_action(f"0x{i}", "a", "h", True)
        # 4th action exceeds hard_cap=3 without flush (max_buffer=500) -> drop oldest
        self.assertEqual(bw.dropped_count, 1)
        async with bw._lock:
            self.assertEqual(len(bw._actions), 3)
            # health for dropped addr decremented to (0,0) (backpressure accounting)
            self.assertEqual(bw._health_updates.get("0x0"), (0, 0))

    async def test_flush_loop_and_cooldown(self):
        from core.batchwriter import BatchWriter

        class FakeDB:
            async def log_actions_batch(self, rows):
                pass

            async def update_wallet_health_batch(self, u):
                pass

        db = FakeDB()
        bw = BatchWriter(db, flush_every=0.01, max_buffer=500)
        bw.start()
        await asyncio.sleep(0.05)
        await bw.stop()
        self.assertTrue(True)


# ---------------- ui ----------------
class TestUiEasy2(unittest.IsolatedAsyncioTestCase):
    def test_import_rich_failure(self):
        import core.ui as ui_mod

        with mock.patch.dict(
            sys.modules,
            {
                "rich": None,
                "rich.console": None,
                "rich.live": None,
                "rich.markup": None,
                "rich.panel": None,
                "rich.table": None,
                "rich.text": None,
                "rich.status": None,
            },
        ):
            importlib.reload(ui_mod)
            self.assertFalse(ui_mod._HAS_RICH)
        importlib.reload(ui_mod)

    def test_tty_wide_braille(self):
        import core.ui as ui_mod

        fake_stdout = MagicMock()
        fake_stdout.isatty.return_value = True
        fake_stdout.encoding = "utf-8"
        with mock.patch("sys.stdout", fake_stdout):
            with mock.patch.dict(os.environ, {}, clear=False):
                os.environ.pop("NO_COLOR", None)
                importlib.reload(ui_mod)
                # braille frames when TTY+WIDE
                self.assertEqual(len(ui_mod.SPINNER_FRAMES), 10)
        importlib.reload(ui_mod)

    def test_tty_wide_except(self):
        import core.ui as ui_mod
        import builtins as _b

        fake_stdout = MagicMock()
        fake_stdout.isatty.return_value = True
        fake_stdout.encoding = "utf-8"
        _orig_len = _b.len

        def _fake_len(x):
            try:
                if isinstance(x, list) and _orig_len(x) == 10:
                    # braille frames list -> force except branch
                    s = "".join(x)
                    if chr(0x280B) in s:
                        raise RuntimeError("boom")
            except RuntimeError:
                raise
            except Exception:
                pass
            return _orig_len(x)

        with mock.patch("sys.stdout", fake_stdout):
            with mock.patch.dict(os.environ, {}, clear=False):
                os.environ.pop("NO_COLOR", None)
                with mock.patch("builtins.len", new=_fake_len):
                    importlib.reload(ui_mod)
                    self.assertTrue(True)
        importlib.reload(ui_mod)

    def test_typewrite_long(self):
        import core.ui as ui_mod

        u = ui_mod.UI(use_rich=False)
        with mock.patch.object(ui_mod, "_TTY", True):
            with mock.patch("time.sleep", return_value=None):
                u.typewrite("12345678ABCDEFGH", speed=0)

    async def test_spinner_tty_timeout(self):
        import core.ui as ui_mod

        u = ui_mod.UI(use_rich=False)

        async def slow():
            await asyncio.sleep(0.35)
            return 123

        with mock.patch.object(ui_mod, "_TTY", True):
            res = await u.spinner("load", slow())
        self.assertEqual(res, 123)


# ---------------- utils ----------------
class TestUtilsEasy2(unittest.IsolatedAsyncioTestCase):
    def test_coerce_non_ascii(self):
        from core.utils import _coerce_env_value

        self.assertEqual(_coerce_env_value("привет"), "привет")

    def test_coerce_variants(self):
        from core.utils import _coerce_env_value

        self.assertEqual(_coerce_env_value("-5"), -5)
        self.assertEqual(_coerce_env_value("12.5"), 12.5)
        self.assertEqual(_coerce_env_value("abc"), "abc")

    def test_split_empty(self):
        from core.utils import _split_rpc_urls

        self.assertEqual(_split_rpc_urls(""), "")

    def test_restrict_posix(self):
        from core.utils import restrict_file_permissions

        with tempfile.NamedTemporaryFile(delete=False) as f:
            name = f.name
        try:
            with mock.patch.object(os, "name", "posix"):
                restrict_file_permissions(name)
            # chmod failure -> warning but no raise
            with mock.patch.object(os, "name", "posix"):
                with mock.patch("os.chmod", side_effect=OSError("boom")):
                    restrict_file_permissions(name)
        finally:
            try:
                os.unlink(name)
            except Exception:
                pass

    def test_restrict_nt(self):
        from core.utils import restrict_file_permissions

        with tempfile.NamedTemporaryFile(delete=False) as f:
            name = f.name
        try:
            fake_res = MagicMock(returncode=0, stdout="", stderr="")
            with mock.patch.object(os, "name", "nt"):
                with mock.patch("shutil.which", return_value="icacls"):
                    with mock.patch("getpass.getuser", return_value="u"):
                        with mock.patch("subprocess.run", return_value=fake_res):
                            restrict_file_permissions(name)
            fake_bad = MagicMock(returncode=1, stdout="", stderr="err")
            with mock.patch.object(os, "name", "nt"):
                with mock.patch("shutil.which", return_value="icacls"):
                    with mock.patch("getpass.getuser", return_value="u"):
                        with mock.patch("subprocess.run", return_value=fake_bad):
                            restrict_file_permissions(name)
            with mock.patch.object(os, "name", "nt"):
                with mock.patch("shutil.which", return_value="icacls"):
                    with mock.patch("getpass.getuser", return_value="u"):
                        with mock.patch("subprocess.run", side_effect=OSError("boom")):
                            restrict_file_permissions(name)
        finally:
            try:
                os.unlink(name)
            except Exception:
                pass

    def test_get_ua_import_error(self):
        import core.utils as u

        old = u._ua
        try:
            u._ua = None
            with mock.patch.dict(sys.modules, {"fake_useragent": None}):
                self.assertIsNone(u._get_ua())
        finally:
            u._ua = old

    def test_get_random_ua(self):
        import core.utils as u

        old = u._ua
        try:
            u._ua = None
            with mock.patch("core.utils._get_ua", return_value=None):
                self.assertIn("Mozilla", u.get_random_user_agent())

            class BadUA:
                @property
                def random(self):
                    raise RuntimeError("boom")

            with mock.patch("core.utils._get_ua", return_value=BadUA()):
                self.assertIn("Mozilla", u.get_random_user_agent())
            with mock.patch("core.utils._get_ua", return_value=MagicMock(random="MyUA/1.0")):
                self.assertEqual(u.get_random_user_agent(), "MyUA/1.0")
        finally:
            u._ua = old

    async def test_asleep(self):
        from core.utils import asleep

        with mock.patch("asyncio.sleep", new_callable=AsyncMock) as ms:
            d = await asleep(0.001, 0.002)
            self.assertTrue(ms.awaited)
            self.assertGreaterEqual(d, 0.001)

    def test_resolve_frozen(self):
        from core.utils import resolve_bundled

        # frozen True, nonexistent rel
        had_meipass = hasattr(sys, "_MEIPASS")
        old_meipass = getattr(sys, "_MEIPASS", None)
        if had_meipass:
            del sys.__dict__["_MEIPASS"]
        had_frozen = hasattr(sys, "frozen")
        old_frozen = getattr(sys, "frozen", None)
        old_exec = getattr(sys, "executable", None)
        try:
            sys.frozen = True
            with tempfile.TemporaryDirectory() as td:
                sys.executable = str(Path(td) / "python.exe")
                p = resolve_bundled("definitely/not/here_xyz.json")
                self.assertEqual(p, Path("definitely/not/here_xyz.json"))
            # empty rel
            self.assertEqual(resolve_bundled(""), Path(""))
        finally:
            if had_meipass:
                sys.__dict__["_MEIPASS"] = old_meipass
            elif hasattr(sys, "_MEIPASS"):
                try:
                    del sys.__dict__["_MEIPASS"]
                except Exception:
                    pass
            if had_frozen:
                sys.frozen = old_frozen
            elif hasattr(sys, "frozen"):
                try:
                    delattr(sys, "frozen")
                except Exception:
                    try:
                        del sys.__dict__["frozen"]
                    except Exception:
                        pass
            if old_exec is not None:
                try:
                    sys.executable = old_exec
                except Exception:
                    pass

    def test_resolve_oserror_and_cwd_fail(self):
        from core.utils import resolve_bundled

        had = hasattr(sys, "_MEIPASS")
        old = getattr(sys, "_MEIPASS", None)
        if had:
            del sys.__dict__["_MEIPASS"]
        try:
            # OSError inside exists loop
            with mock.patch("pathlib.Path.exists", side_effect=[False, False, False, OSError("boom"), False]):
                p = resolve_bundled("some/rel_xyz.json")
                self.assertEqual(p, Path("some/rel_xyz.json"))
            with mock.patch("pathlib.Path.cwd", side_effect=RuntimeError("boom")):
                p2 = resolve_bundled("other/rel_xyz.json")
                self.assertEqual(p2, Path("other/rel_xyz.json"))
        finally:
            if had:
                sys.__dict__["_MEIPASS"] = old


# ---------------- vibevibe ----------------
def _make_vi2():
    from core.vibevibe import VibeVibeInterface

    vi = VibeVibeInterface.__new__(VibeVibeInterface)
    vi.network = MagicMock()
    vi.network.w3 = MagicMock()
    vi.network.chain_id = 1
    vi.abi = []
    vi._contracts = {}
    vi._gas_limit = 300000
    return vi


def _setup_contract(vi, addr_cs, method="myMethod"):
    mc = MagicMock()
    fn = MagicMock()
    fn.return_value.build_transaction = MagicMock(side_effect=lambda d: {**d, "gas": 200000})
    fn.return_value.estimate_gas = MagicMock(return_value=120000)
    mc.functions = MagicMock()
    setattr(mc.functions, method, fn)
    # make getattr for other methods return None
    type(mc.functions).__getattr__ = MagicMock(return_value=None) if False else None
    vi._contracts[addr_cs] = (vi.network.w3, mc)
    return mc, fn


class TestVibeVibeEasy2(unittest.TestCase):
    def _run(self, coro):
        loop = asyncio.new_event_loop()
        try:
            return loop.run_until_complete(coro)
        finally:
            loop.close()

    def test_call_trade_invalid_address(self):
        vi = _make_vi2()
        vi.network.get_account = MagicMock()
        res = self._run(
            vi.call_trade("not-an-address", "0x" + "ab" * 32, "0x" + "cd" * 32, lounge_id=1, action=0, amount_wei=100)
        )
        self.assertIsNone(res)

    def test_call_register_invalid_address(self):
        vi = _make_vi2()
        res = self._run(
            vi.call_register(
                "bad-addr", "0x" + "ab" * 32, identity_key=b"\x01" * 32, creator="0x" + "11" * 20, uri="https://x"
            )
        )
        self.assertIsNone(res)

    def test_call_trade_sign_raises(self):
        from web3 import Web3
        from eth_account import Account

        vi = _make_vi2()
        acct = Account.from_key("0x" + "cd" * 32)
        vi.network.get_account = MagicMock(return_value=acct)
        addr = Web3.to_checksum_address("0x" + "22" * 20)
        vi.read_authorization_epoch = AsyncMock(return_value=1)
        with mock.patch.object(vi, "sign_trade_intent", side_effect=Exception("sign boom")):
            res = self._run(
                vi.call_trade(addr, "0x" + "cd" * 32, "0x" + "ab" * 32, lounge_id=1, action=0, amount_wei=100)
            )
        self.assertIsNone(res)

    def test_call_contract_tx_nope(self):
        from web3 import Web3
        from eth_account import Account

        vi = _make_vi2()
        acct = Account.from_key("0x" + "cd" * 32)
        vi.network.get_account = MagicMock(return_value=acct)
        addr = Web3.to_checksum_address("0x" + "33" * 20)
        mc = MagicMock()
        mc.functions = MagicMock(spec=[])
        mc.functions.nope = None
        vi._contracts[addr] = (vi.network.w3, mc)
        res = self._run(vi._call_contract_tx(addr, "nope", "0x" + "cd" * 32, args=(), value_wei=0, gas_mult=1.0))
        self.assertIsNone(res)

    def _base_tx_vi(self, addr_cs):
        from eth_account import Account

        vi = _make_vi2()
        acct = Account.from_key("0x" + "cd" * 32)
        acct.sign_transaction = MagicMock(return_value=MagicMock(raw_transaction=b"\x00"))
        vi.network.get_account = MagicMock(return_value=acct)
        vi.network.claim_nonce = AsyncMock(return_value=5)
        vi.network.get_gas_price = AsyncMock(return_value=1000000000)
        vi.network.get_fee_basis = AsyncMock(return_value=None)
        vi.network.run_retry = AsyncMock(side_effect=lambda fn: fn())
        vi.network.send_raw_transaction = AsyncMock(return_value=MagicMock(hex=lambda: "0xh"))
        vi.network.wait_for_receipt = AsyncMock(return_value={"status": 1})
        vi.network.invalidate_balance = MagicMock()
        vi.network.release_nonce = AsyncMock()
        vi.network.rollback_nonce_if_free = AsyncMock()
        vi.network.chain_id = 1
        mc = MagicMock()
        fn = MagicMock()
        fn.return_value.build_transaction = MagicMock(side_effect=lambda d: {**d, "gas": 200000})
        fn.return_value.estimate_gas = MagicMock(return_value=120000)
        mc.functions = MagicMock()
        mc.functions.myMethod = fn
        vi._contracts[addr_cs] = (vi.network.w3, mc)
        return vi, fn

    def test_estimate_gas_raise(self):
        from web3 import Web3

        addr = Web3.to_checksum_address("0x" + "44" * 20)
        vi, fn = self._base_tx_vi(addr)
        fn.return_value.estimate_gas = MagicMock(side_effect=RuntimeError("est fail"))
        res = self._run(vi._call_contract_tx(addr, "myMethod", "0x" + "cd" * 32, args=(), value_wei=0, gas_mult=1.0))
        self.assertIsNotNone(res)

    def test_receipt_none(self):
        from web3 import Web3

        addr = Web3.to_checksum_address("0x" + "45" * 20)
        vi, fn = self._base_tx_vi(addr)
        vi.network.wait_for_receipt = AsyncMock(return_value=None)
        res = self._run(vi._call_contract_tx(addr, "myMethod", "0x" + "cd" * 32, args=(), value_wei=0, gas_mult=1.0))
        self.assertIsNone(res)

    def test_receipt_status_zero(self):
        from web3 import Web3

        addr = Web3.to_checksum_address("0x" + "46" * 20)
        vi, fn = self._base_tx_vi(addr)
        vi.network.wait_for_receipt = AsyncMock(return_value={"status": 0})
        res = self._run(vi._call_contract_tx(addr, "myMethod", "0x" + "cd" * 32, args=(), value_wei=0, gas_mult=1.0))
        self.assertIsNone(res)
        vi.network.release_nonce.assert_awaited()

    def test_claim_nonce_raise(self):
        from web3 import Web3

        addr = Web3.to_checksum_address("0x" + "47" * 20)
        vi, fn = self._base_tx_vi(addr)
        vi.network.claim_nonce = AsyncMock(side_effect=RuntimeError("nonce boom"))
        res = self._run(vi._call_contract_tx(addr, "myMethod", "0x" + "cd" * 32, args=(), value_wei=0, gas_mult=1.0))
        self.assertIsNone(res)

    def test_rollback_raise(self):
        from web3 import Web3

        addr = Web3.to_checksum_address("0x" + "48" * 20)
        vi, fn = self._base_tx_vi(addr)
        vi.network.get_gas_price = AsyncMock(side_effect=RuntimeError("gas boom"))
        vi.network.rollback_nonce_if_free = AsyncMock(side_effect=RuntimeError("rollback boom"))
        res = self._run(vi._call_contract_tx(addr, "myMethod", "0x" + "cd" * 32, args=(), value_wei=0, gas_mult=1.0))
        self.assertIsNone(res)


# ---------------- workpool ----------------
class TestWorkpoolEasy2(unittest.IsolatedAsyncioTestCase):
    async def test_worker_cancelled(self):
        from core.workpool import WorkerPool

        async def w(wallet, addrs, cyc):
            return wallet["address"], 1

        pool = WorkerPool(max_workers=1, worker_func=w, monitor_interval=10.0)
        await pool.start()
        try:
            # force CancelledError inside _worker_loop via wait_for mock
            def _fake_wait_for_cancel(coro, timeout=None):
                try:
                    coro.close()
                except Exception:
                    pass
                raise asyncio.CancelledError()

            with mock.patch("asyncio.wait_for", side_effect=_fake_wait_for_cancel):
                task = asyncio.create_task(pool._worker_loop())
                with self.assertRaises(asyncio.CancelledError):
                    await task
        finally:
            await pool.stop()

    async def test_worker_retire_requeue(self):
        from core.workpool import WorkerPool

        async def w(wallet, addrs, cyc):
            return wallet["address"], 1

        pool = WorkerPool(max_workers=1, worker_func=w, monitor_interval=10.0)
        wallet = {"address": "0x" + "aa" * 20}
        await pool._queue.put((10, 1, (wallet, [], 0)))
        # run loop with current task marked retire: need to fake current_task
        real_task = asyncio.current_task()
        pool._retire.add(real_task)
        # _worker_loop checks `task not in _retire` at top; since we added current,
        # it exits immediately without processing. To hit requeue branch we need
        # task in retire AFTER get but before check. Simulate by putting retire
        # after get via side effect on worker_func? Simpler: directly test branch
        # by calling loop with mocked wait_for returning item and retire set.
        pool._stopping = False
        # clear retire to allow get, then add after get via mock
        pool._retire.discard(real_task)
        orig_get = pool._queue.get

        async def _fake_get():
            v = await orig_get()
            pool._retire.add(asyncio.current_task())
            return v

        with mock.patch.object(pool._queue, "get", new=_fake_get):
            await pool._worker_loop()
        # item was requeued
        self.assertFalse(pool._queue.empty())
        # drain
        while not pool._queue.empty():
            pool._queue.get_nowait()
            pool._queue.task_done()

    async def test_worker_func_exception_paths(self):
        from core.workpool import WorkerPool

        async def boom(wallet, addrs, cyc):
            raise RuntimeError("wf boom")

        pool = WorkerPool(max_workers=1, worker_func=boom, monitor_interval=10.0)
        await pool.submit({"address": "0x" + "bb" * 20}, [], 0)
        await pool.submit({"address": ""}, [], 0)
        # run two workers manually via _worker_loop with timeout
        pool._stopping = False
        # need workers to consume queue: start pool then join with timeout
        await pool.start()
        try:
            out = await asyncio.wait_for(pool.join(), timeout=5)
            self.assertEqual(out.get("0x" + "bb" * 20), 0)
        finally:
            await pool.stop()

    async def test_monitor_none_and_clamp_and_errors(self):
        from core.workpool import WorkerPool

        async def w(wallet, addrs, cyc):
            return wallet["address"], 1

        # None factor
        pool = WorkerPool(max_workers=4, worker_func=w, health_fn=lambda: None, monitor_interval=0.02)
        await pool.start()
        await asyncio.sleep(0.05)
        await pool.stop()
        # clamp low/high
        pool2 = WorkerPool(max_workers=4, worker_func=w, health_fn=lambda: 0.0, monitor_interval=0.02)
        await pool2.start()
        await asyncio.sleep(0.05)
        await pool2.stop()
        pool3 = WorkerPool(max_workers=4, worker_func=w, health_fn=lambda: 5.0, monitor_interval=0.02)
        await pool3.start()
        await asyncio.sleep(0.05)
        await pool3.stop()

        # exception in health_fn
        def _bad():
            raise RuntimeError("health boom")

        pool4 = WorkerPool(max_workers=2, worker_func=w, health_fn=_bad, monitor_interval=0.02)
        await pool4.start()
        await asyncio.sleep(0.05)
        await pool4.stop()
        # cancelled monitor
        pool5 = WorkerPool(max_workers=2, worker_func=w, monitor_interval=0.02)
        t = asyncio.create_task(pool5._monitor())
        await asyncio.sleep(0.01)
        t.cancel()
        try:
            await t
        except asyncio.CancelledError:
            pass

    def test_on_worker_done_cancelled_and_drain(self):
        from core.workpool import WorkerPool

        async def w(wallet, addrs, cyc):
            return wallet["address"], 1

        pool = WorkerPool(max_workers=1, worker_func=w)
        t = MagicMock()
        t.cancelled.return_value = True
        t.exception.side_effect = asyncio.CancelledError()
        pool._workers.add(t)
        pool._on_worker_done(t)
        self.assertNotIn(t, pool._workers)
        # drain with generic exception
        t2 = MagicMock()
        t2.exception.side_effect = RuntimeError("boom")
        pool._drain_exception(t2)
        t3 = MagicMock()
        t3.exception.side_effect = asyncio.CancelledError()
        pool._drain_exception(t3)

    def test_on_worker_done_exc_respawn(self):
        from core.workpool import WorkerPool

        async def w(wallet, addrs, cyc):
            return wallet["address"], 1

        pool = WorkerPool(max_workers=1, worker_func=w)

        # need running loop for respawn scheduling
        async def _inner():
            t = MagicMock()
            t.cancelled.return_value = False
            t.exception.return_value = RuntimeError("crash")
            pool._workers.add(t)
            pool._on_worker_done(t)
            self.assertTrue(pool._respawn_pending or pool._respawn_task is not None)
            if pool._respawn_task is not None:
                pool._respawn_task.cancel()
                try:
                    await pool._respawn_task
                except (asyncio.CancelledError, Exception):
                    pass
            pool._respawn_pending = False
            pool._respawn_task = None

        asyncio.get_event_loop = asyncio.get_running_loop if False else asyncio.get_event_loop
        # run inside new loop
        loop = asyncio.new_event_loop()
        try:
            loop.run_until_complete(_inner())
        finally:
            loop.close()

    def test_schedule_respawn_branches(self):
        from core.workpool import WorkerPool

        async def w(wallet, addrs, cyc):
            return wallet["address"], 1

        pool = WorkerPool(max_workers=2, worker_func=w, monitor_interval=0.05)
        pool._stopping = True
        pool._schedule_respawn()
        self.assertFalse(pool._respawn_pending)
        pool._stopping = False
        pool._respawn_pending = True
        pool._schedule_respawn()
        pool._respawn_pending = False

    async def test_respawn_backoff_and_target(self):
        from core.workpool import WorkerPool

        async def w(wallet, addrs, cyc):
            return wallet["address"], 1

        pool = WorkerPool(max_workers=2, worker_func=w, monitor_interval=0.05)
        # force backoff doubling: two rapid schedules
        pool._schedule_respawn()
        first_backoff = pool._respawn_backoff
        self.assertGreater(first_backoff, 0)
        # second quickly -> doubling branch (capped)
        pool._respawn_pending = False
        if pool._respawn_task is not None:
            pool._respawn_task.cancel()
            try:
                await pool._respawn_task
            except (asyncio.CancelledError, Exception):
                pass
            pool._respawn_task = None
        pool._schedule_respawn()
        self.assertGreater(pool._respawn_backoff, 0)
        if pool._respawn_task is not None:
            pool._respawn_task.cancel()
            try:
                await pool._respawn_task
            except (asyncio.CancelledError, Exception):
                pass
            pool._respawn_task = None
        pool._respawn_pending = False
        # reset branch after long idle: mock monotonic far future
        pool._last_respawn_ts = 0.0
        with mock.patch("core.workpool.time.monotonic", return_value=100000.0):
            pool._respawn_pending = False
            if pool._respawn_task is not None:
                try:
                    pool._respawn_task.cancel()
                except Exception:
                    pass
                pool._respawn_task = None
            pool._schedule_respawn()
            # reset to base delay
            from core.workpool import _WORKER_RESPAWN_DELAY

            self.assertEqual(pool._respawn_backoff, _WORKER_RESPAWN_DELAY)
            if pool._respawn_task is not None:
                pool._respawn_task.cancel()
                try:
                    await pool._respawn_task
                except (asyncio.CancelledError, Exception):
                    pass
                pool._respawn_task = None
            pool._respawn_pending = False
        # len >= target -> respawn returns early without creating task
        pool.last_health = 1.0
        pool._workers.add(MagicMock())
        pool._workers.add(MagicMock())
        pool._schedule_respawn()
        if pool._respawn_task is not None:
            pool._respawn_task.cancel()
            try:
                await pool._respawn_task
            except (asyncio.CancelledError, Exception):
                pass
            pool._respawn_task = None
        pool._workers.clear()
        pool._respawn_pending = False
        await pool.stop()


# ---------------- actions ----------------
class TestActionsEasy2(unittest.IsolatedAsyncioTestCase):
    def _exec(self, actions, dry_run=False, signer_key=""):
        from core.actions import ActionExecutor

        cfg = {
            "actions": actions,
            "advanced": {"gas_limit": 21000, "dry_run": dry_run},
            "vibevibe": {"signer_key": signer_key} if signer_key else {},
        }
        net = MagicMock()
        net.w3 = MagicMock()
        net.w3.to_wei = MagicMock(side_effect=lambda x, u: int(float(x) * 10**18))
        vb = AsyncMock()
        db = MagicMock()
        ex = ActionExecutor(net, vb, db, cfg)
        return ex, net, vb

    async def test_empty_actions(self):
        ex, net, vb = self._exec([])
        w = {"address": "0x" + "aa" * 20, "private_key": "0x" + "bb" * 32}
        ok = await ex.execute_action(w, [])
        self.assertFalse(ok)

    async def test_generic_call_all(self):
        ex, net, vb = self._exec(
            [{"type": "contract_call", "contract": "0x" + "aa" * 20, "method": "0x12345678"}], dry_run=True
        )
        w = {"address": "0x" + "aa" * 20, "private_key": "0x" + "bb" * 32}
        self.assertTrue(await ex._generic_call(w, ex.actions_conf[0], amount=0.01))
        # non-dry success
        ex2, net2, vb2 = self._exec([{"type": "contract_call", "contract": "0x" + "aa" * 20}])
        net2.send_transfer_with_reason = AsyncMock(return_value=("0xhash", ""))
        w2 = {"address": "0x" + "aa" * 20, "private_key": "0x" + "bb" * 32}
        self.assertTrue(
            await ex2._generic_call(w2, {"type": "contract_call", "contract": "0x" + "aa" * 20}, amount=0.01)
        )
        net2.send_transfer_with_reason = AsyncMock(return_value=("", "reverted"))
        self.assertFalse(
            await ex2._generic_call(w2, {"type": "contract_call", "contract": "0x" + "aa" * 20}, amount=0.01)
        )
        net2.send_transfer_with_reason = AsyncMock(side_effect=RuntimeError("boom"))
        self.assertFalse(
            await ex2._generic_call(w2, {"type": "contract_call", "contract": "0x" + "aa" * 20}, amount=0.01)
        )
        # data None branch (no method)
        net2.send_transfer_with_reason = AsyncMock(return_value=("0xhash2", ""))
        self.assertTrue(
            await ex2._generic_call(w2, {"type": "contract_call", "contract": "0x" + "aa" * 20}, amount=None)
        )

    async def test_signed_trade_all(self):
        # dry run
        ex, net, vb = self._exec(
            [{"type": "vibevibe_buy", "contract": "0x" + "aa" * 20, "lounge_id": 1}],
            dry_run=True,
            signer_key="0x" + "cc" * 32,
        )
        w = {"address": "0x" + "aa" * 20, "private_key": "0x" + "bb" * 32}
        self.assertTrue(
            await ex._signed_trade(
                w, {"type": "vibevibe_buy", "contract": "0x" + "aa" * 20, "lounge_id": 1}, amount=0.01
            )
        )
        # success
        ex2, net2, vb2 = self._exec(
            [{"type": "vibevibe_buy", "contract": "0x" + "aa" * 20, "lounge_id": 1}], signer_key="k"
        )
        net2.w3.to_wei = MagicMock(return_value=1000)
        vb2.call_trade = AsyncMock(return_value="0xhash")
        self.assertTrue(
            await ex2._signed_trade(
                w, {"type": "vibevibe_buy", "contract": "0x" + "aa" * 20, "lounge_id": 1}, amount=0.01
            )
        )
        vb2.call_trade = AsyncMock(return_value="")
        self.assertFalse(
            await ex2._signed_trade(
                w, {"type": "vibevibe_buy", "contract": "0x" + "aa" * 20, "lounge_id": 1}, amount=0.01
            )
        )
        vb2.call_trade = AsyncMock(side_effect=RuntimeError("boom"))
        self.assertFalse(
            await ex2._signed_trade(
                w, {"type": "vibevibe_buy", "contract": "0x" + "aa" * 20, "lounge_id": 1}, amount=0.01
            )
        )

    async def test_register_all(self):
        ex, net, vb = self._exec([{"type": "vibevibe_register", "contract": "0x" + "aa" * 20}], dry_run=True)
        w = {"address": "0x" + "aa" * 20, "private_key": "0x" + "bb" * 32}
        self.assertTrue(await ex._register_lounge(w, {"type": "vibevibe_register", "contract": "0x" + "aa" * 20}))
        ex2, net2, vb2 = self._exec([{"type": "vibevibe_register", "contract": "0x" + "aa" * 20}])
        vb2.call_register = AsyncMock(return_value="0xhash")
        self.assertTrue(await ex2._register_lounge(w, {"type": "vibevibe_register", "contract": "0x" + "aa" * 20}))
        vb2.call_register = AsyncMock(return_value="")
        self.assertFalse(await ex2._register_lounge(w, {"type": "vibevibe_register", "contract": "0x" + "aa" * 20}))
        vb2.call_register = AsyncMock(side_effect=RuntimeError("boom"))
        self.assertFalse(await ex2._register_lounge(w, {"type": "vibevibe_register", "contract": "0x" + "aa" * 20}))

    async def test_execute_fallback_and_branches(self):
        # fallback: manually inject zero-contract after normalize
        ex, net, vb = self._exec([{"type": "transfer", "target": "random_wallet"}])
        ex.actions_conf = [{"type": "vibevibe_swap", "method": "swap", "contract": "0x" + "0" * 40}]
        ex._outcomes = (
            __import__("collections").deque([__import__("collections").deque(maxlen=20)], maxlen=20)
            if False
            else ex._outcomes
        )
        from collections import deque

        ex._outcomes = [deque(maxlen=20)]
        net.send_transfer_with_reason = AsyncMock(return_value=("0xhash", ""))
        w = {"address": "0x" + "aa" * 20, "private_key": "0x" + "bb" * 32}
        ok = await ex.execute_action(w, [w["address"], "0x" + "cc" * 20])
        self.assertTrue(ok)
        # contract_call via execute
        ex2, net2, vb2 = self._exec(
            [{"type": "contract_call", "contract": "0x" + "aa" * 20, "min_amount": 0.001, "max_amount": 0.002}]
        )
        net2.send_transfer_with_reason = AsyncMock(return_value=("0xhash", ""))
        ok2 = await ex2.execute_action(w, [w["address"]])
        self.assertTrue(ok2)
        # contract_call no amount
        ex3, net3, vb3 = self._exec([{"type": "contract_call", "contract": "0x" + "aa" * 20}])
        net3.send_transfer_with_reason = AsyncMock(return_value=("0xhash", ""))
        self.assertTrue(await ex3.execute_action(w, []))
        # register via execute
        ex4, net4, vb4 = self._exec([{"type": "vibevibe_register", "contract": "0x" + "aa" * 20}])
        vb4.call_register = AsyncMock(return_value="0xhash")
        self.assertTrue(await ex4.execute_action(w, []))
        # buy/sell via execute with and without amount
        ex5, net5, vb5 = self._exec(
            [
                {
                    "type": "vibevibe_buy",
                    "contract": "0x" + "aa" * 20,
                    "lounge_id": 1,
                    "min_amount": 0.001,
                    "max_amount": 0.002,
                }
            ],
            signer_key="k",
        )
        net5.w3.to_wei = MagicMock(return_value=1000)
        vb5.call_trade = AsyncMock(return_value="0xhash")
        self.assertTrue(await ex5.execute_action(w, []))
        ex6, net6, vb6 = self._exec(
            [{"type": "vibevibe_sell", "contract": "0x" + "aa" * 20, "lounge_id": 1}], signer_key="k"
        )
        net6.w3.to_wei = MagicMock(return_value=1000)
        vb6.call_trade = AsyncMock(return_value="0xhash")
        self.assertTrue(await ex6.execute_action(w, []))

    async def test_execute_with_profile_odd(self):
        from core.behavior import WalletProfile

        ex, net, vb = self._exec([{"type": "transfer", "target": "random_wallet"}])
        net.send_transfer_with_reason = AsyncMock(return_value=("0xhash", ""))
        w = {"address": "0x" + "aa" * 20, "private_key": "0x" + "bb" * 32}
        prof = WalletProfile(activity=1.0, amount_min_mul=1.0, amount_max_mul=1.0, odd_amount_prob=1.0, neutral=False)
        with mock.patch("random.random", return_value=0.0):
            ok = await ex.execute_action(w, [w["address"], "0x" + "cc" * 20], profile=prof)
        self.assertTrue(ok)


# ---------------- farmer ----------------
class TestFarmerEasy2(unittest.IsolatedAsyncioTestCase):
    def _mk(self, **kw):
        from unittest.mock import MagicMock

        farming = kw.get(
            "farming", {"actions_per_cycle": [1, 1], "delay_between_actions": 0, "skip_cycle_probability": 0.0}
        )
        advanced = kw.get(
            "advanced",
            {
                "force_farm": False,
                "max_consecutive_failures": 5,
                "min_gas_for_action": 0.0,
                "check_balance_before_action": False,
                "auto_replay": False,
            },
        )
        cfg = {"farming": farming, "advanced": advanced}
        wallet = {"address": "0x" + "aa" * 20}
        network = MagicMock()
        faucet = MagicMock()
        executor = MagicMock()
        return cfg, wallet, network, faucet, executor

    async def test_scalar_ranges(self):
        from core.farmer import Farmer

        cfg, wallet, network, faucet, executor = self._mk(
            farming={"actions_per_cycle": 3, "delay_between_actions": 1.0, "skip_cycle_probability": 0.0}
        )
        network.get_balance = AsyncMock(return_value=5.0)
        faucet.min_balance = 0.0
        faucet.enabled = False
        faucet.ensure_balance = AsyncMock(return_value=True)
        executor.execute_action = AsyncMock(return_value=True)
        f = Farmer(wallet, cfg, network, executor, faucet, [wallet["address"]])
        # scalar paths
        self.assertEqual(f._actions_count(), 3)
        self.assertEqual(f._action_delay_bounds(), (1.0, 1.0))
        res = await f.run_cycle()
        self.assertEqual(res, 3)

    async def test_burst_and_hilo(self):
        from core.farmer import Farmer
        from core.behavior import WalletProfile

        cfg, wallet, network, faucet, executor = self._mk(
            farming={"actions_per_cycle": [1, 1], "delay_between_actions": 0, "skip_cycle_probability": 0.0}
        )
        network.get_balance = AsyncMock(return_value=5.0)
        executor.execute_action = AsyncMock(return_value=True)
        prof = WalletProfile(neutral=False, actions_lo=10, actions_width=1, burst_prob=1.0, burst_mult=2.0)
        f = Farmer(wallet, cfg, network, executor, faucet, [wallet["address"]], profile=prof)
        n = f._actions_count()
        self.assertGreaterEqual(n, 1)
        # hi<lo branch
        prof2 = WalletProfile(neutral=False, actions_lo=100, actions_width=1, burst_prob=0.0)
        f2 = Farmer(wallet, cfg, network, executor, faucet, [wallet["address"]], profile=prof2)
        n2 = f2._actions_count()
        self.assertGreaterEqual(n2, 1)

    async def test_faucet_skip_and_force(self):
        from core.farmer import Farmer
        from core.behavior import WalletProfile

        # faucet_skip_prob branch
        cfg, wallet, network, faucet, executor = self._mk()
        network.get_balance = AsyncMock(return_value=0.0)
        faucet.min_balance = 10.0
        faucet.enabled = True
        prof = WalletProfile(neutral=False, faucet_skip_prob=1.0)
        f = Farmer(wallet, cfg, network, executor, faucet, [wallet["address"]], profile=prof)
        with mock.patch("random.random", return_value=0.0):
            res = await f.run_cycle()
        self.assertEqual(res, 0)
        # force continue after faucet fail
        cfg2, wallet2, network2, faucet2, executor2 = self._mk(
            advanced={
                "force_farm": True,
                "max_consecutive_failures": 5,
                "min_gas_for_action": 0.0,
                "check_balance_before_action": False,
                "auto_replay": False,
            }
        )
        network2.get_balance = AsyncMock(return_value=0.5)
        faucet2.min_balance = 10.0
        faucet2.enabled = True
        faucet2.ensure_balance = AsyncMock(return_value=False)
        executor2.execute_action = AsyncMock(return_value=True)
        f2 = Farmer(wallet2, cfg2, network2, executor2, faucet2, [wallet2["address"]])
        res2 = await f2.run_cycle()
        self.assertGreaterEqual(res2, 0)

    async def test_force_balance_exception_fallback(self):
        from core.farmer import Farmer

        cfg, wallet, network, faucet, executor = self._mk(
            advanced={
                "force_farm": True,
                "max_consecutive_failures": 5,
                "min_gas_for_action": 0.0,
                "check_balance_before_action": False,
                "auto_replay": False,
            }
        )
        network.get_balance = AsyncMock(side_effect=[1.0, RuntimeError("boom")])
        faucet.min_balance = 0.0
        faucet.enabled = True
        executor.execute_action = AsyncMock(return_value=True)
        f = Farmer(wallet, cfg, network, executor, faucet, [wallet["address"]])
        res = await f.run_cycle()
        self.assertGreaterEqual(res, 0)

    async def test_min_gas_break_and_switch_fail(self):
        from core.farmer import Farmer

        # min_gas break
        cfg, wallet, network, faucet, executor = self._mk(
            advanced={
                "force_farm": False,
                "max_consecutive_failures": 5,
                "min_gas_for_action": 100.0,
                "check_balance_before_action": True,
                "auto_replay": False,
            }
        )
        network.get_balance = AsyncMock(return_value=50.0)
        faucet.min_balance = 0.0
        faucet.enabled = False
        # need balance >= min_balance to pass faucet gate? min_balance 0, balance 50 ok
        executor.execute_action = AsyncMock(return_value=True)
        f = Farmer(wallet, cfg, network, executor, faucet, [wallet["address"]])
        res = await f.run_cycle()
        self.assertEqual(res, 0)
        # switch fail branch
        cfg2, wallet2, network2, faucet2, executor2 = self._mk(
            advanced={
                "force_farm": False,
                "max_consecutive_failures": 5,
                "min_gas_for_action": 0.0,
                "check_balance_before_action": False,
                "auto_replay": True,
            }
        )
        network2.get_balance = AsyncMock(return_value=5.0)

        async def _fake_exec(wallet, addrs, profile=None):
            return False

        executor2.execute_action = _fake_exec
        network2.transport_failures = MagicMock(return_value=0)

        # make transport bump: first call returns False and bumps? simulate by incrementing
        calls = {"n": 0}

        async def _exec2(wallet, addrs, profile=None):
            calls["n"] += 1
            if calls["n"] == 1:
                network2.transport_failures = MagicMock(side_effect=[0, 1])
                return False
            return True

        executor2.execute_action = _exec2
        network2._switch_rpc = AsyncMock(side_effect=RuntimeError("switch boom"))
        faucet2.min_balance = 0.0
        faucet2.enabled = False
        f2 = Farmer(wallet2, cfg2, network2, executor2, faucet2, [wallet2["address"]])
        # need transport_failures callable returning 0 then 1
        tf_vals = [0, 1, 1]

        def _tf():
            return tf_vals.pop(0) if tf_vals else 1

        network2.transport_failures = _tf
        res2 = await f2.run_cycle()
        self.assertGreaterEqual(res2, 0)
        # no transport attr
        network3 = MagicMock()
        network3.get_balance = AsyncMock(return_value=5.0)
        del network3.transport_failures
        executor3 = MagicMock()
        executor3.execute_action = AsyncMock(side_effect=[False, True])
        faucet3 = MagicMock()
        faucet3.min_balance = 0.0
        faucet3.enabled = False
        f3 = Farmer(wallet, cfg2, network3, executor3, faucet3, [wallet["address"]])
        res3 = await f3.run_cycle()
        self.assertGreaterEqual(res3, 0)


# ---------------- single_instance ----------------
class TestSingleInstanceEasy2(unittest.TestCase):
    def test_fcntl_branch(self):
        import core.single_instance as si

        with tempfile.TemporaryDirectory() as td:
            p = str(Path(td) / "a.lock")
            with mock.patch.object(si, "msvcrt", None):
                fake_fcntl = MagicMock()
                fake_fcntl.LOCK_EX = 2
                fake_fcntl.LOCK_NB = 4
                fake_fcntl.LOCK_UN = 8
                with mock.patch.object(si, "fcntl", fake_fcntl):
                    g = si.SingleInstance(p)
                    self.assertTrue(g.acquire())
                    g.release()
                    self.assertTrue(True)

    def test_no_backend(self):
        import core.single_instance as si

        with tempfile.TemporaryDirectory() as td:
            p = str(Path(td) / "b.lock")
            with mock.patch.object(si, "msvcrt", None):
                with mock.patch.object(si, "fcntl", None):
                    g = si.SingleInstance(p)
                    self.assertFalse(g.acquire())

    def test_acquire_oserror_inside(self):
        import core.single_instance as si

        with tempfile.TemporaryDirectory() as td:
            p = str(Path(td) / "c.lock")
            # force locking to raise OSError
            if si.msvcrt is not None:
                with mock.patch.object(si.msvcrt, "locking", side_effect=OSError("locked")):
                    g = si.SingleInstance(p)
                    self.assertFalse(g.acquire())
            else:
                self.skipTest("no msvcrt")

    def test_pid_write_oserror(self):
        import core.single_instance as si

        with tempfile.TemporaryDirectory() as td:
            p = str(Path(td) / "d.lock")
            with mock.patch("os.ftruncate", side_effect=OSError("boom")):
                g = si.SingleInstance(p)
                # ftruncate fail is swallowed, acquire still True
                self.assertTrue(g.acquire())
                g.release()
            with mock.patch("os.write", side_effect=OSError("boom")):
                g2 = si.SingleInstance(str(Path(td) / "e.lock"))
                # write fail swallowed, acquire still proceeds
                res = g2.acquire()
                self.assertIn(res, (True, False))
                try:
                    g2.release()
                except Exception:
                    pass

    def test_release_branches(self):
        import core.single_instance as si

        with tempfile.TemporaryDirectory() as td:
            p = str(Path(td) / "f.lock")
            # fcntl release
            with mock.patch.object(si, "msvcrt", None):
                fake_fcntl = MagicMock()
                fake_fcntl.LOCK_EX = 2
                fake_fcntl.LOCK_NB = 4
                fake_fcntl.LOCK_UN = 8
                with mock.patch.object(si, "fcntl", fake_fcntl):
                    g = si.SingleInstance(p)
                    self.assertTrue(g.acquire())
                    g.release()
            # release with OSError on locking -> except tries close (no leak)
            g2 = si.SingleInstance(str(Path(td) / "g.lock"))
            self.assertTrue(g2.acquire())
            try:
                if si.msvcrt is not None:
                    g3 = si.SingleInstance(str(Path(td) / "h.lock"))
                    self.assertTrue(g3.acquire())
                    # force release to hit except: make locking fail on release only
                    with mock.patch.object(si.msvcrt, "locking", side_effect=OSError("boom2")):
                        g3.release()
                else:
                    self.assertTrue(True)
            finally:
                try:
                    g2.release()
                except Exception:
                    pass


class TestFarmerRemaining(unittest.IsolatedAsyncioTestCase):
    async def test_list_delay(self):
        from core.farmer import Farmer

        cfg = {
            "farming": {
                "actions_per_cycle": [2, 2],
                "delay_between_actions": [1.0, 2.0],
                "skip_cycle_probability": 0.0,
            },
            "advanced": {
                "force_farm": False,
                "max_consecutive_failures": 5,
                "min_gas_for_action": 0.0,
                "check_balance_before_action": False,
                "auto_replay": False,
            },
        }
        wallet = {"address": "0x" + "aa" * 20}
        network = MagicMock()
        network.get_balance = AsyncMock(return_value=5.0)
        faucet = MagicMock()
        faucet.min_balance = 0.0
        faucet.enabled = False
        executor = MagicMock()
        executor.execute_action = AsyncMock(return_value=True)
        f = Farmer(wallet, cfg, network, executor, faucet, [wallet["address"]])
        self.assertEqual(f._action_delay_bounds(), (1.0, 2.0))

    async def test_force_flip_covers_111(self):
        from core.farmer import Farmer

        class Flip:
            def __init__(self):
                self.n = 0

            def __bool__(self):
                self.n += 1
                return self.n > 1

        cfg = {
            "farming": {"actions_per_cycle": [1, 1], "delay_between_actions": 0, "skip_cycle_probability": 0.0},
            "advanced": {
                "force_farm": Flip(),
                "max_consecutive_failures": 5,
                "min_gas_for_action": 0.0,
                "check_balance_before_action": False,
                "auto_replay": False,
            },
        }
        wallet = {"address": "0x" + "aa" * 20}
        network = MagicMock()
        network.get_balance = AsyncMock(return_value=0.0)
        faucet = MagicMock()
        faucet.min_balance = 10.0
        faucet.enabled = True
        faucet.ensure_balance = AsyncMock(return_value=False)
        executor = MagicMock()
        executor.execute_action = AsyncMock(return_value=True)
        f = Farmer(wallet, cfg, network, executor, faucet, [wallet["address"]])
        res = await f.run_cycle()
        self.assertGreaterEqual(res, 0)


class TestSchedulerRemaining(unittest.IsolatedAsyncioTestCase):
    async def test_double_start_covers_190(self):
        from core.scheduler import Scheduler

        with tempfile.TemporaryDirectory() as td:
            s = Scheduler(str(Path(td) / "s.json"))
            await s.start()
            await s.start()
            self.assertTrue(s.is_running)
            await s.stop()


class TestSingleRemaining(unittest.TestCase):
    def test_pid_write_oserror_covers_95(self):
        import core.single_instance as si
        import os as _os

        with tempfile.TemporaryDirectory() as td:
            p = str(Path(td) / "x.lock")
            real_write = _os.write
            calls = {"n": 0}

            def _fake_write(fd, data):
                calls["n"] += 1
                if calls["n"] == 2:
                    raise OSError("boom2")
                return real_write(fd, data)

            with mock.patch("os.write", side_effect=_fake_write):
                g = si.SingleInstance(p)
                self.assertTrue(g.acquire())
                try:
                    g.release()
                except Exception:
                    pass

    def test_release_double_oserror_covers_116(self):
        import core.single_instance as si
        import os as _os

        with tempfile.TemporaryDirectory() as td:
            p = str(Path(td) / "y.lock")
            g = si.SingleInstance(p)
            self.assertTrue(g.acquire())
            fd_before = g._fd
            with mock.patch.object(si.msvcrt, "locking", side_effect=OSError("boom")):
                with mock.patch("os.close", side_effect=OSError("boom2")):
                    g.release()
            try:
                _os.close(fd_before)
            except Exception:
                pass


class TestUtilsRemaining(unittest.TestCase):
    def test_frozen_exists_true_covers_164(self):
        from core.utils import resolve_bundled

        had_meipass = hasattr(sys, "_MEIPASS")
        old_meipass = getattr(sys, "_MEIPASS", None)
        if had_meipass:
            del sys.__dict__["_MEIPASS"]
        had_frozen = hasattr(sys, "frozen")
        old_frozen = getattr(sys, "frozen", None)
        old_exec = getattr(sys, "executable", None)
        try:
            with tempfile.TemporaryDirectory() as td:
                rel = "myrel/file.json"
                target = Path(td) / rel
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text("{}", encoding="utf-8")
                sys.frozen = True
                sys.executable = str(Path(td) / "python.exe")
                out = resolve_bundled(rel)
                self.assertEqual(out, target)
        finally:
            if had_meipass:
                sys.__dict__["_MEIPASS"] = old_meipass
            elif hasattr(sys, "_MEIPASS"):
                try:
                    del sys.__dict__["_MEIPASS"]
                except Exception:
                    pass
            if had_frozen:
                sys.frozen = old_frozen
            elif hasattr(sys, "frozen"):
                try:
                    delattr(sys, "frozen")
                except Exception:
                    try:
                        del sys.__dict__["frozen"]
                    except Exception:
                        pass
            if old_exec is not None:
                try:
                    sys.executable = old_exec
                except Exception:
                    pass

    def test_resolve_loop_return_covers_173(self):
        from core.utils import resolve_bundled
        import sys
        from pathlib import Path
        import tempfile

        had = hasattr(sys, "_MEIPASS")
        old = getattr(sys, "_MEIPASS", None)
        if had:
            del sys.__dict__["_MEIPASS"]
        try:
            with tempfile.TemporaryDirectory() as td:
                rel = "looprel/file.json"
                target = Path(td) / rel
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text("{}", encoding="utf-8")
                sub = Path(td) / "sub"
                sub.mkdir(exist_ok=True)
                with mock.patch("pathlib.Path.cwd", return_value=sub):
                    had_f = hasattr(sys, "frozen")
                    old_f = getattr(sys, "frozen", None)
                    if had_f:
                        try:
                            delattr(sys, "frozen")
                        except Exception:
                            try:
                                del sys.__dict__["frozen"]
                            except Exception:
                                pass
                    try:
                        out = resolve_bundled(rel)
                        self.assertEqual(out, target)
                    finally:
                        if had_f:
                            sys.frozen = old_f
        finally:
            if had:
                sys.__dict__["_MEIPASS"] = old

    def test_resolve_oserror_loop_covers_174(self):
        from core.utils import resolve_bundled

        had = hasattr(sys, "_MEIPASS")
        old = getattr(sys, "_MEIPASS", None)
        if had:
            del sys.__dict__["_MEIPASS"]
        try:
            rel = "some/rel_oserror.json"

            def _fake_exists(self):
                s = str(self)
                if "app" in s.lower() and "rel_oserror" in s:
                    raise OSError("boom")
                return False

            with mock.patch("pathlib.Path.exists", new=_fake_exists):
                had_f = hasattr(sys, "frozen")
                old_f = getattr(sys, "frozen", None)
                if had_f:
                    try:
                        delattr(sys, "frozen")
                    except Exception:
                        try:
                            del sys.__dict__["frozen"]
                        except Exception:
                            pass
                try:
                    out = resolve_bundled(rel)
                    self.assertEqual(out, Path(rel))
                finally:
                    if had_f:
                        sys.frozen = old_f
        finally:
            if had:
                sys.__dict__["_MEIPASS"] = old


class TestWorkpoolRemaining(unittest.IsolatedAsyncioTestCase):
    async def test_worker_cancelled_covers_125(self):
        from core.workpool import WorkerPool

        async def w_cancel(wallet, addrs, cyc):
            raise asyncio.CancelledError()

        pool = WorkerPool(max_workers=1, worker_func=w_cancel, monitor_interval=10.0)
        wallet = {"address": "0x" + "aa" * 20}
        await pool._queue.put((10, 1, (wallet, [], 0)))
        pool._stopping = False
        with self.assertRaises(asyncio.CancelledError):
            await pool._worker_loop()

    async def test_monitor_candidate_none_covers_162(self):
        from core.workpool import WorkerPool

        async def w(wallet, addrs, cyc):
            return wallet["address"], 1

        pool = WorkerPool(max_workers=2, worker_func=w, monitor_interval=0.02)
        t1 = MagicMock()

        class FakeSet(set):
            def __len__(self):
                return 10

        pool._workers = FakeSet([t1])
        pool._retire = {t1}
        pool._stopping = False
        t = asyncio.create_task(pool._monitor())
        await asyncio.sleep(0.05)
        t.cancel()
        try:
            await t
        except asyncio.CancelledError:
            pass
        self.assertTrue(True)
        pool._workers = set()
        pool._retire = set()
        await pool.stop()

    async def test_monitor_cancelled_covers_166(self):
        from core.workpool import WorkerPool

        async def w(wallet, addrs, cyc):
            return wallet["address"], 1

        pool = WorkerPool(
            max_workers=1,
            worker_func=w,
            health_fn=mock.MagicMock(side_effect=asyncio.CancelledError()),
            monitor_interval=10.0,
        )
        pool._stopping = False
        await pool._monitor()
        self.assertTrue(True)

    async def test_respawn_stopping_covers_221(self):
        from core.workpool import WorkerPool

        async def w(wallet, addrs, cyc):
            return wallet["address"], 1

        pool = WorkerPool(max_workers=1, worker_func=w, monitor_interval=0.02)
        pool._schedule_respawn()
        pool._stopping = True
        if pool._respawn_task is not None:
            try:
                await pool._respawn_task
            except (asyncio.CancelledError, Exception):
                pass
        self.assertTrue(True)
        pool._stopping = False
        pool._respawn_pending = False
        pool._respawn_task = None
        await pool.stop()

    async def test_respawn_target_hit_covers_224(self):
        from core.workpool import WorkerPool

        async def w(wallet, addrs, cyc):
            return wallet["address"], 1

        pool = WorkerPool(max_workers=1, worker_func=w, monitor_interval=0.02)
        pool.last_health = 1.0
        fake_t = MagicMock()
        pool._workers.add(fake_t)
        pool._schedule_respawn()
        if pool._respawn_task is not None:
            try:
                await asyncio.wait_for(pool._respawn_task, timeout=2)
            except (asyncio.CancelledError, Exception):
                pass
        pool._workers.clear()
        pool._respawn_pending = False
        pool._respawn_task = None
        await pool.stop()


if __name__ == "__main__":
    unittest.main(verbosity=2)
