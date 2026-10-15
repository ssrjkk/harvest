"""Одна команда для проверки сайта: python tools/verify_site.py.

Статические проверки без браузера (stdlib only):
- node --check для docs/assets/site.js;
- все $("#id") из JS существуют в docs/index.html, дублей ID нет;
- все data-i18n ключи покрыты словарём EN;
- пресеты конструктора (NETS в site.js) совпадают с portal/networks.py;
- contract-адреса в шаблонах — в кавычках (иначе YAML парсит 0x0 как int 0);
- в RPC-пресетах нет заглушек example.* / TODO.

Возвращает exit 0, если всё чисто.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DOCS = os.path.join(ROOT, "docs")
HTML = os.path.join(DOCS, "index.html")
JS = os.path.join(DOCS, "assets", "site.js")
CSS = os.path.join(DOCS, "assets", "site.css")

fails: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    print(f"{'PASS' if cond else 'FAIL'} {name} {detail}")
    if not cond:
        fails.append(name)


def main() -> int:
    for p in (HTML, JS, CSS, os.path.join(DOCS, ".nojekyll")):
        check(f"exists {os.path.relpath(p, ROOT)}", os.path.exists(p))
    html = open(HTML, encoding="utf-8").read()
    js = open(JS, encoding="utf-8").read()

    r = subprocess.run(["node", "--check", JS], capture_output=True, text=True)
    check("node --check site.js", r.returncode == 0, (r.stderr or "")[-200:])

    ids = re.findall(r'id="([A-Za-z0-9_-]+)"', html)
    check("no duplicate ids", len(ids) == len(set(ids)))
    used = set(re.findall(r'\$\("#([A-Za-z0-9_-]+)"\)', js))
    missing = sorted(u for u in used if u not in set(ids))
    check("all JS id-refs exist in HTML", not missing, str(missing))

    m = re.search(r"const EN=\{(.*?)\n\};", js, re.S)
    en_keys = set(re.findall(r"(?:[{,]\s*)([a-z][a-z0-9_]*):", "{" + m.group(1))) if m else set()
    html_keys = set(re.findall(r'data-i18n="([a-z0-9_]+)"', html))
    missing_en = sorted(html_keys - en_keys)
    check("i18n keys covered by EN", not missing_en, str(missing_en))

    # Пресеты конструктора: имена/chain — против portal/networks.py (канон),
    # database.path — против shipped config-файлов (ground truth конструктора).
    sys.path.insert(0, ROOT)
    from portal.networks import NETWORKS

    def shipped_db(cfg_file: str) -> str | None:
        p = os.path.join(ROOT, cfg_file)
        if not os.path.exists(p):
            return None
        s = open(p, encoding="utf-8").read()
        m = re.search(r"database:\s*\n\s*path:\s*\"?([^\"\s]+)\"?", s)
        return m.group(1) if m else None

    canon = {n["slug"]: n for n in NETWORKS}
    for mm in re.finditer(r"(\w+):\{file:\"([^\"]+)\",db:\"([^\"]+)\",chain:(\d+),rpc:\"([^\"]+)\"", js):
        slug, cfg, db, chain, rpc = mm.groups()
        c = canon.get(slug)
        if c is not None:
            check(f"NETS {slug} name+chain match portal",
                  c["config"] == cfg and c["chain_id"] == int(chain), f"{cfg}/{chain}")
        else:
            check(f"NETS {slug} config file shipped", os.path.exists(os.path.join(ROOT, cfg)), cfg)
        ref_db = shipped_db(cfg)
        if ref_db is None:
            check(f"NETS {slug} db has reference", False, "no shipped config")
        else:
            check(f"NETS {slug} db matches shipped config", ref_db == db, f"preset={db} ref={ref_db}")
        check(f"NETS {slug} rpc real", "example" not in rpc and "TODO" not in rpc, rpc)

    # contract-адреса обязаны быть в кавычках (голый 0x0…0 = int 0 в YAML 1.1).
    bare = re.findall(r"contract: 0x[0-9a-fA-F]+", js)
    check("no unquoted contract addresses", not bare, str(bare[:3]))

    return 1 if fails else 0


if __name__ == "__main__":
    raise SystemExit(main())
