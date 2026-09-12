"""Управление кошельками: генерация, загрузка, экспорт."""

import asyncio
import logging
from concurrent.futures import ThreadPoolExecutor

from eth_account import Account

from core.database import Database
from core.exporter import export_to_file as export_wallets_to_json
from core.performance import effective_gen_workers

logger = logging.getLogger(__name__)

# батч для массовой вставки в БД (одна транзакция на батч)
_WRITE_BATCH = 200


class WalletManager:
    def __init__(self, config: dict, db: Database) -> None:
        self.config = config
        self.db = db
        self.gen_workers = effective_gen_workers(config)

    def generate_wallet(self) -> dict:
        # web3 6: Account.create() -> .mnemonic
        # web3 7: Account.create_with_mnemonic() -> (account, mnemonic)
        try:
            Account.enable_unaudited_hdwallet_features()
        except Exception:
            pass
        if hasattr(Account, "create_with_mnemonic"):
            account, mnemonic = Account.create_with_mnemonic()
        else:
            account = Account.create()
            mnemonic = getattr(account, "mnemonic", "")
        return {
            "address": account.address,
            "private_key": account.key.hex(),
            "mnemonic": mnemonic,
        }

    async def create_wallets(self, count: int) -> list[dict]:
        """Массовая генерация кошельков + батч-запись в БД.

        Генерация ключей CPU-bound, но PBKDF2 частично отпускает GIL — раскидываем
        батч по _GEN_WORKERS потокам (gather), а не одним агрегатным заданием.
        Результат пишется транзакционными батчами в БД.
        """
        wallets: list[dict] = []
        loop = asyncio.get_running_loop()

        async def _gen_on(pool) -> dict:
            return await loop.run_in_executor(pool, self.generate_wallet)

        with ThreadPoolExecutor(max_workers=self.gen_workers) as pool:
            for start in range(0, count, _WRITE_BATCH):
                n = min(_WRITE_BATCH, count - start)
                batch = await asyncio.gather(*[_gen_on(pool) for _ in range(n)])
                # батч-запись одной транзакцией
                saved = await self.db.save_wallets_batch(
                    [(w["address"], w["private_key"], w["mnemonic"]) for w in batch]
                )
                if not saved:
                    # Ранее save_wallets_batch молча глотал ошибку: кошельки уходили
                    # в возвращаемый список, но в БД их не было — «тихая» потеря
                    # приватных ключей при рестарте. Теперь — громкая остановка.
                    raise RuntimeError(
                        f"Ошибка сохранения кошельков в БД (батч {start + 1}–"
                        f"{min(start + n, count)}): приватные ключи не записались. "
                        "Создание прервано, проверьте базу."
                    )
                wallets.extend(batch)
                logger.info(f"Создано {min(start + n, count)}/{count} кошельков")
        return wallets

    @staticmethod
    def export_to_file(wallets: list, path: str = "wallets.json") -> str:
        """Экспорт кошельков в JSON (address, private_key, mnemonic)."""
        return export_wallets_to_json(wallets, path)
