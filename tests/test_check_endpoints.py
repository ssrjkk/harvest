"""tools/check_endpoints.py: парсинг ответов, чтение конфигов."""

import sys
import unittest
from io import BytesIO
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tools.check_endpoints import _page_status, _rpc_chain_id, _rpc_list  # noqa: E402


class FakeResp:
    def __init__(self, payload: bytes, status: int = 200):
        self._payload = payload
        self.status = status

    def read(self, n=-1):
        return self._payload

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class TestCheckEndpoints(unittest.TestCase):
    def test_chain_hex_and_dec(self):
        for raw, want in ((b'{"result": "0xb6ae"}', "46766"), (b'{"result": 46630}', "46630")):
            with mock.patch("urllib.request.urlopen", return_value=FakeResp(raw)):
                got, note = _rpc_chain_id("http://x", 1)
                self.assertEqual(got, want)
                self.assertEqual(note, "ok")

    def test_chain_unreachable_and_bad_reply(self):
        with mock.patch("urllib.request.urlopen", side_effect=OSError("down")):
            got, note = _rpc_chain_id("http://x", 1)
            self.assertIsNone(got)
            self.assertIn("unreachable", note)
        with mock.patch("urllib.request.urlopen",
                        return_value=FakeResp(b'{"result": null}')):
            got, note = _rpc_chain_id("http://x", 1)
            self.assertIsNone(got)
            self.assertIn("bad reply", note)

    def test_chain_403_hint(self):
        import urllib.error
        err = urllib.error.HTTPError("http://x", 403, "Forbidden", {}, BytesIO(b""))
        with mock.patch("urllib.request.urlopen", side_effect=err):
            _, note = _rpc_chain_id("http://x", 1)
            self.assertIn("403", note)

    def test_rpc_list_from_shipped_configs(self):
        rows = _rpc_list()
        self.assertGreaterEqual(len(rows), 4)
        by_cfg = dict((c, (u, ch)) for c, u, ch in rows)
        self.assertEqual(by_cfg["config_arc.yaml"][1], 5042002)
        for _, url, _ in rows:
            self.assertTrue(url.startswith("https://"), url)

    def test_page_status_empty(self):
        self.assertIn("не задан", _page_status("", 1))


if __name__ == "__main__":
    unittest.main()
