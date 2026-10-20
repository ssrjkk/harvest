"""Шифрование seed-данных кошельков и управление master-ключом.

Приватные ключи и сид-фразы хранятся в БД зашифрованными (AES-256-GCM),
чтобы файл farming_state.db (и бэкапы .backup/, и WAL) не содержали секреты
в открытом виде. Ключ берётся из env FARMER_MASTER_KEY (64 hex) или из файла
database.master_key, который создаётся автоматически с правами только владельца.

Поведение fail-closed: некорректный/недоступный master-ключ — это ошибка, а не
разрешение писать секреты открытым текстом. resolve_master_key никогда не
возвращает None (только bytes или MasterKeyError).
"""

import logging
import os
import secrets
from pathlib import Path

from core.utils import restrict_file_permissions

logger = logging.getLogger(__name__)

_ENC_PREFIX = "enc:"
# AAD: привязывает шифротекст к типу данных (seed кошелька)
_ENV = b"wallet-seed"
_AESGCM = None
_AESGCM_IMPORT_FAILED = False


class MasterKeyError(Exception):
    """Некорректный или недоступный master-ключ: безопасная работа невозможна."""


def _aesgcm():
    """Ленивый импорт cryptography (обязателен при включённом шифровании)."""
    global _AESGCM, _AESGCM_IMPORT_FAILED
    if _AESGCM is None and not _AESGCM_IMPORT_FAILED:
        try:
            from cryptography.hazmat.primitives.ciphers.aead import AESGCM

            _AESGCM = AESGCM
        except ImportError:
            _AESGCM_IMPORT_FAILED = True
    return _AESGCM


def _require_crypto() -> None:
    if _aesgcm() is None:
        raise MasterKeyError(
            "Установите 'cryptography' (pip install cryptography): без неё безопасное хранение seed-данных невозможно"
        )


def _sample_encrypted_seed(db_path: str) -> str | None:
    """Первый зашифрованный seed из БД — для проверки ключа пробной расшифровкой.

    Только чтение, только локальный SQLite. Ошибки не поднимаем: проверка ключа
    — необязательный шаг, её отсутствие не должно ломать запуск.
    """
    if not db_path or db_path.startswith(("postgres://", "postgresql://")):
        return None
    try:
        import sqlite3

        con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=2)
        try:
            row = con.execute(
                "SELECT private_key FROM wallets WHERE private_key LIKE 'enc:%' LIMIT 1"
            ).fetchone()
        finally:
            con.close()
    except Exception as e:  # noqa: BLE001 — БД может быть залочена/отсутствовать
        logger.debug(f"Не удалось взять пробный seed из {db_path}: {e}")
        return None
    return row[0] if row else None


def _key_files(config: dict, db_path: str | None) -> list[Path]:
    """Кандидаты-файлы master-ключа в порядке приоритета.

    Первый — как в конфиге (путь относительно CWD). Второй — рядом с самой
    БД: ключ логично хранить с той базой, которую он защищает, и тогда ферма
    находит его из любой рабочей папки.
    """
    out: list[Path] = []
    configured = config.get("database", {}).get("master_key", "master.key")
    if configured:
        out.append(Path(configured))
    if db_path and not db_path.startswith(("postgres://", "postgresql://")):
        near = Path(db_path).parent / "master.key"
        if near not in out:
            out.append(near)
    return out


def _restrict(path: Path) -> None:
    """Ограничивает права ключа.

    Ключ мог быть создан старой версией или сторонним скриптом с широким
    доступом — пере-ограничиваем. Ошибка не должна ломать чтение ключа.
    """
    try:
        restrict_file_permissions(str(path))
    except Exception:  # noqa: BLE001
        pass


def resolve_master_key(config: dict, ignore_env: bool = False, db_path: str | None = None) -> bytes:
    """Возвращает 32-байтовый master-ключ. Raises MasterKeyError при любом сбое.

    Приоритет: env FARMER_MASTER_KEY > файл database.master_key > master.key
    рядом с БД. Файл создаётся автоматически, если его нет.

    db_path — путь к БД, которую ключ защищает. Если кандидатов несколько,
    выбирается тот, который реально расшифровывает первый seed этой БД: ферма
    могла стартовать из другой папки, и «тот самый» файл в CWD может оказаться
    чужим (и наоборот — верным окажется ключ рядом с БД).

    ignore_env=True — не читать FARMER_MASTER_KEY: для случаев, когда env
    содержит короткий PIN-пароль портала, а ключом БД должен остаться master.key.

    Никогда не возвращает None: потеря/порча ключа — критическая ошибка,
    а не повод незаметно вернуться к открытому тексту (fail-closed).
    """
    env = None if ignore_env else os.environ.get("FARMER_MASTER_KEY")
    if env:
        raw = env[2:] if env.startswith(("0x", "0X")) else env
        try:
            key = bytes.fromhex(raw)
        except ValueError:
            raise MasterKeyError("FARMER_MASTER_KEY не является hex-строкой") from None
        if len(key) != 32:
            raise MasterKeyError("FARMER_MASTER_KEY должен быть ровно 32 байта (64 hex)")
        _require_crypto()
        logger.warning(
            "FARMER_MASTER_KEY задан через env: переменные окружения видны другим "
            "процессам пользователя и могут утечь в логи/дампы/CI. Для production "
            "предпочтите файл database.master_key (0600) или KMS/keyring."
        )
        return key

    existing: list[tuple[Path, bytes]] = []
    for p in _key_files(config, db_path):
        if not p.exists():
            continue
        try:
            data = p.read_bytes()
        except OSError as e:
            raise MasterKeyError(f"Master-ключ {p} не читается: {e}") from e
        if len(data) != 32:
            raise MasterKeyError(f"Master-ключ {p} повреждён (не 32 байта)")
        existing.append((p, data))

    if existing:
        _require_crypto()
        sample = _sample_encrypted_seed(db_path) if db_path else None
        if sample:
            for p, data in existing:
                try:
                    decrypt_seed(data, sample)
                except Exception:  # noqa: BLE001 — просто не этот ключ
                    continue
                _restrict(p)
                return data
            logger.warning(
                f"Ни один master-ключ ({', '.join(str(p) for p, _ in existing)}) не расшифровал "
                f"seed из {db_path} — берём первый. Кошельки этой БД могут оказаться нечитаемыми."
            )
        p, data = existing[0]
        _restrict(p)
        return data

    # Ключа нет — создаём, но только если сможем им шифровать
    _require_crypto()
    path = config.get("database", {}).get("master_key", "master.key")
    if not path:
        # Пустой путь: файла нет и создать нечего (раньше здесь был
        # MasterKeyError на попытке прочитать каталог).
        raise MasterKeyError("путь к master-ключу не задан (database.master_key)")
    p = Path(path)
    try:
        key = secrets.token_bytes(32)
        # Атомарное создание: пишем во временный файл, ограничиваем права ДО
        # замены. Иначе между write_bytes и restrict существует окно, когда
        # другой локальный пользователь может успеть прочитать ключ.
        tmp = p.with_suffix(p.suffix + ".tmp")
        tmp.write_bytes(key)
        restrict_file_permissions(str(tmp))
        tmp.replace(p)
        logger.warning(f"Создан master-ключ {path} — без него кошельки не восстановить!")
        return key
    except OSError as e:
        raise MasterKeyError(f"Не удалось создать master-ключ {path}: {e}") from e


def encrypt_seed(key: bytes | None, plaintext: str | None) -> str | None:
    """Шифрует seed-строку. Возвращает 'enc:' + hex(nonce + ciphertext).

    Fail-closed: без ключа шифровать нечего — отказ вместо молчаливого
    plaintext (защита в глубину; вызывающий Database._encrypt тоже проверяет).
    """
    if plaintext is None:
        return None
    if key is None:
        raise MasterKeyError("master_key не задан — отказ от хранения seed-данных открытым текстом")
    aes = _aesgcm()
    if aes is None:
        raise MasterKeyError("cryptography не установлена — шифрование seed-данных невозможно")
    try:
        nonce = secrets.token_bytes(12)
        ct = aes(key).encrypt(nonce, plaintext.encode("utf-8"), _ENV)
        return _ENC_PREFIX + (nonce + ct).hex()
    except Exception as e:
        raise MasterKeyError(f"Ошибка шифрования seed-данных: {e}") from e


def decrypt_seed(key: bytes | None, value: str | None) -> str | None:
    """Расшифровывает seed-строку. Legacy-значения (без префикса) возвращает как есть."""
    if value is None:
        return None
    if not value.startswith(_ENC_PREFIX):
        return value
    # Fail-closed: шифротекст без ключа — ошибка, а не «вернуть как есть»
    # (иначе get_all_wallets положит "enc:..." в private_key и каждое
    # действие упадёт в sign_transaction вместо понятной диагностики).
    if key is None:
        raise MasterKeyError("master_key не задан — зашифрованные seed-данные нечитаемы")
    aes = _aesgcm()
    if aes is None:
        raise ValueError("cryptography не установлена — расшифровка seed-данных невозможна")
    try:
        raw = bytes.fromhex(value[len(_ENC_PREFIX) :])
        nonce, ct = raw[:12], raw[12:]
        return aes(key).decrypt(nonce, ct, _ENV).decode("utf-8")
    except Exception as e:
        raise ValueError(f"Ошибка расшифровки seed-данных: {e}") from e
