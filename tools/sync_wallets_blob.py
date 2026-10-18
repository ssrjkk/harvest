"""Шифрованный блоб кошельков для облачного бота (замена MotherDuck).

Запускается с ПК вместе с `publish_dash.py`: читает wallets из PORTAL_DB
(расшифровка через FARMER_MASTER_KEY env или master.key рядом с БД),
шифрует весь список одним AES-256-GCM ключом WALLETS_KEY и отдаёт dict-блоб,
который publish_dash.py кладёт в gh-pages как `wallets.enc.json` рядом
со state.json. Бот (botforge/bot.py) скачивает его по HTTPS и расшифровывает
тем же WALLETS_KEY из своих Secrets.

Новых хостов/провайдеров нет: только уже существующие GitHub Pages + BotForge.
В открытом виде ключи на публичный хост не попадают (только шифротекст);
без WALLETS_KEY с обеих сторон — штатный пропуск (бот без кошельков).

Формат блоба (зеркально реализован в botforge/bot.py, без общих импортов):
    {"v": 1, "alg": "AES-256-GCM", "nonce": "<hex 12B>", "ct": "<hex>"}
Открытый текст: JSON-список [{"address","private_key","mnemonic","total_actions"}],
AAD: b"wallets-blob-v1".

Ключ блоба: строго env WALLETS_KEY = 64 hex (32 байта), в файлы/конфиги
не пишется. Генерация: python -c "import secrets; print(secrets.token_hex(32))".
Ключ БД ищется так: FARMER_MASTER_KEY → master.key рядом с БД →
master.key в CWD (поэтому publish запускается из корня репо) — и проверяется
пробной расшифровкой первого seed; чужой ключ отбрасывается, а не роняет всё.

Использование:
    WALLETS_KEY=... PORTAL_DB=farming_run200.db python -m tools.sync_wallets_blob
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import secrets
import sys
from pathlib import Path

logger = logging.getLogger(__name__)

_DB_ENV = "PORTAL_DB"
_KEY_ENV = "WALLETS_KEY"
_AAD = b"wallets-blob-v1"
_BLOB_VERSION = 1


def _db_path() -> str:
    return os.environ.get(_DB_ENV, "farming_vibevibe.db")


def _parse_key(raw: str | None) -> bytes:
    """Строгий разбор WALLETS_KEY: 64 hex = 32 байта, иначе ValueError."""
    s = (raw or "").strip()
    if s[:2].lower() == "0x":
        s = s[2:]
    try:
        key = bytes.fromhex(s)
    except ValueError:
        raise ValueError("WALLETS_KEY не является hex-строкой (нужно 64 hex)") from None
    if len(key) != 32:
        raise ValueError("WALLETS_KEY должен быть ровно 32 байта (64 hex)")
    return key


def _key_from_env() -> bytes:
    return _parse_key(os.environ.get(_KEY_ENV, ""))


def _master_key(db_path: str) -> bytes:
    """Ключ БД: перебирает кандидатов и возвращает тот, что реально расшифровывает.

    Кандидаты: FARMER_MASTER_KEY env, master.key рядом с БД, master.key в CWD.
    Живая ферма могла стартовать из другой папки, чем лежит БД, — файл рядом
    с БД не всегда тот. Выбор проверяется пробной расшифровкой первого seed.
    """
    from core.crypto import MasterKeyError, decrypt_seed, resolve_master_key

    cands: list[tuple[str, bytes]] = []
    try:
        cands.append(("FARMER_MASTER_KEY", resolve_master_key({"database": {"master_key": ""}})))
    except MasterKeyError:
        pass
    for label, path in (
        ("рядом с БД", str(Path(db_path).parent / "master.key")),
        ("в CWD", "master.key"),
    ):
        try:
            cands.append((label, resolve_master_key({"database": {"master_key": path}}, ignore_env=True)))
        except MasterKeyError:
            pass
    # Дубли убираем, порядок сохраняем.
    seen: set[bytes] = set()
    uniq: list[tuple[str, bytes]] = []
    for label, k in cands:
        if k not in seen:
            seen.add(k)
            uniq.append((label, k))
    if not uniq:
        raise MasterKeyError("нет кандидата ключа: ни FARMER_MASTER_KEY, ни master.key")
    sample = _first_seed(db_path)
    if sample is None:
        return uniq[0][1]
    for label, k in uniq:
        try:
            decrypt_seed(k, sample)
            return k
        except Exception:
            continue
    raise MasterKeyError(
        "ни один ключ не расшифровал БД (проверены: %s)" % ", ".join(label for label, _ in uniq)
    )


def _first_seed(db_path: str) -> str | None:
    """Первый enc: private_key для проверки ключа (None — нечего проверять)."""
    import sqlite3

    try:
        con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=10)
        try:
            row = con.execute(
                "SELECT private_key FROM wallets WHERE private_key LIKE 'enc:%' LIMIT 1"
            ).fetchone()
            return row[0] if row else None
        finally:
            con.close()
    except Exception:
        return None


async def _read_wallets(db_path: str, key: bytes) -> list[dict]:
    """Читает wallets read-only (WAL живой фермы не трогаем записью)."""
    import aiosqlite

    from core.crypto import decrypt_seed

    async with aiosqlite.connect(f"file:{db_path}?mode=ro", uri=True, timeout=10) as db:
        try:
            cur = await db.execute(
                "SELECT address, private_key, mnemonic, "
                "COALESCE(total_actions, 0) FROM wallets ORDER BY rowid"
            )
            raw = await cur.fetchall()
        except Exception as e:
            raise RuntimeError(f"wallets unreadable: {e}") from e
    rows: list[dict] = []
    for addr, pk, mn, acts in raw:
        try:
            rows.append({
                "address": addr,
                "private_key": decrypt_seed(key, pk),
                "mnemonic": decrypt_seed(key, mn) if mn else None,
                "total_actions": int(acts or 0),
            })
        except Exception as e:
            logger.warning("Кошелёк %s не расшифрован (%s) — пропуск", str(addr)[:10], e)
    return rows


def encrypt_wallets(rows: list[dict], key: bytes) -> dict:
    """Шифрует список кошельков в блоб. Raises MasterKeyError/ValueError."""
    from core.crypto import _aesgcm

    aes = _aesgcm()
    if aes is None:
        raise ValueError("cryptography не установлена — шифрование блоба невозможно")
    if len(key) != 32:
        raise ValueError("ключ должен быть 32 байта")
    nonce = secrets.token_bytes(12)
    pt = json.dumps(rows, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    ct = aes(key).encrypt(nonce, pt, _AAD)
    from datetime import UTC, datetime

    return {"v": _BLOB_VERSION, "alg": "AES-256-GCM",
            "built_at": datetime.now(UTC).isoformat(timespec="seconds"),
            "nonce": nonce.hex(), "ct": ct.hex()}


def decrypt_blob(blob: dict, key: bytes) -> list[dict]:
    """Расшифровывает блоб. Raises ValueError при любом сбое (fail-closed)."""
    from core.crypto import _aesgcm

    aes = _aesgcm()
    if aes is None:
        raise ValueError("cryptography не установлена — расшифровка блоба невозможна")
    try:
        if not isinstance(blob, dict) or blob.get("v") != _BLOB_VERSION:
            raise ValueError("неизвестная версия блоба")
        nonce = bytes.fromhex(blob["nonce"])
        ct = bytes.fromhex(blob["ct"])
        pt = aes(key).decrypt(nonce, ct, _AAD)
        rows = json.loads(pt.decode("utf-8"))
    except ValueError:
        raise
    except Exception as e:
        raise ValueError(f"блоб не расшифрован: {e}") from e
    if not isinstance(rows, list):
        raise ValueError("блоб не расшифрован: неверная структура")
    return rows


async def sync(db_path: str | None = None, key_hex: str | None = None) -> dict:
    """Строит шифрованный блоб. Возвращает отчёт (не бросает без причины)."""
    db = db_path or _db_path()
    try:
        key = _parse_key(key_hex if key_hex is not None else os.environ.get(_KEY_ENV, ""))
    except ValueError as e:
        return {"ok": False, "reason": f"no WALLETS_KEY — пропуск (бот без кошельков): {e}"}
    if not Path(db).exists():
        return {"ok": False, "reason": f"БД не найдена: {db}"}
    try:
        mkey = _master_key(db)
    except Exception as e:
        return {"ok": False, "reason": f"master-key недоступен: {e}"}
    try:
        rows = await _read_wallets(db, mkey)
    except Exception as e:
        return {"ok": False, "reason": f"read failed: {e}"}
    try:
        blob = await asyncio.to_thread(encrypt_wallets, rows, key)
    except Exception as e:
        return {"ok": False, "reason": f"encrypt failed: {e}"}
    return {"ok": True, "blob": blob, "count": len(rows)}


async def _amain() -> int:
    res = await sync()
    if res.get("ok"):
        print(f"блоб собран: {res.get('count')} кошельков (wallets.enc.json)")
        return 0
    print(f"блоб пропущен: {res.get('reason')}")
    return 2


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    return asyncio.run(_amain())


if __name__ == "__main__":
    sys.exit(main())
