"""Тесты разведки сетей (core/netdiscovery.py) — только чистые функции,
без сети. Покрывают сборку конфига, агрегацию контрактов и отчёт.
"""

import sys
import tempfile
import unittest
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.netdiscovery import (
    NETWORK_REGISTRY,
    ContractHint,
    DiscoverReport,
    NetworkCandidate,
    ProbeResult,
    _action_from_hint,
    _candidate_for,
    _fmt_probe,
    _is_contract_addr,
    aggregate_contracts,
    build_config,
    format_report,
    write_config,
)

_ADDR = "0x" + "ab" * 20
_ADDR2 = "0x" + "cd" * 20


def _probe(chain_id: int = 46630, gas: int | None = 10**9, cand_chain: int | None = None) -> ProbeResult:
    cc = chain_id if cand_chain is None else cand_chain
    cand = NetworkCandidate(
        id="t", name="TestNet", chain_id=cc, symbol="TST",
        rpc_urls=("https://rpc.test",), explorer="https://explorer.test",
    )
    return ProbeResult(
        candidate=cand, rpc_url="https://rpc.test", chain_id=chain_id,
        block_number=100, gas_price_wei=gas, latency_ms=12.5,
    )


class TestHelpers(unittest.TestCase):
    def test_candidate_for_known(self):
        known = NETWORK_REGISTRY[0]
        self.assertEqual(_candidate_for(known.chain_id), known)

    def test_candidate_for_unknown_is_synthetic(self):
        c = _candidate_for(999999)
        self.assertEqual(c.chain_id, 999999)
        self.assertEqual(c.id, "chain-999999")
        self.assertEqual(c.symbol, "ETH")

    def test_is_contract_addr(self):
        self.assertTrue(_is_contract_addr(_ADDR))
        self.assertFalse(_is_contract_addr(None))
        self.assertFalse(_is_contract_addr("0x123"))
        self.assertFalse(_is_contract_addr("nothex"))
        self.assertFalse(_is_contract_addr("0x" + "0" * 40))

    def test_probe_chain_ok(self):
        self.assertTrue(_probe(chain_id=46630, cand_chain=46630).chain_ok)
        self.assertFalse(_probe(chain_id=1, cand_chain=2).chain_ok)


class TestAggregate(unittest.TestCase):
    def test_aggregate_counts_and_selectors(self):
        blocks = [
            {"transactions": [
                {"to": _ADDR, "input": "0xdeadbeef" + "00" * 10},
                {"to": _ADDR, "input": "0xdeadbeef" + "11" * 10},
                {"to": _ADDR2, "input": "0xcafebabe" + "00" * 10},
                {"to": None, "input": "0x"},
                "garbage",
            ]},
            None,
        ]
        hints = aggregate_contracts(blocks)
        self.assertEqual(hints[0].address, _ADDR.lower())
        self.assertEqual(hints[0].calls, 2)
        self.assertEqual(hints[0].top_selectors[0], "0xdeadbeef")
        self.assertEqual(hints[1].calls, 1)

    def test_aggregate_empty(self):
        self.assertEqual(aggregate_contracts([None, {}, {"transactions": None}]), [])

    def test_action_from_hint(self):
        h = ContractHint(address=_ADDR, calls=5, selectors=Counter({"0xaaaa": 3}))
        a = _action_from_hint(h)
        self.assertEqual(a["type"], "contract_call")
        self.assertEqual(a["contract"], _ADDR)
        self.assertEqual(a["method"], "0xaaaa")
        self.assertIn("min_amount", a)

    def test_action_from_hint_no_selectors(self):
        h = ContractHint(address=_ADDR, calls=1, selectors=Counter())
        self.assertNotIn("method", _action_from_hint(h))


class TestBuildConfig(unittest.TestCase):
    def test_build_config_minimal(self):
        cfg = build_config(_probe())
        self.assertEqual(cfg["network"]["chain_id"], 46630)
        self.assertEqual(cfg["network"]["currency"], "TST")
        self.assertEqual(cfg["actions"][0]["type"], "transfer")
        self.assertNotIn("meta", cfg["network"])

    def test_build_config_with_hints(self):
        h = ContractHint(address=_ADDR, calls=3, selectors=Counter({"0xaaaa": 2}))
        cfg = build_config(_probe(), [h])
        types = [a["type"] for a in cfg["actions"]]
        self.assertIn("contract_call", types)
        self.assertEqual(cfg["network"]["meta"]["block"], 100)
        self.assertEqual(cfg["network"]["meta"]["hints"][0]["address"], _ADDR)


class TestReportAndWrite(unittest.TestCase):
    def test_fmt_probe(self):
        self.assertIn("OK", _fmt_probe(_probe()))
        self.assertIn("MISMATCH", _fmt_probe(_probe(chain_id=1, cand_chain=2)))

    def test_format_report_empty(self):
        self.assertIn("ни одна сеть", format_report(DiscoverReport()))

    def test_format_report_full(self):
        rep = DiscoverReport(
            live=[_probe()],
            hints=[ContractHint(address=_ADDR, calls=2, selectors=Counter({"0xbb": 1}))],
            config_path="cfg.yaml",
            config={"actions": [{"type": "transfer"}]},
            error="oops",
        )
        text = format_report(rep)
        self.assertIn("живые", text)
        self.assertIn("ТОП-1", text)
        self.assertIn("Конфиг записан", text)
        self.assertIn("ОШИБКА: oops", text)

    def test_write_config(self):
        with tempfile.TemporaryDirectory() as d:
            out = str(Path(d) / "auto.yaml")
            self.assertEqual(write_config({"network": {"chain_id": 1}}, out), out)
            self.assertIn("chain_id", Path(out).read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
