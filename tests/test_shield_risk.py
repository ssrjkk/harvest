"""Тесты RiskEngine: математика затухания, пороги, сигналы."""

import asyncio
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from portal.shield.config import RiskConfig
from portal.shield.risk import HIGH, LOW, MEDIUM, Observation, RiskEngine


def _cfg(**kw):
    base = dict(
        half_life_s=300.0, medium_threshold=25.0, high_threshold=50.0,
        w_fast_interval=2.0, tarpit_min_s=0.01, tarpit_max_s=0.02,
    )
    base.update(kw)
    return RiskConfig(**base)


class TestRiskMath(unittest.IsolatedAsyncioTestCase):
    async def test_decay_halves_score(self):
        eng = RiskEngine(_cfg(half_life_s=0.05))
        for _ in range(5):
            await eng.observe("u")
        s0 = await eng.score_of("u")
        self.assertGreater(s0, 0)
        await asyncio.sleep(0.07)
        s1 = await eng.score_of("u")
        # half-life 50мс: за 70мс счёт должен заметно просесть, но не в ноль.
        self.assertLess(s1, s0 * 0.7)
        self.assertGreater(s1, 0)

    async def test_unknown_subject_zero(self):
        eng = RiskEngine(_cfg())
        self.assertEqual(await eng.score_of("nobody"), 0.0)

    async def test_tarpit_scales_with_score(self):
        self.assertAlmostEqual(
            RiskEngine.tarpit_delay(25.0, 25.0, 50.0, 1.0, 3.0), 1.0)
        self.assertAlmostEqual(
            RiskEngine.tarpit_delay(50.0, 25.0, 50.0, 1.0, 3.0), 3.0)
        self.assertAlmostEqual(
            RiskEngine.tarpit_delay(37.5, 25.0, 50.0, 1.0, 3.0), 2.0)

    async def test_metronome_triggers(self):
        eng = RiskEngine(_cfg())
        v, _rs = await eng.observe("m")
        self.assertEqual(v.level, LOW)
        for _ in range(40):
            v, _rs = await eng.observe("m", Observation(payload_hash="same"))
        self.assertEqual(v.level, HIGH)
        self.assertTrue(v.shadowban)

    async def test_shadowban_can_be_disabled(self):
        eng = RiskEngine(_cfg(shadowban_enabled=False))
        v = None
        for _ in range(60):
            v, _rs = await eng.observe("s", Observation(payload_hash="x"))
        self.assertEqual(v.level, HIGH)
        self.assertFalse(v.shadowban)

    async def test_medium_tarpit_range(self):
        eng = RiskEngine(_cfg(medium_threshold=4.0, high_threshold=100.0))
        v, _rs = await eng.observe("t")
        self.assertEqual(v.level, LOW)
        for _ in range(3):
            v, _rs = await eng.observe("t")
        self.assertEqual(v.level, MEDIUM)
        self.assertGreaterEqual(v.tarpit_s, 0.01)
        self.assertLessEqual(v.tarpit_s, 0.02)

    async def test_reset_and_evict(self):
        eng = RiskEngine(_cfg())
        await eng.observe("gone")
        await eng.reset("gone")
        self.assertEqual(await eng.score_of("gone"), 0.0)
        await eng.observe("old")
        eng._states["old"].last_ts -= 5000.0
        self.assertEqual(await eng.evict(older_than_s=60.0), 1)

    async def test_state_cap_evicts_oldest(self):
        from portal.shield.risk import SubjectState

        eng = RiskEngine(_cfg())
        now = __import__("time").monotonic()
        # Забиваем под завязку старыми субъектами + один свежий.
        for i in range(20_000):
            eng._states[f"s{i}"] = SubjectState(score=0.0, last_ts=now - 9999.0)
        eng._states["fresh"] = SubjectState(score=0.0, last_ts=now)
        await eng.observe("newcomer")
        self.assertLessEqual(len(eng._states), 20_001)
        self.assertIn("newcomer", eng._states)
        self.assertIn("fresh", eng._states)
        # Флуд свежими: первая чистка ничего не снимает — срабатывает срез старейших.
        eng2 = RiskEngine(_cfg())
        for i in range(20_000):
            eng2._states[f"f{i}"] = SubjectState(score=0.0, last_ts=now)
        await eng2.observe("late")
        self.assertLessEqual(len(eng2._states), 20_001)
        self.assertIn("late", eng2._states)

    async def test_payload_hash_stable(self):
        a = RiskEngine.payload_hash_of("u", "act", "body")
        b = RiskEngine.payload_hash_of("u", "act", "body")
        c = RiskEngine.payload_hash_of("u", "act", "other")
        self.assertEqual(a, b)
        self.assertNotEqual(a, c)


if __name__ == "__main__":
    unittest.main(verbosity=2)
