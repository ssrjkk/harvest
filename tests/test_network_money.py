"""Money-path tests: nonce, gas, send, receipts.
"""

import asyncio
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from web3 import Web3

from core.network import NetworkManager

ADDR_A = "0x" + "ab" * 20
ADDR_B = "0x" + "cd" * 20
FAKE_HASH = b"\x11" * 32
PRIV = "0x" + "11" * 32
_CK = Web3.to_checksum_address(ADDR_A)  # РґР»СЏ РїСЂСЏРјС‹С… Р·Р°РїРёСЃРµР№ РІ FakeDb (РєРѕРґ РёС‰РµС‚ РїРѕ checksum)


class _FakeAccount:
    def __init__(self, address=ADDR_A):
        self.address = address

    def sign_transaction(self, tx):
        assert tx["chainId"] == 46630
        return SimpleNamespace(raw_transaction=b"\xf8\x01test-raw")


class _FakeAccountNS:
    def from_key(self, key):
        return _FakeAccount()


class _FakeEth:
    """РЎРёРЅС…СЂРѕРЅРЅС‹Р№ С„РµР№Рє eth-РЅРѕРґС‹ (РІС‹Р·С‹РІР°РµС‚СЃСЏ С‡РµСЂРµР· run_in_executor)."""

    def __init__(self):
        self.eth = self
        self.balances = {}
        self.nonces = {}
        self.gas_price = 10**9
        self.fee_history_data = {"baseFeePerGas": [100, 200], "reward": [[5]]}
        self.receipts = {}
        self.sent = []
        self.fail_budget = 0
        self.calls = []
        self.account = _FakeAccountNS()

    def _maybe_fail(self, name):
        self.calls.append(name)
        if self.fail_budget > 0:
            self.fail_budget -= 1
            raise ConnectionError("node down")

    def get_balance(self, address):
        self._maybe_fail("balance")
        return self.balances.get(address.lower(), 0)

    def get_transaction_count(self, address, block):
        self._maybe_fail("nonce")
        return self.nonces.get(address.lower(), 0)

    def get_transaction_receipt(self, tx_hash):
        self._maybe_fail("receipt")
        return self.receipts.get(tx_hash)

    def send_raw_transaction(self, raw):
        self._maybe_fail("send")
        self.sent.append(raw)
        return FAKE_HASH

    def fee_history(self, n, block, rewards):
        self._maybe_fail("feehistory")
        return dict(self.fee_history_data)

    @staticmethod
    def to_wei(amount, unit):
        return Web3.to_wei(amount, unit)


class _FakeDb:
    def __init__(self):
        self.nonces = {}

    async def get_nonce(self, address):
        return self.nonces.get(address.lower())

    async def set_nonce(self, address, nonce):
        self.nonces[address.lower()] = nonce


def _manager(urls="https://rpc.example", eip1559=True):
    cfg = {
        "network": {"rpc_url": urls, "chain_id": 46630},
        "advanced": {"eip1559": eip1559, "receipt_timeout": 30},
        "cache": {},
    }
    nm = NetworkManager(cfg, _FakeDb())
    nm.w3 = _FakeEth()
    return nm


def _nosleep():
    return mock.patch("asyncio.sleep", new=mock.AsyncMock(return_value=None))


class TestNonceLifecycle(unittest.IsolatedAsyncioTestCase):
    async def test_claim_increments(self):
        nm = _manager()
        nm.w3.nonces = {ADDR_A: 7}
        self.assertEqual(await nm.claim_nonce(ADDR_A), 7)
        self.assertEqual(await nm.claim_nonce(ADDR_A), 8)

    async def test_db_nonce_wins_when_higher(self):
        nm = _manager()
        nm.w3.nonces = {ADDR_A: 3}
        nm._db.nonces = {ADDR_A: 10}
        self.assertEqual(await nm.claim_nonce(ADDR_A), 10)

    async def test_release_returns_only_last(self):
        nm = _manager()
        nm.w3.nonces = {ADDR_A: 5}
        await nm.claim_nonce(ADDR_A)  # db -> 6
        await nm.release_nonce(ADDR_A, 5)  # db == 5+1 -> back to 5
        self.assertEqual(nm._db.nonces[ADDR_A], 5)
        nm._db.nonces[ADDR_A] = 9  # С‡СѓР¶Р°СЏ Р·Р°СЏРІРєР° СѓС€Р»Р° РґР°Р»СЊС€Рµ
        await nm.release_nonce(ADDR_A, 5)
        self.assertEqual(nm._db.nonces[ADDR_A], 9)

    async def test_release_without_lock_noop(self):
        nm = _manager()
        await nm.release_nonce(ADDR_A, 1)

    async def test_rollback_when_free(self):
        nm = _manager()
        nm.w3.nonces = {ADDR_A: 5}
        nm._db.nonces = {ADDR_A: 8}
        nm._nonce_locks[_CK] = asyncio.Lock()
        await nm.rollback_nonce_if_free(ADDR_A, 7)
        self.assertEqual(nm._db.nonces[ADDR_A], 7)

    async def test_rollback_keeps_when_busy(self):
        nm = _manager()
        nm.w3.nonces = {ADDR_A: 9}  # pending РІС‹С€Рµ -> Р·Р°РЅСЏС‚
        nm._db.nonces[ADDR_A] = 8
        nm._nonce_locks[_CK] = asyncio.Lock()
        await nm.rollback_nonce_if_free(ADDR_A, 7)
        self.assertEqual(nm._db.nonces[ADDR_A], 8)

    async def test_rollback_keeps_when_rpc_down(self):
        nm = _manager()
        nm.w3.fail_budget = 999
        nm._db.nonces = {ADDR_A: 8}
        nm._nonce_locks[_CK] = asyncio.Lock()
        with _nosleep(), mock.patch.object(nm, "_switch_rpc", new=mock.AsyncMock()):
            await nm.rollback_nonce_if_free(ADDR_A, 7)
        self.assertEqual(nm._db.nonces[ADDR_A], 8)


class TestGasAndFees(unittest.IsolatedAsyncioTestCase):
    async def test_gas_price_cached(self):
        nm = _manager()
        self.assertEqual(await nm.get_gas_price(), 10**9)
        nm.w3.gas_price = 5
        # РІС‚РѕСЂРѕР№ РІС‹Р·РѕРІ вЂ” РёР· РєСЌС€Р°, Р±РµР· РїРѕС…РѕРґР° РІ w3
        self.assertEqual(await nm.get_gas_price(), 10**9)

    async def test_fee_basis_disabled(self):
        nm = _manager(eip1559=False)
        self.assertIsNone(await nm.get_fee_basis(1.0))

    async def test_fee_basis_no_history_falls_back(self):
        nm = _manager()
        nm.w3.fee_history_data = {}
        self.assertIsNone(await nm.get_fee_basis(1.0))
        self.assertFalse(nm._eip1559_capability)
        self.assertIsNone(await nm.get_fee_basis(1.0))

    async def test_fee_basis_math_and_cache(self):
        nm = _manager()
        f = await nm.get_fee_basis(2.0)
        gwei = Web3.to_wei(1, "gwei")
        self.assertEqual(f["maxPriorityFeePerGas"], gwei)
        self.assertEqual(f["maxFeePerGas"], int((200 + gwei) * 2.0) + gwei)
        self.assertEqual(
            nm._build_1559_fields(100, 50, 1.0, 1.0),
            {"maxFeePerGas": 200, "maxPriorityFeePerGas": 50},
        )
        n = len(nm.w3.calls)
        await nm.get_fee_basis(2.0)
        self.assertEqual(len(nm.w3.calls), n)  # РІС‚РѕСЂРѕР№ СЂР°Р· вЂ” РёР· РєСЌС€Р°


class TestRunRetry(unittest.IsolatedAsyncioTestCase):
    async def test_success_first_try(self):
        nm = _manager()
        self.assertEqual(await nm.run_retry(lambda: 42), 42)

    async def test_retryable_then_success(self):
        nm = _manager()
        attempts = {"n": 0}

        def flaky():
            attempts["n"] += 1
            if attempts["n"] < 3:
                raise ConnectionError("flap")
            return "ok"

        with _nosleep(), mock.patch.object(nm, "_switch_rpc", new=mock.AsyncMock()):
            self.assertEqual(await nm.run_retry(flaky), "ok")
        self.assertEqual(attempts["n"], 3)

    async def test_non_retryable_raises_at_once(self):
        nm = _manager()
        with self.assertRaises(ValueError):
            await nm.run_retry(lambda: (_ for _ in ()).throw(ValueError("bad")))

    async def test_breaker_open_raises(self):
        nm = _manager()
        for _ in range(6):
            nm._circuit_breaker.record_failure()
        with self.assertRaises(ConnectionError):
            await nm.run_retry(lambda: 1)


class TestAccountsAndCache(unittest.TestCase):
    def test_get_account_cached_by_address(self):
        nm = _manager()
        a1 = nm.get_account(PRIV)
        a2 = nm.get_account(PRIV)
        self.assertIs(a1, a2)
        self.assertEqual(a1.address, ADDR_A)

    def test_clear_accounts_cache(self):
        nm = _manager()
        nm.get_account(PRIV)
        nm.clear_accounts_cache()
        self.assertEqual(nm._accounts, {})

    def test_invalidate_balance(self):
        nm = _manager()
        nm._balance_cache.set(Web3.to_checksum_address(ADDR_A), 5.0)
        nm.invalidate_balance(ADDR_A)
        self.assertIsNone(nm._balance_cache.get(Web3.to_checksum_address(ADDR_A)))
        nm.invalidate_balance("not-an-address")  # РЅРµ РїР°РґР°РµС‚


class TestCrosscheck(unittest.IsolatedAsyncioTestCase):
    async def _two_nodes(self):
        nm = _manager(urls=["https://a.example", "https://b.example"])
        for n in nm._nodes:
            n.record(50.0, success=True)
        return nm

    async def test_single_node_trusts_primary(self):
        nm = _manager()
        self.assertEqual(await nm._crosscheck_receipt_status(FAKE_HASH, 0), 0)

    async def test_agreeing_secondary(self):
        nm = await self._two_nodes()
        sec = mock.Mock()
        sec.eth.get_transaction_receipt = mock.Mock(return_value={"status": 1})
        nm._connect_rpc = mock.Mock(return_value=sec)
        nm._close_w3_provider = mock.AsyncMock()
        self.assertEqual(await nm._crosscheck_receipt_status(FAKE_HASH, 1), 1)

    async def test_secondary_revert_wins(self):
        nm = await self._two_nodes()
        sec = mock.Mock()
        sec.eth.get_transaction_receipt = mock.Mock(return_value={"status": 0})
        nm._connect_rpc = mock.Mock(return_value=sec)
        nm._close_w3_provider = mock.AsyncMock()
        self.assertEqual(await nm._crosscheck_receipt_status(FAKE_HASH, 1), 0)

    async def test_secondary_missing_keeps_primary(self):
        nm = await self._two_nodes()
        sec = mock.Mock()
        sec.eth.get_transaction_receipt = mock.Mock(return_value=None)
        nm._connect_rpc = mock.Mock(return_value=sec)
        nm._close_w3_provider = mock.AsyncMock()
        self.assertEqual(await nm._crosscheck_receipt_status(FAKE_HASH, 0), 0)

    async def test_secondary_error_keeps_primary(self):
        nm = await self._two_nodes()
        nm._connect_rpc = mock.Mock(side_effect=ConnectionError("down"))
        self.assertEqual(await nm._crosscheck_receipt_status(FAKE_HASH, 1), 1)


class TestReceiptWait(unittest.IsolatedAsyncioTestCase):
    async def test_immediate_receipt(self):
        nm = _manager()
        nm.w3.receipts = {FAKE_HASH: {"status": 1}}
        self.assertEqual(await nm.wait_for_receipt(FAKE_HASH, timeout=5), {"status": 1})

    async def test_timeout_returns_none(self):
        nm = _manager()
        self.assertIsNone(await nm.wait_for_receipt(b"\x22" * 32, timeout=0.01))

    async def test_error_then_receipt(self):
        nm = _manager()
        nm.w3.fail_budget = 2
        nm.w3.receipts = {b"\x33" * 32: {"status": 1}}
        with _nosleep(), mock.patch.object(nm, "_switch_rpc", new=mock.AsyncMock()):
            r = await nm.wait_for_receipt(b"\x33" * 32, timeout=30)
        self.assertEqual(r, {"status": 1})


class TestSendTransfer(unittest.IsolatedAsyncioTestCase):
    def _nm(self):
        nm = _manager()
        nm.w3.nonces = {ADDR_A: 7}
        return nm

    async def test_happy_path_returns_hash(self):
        nm = self._nm()
        nm.w3.receipts = {FAKE_HASH: {"status": 1}}
        h = await nm.send_transfer(PRIV, ADDR_B, 0.001)
        self.assertEqual(h, FAKE_HASH.hex())
        self.assertEqual(len(nm.w3.sent), 1)
        self.assertIsNone(nm._balance_cache.get(ADDR_A))

    async def test_reverted_returns_none(self):
        nm = self._nm()
        nm.w3.receipts = {FAKE_HASH: {"status": 0}}
        self.assertIsNone(await nm.send_transfer(PRIV, ADDR_B, 0.001))

    async def test_timeout_keeps_nonce(self):
        nm = self._nm()
        nm._receipt_timeout = 0.01
        h = await nm.send_transfer(PRIV, ADDR_B, 0.001)
        self.assertIsNone(h)
        # tx СѓС€С‘Р» (sent), nonce РќР• РѕС‚РєР°С‚Р°РЅ: db РѕСЃС‚Р°Р»СЃСЏ РЅР° claimed+1
        self.assertEqual(nm._db.nonces[ADDR_A], 8)

    async def test_send_error_releases_nonce(self):
        nm = self._nm()
        nm.w3.nonces = {ADDR_A: 7}
        nm.w3.send_raw_transaction = mock.Mock(
            side_effect=ConnectionError("mempool busy")
        )
        with _nosleep(), mock.patch.object(nm, "_switch_rpc", new=mock.AsyncMock()):
            h = await nm.send_transfer(PRIV, ADDR_B, 0.001)
        self.assertIsNone(h)
        self.assertEqual(nm._db.nonces[ADDR_A], 7)


class TestProbesAndHealth(unittest.IsolatedAsyncioTestCase):
    async def test_probe_all(self):
        nm = _manager(urls=["https://a.example", "https://b.example"])
        nm.latency_probe = mock.AsyncMock(side_effect=[0.05, None])
        self.assertEqual(
            await nm.probe_all(),
            [("https://a.example", 0.05), ("https://b.example", None)],
        )

    def test_record_and_best(self):
        nm = _manager(urls=["https://a.example", "https://b.example"])
        nm._record_latency("https://a.example", 0.05)
        nm._record_latency("https://b.example", None)
        self.assertEqual(nm.best_rpc_url(), "https://a.example")
        health = nm.node_health()
        self.assertEqual(len(health), 2)
        self.assertFalse(health[0]["dead"])

    def test_node_poll_degraded(self):
        nm = _manager()
        self.assertFalse(nm._node_poll_degraded())
        for n in nm._nodes:
            if n.url == nm.rpc_url:
                for _ in range(4):
                    n.record(2500.0, success=False)
        self.assertTrue(nm._node_poll_degraded())

    def test_log_helpers(self):
        nm = _manager()
        nm.log_diagnostics()

    def test_start_monitor_single_is_noop(self):
        nm = _manager()
        nm.start_monitor()
        self.assertFalse(nm.monitor_started())

    async def test_start_monitor_multi_spawns(self):
        nm = _manager(urls=["https://a.example", "https://b.example"])
        nm.probe_all = mock.AsyncMock(
            return_value=[("https://a.example", 0.05), ("https://b.example", 0.09)]
        )
        try:
            nm.start_monitor()
            self.assertTrue(nm.monitor_started())
        finally:
            if nm._monitor_task is not None:
                nm._monitor_task.cancel()

    async def test_monitor_single_iteration_then_cancel(self):
        nm = _manager(urls=["https://a.example", "https://b.example"])
        nm.probe_all = mock.AsyncMock(return_value=[("https://a.example", 0.05), ("https://b.example", 0.09)])
        nm._switch_to = mock.AsyncMock()
        task = asyncio.create_task(nm._monitor_rpc())
        await asyncio.sleep(0.05)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task

    def test_diagnostics(self):
        nm = _manager()
        d = nm.diagnostics()
        self.assertIsInstance(d, dict)
        self.assertIn("rpc_urls", d)
        self.assertEqual(d["chain_id"], 46630)


class TestClosePaths(unittest.IsolatedAsyncioTestCase):
    async def test_close_session_variants(self):
        nm = _manager()
        await nm._close_session(None)
        sync = mock.Mock()
        await nm._close_session(sync)
        sync.close.assert_called_once()
        Async = mock.AsyncMock()
        await nm._close_session(Async)
        bad = mock.Mock()
        bad.close.side_effect = RuntimeError("x")
        await nm._close_session(bad)  # РЅРµ РїР°РґР°РµС‚

    async def test_close_w3_provider(self):
        nm = _manager()
        await nm._close_w3_provider(None)
        # РїСЂРѕРІР°Р№РґРµСЂ Р±РµР· РєСЌС€Р° СЃРµСЃСЃРёР№ вЂ” no-op
        await nm._close_w3_provider(SimpleNamespace(provider=SimpleNamespace()))
        s = mock.AsyncMock()
        mgr = SimpleNamespace(
            session_cache=SimpleNamespace(items=lambda: [("e", s)]),
            _explicit_session=None,
        )
        await nm._close_w3_provider(SimpleNamespace(provider=SimpleNamespace(_request_session_manager=mgr)))

    def test_connect_rpc_http_warns(self):
        nm = _manager()
        w3 = nm._connect_rpc("http://127.0.0.1:8545")
        self.assertIsNotNone(w3)


if __name__ == "__main__":
    unittest.main(verbosity=2)



