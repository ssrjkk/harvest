"""Цикл фарминга для одного кошелька.

Использует общие (на весь пул) network/vibevibe/db/faucet/executor —
не создаёт свой RPC-провайдер на каждый кошелёк.
"""

import logging
import random

from core import utils
from core.actions import ActionExecutor
from core.behavior import WalletProfile, day_of_week_factor, hour_window_factor, time_of_day_factor
from core.faucet import Faucet
from core.network import NetworkManager

logger = logging.getLogger(__name__)


class Farmer:
    def __init__(
        self,
        wallet: dict,
        config: dict,
        network: NetworkManager,
        executor: ActionExecutor,
        faucet: Faucet,
        all_addresses: list,
        profile: WalletProfile | None = None,
    ):
        self.wallet = wallet
        self.network = network
        self.faucet = faucet
        self.executor = executor
        self.all_addresses = all_addresses
        self.profile = profile if profile is not None else WalletProfile(neutral=True)
        self.farming = config["farming"]
        self.advanced = config.get("advanced", {})
        self._consecutive_failures = 0
        self._max_consecutive_failures = self.advanced.get("max_consecutive_failures", 10)

    def _skip_probability(self) -> float:
        base = self.farming.get("skip_cycle_probability", 0.05)
        if self.profile.neutral:
            return max(0.0, base)
        # Активность: выше activity — реже пропускает. Время суток, день недели
        # и личное окно активности добавляют естественную неровность.
        p = (
            base
            * (2.0 - self.profile.activity)
            * time_of_day_factor(self.profile.daily_window_strength)
            * day_of_week_factor(self.profile.weekend_strength)
            * hour_window_factor(self.profile)
        )
        return max(0.0, min(1.0, p))

    def _actions_count(self) -> int:
        raw_apt = self.farming["actions_per_cycle"]
        if isinstance(raw_apt, (list, tuple)):
            try:
                a, b = int(raw_apt[0]), int(raw_apt[1])
            except (TypeError, ValueError, IndexError):
                a = b = 1
            # Перевёрнутая пара [8,3] роняла randint с ValueError при
            # нейтральном профиле (валидатор такое ловит, прямой вызов — нет).
            min_act, max_act = (a, b) if a <= b else (b, a)
        else:
            try:
                min_act = max_act = int(raw_apt)
            except (TypeError, ValueError):
                min_act = max_act = 1
        if self.profile.neutral:
            return random.randint(min_act, max_act)
        lo = max(min_act, self.profile.actions_lo)
        hi = min(max_act, self.profile.actions_lo + self.profile.actions_width - 1)
        if hi < lo:
            hi = lo
        num = random.randint(lo, hi)
        # Редкий «всплеск» активности: кошелёк делает больше действий, чем обычно.
        if random.random() < self.profile.burst_prob:
            num = min(max(max_act, lo) * 2, max(1, int(num * self.profile.burst_mult)))
        return num

    def _action_delay_bounds(self) -> tuple[float, float]:
        raw_dly = self.farming["delay_between_actions"]
        if isinstance(raw_dly, (list, tuple)):
            try:
                a, b = float(raw_dly[0]), float(raw_dly[1])
            except (TypeError, ValueError, IndexError):
                a = b = 5.0
            min_d, max_d = (a, b) if a <= b else (b, a)
        else:
            try:
                min_d = max_d = float(raw_dly)
            except (TypeError, ValueError):
                min_d = max_d = 5.0
        if self.profile.neutral:
            return min_d, max_d
        scale = self.profile.delay_actions_scale * random.uniform(0.85, 1.15)
        return min_d * scale, max_d * scale

    async def run_cycle(self) -> int:
        address = self.wallet["address"]
        force = self.advanced.get("force_farm", False)

        # имитация человека: иногда пропускаем цикл. Проверяем ДО баланса/крана,
        # чтобы не тратить rate-лимитированный запрос крана и RPC-вызов впустую.
        if random.random() < self._skip_probability():
            logger.info(f"Кошелёк {address[:10]} случайно пропустил цикл")
            return 0

        balance = await self.network.get_balance(address)
        if balance < self.faucet.min_balance:
            if force:
                logger.debug(f"{address[:10]} баланс {balance:.6f}, force_farm — продолжаю")
            elif not self.faucet.enabled:
                logger.warning(f"{address[:10]} баланс низкий, кран отключён, пропуск")
                return 0
            elif not self.profile.neutral and random.random() < self.profile.faucet_skip_prob:
                # Индивидуальная привычка: часть кошельков не каждый раз идёт за краном.
                logger.info(f"{address[:10]} по профилю пропустил кран, цикл пропущен")
                return 0
            else:
                logger.info(f"{address[:10]} баланс {balance:.6f}, запрашиваю кран")
                funded = await self.faucet.ensure_balance(self.network, address)
                if not funded:
                    if force:
                        logger.debug(f"{address[:10]} кран не сработал, force_farm — продолжаю")
                    else:
                        logger.warning(f"{address[:10]} не удалось пополнить, пропуск")
                        return 0

        # Gate от RPC-шторма: с нулевым балансом ни одна транзакция не пройдёт
        # (have 0 want ...), даже с force_farm — пропускаем цикл, а не жжём квоту.
        if force:
            try:
                _bal_now = await self.network.get_balance(address)
            except Exception:  # noqa: BLE001
                _bal_now = balance
            if _bal_now <= 0:
                logger.warning(f"{address[:10]} баланс 0, force_farm — пропуск (нет газа)")
                return 0

        num_actions = self._actions_count()
        min_d, max_d = self._action_delay_bounds()
        success_count = 0
        min_gas = self.advanced.get("min_gas_for_action", 0.003)
        _force_balance_logged = False

        for i in range(num_actions):
            # Max consecutive failures: если слишком много подряд ошибок — стоп
            if self._consecutive_failures >= self._max_consecutive_failures:
                logger.warning(f"{address[:10]} {self._consecutive_failures} подряд ошибок, стоп")
                break

            if self.advanced.get("check_balance_before_action", True) and not force:
                bal = await self.network.get_balance(address)
                if bal < min_gas:
                    logger.warning(f"{address[:10]} баланс {bal:.6f} < {min_gas}, скип действия")
                    break
            elif force:
                # force_farm пропускает порог min_gas, но не нулевой баланс:
                # одна проверка на цикл, дальше — без RPC на каждую итерацию.
                if not _force_balance_logged:
                    logger.debug(f"{address[:10]} force_farm — баланс-чек пропущен (кроме 0)")
                    _force_balance_logged = True

            tf = getattr(self.network, "transport_failures", None)
            tf_before = tf() if callable(tf) else 0
            if self.profile.neutral:
                ok = await self.executor.execute_action(self.wallet, self.all_addresses)
            else:
                ok = await self.executor.execute_action(self.wallet, self.all_addresses, profile=self.profile)
            if not ok and self.advanced.get("auto_replay", True):
                # Ротация RPC — только при транспортной проблеме: breaker считает
                # лишь retryable-ошибки сети, логический фейл (revert/конфиг)
                # ротацией не лечится — повторяем на той же ноде с новыми
                # случайными параметрами действия.
                tf_after = tf() if callable(tf) else tf_before
                if tf_after > tf_before:
                    logger.debug(f"{address[:10]} auto-replay: transport trouble, retrying with RPC rotation")
                    try:
                        await self.network._switch_rpc()
                    except Exception:
                        pass
                else:
                    logger.debug(f"{address[:10]} auto-replay: logic failure, retrying on same node")
                if self.profile.neutral:
                    ok = await self.executor.execute_action(self.wallet, self.all_addresses)
                else:
                    ok = await self.executor.execute_action(self.wallet, self.all_addresses, profile=self.profile)
            if ok:
                success_count += 1
                self._consecutive_failures = 0
            else:
                self._consecutive_failures += 1

            # задержка между действиями (кроме последней)
            if i < num_actions - 1:
                await utils.asleep(min_d, max_d)

        return success_count
