"""Тесты экспорта: CSV/JSON, дедупликация, шифрование/дешифрование (AES-256-CBC+HMAC)."""

import csv
import importlib
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.exporter import (
    decrypt_file,
    dedup_export,
    encrypt_file,
    export_csv,
    export_json,
    export_to_file,
)


def sample_wallets():
    return [
        {
            "address": "0x1111",
            "private_key": "0xaaaa",
            "mnemonic": "word one",
            "total_actions": 5,
        },
        {
            "address": "0x2222",
            "private_key": "0xbbbb",
            "mnemonic": "",
            "total_actions": 0,
        },
    ]


class TestDedup(unittest.TestCase):
    def test_dupes_removed(self):
        ws = sample_wallets() + [
            {"address": "0x1111", "private_key": "0xcccc"},  # dup address
            {"address": "0x3333", "private_key": "0xaaaa"},  # dup key
        ]
        out = dedup_export(ws)
        self.assertEqual(len(out), 2)


class TestExports(unittest.TestCase):
    def test_csv(self):
        with tempfile.TemporaryDirectory() as td:
            p = export_csv(sample_wallets(), str(Path(td) / "w.csv"))
            with open(p, newline="", encoding="utf-8-sig") as f:
                rows = list(csv.reader(f))
            self.assertEqual(rows[0], ["address", "private_key", "mnemonic", "actions"])
            self.assertEqual(len(rows), 3)

    def test_csv_formula_injection_neutralized(self):
        # Ячейки =+@ и табы не должны превращаться в формулы в Excel/Sheets.
        evil = "=cmd|' /C calc'!A0"
        with tempfile.TemporaryDirectory() as td:
            p = export_csv(
                [
                    {
                        "address": '=HYPERLINK("http://evil")',
                        "private_key": "@SUM(A1:A2)",
                        "mnemonic": evil,
                        "total_actions": 3,
                    }
                ],
                str(Path(td) / "w.csv"),
            )
            with open(p, newline="", encoding="utf-8-sig") as f:
                rows = list(csv.reader(f))
            addr, key, mnemonic, actions = rows[1]
            self.assertTrue(addr.startswith("'"))
            self.assertTrue(key.startswith("'"))
            self.assertTrue(mnemonic.startswith("'"))
            self.assertFalse(mnemonic.startswith("="))
            # Безопасные значения (hex/числа) не трогаем.
            self.assertEqual(actions, "3")

    def test_json(self):
        with tempfile.TemporaryDirectory() as td:
            p = export_json(sample_wallets(), str(Path(td) / "w.json"))
            data = json.loads(Path(p).read_text(encoding="utf-8"))
            self.assertEqual(len(data), 2)
            self.assertEqual(data[0]["address"], "0x1111")

    def test_export_to_file(self):
        with tempfile.TemporaryDirectory() as td:
            p = export_to_file(sample_wallets(), str(Path(td) / "w.json"))
            data = json.loads(Path(p).read_text(encoding="utf-8"))
            self.assertNotIn("actions", data[0])
            self.assertIn("private_key", data[0])


try:
    importlib.import_module("cryptography")
    HAS_CRYPTO = True
except ImportError:
    HAS_CRYPTO = False


class TestEncryptDecrypt(unittest.TestCase):
    def test_roundtrip(self):
        if not HAS_CRYPTO:
            self.skipTest("cryptography не установлена")
        with tempfile.TemporaryDirectory() as td:
            path = str(Path(td) / "w.json")
            export_csv(sample_wallets(), path)
            enc = encrypt_file(path, "пароль-123")
            self.assertTrue(enc.endswith(".enc"))
            self.assertFalse(Path(path).exists())
            dec = decrypt_file(enc, "пароль-123")
            self.assertEqual(dec, path)
            with open(path, newline="", encoding="utf-8") as f:
                rows = list(csv.reader(f))
            self.assertEqual(len(rows), 3)

    def test_wrong_password_keeps_file(self):
        if not HAS_CRYPTO:
            self.skipTest("cryptography не установлена")
        with tempfile.TemporaryDirectory() as td:
            path = str(Path(td) / "w.json")
            export_csv(sample_wallets(), path)
            enc = encrypt_file(path, "pw")
            dec = decrypt_file(enc, "wrong")
            self.assertEqual(dec, enc)  # ничего не изменено
            self.assertTrue(Path(enc).exists())

    def test_encrypt_no_tmp_leftover(self):
        # Раунд 14: .enc публикуется атомарно (tmp->restrict->replace) —
        # после шифрования не должно оставаться временных файлов.
        if not HAS_CRYPTO:
            self.skipTest("cryptography не установлена")
        with tempfile.TemporaryDirectory() as td:
            path = str(Path(td) / "w.json")
            export_csv(sample_wallets(), path)
            enc = encrypt_file(path, "pw")
            self.assertTrue(Path(enc).exists())
            self.assertFalse(Path(enc + ".tmp").exists())
            self.assertFalse(Path(path + ".tmp").exists())

    def test_v1_encrypted_file_still_decrypts(self):
        # Ленивая деривация v1 (раунд 14) не должна сломать чтение старых
        # файлов формата v1 (AES+MAC одним ключом).
        if not HAS_CRYPTO:
            self.skipTest("cryptography не установлена")
        import hashlib
        import hmac as hmac_module
        import os

        from cryptography.hazmat.primitives import padding as sym_padding
        from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

        with tempfile.TemporaryDirectory() as td:
            path = str(Path(td) / "w.json")
            export_csv(sample_wallets(), path)
            data = Path(path).read_bytes()
            salt, iv = os.urandom(16), os.urandom(16)
            key = hashlib.pbkdf2_hmac("sha256", "пароль-v1".encode(), salt, 100_000, dklen=32)
            padder = sym_padding.PKCS7(128).padder()
            padded = padder.update(data) + padder.finalize()
            cipher = Cipher(algorithms.AES(key), modes.CBC(iv))
            encryptor = cipher.encryptor()
            enc = encryptor.update(padded) + encryptor.finalize()
            mac = hmac_module.new(key, salt + iv + enc, hashlib.sha256).digest()
            enc_path = str(Path(td) / "w_v1.json.enc")
            Path(enc_path).write_bytes(salt + iv + enc + mac)
            dec = decrypt_file(enc_path, "пароль-v1")
            self.assertEqual(Path(dec).read_bytes(), data)


if __name__ == "__main__":
    unittest.main(verbosity=2)
