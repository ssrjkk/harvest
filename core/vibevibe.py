"""Работа с контрактами VibeVibe через загружаемый ABI.

Реальные подписанные транзакции к методам контракта (swap, mint и т.п.).
Метод и наличие аргумента (amount) настраиваются в config.yaml -> actions.
Nonce берётся из БД через claim_nonce — параллельные транзакции одного адреса не конфликтуют.
"""

import asyncio
import json
import logging
from typing import Any

from web3 import Web3

from core.network import NetworkManager
from core.utils import resolve_bundled

logger = logging.getLogger(__name__)

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
            nonce = await self.network.claim_nonce(from_addr)
            gas_price = int(await self.network.get_gas_price() * gas_mult)

            def _build_tx() -> dict:
                # Контракт берём свежим на каждой попытке: после RPC-failover
                # self.w3 меняется, и закэшированный ранее contract укажет на старый провайдер
                contract = self._get_contract(contract_address)
                fn = getattr(contract.functions, method)
                tx = fn(*args).build_transaction(
                    {
                        "from": from_addr,
                        "nonce": nonce,
                        "gasPrice": gas_price,
                        "value": 0,
                        "chainId": self.network.chain_id,
                    }
                )
                # оценка газа через estimateGas с ограничением configured gas_limit
                try:
                    estimated = fn(*args).estimate_gas({"from": from_addr, "value": 0})
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
            if receipt is not None and receipt.get("status") == 1:
                self.network.invalidate_balance(from_addr)
                return tx_hash.hex()
            logger.warning(
                f"Метод {method} reverted: tx={tx_hash.hex()}, status={receipt.get('status') if receipt else 'unknown'}"
            )
            self.network.invalidate_balance(from_addr)
            await self.network.release_nonce(from_addr, nonce)
            return None
        except Exception as e:
            logger.error(f"Контрактный вызов {method} ошибка: {e}")
            # Откатываем nonce только если транзакция гарантированно не ушла в сеть
            if nonce is not None and not sent and from_addr is not None:
                try:
                    await self.network.rollback_nonce_if_free(from_addr, nonce)
                except Exception as e:
                    logger.warning(f"Не удалось откатить nonce {nonce} для {from_addr}: {e}")
            return None
