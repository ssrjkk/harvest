"""Работа с контрактами VibeVibe через загружаемый ABI.

Реальные подписанные транзакции к методам контракта (swap, mint и т.п.).
Метод и наличие аргумента (amount) настраиваются в config.yaml -> actions.
Nonce берётся из БД через claim_nonce — параллельные транзакции одного адреса не конфликтуют.

Дополнительно: EIP-712 подпись TradeIntent для VibePassMarket (buy/sell).
Владелец рынка подписывает intent ключом tradeSigner (authorization bytes),
а фермер передаёт их в buy/sell. Ключ подписи настраивается через
config -> vibevibe.signer_key или env FARMER_VIBE_SIGNER_KEY.
"""

import asyncio
import json
import logging
import secrets
import time
from typing import Any

from web3 import Web3

from core.network import NetworkManager
from core.utils import resolve_bundled

logger = logging.getLogger(__name__)

# EIP-712 типы для VibePassMarket.TradeIntent (контракт верифицирован, схема фиксирована).
# EIP712Domain eth-account строит сам из domain_data (name/version/chainId/verifyingContract).
# struct TradeIntent(address account,uint256 loungeId,uint8 action,address recipient,
#                    uint256 limit,uint256 deadline,bytes32 requestId,uint256 authorizationEpoch)
_TRADE_INTENT_TYPES = {
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
}

# EIP-712 домен VibePassMarket (конструктор EIP712("VibePassMarket", "0")).
_TRADE_DOMAIN_NAME = "VibePassMarket"
_TRADE_DOMAIN_VERSION = "0"

# action: 0 buy, 1 sell (см. VibePassMarketBase.TradeIntent).
_TRADE_ACTION_BUY = 0
_TRADE_ACTION_SELL = 1

# Слиппедж-запас для buy: msg.value = сумма, limit = maxTotalCost с запасом.
# Иначе подпись (максимальная цена) vs изменившийся курс рынка дают revert
# SlippageExceeded на каждом скачке цены.
_TRADE_SLIPPAGE_BPS = 300  # 3%

_PLACEHOLDER_ABI = [
    {
        "inputs": [{"internalType": "uint256", "name": "amount", "type": "uint256"}],
        "name": "swap",
        "outputs": [],
        "stateMutability": "nonpayable",
        "type": "function",
    },
    {
        "inputs": [],
        "name": "mint",
        "outputs": [],
        "stateMutability": "nonpayable",
        "type": "function",
    },
]


class VibeVibeInterface:
    def __init__(self, network: NetworkManager, config: dict) -> None:
        self.network = network
        # ABI path берётся из config (network.abi_path) или дефолт vibevibe
        net_cfg = config.get("network", {})
        abi_path = net_cfg.get("abi_path", "abi/vibevibe.json")
        self.abi = self._load_abi(abi_path)
        self._contracts: dict[str, tuple[Any, Any]] = {}
        # Gas limit из конфига для ограничения estimate_gas.
        # Дефолт 300000 — контрактные вызовы требуют больше gas, чем transfer (21000).
        self._gas_limit = config.get("advanced", {}).get("gas_limit", 300000)

    @property
    def w3(self):
        """Динамический провайдер — после RPC-failover контракты работают через новый."""
        return self.network.w3

    def _load_abi(self, abi_path: str = "abi/vibevibe.json") -> list:
        path = resolve_bundled(abi_path)
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
            abi = data.get("abi") if isinstance(data, dict) else data
            if not abi:
                logger.warning(f"ABI пуст ({path}), используется заглушка. Замените {path}.")
                return _PLACEHOLDER_ABI
            return abi
        except FileNotFoundError:
            logger.warning(f"ABI-файл не найден ({path}), используется заглушка. Замените {path}.")
            return _PLACEHOLDER_ABI

    def _get_contract(self, address: str) -> Any:
        address = Web3.to_checksum_address(address)
        entry = self._contracts.get(address)
        if entry is None or entry[0] is not self.w3:
            # Контракт привязан к w3; после failover пересоздаём на новом провайдере
            # и закрываем устаревшие ссылки, чтобы не держать старые сессии
            if entry is not None:
                # _close_w3_provider асинхронный: дожимаем через ensure_future и
                # снимаем результат в done-callback (иначе — unused coroutine).
                task = asyncio.ensure_future(self.network._close_w3_provider(entry[0]))

                def _on_done(t: asyncio.Task) -> None:
                    if not t.cancelled():
                        t.exception()

                task.add_done_callback(_on_done)
                del self._contracts[address]
            self._contracts[address] = (
                self.w3,
                self.w3.eth.contract(address=address, abi=self.abi),
            )
        return self._contracts[address][1]

    async def call_method(
        self,
        contract_address: str,
        method: str,
        private_key: str,
        amount_wei: int | None = None,
        gas_mult: float = 1.0,
        value_wei: int | None = None,
    ) -> str | None:
        """Вызывает метод контракта, подписывает и ждёт подтверждения. None при ошибке."""
        sent = False
        nonce = None
        from_addr = None
        try:
            contract_address = Web3.to_checksum_address(contract_address)
            account = self.network.get_account(private_key)
            from_addr = Web3.to_checksum_address(account.address)
            # Ранняя проверка наличия метода (не блокирует, ошибается громко)
            if getattr(self._get_contract(contract_address).functions, method, None) is None:
                logger.error(f"Метод {method!r} не найден в ABI")
                return None

            args = (amount_wei,) if amount_wei is not None else ()
            tx_value = int(value_wei or 0)
            nonce = await self.network.claim_nonce(from_addr)
            legacy_gas_price = int(await self.network.get_gas_price() * gas_mult)
            # EIP-1559: dict с maxFeePerGas/maxPriorityFeePerGas при поддержке сетью,
            # иначе None → legacy gasPrice ровно как раньше (graceful fallback, nonce не трогает)
            fee_1559 = await self.network.get_fee_basis(gas_mult)

            def _build_tx() -> dict:
                # Контракт берём свежим на каждой попытке: после RPC-failover
                # self.w3 меняется, и закэшированный ранее contract укажет на старый провайдер
                contract = self._get_contract(contract_address)
                fn = getattr(contract.functions, method)
                fee_fields = fee_1559 if fee_1559 else {"gasPrice": legacy_gas_price}
                tx = fn(*args).build_transaction(
                    {
                        "from": from_addr,
                        "nonce": nonce,
                        "value": tx_value,
                        "chainId": self.network.chain_id,
                        **fee_fields,
                    }
                )
                # оценка газа через estimateGas с ограничением configured gas_limit
                try:
                    estimated = fn(*args).estimate_gas({"from": from_addr, "value": tx_value})
                    gas_limit = int(estimated * 1.3) + 50000  # буфер 30% + запас
                except Exception as e:
                    logger.warning(
                        f"estimate_gas упал для {contract_address[:10]}:{method} — fallback gas {self._gas_limit}: {e}"
                    )
                    gas_limit = self._gas_limit  # fallback из конфига
                # Ограничиваем gas_limit из конфига (advanced.gas_limit)
                tx["gas"] = min(gas_limit, self._gas_limit)
                return tx

            tx = await self.network.run_retry(_build_tx)
            signed = account.sign_transaction(tx)
            tx_hash = await self.network.send_raw_transaction(signed.raw_transaction)
            sent = True
            receipt = await self.network.wait_for_receipt(tx_hash)
            if receipt is None:
                # Не дождались ресипта в пределах бюджета: tx принят в mempool,
                # nonce занят — откатывать нельзя (может уйти в сеть позже).
                logger.warning(f"Метод {method} unconfirmed: tx={tx_hash.hex()} (в mempool)")
                return None
            if receipt.get("status") != 1:
                logger.warning(f"Метод {method} reverted: tx={tx_hash.hex()}, status={receipt.get('status')}")
                self.network.invalidate_balance(from_addr)
                await self.network.release_nonce(from_addr, nonce)
                return None
            self.network.invalidate_balance(from_addr)
            return tx_hash.hex()
        except Exception as e:
            logger.error(f"Контрактный вызов {method} ошибка: {e}")
            # Откатываем nonce только если транзакция гарантированно не ушла в сеть
            if nonce is not None and not sent and from_addr is not None:
                try:
                    await self.network.rollback_nonce_if_free(from_addr, nonce)
                except Exception as e:
                    logger.warning(f"Не удалось откатить nonce {nonce} для {from_addr}: {e}")
            return None

    async def read_authorization_epoch(self, contract_address: str) -> int:
        """Текущий authorizationEpoch VibePassMarket (нужен для EIP-712 digest)."""
        try:
            contract_address = Web3.to_checksum_address(contract_address)
            contract = self._get_contract(contract_address)
            epoch = await self.network.run_in_executor(contract.functions.authorizationEpoch().call)
            return int(epoch or 0)
        except Exception as e:
            logger.warning(f"authorizationEpoch недоступен для {contract_address}: {e}")
            return 0

    def sign_trade_intent(
        self,
        contract_address: str,
        signer_key: str,
        *,
        account: str,
        lounge_id: int,
        action: int,
        recipient: str,
        limit: int,
        deadline: int,
        request_id: bytes,
        authorization_epoch: int,
    ) -> bytes:
        """EIP-712 подпись TradeIntent ключом tradeSigner → authorization bytes.

        Возвращает 65-байтовую подпись (r||s||v), которую VibePassMarket
        принимает через SignatureChecker.isValidSignatureNow. digest считается
        ровно как в контракте: _hashTypedDataV4(keccak256(abi.encode(...))).
        """
        from eth_account import Account
        from eth_account.messages import encode_typed_data

        domain = {
            "name": _TRADE_DOMAIN_NAME,
            "version": _TRADE_DOMAIN_VERSION,
            "chainId": self.network.chain_id,
            "verifyingContract": Web3.to_checksum_address(contract_address),
        }
        message = {
            "account": Web3.to_checksum_address(account),
            "loungeId": int(lounge_id),
            "action": int(action),
            "recipient": Web3.to_checksum_address(recipient),
            "limit": int(limit),
            "deadline": int(deadline),
            "requestId": "0x" + request_id.hex(),
            "authorizationEpoch": int(authorization_epoch),
        }
        signed = Account.sign_message(
            encode_typed_data(
                domain_data=domain,
                message_types=_TRADE_INTENT_TYPES,
                message_data=message,
            ),
            private_key=signer_key,
        )
        return signed.signature

    async def call_trade(
        self,
        contract_address: str,
        private_key: str,
        signer_key: str,
        *,
        lounge_id: int,
        action: int,
        amount_wei: int,
        gas_mult: float = 1.0,
    ) -> str | None:
        """Buy/sell на VibePassMarket через EIP-712 подпись tradeSigner.

        authorization — подпись TradeIntent ключом signer_key; без неё
        frontendOnly рынок ревертит InvalidAuthorization. Buy — payable
        (value = amount_wei), sell — no-value (amount_wei = minProceeds).
        """
        try:
            contract_address = Web3.to_checksum_address(contract_address)
            account = self.network.get_account(private_key)
            from_addr = Web3.to_checksum_address(account.address)
            if action not in (_TRADE_ACTION_BUY, _TRADE_ACTION_SELL):
                logger.error(f"call_trade: неизвестный action={action}")
                return None
            method = "buy" if action == _TRADE_ACTION_BUY else "sell"
            # Buy: limit = maxTotalCost (с запасом на слиппедж), value = amount_wei.
            # Sell: limit = minProceeds (amount_wei), value = 0.
            if action == _TRADE_ACTION_BUY:
                limit = int(amount_wei * (1 + _TRADE_SLIPPAGE_BPS / 10000))
            else:
                limit = int(amount_wei)
            epoch = await self.read_authorization_epoch(contract_address)
            deadline = int(time.time()) + 600
            request_id = secrets.token_bytes(32)
            authorization = self.sign_trade_intent(
                contract_address,
                signer_key,
                account=from_addr,
                lounge_id=lounge_id,
                action=action,
                recipient=from_addr,
                limit=limit,
                deadline=deadline,
                request_id=request_id,
                authorization_epoch=epoch,
            )
            if action == _TRADE_ACTION_BUY:
                args = (lounge_id, limit, deadline, request_id, authorization)
                value = int(amount_wei)
            else:
                args = (lounge_id, limit, from_addr, deadline, request_id, authorization)
                value = 0
            # call_method не умеет positional args — строим tx здесь.
            return await self._call_contract_tx(
                contract_address, method, private_key, args=args, value_wei=value, gas_mult=gas_mult
            )
        except Exception as e:
            logger.error(f"VibePassMarket trade (action={action}) ошибка: {e}")
            return None

    async def call_register(
        self,
        contract_address: str,
        private_key: str,
        *,
        identity_key: bytes,
        creator: str,
        uri: str,
        gas_mult: float = 1.0,
    ) -> str | None:
        """registerLounge на VibeLoungeRegistry (только REGISTRAR_ROLE).

        Обычный кошелёк вызов ревертит (NotCreator/роль). Метод существует для
        полноты и подписанных платформенных флоу; в пачном фарме не используется.
        """
        try:
            contract_address = Web3.to_checksum_address(contract_address)
            args = (Web3.to_checksum_address(identity_key) if isinstance(identity_key, str) else identity_key,
                    Web3.to_checksum_address(creator), uri)
            return await self._call_contract_tx(
                contract_address, "registerLounge", private_key, args=args, value_wei=0, gas_mult=gas_mult
            )
        except Exception as e:
            logger.error(f"registerLounge ошибка: {e}")
            return None

    async def _call_contract_tx(
        self,
        contract_address: str,
        method: str,
        private_key: str,
        *,
        args: tuple,
        value_wei: int,
        gas_mult: float,
    ) -> str | None:
        """Общий подписанный вызов контракта с явным tuple аргументов и value."""
        sent = False
        nonce = None
        from_addr = None
        try:
            contract_address = Web3.to_checksum_address(contract_address)
            account = self.network.get_account(private_key)
            from_addr = Web3.to_checksum_address(account.address)
            if getattr(self._get_contract(contract_address).functions, method, None) is None:
                logger.error(f"Метод {method!r} не найден в ABI")
                return None
            tx_value = int(value_wei or 0)
            nonce = await self.network.claim_nonce(from_addr)
            legacy_gas_price = int(await self.network.get_gas_price() * gas_mult)
            fee_1559 = await self.network.get_fee_basis(gas_mult)

            def _build_tx() -> dict:
                contract = self._get_contract(contract_address)
                fn = getattr(contract.functions, method)
                fee_fields = fee_1559 if fee_1559 else {"gasPrice": legacy_gas_price}
                tx = fn(*args).build_transaction(
                    {
                        "from": from_addr,
                        "nonce": nonce,
                        "value": tx_value,
                        "chainId": self.network.chain_id,
                        **fee_fields,
                    }
                )
                try:
                    estimated = fn(*args).estimate_gas({"from": from_addr, "value": tx_value})
                    gas_limit = int(estimated * 1.3) + 50000
                except Exception as e:
                    logger.warning(
                        f"estimate_gas упал для {contract_address[:10]}:{method} — fallback gas {self._gas_limit}: {e}"
                    )
                    gas_limit = self._gas_limit
                tx["gas"] = min(gas_limit, self._gas_limit)
                return tx

            tx = await self.network.run_retry(_build_tx)
            signed = account.sign_transaction(tx)
            tx_hash = await self.network.send_raw_transaction(signed.raw_transaction)
            sent = True
            receipt = await self.network.wait_for_receipt(tx_hash)
            if receipt is None:
                logger.warning(f"Метод {method} unconfirmed: tx={tx_hash.hex()} (в mempool)")
                return None
            if receipt.get("status") != 1:
                logger.warning(f"Метод {method} reverted: tx={tx_hash.hex()}, status={receipt.get('status')}")
                self.network.invalidate_balance(from_addr)
                await self.network.release_nonce(from_addr, nonce)
                return None
            self.network.invalidate_balance(from_addr)
            return tx_hash.hex()
        except Exception as e:
            logger.error(f"Контрактный вызов {method} ошибка: {e}")
            if nonce is not None and not sent and from_addr is not None:
                try:
                    await self.network.rollback_nonce_if_free(from_addr, nonce)
                except Exception as e:
                    logger.warning(f"Не удалось откатить nonce {nonce} для {from_addr}: {e}")
            return None
