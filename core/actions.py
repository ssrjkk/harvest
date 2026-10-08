"""Выполнение действий: трансфер и вызовы контрактов VibeVibe.

Если контрактные действия (swap/mint) не настроены (адрес заглушка 0x0...),
автоматически подставляется реальный on-chain трансфер между своими кошельками,
чтобы фарм всегда давал транзакции даже без готовых адресов контрактов.
"""

import logging
import os
import random
import secrets
from collections import deque

from web3 import Web3

from core.batchwriter import BatchWriter
from core.behavior import WalletProfile, gas_multiplier
from core.database import Database
from core.network import NetworkManager
from core.vibevibe import _TRADE_ACTION_BUY, _TRADE_ACTION_SELL, VibeVibeInterface

logger = logging.getLogger(__name__)

# Все контрактные действия (любая сеть) — обрабатываются через vibevibe.call_method
CONTRACT_ACTIONS = frozenset(
    {
        "vibevibe_swap",
        "vibevibe_mint",
        # Универсальный вызов найденного контракта (value или 4-байт селектор)
        "contract_call",
        "flop_compute",
        "flop_validate",
        "flop_stake",
        "arc_launch",
        "arc_trade",
        "arc_add_liquidity",
        # VibePassMarket: buy/sell через EIP-712 подпись tradeSigner
        "vibevibe_buy",
        "vibevibe_sell",
        # VibeLoungeRegistry: registerLounge (только REGISTRAR_ROLE)
        "vibevibe_register",
    }
)

# Адаптивные веса: сколько образцов нужно, чтобы «отключить» стабильно падающий
# тип действия, и на каком окне история успеха.
_ADAPT_MIN_SAMPLES = 5
_OUTCOME_WINDOW = 20


def _is_zero(addr) -> bool:
    """True только для пустого значения или настоящего нулевого адреса (0x0...0)."""
    if not addr:
        return True
    try:
        return int(addr, 16) == 0
    except ValueError:
        return False


def _is_valid_contract(addr) -> bool:
    """True если это настоящий 0x-адрес (40 hex), а не заглушка/мусор."""
    if not addr or _is_zero(addr):
        return False
    return Web3.is_address(addr)


def _fake_tx_hash() -> str:
    # secrets, а не random: Mersenne Twister предсказуем, а хэш показывается
    # как «подтверждённая» транзакция — коллизии/предсказуемость недопустимы.
    return "0x" + secrets.token_hex(32)


def _normalize_action(action_conf: dict) -> dict:
    """Возвращает действие, гарантированно пригодное для выполнения.

    Контрактное действие без настроенного адреса контракта (0x0... или пусто)
    не может быть отправлено. Вместо того чтобы при каждом выборе сваливаться в
    жёстко зашитые 0.0001..0.0005, заменяем его на transfer, сохраняя заданные
    в конфиге min/max_amount (если были) — поведение предсказуемо и без сюрпризов.
    """
    atype = action_conf.get("type", "")
    if atype not in CONTRACT_ACTIONS:
        return action_conf
    if _is_valid_contract(action_conf.get("contract", "")):
        return action_conf
    logger.warning(
        f"Действие {atype}: адрес контракта не задан (0x0...), заменено на transfer. Заполните contract в config.yaml."
    )
    return {
        "type": "transfer",
        "target": "random_wallet",
        "min_amount": action_conf.get("min_amount", 0.0001),
        "max_amount": action_conf.get("max_amount", 0.001),
    }


def _adaptive_scores(weights: list[float], outcomes: list[list[bool]]) -> list[float]:
    """Взвешивает типы действий по истории успеха.

    - Нет истории → вес как в конфиге.
    - Успешный тип усиливается (до 1.5x базового).
    - Падающий тип ослабляется (до 0.25x базового).
    - Тип с >= _ADAPT_MIN_SAMPLES попытками и НИ ОДНОГО успеха — обнуляется:
      воркер не тратит RPC впустую на заведомо мёртвое действие.
    - Если всё обнулилось (аномалия) — возвращаем равные веса.
    """
    scores = []
    for w, rec in zip(weights, outcomes, strict=False):
        w = max(float(w), 0.0)
        if rec:
            n = len(rec)
            ok = sum(1 for r in rec if r)
            rate = ok / n
            if n >= _ADAPT_MIN_SAMPLES and ok == 0:
                w = 0.0
            else:
                w *= 0.5 + rate
        scores.append(w)
    if sum(scores) <= 0:
        scores = [1.0] * len(scores)
    return scores


class ActionExecutor:
    def __init__(
        self,
        network: NetworkManager,
        vibevibe: VibeVibeInterface,
        db: Database,
        config: dict,
    ) -> None:
        self.network = network
        self.vibevibe = vibevibe
        # Конфиг действий нормализуем один раз при старте: действия с заглушкой
        # контракта превращаются в transfer, чтобы _pick_action не выбирал
        # заведомо нерабочие варианты и не спотыкался на fallback каждый раз.
        self.actions_conf = [_normalize_action(a) for a in (config.get("actions") or [])]
        # Валидация диапазонов сумм при старте: random.uniform молча инвертирует
        # min>max, порождая суммы вопреки ожиданиям оператора.
        for a in self.actions_conf:
            if "min_amount" in a and "max_amount" in a:
                lo, hi = a["min_amount"], a["max_amount"]
                if lo > hi:
                    logger.warning(
                        f"Действие {a.get('type')}: min_amount={lo} > max_amount={hi} — диапазон инвертирован"
                    )
                    a["min_amount"], a["max_amount"] = hi, lo
        # История исходов действий по индексу нормализованного конфига —
        # веса становятся адаптивными (см. _adaptive_scores).
        self._outcomes: list[deque] = [deque(maxlen=_OUTCOME_WINDOW) for _ in self.actions_conf]
        self.gas_limit = config["advanced"].get("gas_limit", 21000)
        self.dry_run = config.get("advanced", {}).get("dry_run", False)
        flush_on_action = config.get("advanced", {}).get("flush_on_action", False)
        self.writer = BatchWriter(db, flush_every=2.0, flush_on_action=flush_on_action)
        # Ключ tradeSigner для EIP-712 подписи VibePassMarket (buy/sell).
        # config -> vibevibe.signer_key или env FARMER_VIBE_SIGNER_KEY.
        self.signer_key = (config.get("vibevibe") or {}).get("signer_key") or os.environ.get(
            "FARMER_VIBE_SIGNER_KEY", ""
        )

    async def buffer_log(
        self,
        address: str,
        action_type: str,
        tx_hash: str,
        success: bool,
        details: str = "",
    ) -> None:
        await self.writer.add_action(address, action_type, tx_hash, success, details)

    async def flush(self) -> None:
        await self.writer.flush()

    def _has_valid_contract(self, action_conf: dict) -> bool:
        return _is_valid_contract(action_conf.get("contract", ""))

    def _pick_action(self, profile: WalletProfile | None = None) -> tuple[int, dict]:
        if not self.actions_conf:
            # Нет ни одного действия в конфиге: раньше random.choices(range(0))
            # на пустом списке ронял воркер IndexError на КАЖДЫЙ кошелёк.
            return -1, {}
        weights = [a.get("weight", 1.0) for a in self.actions_conf]
        if profile is not None and profile.action_multipliers:
            weights = [
                w * profile.action_multipliers.get(a.get("type", ""), 1.0)
                for w, a in zip(weights, self.actions_conf, strict=False)
            ]
        if not weights or sum(max(float(w), 0.0) for w in weights) <= 0:
            weights = [1.0] * len(self.actions_conf)
        scores = _adaptive_scores(weights, [list(d) for d in self._outcomes])
        idx = random.choices(range(len(self.actions_conf)), weights=scores, k=1)[0]
        return idx, self.actions_conf[idx]

    def _record_type(self, idx: int, ok: bool) -> None:
        if 0 <= idx < len(self._outcomes):
            self._outcomes[idx].append(ok)

    async def transfer(self, wallet: dict, to_address: str, amount: float, gas_mult: float = 1.0) -> bool:
        if self.dry_run:
            tx_hash = _fake_tx_hash()
            logger.debug(f"DRY-RUN transfer: {wallet['address'][:10]} -> {to_address[:10]}, {amount:.6f}")
            await self.buffer_log(
                wallet["address"],
                "transfer",
                tx_hash,
                True,
                f"[DRY-RUN] amount={amount:.6f}, to={to_address}",
            )
            return True
        try:
            result_hash = await self.network.send_transfer(
                wallet["private_key"],
                to_address,
                amount,
                gas_limit=self.gas_limit,
                gas_price_mult=gas_mult,
            )
            if result_hash:
                await self.buffer_log(
                    wallet["address"],
                    "transfer",
                    result_hash,
                    True,
                    f"amount={amount:.6f}, to={to_address}",
                )
                return True
            await self.buffer_log(
                wallet["address"],
                "transfer",
                "",
                False,
                f"to={to_address}, amount={amount:.6f}",
            )
        except Exception as e:
            logger.error(f"Transfer error: {e}")
            await self.buffer_log(wallet["address"], "transfer", "", False, str(e)[:300])
        return False

    async def _contract_call(
        self,
        wallet: dict,
        action_conf: dict,
        amount: float | None = None,
        gas_mult: float = 1.0,
    ) -> bool:
        method = action_conf.get("method", "swap")
        if self.dry_run:
            tx_hash = _fake_tx_hash()
            logger.debug(f"DRY-RUN contract call: {method} for {wallet['address'][:10]}")
            await self.buffer_log(
                wallet["address"],
                action_conf["type"],
                tx_hash,
                True,
                f"[DRY-RUN] method={method}",
            )
            return True
        contract = action_conf.get("contract", "")
        try:
            amount_wei = self.network.w3.to_wei(amount, "ether") if amount is not None else None
            result_hash = await self.vibevibe.call_method(
                contract, method, wallet["private_key"], amount_wei=amount_wei, gas_mult=gas_mult
            )
            if result_hash:
                await self.buffer_log(
                    wallet["address"],
                    action_conf["type"],
                    result_hash,
                    True,
                    f"contract={contract}, method={method}",
                )
                return True
            await self.buffer_log(
                wallet["address"],
                action_conf["type"],
                "",
                False,
                f"contract={contract}, method={method}",
            )
        except Exception as e:
            logger.error(f"Контрактное действие {method} ошибка: {e}")
            await self.buffer_log(wallet["address"], action_conf["type"], "", False, str(e)[:300])
        return False

    async def _generic_call(
        self,
        wallet: dict,
        action_conf: dict,
        amount: float | None = None,
        gas_mult: float = 1.0,
    ) -> bool:
        """contract_call: вызов найденного контракта без известного ABI.

        Если method задан 4-байт селектором (0x+8 hex) — кладём его в calldata;
        иначе это value-only вызов (просто перевод на адрес контракта).
        """
        contract = action_conf.get("contract", "")
        method = action_conf.get("method") or ""
        data = method if method.startswith("0x") and len(method) == 10 else None
        if self.dry_run:
            tx_hash = _fake_tx_hash()
            logger.debug(f"DRY-RUN contract_call: {contract} value={amount} data={data}")
            await self.buffer_log(
                wallet["address"],
                action_conf["type"],
                tx_hash,
                True,
                f"[DRY-RUN] contract={contract}, method={method}",
            )
            return True
        try:
            value = amount if amount is not None else 0.0
            result_hash = await self.network.send_transfer(
                wallet["private_key"],
                contract,
                value,
                gas_limit=self.gas_limit,
                gas_price_mult=gas_mult,
                data_hex=data,
            )
            if result_hash:
                await self.buffer_log(
                    wallet["address"],
                    action_conf["type"],
                    result_hash,
                    True,
                    f"contract={contract}, value={value:.6f}, data={data or ''}",
                )
                return True
            await self.buffer_log(
                wallet["address"],
                action_conf["type"],
                "",
                False,
                f"contract={contract}, value={value:.6f}, data={data or ''}",
            )
        except Exception as e:
            logger.error(f"contract_call {contract} ошибка: {e}")
            await self.buffer_log(wallet["address"], action_conf["type"], "", False, str(e)[:300])
        return False

    async def _signed_trade(
        self,
        wallet: dict,
        action_conf: dict,
        amount: float | None = None,
        gas_mult: float = 1.0,
    ) -> bool:
        """vibevibe_buy/sell: EIP-712 подпись tradeSigner + вызов VibePassMarket.

        Без signer_key (config vibevibe.signer_key / FARMER_VIBE_SIGNER_KEY)
        действие невыполнимо — возвращает False (адаптивные веса отключат его).
        """
        atype = action_conf["type"]
        contract = action_conf.get("contract", "")
        action = _TRADE_ACTION_SELL if atype == "vibevibe_sell" else _TRADE_ACTION_BUY
        lounge_id = int(action_conf.get("lounge_id", 0) or 0)
        if lounge_id <= 0:
            logger.warning(f"{atype}: не задан lounge_id (конфиг actions[].lounge_id) — действие пропущено")
            return False
        if not self.signer_key:
            logger.warning(
                f"{atype}: не задан ключ tradeSigner (config vibevibe.signer_key / "
                "FARMER_VIBE_SIGNER_KEY) — действие пропущено"
            )
            return False
        if self.dry_run:
            tx_hash = _fake_tx_hash()
            logger.debug(f"DRY-RUN {atype}: lounge={lounge_id} amount={amount}")
            await self.buffer_log(wallet["address"], atype, tx_hash, True, f"[DRY-RUN] lounge_id={lounge_id}")
            return True
        try:
            amount_wei = self.network.w3.to_wei(amount, "ether") if amount is not None else 0
            result_hash = await self.vibevibe.call_trade(
                contract,
                wallet["private_key"],
                self.signer_key,
                lounge_id=lounge_id,
                action=action,
                amount_wei=int(amount_wei),
                gas_mult=gas_mult,
            )
            if result_hash:
                await self.buffer_log(
                    wallet["address"], atype, result_hash, True, f"lounge_id={lounge_id}, amount={amount}"
                )
                return True
            await self.buffer_log(
                wallet["address"], atype, "", False, f"lounge_id={lounge_id}, amount={amount}"
            )
        except Exception as e:
            logger.error(f"{atype} ошибка: {e}")
            await self.buffer_log(wallet["address"], atype, "", False, str(e)[:300])
        return False

    async def _register_lounge(
        self,
        wallet: dict,
        action_conf: dict,
        gas_mult: float = 1.0,
    ) -> bool:
        """vibevibe_register: registerLounge (REGISTRAR_ROLE). В пачном фарме не работает."""
        contract = action_conf.get("contract", "")
        if self.dry_run:
            tx_hash = _fake_tx_hash()
            logger.debug(f"DRY-RUN vibevibe_register for {wallet['address'][:10]}")
            await self.buffer_log(wallet["address"], "vibevibe_register", tx_hash, True, "[DRY-RUN] register")
            return True
        try:
            identity_key = bytes.fromhex(action_conf.get("identity_key", "ab")[:64])
            uri = action_conf.get("uri", "https://vibevibe.fun")
            result_hash = await self.vibevibe.call_register(
                contract,
                wallet["private_key"],
                identity_key=identity_key,
                creator=wallet["address"],
                uri=uri,
                gas_mult=gas_mult,
            )
            if result_hash:
                await self.buffer_log(wallet["address"], "vibevibe_register", result_hash, True, f"uri={uri}")
                return True
            await self.buffer_log(wallet["address"], "vibevibe_register", "", False, f"uri={uri}")
        except Exception as e:
            logger.error(f"vibevibe_register ошибка: {e}")
            await self.buffer_log(wallet["address"], "vibevibe_register", "", False, str(e)[:300])
        return False

    async def execute_action(
        self,
        wallet: dict,
        all_addresses: list,
        profile: WalletProfile | None = None,
    ) -> bool:
        idx, action_conf = self._pick_action(profile)
        if idx < 0:
            logger.warning("Не настроено ни одного действия (actions пуст) — действие пропущено")
            return False
        atype = action_conf["type"]
        my_addr = wallet["address"]
        # Множитель газа — локальная переменная вызова: ActionExecutor общий для
        # всех кошельков, хранить его в self нельзя (гонка между корутинами).
        gas_mult = gas_multiplier(profile) if profile is not None else 1.0

        def _amount(lo: float, hi: float) -> float:
            if profile is not None and not profile.neutral:
                lo *= profile.amount_min_mul
                hi *= profile.amount_max_mul
            lo, hi = max(lo, 0.0), max(hi, lo)
            amount = random.uniform(lo, hi)
            if profile is not None and not profile.neutral and random.random() < profile.odd_amount_prob:
                amount = round(amount, random.randint(4, 8))
            return max(amount, 1e-9)

        if atype in CONTRACT_ACTIONS and not self._has_valid_contract(action_conf):
            logger.debug(f"{atype} нет адреса контракта, fallback на transfer для {my_addr[:10]}")
            atype = "transfer"
            action_conf = {
                "type": "transfer",
                "target": "random_wallet",
                "min_amount": 0.0001,
                "max_amount": 0.0005,
            }

        ok = False
        if atype == "transfer":
            target = action_conf.get("target")
            if target == "random_wallet":
                candidates = [a for a in all_addresses if a != my_addr]
                if not candidates:
                    logger.warning("Нет других кошельков для трансфера")
                    return False
                to_addr = random.choice(candidates)
            else:
                to_addr = target
            if not to_addr:
                return False
            amount = _amount(
                action_conf.get("min_amount", 0.0001),
                action_conf.get("max_amount", 0.001),
            )
            ok = await self.transfer(wallet, to_addr, amount, gas_mult=gas_mult)

        elif atype == "contract_call":
            has_amt = "min_amount" in action_conf and "max_amount" in action_conf
            send_amount: float | None = (
                _amount(action_conf["min_amount"], action_conf["max_amount"]) if has_amt else 0.0
            )
            ok = await self._generic_call(wallet, action_conf, amount=send_amount, gas_mult=gas_mult)

        elif atype == "vibevibe_register":
            ok = await self._register_lounge(wallet, action_conf, gas_mult=gas_mult)

        elif atype in ("vibevibe_buy", "vibevibe_sell"):
            has_amt = "min_amount" in action_conf and "max_amount" in action_conf
            send_amount = _amount(action_conf["min_amount"], action_conf["max_amount"]) if has_amt else None
            ok = await self._signed_trade(wallet, action_conf, amount=send_amount, gas_mult=gas_mult)

        elif atype in CONTRACT_ACTIONS:
            # amount передаём только если в конфиге заданы min/max_amount.
            # no-arg методы (mint, validateTask и т.п.) вызываются без суммы —
            # иначе fn(*args) падает на пустой сигнатуре и действие всегда фейлит.
            has_amt = "min_amount" in action_conf and "max_amount" in action_conf
            send_amount = _amount(action_conf["min_amount"], action_conf["max_amount"]) if has_amt else None
            ok = await self._contract_call(wallet, action_conf, amount=send_amount, gas_mult=gas_mult)

        else:
            logger.warning(f"Неизвестный тип действия: {atype}")

        # Пишем исход в историю адаптации весов: пул учится на своём же опыте.
        self._record_type(idx, ok)
        return ok
