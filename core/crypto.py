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


def resolve_master_key(config: dict, ignore_env: bool = False) -> bytes:
    """Возвращает 32-байтовый master-ключ. Raises MasterKeyError при любом сбое.

    Приоритет: env FARMER_MASTER_KEY > файл database.master_key.
    Файл создаётся автоматически, если его нет.

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
        return key

    path = config.get("database", {}).get("master_key", "master.key")
    p = Path(path)
    if p.exists():
        try:
            data = p.read_bytes()
        except OSError as e:
            raise MasterKeyError(f"Master-ключ {path} не читается: {e}") from e
        if len(data) != 32:
            raise MasterKeyError(f"Master-ключ {path} повреждён (не 32 байта)")
        # Ключ мог быть создан старой версией/сторонним скриптом с широким доступом —
        # пере-ограничиваем права на лучший эффект (доступность ключа важнее).
        try:
            restrict_file_permissions(str(p))
        except Exception:
            pass
        _require_crypto()
        return data

    # Ключа нет — создаём, но только если сможем им шифровать
    _require_crypto()
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
    """Шифрует seed-строку. Возвращает 'enc:' + hex(nonce + ciphertext)."""
    if key is None or plaintext is None:
        # Plaintext-режим возможен только у БД без настроенного ключа
        return plaintext
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
    if key is None or value is None:
        return value
    if not value.startswith(_ENC_PREFIX):
        return value
    aes = _aesgcm()
    if aes is None:
        raise ValueError("cryptography не установлена — расшифровка seed-данных невозможна")
    try:
        raw = bytes.fromhex(value[len(_ENC_PREFIX) :])
        nonce, ct = raw[:12], raw[12:]
        return aes(key).decrypt(nonce, ct, _ENV).decode("utf-8")
    except Exception as e:
        raise ValueError(f"Ошибка расшифровки seed-данных: {e}") from e
