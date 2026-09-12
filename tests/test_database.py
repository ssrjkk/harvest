"""Тесты БД: инициализация, сохранение/чтение кошельков (шифрование), счётчики, бэкапы."""

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

from core.database import Database, _redact_rpc_url


def make_keyfile(td: Path) -> bytes:
    key = bytes(range(32))
    (td / "master.key").write_bytes(key)
    return key


class TestDatabase(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.dir = Path(self.td.name)
        self.key = make_keyfile(self.dir)
        self.db = Database(str(self.dir / "state.db"), master_key=self.key)

    async def asyncTearDown(self):
        await self.db.close()

    def tearDown(self):
        self.td.cleanup()

    async def test_init_and_stats(self):
        await self.db.init()
        st = await self.db.get_stats()
        self.assertEqual(st["total_wallets"], 0)
        await self.db.close()

    async def test_wallet_roundtrip(self):
        if not HAS_ETH:
            self.skipTest("eth_account не установлена")
        await self.db.init()
        acc = Account.create()
        await self.db.save_wallets_batch(
            [
                (
                    acc.address,
                    acc.key.hex(),
                    "seed one two three",
                )
            ]
        )
        wallets = await self.db.get_all_wallets()
        self.assertEqual(len(wallets), 1)
        self.assertEqual(wallets[0]["address"], acc.address)
        self.assertEqual(wallets[0]["private_key"], acc.key.hex())
        self.assertEqual(wallets[0]["mnemonic"], "seed one two three")
        await self.db.close()

    async def test_health_update_and_stats(self):
        if not HAS_ETH:
            self.skipTest("eth_account не установлена")
        await self.db.init()
        acc = Account.create()
        await self.db.save_wallets_batch(
            [
                (
                    acc.address,
                    acc.key.hex(),
                    "",
                )
            ]
        )
        await self.db.update_wallet_health_batch({acc.address: (3, 2)})
        st = await self.db.get_stats()
        # health-счётчик увеличивает total_actions (успеш = 2 из 3 попыток)
        self.assertEqual(st["total_actions"], 2)
        await self.db.close()

    async def test_backup_cycle(self):
        if not HAS_ETH:
            self.skipTest("eth_account не установлена")
        await self.db.init()
        acc = Account.create()
        await self.db.save_wallets_batch(
            [
                (
                    acc.address,
                    acc.key.hex(),
                    "",
                )
            ]
        )
        await self.db.backup_now()
        backups = self.db.list_backups()
        self.assertEqual(len(backups), 1)
        self.assertTrue(await self.db.restore_backup(0))
        self.assertTrue(await self.db.delete_backup(0))
        backups = self.db.list_backups()
        self.assertEqual(len(backups), 0)
        await self.db.close()

    async def test_wrong_key_fails_closed(self):
        # Чтение БД ключом, не подходящим к данным, происходит fail-closed:
        # кошельки не расшифровываются и пропускаются.
        if not HAS_ETH:
            self.skipTest("eth_account не установлена")
        await self.db.init()
        acc = Account.create()
        await self.db.save_wallets_batch(
            [
                (
                    acc.address,
                    acc.key.hex(),
                    "",
                )
            ]
        )
        await self.db.close()
        bad = Database(str(self.dir / "state.db"), master_key=bytes(range(1, 33)))
        wallets = await bad.get_all_wallets()
        self.assertEqual(len(wallets), 0)
        await bad.close()

    async def test_cycle_history_record_and_read(self):
        await self.db.init()
        await self.db.record_cycle(
            mode="cycle",
            wallets=10,
            wallets_ok=8,
            actions_ok=33,
            errors=2,
            duration_s=12.5,
            rpc_url="http://rpc.test",
            rpc_calls=312,
            rpc_errors=7,
            rpc_latency_ms=180,
        )
        hist = await self.db.get_cycle_history(5)
        self.assertEqual(len(hist), 1)
        self.assertEqual(hist[0]["actions_ok"], 33)
        self.assertEqual(hist[0]["wallets"], 10)
        self.assertEqual(hist[0]["errors"], 2)
        self.assertEqual(hist[0]["rpc_url"], "http://rpc.test")
        self.assertEqual(hist[0]["rpc_calls"], 312)
        self.assertEqual(hist[0]["rpc_errors"], 7)
        self.assertEqual(hist[0]["rpc_latency_ms"], 180)
        st = await self.db.get_cycle_stats()
        self.assertEqual(st["cycles"], 1)
        self.assertEqual(st["wallets"], 10)
        self.assertEqual(st["actions"], 33)
        await self.db.close()

    async def test_cycle_history_telemetry_defaults(self):
        """Запись без телеметрии (старые вызовы) даёт нули, не ломая чтение."""
        await self.db.init()
        await self.db.record_cycle(wallets=3, actions_ok=5)
        hist = await self.db.get_cycle_history(5)
        self.assertEqual(hist[0]["rpc_calls"], 0)
        self.assertEqual(hist[0]["rpc_errors"], 0)
        self.assertEqual(hist[0]["rpc_latency_ms"], 0)
        await self.db.close()

    async def test_cycle_history_migrates_old_schema(self):
        """БД со старой схемой (без колонок телеметрии) -> _ensure_schema добавляет их."""
        db_conn = await self.db._connect()
        await db_conn.execute(
            """
            CREATE TABLE IF NOT EXISTS cycle_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                started_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                mode TEXT NOT NULL DEFAULT 'cycle',
                duration_s REAL DEFAULT 0,
                wallets INTEGER DEFAULT 0,
                wallets_ok INTEGER DEFAULT 0,
                actions_ok INTEGER DEFAULT 0,
                errors INTEGER DEFAULT 0,
                rpc_url TEXT DEFAULT ''
            )
            """
        )
        await db_conn.commit()
        await self.db._ensure_schema(db_conn)
        await db_conn.commit()
        async with db_conn.execute("PRAGMA table_info(cycle_history)") as cursor:
            cols = {row[1] for row in await cursor.fetchall()}
        for col in ("rpc_calls", "rpc_errors", "rpc_latency_ms"):
            self.assertIn(col, cols)
        await self.db.close()

    async def test_cycle_history_prune_keeps_latest(self):
        await self.db.init()
        for i in range(7):
            await self.db.record_cycle(wallets=i, actions_ok=i, keep=5)
        hist = await self.db.get_cycle_history(50)
        self.assertEqual(len(hist), 5)
        # Свежие первыми: последний записанный цикл с actions_ok=6
        self.assertEqual(hist[0]["actions_ok"], 6)
        st = await self.db.get_cycle_stats()
        self.assertEqual(st["cycles"], 5)
        await self.db.close()

    async def test_cycle_history_empty(self):
        await self.db.init()
        self.assertEqual(await self.db.get_cycle_history(10), [])
        st = await self.db.get_cycle_stats()
        self.assertEqual(st["cycles"], 0)
        await self.db.close()

    async def test_top_wallets_no_keys(self):
        await self.db.init()
        await self.db.save_wallets_batch([("aa1", "k1", "m1"), ("aa2", "k2", "m2"), ("aa3", "k3", "m3")])
        await self.db.update_wallet_health_batch({"aa1": (1, 5), "aa2": (1, 2), "aa3": (1, 7)})
        top = await self.db.get_top_wallets(2)
        self.assertEqual([w["address"] for w in top], ["aa3", "aa1"])
        self.assertEqual(len(top), 2)
        self.assertNotIn("private_key", top[0])
        all_top = await self.db.get_top_wallets()
        self.assertEqual(len(all_top), 3)
        self.assertEqual(all_top[-1]["total_actions"], 2)
        await self.db.close()

    async def test_all_addresses_no_key_decrypt(self):
        await self.db.init()
        await self.db.save_wallets_batch([("aa1", "k1", "m1"), ("aa2", "k2", "m2"), ("aa3", "k3", "m3")])
        addrs = await self.db.get_all_addresses()
        self.assertEqual(addrs, ["aa1", "aa2", "aa3"])
        limited = await self.db.get_all_addresses(limit=2)
        self.assertEqual(len(limited), 2)
        await self.db.close()

    async def test_any_seed_encrypted_states(self):
        # БД без master-ключа хранит seed-значения как есть
        plain_db = Database(str(self.dir / "plain.db"))
        await plain_db.init()
        await plain_db.save_wallets_batch([("aa1", "k1", "m1")])
        self.assertFalse(await plain_db.any_seed_encrypted())
        await plain_db.close()
        # с ключом — значения получают префикс шифрования
        await self.db.init()
        await self.db.save_wallets_batch([("aa2", "k2", "m2")])
        self.assertTrue(await self.db.any_seed_encrypted())

    async def test_cycle_history_redacts_rpc_url(self):
        # RPC URL с API-ключом/credentials не должен попадать в БД и историю.
        await self.db.init()
        dirty = "https://user:pass@rpc.example/rpc/v1?api_key=SECRET&foo=bar#frag"
        await self.db.record_cycle(wallets=1, actions_ok=1, rpc_url=dirty, keep=3)
        hist = await self.db.get_cycle_history(5)
        self.assertEqual(hist[0]["rpc_url"], "https://rpc.example/rpc/v1")
        self.assertNotIn("SECRET", hist[0]["rpc_url"])
        self.assertNotIn("pass", hist[0]["rpc_url"])
        await self.db.close()


class TestRedactRpcUrl(unittest.TestCase):
    def test_query_and_credentials_stripped(self):
        self.assertEqual(
            _redact_rpc_url("https://user:pass@rpc.example/x?api_key=abc&d=1#frag"),
            "https://rpc.example/x",
        )

    def test_plain_url_unchanged(self):
        url = "https://rpc.example/rpc"
        self.assertEqual(_redact_rpc_url(url), url)

    def test_http_loopback_unchanged(self):
        url = "http://127.0.0.1:8545"
        self.assertEqual(_redact_rpc_url(url), url)

    def test_empty(self):
        self.assertEqual(_redact_rpc_url(""), "")
        self.assertIsNone(_redact_rpc_url(None))

    def test_list_redacted_elementwise(self):
        urls = [
            "https://user:pass@rpc.example/x?api_key=abc",
            "http://127.0.0.1:8545",
        ]
        out = _redact_rpc_url(urls)
        assert isinstance(out, list)
        self.assertEqual(out, ["https://rpc.example/x", "http://127.0.0.1:8545"])
        self.assertNotIn("abc", " ".join(out))
        self.assertNotIn("user", " ".join(out))


if __name__ == "__main__":
    unittest.main(verbosity=2)
