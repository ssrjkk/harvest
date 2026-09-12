"""Асинхронный кран: несколько стратегий, ретраи, ограничение по балансу.

Pre-flight validate(): быстро проверяет доступность URL-ов крана и
отбрасывает мёртвые стратегии, чтобы не висеть на таймаутах мёртвых доменов.
"""

import asyncio
import logging
import random

import aiohttp

from core import utils
from core.database import _redact_rpc_url
from core.network import NetworkManager

logger = logging.getLogger(__name__)

# общий таймаут на один запрос крана (короткий, чтобы не висеть)
_REQUEST_TIMEOUT = 8


class Faucet:
    def __init__(self, config: dict) -> None:
        # Принимает как полный конфиг (с секцией faucet/proxy), так и
        # устаревший «faucet-only» словарь.
        faucet_cfg = config.get("faucet", config)
        # Программный сбор конфига может протащить не-dict элементы — их
        # отбрасываем, чтобы validate()/_request_strategy не ловили AttributeError.
        self.strategies = [s for s in faucet_cfg.get("strategies", []) if isinstance(s, dict)]
        try:
            self.retries = max(int(faucet_cfg.get("retries", 3)), 1)
        except (TypeError, ValueError):
            logger.warning("faucet.retries некорректен — использую 3")
            self.retries = 3
        self.min_balance = faucet_cfg.get("min_balance", 0.005)
        self.target_balance = faucet_cfg.get("target_balance", 0.0)
        self.delay_range = faucet_cfg.get("delay_between_requests", [5, 15])
        if (
            not isinstance(self.delay_range, (list, tuple))
            or len(self.delay_range) != 2
            or not all(isinstance(v, (int, float)) and v >= 0 for v in self.delay_range)
            or self.delay_range[0] > self.delay_range[1]
        ):
            logger.warning(f"faucet.delay_between_requests некорректен ({self.delay_range}) — использую [5, 15]")
            self.delay_range = [5, 15]
        self.enabled = faucet_cfg.get("enabled", True)
        try:
            self.max_concurrent = max(int(faucet_cfg.get("max_concurrent", 8)), 1)
        except (TypeError, ValueError):
            logger.warning("faucet.max_concurrent некорректен — использую 8")
            self.max_concurrent = 8
        self._reachable: list[bool] | None = None
        self._session: aiohttp.ClientSession | None = None
        self._connector: aiohttp.TCPConnector | None = None
        proxy_cfg = config.get("proxy", {})
        self._proxy_enabled = proxy_cfg.get("enabled", False)
        self._proxies = self._load_proxies(proxy_cfg)
        self._proxy_rotate = proxy_cfg.get("rotate", "sequential")
        self._proxy_index = 0

    @staticmethod
    def _load_proxies(proxy_cfg: dict) -> list[str]:
        items = proxy_cfg.get("list", [])
        if isinstance(items, str):
            try:
                with open(items, encoding="utf-8") as f:
                    return [line.strip() for line in f if line.strip() and not line.strip().startswith("#")]
            except OSError as e:
                logger.warning(f"Прокси-файл не читается: {e}")
                return []
        return [p for p in items if isinstance(p, str) and p]

    def _pick_proxy(self) -> str | None:
        if not self._proxy_enabled or not self._proxies:
            return None
        if self._proxy_rotate == "random":
            return random.choice(self._proxies)
        proxy = self._proxies[self._proxy_index % len(self._proxies)]
        self._proxy_index += 1
        return proxy

    async def _get_session(self) -> aiohttp.ClientSession:
        """Лениво создаёт общий aiohttp ClientSession (connection pooling).

        Переиспользует TCP-коннекты вместо создания нового на каждый запрос.
        """
        if self._session is None or self._session.closed:
            self._connector = aiohttp.TCPConnector(
                limit=20,  # максимум одновременных коннектов
                ttl_dns_cache=300,  # DNS кэш 5 мин
                enable_cleanup_closed=True,
            )
            self._session = aiohttp.ClientSession(connector=self._connector)
        return self._session

    async def close(self) -> None:
        """Закрывает общий session и connector (освобождает коннекты)."""
        if self._session and not self._session.closed:
            await self._session.close()
            self._session = None
        if self._connector and not self._connector.closed:
            await self._connector.close()
            self._connector = None

    async def validate(self) -> int:
        """Проверяет ДОСТУПНОСТЬ URL-ов крана. Возвращает число живых стратегий.

        Это connectivity-проба, а не функциональная проверка: любой HTTP-ответ
        (включая 4xx/5xx — например, валидация адреса) означает, что URL жив.
        Просьба о пополнении не выполняется — квота крана не тратится.
        """
        if not self.enabled or not self.strategies:
            self._reachable = []
            return 0
        self._reachable = []
        session = await self._get_session()

        async def _probe(url: str | None) -> bool:
            if not url:
                return False
            try:
                # GET-проба обычно дешевле POST и не тратит лимиты пополнений.
                # Если сервер не поддерживает GET (405/404) — это тоже ответ,
                # значит URL доступен.
                async with session.get(
                    url,
                    headers={"User-Agent": utils.get_random_user_agent()},
                    timeout=aiohttp.ClientTimeout(total=_REQUEST_TIMEOUT),
                ) as resp:
                    logger.info(f"Кран стратегия доступна (HTTP {resp.status})")
                    return True
            except Exception:
                return False

        # Пробы всех стратегий идут ПАРАЛЛЕЛЬНО — при большом списке кранов
        # валидация не растягивается на сумму таймаутов (8с * N).
        probes = await asyncio.gather(*[_probe(s.get("url")) for s in self.strategies], return_exceptions=True)
        for strategy, ok in zip(self.strategies, probes, strict=True):
            alive = bool(ok) and not isinstance(ok, BaseException)
            self._reachable.append(alive)
            if not alive:
                disp = strategy.get("type")
                if not disp:
                    disp = _redact_rpc_url(strategy.get("url")) or strategy.get("url") or "?"
                logger.warning(f"Кран стратегия {disp} недоступна — пропуск")
        return sum(self._reachable)

    def _live_strategies(self) -> list[dict]:
        if self._reachable is None:
            return self.strategies
        return [s for s, alive in zip(self.strategies, self._reachable, strict=True) if alive]

    async def _request_strategy(
        self,
        session: aiohttp.ClientSession,
        strategy: dict,
        proxy: str | None,
        address: str,
    ) -> bool:
        url = strategy.get("url")
        if not url:
            return False
        try:
            if strategy.get("type") == "chainstack":
                payload = {
                    "network": strategy.get("network_param", "robinhood"),
                    "address": address,
                }
            else:
                payload = {"address": address}
            headers = {
                "User-Agent": utils.get_random_user_agent(),
                "Content-Type": "application/json",
            }
            async with session.post(
                url,
                json=payload,
                headers=headers,
                proxy=proxy,
                timeout=aiohttp.ClientTimeout(total=_REQUEST_TIMEOUT),
            ) as resp:
                if resp.status in (200, 201, 202):
                    logger.info(f"Faucet OK ({strategy.get('type')}) for {address[:10]}")
                    return True
                text = await resp.text()
                logger.debug(f"Faucet {strategy.get('type')} [{resp.status}]: {text[:80]}")
        except TimeoutError:
            logger.debug(f"Faucet {strategy.get('type')} timeout для {address[:10]}")
        except aiohttp.ClientError as e:
            logger.debug(f"Faucet {strategy.get('type')} HTTP ошибка: {e}")
        except Exception as e:
            logger.debug(f"Faucet {strategy.get('type')} ошибка: {e}")
        return False

    async def request_tokens(self, address: str, proxy: str | None = None, retries: int | None = None) -> bool:
        if not self.enabled:
            return False
        if proxy is None:
            proxy = self._pick_proxy()
        live = self._live_strategies()
        if not live:
            logger.info("Кран: нет живых стратегий, пропуск пополнения")
            return False
        session = await self._get_session()
        attempts = max(1, retries if retries is not None else self.retries)
        for attempt in range(attempts):
            for strategy in live:
                if await self._request_strategy(session, strategy, proxy, address):
                    return True
                # Exponential backoff между стратегиями
                await asyncio.sleep(random.uniform(1, 3) * (attempt + 1))
            if attempt < attempts - 1:
                # Exponential backoff между retry-циклами
                backoff = random.uniform(*self.delay_range) * (2**attempt)
                await asyncio.sleep(min(backoff, 60))
        logger.debug(f"Кран не смог пополнить {address[:10]} после {attempts} попыток")
        return False

    async def ensure_balance(self, network: NetworkManager, address: str) -> bool:
        goal = self.target_balance if self.target_balance > 0 else self.min_balance
        # refresh=True: не верить TTL-кэшу баланса — иначе пополнение крана не видно
        balance = await network.get_balance(address, refresh=True)
        if balance >= goal:
            return True
        logger.info(f"Баланс {balance:.6f} < цель {goal}, запрашиваю кран для {address[:10]}")
        if self.target_balance > 0:
            for _ in range(self.retries):
                # Один проход по стратегиям на итерацию (retries=1), цикл по балансу
                # ведёт внешний цикл — иначе суммарно до retries² запросов.
                await self.request_tokens(address, retries=1)
                balance = await network.get_balance(address, refresh=True)
                if balance >= self.target_balance:
                    break
            return balance >= self.min_balance
        return await self.request_tokens(address)

    def concurrent_batch(self, workers: int) -> int:
        """Потолок одновременных запросов крана: min(воркеры, лимит из конфига).

        Лимит конфига защищает от rate limit крана при большом max_workers.
        """
        return max(1, min(int(workers), self.max_concurrent))

    async def request_batch(
        self,
        addresses: list[str],
        batch_size: int,
        network: NetworkManager | None = None,
        progress=None,
        pause: float = 0.0,
        max_concurrent: int = 8,
    ) -> tuple[int, int]:
        """Конкурентно запрашивает токены для списка адресов батчами.

        Если network передан — проверяет баланс перед запросом (ensure_balance),
        иначе шлёт слепой запрос (request_tokens). Возвращает (success, fail).

        progress: опциональный callback (done, total) для вывода прогресса.
        pause: задержка (сек) между батчами — защита от rate limit.
        max_concurrent: потолок одновременных HTTP-запросов к кранам (rate limit).
        """
        total = len(addresses)
        success = 0
        fail = 0
        sem = asyncio.Semaphore(max_concurrent)
        for start in range(0, total, batch_size):
            batch = addresses[start : start + batch_size]

            async def _one(addr) -> bool:
                async with sem:
                    try:
                        if network is not None:
                            return await self.ensure_balance(network, addr)
                        return await self.request_tokens(addr)
                    except Exception:
                        return False

            results = await asyncio.gather(*[_one(a) for a in batch], return_exceptions=True)
            for r in results:
                if r is True:
                    success += 1
                else:
                    fail += 1
            if progress is not None:
                progress(min(start + batch_size, total), total)
            if pause and start + batch_size < total:
                await asyncio.sleep(pause)
        return success, fail
