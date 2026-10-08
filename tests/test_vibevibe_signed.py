"""Тесты подписанного флоу VibePassMarket: EIP-712 TradeIntent, call_trade, call_register.

Проверяем, что:
- sign_trade_intent даёт 65-байтовую ECDSA-подпись, восстанавливаемую в account;
- digest соответствует схеме контракта (domain VibePassMarket/"0", chainId, verifyingContract);
- call_trade вызывает buy (payable, value=amount) / sell (no-value) с authorization;
- call_register вызывает registerLounge;
- ActionExecutor корректно маршрутизирует vibevibe_buy/sell/register и пропускает
  действие без signer_key / lounge_id.
"""

import asyncio
import sys
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from eth_account import Account
from eth_account.messages import encode_typed_data
from web3 import Web3

from core.actions import ActionExecutor
from core.vibevibe import (
    _TRADE_ACTION_BUY,
    _TRADE_ACTION_SELL,
    _TRADE_DOMAIN_NAME,
    _TRADE_DOMAIN_VERSION,
    VibeVibeInterface,
)

_ADDR = "0x" + "11" * 20
_ADDR_CS = Web3.to_checksum_address(_ADDR)
_SIGNER_KEY = "0x" + "ab" * 32
_SIGNER_ADDR = Account.from_key(_SIGNER_KEY).address
_MARKET = "0x" + "22" * 20
_MARKET_CS = Web3.to_checksum_address(_MARKET)


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _make_vi():
    vi = VibeVibeInterface.__new__(VibeVibeInterface)
    vi.network = MagicMock()
    vi.network.w3 = MagicMock()
    vi.network.chain_id = 46630
    vi.abi = []
    vi._contracts = {}
    vi._gas_limit = 300000
    return vi


class TestSignTradeIntent(unittest.TestCase):
    def test_signature_is_65_bytes_and_recovers_signer(self):
        vi = _make_vi()
        sig = vi.sign_trade_intent(
            _MARKET_CS,
            _SIGNER_KEY,
            account=_ADDR_CS,
            lounge_id=1,
            action=_TRADE_ACTION_BUY,
            recipient=_ADDR_CS,
            limit=10**15,
            deadline=9999999999,
            request_id=b"\x01" * 32,
            authorization_epoch=3,
        )
        self.assertEqual(len(sig), 65)

        # EIP-712 digest считается так же, как в контракте (ручная проверка схемы).
        domain = {
            "name": _TRADE_DOMAIN_NAME,
            "version": _TRADE_DOMAIN_VERSION,
            "chainId": 46630,
            "verifyingContract": _MARKET_CS,
        }
        message = {
            "account": _ADDR_CS,
            "loungeId": 1,
            "action": _TRADE_ACTION_BUY,
            "recipient": _ADDR_CS,
            "limit": 10**15,
            "deadline": 9999999999,
            "requestId": "0x" + (b"\x01" * 32).hex(),
            "authorizationEpoch": 3,
        }
        enc = encode_typed_data(
            domain_data=domain,
            message_types={
                "TradeIntent": [
                    {"name": "account", "type": "address"},
                    {"name": "loungeId", "type": "uint256"},
                    {"name": "action", "type": "uint8"},
                    {"name": "recipient", "type": "address"},
                    {"name": "limit", "type": "uint256"},
                    {"name": "deadline", "type": "uint256"},
                    {"name": "requestId", "type": "bytes32"},
                    {"name": "authorizationEpoch", "type": "uint256"},
                ],
            },
            message_data=message,
        )
        recovered = Account.recover_message(enc, signature=sig)
        self.assertEqual(recovered, _SIGNER_ADDR)

    def test_signature_different_for_sell(self):
        vi = _make_vi()
        sig_buy = vi.sign_trade_intent(
            _MARKET_CS, _SIGNER_KEY, account=_ADDR_CS, lounge_id=1, action=_TRADE_ACTION_BUY,
            recipient=_ADDR_CS, limit=100, deadline=9999999999, request_id=b"\x02" * 32,
            authorization_epoch=0,
        )
        sig_sell = vi.sign_trade_intent(
            _MARKET_CS, _SIGNER_KEY, account=_ADDR_CS, lounge_id=1, action=_TRADE_ACTION_SELL,
            recipient=_ADDR_CS, limit=100, deadline=9999999999, request_id=b"\x02" * 32,
            authorization_epoch=0,
        )
        self.assertNotEqual(sig_buy, sig_sell)


class TestReadAuthorizationEpoch(unittest.TestCase):
    def test_reads_epoch_from_contract(self):
        vi = _make_vi()
        mock_contract = MagicMock()
        mock_contract.functions.authorizationEpoch().call = MagicMock(return_value=7)
        vi._contracts[_MARKET_CS] = (vi.network.w3, mock_contract)
        vi.network.run_in_executor = AsyncMock(side_effect=lambda fn: fn())
        self.assertEqual(_run(vi.read_authorization_epoch(_MARKET_CS)), 7)

    def test_epoch_error_returns_zero(self):
        vi = _make_vi()
        mock_contract = MagicMock()
        mock_contract.functions.authorizationEpoch().call = MagicMock(side_effect=RuntimeError("boom"))
        vi._contracts[_MARKET_CS] = (vi.network.w3, mock_contract)
        vi.network.run_in_executor = AsyncMock(side_effect=lambda fn: fn())
        self.assertEqual(_run(vi.read_authorization_epoch(_MARKET_CS)), 0)


class TestCallTrade(unittest.TestCase):
    def _setup_vi(self):
        vi = _make_vi()
        vi.network.get_account = MagicMock(return_value=Account.from_key("0x" + "cd" * 32))
        vi.network.claim_nonce = AsyncMock(return_value=0)
        vi.network.get_gas_price = AsyncMock(return_value=1000000000)
        vi.network.get_fee_basis = AsyncMock(return_value=None)
        vi.network.run_retry = AsyncMock(side_effect=lambda fn: fn())
        vi.network.send_raw_transaction = AsyncMock(return_value=MagicMock(hex=lambda: "0xhash"))
        vi.network.wait_for_receipt = AsyncMock(return_value={"status": 1})
        vi.network.invalidate_balance = MagicMock()
        vi.network.release_nonce = AsyncMock()
        vi.network.rollback_nonce_if_free = AsyncMock()
        vi.network.run_in_executor = AsyncMock(side_effect=lambda fn: fn())
        vi.read_authorization_epoch = AsyncMock(return_value=5)
        # contract with buy/sell methods
        mock_contract = MagicMock()

        def _make_fn(method):
            fn = MagicMock()
            fn.return_value.build_transaction = MagicMock(side_effect=lambda d: {**d, "gas": 200000})
            fn.return_value.estimate_gas = MagicMock(return_value=120000)
            return fn

        mock_contract.functions = MagicMock()
        mock_contract.functions.buy = _make_fn("buy")
        mock_contract.functions.sell = _make_fn("sell")
        vi._contracts[_MARKET_CS] = (vi.network.w3, mock_contract)
        return vi

    def test_call_buy_success(self):
        vi = self._setup_vi()
        result = _run(
            vi.call_trade(
                _MARKET_CS,
                "0x" + "cd" * 32,
                _SIGNER_KEY,
                lounge_id=1,
                action=_TRADE_ACTION_BUY,
                amount_wei=10**15,
            )
        )
        self.assertIsNotNone(result)
        # buy должен быть payable: value = amount
        buy = vi._contracts[_MARKET_CS][1].functions.buy
        tx = buy.return_value.build_transaction.call_args[0][0]
        self.assertEqual(tx["value"], 10**15)
        # authorization — 65 байт, а не пустой
        args = buy.call_args[0]
        self.assertEqual(len(args[-1]), 65)

    def test_call_sell_success(self):
        vi = self._setup_vi()
        result = _run(
            vi.call_trade(
                _MARKET_CS,
                "0x" + "cd" * 32,
                _SIGNER_KEY,
                lounge_id=1,
                action=_TRADE_ACTION_SELL,
                amount_wei=10**15,
            )
        )
        self.assertIsNotNone(result)
        sell = vi._contracts[_MARKET_CS][1].functions.sell
        tx = sell.return_value.build_transaction.call_args[0][0]
        self.assertEqual(tx["value"], 0)

    def test_call_trade_bad_action(self):
        vi = self._setup_vi()
        result = _run(
            vi.call_trade(
                _MARKET_CS,
                "0x" + "cd" * 32,
                _SIGNER_KEY,
                lounge_id=1,
                action=99,
                amount_wei=10**15,
            )
        )
        self.assertIsNone(result)


class TestCallRegister(unittest.TestCase):
    def test_call_register_calls_contract(self):
        vi = _make_vi()
        vi.network.get_account = MagicMock(return_value=Account.from_key("0x" + "cd" * 32))
        vi.network.claim_nonce = AsyncMock(return_value=0)
        vi.network.get_gas_price = AsyncMock(return_value=1000000000)
        vi.network.get_fee_basis = AsyncMock(return_value=None)
        vi.network.run_retry = AsyncMock(side_effect=lambda fn: fn())
        vi.network.send_raw_transaction = AsyncMock(return_value=MagicMock(hex=lambda: "0xhash"))
        vi.network.wait_for_receipt = AsyncMock(return_value={"status": 1})
        vi.network.invalidate_balance = MagicMock()
        vi.network.release_nonce = AsyncMock()
        vi.network.rollback_nonce_if_free = AsyncMock()
        mock_contract = MagicMock()
        mock_reg = MagicMock()
        mock_reg.return_value.build_transaction = MagicMock(side_effect=lambda d: {**d, "gas": 200000})
        mock_reg.return_value.estimate_gas = MagicMock(return_value=120000)
        mock_contract.functions = MagicMock()
        mock_contract.functions.registerLounge = mock_reg
        vi._contracts[_MARKET_CS] = (vi.network.w3, mock_contract)
        result = _run(
            vi.call_register(
                _MARKET_CS,
                "0x" + "cd" * 32,
                identity_key=b"\x05" * 32,
                creator=_ADDR_CS,
                uri="https://vibevibe.fun",
            )
        )
        self.assertIsNotNone(result)
        args = mock_reg.call_args[0]
        self.assertEqual(args[2], "https://vibevibe.fun")


def _action_executor(signer_key=""):
    cfg = {
        "network": {"rpc_url": "http://localhost:8545", "chain_id": 46630},
        "actions": [],
        "advanced": {"gas_limit": 300000, "dry_run": False},
        "vibevibe": {"signer_key": signer_key},
        "database": {"path": "db.sqlite"},
    }
    net = MagicMock()
    net.w3 = MagicMock()
    net.w3.to_wei = MagicMock(side_effect=lambda x, u: int(x * 10**18))
    vibevibe = MagicMock()
    executor = ActionExecutor.__new__(ActionExecutor)
    executor.network = net
    executor.vibevibe = vibevibe
    executor.actions_conf = []
    executor._outcomes = []
    executor.gas_limit = 300000
    executor.dry_run = False
    executor.writer = MagicMock()
    executor.signer_key = (cfg.get("vibevibe") or {}).get("signer_key") or ""
    executor.buffer_log = AsyncMock()
    return executor, net, vibevibe


class TestActionDispatch(unittest.TestCase):
    def test_signed_trade_skipped_without_signer_key(self):
        executor, _, vibevibe = _action_executor()
        ok = _run(
            executor._signed_trade(
                {"address": _ADDR_CS},
                {"type": "vibevibe_buy", "contract": _MARKET_CS, "lounge_id": 1},
                amount=0.001,
            )
        )
        self.assertFalse(ok)
        vibevibe.call_trade.assert_not_called()

    def test_signed_trade_skipped_without_lounge_id(self):
        executor, _, vibevibe = _action_executor(signer_key=_SIGNER_KEY)
        ok = _run(
            executor._signed_trade(
                {"address": _ADDR_CS},
                {"type": "vibevibe_buy", "contract": _MARKET_CS, "lounge_id": 0},
                amount=0.001,
            )
        )
        self.assertFalse(ok)
        vibevibe.call_trade.assert_not_called()

    def test_signed_trade_calls_contract(self):
        executor, _, vibevibe = _action_executor(signer_key=_SIGNER_KEY)
        vibevibe.call_trade = AsyncMock(return_value="0xabc")
        ok = _run(
            executor._signed_trade(
                {"address": _ADDR_CS, "private_key": "0x" + "cd" * 32},
                {
                    "type": "vibevibe_sell",
                    "contract": _MARKET_CS,
                    "lounge_id": 3,
                    "min_amount": 0.001,
                    "max_amount": 0.002,
                },
                amount=0.0015,
            )
        )
        self.assertTrue(ok)
        vibevibe.call_trade.assert_awaited_once()
        call = vibevibe.call_trade.await_args
        self.assertEqual(call.kwargs["lounge_id"], 3)
        self.assertEqual(call.kwargs["action"], _TRADE_ACTION_SELL)


if __name__ == "__main__":
    unittest.main()
