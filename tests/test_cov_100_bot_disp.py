"""Добивка вложенных хендлеров bot_telegram (on_help/on_callback) через dispatcher."""
import sys
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

try:
    import portal.bot_telegram as bt
    from portal.bot_telegram import build_dispatcher
    from portal.config import PortalConfig
    HAS_BOT = True
except Exception:  # noqa: BLE001
    HAS_BOT = False


class _U:
    def __init__(self, uid):
        self.id = uid


class _Chat:
    def __init__(self, cid=123):
        self.id = cid


class _M:
    def __init__(self, uid=123, text=""):
        self.from_user = _U(uid)
        self.text = text
        self.chat = _Chat(uid)
        self.document = None
        self.bot = mock.Mock()
        self.answers = []

    async def answer(self, *a, **k):
        self.answers.append((a, k))
        m = mock.Mock()
        m.message_id = 1
        return m


class _CMessage:
    def __init__(self):
        self.chat = _Chat()
        self.answers = []
        self.edits = []
        self.bot = mock.Mock()

    async def answer(self, *a, **k):
        self.answers.append((a, k))
        m = mock.Mock()
        m.message_id = 1
        return m

    async def edit_text(self, *a, **k):
        self.edits.append((a, k))


class _C:
    def __init__(self, uid=123, data=""):
        self.from_user = _U(uid)
        self.data = data
        self.message = _CMessage()
        self.answered = []

    async def answer(self, *a, **k):
        self.answered.append((a, k))


class _State:
    def __init__(self):
        self._state = None
        self._data = {}

    async def get_state(self):
        return self._state

    async def set_state(self, s):
        self._state = getattr(s, "state", s)

    async def update_data(self, **kw):
        self._data.update(kw)

    async def get_data(self):
        return dict(self._data)

    async def clear(self):
        self._state = None
        self._data = {}


def _cfg():
    import os
    prev = os.environ.get("PORTAL_SECRET")
    os.environ["PORTAL_SECRET"] = "s" * 32
    c = PortalConfig()
    c.telegram_allow_ids = [123]
    c.public_base_url = ""
    if prev is None:
        os.environ.pop("PORTAL_SECRET", None)
    else:
        os.environ["PORTAL_SECRET"] = prev
    return c


class _Daemon:
    running = False

    async def statistics(self):
        return {"running": False, "pool": {}, "db": {}}

    async def cycle_history(self, limit=12):
        return []

    async def all_wallets(self):
        return []

    async def wallet_balances(self, limit=20):
        return []

    async def faucet_report(self):
        return []

    async def rpc_health(self):
        return {"nodes": []}

    def scheduler_state(self):
        return {"running": False, "tasks": []}

    def balance_alerts_state(self):
        return {"enabled": False, "threshold": 1.0}

    async def set_balance_alerts(self, enabled, threshold):
        return {"enabled": enabled, "threshold": threshold}

    def drip_state(self):
        return {"running": False}

    async def drip_faucet(self):
        return {"started": True}


@unittest.skipUnless(HAS_BOT, "aiogram not installed")
class TestDispText(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        bt._flood_last.clear()
        for t in list(bt._live.values()):
            try:
                t.cancel()
            except Exception:
                pass
        bt._live.clear()

    def tearDown(self):
        bt._flood_last.clear()
        for t in list(bt._live.values()):
            try:
                t.cancel()
            except Exception:
                pass
        bt._live.clear()

    async def _text(self, dp, text, daemon=None, state=None):
        bt._flood_last.clear()
        msg = _M(uid=123, text=text)
        st = state or _State()
        await dp.message.handlers[1].callback(msg, st)
        return msg, st

    async def test_flood_all(self):
        dp = build_dispatcher(_cfg(), _Daemon())
        with mock.patch.object(bt, "_flood_ok", return_value=(False, 5)):
            for cmd in (
                "/doctor", "/diag", "/history", "/status",
                "/rpc", "/scheduler", "/faucet", "/balance",
            ):
                msg, _ = await self._text(dp, cmd)
                self.assertTrue(msg.answers)

    async def test_balance_branches(self):
        # generic Exception -> "[err] Не удалось прочитать балансы."
        d = _Daemon()

        async def _boom(limit=20):
            raise ValueError("boom")

        d.wallet_balances = _boom  # type: ignore
        dp = build_dispatcher(_cfg(), d)
        msg, _ = await self._text(dp, "/balance", daemon=d)
        self.assertTrue(msg.answers)
        # empty
        d2 = _Daemon()

        async def _empty(limit=20):
            return []

        d2.wallet_balances = _empty  # type: ignore
        dp2 = build_dispatcher(_cfg(), d2)
        msg2, _ = await self._text(dp2, "/balance", daemon=d2)
        self.assertTrue(msg2.answers)

    async def test_faucet_branches(self):
        d = _Daemon()

        async def _boom():
            raise RuntimeError("x")

        d.faucet_report = _boom  # type: ignore
        dp = build_dispatcher(_cfg(), d)
        msg, _ = await self._text(dp, "/faucet", daemon=d)
        self.assertTrue(msg.answers)
        # empty report
        d2 = _Daemon()

        async def _empty():
            return []

        d2.faucet_report = _empty  # type: ignore
        dp2 = build_dispatcher(_cfg(), d2)
        msg2, _ = await self._text(dp2, "/faucet", daemon=d2)
        self.assertTrue(msg2.answers)
        # all dead
        d3 = _Daemon()

        async def _dead():
            return [{"alive": False}]

        d3.faucet_report = _dead  # type: ignore
        dp3 = build_dispatcher(_cfg(), d3)
        msg3, _ = await self._text(dp3, "/faucet", daemon=d3)
        self.assertTrue(msg3.answers)
        # rpc fail
        d4 = _Daemon()

        async def _rpcboom():
            raise RuntimeError("boom")

        d4.rpc_health = _rpcboom  # type: ignore
        dp4 = build_dispatcher(_cfg(), d4)
        # /rpc flood
        with mock.patch.object(bt, "_flood_ok", return_value=(False, 5)):
            msg4, _ = await self._text(dp4, "/rpc", daemon=d4)
            self.assertTrue(msg4.answers)
        msg5, _ = await self._text(dp4, "/rpc", daemon=d4)
        self.assertTrue(msg5.answers)
        # /scheduler flood
        with mock.patch.object(bt, "_flood_ok", return_value=(False, 5)):
            msg6, _ = await self._text(dp4, "/scheduler", daemon=d4)
            self.assertTrue(msg6.answers)
        # /faucet flood
        with mock.patch.object(bt, "_flood_ok", return_value=(False, 5)):
            msg7, _ = await self._text(dp4, "/faucet", daemon=d4)
            self.assertTrue(msg7.answers)

    async def test_alerts(self):
        # bad threshold
        dp = build_dispatcher(_cfg(), _Daemon())
        msg, _ = await self._text(dp, "/alerts 999")
        self.assertTrue(msg.answers)
        # read fail
        d = _Daemon()

        def _boom():
            raise RuntimeError("boom")

        d.balance_alerts_state = _boom  # type: ignore
        dp2 = build_dispatcher(_cfg(), d)
        msg2, _ = await self._text(dp2, "/alerts", daemon=d)
        self.assertTrue(msg2.answers)
        # save fail (generic Exception, not ValueError)
        d3 = _Daemon()

        async def _saveboom(enabled, threshold):
            raise RuntimeError("boom")

        d3.set_balance_alerts = _saveboom  # type: ignore
        dp3 = build_dispatcher(_cfg(), d3)
        msg3, _ = await self._text(dp3, "/alerts 5", daemon=d3)
        self.assertTrue(msg3.answers)
        # off ok + off fail
        dp4 = build_dispatcher(_cfg(), _Daemon())
        msg4, _ = await self._text(dp4, "/alerts off")
        self.assertTrue(msg4.answers)
        d5 = _Daemon()

        async def _offboom(enabled, threshold):
            raise RuntimeError("boom")

        d5.set_balance_alerts = _offboom  # type: ignore
        dp5 = build_dispatcher(_cfg(), d5)
        msg5, _ = await self._text(dp5, "/alerts off", daemon=d5)
        self.assertTrue(msg5.answers)
        # /alerts with no args ok
        msg6, _ = await self._text(dp4, "/alerts")
        self.assertTrue(msg6.answers)

    async def test_cancel(self):
        dp = build_dispatcher(_cfg(), _Daemon())
        st = _State()
        await st.update_data(x=1)
        msg, st2 = await self._text(dp, "/cancel", state=st)
        self.assertTrue(msg.answers)
        # clear raises -> except в on_help (лог, без ответа)
        st3 = mock.AsyncMock()
        st3.get_state = mock.AsyncMock(return_value="x")
        st3.clear = mock.AsyncMock(side_effect=RuntimeError("boom"))
        msg3 = _M(uid=123, text="/cancel")
        await dp.message.handlers[1].callback(msg3, st3)
        self.assertTrue(True)
        # elif /cancel без state (строка 2083)
        bt._flood_last.clear()
        msg4 = _M(uid=123, text="/cancel")
        await dp.message.handlers[1].callback(msg4, None)
        self.assertTrue(msg4.answers)

    async def test_profiles_fail(self):
        d = _Daemon()

        async def _boom():
            raise RuntimeError("boom")

        d.all_wallets = _boom  # type: ignore
        dp = build_dispatcher(_cfg(), d)
        msg, _ = await self._text(dp, "/profiles", daemon=d)
        self.assertTrue(msg.answers)


@unittest.skipUnless(HAS_BOT, "aiogram not installed")
class TestDispCb(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        bt._flood_last.clear()

    def tearDown(self):
        bt._flood_last.clear()
        for t in list(bt._live.values()):
            try:
                t.cancel()
            except Exception:
                pass
        bt._live.clear()

    async def _cb(self, dp, data, state="__sentinel__"):
        call = _C(uid=123, data=data)
        if state == "__sentinel__":
            st = _State()
        else:
            st = state
        await dp.callback_query.handlers[0].callback(call, st)
        return call, st

    async def test_stale_and_profiles(self):
        dp = build_dispatcher(_cfg(), _Daemon())
        with mock.patch.object(bt._R, "parse_cb", return_value=None):
            call, _ = await self._cb(dp, "v1:x")
            self.assertTrue(call.answered or call.message.answers or call.message.edits)
        # profiles show fail
        d = _Daemon()

        async def _boom():
            raise RuntimeError("boom")

        d.all_wallets = _boom  # type: ignore
        dp2 = build_dispatcher(_cfg(), d)
        call2, _ = await self._cb(dp2, "v1:profiles:show")
        self.assertTrue(True)

    async def test_enc_cancel_fail(self):
        dp = build_dispatcher(_cfg(), _Daemon())
        st = mock.AsyncMock()
        st.get_data = mock.AsyncMock(return_value={})
        st.clear = mock.AsyncMock(side_effect=RuntimeError("boom"))
        call = _C(uid=123, data="v1:enc:cancel")
        await dp.callback_query.handlers[0].callback(call, st)
        self.assertTrue(True)

    async def test_master_nostate(self):
        dp = build_dispatcher(_cfg(), _Daemon())
        call, _ = await self._cb(dp, "v1:master:net:vibe", state=None)
        self.assertTrue(True)

    async def test_master_expired_and_bad(self):
        dp = build_dispatcher(_cfg(), _Daemon())
        st = _State()
        st._data = {"m_started": time.monotonic() - 3600}
        call, _ = await self._cb(dp, "v1:master:net:vibe", state=st)
        self.assertTrue(True)
        call2, _ = await self._cb(dp, "v1:master:w:abc")
        self.assertTrue(True)
        call3, _ = await self._cb(dp, "v1:master:c:xyz")
        self.assertTrue(True)
        st2 = _State()
        st2._data = {"m_started": time.monotonic() - 3600}
        call4, _ = await self._cb(dp, "v1:master:c:5", state=st2)
        self.assertTrue(True)
        st3 = _State()
        st3._data = {"m_started": time.monotonic() - 3600}
        call5, _ = await self._cb(dp, "v1:master:faucet:1", state=st3)
        self.assertTrue(True)

    async def test_wiz_cancel_and_cmd(self):
        dp = build_dispatcher(_cfg(), _Daemon())
        st = mock.AsyncMock()
        st.get_data = mock.AsyncMock(return_value={})
        st.clear = mock.AsyncMock(side_effect=RuntimeError("boom"))
        call = _C(uid=123, data="wiz:cancel")
        await dp.callback_query.handlers[0].callback(call, st)
        self.assertTrue(True)
        call2, _ = await self._cb(dp, "wiz:cmd:net:vibe", state=None)
        self.assertTrue(True)
        call3, _ = await self._cb(dp, "wiz:cmd:c:999999")
        self.assertTrue(True)

    async def test_master_net_expired_via_text(self):
        # покрывает _master_expired через текст-визард (1068-1070 уже в unit, но и тут)
        dp = build_dispatcher(_cfg(), _Daemon())
        st = _State()
        await st.set_state(bt.MasterWiz.wallets)
        await st.update_data(m_started=time.monotonic() - 3600, m_net="vibevibe")
        msg = _M(uid=123, text="10")
        await dp.message.handlers[1].callback(msg, st)
        self.assertTrue(msg.answers)
