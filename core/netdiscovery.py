"""Автоконфигурация сетей: разведка живых тестнетов и генерация конфига.

Прорывная фича «zero-config»: HARVEST сам находит живую сеть из встроенного
реестра (проба RPC: chain_id / blockNumber / gasPrice), сканирует свежие блоки
и предлагает ТОП вызываемых контрактов (подсказка протокола), после чего
собирает рабочий конфиг config_auto.yaml — RPC, chain_id, символ, explorer,
кран и действия уже вписанными. Другие фармеры настраивают всё это руками.

CLI:
    python -m core.netdiscovery --scan                 # живые сети + отчёт
    python -m core.netdiscovery --scan --hunt          # + ТОП контрактов
    python -m core.netdiscovery --rpc https://...      # разведка конкретного RPC
    python -m core.netdiscovery --out config_auto.yaml # куда писать конфиг
"""

from __future__ import annotations

import argparse
import asyncio
import time
from collections import Counter
from dataclasses import dataclass, field
from typing import Any

import aiohttp
import yaml

_PROBE_TIMEOUT = 7.0  # сколько ждать один JSON-RPC запрос
_MAX_CONCURRENCY = 8  # параллельность проб/блоков
HUNT_MAX_BLOCKS = 30  # сколько последних блоков сканировать по умолчанию
HUNT_TOP = 8  # сколько верхних контрактов показать
OUT_DEFAULT = "config_auto.yaml"

# Адреса, которые не являются пользовательским контрактом (системные).
_SYSTEM_EMPTY = frozenset(
    {
        "0x0000000000000000000000000000000000000000",  # contract creation
        "0x0000000000000000000000000000000000000001",  # precompile ecrecover
        "0x0000000000000000000000000000000000000002",  # precompile sha256
        "0x0000000000000000000000000000000000000003",  # precompile ripemd160
        "0x0000000000000000000000000000000000000004",  # precompile identity
        "0x0000000000000000000000000000000000000005",  # precompile modexp
        "0x0000000000000000000000000000000000000006",  # precompile alt_bn128
        "0x0000000000000000000000000000000000000007",  # precompile blake2
        "0x0000000000000000000000000000000000000008",  # precompile kzg
        "0x000000000000000000000000000000000000defc",  # volcano
        "0x000000000000000000000000000000000000dead",  # burn-адрес
    }
)


@dataclass(frozen=True)
class NetworkCandidate:
    """Запись реестра: известная сеть с кандидатами RPC и крана."""

    id: str
    name: str
    chain_id: int
    symbol: str
    rpc_urls: tuple[str, ...]
    explorer: str = ""
    faucets: tuple[dict, ...] = ()
    notes: str = ""


# Реестр живых тестнетов. Порядок — приоритет (сначала родные/наши).
NETWORK_REGISTRY: tuple[NetworkCandidate, ...] = (
    NetworkCandidate(
        id="robinhood",
        name="Robinhood Chain Testnet",
        chain_id=46630,
        symbol="ETH",
        rpc_urls=("https://rpc.testnet.chain.robinhood.com",),
        explorer="https://explorer.testnet.chain.robinhood.com",
        faucets=(
            {
                "type": "chainstack",
                "url": "https://mcp.chainstack.com/mcp",
                "network_param": "robinhood",
            },
            {"type": "direct", "url": "https://faucet.testnet.chain.robinhood.com/claim"},
        ),
        notes="Rewards-программа VibeVibe",
    ),
    NetworkCandidate(
        id="soneium",
        name="Soneium Minato Testnet",
        chain_id=1946,
        symbol="ETH",
        rpc_urls=("https://rpc.minato.soneium.org",),
        explorer="https://explorer-testnet.soneium.org",
        notes="Sony Layer-2",
    ),
    NetworkCandidate(
        id="base_sepolia",
        name="Base Sepolia",
        chain_id=84532,
        symbol="ETH",
        rpc_urls=("https://sepolia.base.org",),
        explorer="https://sepolia.basescan.org",
        notes="Coinbase L2",
    ),
    NetworkCandidate(
        id="unichain",
        name="Unichain Sepolia",
        chain_id=1301,
        symbol="ETH",
        rpc_urls=("https://sepolia.unichain.org",),
        explorer="https://sepolia.uniscan.xyz",
        notes="Uniswap L2",
    ),
    NetworkCandidate(
        id="op_sepolia",
        name="Optimism Sepolia",
        chain_id=11155420,
        symbol="ETH",
        rpc_urls=("https://sepolia.optimism.io",),
        explorer="https://sepolia-optimistic.etherscan.io",
        notes="OP Stack",
    ),
    NetworkCandidate(
        id="arb_sepolia",
        name="Arbitrum Sepolia",
        chain_id=421614,
        symbol="ETH",
        rpc_urls=("https://sepolia-rollup.arbitrum.io/rpc",),
        explorer="https://sepolia.arbiscan.io",
        notes="Arbitrum L2",
    ),
    NetworkCandidate(
        id="eth_sepolia",
        name="Ethereum Sepolia",
        chain_id=11155111,
        symbol="ETH",
        rpc_urls=("https://ethereum-sepolia-rpc.publicnode.com",),
        explorer="https://sepolia.etherscan.io",
        notes="основная тестовая сеть Ethereum",
    ),
    NetworkCandidate(
        id="scroll_sepolia",
        name="Scroll Sepolia",
        chain_id=534351,
        symbol="ETH",
        rpc_urls=("https://sepolia-rpc.scroll.io",),
        explorer="https://sepolia.scrollscan.com",
    ),
    NetworkCandidate(
        id="ink_sepolia",
        name="Ink Sepolia",
        chain_id=763373,
        symbol="INK",
        rpc_urls=("https://rpc-gel.inkonchain.com",),
        explorer="https://explorer-sepolia.inkonchain.com",
    ),
    NetworkCandidate(
        id="abstract",
        name="Abstract Sepolia",
        chain_id=11124,
        symbol="ETH",
        rpc_urls=("https://api.testnet.abs.xyz",),
        explorer="https://sepolia.abscan.org",
    ),
)


@dataclass
class ProbeResult:
    """Успешная проба RPC: сеть жива и отвечает."""

    candidate: NetworkCandidate
    rpc_url: str
    chain_id: int
    block_number: int
    gas_price_wei: int | None
    latency_ms: float

    @property
    def chain_ok(self) -> bool:
        return self.chain_id == self.candidate.chain_id


@dataclass
class ContractHint:
    """Найденный «активный» контракт: как часто вызывался и какие методы."""

    address: str
    calls: int
    selectors: Counter[str]

    @property
    def top_selectors(self) -> list[str]:
        return [s for s, _ in self.selectors.most_common(5)]


@dataclass
class DiscoverReport:
    """Результат автоконфигурации: живые сети, подсказки, путь до конфига."""

    live: list[ProbeResult] = field(default_factory=list)
    hints: list[ContractHint] = field(default_factory=list)
    config_path: str = ""
    config: dict = field(default_factory=dict)
    error: str = ""


# ---------------------------------------------------------------------------
# JSON-RPC пробы
# ---------------------------------------------------------------------------


async def _rpc(method_name: str, url: str, params: list | None = None) -> Any:
    """Лёгкий JSON-RPC запрос (без key, без web3-обвязки)."""
    payload = {"jsonrpc": "2.0", "id": 987, "method": method_name, "params": params or []}
    timeout = aiohttp.ClientTimeout(total=_PROBE_TIMEOUT)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.post(url, json=payload, headers={"Content-Type": "application/json"}) as resp:
            if resp.status != 200:
                raise RuntimeError(f"HTTP {resp.status}")
            data = await resp.json(content_type=None)
    if data.get("error") is not None:
        raise RuntimeError(f"RPC error: {data['error']}")
    return data.get("result")


async def rpc_probe(url: str, expected_chain_id: int | None = None) -> ProbeResult | None:
    """Проба одного RPC. Возвращает None, если эндпоинт мёртв/не JSON-RPC."""
    t0 = time.monotonic()
    try:
        chain_id = int(await _rpc("eth_chainId", url), 16)
        if expected_chain_id is not None and chain_id != expected_chain_id:
            return None
        block = int(await _rpc("eth_blockNumber", url), 16)
        gas_price: int | None = None
        try:
            gas_price = int(await _rpc("eth_gasPrice", url), 16)
        except Exception:
            pass  # gasPrice опционален (не все L2 его отдают)
        return ProbeResult(
            candidate=_candidate_for(chain_id),
            rpc_url=url,
            chain_id=chain_id,
            block_number=block,
            gas_price_wei=gas_price,
            latency_ms=(time.monotonic() - t0) * 1000.0,
        )
    except Exception:
        return None


def _candidate_for(chain_id: int) -> NetworkCandidate:
    """Кандидат реестра по chain_id; для неизвестных — синтетический."""
    for c in NETWORK_REGISTRY:
        if c.chain_id == chain_id:
            return c
    return NetworkCandidate(
        id=f"chain-{chain_id}", name=f"Chain {chain_id}", chain_id=chain_id, symbol="ETH", rpc_urls=()
    )


# ---------------------------------------------------------------------------
# Сканер реестра
# ---------------------------------------------------------------------------


async def scan_networks(
    registry: tuple[NetworkCandidate, ...] | list[NetworkCandidate] = NETWORK_REGISTRY,
    *,
    concurrency: int = _MAX_CONCURRENCY,
) -> list[ProbeResult]:
    """Проба всех RPC реестра параллельно. Живые — отсортированы по latency."""
    sem = asyncio.Semaphore(max(1, concurrency))

    async def _guarded(url: str) -> ProbeResult | None:
        async with sem:
            return await rpc_probe(url)

    jobs = [url for c in registry for url in c.rpc_urls]
    results = await asyncio.gather(*(_guarded(u) for u in jobs))
    live = [r for r in results if r is not None and r.chain_ok]
    live.sort(key=lambda r: r.latency_ms)
    return live


# ---------------------------------------------------------------------------
# Хантинг контрактов
# ---------------------------------------------------------------------------


def _is_contract_addr(addr: str | None) -> bool:
    if not addr or not isinstance(addr, str) or not addr.startswith("0x") or len(addr) != 42:
        return False
    return addr.lower() not in _SYSTEM_EMPTY


def aggregate_contracts(blocks: list[dict | None]) -> list[ContractHint]:
    """ТОП вызываемых контрактов по свежим блокам (чистая агрегация).

    Считает внешние вызовы: tx с to != null и адресом-контрактом. Для каждого
    топ-контракта собирает частоты method-селекторов (первые 4 байта calldata).
    """
    hits: Counter[str] = Counter()
    sel: dict[str, Counter[str]] = {}
    for block in blocks:
        if not isinstance(block, dict):
            continue
        for tx in block.get("transactions") or []:
            if not isinstance(tx, dict):
                continue
            to = tx.get("to")
            if not _is_contract_addr(to):
                continue
            key = (to or "").lower()
            hits[key] += 1
            data = tx.get("input") or tx.get("data") or ""
            if isinstance(data, str) and data.startswith("0x") and len(data) >= 10 and data != "0x":
                sel.setdefault(key, Counter())[data[:10]] += 1
    hints = [ContractHint(address=a, calls=n, selectors=sel.get(a, Counter())) for a, n in hits.most_common(HUNT_TOP)]
    return hints


async def hunt_contracts(
    rpc_url: str,
    *,
    max_blocks: int = HUNT_MAX_BLOCKS,
    top: int = HUNT_TOP,
) -> list[ContractHint]:
    """Сканирование последних N блоков и ТОП вызываемых контрактов."""
    latest = int(await _rpc("eth_blockNumber", rpc_url), 16)
    sem = asyncio.Semaphore(max(1, min(_MAX_CONCURRENCY, max_blocks)))

    async def _block(number: int) -> dict | None:
        async with sem:
            try:
                return await _rpc("eth_getBlockByNumber", rpc_url, [hex(number), True])
            except Exception:
                return None

    window = range(max(0, latest - max_blocks), latest + 1)
    blocks = await asyncio.gather(*(_block(n) for n in window))
    return aggregate_contracts(list(blocks))[:top]


# ---------------------------------------------------------------------------
# Сборка конфига
# ---------------------------------------------------------------------------


def _action_from_hint(hint: ContractHint, weight: float = 0.3) -> dict:
    """contract_call действие из найденного контракта (без ABI — value+data)."""
    action: dict[str, Any] = {"type": "contract_call", "contract": hint.address, "weight": weight}
    sels = hint.top_selectors
    if sels:
        action["method"] = sels[0]
    action["min_amount"] = 0.00005
    action["max_amount"] = 0.0005
    return action


def build_config(probe: ProbeResult, hints: list[ContractHint] | None = None) -> dict:
    """Полный валидный конфиг из живой пробы (+ подсказки контрактов).

    Заглушка transfer всегда на месте (фарм работает в любом случае);
    найденные контракты добавляются как contract_call. network.meta
    хранит инфо о разведке и список hints — для портала/doctor.
    """
    hints = hints or []
    actions: list[dict] = [
        {
            "type": "transfer",
            "target": "random_wallet",
            "weight": 0.6,
            "min_amount": 0.00005,
            "max_amount": 0.0003,
        }
    ]
    actions += [_action_from_hint(h, weight=round(0.4 / max(len(hints), 1), 4)) for h in hints]

    config: dict[str, Any] = {
        "network": {
            "name": probe.candidate.name,
            "rpc_url": probe.rpc_url,
            "chain_id": probe.chain_id,
            "currency": probe.candidate.symbol,
            "explorer": probe.candidate.explorer or "",
        },
        "wallets": {"count": 50, "file": "wallets.json", "generate_if_missing": True},
        "faucet": {
            "enabled": bool(probe.candidate.faucets),
            "min_balance": 0.005,
            "target_balance": 0.05,
            "retries": 3,
            "max_concurrent": 8,
            "strategies": [dict(s) for s in probe.candidate.faucets],
            "delay_between_requests": [5, 15],
        },
        "actions": actions,
        "farming": {
            "actions_per_cycle": [3, 8],
            "delay_between_actions": [5, 20],
            "delay_between_cycles": [3600, 7200],
            "skip_cycle_probability": 0.05,
        },
        "advanced": {
            "check_balance_before_action": True,
            "force_farm": False,
            "gas_limit": 300000,
            "min_gas_for_action": 0.003,
        },
        "threading": {"max_workers": 20, "rpc_threads": 0, "timeout_per_wallet": 0},
        "cache": {
            "gas_price_ttl": 15,
            "balance_ttl": 10,
            "nonce_ttl": 30,
            "rpc_rate_limit": 50,
            "rpc_rate_floor": 0.2,
        },
        "proxy": {"enabled": False, "list": [], "rotate": "sequential"},
        "database": {"path": "farming_state.db", "master_key": "master.key", "log_keep": 200000},
        "license": {
            "enabled": False,
            "deploy_url": "",
            "deploy_salt": "harvest-deploy-salt-v1",
            "grace_days": 7,
            "cache_file": ".license_cache",
        },
        "logging": {
            "level": "INFO",
            "file": "logs/farm.log",
            "max_bytes": 10485760,
            "backup_count": 5,
            "console": True,
            "colors": True,
        },
    }
    if hints:
        config["network"]["meta"] = {
            "discovered_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "block": probe.block_number,
            "gas_price_wei": probe.gas_price_wei,
            "hints": [
                {
                    "address": h.address,
                    "calls": h.calls,
                    "selectors": h.top_selectors,
                }
                for h in hints
            ],
        }
    return config


def write_config(config: dict, out_path: str = OUT_DEFAULT) -> str:
    """Пишет конфиг в YAML. Возвращает путь (нормализованный)."""
    with open(out_path, "w", encoding="utf-8") as f:
        yaml.safe_dump(config, f, allow_unicode=True, sort_keys=False)
    return out_path


# ---------------------------------------------------------------------------
# Отчёт и CLI
# ---------------------------------------------------------------------------


def _fmt_probe(p: ProbeResult) -> str:
    mark = "OK" if p.chain_ok else "chain_id MISMATCH"
    gas = f", gas={p.gas_price_wei}" if p.gas_price_wei else ""
    return f"  {mark:<16} {p.candidate.name:<28} chain={p.chain_id} block={p.block_number}{gas} {p.latency_ms:6.0f}ms"


def format_report(report: DiscoverReport) -> str:
    """Человекочитаемый отчёт о разведке (используют CLI, auto.py, main.py)."""
    if not report.live:
        lines = ["Разведка сетей: ни одна сеть не ответила"]
    else:
        lines = ["Разведка сетей (живые, по задержке):"]
        lines += [_fmt_probe(p) for p in report.live]
    if report.hints:
        lines.append("")
        lines.append(f"ТОП-{len(report.hints)} активных контрактов (последние блоки):")
        for h in report.hints:
            sels = ", ".join(h.top_selectors) if h.top_selectors else "(value-only)"
            lines.append(f"  {h.address}  calls={h.calls}  [{sels}]")
    if report.config_path:
        lines.append("")
        lines.append(f"Конфиг записан: {report.config_path}")
        acts = ", ".join(a.get("type", "?") for a in report.config.get("actions", []))
        lines.append(f"  действия: {acts}")
    if report.error:
        lines.append(f"  ОШИБКА: {report.error}")
    return "\n".join(lines)


async def auto_discover(
    *,
    scan: bool = True,
    hunt: bool = False,
    out_path: str = OUT_DEFAULT,
    rpc_url: str | None = None,
    max_blocks: int = HUNT_MAX_BLOCKS,
) -> DiscoverReport:
    """Оркестратор: разведка -> хантинг -> конфиг -> запись."""
    report = DiscoverReport(config_path=out_path)
    try:
        if rpc_url:
            probe = await rpc_probe(rpc_url)
            if probe is None or not probe.chain_ok:
                report.error = f"RPC недоступен или chain_id не совпал: {rpc_url}"
                return report
            report.live = [probe]
        elif scan:
            report.live = await scan_networks()
        if not report.live:
            report.error = "нет живых сетей"
            return report
        probe = report.live[0]
        if hunt:
            report.hints = await hunt_contracts(probe.rpc_url, max_blocks=max_blocks)
        report.config = build_config(probe, report.hints)
        report.config_path = write_config(report.config, out_path)
    except Exception as e:
        report.error = str(e)
    return report


def _parse_cli() -> argparse.Namespace:
    p = argparse.ArgumentParser(prog="netdiscovery", description="Автоконфигурация сети HARVEST")
    p.add_argument("--scan", action="store_true", help="Проба всех сетей реестра")
    p.add_argument("--hunt", action="store_true", help="Хантинг топ-контрактов (по первой живой сети)")
    p.add_argument("--rpc", default=None, help="Разведать конкретный RPC вместо скана")
    p.add_argument("--out", default=OUT_DEFAULT, help="Файл конфига (default: %(default)s)")
    p.add_argument("--max-blocks", type=int, default=HUNT_MAX_BLOCKS, help="Блоков для хантинга")
    return p.parse_args()


def main_cli() -> int:
    args = _parse_cli()
    if not (args.scan or args.rpc):
        print("Использование: python -m core.netdiscovery --scan [--hunt] [--out config_auto.yaml]")
        print("               python -m core.netdiscovery --rpc https://... [--hunt]")
        return 2
    report = asyncio.run(auto_discover(scan=args.scan, hunt=args.hunt, out_path=args.out, rpc_url=args.rpc))
    print(format_report(report))
    return 0 if (report.live and report.config_path and not report.error) else 1


if __name__ == "__main__":
    raise SystemExit(main_cli())
