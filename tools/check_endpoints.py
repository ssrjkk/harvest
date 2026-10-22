"""Живость внешних эндпоинтов harvest: RPC, эксплореры, краны.

Использование:  python tools/check_endpoints.py [--timeout 8]

RPC — жёсткая проверка: eth_chainId обязан совпасть с chain_id из конфига
(ответ hex или dec — принимаем оба). Страницы (explorer/faucet) — только
отчёт: многие отдают 403 бот-защите, это не приговор, а повод посмотреть.

Источники URL — те же, что едут в прод: config_*.yaml (rpc/chain) и
portal/networks.py (faucet/explorer). Новых мест правды не заводим.
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

TIMEOUT_DEFAULT = 8


def _rpc_chain_id(url: str, timeout: float) -> tuple[str | None, str]:
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "eth_chainId", "params": []}).encode()
    try:
        req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            payload = json.loads(r.read(65536).decode("utf-8", "replace"))
    except Exception as e:  # noqa: BLE001 — любая сетевая беда = строка в отчёте
        hint = " (похоже на бот-защиту — проверьте из браузера/с фарм-машины)" if "403" in str(e) else ""
        return None, f"unreachable: {type(e).__name__}: {e}{hint}"[:160]
    try:
        raw = payload["result"]
        got = int(raw, 16) if isinstance(raw, str) and raw.startswith("0x") else int(raw)
        return str(got), "ok"
    except (KeyError, TypeError, ValueError):
        return None, f"bad reply: {str(payload)[:100]}"


def _page_status(url: str, timeout: float) -> str:
    if not url:
        return "— (не задан)"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "harvest-endpoint-check/1.0"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return f"HTTP {r.status}"
    except Exception as e:  # noqa: BLE001
        return f"WARN {type(e).__name__}: {e}"[:120]


def _rpc_list() -> list[tuple[str, str, int]]:
    """(имя, rpc_url, chain_id) из shipped-конфигов."""
    import yaml

    out = []
    for cfg in ("config_robinhood.yaml", "config_flop.yaml", "config_arc.yaml",
                "config_vibevibe.yaml", "config.simple.yaml"):
        try:
            net = yaml.safe_load((ROOT / cfg).read_text(encoding="utf-8"))["network"]
        except Exception:  # noqa: BLE001 — битый конфиг увидит config_validate, не мы
            continue
        rpc = net.get("rpc_url") or ""
        if isinstance(rpc, (list, tuple)):
            rpc = rpc[0] if rpc else ""
        try:
            out.append((cfg, str(rpc), int(net.get("chain_id", 0))))
        except (TypeError, ValueError):
            continue
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description="Проверить живость RPC/кранов/эксплореров harvest.")
    ap.add_argument("--timeout", type=float, default=TIMEOUT_DEFAULT)
    args = ap.parse_args()

    from portal.networks import NETWORKS

    bad = 0
    print(f"{'ENDPOINT':58s} {'EXPECT':>10s} {'GOT':>10s}  RESULT")
    for cfg, url, chain in _rpc_list():
        got, note = _rpc_chain_id(url, args.timeout)
        ok = got == str(chain)
        bad += not ok
        print(f"{url[:58]:58s} {chain:>10d} {(got or '-'):>10s}  {'OK' if ok else 'FAIL ' + note}")
    for n in NETWORKS:
        for kind in ("explorer", "faucet"):
            url = n.get(kind) or ""
            print(f"{n['slug']:12s} {kind:8s} {url[:60]:60s}  {_page_status(url, args.timeout)}")
    print("rpc fails:", bad, "(страницы — только отчёт, на код возврата не влияют)")
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
