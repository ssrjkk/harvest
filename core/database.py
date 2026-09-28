"""SQLite-хранилище состояния кошельков и логов действий (aiosqlite).

Persistent connection + WAL mode для максимальной скорости при 500+ кошельках.
"""

import asyncio
import logging
import os
import shutil
import sqlite3
from pathlib import Path
from typing import overload

import aiosqlite

from core.crypto import _ENC_PREFIX
from core.crypto import decrypt_seed as _decrypt_seed
from core.crypto import encrypt_seed as _encrypt_seed
from core.utils import restrict_file_permissions

logger = logging.getLogger(__name__)


def _restrict_backup_dir(directory: Path) -> None:
    """Каталог бэкапов — только владелец (Unix: 0700), чтобы другие локальные
    пользователи не могли читать его содержимое/наличие шифротекста."""
    if os.name != "nt":
        try:
            directory.chmod(0o700)
        except OSError as e:
            logger.warning(f"Не удалось ограничить права каталога бэкапов {directory}: {e}")


# WAL mode + optimized pragma
_PRAGMAS = "PRAGMA journal_mode=WAL; PRAGMA synchronous=NORMAL; PRAGMA cache_size=-64000; PRAGMA busy_timeout=5000;"


def _redact_one(url: str) -> str:
    if not url:
        return url
    try:
        from urllib.parse import urlsplit, urlunsplit

        parts = urlsplit(url)
    except Exception:
        return url.split("?", 1)[0]
    netloc = parts.netloc.rsplit("@", 1)[-1]
    return urlunsplit((parts.scheme, netloc, parts.path, "", ""))


@overload
def _redact_rpc_url(url: list[str]) -> list[str]: ...


@overload
def _redact_rpc_url(url: str | None) -> str | None: ...


def _redact_rpc_url(url: str | list[str] | None) -> str | list[str] | None:
    """Очищает RPC URL от credentials и query-string перед хранением/выводом.

    Публичные RPC-эндпоинты часто получают ключ через query (?api_key=...);
        перситить или выводить такой URL (CLI --history, веб cycle-history) нельзя.
        Схемы и хостов достаточно для диагностики. Список rpc_url обрабатывается
        поэлементно (конфиг сети допускает несколько эндпоинтов).
    """
    if isinstance(url, list):
        return [_redact_one(u) for u in url]
    return _redact_one(url) if url is not None else None


class Database:
    def __init__(self, path: str = "farming_state.db", master_key: bytes | None = None) -> None:
        self.path = path
        self._key = master_key
        self._db: aiosqlite.Connection | None = None
        self._connect_lock = asyncio.Lock()

    async def _connect(self) -> aiosqlite.Connection:
        if self._db is None:
            async with self._connect_lock:
                if self._db is None:
                    self._db = await aiosqlite.connect(self.path)
                    await self._db.executescript(_PRAGMAS)
        return self._db

    async def close(self) -> None:
        if self._db:
            try:
                await self._db.close()
            except Exception:
                pass
            self._db = None

    def _encrypt(self, value: str | None) -> str | None:
        """Шифрует seed-строку текущим master-ключом (без ключа — как есть)."""
        return _encrypt_seed(self._key, value)

    def _decrypt(self, value: str | None) -> str | None:
        """Расшифровывает seed-строку. Raises при неверном ключе/повреждении."""
        return _decrypt_seed(self._key, value)

    async def _backup_if_needed(self) -> None:
        """Создаёт бэкап БД перед schema changes (если файл существует и >0 байт).

        Бэкапы хранятся в .backup/ рядом с БД, максимум 3 последних.
        Предотвращает потерю данных при corrupted WAL/main файле.
        Консистентный снимок делается через SQLite backup API — в отличие от
        copy2, он не даёт torn-копию при живой записи и учитывает WAL.
        Синхронные файловые операции выполняются через executor.
        """
        db_path = Path(self.path)
        if not db_path.exists():
            return
        backup_dir = db_path.parent / ".backup"
        backup_dir.mkdir(exist_ok=True)
        _restrict_backup_dir(backup_dir)
        backup_name = f"{db_path.name}.bak"
        backup_path = backup_dir / backup_name

        def _snapshot() -> bool:
            tmp_path = backup_path.with_suffix(".tmp")
            try:
                src = sqlite3.connect(self.path, timeout=5)
                dst = sqlite3.connect(tmp_path)
                try:
                    src.backup(dst)
                finally:
                    dst.close()
                    src.close()
                tmp_path.replace(backup_path)
            except Exception as e:
                logger.warning(f"DB backup failed: {e}")
                try:
                    tmp_path.unlink()
                except OSError:
                    pass
                return False
            return True

        def _rotate() -> None:
            # Ротация: максимум 3 бэкапов (.bak.1, .bak.2, .bak.3)
            for i in range(2, 0, -1):
                src = backup_dir / f"{backup_name}.{i}"
                dst = backup_dir / f"{backup_name}.{i + 1}"
                if src.exists():
                    if dst.exists():
                        dst.unlink()
                    shutil.move(str(src), str(dst))
            if backup_path.exists():
                rotated = backup_dir / f"{backup_name}.1"
                if rotated.exists():
                    rotated.unlink()
                shutil.move(str(backup_path), str(rotated))

        def _do_backup() -> None:
            if _snapshot():
                _rotate()

        await asyncio.to_thread(_do_backup)

    async def _recover_corrupted_db(self) -> None:
        """Восстанавливает БД из бэкапа при integrity_check failure.

        Закрывает текущее соединение, заменяет файл из бэкапа,
        переоткрывает. Если бэкапа нет — пересоздаёт чистую БД.
        """
        logger.warning("Attempting DB recovery from backup...")
        await self.close()
        backup_dir = Path(self.path).parent / ".backup"
        backup_path = backup_dir / f"{Path(self.path).name}.bak.1"
        db_path = Path(self.path)
        if backup_path.exists():
            try:
                shutil.copy2(backup_path, db_path)
                # Убираем stale WAL/SHM, чтобы они не «доиграли» поверх восстановленного файла
                for suffix in ("-wal", "-shm"):
                    p = db_path.with_suffix(db_path.suffix + suffix)
                    if p.exists():
                        p.unlink()
                logger.info("DB restored from backup")
                return
            except Exception as e:
                logger.error(f"Backup restore failed: {e}")
        # Нет бэкапа — удаляем corrupted файл, пересоздадим чистый
        try:
            db_path.unlink()
            for suffix in ("-wal", "-shm"):
                p = db_path.with_suffix(db_path.suffix + suffix)
                if p.exists():
                    p.unlink()
            logger.warning("Corrupted DB deleted, will recreate clean")
        except Exception as e:
            logger.error(f"Failed to remove corrupted DB: {e}")

    async def _maybe_vacuum(self) -> None:
        """VACUUM если файл БД раздулся (>2x от nominal size).

        Nominal size = количество_кошельков * ~350 байт + actions_log overhead.
        При 10k кошельков: ~3.5MB nominal, vacuum если >7MB.
        """
        db_path = Path(self.path)
        if not db_path.exists():
            return
        size_mb = db_path.stat().st_size / (1024 * 1024)
        # Порог: 5MB или если файл >2x от ожидаемого
        if size_mb < 5:
            return
        try:
            db = await self._connect()
            async with db.execute("SELECT COUNT(*) FROM wallets") as cursor:
                row = await cursor.fetchone()
            wallet_count = row[0] if row else 0
            expected_bytes = max(wallet_count * 350, 1024 * 1024)  # min 1MB
            actual_bytes = db_path.stat().st_size
            if actual_bytes > expected_bytes * 2:
                logger.info(f"DB vacuum: {size_mb:.1f}MB > {expected_bytes / 1024 / 1024:.1f}MB expected")
                await db.execute("VACUUM")
                new_size = db_path.stat().st_size / (1024 * 1024)
                logger.info(f"DB vacuum done: {size_mb:.1f}MB -> {new_size:.1f}MB")
        except Exception as e:
            msg = str(e).lower()
            # VACUUM при активных ридерах штатно недоступен ("database is locked") —
            # это не ошибка, а отложенный бэкграунд-джоб.
            if "locked" in msg or "busy" in msg:
                logger.debug(f"DB vacuum deferred: {e}")
            else:
                logger.warning(f"DB vacuum failed: {e}")

    async def init(self) -> None:
        parent = Path(self.path).parent
        if str(parent) != ".":
            parent.mkdir(parents=True, exist_ok=True)
        # Бэкап перед schema changes: при corrupted WAL/main файле
        # бэкап спасает данные от предыдущих сессий.
        await self._backup_if_needed()
        db = await self._connect()
        await self._ensure_schema(db)
        await db.commit()
        # WAL checkpoint: сбрасывает WAL-журнал в основной файл БД,
        # предотвращая бесконечный рост .db-wal при долгом run_forever.
        try:
            async with db.execute("PRAGMA wal_checkpoint(TRUNCATE)") as cursor:
                await cursor.fetchall()
        except Exception:
            pass
        # Integrity check: ловит corrupted БД до того, как начнётся работа.
        # При corrupted WAL/main файле — пересоздаём из бэкапа или чисто.
        try:
            async with db.execute("PRAGMA integrity_check") as cursor:
                row = await cursor.fetchone()
            if row and row[0] != "ok":
                logger.error(f"DB integrity check failed: {row[0]}")
                await self._recover_corrupted_db()
                # Восстановленная файловая БД нуждается в схеме заново
                db = await self._connect()
                await self._ensure_schema(db)
                await db.commit()
        except Exception as e:
            logger.error(f"DB integrity check error: {e}")
        # Одноразовое шифрование seed-данных, оставшихся от старых версий
        if await self._migrate_legacy_seeds():
            # Старый открытый текст остаётся в freed pages — пересобираем файл,
            # чтобы действительно затереть его, и сбрасываем WAL.
            try:
                async with db.execute("PRAGMA wal_checkpoint(TRUNCATE)") as cursor:
                    await cursor.fetchall()
                await db.execute("VACUUM")
            except Exception as e:
                logger.warning(f"Очистка БД после миграции шифрования не выполнена: {e}")
            # Бэкапы были скопированы ДО миграции и содержат открытые ключи:
            # удаляем их и создаём свежие (уже зашифрованные).
            self._purge_backups()
            await self._backup_if_needed()
        # Vacuum: убирает freed pages, сжимает файл.
        await self._maybe_vacuum()
        # Ограничение прав на файлы БД (и бэкапы) — только владелец
        await self._restrict_db_files()

    async def _ensure_schema(self, db) -> None:
        """Создаёт таблицы (и применяет миграции колонок), если их ещё нет."""
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS wallets (
                address TEXT PRIMARY KEY,
                private_key TEXT NOT NULL,
                mnemonic TEXT,
                total_actions INTEGER DEFAULT 0,
                total_attempts INTEGER DEFAULT 0,
                last_cycle INTEGER,
                last_action TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        # Добавляем колонку total_attempts если её нет (миграция)
        try:
            await db.execute("ALTER TABLE wallets ADD COLUMN total_attempts INTEGER DEFAULT 0")
        except Exception:
            pass  # Колонка уже существует
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS actions_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                address TEXT,
                action_type TEXT,
                tx_hash TEXT,
                success BOOLEAN,
                timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                details TEXT
            )
            """
        )
        # Индекс для быстрой подрезки по address (prune_actions_log)
        await db.execute("CREATE INDEX IF NOT EXISTS idx_actions_log_address ON actions_log(address)")
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS nonces (
                address TEXT PRIMARY KEY,
                current_nonce INTEGER NOT NULL
            )
            """
        )
        await db.execute(
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
        await db.execute("CREATE INDEX IF NOT EXISTS idx_cycle_history_started ON cycle_history(started_at)")
        # Миграция: колонки RPC-телеметрии (для БД, созданных до цикла «адаптивная мощность»)
        async with db.execute("PRAGMA table_info(cycle_history)") as cur:
            existing_cycle_cols = {row[1] for row in await cur.fetchall()}
        for col, ddl in (
            ("rpc_calls", "INTEGER DEFAULT 0"),
            ("rpc_errors", "INTEGER DEFAULT 0"),
            ("rpc_latency_ms", "INTEGER DEFAULT 0"),
        ):
            if col not in existing_cycle_cols:
                await db.execute(f"ALTER TABLE cycle_history ADD COLUMN {col} {ddl}")

        from core.groups import GroupManager

        gm = GroupManager(self.path)
        await gm.ensure_schema(db)
        await db.commit()

    async def _migrate_legacy_seeds(self) -> bool:
        """Одноразовое шифрование seed-данных, оставшихся от старых версий БД.

        Возвращает True, если выполнялись UPDATE (нужно очистить freed pages).
        """
        if self._key is None:
            return False
        try:
            db = await self._connect()
            async with db.execute("SELECT address, private_key, mnemonic FROM wallets") as cursor:
                rows = await cursor.fetchall()
            updates = []
            for addr, pk, mn in rows:
                # Антиповтор: прогон над уже зашифрованными значениями
                # приводил к двойному шифрованию на каждом init() и порче seed-данных.
                enc_pk = self._encrypt(pk) if pk and not pk.startswith(_ENC_PREFIX) else pk
                enc_mn = self._encrypt(mn) if mn and not mn.startswith(_ENC_PREFIX) else mn
                if enc_pk != pk or enc_mn != mn:
                    updates.append(
                        (
                            enc_pk if enc_pk != pk else pk,
                            enc_mn if enc_mn != mn else mn,
                            addr,
                        )
                    )
            if updates:
                await db.executemany(
                    "UPDATE wallets SET private_key=?, mnemonic=? WHERE address=?",
                    updates,
                )
                await db.commit()
                logger.info(f"Seed-данные: зашифровано {len(updates)} кошельков")
                return True
        except Exception as e:
            logger.warning(f"Миграция шифрования seed-данных не выполнена: {e}")
        return False

    async def _restrict_db_files(self) -> None:
        """Ограничивает права на файлы БД, WAL/SHM и бэкапы (только владелец).

        Асинхронный вызов через executor (icacls/chmod) — не блокирует event loop.
        """
        for name in (self.path, f"{self.path}-wal", f"{self.path}-shm"):
            if Path(name).exists():
                restrict_file_permissions(name)
        backup_dir = Path(self.path).parent / ".backup"
        if backup_dir.exists():
            _restrict_backup_dir(backup_dir)
            try:
                for f in backup_dir.glob(f"{Path(self.path).name}.bak*"):
                    restrict_file_permissions(str(f))
            except Exception:
                pass

    def _purge_backups(self) -> None:
        """Удаляет бэкапы текущей БД (используется после миграции шифрования)."""
        backup_dir = Path(self.path).parent / ".backup"
        try:
            for f in backup_dir.glob(f"{Path(self.path).name}.bak*"):
                f.unlink()
        except Exception as e:
            logger.warning(f"Очистка бэкапов не выполнена: {e}")

    async def backup_now(self) -> str | None:
        """Принудительный бэкап БД. Возвращает путь к свежему бэкапу (или None)."""
        try:
            await self._backup_if_needed()
        except Exception as e:
            logger.error(f"Бэкап не выполнен: {e}")
            return None
        # _restrict_db_files не вызывается автоматически после принудительного
        # бэкапа (в отличие от init/restore): сводим права на свежие .bak* сейчас.
        try:
            await self._restrict_db_files()
        except Exception as e:
            logger.warning(f"Ограничение прав на бэкапы не выполнено: {e}")
        backups = self.list_backups()
        return str(backups[0]) if backups else None

    def list_backups(self) -> list[Path]:
        """Список бэкапов (свежий первый). .bak -> .bak.1 -> ..."""
        backup_dir = Path(self.path).parent / ".backup"
        try:
            found = sorted(
                backup_dir.glob(f"{Path(self.path).name}.bak*"),
                key=lambda p: p.stat().st_mtime,
                reverse=True,
            )
        except Exception:
            return []
        return found

    async def restore_backup(self, index: int = 0) -> bool:
        """Восстанавливает БД из бэкапа (index с 0 = свежайший).

        Закрывает соединение, копирует бэкап поверх main, удаляет stale
        WAL/SHM, переоткрывает и пересоздаёт схему при необходимости.
        """
        backups = self.list_backups()
        if not backups:
            logger.warning("Нет бэкапов для восстановления")
            return False
        if index >= len(backups):
            logger.warning(f"Бэкап с индексом {index} не существует (доступно {len(backups)})")
            return False
        src = backups[index]
        db_path = Path(self.path)
        logger.warning(f"Восстановление из {src.name}...")
        await self.close()
        try:
            shutil.copy2(src, db_path)
            for suffix in ("-wal", "-shm"):
                p = db_path.with_suffix(db_path.suffix + suffix)
                if p.exists():
                    p.unlink()
            db = await self._connect()
            await self._ensure_schema(db)
            await db.commit()
            await self._restrict_db_files()
            logger.info(f"БД восстановлена из {src.name}")
            return True
        except Exception as e:
            logger.error(f"Восстановление не удалось: {e}")
            return False

    async def delete_backup(self, index: int) -> bool:
        backups = self.list_backups()
        if index >= len(backups):
            return False
        try:
            backups[index].unlink()
            return True
        except Exception as e:
            logger.error(f"Удаление бэкапа {index} не удалось: {e}")
            return False

    async def save_wallets_batch(self, rows: list[tuple]) -> bool:
        """Массовая вставка кошельков одним транзакционным батчем.

        rows: list of (address, private_key, mnemonic). Значительно быстрее
        поштучных commits при генерации сотен/тысяч кошельков.
        Возвращает True при успехе, False при ошибке записи (батч НЕ сохранён).
        """
        if not rows:
            return True
        # Шифруем seed-данные перед записью (legacy: без ключа — как есть)
        rows = [(addr, self._encrypt(pk), self._encrypt(mn)) for addr, pk, mn in rows]
        db = await self._connect()
        # WAL + synchronous=NORMAL (глобально) коммитит до сброса на диск: при
        # внезапном падении питания могут потеряться последние коммиты. Для
        # приватных ключей свежесозданных кошельков это неприемлемо — этот
        # конкретный коммит форсируем в FULL (fsync до return).
        try:
            await db.execute("PRAGMA synchronous=FULL")
        except Exception:
            pass
        try:
            # UPSERT вместо INSERT OR REPLACE: REPLACE удалял существующую строку
            # и вставлял новую с дефолтами — повторный импорт того же адреса
            # затирал total_actions/total_attempts/last_action/created_at
            # (накопленная статистика и история «исчезали»). Здесь обновляются
            # только seed-поля, счётчики сохраняются.
            await db.executemany(
                """INSERT INTO wallets (address, private_key, mnemonic) VALUES (?, ?, ?)
                   ON CONFLICT(address) DO UPDATE SET
                       private_key = excluded.private_key,
                       mnemonic = excluded.mnemonic""",
                rows,
            )
            await db.commit()
            return True
        except Exception as e:
            logger.error(f"Ошибка батч-вставки кошельков ({len(rows)}): {e}")
            return False
        finally:
            try:
                await db.execute("PRAGMA synchronous=NORMAL")
            except Exception:
                pass

    async def update_wallet_health_batch(self, updates: dict[str, tuple[int, int]]) -> None:
        """Массовое обновление health score: {address: (attempts, successes)}.

        total_attempts += attempts, total_actions += successes.
        Raises: пробрасывает ошибку БД — BatchWriter решает, повторять
        или отбрасывать батч (молчаливый swallow здесь превращал его
        retry/requeue-механику в мёртвый код).
        """
        db = await self._connect()
        try:
            rows = [(a, s, addr) for addr, (a, s) in updates.items()]
            await db.executemany(
                """UPDATE wallets SET
                   total_attempts = total_attempts + ?,
                   total_actions = total_actions + ?,
                   last_action = datetime('now')
                   WHERE address = ?""",
                rows,
            )
            await db.commit()
        except Exception as e:
            logger.error(f"Ошибка батч-обновления health ({len(updates)}): {e}")
            raise

    def get_group_manager(self) -> "GroupManager":
        from core.groups import GroupManager

        gm = GroupManager(self.path)
        return gm

    async def get_addresses_by_group(self, group_id: int) -> list[str]:
        gm = self.get_group_manager()
        db = await self._connect()
        gm.set_db(db)
        return await gm.get_group_addresses(group_id)

    async def get_all_addresses(self, limit: int = 0) -> list[str]:
        """Список адресов БЕЗ дешифровки seed-данных (мониторы/краны/лидерборды).

        Горячие циклы live-консоли не должны трогать (и расшифровывать)
        приватные ключи — им нужны только адреса.
        """
        try:
            db = await self._connect()
            sql = "SELECT address FROM wallets ORDER BY address"
            if limit and limit > 0:
                cursor = await db.execute(f"{sql} LIMIT ?", (int(limit),))
            else:
                cursor = await db.execute(sql)
            rows = await cursor.fetchall()
            return [r[0] for r in rows]
        except Exception as e:
            logger.error(f"Ошибка чтения адресов: {e}")
            return []

    async def any_seed_encrypted(self) -> bool:
        """Есть ли в БД зашифрованные seed-данные (без массовой дешифровки).

        Проверяем ВСЕ строки (а не первую): если первый кошелёк записан
        plaintext'ом после смены master-ключа, а остальные — шифрованные,
        «по первому» было бы False и потеря зашифрованных кошельков прошла бы
        молча (auto.py генерировал бы новые поверх нерасшифровываемых).
        """
        try:
            db = await self._connect()
            async with db.execute(
                "SELECT 1 FROM wallets WHERE private_key LIKE ? LIMIT 1",
                (f"{_ENC_PREFIX}%",),
            ) as cursor:
                row = await cursor.fetchone()
            return row is not None
        except Exception as e:
            logger.error(f"Ошибка проверки шифрования seed-данных: {e}")
            return False

    async def count_wallets(self) -> int:
        """Сырое количество строк в wallets — БЕЗ дешифровки seed-данных.

        Отличается от get_all_wallets(): строки, не расшифровавшиеся текущим
        master-ключом, здесь учитываются. Разница счётчиков — индикатор
        подменённого/неверного ключа (см. автозащиту в auto.py от молчаливой
        генерации кошельков поверх нечитаемых строк).
        """
        try:
            db = await self._connect()
            async with db.execute("SELECT COUNT(*) FROM wallets") as cursor:
                row = await cursor.fetchone()
            return int(row[0]) if row else 0
        except Exception as e:
            logger.error(f"Ошибка подсчёта кошельков: {e}")
            return 0

    async def get_all_wallets(self) -> list[dict]:
        try:
            db = await self._connect()
            cursor = await db.execute("SELECT address, private_key, mnemonic, total_actions FROM wallets")
            rows = await cursor.fetchall()
            wallets = []
            for r in rows:
                try:
                    pk = self._decrypt(r[1])
                except Exception as e:
                    # Ключ не подходит к значению (неверный master-ключ?) — кошелёк неиспользуем
                    logger.error(f"Кошелёк {r[0]} не расшифрован ({e}) — пропуск")
                    continue
                mn = None
                if r[2]:
                    try:
                        mn = self._decrypt(r[2])
                    except Exception as e:
                        # private_key расшифровался, значит ключ верный — это порча строки mnemonic
                        logger.warning(f"Кошелёк {r[0]}: mnemonic не расшифрован ({e}) — экспорт без сид-фразы")
                        mn = ""
                wallets.append(
                    {
                        "address": r[0],
                        "private_key": pk,
                        "mnemonic": mn,
                        "total_actions": r[3],
                    }
                )
            return wallets
        except Exception as e:
            logger.error(f"Ошибка чтения кошельков из БД: {e}")
            return []

    async def log_actions_batch(self, rows: list[tuple]) -> None:
        """Массовая запись логов действий одним батчем.

        rows: list of (address, action_type, tx_hash, success:int, details).
        Raises: пробрасывает ошибку БД — BatchWriter сам решает, повторять
        или отбрасывать батч (см. update_wallet_health_batch).
        """
        if not rows:
            return
        db = await self._connect()
        try:
            await db.executemany(
                "INSERT INTO actions_log (address, action_type, tx_hash, success, details) VALUES (?, ?, ?, ?, ?)",
                rows,
            )
            await db.commit()
        except Exception as e:
            logger.error(f"Ошибка батч-записи логов ({len(rows)}): {e}")
            raise

    async def get_nonce(self, address: str) -> int | None:
        try:
            db = await self._connect()
            cursor = await db.execute("SELECT current_nonce FROM nonces WHERE address = ?", (address,))
            row = await cursor.fetchone()
            return row[0] if row else None
        except Exception as e:
            logger.error(f"Ошибка чтения nonce для {address[:10]}: {e}")
            return None

    async def set_nonce(self, address: str, nonce: int) -> None:
        try:
            db = await self._connect()
            await db.execute(
                "INSERT OR REPLACE INTO nonces (address, current_nonce) VALUES (?, ?)",
                (address, nonce),
            )
            await db.commit()
        except Exception as e:
            logger.error(f"Ошибка записи nonce для {address[:10]}: {e}")

    async def prune_actions_log(self, keep_latest: int = 200000) -> None:
        """Подрезает actions_log, оставляя последние keep_latest записей.

        keep_latest <= 0 — подрезка отключена: оператор скорее хотел НЕ трогать
        лог, чем стереть его целиком (раньше LIMIT 0 в NOT IN давал полную
        чистку без предупреждения).

        Предотвращает бесконечный рост БД при долгом run_forever с сотнями
        кошельков (в лог пишется каждая транзакция).
        """
        if int(keep_latest) <= 0:
            return
        try:
            db = await self._connect()
            await db.execute(
                "DELETE FROM actions_log WHERE id NOT IN (SELECT id FROM actions_log ORDER BY id DESC LIMIT ?)",
                (int(keep_latest),),
            )
            await db.commit()
        except Exception as e:
            logger.error(f"Ошибка подрезки лога действий: {e}")

    async def get_top_wallets(self, limit: int = 10) -> list[dict]:
        """Топ кошельков по total_actions — без загрузки и дешифровки ключей.

        Лидерборд в UI не должен трогать seed-данные (приватные ключи в RAM):
        address и счётчик достаются одним запросом.
        """
        rows: list[dict] = []
        try:
            db = await self._connect()
            async with db.execute(
                "SELECT address, total_actions FROM wallets ORDER BY total_actions DESC, address LIMIT ?",
                (int(max(limit, 1)),),
            ) as cursor:
                for row in await cursor.fetchall():
                    rows.append({"address": row[0], "total_actions": int(row[1] or 0)})
        except Exception as e:
            logger.error(f"Ошибка чтения топа кошельков: {e}")
        return rows

    async def get_stats(self) -> dict:
        try:
            db = await self._connect()
            # Один round-trip вместо четырёх отдельных запросов.
            async with db.execute(
                """
                SELECT
                    (SELECT COUNT(*) FROM wallets),
                    (SELECT COALESCE(SUM(total_actions), 0) FROM wallets),
                    (SELECT COUNT(*) FROM actions_log WHERE success = 1),
                    (SELECT COUNT(*) FROM actions_log)
                """
            ) as c:
                row = await c.fetchone()
            total_wallets, total_actions, success_actions, total_log_entries = row
            return {
                "total_wallets": total_wallets,
                "total_actions": total_actions,
                "success_actions": success_actions,
                "total_log_entries": total_log_entries,
            }
        except Exception as e:
            logger.error(f"Ошибка чтения статистики: {e}")
            return {
                "total_wallets": 0,
                "total_actions": 0,
                "success_actions": 0,
                "total_log_entries": 0,
            }

    async def record_cycle(
        self,
        *,
        mode: str = "cycle",
        duration_s: float = 0.0,
        wallets: int = 0,
        wallets_ok: int = 0,
        actions_ok: int = 0,
        errors: int = 0,
        rpc_url: str = "",
        rpc_calls: int = 0,
        rpc_errors: int = 0,
        rpc_latency_ms: int = 0,
        keep: int = 2000,
    ) -> None:
        """Пишет одну строку в журнал циклов, затем подрезает до keep последних.

        rpc_url очищается от credentials/query-string перед записью: публичный
        RPC-эндпоинт часто несёт API-ключ в URL (?api_key=...), который нельзя
        персистить в БД и показывать в истории/веб-панели.
        """
        try:
            db = await self._connect()
            await db.execute(
                """
                INSERT INTO cycle_history (mode, duration_s, wallets, wallets_ok, actions_ok, errors, rpc_url,
                                           rpc_calls, rpc_errors, rpc_latency_ms)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    mode,
                    duration_s,
                    int(wallets),
                    int(wallets_ok),
                    int(actions_ok),
                    int(errors),
                    _redact_rpc_url(rpc_url),
                    int(rpc_calls),
                    int(rpc_errors),
                    int(rpc_latency_ms),
                ),
            )
            await db.commit()
            if keep > 0:
                await db.execute(
                    "DELETE FROM cycle_history WHERE id NOT IN (SELECT id FROM cycle_history ORDER BY id DESC LIMIT ?)",
                    (int(keep),),
                )
                await db.commit()
        except Exception as e:
            logger.error(f"Ошибка записи истории цикла: {e}")

    async def get_cycle_history(self, limit: int = 20) -> list[dict]:
        """Последние завершённые циклы фарма (свежие первыми)."""
        rows: list[dict] = []
        try:
            db = await self._connect()
            async with db.execute(
                """
                SELECT id, started_at, mode, duration_s, wallets, wallets_ok, actions_ok, errors, rpc_url,
                       rpc_calls, rpc_errors, rpc_latency_ms
                FROM cycle_history
                ORDER BY id DESC
                LIMIT ?
                """,
                (int(max(limit, 1)),),
            ) as cursor:
                for row in await cursor.fetchall():
                    rows.append(
                        {
                            "id": row[0],
                            "started_at": row[1],
                            "mode": row[2],
                            "duration_s": float(row[3] or 0),
                            "wallets": int(row[4] or 0),
                            "wallets_ok": int(row[5] or 0),
                            "actions_ok": int(row[6] or 0),
                            "errors": int(row[7] or 0),
                            "rpc_url": row[8] or "",
                            "rpc_calls": int(row[9] or 0),
                            "rpc_errors": int(row[10] or 0),
                            "rpc_latency_ms": int(row[11] or 0),
                        }
                    )
        except Exception as e:
            logger.error(f"Ошибка чтения истории циклов: {e}")
        return rows

    async def get_cycle_stats(self) -> dict:
        """Агрегат по журналу циклов для экрана статистики (один запрос)."""
        try:
            db = await self._connect()
            async with db.execute(
                """
                SELECT
                    COUNT(*),
                    COALESCE(SUM(wallets), 0),
                    COALESCE(SUM(actions_ok), 0),
                    COALESCE(SUM(duration_s), 0),
                    MAX(started_at)
                FROM cycle_history
                """
            ) as c:
                row = await c.fetchone()
            cycles, wallets, actions, duration_s, last_started = row
            return {
                "cycles": int(cycles),
                "wallets": int(wallets),
                "actions": int(actions),
                "duration_s": float(duration_s or 0),
                "last_started_at": last_started,
            }
        except Exception as e:
            logger.error(f"Ошибка чтения статистики циклов: {e}")
            return {"cycles": 0, "wallets": 0, "actions": 0, "duration_s": 0.0, "last_started_at": None}
