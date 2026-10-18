"""Добивка bot_telegram pure-функций + визарды (вложенные хендлеры — в test_cov_100_bot_disp.py)."""
import asyncio
import sys
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

try:
    from portal import bot_telegram as bt
    from portal.bot_telegram import _fmt_rpc, _fmt_scheduler
    HAS_BOT = True
except Exception:  # noqa: BLE001
    HAS_BOT = False


@unittest.skipUnless(HAS_BOT, "aiogram не установлен")
class TestFmtRpcSched(unittest.TestCase):
    def test_fmt_rpc_empty(self):
        self.assertEqual(_fmt_rpc({}), "RPC: нет данных.")
        self.assertEqual(_fmt_rpc(None), "RPC: нет данных.")
        self.assertEqual(_fmt_rpc("x"), "RPC: нет данных.")
        self.assertIn("нод нет", _fmt_rpc({"nodes": []}))

    def test_fmt_rpc_nodes(self):
        self.assertIn("[?]", _fmt_rpc({"nodes": ["x", {"url": "u"}]}))
        many = [{"url": f"https://r{i}.test", "ok": True, "latency_ms": 5} for i in range(11)]
        s = _fmt_rpc({"nodes": many})
        self.assertIn("+1", s)
        s2 = _fmt_rpc({"url": "https://x"})
        self.assertIn("harvest", s2)

    def test_fmt_sched(self):
        self.assertEqual(_fmt_scheduler(None), "Планировщик: нет данных.")
        self.assertEqual(_fmt_scheduler("x"), "Планировщик: нет данных.")
        s = _fmt_scheduler({"running": True, "tasks": ["x", {"task_id": "t1", "name": "n"}]})
        self.assertIn("t1", s)
        many = [{"task_id": f"t{i}"} for i in range(11)]
        s2 = _fmt_scheduler({"running": False, "tasks": many})
        self.assertIn("+1", s2)
        s3 = _fmt_scheduler({"running": True, "tasks": []})
        self.assertIn("задач нет", s3)

    def test_wiz_cancel_kb(self):
        kb = bt._wiz_cancel_kb()
        self.assertTrue(kb.inline_keyboard)

    def test_redacting_import_fail(self):
        import logging
        rec = logging.LogRecord("x", 20, "p", 1, "msg", (), None)
        rec.exc_info = (ValueError, ValueError("boom"), None)
        orig_import = __import__

        def _fake(name, *a, **k):
            if name == "portal.bot_render":
                raise ImportError("nope")
            return orig_import(name, *a, **k)

        with mock.patch("builtins.__import__", side_effect=_fake):
            filt = getattr(bt, "_RedactingFilter", None)
            if filt is not None:
                self.assertTrue(filt().filter(rec))
        with mock.patch("traceback.format_exception", side_effect=RuntimeError("boom")):
            filt2 = getattr(bt, "_RedactingFilter", None)
            if filt2 is not None:
                rec2 = logging.LogRecord("x", 20, "p", 1, "m", (), None)
                rec2.exc_info = (ValueError, ValueError("x"), None)
                rec2.exc_text = None
                try:
                    filt2().filter(rec2)
                except Exception:
                    pass


@unittest.skipUnless(HAS_BOT, "aiogram не установлен")
class TestWizards(unittest.IsolatedAsyncioTestCase):
    async def _msg(self, text="hi"):
        m = mock.AsyncMock()
        m.text = text
        m.answer = mock.AsyncMock()
        m.delete = mock.AsyncMock()
        m.from_user = mock.MagicMock()
        m.from_user.id = 1
        return m

    async def _state(self, data=None, cur="s"):
        st = mock.AsyncMock()
        st.get_data = mock.AsyncMock(return_value=data or {})
        st.get_state = mock.AsyncMock(return_value=cur)
        st.clear = mock.AsyncMock()
        st.set_state = mock.AsyncMock()
        st.update_data = mock.AsyncMock()
        return st

    async def test_master_finish_bad_yaml(self):
        msg = await self._msg()
        st = await self._state({"m_net": "vibevibe", "m_wallets": 5, "m_cycles": 1})
        with mock.patch("portal.bot_ops.build_config_yaml", side_effect=ValueError("bad")):
            await bt._master_finish(msg, st)
        st2 = await self._state({})
        await bt._master_finish(msg, st2)

    async def test_master_handle_getstate_fail(self):
        msg = await self._msg()
        st = mock.AsyncMock()
        st.get_state = mock.AsyncMock(side_effect=RuntimeError("boom"))
        r = await bt._master_handle_text(msg, st)
        self.assertFalse(r)

    async def test_master_cancel(self):
        msg = await self._msg("/cancel")
        st = await self._state({"m_started": time.monotonic()}, cur="MasterWiz:wallets")
        st.get_state = mock.AsyncMock(return_value="MasterWiz:wallets")
        await bt._master_handle_text(msg, st)

    async def test_enc_handle(self):
        daemon = mock.AsyncMock()
        msg = await self._msg("/cancel")
        st = mock.AsyncMock()
        st.get_state = mock.AsyncMock(side_effect=RuntimeError("boom"))
        r = await bt._enc_handle_text(msg, st, daemon)
        self.assertFalse(r)
        st2 = await self._state({}, cur="EncWiz:password")
        st2.get_state = mock.AsyncMock(return_value="EncWiz:password")
        msg2 = await self._msg("/cancel")
        await bt._enc_handle_text(msg2, st2, daemon)

    async def test_enc_delete_fail(self):
        daemon = mock.AsyncMock()
        msg = await self._msg("longpassword123")
        msg.delete = mock.AsyncMock(side_effect=RuntimeError("del fail"))
        msg.bot = mock.MagicMock()
        st = await self._state({"enc_fmt": "csv"}, cur="EncWiz:password")
        # подменяем get_state чтобы вернуть реальный EncWiz.password.state
        st.get_state = mock.AsyncMock(return_value=bt.EncWiz.password.state)
        with mock.patch.object(bt, "_send_encrypted", new=mock.AsyncMock(return_value=None)):
            r = await bt._enc_handle_text(msg, st, daemon)
            self.assertTrue(r)

    async def test_live_cancel(self):
        msg = mock.MagicMock()
        msg.bot = mock.MagicMock()
        daemon = mock.MagicMock()
        daemon.statistics = mock.AsyncMock(return_value={"running": False, "pool": {}, "db": {}})
        t = asyncio.create_task(bt._live_loop(99999, msg, daemon, every=10, ttl=999))
        await asyncio.sleep(0.01)
        t.cancel()
        try:
            await t
        except asyncio.CancelledError:
            pass
        bt._live[12345] = asyncio.current_task()
        try:
            await bt._live_loop(12345, msg, daemon, every=0, ttl=0.01)
        except Exception:
            pass
        bt._live.pop(12345, None)
        bt._live.pop(99999, None)
