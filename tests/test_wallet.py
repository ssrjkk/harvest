"""Тесты WalletManager: отказ БД не должен молча терять приватные ключи."""

import asyncio
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.wallet import WalletManager


class _SaveFailDb:
    """БД, у которой батч-запись всегда падает (имитация ошибки записи)."""

    def __init__(self) -> None:
        self.calls = 0
        self.rows: list[tuple] = []

    async def save_wallets_batch(self, rows: list[tuple]) -> bool:
        self.calls += 1
        self.rows.extend(rows)
        return False


class _SaveOkDb:
    def __init__(self) -> None:
        self.calls = 0
        self.rows: list[tuple] = []

    async def save_wallets_batch(self, rows: list[tuple]) -> bool:
        self.calls += 1
        self.rows.extend(rows)
        return True


class TestWalletBatchFailure(unittest.TestCase):
    def test_failed_batch_raises_and_stops(self):
        # Раунд 14: раньше ошибка БД глоталась, кошельки «появлялись» в памяти
        # оператора, но после рестарта исчезали (silent data loss).
        cfg = {"threading": {"gen_workers": 1}}
        db = _SaveFailDb()
        wm = WalletManager(cfg, db)  # type: ignore[arg-type]
        with self.assertRaises(RuntimeError) as ctx:
            asyncio.run(wm.create_wallets(3))
        self.assertIn("не записались", str(ctx.exception))
        self.assertEqual(db.calls, 1, "должен остановиться на первом же упавшем батче")

    def test_success_returns_all_wallets(self):
        cfg = {"threading": {"gen_workers": 1}}
        db = _SaveOkDb()
        wm = WalletManager(cfg, db)  # type: ignore[arg-type]
        wallets = asyncio.run(wm.create_wallets(3))
        self.assertEqual(len(wallets), 3)
        self.assertEqual(db.calls, 1)
        self.assertEqual(len(db.rows), 3)
        # Приватные ключи дошли до БД (в зашифрованном виде хранилище само).
        self.assertTrue(all(len(r) == 3 for r in db.rows))


if __name__ == "__main__":
    unittest.main(verbosity=2)
