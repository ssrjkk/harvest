"""Экспорт кошельков: CSV/JSON с дедупликацией и ограничением прав.

Единая точка экспорта для auto-режима и CLI-меню, чтобы не дублировать
логику (раньше у auto.py были свои export_csv/export_json/_dedup_export).
Здесь же шифрование/расшифровка export-файлов (AES-256-CBC + keyed HMAC
с раздельными ключами).

Формат v2: [16B salt][16B IV][ciphertext][32B HMAC]; ключи PBKDF2(dklen=64):
[0:32] — AES-CBC, [32:64] — HMAC (key separation). Формат v1 (единый ключ
на шифрование и MAC) умеем читать для обратной совместимости.
"""

import csv
import hashlib
import io
import json
import os

from colorama import Fore, Style

from core.utils import restrict_file_permissions

# порядок колонок в CSV-экспорте
_CSV_COLUMNS = ["address", "private_key", "mnemonic", "actions"]

# итерации PBKDF2 (та же стоимость, что и в v1)
_PBKDF2_ITERS = 100_000


def _derive_keys(password: str, salt: bytes) -> tuple[bytes, bytes]:
    """PBKDF2-SHA256(dklen=64) -> (enc_key, mac_key) — раздельные ключи."""
    material = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, _PBKDF2_ITERS, dklen=64)
    return material[:32], material[32:]


def _derive_key_v1(password: str, salt: bytes) -> bytes:
    return hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, _PBKDF2_ITERS, dklen=32)


def dedup_export(wallets: list[dict]) -> list[dict]:
    """Удаляет дубликаты по address и private_key из данных экспорта."""
    seen_addrs = set()
    seen_keys = set()
    unique = []
    for w in wallets:
        addr = w.get("address", "")
        key = w.get("private_key", "")
        if addr in seen_addrs or key in seen_keys:
            continue
        seen_addrs.add(addr)
        seen_keys.add(key)
        unique.append(w)
    dupes = len(wallets) - len(unique)
    if dupes > 0:
        print(f"  {Fore.YELLOW}Удалено {dupes} дубликатов из экспорта{Style.RESET_ALL}")
    return unique


def _atomic_write_restricted(path: str, data: str, encoding: str = "utf-8") -> None:
    """Пишет файл с приватными ключами через tmp + chmod до переноса.

    Наивное open(path) оставляло окно, пока файл с ключами без прав
    виден другим локальным пользователям (на Windows до установки ACL).
    Правый порядок: tmp -> restrict -> os.replace (атомарно). Перед
    replace — flush+fsync: при крахе в середине записи tmp не остаётся
    частичным (иначе возможен битый файл в точке исправной замены).
    """
    tmp = path + ".tmp"
    with open(tmp, "w", newline="", encoding=encoding) as f:
        f.write(data)
        f.flush()
        os.fsync(f.fileno())
    restrict_file_permissions(tmp)
    os.replace(tmp, path)


def _csv_cell_safe(value) -> str:
    """Нейтрализует формульные инъекции в электронных таблицах.

    Если ячейка начинается с = + - @ (или таба/CR), Excel/Sheets
    исполнят как формулу (DDE/Lookup-атаки в CSV-экспорте при самодельных
    мнемониках/метаданных). Апостроф-префикс обезвреживает. Hex-адреса и
    BIP39-слова не затронуты.
    """
    s = str(value)
    if s[:1] in {"=", "+", "-", "@", "\t", "\r"}:
        return "'" + s
    return s


def export_csv(wallets: list[dict], path: str) -> str:
    """Экспортирует кошельки в CSV и возвращает путь к файлу.

    Кодировка utf-8-sig (BOM), чтобы Excel корректно открывал кириллицу.
    """
    wallets = dedup_export(wallets)
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(_CSV_COLUMNS)
    for v in wallets:
        w.writerow(
            [
                _csv_cell_safe(v["address"]),
                _csv_cell_safe(v["private_key"]),
                _csv_cell_safe(v.get("mnemonic", "")),
                _csv_cell_safe(v.get("total_actions", 0)),
            ]
        )
    _atomic_write_restricted(path, buf.getvalue(), encoding="utf-8-sig")
    return path


def export_json(wallets: list[dict], path: str) -> str:
    """Экспортирует кошельки в JSON и возвращает путь к файлу."""
    wallets = dedup_export(wallets)
    _atomic_write_restricted(path, json.dumps(wallets, indent=2, ensure_ascii=False))
    return path


def export_to_file(wallets: list, path: str = "wallets.json") -> str:
    """Экспорт в JSON со стандартным набором полей кошелька.

    Намеренно не включает поле actions/total_actions — используется
    в CLI-меню для быстрого выгрузки свежесозданных кошельков.
    """
    data = [
        {
            "address": w["address"],
            "private_key": w["private_key"],
            "mnemonic": w.get("mnemonic", ""),
        }
        for w in wallets
    ]
    return export_json(data, path)


def encrypt_file(path: str, password: str) -> str:
    """Шифрует файл AES-256-CBC (PBKDF2 key derivation + keyed HMAC).

    Формат: [16-byte salt][16-byte IV][encrypted data][32-byte HMAC-SHA256].
    Исходный файл заменяется на зашифрованную версию с расширением .enc.
    Возвращает путь к зашифрованному файлу (или исходный при ошибке).
    """
    try:
        import hmac as hmac_module

        from cryptography.hazmat.primitives import padding as sym_padding
        from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

        with open(path, "rb") as f:
            data = f.read()
        salt = os.urandom(16)
        iv = os.urandom(16)
        enc_key, mac_key = _derive_keys(password, salt)

        padder = sym_padding.PKCS7(128).padder()
        padded = padder.update(data) + padder.finalize()

        cipher = Cipher(algorithms.AES(enc_key), modes.CBC(iv))
        enc = cipher.encryptor()
        ciphertext = enc.update(padded) + enc.finalize()

        mac = hmac_module.new(mac_key, salt + iv + ciphertext, hashlib.sha256).digest()

        enc_path = path + ".enc"
        # Атомарная публикация: tmp с последующим replace, чтобы при падении
        # в середине записи не осталось частичного .enc (и plaintext на диске
        # дольше, чем необходимо). Права — до публикации, как в БД/master.key.
        tmp_path = enc_path + ".tmp"
        with open(tmp_path, "wb") as f:
            f.write(salt + iv + ciphertext + mac)
            f.flush()
            os.fsync(f.fileno())
        restrict_file_permissions(tmp_path)
        os.replace(tmp_path, enc_path)
        try:
            os.remove(path)
        except OSError as e:
            # Исходник остался открытым текстом: делаем РЕШЕТО громким и
            # возвращаем исходный путь — auto.py/CLI увидят «не зашифровано»
            # и не напечатают ложный успех (раньше рядом с .enc мог лежать
            # plaintext, а пользователь получал «Файлы зашифрованы»).
            msg = (
                "  {Fore.RED}Исходный файл НЕ удалён после шифрования — он остался "
                "открытым текстом: {e}{Style.RESET_ALL}"
            )
            print(msg.format(Fore=Fore, e=e, Style=Style))
            return path
        return enc_path
    except ImportError:
        print(f"  {Fore.YELLOW}Для шифрования установите: pip install cryptography{Style.RESET_ALL}")
        return path
    except Exception as e:
        print(f"  {Fore.YELLOW}Ошибка шифрования: {e}{Style.RESET_ALL}")
        return path


def decrypt_file(path: str, password: str) -> str:
    """Расшифровывает файл, созданный encrypt_file.

    Проверяет HMAC целостности: при несовпадении (неверный пароль или
    повреждённый файл) ничего не изменяет и возвращает исходный путь.
    Зашифрованный файл удаляется, на его место пишется расшифрованный.
    """
    try:
        import hmac as hmac_module

        from cryptography.hazmat.primitives import padding as sym_padding
        from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

        with open(path, "rb") as f:
            blob = f.read()
        if len(blob) < 16 + 16 + 32:
            print(f"  {Fore.RED}Файл повреждён (слишком мал): {path}{Style.RESET_ALL}")
            return path
        salt, iv, ciphertext, mac = blob[:16], blob[16:32], blob[32:-32], blob[-32:]

        # Сначала пробуем v2 (раздельные ключи), затем v1 (единый ключ) —
        # это сохраняет совместимость со старыми .enc-файлами.
        # DEPRECATED: v1 (AES+MAC одним ключом) поддерживается только для
        # чтения. Новые файлы писать v2 (encrypt-then-MAC, key separation).
        key_v2 = _derive_keys(password, salt)

        def _verify(pair) -> tuple[bytes, bytes] | None:
            enc_key, mac_key = pair
            expected = hmac_module.new(mac_key, salt + iv + ciphertext, hashlib.sha256).digest()
            if hmac_module.compare_digest(expected, mac):
                return enc_key, mac_key
            return None

        # v1 (единый ключ) деривируем лениво, только если v2 не подошёл:
        # при неверном пароле не тратим лишние 100k итераций PBKDF2 на
        # ключевой материал, который всё равно не используется (и не
        # удешевляем подбор пароля конкатенацией обеих версий).
        derived = _verify(key_v2)
        if derived is None:
            derived = _verify((_derive_key_v1(password, salt),) * 2)
        if derived is None:
            print(f"  {Fore.RED}Неверный пароль или файл повреждён: {path}{Style.RESET_ALL}")
            return path
        enc_key, _mac_key = derived

        cipher = Cipher(algorithms.AES(enc_key), modes.CBC(iv))
        dec = cipher.decryptor()
        padded = dec.update(ciphertext) + dec.finalize()
        unpadder = sym_padding.PKCS7(128).unpadder()
        data = unpadder.update(padded) + unpadder.finalize()

        out_path = path[:-4] if path.endswith(".enc") else path + ".dec"
        tmp_out = out_path + ".tmp"
        with open(tmp_out, "wb") as wf:
            wf.write(data)
        restrict_file_permissions(tmp_out)
        os.replace(tmp_out, out_path)
        os.remove(path)
        return out_path
    except ImportError:
        print(f"  {Fore.YELLOW}Для расшифровки установите: pip install cryptography{Style.RESET_ALL}")
        return path
    except Exception as e:
        print(f"  {Fore.RED}Ошибка расшифровки: {e}{Style.RESET_ALL}")
        return path
