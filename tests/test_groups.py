"""Тесты GroupManager: группы кошельков, теги, bulk-операции."""

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import aiosqlite

from core.groups import GroupManager


class TestGroupManager(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self.td.name) / "test.db")
        self.db = await aiosqlite.connect(self.db_path)
        self.gm = GroupManager(self.db_path)
        self.gm.set_db(self.db)
        await self.gm.ensure_schema(self.db)

    async def asyncTearDown(self):
        await self.db.close()
        self.td.cleanup()

    async def test_create_group(self):
        group = await self.gm.create_group("Test Group", "#ff0000")
        self.assertEqual(group["name"], "Test Group")
        self.assertEqual(group["color"], "#ff0000")
        self.assertEqual(group["count"], 0)
        self.assertIn("id", group)

    async def test_create_duplicate_group_raises(self):
        await self.gm.create_group("Duplicate")
        with self.assertRaises(ValueError) as ctx:
            await self.gm.create_group("Duplicate")
        self.assertIn("already exists", str(ctx.exception))

    async def test_delete_group(self):
        group = await self.gm.create_group("ToDelete")
        group_id = group["id"]
        result = await self.gm.delete_group(group_id)
        self.assertTrue(result)
        groups = await self.gm.get_all_groups()
        self.assertEqual(len(groups), 0)

    async def test_rename_group(self):
        group = await self.gm.create_group("OldName")
        result = await self.gm.rename_group(group["id"], "NewName")
        self.assertTrue(result)
        groups = await self.gm.get_all_groups()
        self.assertEqual(groups[0]["name"], "NewName")

    async def test_get_all_groups_with_count(self):
        g1 = await self.gm.create_group("Group1")
        g2 = await self.gm.create_group("Group2")
        await self.gm.add_wallet_to_group("0x111", g1["id"])
        await self.gm.add_wallet_to_group("0x222", g1["id"])
        await self.gm.add_wallet_to_group("0x333", g2["id"])
        groups = await self.gm.get_all_groups()
        self.assertEqual(len(groups), 2)
        g1_data = next(g for g in groups if g["name"] == "Group1")
        g2_data = next(g for g in groups if g["name"] == "Group2")
        self.assertEqual(g1_data["count"], 2)
        self.assertEqual(g2_data["count"], 1)

    async def test_add_wallet_to_group(self):
        group = await self.gm.create_group("TestGroup")
        result = await self.gm.add_wallet_to_group("0xabc", group["id"])
        self.assertTrue(result)
        addresses = await self.gm.get_group_addresses(group["id"])
        self.assertIn("0xabc", addresses)

    async def test_add_wallet_duplicate_ignored(self):
        group = await self.gm.create_group("TestGroup")
        await self.gm.add_wallet_to_group("0xabc", group["id"])
        result = await self.gm.add_wallet_to_group("0xabc", group["id"])
        self.assertTrue(result)
        addresses = await self.gm.get_group_addresses(group["id"])
        self.assertEqual(len(addresses), 1)

    async def test_remove_wallet_from_group(self):
        group = await self.gm.create_group("TestGroup")
        await self.gm.add_wallet_to_group("0xabc", group["id"])
        result = await self.gm.remove_wallet_from_group("0xabc", group["id"])
        self.assertTrue(result)
        addresses = await self.gm.get_group_addresses(group["id"])
        self.assertNotIn("0xabc", addresses)

    async def test_get_wallet_groups(self):
        g1 = await self.gm.create_group("Group1")
        g2 = await self.gm.create_group("Group2")
        await self.gm.add_wallet_to_group("0xwallet", g1["id"])
        await self.gm.add_wallet_to_group("0xwallet", g2["id"])
        groups = await self.gm.get_wallet_groups("0xwallet")
        self.assertEqual(len(groups), 2)
        names = {g["name"] for g in groups}
        self.assertEqual(names, {"Group1", "Group2"})

    async def test_get_group_addresses_empty(self):
        group = await self.gm.create_group("Empty")
        addresses = await self.gm.get_group_addresses(group["id"])
        self.assertEqual(addresses, [])

    async def test_add_bulk_to_group(self):
        group = await self.gm.create_group("Bulk")
        addresses = ["0x1", "0x2", "0x3", "0x4"]
        count = await self.gm.add_bulk_to_group(addresses, group["id"])
        self.assertEqual(count, 4)
        stored = await self.gm.get_group_addresses(group["id"])
        self.assertEqual(len(stored), 4)

    async def test_add_bulk_empty_list(self):
        group = await self.gm.create_group("Bulk")
        count = await self.gm.add_bulk_to_group([], group["id"])
        self.assertEqual(count, 0)

    async def test_no_db_raises(self):
        gm = GroupManager("dummy.db")
        with self.assertRaises(RuntimeError):
            await gm.create_group("Test")
        with self.assertRaises(RuntimeError):
            await gm.delete_group(1)
        with self.assertRaises(RuntimeError):
            await gm.rename_group(1, "New")
        with self.assertRaises(RuntimeError):
            await gm.add_wallet_to_group("0x1", 1)
        with self.assertRaises(RuntimeError):
            await gm.remove_wallet_from_group("0x1", 1)
        with self.assertRaises(RuntimeError):
            await gm.add_bulk_to_group(["0x1"], 1)

    async def test_no_db_returns_empty(self):
        gm = GroupManager("dummy.db")
        self.assertEqual(await gm.get_all_groups(), [])
        self.assertEqual(await gm.get_wallet_groups("0x1"), [])
        self.assertEqual(await gm.get_group_addresses(1), [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
