"""Одна команда для проверки сайта: python tools/verify_site.py.

Статические проверки без браузера (stdlib only):
- node --check для docs/assets/common.js, field.js, demo.js;
- все $("#id") из JS существуют на страницах, где этот JS подключён;
- все data-i18n ключи всех страниц покрыты словарём EN (common.js);
- пресеты конструктора (NETS в demo.js) совпадают с portal/networks.py;
- contract-адреса в шаблонах — в кавычках (иначе YAML парсит 0x0 как int 0);
- в RPC-пресетах нет заглушек example.* / TODO;
- шапка/навигация одинаковы на всех 4 страницах.

Возвращает exit 0, если всё чисто.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DOCS = os.path.join(ROOT, "docs")
CSS = os.path.join(DOCS, "assets", "site.css")

sys.path.insert(0, ROOT)
from core.version import VERSION  # noqa: E402  (source of truth for the site version)

SITE_VERSION = f"v{VERSION}"

# Порядок ссылок в навигации: одинаковый на всех страницах.
NAV_LINKS = ["index.html", "field.html", "demo.html", "docs.html"]

# страница -> подключаемые JS (порядок как в <script>)
PAGES: dict[str, list[str]] = {
    "index.html": ["assets/common.js"],
    "field.html": ["assets/common.js", "assets/field.js"],
    "demo.html": ["assets/common.js", "assets/demo.js"],
    "docs.html": ["assets/common.js"],
}

fails: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    print(f"{'PASS' if cond else 'FAIL'} {name} {detail}")
    if not cond:
        fails.append(name)


def main() -> int:
    want = ([os.path.join(DOCS, n) for n in PAGES]
            + [os.path.join(DOCS, "assets", n) for n in ("common.js", "field.js", "demo.js")]
            + [CSS, os.path.join(DOCS, ".nojekyll"), os.path.join(DOCS, "robots.txt"),
               os.path.join(DOCS, "assets", "og.png"),
               os.path.join(DOCS, "manifest.webmanifest"), os.path.join(DOCS, "sw.js"),
               os.path.join(DOCS, "404.html"),
               os.path.join(DOCS, "offline.html"), os.path.join(DOCS, "terminal.html")])
    for p in want:
        check(f"exists {os.path.relpath(p, ROOT)}", os.path.exists(p))
    check("terminal.css restored as theme", os.path.exists(os.path.join(DOCS, "assets", "terminal.css")))
    pages = {n: open(os.path.join(DOCS, n), encoding="utf-8").read() for n in PAGES}
    js = {n: open(os.path.join(DOCS, "assets", n), encoding="utf-8").read()
          for n in ("common.js", "field.js", "demo.js")}

    for n in ("common.js", "field.js", "demo.js", "sw.js"):
        p = os.path.join(DOCS, "assets", n) if n != "sw.js" else os.path.join(DOCS, n)
        r = subprocess.run(["node", "--check", p], capture_output=True, text=True)
        check(f"node --check {n}", r.returncode == 0, (r.stderr or "")[-200:])
    for name, html in pages.items():
        check(f"{name}: pwa manifest linked", 'rel="manifest"' in html)
        check(f"{name}: pwa sw registered", "serviceWorker" in html)
        check(f"{name}: og meta present", 'property="og:image"' in html and "og.png" in html)
        check(f"{name}: theme-color present", 'name="theme-color"' in html)
    sw = os.path.join(DOCS, "sw.js")
    if os.path.exists(sw):
        swtxt = open(sw, encoding="utf-8").read()
        check("sw: wallets blob not precached", "wallets.enc" not in swtxt, "")
        check("sw: terminal.css precached", "terminal.css" in swtxt, "")
        for n in list(PAGES) + ["assets/common.js", "assets/field.js", "assets/demo.js",
                                "assets/terminal.css",
                                "404.html", "offline.html", "terminal.html"]:
            check(f"sw precaches {n}", f'"./{n}"' in swtxt, "")
    # Блок PnL на «Поле»: разметка, рендер и словарь — вместе, иначе блок
    # либо не отрисуется, либо останется с английскими подписями.
    field_html = pages.get("field.html", "")
    field_js = js.get("field.js", "")
    common_js = js.get("common.js", "")
    check("field.html: pnl block", 'id="pnlBox"' in field_html)
    check("field.html: pnl tables", 'id="tPnlTypes"' in field_html and 'id="tPnlReasons"' in field_html)
    check("field.js: pnl render", "function renderPnl" in field_js and "renderPnl(s.pnl)" in field_js)
    for key in ("pnl_h", "pnl_ok", "pnl_farm", "pnl_faucet", "pnl_fail", "pnl_types",
                "pnl_reasons", "pnl_note"):
        check(f"i18n key {key}", f"{key}:" in common_js)
    # Ссылка на эксплорер в отчёте не должна попадать в публичное состояние:
    # адреса кошельков в публичную ветку не идут.
    check("field.js: no explorer address link", "/address/" not in field_js)

    # Архив бота обязан содержать свежие исходники. Сравниваем байты
    # записей в zip с файлами worktree напрямую — без пересборки: deflate
    # зависит от zlib тулчейна, а mtime на свежем клоне врёт.
    import zipfile as _zf
    zipp = os.path.join(ROOT, "botforge", "harvest-botforge.zip")
    if os.path.exists(zipp):
        sys.path.insert(0, os.path.join(ROOT, "tools"))
        try:
            from build_botforge_zip import SOURCES as _srcs
            with _zf.ZipFile(zipp) as _z:
                _archived = {n: _z.read(n) for n in _z.namelist()}
            _expected = {}
            for _src_name, _arc_name in _srcs:
                _p = os.path.join(ROOT, "botforge", _src_name)
                _expected[_arc_name] = open(_p, "rb").read() if os.path.exists(_p) else None
            _same = _archived == _expected
            _detail = "" if _same else (
                "missing=" + str(sorted(set(_expected) - set(_archived))) +
                " extra=" + str(sorted(set(_archived) - set(_expected))) +
                " stale=" + str(sorted(k for k in _expected if k in _archived and _archived[k] != _expected[k])))
            check("botforge zip matches sources", _same, _detail)
        except Exception as e:  # noqa: BLE001 — проверка не должна ронять verify
            check("botforge zip matches sources", False, f"read failed: {e}")

    # Mini App (этап 3): клиент без фреймворков, id-ссылки целы, внешнего —
    # только telegram-web-app.js, инлайн-скриптов нет (CSP без nonce).
    mhtml = os.path.join(ROOT, "portal", "static", "miniapp", "app.html")
    mjs = os.path.join(ROOT, "portal", "static", "miniapp", "app.js")
    mcss = os.path.join(ROOT, "portal", "static", "miniapp", "miniapp.css")
    for p in (mhtml, mjs, mcss):
        check(f"exists {os.path.relpath(p, ROOT)}", os.path.exists(p))
    if os.path.exists(mjs):
        r = subprocess.run(["node", "--check", mjs], capture_output=True, text=True)
        check("node --check miniapp app.js", r.returncode == 0, (r.stderr or "")[-200:])
    if os.path.exists(mhtml):
        mh = open(mhtml, encoding="utf-8").read()
        check("miniapp: only telegram.org external",
              "https://telegram.org/js/telegram-web-app.js" in mh
              and mh.count("http") <= 4, "")
        check("miniapp: no inline scripts",
              not re.search(r"<script(?![^>]*src=)", mh), "")

    # Шапка/навигация одинаковы везде: те же 4 ссылки, ровно один aria-current.
    navs = {}
    navs_norm = {}
    for name, html in pages.items():
        m = re.search(r"<nav.*?</nav>", html, re.S)
        navs[name] = m.group(0) if m else ""
        links = re.findall(r'href="([^"]+)"', navs[name])
        check(f"{name}: nav has 4 page links", links == NAV_LINKS, str(links))
        check(f"{name}: single aria-current", navs[name].count('aria-current="page"') == 1, "")
        # Для «везде одинаково» aria-current вырезаем: на каждой странице он
        # законно стоит на своей ссылке — это и есть активный пункт меню.
        navs_norm[name] = navs[name].replace(' aria-current="page"', "")
    check("nav identical on all pages", len(set(navs_norm.values())) == 1, "")

    # Внутренние ссылки и ресурсы резолвятся в реальные файлы docs/: страницы
    # лежат в корне docs/, поэтому относительный путь проверяем от DOCS.
    link_pages = dict(pages)
    for extra in ("terminal.html", "offline.html", "404.html"):
        p = os.path.join(DOCS, extra)
        if os.path.exists(p):
            link_pages[extra] = open(p, encoding="utf-8").read()
    for name, html in link_pages.items():
        refs = set(re.findall(r'(?:href|src)="([^"]+)"', html))
        broken = []
        for r in sorted(refs):
            if re.match(r"^(?:[a-z][a-z0-9+.-]*:|//|#)", r, re.I):
                continue  # внешняя ссылка, data:, чистый якорь
            rel = r.split("#")[0].split("?")[0]
            if rel and not os.path.exists(os.path.join(DOCS, rel)):
                broken.append(r)
        check(f"{name}: internal links resolve", not broken, str(broken))

    # Старые якоря ленты редиректятся на существующие страницы.
    m = re.search(r"var map=\{.*?\};", pages["index.html"], re.S)
    targets = re.findall(r'"([a-z]+\.html)"', m.group(0)) if m else []
    check("legacy anchor map present", bool(targets), "")
    check("legacy anchor map targets exist",
          all(os.path.exists(os.path.join(DOCS, t)) for t in targets), str(targets))

    # Палитра команд и появление при скролле: разметка и флаг rv — на всех
    # страницах (общий common.js подключается везде), плюс страховка от
    # «контент навсегда остался невидимым», если common.js не загрузится.
    for name, html in pages.items():
        check(f"{name}: command palette markup",
              all(f'id="{i}"' in html for i in ("cmdk", "cmdkIn", "cmdkList", "cmdkScrim")), "")
        check(f"{name}: rv boot flag before paint", 'classList.add("rv")' in html, "")
        check(f"{name}: rv failsafe", 'classList.remove("rv")' in html, "")
        check(f"{name}: reveal targets marked", "data-reveal" in html, "")
    css_txt = open(os.path.join(DOCS, "assets", "site.css"), encoding="utf-8").read()
    check("css: reveal scoped to .rv", ".rv [data-reveal]" in css_txt, "")
    check("css: palette styles", ".cmdk-box" in css_txt and ".cmdk-item" in css_txt, "")
    rm = css_txt.split("prefers-reduced-motion:reduce")[-1]
    check("css: reveal disabled under reduced-motion", ".rv [data-reveal]" in rm, "")

    # Съёмка скриншотов ждёт общий якорь готовности страницы; он обязан быть
    # на каждой странице, иначе playwright падает по таймауту (уже случилось
    # на #proView — он скрыт в режиме «Новичок»).
    shoot_path = os.path.join(ROOT, "tools", "shoot_site.py")
    shoot = open(shoot_path, encoding="utf-8").read() if os.path.exists(shoot_path) else ""
    m = re.search(r'PAGE_READY\s*=\s*"([^"]+)"', shoot)
    ready = m.group(1).lstrip("#") if m else ""
    check("shoot_site: PAGE_READY declared", bool(ready), str(ready))
    for name, html in pages.items():
        check(f"{name}: has #{ready} (shoot_site anchor)", f'id="{ready}"' in html, "")

    for name, html in pages.items():
        ids = re.findall(r'id="([A-Za-z0-9_-]+)"', html)
        check(f"{name}: no duplicate ids", len(ids) == len(set(ids)))
    # id-ссылки JS: каждый файл проверяется против страниц, где он подключён.
    for name, included in PAGES.items():
        html = pages[name]
        pool = set(re.findall(r'id="([A-Za-z0-9_-]+)"', html))
        for asset in included:
            code = js[os.path.basename(asset)]
            used = set(re.findall(r'\$\("#([A-Za-z0-9_-]+)"\)', code))
            used |= set(re.findall(r'getElementById\("([A-Za-z0-9_-]+)"\)', code))
            dynamic = {"liveRetry"}
            missing = sorted(u for u in used if u not in pool and u not in dynamic)
            check(f"{asset} id-refs exist on {name}", not missing, str(missing))
    # Wizard-шаги w1..w3/st1..st3 обязаны существовать статически.
    for wid in ("w1", "w2", "w3", "st1", "st2", "st3"):
        check(f"wizard step #{wid}", f'id="{wid}"' in pages["demo.html"])

    m = re.search(r"const EN=\{(.*?)\n\};", js["common.js"], re.S)
    en_keys = set(re.findall(r"(?:[{,]\s*)([a-z][a-z0-9_]*):", "{" + m.group(1))) if m else set()
    html_keys: set[str] = set()
    for html in pages.values():
        html_keys |= set(re.findall(r'data-i18n(?:-ph|-aria)?="([a-z0-9_]+)"', html))
    missing_en = sorted(html_keys - en_keys)
    check("i18n keys covered by EN", not missing_en, str(missing_en))
    check("terminal theme wired",
          '"terminal"' in js["common.js"] and "terminal.css" in pages["index.html"]
          and 'data-theme="terminal"' in pages["index.html"], "")

    # Ссылки на несуществующие ветки GitHub: master удалён, канон — main.
    # Проверяем весь docs/, а не только страницы из PAGES (футеры, 404).
    dead_branch = []
    for fname in sorted(os.listdir(DOCS)):
        if not fname.endswith(".html"):
            continue
        html = open(os.path.join(DOCS, fname), encoding="utf-8").read()
        if "blob/master/" in html or "tree/master/" in html:
            dead_branch.append(fname)
    check("no links to deleted master branch", not dead_branch, str(dead_branch))

    # Версия сайта — из core/version.py, а не из копии в проверялке. Тикеры,
    # шапки и футеры должны говорить одно и то же.
    versions = set()
    for name, html in list(pages.items()) + [("field.js", js["field.js"]), ("common.js", js["common.js"])]:
        versions |= set(re.findall(r"\bv\d+\.\d+\.\d+\b", html))
    check("single site version everywhere", versions == {SITE_VERSION}, str(sorted(versions)))
    # Числа тестов больше не выносим на витрину: они быстро устаревают и
    # превращают проект в отчёт. Проверяем, что счётчики не вернулись.
    count_claims = set()
    for name, html in list(pages.items()) + [("field.js", js["field.js"]), ("common.js", js["common.js"])]:
        count_claims |= set(re.findall(r"\b\d{3,}\s*(?:TESTS|ТЕСТОВ|tests|тестов)\b", html, re.I))
        count_claims |= set(re.findall(r"\b\d{3,}\s*[·-]\s*\d+\s*(?:файл|files?)\b", html, re.I))
    check("no numeric test-count claims", not count_claims, str(sorted(count_claims)))
    # Честность витрины: никаких процентов сапплая и дропов в коде сайта и каталоге сетей.
    drop_hype = []
    for label, text in [("field.js", js["field.js"]), ("common.js", js["common.js"])] + list(pages.items()):
        if re.search(r"5%\s*сапплая", text):
            drop_hype.append(label)
    try:
        nets_src = open(os.path.join(ROOT, "portal", "networks.py"), encoding="utf-8").read()
        if re.search(r"5%\s*сапплая", nets_src):
            drop_hype.append("portal/networks.py")
    except OSError:
        pass
    check("no drop-supply hype", not drop_hype, str(drop_hype))
    # Кран один и тот же везде: прямые claim-URL Robinhood совпадают.
    faucet_mismatch = []
    try:
        nets_src = open(os.path.join(ROOT, "portal", "networks.py"), encoding="utf-8").read()
        if "faucet.testnet.chain.robinhood.com/claim" not in nets_src:
            faucet_mismatch.append("portal/networks.py: no direct claim URL")
    except OSError as e:
        faucet_mismatch.append(f"portal/networks.py unreadable: {e}")
    if "faucet.testnet.chain.robinhood.com/claim" not in js["field.js"]:
        faucet_mismatch.append("field.js: no direct claim URL")
    check("faucet URLs consistent", not faucet_mismatch, str(faucet_mismatch))
    # SW не должен класть в кэш сырой Request с query: иначе рост ключей.
    swtxt = open(os.path.join(DOCS, "sw.js"), encoding="utf-8").read()
    check("sw caches via query-stripped key", "c.put(cacheKey" in swtxt or "c.put(stateKey" in swtxt, "")
    # Переключение языка обязано перерисовывать и JS-тексты поля
    # (зум/инспектор/плейсхолдер/пустые чарты), а не только data-i18n.
    check("field.js repaints on hv:lang", js["field.js"].count("hv.onLang") >= 2, "")

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

    demo_js = js["demo.js"]
    canon = {n["slug"]: n for n in NETWORKS}
    for mm in re.finditer(r"(\w+):\{file:\"([^\"]+)\",db:\"([^\"]+)\",chain:(\d+),rpc:\"([^\"]+)\"", demo_js):
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
    bare = re.findall(r"contract: 0x[0-9a-fA-F]+", demo_js)
    check("no unquoted contract addresses", not bare, str(bare[:3]))

    # Версия портала — из core.version, а не из копий в коде и статике.
    # HTML портала несёт плейсхолдер, бот — _FOOT из bot_ops.
    from portal import render_page

    portal_html = ["portal/static/index.html", "portal/static/login.html",
                   "portal/static/miniapp/app.html"]
    for rel in portal_html:
        text = open(os.path.join(ROOT, rel), encoding="utf-8").read()
        check(f"{rel} version via placeholder",
              "{{HARVEST_VERSION}}" in text
              and not re.findall(r"\bv?\d+\.\d+\.\d+\b", text), "")
        rendered = render_page(os.path.join(ROOT, rel))
        check(f"{rel} renders version {SITE_VERSION}",
              "{{HARVEST_VERSION}}" not in rendered
              and SITE_VERSION.lstrip("v") in rendered, "")
    stale_py = []
    for rel in ["portal/bot_ops.py", "portal/bot_telegram.py", "portal/api.py",
                "portal/miniapp.py", "portal/__init__.py"]:
        text = open(os.path.join(ROOT, rel), encoding="utf-8").read()
        for m in re.findall(r"(?<![\d.])(\d+\.\d+\.\d+)(?!\.?\d)", text):
            if m != SITE_VERSION.lstrip("v"):
                stale_py.append(f"{rel}: {m}")
    check("portal py has no stale version copies", not stale_py, str(stale_py))
    # Поле не врёт про живость: running — проба лока, network — из имени БД,
    # health/dyn — только из свежего снимка метрик, а не захардкоженные 1.0/0.
    pub = open(os.path.join(ROOT, "publish_dash.py"), encoding="utf-8").read()
    check("publish derives running/network",
          "_farm_running(DB)" in pub and "_network_slug(DB)" in pub
          and '"running": False' not in pub, "")
    check("publish reads metrics snapshots",
          "metrics_snapshots" in pub and "_fresh_ts" in pub
          and '"health_factor": 1.0' not in pub, "")
    # Снимки пишут оба пути фарма: демон (recorder-цикл) и run_once пула
    # (auto/CLI/экраны через хук в конце цикла) плюс финал в auto.
    pool_src = open(os.path.join(ROOT, "core", "pool.py"), encoding="utf-8").read()
    auto_src = open(os.path.join(ROOT, "auto.py"), encoding="utf-8").read()
    check("pool records health snapshot",
          "async def record_health_snapshot" in pool_src
          and "await self.record_health_snapshot(len(wallets))" in pool_src, "")
    check("auto records health snapshot",
          "await pool.record_health_snapshot(len(wallets))" in auto_src, "")

    # Контракт state.json: продюсер (publish.collect) против потребителей
    # (поле, боты). Типы и ключи, а не только наличие блоков в HTML.
    import asyncio as _asyncio
    import sqlite3 as _sqlite3
    import tempfile as _tempfile
    import publish_dash as _pd
    with _tempfile.TemporaryDirectory() as _td:
        _dbp = os.path.join(_td, "farming_robinhood.db")
        _con = _sqlite3.connect(_dbp)
        _con.execute("CREATE TABLE wallets (address TEXT PRIMARY KEY, private_key TEXT, "
                     "mnemonic TEXT, total_actions INTEGER DEFAULT 0, "
                     "total_attempts INTEGER DEFAULT 0, last_cycle INTEGER)")
        _con.execute("CREATE TABLE cycle_history (id INTEGER PRIMARY KEY AUTOINCREMENT, "
                     "started_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP, mode TEXT, "
                     "duration_s REAL, wallets INTEGER, wallets_ok INTEGER, "
                     "actions_ok INTEGER, errors INTEGER)")
        _con.commit()
        _con.close()
        _old_db, _pd.DB = _pd.DB, _dbp
        try:
            _st = _asyncio.run(_pd.collect())
        finally:
            _pd.DB = _old_db
    _bad = []
    if not isinstance(_st.get("running"), bool):
        _bad.append("running")
    if _st.get("network") != "robinhood":
        _bad.append("network")
    if not isinstance(_st.get("wallet_count"), int):
        _bad.append("wallet_count")
    if not isinstance(_st.get("updated_at"), str):
        _bad.append("updated_at")
    _pool = _st.get("pool") or {}
    for _k in ("actions", "errors", "processed", "cycles", "dropped"):
        if not isinstance(_pool.get(_k), int):
            _bad.append("pool." + _k)
    for _k in ("health_factor",):
        if _st.get(_k) is not None and not isinstance(_st.get(_k), (int, float)):
            _bad.append(_k)
    if _pool.get("dyn_workers") is not None and not isinstance(_pool.get("dyn_workers"), (int, float)):
        _bad.append("pool.dyn_workers")
    _wc = _st.get("wallet_cells") or {}
    if _wc.get("v") != 2 or not isinstance(_wc.get("cells"), str) or not isinstance(_wc.get("details"), list):
        _bad.append("wallet_cells")
    for _n in _st.get("networks", []):
        for _k in ("tagline_en", "description_en", "rewards_en", "activities_en"):
            if _k not in _n:
                _bad.append("networks." + _k)
                break
    check("state.json schema contract", not _bad, str(sorted(set(_bad))))

    # exe-spec ссылается на существующие файлы: протухшие datas/entry —
    # битый дистрибутив, а CI exe не собирает и не заметит.
    spec = open(os.path.join(ROOT, "harvest.spec"), encoding="utf-8").read()
    _spec_missing = []
    if not os.path.exists(os.path.join(ROOT, "main.py")):
        _spec_missing.append("main.py")
    for _src in re.findall(r"\(\s*'([^']+)'\s*,\s*'[^']*'\s*\)", spec):
        if not os.path.exists(os.path.join(ROOT, _src)):
            _spec_missing.append(_src)
    check("harvest.spec datas exist", not _spec_missing, str(_spec_missing))

    # Токены: сгенерированный CSS свеж (пересборка не должна ничего менять).
    before = open(os.path.join(DOCS, "assets", "tokens.css"), encoding="utf-8").read()
    r = subprocess.run([sys.executable, os.path.join(ROOT, "tools", "build_tokens.py")],
                       capture_output=True, text=True)
    after = open(os.path.join(DOCS, "assets", "tokens.css"), encoding="utf-8").read()
    check("tokens.css fresh", r.returncode == 0 and before == after)

    # Обязательные узлы по страницам.
    required: dict[str, list[str]] = {
        "index.html": ["navToggle", "mainNav", "tickerIn"],
        "field.html": ["navToggle", "mainNav", "fieldGrid", "fieldEmpty", "fieldLegend",
                       "fieldMore", "lastCycle", "cellTip", "fxFilters", "fxZoom", "fxQ",
                       "fxInspector", "updated", "term", "dot", "status", "net", "health",
                       "liveAlert", "staleAlert", "nets", "chartMain", "chartProc", "tWallets", "tHistory"],
        "demo.html": ["navToggle", "mainNav", "modeNew", "modePro", "newView", "proView",
                      "cfgNet", "cfgOut", "cmdOut", "wRows", "simLog", "simChart",
                      "vText", "vOut"],
        "docs.html": ["navToggle", "mainNav"],
    }
    for name, els in required.items():
        html = pages[name]
        for el in els:
            check(f"{name} has #{el}", f'id="{el}"' in html)
    for sec, page in [("field", "field.html"), ("faucet", "field.html"), ("bot", "field.html"),
                      ("app", "demo.html"), ("overview", "index.html"),
                      ("networks", "index.html"), ("quickstart", "index.html"),
                      ("specs", "docs.html"), ("arch", "docs.html"), ("portal", "docs.html"),
                      ("security", "docs.html"), ("tests", "docs.html"), ("license", "docs.html")]:
        check(f"{page} has section #{sec}", f'id="{sec}"' in pages[page])
    css = open(os.path.join(DOCS, "assets", "site.css"), encoding="utf-8").read()
    for cls in [".field-grid", ".cell.c-o", ".cell-tip", ".dot.mut",
                ".termline.live", ".is-loading", ".nav-toggle", "nav.main.open"]:
        check(f"css has {cls}", cls in css)

    # Переименование Поле: никаких «пульт» в пользовательском сайте и боте.
    for rel in ["docs/index.html", "docs/field.html", "docs/demo.html", "docs/docs.html",
                "docs/assets/common.js", "docs/assets/field.js", "docs/assets/demo.js",
                "portal/bot_ops.py", "portal/bot_telegram.py"]:
        txt = open(os.path.join(ROOT, rel), encoding="utf-8").read()
        check(f"no пульт in {rel}", re.search(r"[Пп]ульт", txt) is None)

    # Блок PnL в локальном портале (веб-дашборд): разметка, рендер и стили
    # знаковых значений — вместе, иначе блок либо не отрисуется, либо останется
    # без подкраски дельт.
    portal_index = os.path.join(ROOT, "portal", "static", "index.html")
    portal_js = os.path.join(ROOT, "portal", "static", "app.js")
    portal_css = os.path.join(ROOT, "portal", "static", "app.css")
    if os.path.exists(portal_index):
        phtml = open(portal_index, encoding="utf-8").read()
        check("portal: pnl panel", 'id="pnlPanel"' in phtml)
        pnl_fields = ("Ok", "Farm", "Faucet", "Fail", "Rate", "Sample", "Window", "When", "Reasons")
        check("portal: pnl fields", all(f'id="pnl{k}"' in phtml for k in pnl_fields))
    if os.path.exists(portal_js):
        pjs = open(portal_js, encoding="utf-8").read()
        check("portal: renderPnl", "function renderPnl" in pjs and "renderPnl(s.pnl)" in pjs)
    if os.path.exists(portal_css):
        pcss = open(portal_css, encoding="utf-8").read()
        check("portal: pnl sign colors", ".metric-value.pos" in pcss and ".metric-value.neg" in pcss)

    return 1 if fails else 0


if __name__ == "__main__":
    raise SystemExit(main())
