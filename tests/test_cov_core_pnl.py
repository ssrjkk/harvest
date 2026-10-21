"""Coverage 100% for core/pnl.py — PnL-отчёт прогона (юнит-моки, без живого RPC)."""

import asyncio
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.pnl import (  # noqa: E402
    PnlTracker,
    _delta,
    _fmt_eth,
    build_report,
    describe,
    failure_reason,
    pick_sample,
    render_html,
    resolve_network_meta,
    snapshot,
    summarize_actions,
)


class _FakeNet:
    """Маленький фейк сети с заранее заданными балансами."""

    def __init__(self, by_addr: dict[str, float]):
        self._bal = dict(by_addr)

    async def get_balance(self, addr: str, refresh: bool = False) -> float:
        return self._bal[addr]


class _FakeDB:
    """Фейк БД с предзаданными действиями (уважает since_id)."""

    def __init__(self):
        self.snapshots = []

    async def get_actions_since(self, since_id: int = 0, limit: int = 5000):
        rows = [
            {"id": 1, "address": "0x0000000000000000000000000000000000000001",
             "action_type": "transfer", "tx_hash": "0x" + "a" * 62, "success": True,
             "timestamp": "2026-01-01 00:00:00", "details": "amount=0.1, to=0x2"},
            {"id": 2, "address": "0x0000000000000000000000000000000000000002",
             "action_type": "transfer", "tx_hash": "", "success": False,
             "timestamp": "2026-01-01 00:00:01", "details": "error=reverted; to=0x3, amount=0.2"},
            {"id": 3, "address": "0x0000000000000000000000000000000000000002",
             "action_type": "stake", "tx_hash": "0x" + "b" * 62, "success": True,
             "timestamp": "2026-01-01 00:00:02", "details": ""},
            {"id": 4, "address": "0x0000000000000000000000000000000000000003",
             "action_type": "transfer", "tx_hash": "0x" + "c" * 62, "success": True,
             "timestamp": "2026-01-01 00:00:03", "details": ""},
        ]
        return [r for r in rows if r["id"] > since_id][:limit]

    async def save_pnl_snapshot(self, snap: dict) -> None:
        self.snapshots.append(snap)


AFT1 = "0x0000000000000000000000000000000000000001"
AFT2 = "0x0000000000000000000000000000000000000002"
ADDRS = [AFT1, AFT2, "0x0000000000000000000000000000000000000003"]


def _seed_tracker_sample2():
    """Трекер с тремя метками на консистентных балансах."""
    net = _FakeNet({a: 1.0 for a in ADDRS})
    t = PnlTracker(ADDRS, sample=2, concurrency=1)
    asyncio.run(t.mark(net, "pre_faucet"))
    asyncio.run(t.mark(net, "pre_farm"))
    return t


class PnlTest(unittest.TestCase):
    def test_pick_sample(self):
        self.assertEqual(pick_sample(ADDRS, 0), ADDRS)
        self.assertEqual(pick_sample(ADDRS, 10), ADDRS)
        self.assertEqual(len(pick_sample(ADDRS, 2)), 2)
        self.assertEqual(len(pick_sample(ADDRS, 2)), 2)

    def test_resolve_network_meta(self):
        self.assertEqual(
            resolve_network_meta({"chain_id": 46630, "name": "N", "slug": "S",
                                  "currency": "ETH", "explorer": "E"}),
            {"name": "N", "slug": "S", "chain_id": 46630, "currency": "ETH", "explorer": "E"},
        )
        m = resolve_network_meta({"chain_id": 46630})
        self.assertEqual(m["name"], "Chain 46630")
        self.assertEqual(m["currency"], "ETH")
        self.assertEqual(m["explorer"], "https://explorer.testnet.chain.robinhood.com")
        self.assertEqual(resolve_network_meta({"chain_id": 123})["explorer"], "")

    def test_delta_fmt(self):
        self.assertIsNone(_delta(None, 1.0))
        self.assertIsNone(_delta(1.0, None))
        self.assertAlmostEqual(_delta(1.0, 2.0), 1.0)
        self.assertEqual(_fmt_eth(None), "—")
        self.assertEqual(_fmt_eth(1.5), "1.500000")
        self.assertEqual(_fmt_eth(1.5, 2), "1.50")

    def test_snapshot(self):
        res = asyncio.run(snapshot(_FakeNet({AFT1: 1.0, AFT2: 2.0}), [AFT1, AFT2], concurrency=2))
        self.assertEqual(res[AFT1], 1.0)
        self.assertEqual(res[AFT2], 2.0)
        self.assertEqual(asyncio.run(snapshot(_FakeNet({}), [])), {})

    def test_snapshot_errors(self):
        class _Flaky:
            async def get_balance(self, addr, refresh=False):
                raise ConnectionError("down")

        res = asyncio.run(snapshot(_Flaky(), [AFT1], concurrency=1))
        self.assertEqual(res[AFT1], None)

    def test_failure_reason(self):
        self.assertEqual(failure_reason("error=reverted; to=0x1, amount=0.1"), "reverted")
        self.assertEqual(failure_reason("error=no receipt (в mempool); to=0x1"), "no receipt (в mempool)")
        self.assertEqual(failure_reason("error=; to=0x1"), "причина не записана")
        # записи, сделанные до появления причины
        self.assertEqual(failure_reason("to=0x1, amount=0.1"), "причина не записана")
        self.assertEqual(failure_reason(""), "причина не записана")

    def test_summarize_actions(self):
        s = summarize_actions([], sample={AFT1})
        self.assertEqual(s["total_ok"], 0)
        self.assertEqual(s["total_fail"], 0)
        self.assertEqual(s["recent_txs"], [])
        self.assertEqual(s["per_wallet"], [])

        actions = asyncio.run(_FakeDB().get_actions_since())
        s = summarize_actions(actions, sample={AFT1, AFT2})
        self.assertEqual(s["total_ok"], 3)
        self.assertEqual(s["total_fail"], 1)
        self.assertEqual(s["recent_txs"], [actions[0], actions[2], actions[3]])
        by = dict(s["per_wallet"])
        self.assertEqual(by[AFT2], {"ok": 1, "fail": 1})
        # причины отказов собраны отдельно
        self.assertEqual(s["by_reason"], {"reverted": 1})

    def test_tracker(self):
        t = _seed_tracker_sample2()
        self.assertEqual(t.order, ["pre_faucet", "pre_farm"])

        net = _FakeNet({a: 2.0 for a in ADDRS})
        asyncio.run(t.mark(net, "post_run"))

        s1 = t.section("pre_faucet", "pre_farm")
        self.assertEqual(s1["measured"], len(t.addresses))

        s2 = t.section("nope", "pre_farm")
        self.assertIsNone(s2["sum_delta"])
        self.assertEqual(s2["measured"], 0)

        # пустая метка → ветка раннего выхода (строка 149)
        s3 = t.section("", "pre_farm")
        self.assertIsNone(s3["sum_delta"])
        self.assertEqual(s3["measured"], 0)

    def test_render_html_no_explorer(self):
        t = _seed_tracker_sample2()
        actions = {
            "by_type": {"transfer": {"ok": 2, "fail": 1}, "stake": {"ok": 1, "fail": 0}},
            "per_wallet": [("0x" + "a" * 40, {"ok": 1, "fail": 0})],
            "recent_txs": [{"tx_hash": "0x" + "b" * 62, "action_type": "transfer",
                           "address": "0x" + "c" * 40}],
            "total_ok": 3,
            "total_fail": 1,
        }
        doc = render_html(
            tracker=t,
            faucet_delta=0.5,
            farm_delta=-0.25,
            actions=actions,
            network={"name": "N", "slug": "S", "chain_id": 46630},
            explorer="",
            currency="ETH",
            out_path="pnl_report.html",
        )
        self.assertIn("<html", doc)
        self.assertIn("PnL", doc)
        self.assertIn("ссылок нет", doc)
        self.assertNotIn("/address/", doc)
        self.assertIn("<code>0xbbbbbbbbbbbbbbbb…</code>", doc)

    def test_render_html_failure_reasons(self):
        t = _seed_tracker_sample2()
        actions = {
            "by_type": {"transfer": {"ok": 1, "fail": 2}},
            "per_wallet": [],
            "recent_txs": [],
            "by_reason": {"reverted": 2},
            "total_ok": 1,
            "total_fail": 2,
        }
        doc = render_html(
            tracker=t,
            faucet_delta=0.0,
            farm_delta=0.0,
            actions=actions,
            network={"name": "N", "slug": "S", "chain_id": 46630},
            explorer="https://explorer.testnet.chain.robinhood.com",
            currency="ETH",
            out_path="pnl_report.html",
        )
        self.assertIn("Причины отказов", doc)
        self.assertIn("reverted", doc)

    def test_render_html_empty_sections(self):
        t = _seed_tracker_sample2()
        empty = {
            "by_type": {},
            "per_wallet": [],
            "recent_txs": [],
            "total_ok": 0,
            "total_fail": 0,
        }
        doc = render_html(
            tracker=t,
            faucet_delta=None,
            farm_delta=None,
            actions=empty,
            network={"name": "N", "slug": "S", "chain_id": 46630},
            explorer="https://explorer.testnet.chain.robinhood.com",
            currency="ETH",
            out_path="pnl_report.html",
        )
        # пустые секции действий и tx — честные заглушки «нет данных»
        self.assertEqual(doc.count("нет данных"), 2)

        # трекер без адресов: секция кошельков тоже пустая
        t0 = PnlTracker([], sample=5)
        doc0 = render_html(
            tracker=t0,
            faucet_delta=None,
            farm_delta=None,
            actions=empty,
            network={"name": "N", "slug": "S", "chain_id": 46630},
            explorer="https://explorer.testnet.chain.robinhood.com",
            currency="ETH",
            out_path="pnl_report.html",
        )
        self.assertEqual(doc0.count("нет данных"), 3)

        # пустые адрес/хеш — ветви addr_link/tx_link
        t1 = _seed_tracker_sample2()
        empty2 = {
            "by_type": {"transfer": {"ok": 1, "fail": 0}},
            "per_wallet": [],
            "recent_txs": [{"tx_hash": "", "action_type": "transfer", "address": ""}],
            "total_ok": 1,
            "total_fail": 0,
        }
        doc1 = render_html(
            tracker=t1,
            faucet_delta=0.0,
            farm_delta=0.0,
            actions=empty2,
            network={"name": "N", "slug": "S", "chain_id": 46630},
            explorer="https://explorer.testnet.chain.robinhood.com",
            currency="ETH",
            out_path="pnl_report.html",
        )
        self.assertIn('<span class="mut">—</span>', doc1)

    def test_render_html_with_explorer(self):
        t = _seed_tracker_sample2()
        actions = {
            "by_type": {"transfer": {"ok": 1, "fail": 0}},
            "per_wallet": [("0x" + "a" * 40, {"ok": 1, "fail": 0})],
            "recent_txs": [{"tx_hash": "0x" + "b" * 62, "action_type": "transfer",
                           "address": "0x" + "c" * 40}],
            "total_ok": 1,
            "total_fail": 0,
        }
        doc = render_html(
            tracker=t,
            faucet_delta=0.5,
            farm_delta=-0.25,
            actions=actions,
            network={"name": "N", "slug": "S", "chain_id": 46630},
            explorer="https://explorer.testnet.chain.robinhood.com",
            currency="ETH",
            out_path="pnl_report.html",
        )
        self.assertIn("explorer.testnet", doc)
        self.assertIn("/address/", doc)
        self.assertIn("/tx/", doc)

    def test_build_and_describe(self):
        async def run():
            t = PnlTracker(ADDRS, sample=2, concurrency=1)
            net = _FakeNet({a: 1.0 for a in ADDRS})
            await t.mark(net, "pre_faucet")
            await t.mark(net, "pre_farm")
            await t.mark(net, "post_run")
            with tempfile.TemporaryDirectory() as d:
                tmp = Path(d) / "report.html"
                summary = await build_report(
                    tracker=t,
                    db=_FakeDB(),
                    network={"name": "N", "slug": "S", "chain_id": 46630},
                    currency="ETH",
                    explorer="https://explorer.testnet.chain.robinhood.com",
                    out_path=str(tmp),
                )
                self.assertTrue(tmp.exists())
                self.assertGreater(tmp.stat().st_size, 100)
                desc = describe(summary)
                self.assertIn("report.html", desc)
                self.assertIn("3", desc)  # ok-счёт из сводки
                return desc
        self.assertIsInstance(asyncio.run(run()), str)

    def test_build_report_respects_since_id(self):
        """Отчёт считает только действия текущего прогона."""

        async def run():
            t = PnlTracker(ADDRS, sample=2, concurrency=1)
            net = _FakeNet({a: 1.0 for a in ADDRS})
            await t.mark(net, "pre_faucet")
            await t.mark(net, "pre_farm")
            await t.mark(net, "post_run")
            with tempfile.TemporaryDirectory() as d:
                tmp = Path(d) / "r.html"
                return await build_report(
                    tracker=t,
                    db=_FakeDB(),
                    network={"name": "N", "slug": "S", "chain_id": 46630},
                    currency="ETH",
                    explorer="",
                    out_path=str(tmp),
                    since_id=2,
                )

        summary = asyncio.run(run())
        # id>2 → только записи 3 и 4: два успешных, ни одной ошибки
        self.assertEqual(summary["ok"], 2)
        self.assertEqual(summary["fail"], 0)

    def test_build_report_saves_snapshot_without_addresses(self):
        """Снапшот для дашборда: агрегаты, без адресов кошельков."""

        async def run():
            t = PnlTracker(ADDRS, sample=2, concurrency=1)
            net = _FakeNet({a: 1.0 for a in ADDRS})
            await t.mark(net, "pre_faucet")
            await t.mark(net, "pre_farm")
            await t.mark(net, "post_run")
            db = _FakeDB()
            with tempfile.TemporaryDirectory() as d:
                tmp = Path(d) / "r.html"
                await build_report(
                    tracker=t,
                    db=db,
                    network={"name": "N", "slug": "S", "chain_id": 46630},
                    currency="ETH",
                    explorer="",
                    out_path=str(tmp),
                )
            return db.snapshots

        snaps = asyncio.run(run())
        self.assertEqual(len(snaps), 1)
        snap = snaps[0]
        self.assertEqual(snap["actions_ok"], 3)
        self.assertEqual(snap["actions_fail"], 1)
        self.assertEqual(snap["window_actions"], 4)
        self.assertEqual(snap["by_reason"], [{"reason": "reverted", "count": 1}])
        self.assertEqual(snap["by_type"][0]["type"], "transfer")
        # ни одного адреса в снапшоте быть не должно
        blob = json.dumps(snap, ensure_ascii=False)
        for addr in ADDRS:
            self.assertNotIn(addr, blob)

    def test_build_report_survives_snapshot_failure(self):
        """Ошибка записи снапшота не должна ломать отчёт."""

        async def run():
            t = PnlTracker(ADDRS, sample=2, concurrency=1)
            net = _FakeNet({a: 1.0 for a in ADDRS})
            await t.mark(net, "pre_faucet")
            await t.mark(net, "pre_farm")
            await t.mark(net, "post_run")

            class _BrokenDB(_FakeDB):
                async def save_pnl_snapshot(self, snap: dict) -> None:
                    raise RuntimeError("db down")

            with tempfile.TemporaryDirectory() as d:
                tmp = Path(d) / "r.html"
                summary = await build_report(
                    tracker=t,
                    db=_BrokenDB(),
                    network={"name": "N", "slug": "S", "chain_id": 46630},
                    currency="ETH",
                    explorer="",
                    out_path=str(tmp),
                )
                self.assertTrue(tmp.exists())
                return summary

        summary = asyncio.run(run())
        self.assertEqual(summary["ok"], 3)


if __name__ == "__main__":
    unittest.main()
