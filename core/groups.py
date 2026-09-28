"""Группы кошельков: теги для организации и выборочного фарма.

Группы хранятся в БД (таблица wallet_groups + wallet_group_map).
Кошелёк может принадлежать нескольким группам одновременно.
"""

import logging

import aiosqlite

logger = logging.getLogger(__name__)


class GroupManager:
    """Управление группами кошельков через существующее соединение БД."""

    def __init__(self, db_path: str) -> None:
        self.db_path = db_path
        self._db: aiosqlite.Connection | None = None

    def set_db(self, db: aiosqlite.Connection) -> None:
        self._db = db

    async def ensure_schema(self, db: aiosqlite.Connection) -> None:
        await db.execute(
            """CREATE TABLE IF NOT EXISTS wallet_groups (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT UNIQUE NOT NULL,
                color TEXT DEFAULT '#007bff',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )"""
        )
        await db.execute(
            """CREATE TABLE IF NOT EXISTS wallet_group_map (
                address TEXT NOT NULL,
                group_id INTEGER NOT NULL,
                PRIMARY KEY (address, group_id),
                FOREIGN KEY (group_id) REFERENCES wallet_groups(id) ON DELETE CASCADE
            )"""
        )
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_wgm_group ON wallet_group_map(group_id)"
        )

    async def create_group(self, name: str, color: str = "#007bff") -> dict:
        if not self._db:
            raise RuntimeError("GroupManager: DB not set")
        try:
            await self._db.execute(
                "INSERT INTO wallet_groups (name, color) VALUES (?, ?)", (name, color)
            )
            await self._db.commit()
            async with self._db.execute(
                "SELECT id, name, color, created_at FROM wallet_groups WHERE name = ?", (name,)
            ) as cur:
                row = await cur.fetchone()
            return {"id": row[0], "name": row[1], "color": row[2], "created_at": row[3], "count": 0}
        except Exception as e:
            if "UNIQUE" in str(e).upper():
                raise ValueError(f"Group '{name}' already exists")
            raise

    async def delete_group(self, group_id: int) -> bool:
        if not self._db:
            raise RuntimeError("GroupManager: DB not set")
        try:
            await self._db.execute("DELETE FROM wallet_group_map WHERE group_id = ?", (group_id,))
            await self._db.execute("DELETE FROM wallet_groups WHERE id = ?", (group_id,))
            await self._db.commit()
            return True
        except Exception as e:
            logger.error("Failed to delete group %d: %s", group_id, e)
            return False

    async def rename_group(self, group_id: int, new_name: str) -> bool:
        if not self._db:
            raise RuntimeError("GroupManager: DB not set")
        try:
            await self._db.execute(
                "UPDATE wallet_groups SET name = ? WHERE id = ?", (new_name, group_id)
            )
            await self._db.commit()
            return True
        except Exception as e:
            logger.error("Failed to rename group %d: %s", group_id, e)
            return False

    async def get_all_groups(self) -> list[dict]:
        if not self._db:
            return []
        try:
            async with self._db.execute(
                """SELECT g.id, g.name, g.color, g.created_at,
                          COUNT(m.address) as cnt
                   FROM wallet_groups g
                   LEFT JOIN wallet_group_map m ON g.id = m.group_id
                   GROUP BY g.id
                   ORDER BY g.name"""
            ) as cur:
                rows = await cur.fetchall()
            return [
                {"id": r[0], "name": r[1], "color": r[2], "created_at": r[3], "count": r[4]}
                for r in rows
            ]
        except Exception as e:
            logger.error("Failed to list groups: %s", e)
            return []

    async def add_wallet_to_group(self, address: str, group_id: int) -> bool:
        if not self._db:
            raise RuntimeError("GroupManager: DB not set")
        try:
            await self._db.execute(
                "INSERT OR IGNORE INTO wallet_group_map (address, group_id) VALUES (?, ?)",
                (address, group_id),
            )
            await self._db.commit()
            return True
        except Exception as e:
            logger.error("Failed to add wallet to group: %s", e)
            return False

    async def remove_wallet_from_group(self, address: str, group_id: int) -> bool:
        if not self._db:
            raise RuntimeError("GroupManager: DB not set")
        try:
            await self._db.execute(
                "DELETE FROM wallet_group_map WHERE address = ? AND group_id = ?",
                (address, group_id),
            )
            await self._db.commit()
            return True
        except Exception as e:
            logger.error("Failed to remove wallet from group: %s", e)
            return False

    async def get_wallet_groups(self, address: str) -> list[dict]:
        if not self._db:
            return []
        try:
            async with self._db.execute(
                """SELECT g.id, g.name, g.color
                   FROM wallet_groups g
                   JOIN wallet_group_map m ON g.id = m.group_id
                   WHERE m.address = ?
                   ORDER BY g.name""",
                (address,),
            ) as cur:
                rows = await cur.fetchall()
            return [{"id": r[0], "name": r[1], "color": r[2]} for r in rows]
        except Exception as e:
            logger.error("Failed to get wallet groups: %s", e)
            return []

    async def get_group_addresses(self, group_id: int) -> list[str]:
        if not self._db:
            return []
        try:
            async with self._db.execute(
                "SELECT address FROM wallet_group_map WHERE group_id = ?", (group_id,)
            ) as cur:
                rows = await cur.fetchall()
            return [r[0] for r in rows]
        except Exception as e:
            logger.error("Failed to get group addresses: %s", e)
            return []

    async def add_bulk_to_group(self, addresses: list[str], group_id: int) -> int:
        if not self._db:
            raise RuntimeError("GroupManager: DB not set")
        if not addresses:
            return 0
        try:
            rows = [(addr, group_id) for addr in addresses]
            await self._db.executemany(
                "INSERT OR IGNORE INTO wallet_group_map (address, group_id) VALUES (?, ?)",
                rows,
            )
            await self._db.commit()
            return len(addresses)
        except Exception as e:
            logger.error("Failed bulk add to group: %s", e)
            return 0
