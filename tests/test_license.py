"""Тесты лицензионного гейта: анализ, кэш (HMAC-форджинг), офлайн grace-окно."""

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.license import LicenseManager


def make_cfg(tdir: str, **over) -> dict:
    cfg = {
        "license": {
            "enabled": True,
            "deploy_url": "http://127.0.0.1:1/license.json",
            "deploy_salt": "salt-test",
            "grace_days": 7,
            "cache_file": str(Path(tdir) / ".license_cache"),
            "timeout": 1.0,
        }
    }
    cfg["license"].update(over)
    return cfg


class TestDisabledAndConfig(unittest.TestCase):
    def test_disabled_ok(self):
        lm = LicenseManager({"license": {"enabled": False}})
        import asyncio

        ok, reason = asyncio.run(lm.require_activation())
        self.assertTrue(ok)

    def test_enabled_without_url(self):
        with tempfile.TemporaryDirectory() as td:
            cfg = make_cfg(td, deploy_url="")
            lm = LicenseManager(cfg)
            import asyncio

            ok, reason = asyncio.run(lm.require_activation())
            self.assertFalse(ok)
            self.assertIn("deploy_url не задан", reason)


class TestAnalyze(unittest.TestCase):
    def test_revoked(self):
        lm = LicenseManager(make_cfg(tempfile.mkdtemp()))
        ok, reason = lm._analyze({"app": "harvest", "v": 2, "status": "revoked", "pass": "x"})
        self.assertFalse(ok)

    def test_empty(self):
        lm = LicenseManager(make_cfg(tempfile.mkdtemp()))
        ok, _ = lm._analyze(None)
        self.assertFalse(ok)
        ok, _ = lm._analyze({})
        self.assertFalse(ok)

    def test_expired(self):
        lm = LicenseManager(make_cfg(tempfile.mkdtemp()))
        ok, _ = lm._analyze({"pass": "x", "expires": "2020-01-01T00:00:00Z"})
        self.assertFalse(ok)

    def test_old_version(self):
        lm = LicenseManager(make_cfg(tempfile.mkdtemp()))
        ok, _ = lm._analyze({"pass": "x", "v": 1})
        self.assertFalse(ok)

    def test_good(self):
        lm = LicenseManager(make_cfg(tempfile.mkdtemp()))
        ok, _ = lm._analyze({"pass": "x"})
        self.assertTrue(ok)

    def test_foreign_app(self):
        lm = LicenseManager(make_cfg(tempfile.mkdtemp()))
        ok, _ = lm._analyze({"app": "other", "pass": "x"})
        self.assertFalse(ok)


class TestHash(unittest.TestCase):
    def test_make_hash(self):
        import hashlib

        lm = LicenseManager(make_cfg(tempfile.mkdtemp()))
        h = lm.make_hash("pw", "salt")
        self.assertTrue(h.startswith("pbkdf2_sha256$210000$salt$"))
        dk = hashlib.pbkdf2_hmac("sha256", b"pw", b"salt", 210_000)
        self.assertEqual(h.split("$", 3)[3], dk.hex())
        self.assertNotEqual(h, lm.make_hash("pw2", "salt"))

    def test_verify_new_format(self):
        lm = LicenseManager(make_cfg(tempfile.mkdtemp()))
        h = lm.make_hash("secret", "salt-test")
        self.assertTrue(lm._verify_password({"pass": h}, "secret"))
        self.assertFalse(lm._verify_password({"pass": h}, "wrong"))

    def test_verify_legacy_sha256(self):
        import hashlib

        lm = LicenseManager(make_cfg(tempfile.mkdtemp()))
        legacy = hashlib.sha256(b"salt-testsecret").hexdigest()
        self.assertTrue(lm._verify_password({"pass": legacy}, "secret"))
        self.assertFalse(lm._verify_password({"pass": legacy}, "wrong"))

    def test_verify_rejects_ridiculous_iterations(self):
        import hashlib

        lm = LicenseManager(make_cfg(tempfile.mkdtemp()))
        dk = hashlib.pbkdf2_hmac("sha256", b"x", b"s", 1)
        forged = f"pbkdf2_sha256$1$s${dk.hex()}"
        self.assertFalse(lm._verify_password({"pass": forged}, "x"))

    def test_verify_rejects_huge_iterations_cpu_dos(self):
        # Скомпрометированный license-hub не должен заставлять клиента жечь CPU:
        # более 1M итераций (генерилось бы ~секунды на каждую попытку) — отказ.
        lm = LicenseManager(make_cfg(tempfile.mkdtemp()))
        forged = f"pbkdf2_sha256$5000000$s${'0' * 64}"
        self.assertFalse(lm._verify_password({"pass": forged}, "x"))

    def test_cli_setpass(self):
        # python -m core.license setpass должен возвращать тот же hash
        root = str(Path(__file__).resolve().parent.parent)
        out = subprocess.run(
            [sys.executable, "-m", "core.license", "setpass", "secret", "salt-cli"],
            cwd=root,
            capture_output=True,
            text=True,
            timeout=30,
        )
        self.assertEqual(out.returncode, 0)
        self.assertEqual(
            out.stdout.strip(),
            LicenseManager.make_hash("secret", "salt-cli"),
        )


class TestCache(unittest.TestCase):
    def test_roundtrip(self):
        with tempfile.TemporaryDirectory() as td:
            lm = LicenseManager(make_cfg(td))
            payload = {"app": "harvest", "status": "active", "pass": "abc"}
            lm._cache_write(payload, "abc")
            cache = lm._cache_read()
            assert cache is not None
            self.assertEqual(cache["payload"]["pass"], "abc")

    def test_forge_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            lm = LicenseManager(make_cfg(td))
            lm._cache_write({"pass": "abc"}, "abc")
            path = Path(td) / ".license_cache"
            data = json.loads(path.read_text(encoding="utf-8"))
            data["raw"] = data["raw"].replace("abc", "hacked")
            path.write_text(json.dumps(data), encoding="utf-8")
            self.assertIsNone(lm._cache_read())

    def test_swap_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            lm = LicenseManager(make_cfg(td))
            lm._cache_write({"pass": "aaa"}, "aaa")
            path = Path(td) / ".license_cache"
            data = json.loads(path.read_text(encoding="utf-8"))
            data = {"raw": data["raw"], "sig": "0" * 64}
            path.write_text(json.dumps(data), encoding="utf-8")
            self.assertIsNone(lm._cache_read())

    def test_cache_write_atomic_no_tmp_leftover(self):
        with tempfile.TemporaryDirectory() as td:
            lm = LicenseManager(make_cfg(td))
            lm._cache_write({"pass": "abc"}, "abc")
            self.assertTrue((Path(td) / ".license_cache").is_file())
            # атомарная запись не оставляет временных артефактов
            self.assertFalse((Path(td) / ".license_cache.tmp").exists())
            cache = lm._cache_read()
            self.assertIsNotNone(cache)
            if cache is not None:
                self.assertEqual(cache["payload"]["pass"], "abc")


class TestFetchPolicy(unittest.TestCase):
    def test_http_non_loopback_refused(self):
        lm = LicenseManager(make_cfg(tempfile.mkdtemp(), deploy_url="http://license.evil.example/license.json"))
        self.assertIsNone(lm._fetch())

    def test_loopback_http_allowed_but_unreachable(self):
        # 127.0.0.1:1 недоступен — соединение отклонится мгновенно; главное,
        # схема http на loopback не отсекается до сетевого вызова.
        lm = LicenseManager(make_cfg(tempfile.mkdtemp(), deploy_url="http://127.0.0.1:1/license.json"))
        self.assertIsNone(lm._fetch())

    def test_malformed_url_returns_none(self):
        lm = LicenseManager(make_cfg(tempfile.mkdtemp(), deploy_url="::::not-a-url"))
        self.assertIsNone(lm._fetch())


class TestOfflineGrace(unittest.TestCase):
    def test_no_cache_unreachable(self):
        with tempfile.TemporaryDirectory() as td:
            cfg = make_cfg(td, grace_days=7)
            # delete cache file if any
            Path(td, ".license_cache").unlink(missing_ok=True)
            import asyncio

            lm = LicenseManager(cfg)
            ok, reason = asyncio.run(lm.require_activation())
            self.assertFalse(ok)
            self.assertIn("нет связи", reason)

    def test_expired_cache(self):
        with tempfile.TemporaryDirectory() as td:
            cfg = make_cfg(td, grace_days=-1)
            import asyncio

            lm = LicenseManager(cfg)
            payload = {"app": "harvest", "status": "active", "pass": "abc"}
            lm._cache_write(payload, "abc")
            ok, reason = asyncio.run(lm.require_activation())
            self.assertFalse(ok)
            self.assertIn("протух", reason)

    def test_fresh_cache_ok(self):
        with tempfile.TemporaryDirectory() as td:
            cfg = make_cfg(td, grace_days=7)
            import asyncio

            lm = LicenseManager(cfg)
            payload = {"app": "harvest", "status": "active", "pass": "abc"}
            lm._cache_write(payload, "abc")
            ok, reason = asyncio.run(lm.require_activation())
            self.assertTrue(ok)


if __name__ == "__main__":
    unittest.main(verbosity=2)
