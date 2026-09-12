"""Тесты крипто-слоя: шифрование/дешифрование и fail-closed master-ключ."""

import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.crypto import MasterKeyError, decrypt_seed, encrypt_seed, resolve_master_key


class HasKeyConfigMixin:
    def make_config(self, key_file):
        return {"database": {"master_key": str(key_file)}}


class TestCryptoRoundtrip(unittest.TestCase):
    def test_roundtrip(self):
        key = bytes(range(32))
        plain = "0x" + "ab" * 32
        enc = encrypt_seed(key, plain)
        assert isinstance(enc, str)
        self.assertTrue(enc.startswith("enc:"))
        self.assertEqual(decrypt_seed(key, enc), plain)

    def test_null_handling(self):
        self.assertIsNone(encrypt_seed(None, None))
        self.assertIsNone(decrypt_seed(None, None))
        self.assertIsNone(decrypt_seed(bytes(range(32)), None))

    def test_wrong_key_fails(self):
        enc = encrypt_seed(bytes(range(32)), "0x" + "aa" * 32)
        with self.assertRaises(ValueError):
            decrypt_seed(bytes(range(1, 33)), enc)


class TestMasterKey(unittest.TestCase, HasKeyConfigMixin):
    def tearDown(self):
        os.environ.pop("FARMER_MASTER_KEY", None)

    def test_file_key(self):
        with tempfile.NamedTemporaryFile(delete=False, suffix=".key") as f:
            f.write(bytes.fromhex("ab" * 32))
            path = f.name
        try:
            cfg = {"database": {"master_key": path}}
            self.assertEqual(resolve_master_key(cfg), bytes.fromhex("ab" * 32))
        finally:
            os.unlink(path)

    def test_missing_key_fails_closed(self):
        with self.assertRaises(MasterKeyError):
            resolve_master_key({"database": {"master_key": "/nonexistent/definitely/not/here"}})

    def test_create_key_atomic_no_leftovers(self):
        # Создание ключа должно быть атомарным: после успеха нет .tmp,
        # а файл содержит ровно тот ключ, что вернула функция.
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "master.key"
            cfg = self.make_config(path)
            key = resolve_master_key(cfg)
            self.assertEqual(len(key), 32)
            self.assertTrue(path.exists())
            self.assertEqual(path.read_bytes(), key)
            self.assertEqual(list(Path(d).glob("*.tmp")), [])
            # Повторный вызов возвращает тот же сохранённый ключ
            self.assertEqual(resolve_master_key(cfg), key)

    def test_env_key(self):
        os.environ["FARMER_MASTER_KEY"] = "cd" * 32
        cfg = {"database": {"master_key": ""}}
        self.assertEqual(resolve_master_key(cfg), bytes.fromhex("cd" * 32))

    def test_bad_env_fails_closed(self):
        os.environ["FARMER_MASTER_KEY"] = "nothex!"
        with self.assertRaises(MasterKeyError):
            resolve_master_key({"database": {"master_key": ""}})

    def test_ignore_env_skips_env_and_reads_file(self):
        os.environ["FARMER_MASTER_KEY"] = "cd" * 32
        with tempfile.NamedTemporaryFile(delete=False, suffix=".key") as f:
            f.write(bytes.fromhex("ab" * 32))
            path = f.name
        try:
            cfg = {"database": {"master_key": path}}
            # ignore_env=True игнорирует даже валидный env — читаем файл
            self.assertEqual(resolve_master_key(cfg, ignore_env=True), bytes.fromhex("ab" * 32))
        finally:
            os.unlink(path)

    def test_ignore_env_rescues_from_bad_env(self):
        os.environ["FARMER_MASTER_KEY"] = "nothex!"
        with tempfile.NamedTemporaryFile(delete=False, suffix=".key") as f:
            f.write(bytes.fromhex("ef" * 32))
            path = f.name
        try:
            cfg = {"database": {"master_key": path}}
            # Обычный путь fail-closed
            with self.assertRaises(MasterKeyError):
                resolve_master_key(cfg)
            # ignore_env=True — работает от файла, не трогая env
            self.assertEqual(resolve_master_key(cfg, ignore_env=True), bytes.fromhex("ef" * 32))
        finally:
            os.unlink(path)


if __name__ == "__main__":
    unittest.main(verbosity=2)
