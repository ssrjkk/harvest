"""Офлайн-тесты портала: сессии, мастер-ключ, подпись Mini App, id_token."""

import base64
import hashlib
import hmac
import json
import time
import unittest
from urllib.parse import urlencode

from portal import auth
from portal.config import validate_master_key


def _jwt(payload: dict, aud: str) -> str:
    body = payload | {"aud": aud}
    enc = base64.urlsafe_b64encode(json.dumps(body).encode()).rstrip(b"=").decode()
    return f"h.{enc}.s"


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _hmac_sha256(key: bytes, data: bytes) -> bytes:
    return hmac.new(key, data, hashlib.sha256).digest()


class TestIdToken(unittest.TestCase):
    _ISS = "https://accounts.google.com"

    @staticmethod
    def _sample(aud="client-1", iss=_ISS, exp=None):
        payload = {
            "email": "user@x.com",
            "email_verified": True,
            "name": "X",
            "sub": "s1",
            "iss": iss,
            "exp": exp if exp is not None else int(time.time()) + 3600,
        }
        return _jwt(payload, aud)

    def test_valid_aud_accepted(self):
        payload = auth._parse_id_token(self._sample(), "client-1")
        assert payload is not None
        self.assertEqual(payload["email"], "user@x.com")

    def test_wrong_aud_rejected(self):
        self.assertIsNone(auth._parse_id_token(self._sample(), "other-client"))

    def test_unverified_email_rejected(self):
        token = _jwt(
            {
                "email": "u@x.com",
                "email_verified": False,
                "iss": self._ISS,
                "exp": int(time.time()) + 3600,
            },
            "client-1",
        )
        self.assertIsNone(auth._parse_id_token(token, "client-1"))

    def test_wrong_issuer_rejected(self):
        token = self._sample(iss="https://evil.example")
        self.assertIsNone(auth._parse_id_token(token, "client-1"))

    def test_valid_nonce_accepted(self):
        payload = {
            "email": "user@x.com",
            "email_verified": True,
            "name": "X",
            "sub": "s1",
            "iss": self._ISS,
            "exp": int(time.time()) + 3600,
            "nonce": "n-secret-1",
        }
        self.assertIsNotNone(auth._parse_id_token(_jwt(payload, "client-1"), "client-1", "n-secret-1"))

    def test_non_matching_nonce_rejected(self):
        # Переигранный/чужой id_token с другим nonce — отклоняется.
        token = self._sample()
        self.assertIsNone(auth._parse_id_token(token, "client-1", "expected-nonce"))

    def test_non_matching_nonce_claim_rejected(self):
        payload = {
            "email": "user@x.com",
            "email_verified": True,
            "iss": self._ISS,
            "exp": int(time.time()) + 3600,
            "nonce": "old-nonce",
        }
        token = _jwt(payload, "client-1")
        self.assertIsNone(auth._parse_id_token(token, "client-1", "current-nonce"))

    def test_nonce_ignored_when_not_expected(self):
        # Обратная совместимость: если nonce не передавали, наличие/отсутствие
        # claim'а nonce не влияет на валидацию.
        payload = {
            "email": "user@x.com",
            "email_verified": True,
            "sub": "s1",
            "iss": self._ISS,
            "exp": int(time.time()) + 3600,
        }
        self.assertIsNotNone(auth._parse_id_token(_jwt(payload, "client-1"), "client-1"))

    def test_expired_rejected(self):
        token = self._sample(exp=int(time.time()) - 120)
        self.assertIsNone(auth._parse_id_token(token, "client-1"))

    def test_exp_within_leeway_accepted(self):
        token = self._sample(exp=int(time.time()) + 30)
        self.assertIsNotNone(auth._parse_id_token(token, "client-1"))

    def test_future_nbf_rejected(self):
        payload = {
            "email": "user@x.com",
            "email_verified": True,
            "iss": self._ISS,
            "exp": int(time.time()) + 3600,
            "nbf": int(time.time()) + 300,
        }
        self.assertIsNone(auth._parse_id_token(_jwt(payload, "client-1"), "client-1"))

    def test_past_nbf_accepted(self):
        payload = {
            "email": "user@x.com",
            "email_verified": True,
            "iss": self._ISS,
            "exp": int(time.time()) + 3600,
            "nbf": int(time.time()) - 300,
        }
        self.assertIsNotNone(auth._parse_id_token(_jwt(payload, "client-1"), "client-1"))

    def test_impossible_future_iat_rejected(self):
        payload = {
            "email": "user@x.com",
            "email_verified": True,
            "iss": self._ISS,
            "exp": int(time.time()) + 3600,
            "iat": int(time.time()) + 600,
        }
        self.assertIsNone(auth._parse_id_token(_jwt(payload, "client-1"), "client-1"))

    def test_garbage_rejected(self):
        self.assertIsNone(auth._parse_id_token("", "client-1"))
        self.assertIsNone(auth._parse_id_token("aaa..bbb", "client-1"))


class TestSessions(unittest.TestCase):
    def test_roundtrip(self):
        token = auth.sign_token("sec", "u1", "Alice")
        payload = auth.read_token("sec", token)
        assert payload is not None
        self.assertEqual(payload["sub"], "u1")
        self.assertEqual(payload["name"], "Alice")
        self.assertTrue(payload.get("jti"))

    def test_jti_unique(self):
        a = auth.read_token("sec", auth.sign_token("sec", "u1"))
        b = auth.read_token("sec", auth.sign_token("sec", "u1"))
        assert a is not None and b is not None
        self.assertNotEqual(a["jti"], b["jti"])

    def test_wrong_secret_rejected(self):
        token = auth.sign_token("a", "u1")
        self.assertIsNone(auth.read_token("b", token))

    def test_tampered_body_rejected(self):
        token = auth.sign_token("a", "u1")
        body, sig = token.rsplit(".", 1)
        mangled = body[:-4] + "quj0"
        self.assertIsNone(auth.read_token("a", f"{mangled}.{sig}"))

    def test_expired_rejected(self):
        token = auth.sign_token("a", "u1", ttl=0)
        self.assertIsNone(auth.read_token("a", token))

    def test_malformed_exp_rejected(self):
        # Валидная подпись, но exp — не число: read_token обязан вернуть None,
        # а не упасть (иначе 500 в api._session_user).
        body = auth._b64url(json.dumps({"sub": "u1", "exp": "not-an-int"}).encode("utf-8"))
        sig = _b64url(_hmac_sha256(b"sec", body.encode("ascii")))
        self.assertIsNone(auth.read_token("sec", f"{body}.{sig}"))


class TestMasterKey(unittest.TestCase):
    def test_right_key(self):
        self.assertTrue(auth.check_master_key("aa" * 32, "AA" * 32))

    def test_wrong_key(self):
        self.assertFalse(auth.check_master_key("aa" * 32, "bb" * 32))

    def test_empty(self):
        self.assertFalse(auth.check_master_key("", "x"))
        self.assertFalse(auth.check_master_key("x", ""))

    def test_hex_case_insensitive(self):
        # 64-hex мастер-ключ: регистр символов не должен мешать входу.
        self.assertTrue(auth.check_master_key("AbCd" * 16, "aBcD" * 16))

    def test_pin_case_sensitive(self):
        # PIN-деградация: downcasing уполовинил бы энтропию алфавитного пароля.
        self.assertTrue(auth.check_master_key("MyPin-2026", "MyPin-2026"))
        self.assertFalse(auth.check_master_key("MyPin-2026", "mypin-2026"))


class TestMasterKeyValidation(unittest.TestCase):
    def test_hex_key_ok(self):
        st = validate_master_key("a" * 64)
        self.assertTrue(st.ok)
        self.assertEqual(st.reason, "")

    def test_short_string_rejected(self):
        st = validate_master_key("short")
        self.assertFalse(st.ok)

    def test_empty_rejected(self):
        st = validate_master_key("")
        self.assertFalse(st.ok)

    def test_pin_ok_but_warns(self):
        st = validate_master_key("some-passphrase-123")
        self.assertTrue(st.ok)
        self.assertNotEqual(st.reason, "")


class TestTelegramInitData(unittest.TestCase):
    def _sample(self, token, user_id=123, auth_date=None):
        user = {"id": user_id, "first_name": "Test", "username": "tester"}
        raw = json.dumps(user)
        pairs = {
            "query_id": "AAFxyz",
            "user": raw,
            "auth_date": str(auth_date if auth_date is not None else int(time.time())),
        }
        check = "\n".join(f"{k}={v}" for k, v in sorted(pairs.items()))
        secret = _hmac_sha256(b"WebAppData", token.encode())
        pairs["hash"] = _hmac_sha256(secret, check.encode()).hex()
        return urlencode(pairs)

    def test_valid_signature(self):
        token = "123:secret"
        user = auth.validate_telegram_init_data(token, self._sample(token))
        assert user is not None
        self.assertEqual(user["id"], 123)

    def test_wrong_token_rejected(self):
        data = self._sample("123:secret")
        self.assertIsNone(auth.validate_telegram_init_data("999:other", data))

    def test_tampered_hash_rejected(self):
        data = self._sample("123:secret")
        data += "&hash=00000000000000000000000000000000"
        self.assertIsNone(auth.validate_telegram_init_data("123:secret", data))

    def test_stale_init_data_rejected(self):
        stale = int(time.time()) - 8 * 24 * 3600
        data = self._sample("123:secret", auth_date=stale)
        self.assertIsNone(auth.validate_telegram_init_data("123:secret", data))

    def test_ten_hours_old_rejected(self):
        # Окно реплея — 10 минут (раньше 24ч): старые подписи не проходят.
        recent = int(time.time()) - 10 * 3600
        data = self._sample("123:secret", auth_date=recent)
        self.assertIsNone(auth.validate_telegram_init_data("123:secret", data))

    def test_one_minute_old_accepted(self):
        recent = int(time.time()) - 60
        data = self._sample("123:secret", auth_date=recent)
        self.assertIsNotNone(auth.validate_telegram_init_data("123:secret", data))

    def test_empty_rejected(self):
        self.assertIsNone(auth.validate_telegram_init_data("t", ""))


class TestConfigHelpers(unittest.TestCase):
    def test_google_auth_url(self):
        from portal.config import PortalConfig

        c = PortalConfig.__new__(PortalConfig)
        c.google_client_id = "id"
        c.google_client_secret = "secret"
        c.google_redirect_uri = ""
        c.public_base_url = "https://x.example"
        url = c.google_auth_url("zzz")
        self.assertIn("client_id=id", url)
        self.assertIn("state=zzz", url)
        self.assertIn("oauth2/v2/auth", url)

    def test_google_auth_url_pkce(self):
        from portal.config import PortalConfig

        c = PortalConfig.__new__(PortalConfig)
        c.google_client_id = "id"
        c.google_client_secret = "secret"
        c.google_redirect_uri = ""
        c.public_base_url = "https://x.example"
        url = c.google_auth_url("zzz", code_challenge="C0deChall")
        self.assertIn("code_challenge=C0deChall", url)
        self.assertIn("code_challenge_method=S256", url)

    def test_pkce_pair(self):
        verifier, challenge = auth.pkce_pair()
        self.assertTrue(len(verifier) >= 43)
        expected = _b64url(hashlib.sha256(verifier.encode("ascii")).digest())
        self.assertEqual(challenge, expected)

    def test_csrf_state_strict(self):
        state = auth.csrf_state("sec")
        self.assertTrue(auth.verify_csrf_state("sec", state))
        self.assertFalse(auth.verify_csrf_state("sec", ""))
        self.assertFalse(auth.verify_csrf_state("sec", "short"))
        self.assertFalse(auth.verify_csrf_state("sec", "a b c d"))

    def test_links_sanitized(self):
        import json
        import os
        import tempfile

        from portal.config import load_links

        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8") as fh:
            json.dump(
                {
                    "title": "<b>X</b>",
                    "items": [
                        {"label": "ok", "url": "https://ok.example"},
                        {"label": "bad", "url": "javascript:alert(1)"},
                        {"label": "", "url": "https://empty.example"},
                    ],
                },
                fh,
            )
            path = fh.name
        try:
            links = load_links(path)
            self.assertEqual(len(links["items"]), 1)
            self.assertEqual(links["items"][0]["url"], "https://ok.example")
        finally:
            os.unlink(path)


if __name__ == "__main__":
    unittest.main()
