"""Тесты Farmer.run_cycle: ветвления баланса, force_farm, цепочки отказов."""

import sys
import unittest
from pathlib import Path
from typing import cast

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.actions import ActionExecutor
from core.farmer import Farmer
from core.faucet import Faucet
from core.network import NetworkManager


class FakeNetwork:
    def __init__(self, balance: float = 1.0):
        self.balance = balance

    async def get_balance(self, address, refresh=False):
        return self.balance


class FakeFaucet:
    def __init__(self, min_balance: float = 1.0, enabled: bool = True, funded: bool = True):
        self.min_balance = min_balance
        self.enabled = enabled
        self.funded = funded
        self.ensure_calls = 0

    async def ensure_balance(self, network, address):
        self.ensure_calls += 1
        return self.funded


class FakeExecutor:
    def __init__(self, outcome: bool = True):
        self.outcome = outcome
        self.calls = 0

    async def execute_action(self, wallet, all_addresses):
        self.calls += 1
        return self.outcome


def _farmer(balance=1.0, funded=True, force_farm=False, check_balance=True, actions=3, fail_after=None, skip=0.0):
    cfg = {
        "farming": {
            "actions_per_cycle": [actions, actions],
            "delay_between_actions": 0,
            "skip_cycle_probability": skip,
        },
        "advanced": {
            "force_farm": force_farm,
            "max_consecutive_failures": 5,
            "min_gas_for_action": 0.0,
            "check_balance_before_action": check_balance,
        },
    }
    wallet = {"address": "0x" + "1" * 40}
    network = FakeNetwork(balance)
    faucet = FakeFaucet(min_balance=1.0, funded=funded)
    executor = FakeExecutor(outcome=True if fail_after is None else False)
    farmer = Farmer(
        wallet,
        cfg,
        cast(NetworkManager, network),
        cast(ActionExecutor, executor),
        cast(Faucet, faucet),
        [wallet["address"]],
    )
    return farmer, network, faucet, executor


class TestRunCycleSuccess(unittest.TestCase):
    async def _run(self, **kw):
        farmer, _, _, executor = _farmer(**kw)
        res = await farmer.run_cycle()
        return res, executor

    def test_skip_cycle_does_not_waste_faucet(self):
        # Пропущенный цикл не должен тратить rate-лимитированный запрос крана,
        # даже если баланс кошелька низкий.
        farmer, _, faucet, executor = _farmer(balance=0.1, funded=True, skip=1.0)
        self.assertEqual(asyncio_run(lambda: farmer.run_cycle()), 0)
        self.assertEqual(faucet.ensure_calls, 0)
        self.assertEqual(executor.calls, 0)

    def test_sufficient_balance_runs_all_actions(self):
        res, executor = asyncio_run(lambda: self._run())
        self.assertEqual(res, 3)
        self.assertEqual(executor.calls, 3)

    def test_sufficient_balance_runs_actions_keeps_success_count(self):
        res, executor = asyncio_run(lambda: self._run(actions=7))
        self.assertEqual(res, 7)
        self.assertEqual(executor.calls, 7)

    def test_low_balance_and_faucet_fails_skips(self):
        res, executor = asyncio_run(lambda: self._run(balance=0.1, funded=False))
        self.assertEqual(res, 0)
        self.assertEqual(executor.calls, 0)

    def test_low_balance_faucet_passes(self):
        res, executor = asyncio_run(lambda: self._run(balance=0.1, funded=True))
        self.assertEqual(res, 3)
        self.assertEqual(executor.calls, 3)

    def test_faucet_disabled_and_balance_low_skips(self):
        async def main():
            cfg = {
                "farming": {
                    "actions_per_cycle": [3, 3],
                    "delay_between_actions": 0,
                    "skip_cycle_probability": 0.0,
                },
                "advanced": {"force_farm": False, "max_consecutive_failures": 5},
            }
            wallet = {"address": "0x" + "2" * 40}
            network = FakeNetwork(0.01)
            faucet = FakeFaucet(min_balance=1.0, enabled=False)
            executor = FakeExecutor()
            farmer = Farmer(
                wallet,
                cfg,
                cast(NetworkManager, network),
                cast(ActionExecutor, executor),
                cast(Faucet, faucet),
                [wallet["address"]],
            )
            res = await farmer.run_cycle()
            self.assertEqual(res, 0)
            self.assertEqual(executor.calls, 0)

        asyncio_run(main)

    def test_force_farm_overrides_low_balance(self):
        res, executor = asyncio_run(lambda: self._run(balance=0.01, force_farm=True))
        self.assertEqual(res, 3)
        self.assertEqual(executor.calls, 3)

    def test_consecutive_failures_break_early(self):
        async def main():
            cfg = {
                "farming": {
                    "actions_per_cycle": [10, 10],
                    "delay_between_actions": 0,
                    "skip_cycle_probability": 0.0,
                },
                "advanced": {
                    "force_farm": False,
                    "max_consecutive_failures": 2,
                    "check_balance_before_action": False,
                    "auto_replay": False,
                },
            }
            wallet = {"address": "0x" + "3" * 40}
            network = FakeNetwork(1.0)
            faucet = FakeFaucet(min_balance=1.0)
            executor = FakeExecutor(outcome=False)
            farmer = Farmer(
                wallet,
                cfg,
                cast(NetworkManager, network),
                cast(ActionExecutor, executor),
                cast(Faucet, faucet),
                [wallet["address"]],
            )
            res = await farmer.run_cycle()
            self.assertEqual(res, 0)
            self.assertEqual(executor.calls, 2)

        asyncio_run(main)


class FakeNetworkWithSwitch:
    def __init__(self, balance: float = 1.0):
        self.balance = balance
        self.switch_calls = 0

    async def get_balance(self, address, refresh=False):
        return self.balance

    async def _switch_rpc(self):
        self.switch_calls += 1


class FakeExecutorVariable:
    """Executor that fails first N calls, then succeeds."""
    def __init__(self, fail_count: int = 0):
        self.fail_count = fail_count
        self.calls = 0

    async def execute_action(self, wallet, all_addresses, profile=None):
        self.calls += 1
        if self.calls <= self.fail_count:
            return False
        return True


class TestAutoReplay(unittest.TestCase):
    def test_auto_replay_retries_on_failure(self):
        async def main():
            cfg = {
                "farming": {
                    "actions_per_cycle": [1, 1],
                    "delay_between_actions": 0,
                    "skip_cycle_probability": 0.0,
                },
                "advanced": {
                    "force_farm": False,
                    "max_consecutive_failures": 5,
                    "check_balance_before_action": False,
                    "auto_replay": True,
                },
            }
            wallet = {"address": "0x" + "4" * 40}
            network = FakeNetworkWithSwitch(1.0)
            faucet = FakeFaucet(min_balance=1.0)
            executor = FakeExecutorVariable(fail_count=1)
            farmer = Farmer(
                wallet,
                cfg,
                cast(NetworkManager, network),
                cast(ActionExecutor, executor),
                cast(Faucet, faucet),
                [wallet["address"]],
            )
            res = await farmer.run_cycle()
            self.assertEqual(res, 1)
            self.assertEqual(executor.calls, 2)
            self.assertEqual(network.switch_calls, 1)

        asyncio_run(main)

    def test_auto_replay_disabled(self):
        async def main():
            cfg = {
                "farming": {
                    "actions_per_cycle": [1, 1],
                    "delay_between_actions": 0,
                    "skip_cycle_probability": 0.0,
                },
                "advanced": {
                    "force_farm": False,
                    "max_consecutive_failures": 5,
                    "check_balance_before_action": False,
                    "auto_replay": False,
                },
            }
            wallet = {"address": "0x" + "5" * 40}
            network = FakeNetworkWithSwitch(1.0)
            faucet = FakeFaucet(min_balance=1.0)
            executor = FakeExecutorVariable(fail_count=1)
            farmer = Farmer(
                wallet,
                cfg,
                cast(NetworkManager, network),
                cast(ActionExecutor, executor),
                cast(Faucet, faucet),
                [wallet["address"]],
            )
            res = await farmer.run_cycle()
            self.assertEqual(res, 0)
            self.assertEqual(executor.calls, 1)
            self.assertEqual(network.switch_calls, 0)

        asyncio_run(main)

    def test_auto_replay_with_profile(self):
        async def main():
            from core.behavior import WalletProfile
            cfg = {
                "farming": {
                    "actions_per_cycle": [1, 1],
                    "delay_between_actions": 0,
                    "skip_cycle_probability": 0.0,
                },
                "advanced": {
                    "force_farm": False,
                    "max_consecutive_failures": 5,
                    "check_balance_before_action": False,
                    "auto_replay": True,
                },
            }
            wallet = {"address": "0x" + "6" * 40}
            network = FakeNetworkWithSwitch(1.0)
            faucet = FakeFaucet(min_balance=1.0)
            executor = FakeExecutorVariable(fail_count=1)
            profile = WalletProfile(neutral=False)
            farmer = Farmer(
                wallet,
                cfg,
                cast(NetworkManager, network),
                cast(ActionExecutor, executor),
                cast(Faucet, faucet),
                [wallet["address"]],
                profile=profile,
            )
            res = await farmer.run_cycle()
            self.assertEqual(res, 1)
            self.assertEqual(executor.calls, 2)

        asyncio_run(main)


def asyncio_run(fn):
    import asyncio

    return asyncio.run(fn())


if __name__ == "__main__":
    unittest.main(verbosity=2)
