"""Тесты параллельного импорта кошельков (CPU-bound вывод адреса по ядрам)."""

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

try:
    from eth_account import Account

    HAS_ETH = True
except ImportError:
    HAS_ETH = False

import main as main_mod
from core.database import Database


def _cfg(wallets_file: str) -> dict:
    return {"wallets": {"file": wallets_file}, "threading": {"max_workers": 4, "gen_workers": 4}}


class TestImportWallets(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.dir = Path(self.td.name)
        self.db = Database(str(self.dir / "state.db"))

    async def asyncTearDown(self):
        await self.db.close()

    def tearDown(self):
        self.td.cleanup()

    async def test_import_valid_wallets(self):
        if not HAS_ETH:
            self.skipTest("eth_account не установлена")
        await self.db.init()
        acc1 = Account.create()
        acc2 = Account.create()
        wf = self.dir / "wallets.json"
        wf.write_text(
            json.dumps(
                [
                    {"address": acc1.address, "private_key": acc1.key.hex(), "mnemonic": "m1"},
                    {"address": acc2.address, "private_key": acc2.key.hex(), "mnemonic": "m2"},
                ]
            ),
            encoding="utf-8",
        )
        await main_mod.import_wallets(_cfg(str(wf)), self.db)
        wallets = await self.db.get_all_wallets()
        self.assertEqual(len(wallets), 2)
        addrs = {w["address"].lower() for w in wallets}
        self.assertEqual(addrs, {acc1.address.lower(), acc2.address.lower()})

    async def test_import_skips_invalid_and_dups(self):
        if not HAS_ETH:
            self.skipTest("eth_account не установлена")
        await self.db.init()
        acc = Account.create()
        wf = self.dir / "wallets.json"
        wf.write_text(
            json.dumps(
                [
                    {"address": acc.address, "private_key": acc.key.hex()},
                    {"address": acc.address, "private_key": acc.key.hex()},  # дубль
                    # ключ валиден, но адрес подменён — не соответствует ключу
                    {"address": "0x" + "1" * 40, "private_key": acc.key.hex()},
                    {"address": "0x2222", "private_key": "0x1234"},  # мусор
                    {"private_key": "0x" + "1" * 64},  # нет адреса
                ]
            ),
            encoding="utf-8",
        )
        await main_mod.import_wallets(_cfg(str(wf)), self.db)
        wallets = await self.db.get_all_wallets()
        self.assertEqual(len(wallets), 1)
        self.assertEqual(wallets[0]["address"].lower(), acc.address.lower())

    async def test_import_file_missing(self):
        await main_mod.import_wallets(_cfg(str(self.dir / "nope.json")), self.db)
        self.assertEqual(await self.db.get_all_wallets(), [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
