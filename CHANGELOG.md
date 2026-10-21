# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added
- **Master-ключ ищется рядом со своей БД и проверяется пробной расшифровкой.**
  Кандидаты: `FARMER_MASTER_KEY` → `database.master_key` (относительно CWD) →
  `master.key` рядом с `database.path`. Если кандидатов несколько, выигрывает
  тот, который реально расшифровывает первый seed этой БД: ферма могла
  стартовать из другой папки, и «тот самый» файл в CWD оказывался чужим.
  Побочный эффект — `FARMER_MASTER_KEY` больше не обязателен: ключ лежит
  рядом с базой, которую защищает, и его не надо держать в переменной
  окружения (а она видна другим процессам пользователя)
- **Команда `/pnl` в обоих ботах.** Локальный бот портала берёт агрегаты из
  таблицы `pnl_snapshots`, облачный BotForge-бот — из `state.json`. Показывает
  успешных действий и success rate, Δ фарма и Δ крана, выборку кошельков и
  **причины отказов** с числами. Адреса кошельков не показываются
- **PnL последнего прогона на «Поле»** (дашборд). Ферма пишет агрегаты PnL в
  таблицу `pnl_snapshots`, публикатор кладёт их в `state.json`, сайт рисует
  блок: успешных действий и success rate, Δ фарма и Δ крана, отказы, разбивку
  по типам действий и **причинам отказов**. В публичную ветку идут только
  счётчики — адреса кошельков не раскрываются (детальный отчёт с разбивкой по
  кошелькам и ссылками на эксплорер остаётся локальным, `pnl_report.html`).
  Версия сайта → 2.4.0, кэш Service Worker переведён на `harvest-v2.4.0`
- **Сборщик деплой-архива бота** (`tools/build_botforge_zip.py`): `botforge/harvest-botforge.zip`
  собирается одной командой и не может разойтись с исходниками. Раньше архив
  собирался руками, и после bump-зависимостей в нём лежал устаревший
  `requirements.txt` — `verify_site` это ловил, но чинить приходилось вручную.
  Сборка детерминированная (фиксированные метки записей), состав прежний:
  `main.py` (копия `bot.py`), `bot.py`, `requirements.txt`, `README.md`
- **Ферма не уходит в холостую работу.** Если кран недоступен и на кошельках
  нет средств, прогон останавливается до отправки транзакций: каждый перевод
  тогда заведомо падает («insufficient funds»), сеть сожжёт лимит, а лог
  забьётся бесполезными отказами. Проверяется выборка кошельков (5 шт.);
  если средства заведены вручную — фарм идёт. Обход: `--force-farm`.
  `run_once` теперь возвращает код (0 — ок, 1 — останов), он доходит до
  кода возврата процесса и до планировщика
- **PnL-отчёт по прогону фермы** (`core/pnl.py`, `python auto.py` → `pnl_report.html`):
  самодостаточный HTML без внешних файлов, со ссылками на эксплорер (адреса
  и tx-хеши) и разбивкой по фазам. «PnL» на тестнете — движение нативного
  баланса: Δ по крану и по фарму, разбивка по типам действий, success rate,
  по-кошелёчные «до/после», причины отказов. Балансы снимаются по
  равномерной выборке (`--pnl-sample`, 0 = все) — 1000 кошельков × 2 снимка
  это 2000 RPC-вызовов ради отчёта. Флаги: `--pnl-sample N`, `--pnl-out PATH`,
  `--skip-pnl`. Точный `gasUsed` недоступен (`send_transfer` отдаёт только
  tx_hash), поэтому раздел про газ честно помечен как оценка; на Δ баланса
  это не влияет
- Шифрованный блоб кошельков вместо MotherDuck: `tools/sync_wallets_blob.py`
  (AES-256-GCM, ключ `WALLETS_KEY` 64 hex только в env) → `wallets.enc.json`
  в gh-pages рядом со `state.json`; BotForge-бот скачивает и расшифровывает
  тем же ключом из Secrets. Нового провайдера нет, в открытом виде ключи
  на публичный хост не попадают; без ключа с обеих сторон — штатный пропуск.
  Выбор ключа БД — пробной расшифровкой (ферма могла стартовать из другой
  папки, чем лежит БД). `duckdb` удалён из зависимостей.
- BotForge-бот: кэш расшифрованного блоба в памяти на 300с (+ETag/304 —
  листание 250 страниц /wallets не качает 246КБ заново), понятная ошибка
  при 404 (блоб ещё не опубликован); `verify_site` следит за свежестью
  `harvest-botforge.zip` и отсутствием блоба в precache SW.
- Покрытие 100%: CI-гейты `portal` и `core` подняты до `--cov-fail-under=100`;
  новые добивочные тесты `test_cov_100*` (shield, api, bot_telegram/render,
  farm/miniapp); исправлен мёртвый код `/alerts off` в `portal/bot_telegram.py`
  (проверка `off` до `float()`).
- Команды бота /pause /resume /stop /faucet /rpc /scheduler /alerts /cancel
  (все в setMyCommands), лёгкий антифлуд чтений 5/10/15с, /faucet с разбором
  по стратегиям, /alerts с порогом, /profiles с объяснением пустого флота
- Mini App API: GET/POST /alerts, GET /rpc, GET /faucet (со стратегиями),
  POST /network/switch; поле: СТОП + кран/RPC-строки, алерты и расписание
  в Зоне, запуск сети из визарда, tru()-двуязычие, clipboard-фолбэк
- WS-тикеты: POST /api/ws/token (60с в query, сессия только в cookie),
  кап до prepare (503), revoke кикает живой сокет
- PWA сайта: manifest, sw.js (state.json без query-роста, только r.ok),
  offline.html, иконки 192/512, apple-touch-icon
- Пресеты человечности в конструкторах сайта (Stealth/Balanced/Degen):
  сегменты в t-config (секция behavior в генерируемом YAML) и t-cmd
  (Degen включает --fast); пресеты NETS синхронизированы с shipped YAML
- Текстовая команда /menu (экран Меню: все вторичные разделы)
- Pre-flight крана: таймаут 8→12 с для медленных линков
- verify_site: окна faucet/bot/specs, фильтры поля и пресеты в обязательных ID
- Поведенческие knob'ы диапазоном: любой knob вероятности/силы в `behavior:`
  (`rest_probability`, `odd_amount_prob`, `faucet_skip_prob`, `daily_window`,
  `burst_prob`, `burst_multiplier`, `gas_deviation`, `amount_jitter`,
  `action_mix`) принимает `[lo, hi]` — розыгрыш на кошелёк детерминированно
  от адреса (`core/behavior.py::_draw_bounded`, скаляр — как раньше);
  `cfg_hi` для бюджетов таймаутов пула; `config_vibevibe.yaml`: секция
  `behavior` с широкими диапазонами, workers 20→32, кран 8→16,
  действий за цикл [3,8]→[3,10]
- Оси ритмов профилей: `weekend_strength` (выходные активнее),
  `day_start`/`day_width` (личное окно: внутри ×0.85, снаружи ×1.8
  к вероятности пропуска; `day_of_week_factor`/`hour_window_factor`,
  учет в `Farmer._skip_probability`); флот добит до 1000 кошельков;
  паритет behavior-диапазонов и мощности во всех сетях
- P4: PNG-карточка статуса (`portal/status_card.py`, палитра tokens.json,
  кнопка «Карточка» в диагностике бота, Pillow в requirements-portal +
  fonts-dejavu в Docker), scheduler-view и CSV истории в Mini App
  (`GET /api/miniapp/scheduler`), ruff-чистота всего репо, Dockerfile
  зафиксирован на python:3.12-slim
- Mini App (этап 3 ТЗ): `portal/miniapp.py` (`/app/`, `/api/miniapp/*`,
  initData в заголовке, uniform-401, one-time confirm-токены, ETag/304,
  экспорт тем же кодом бота в чат), клиент без фреймворков
  (`portal/static/miniapp/`), `PORTAL_MINIAPP_URL`/`PORTAL_MINIAPP_PUBLIC`,
  Caddy только на Mini App-пути, auth отклоняет будущие auth_date
- Бот v1 (этап 2 ТЗ): `portal/bot_render.py` (единый рендер, RU/EN),
  `portal/wallet_cells.py` (общие клетки iwoer), `portal/bot_settings.py`
  (режим/язык), домашнее Поле на editMessageText, навигация `v1:*` ≤ 64 байт,
  мастер 3 шага с TTL, Live 10 мин, шифрованный экспорт .enc, ключи с
  подтверждением, setMyCommands RU, трекер 409, редакция токена в логах
- Палитра команд на сайте: `Ctrl+K` (или `/`) — переход между страницами и по
  разделам текущей страницы, смена темы и языка. Фильтр по подстроке, `↑↓`/`Enter`,
  `Esc`; собирается при открытии, поэтому подписи всегда на текущем языке
- Появление секций при скролле (`IntersectionObserver`), сетки раскрываются
  каскадом через `--d`. Прячет только класс `rv`, который ставит инлайн-скрипт в
  `<head>` и только при разрешённом motion: без JS и при `prefers-reduced-motion`
  контент виден сразу. Через 2.5с после `load` класс снимается — если
  `common.js` не загрузился, страница остаётся читаемой
- Состояние фильтра поля живёт в URL (`?f=w&z=1&q=12`): переживает перезагрузку
  и шарится ссылкой. Выбранная клетка помечается рамкой (`.cell.sel`)
- i18n научился переводить `placeholder` через `data-i18n-ph`

### Changed
- Сайт разбит с одной ленты на 4 страницы: `index.html` (главная: обзор, сети,
  быстрый старт), `field.html` (Поле, кран, бот), `demo.html` (демо-зона),
  `docs.html` (факты, архитектура, портал, безопасность, тесты, лицензия)
- `docs/assets/site.js` нарезан на `common.js` (темы/i18n/nav/копирование,
  общий API `window.hv`), `field.js` (живое поле) и `demo.js` (конструкторы
  и симулятор); страничный скрипт выходит по своему якорю (`#fieldGrid`/`#proView`)
- Старые якоря ленты (`#field`, `#app`, `#specs`, …) редиректятся на новые
  страницы, поэтому старые закладки и ссылки из Telegram не ломаются
- `sw.js`: кэш `v2.3.1`, в precache новые страницы и скрипты вместо `site.js`
- `publish_dash.py`: новые страницы в `SITE_FILES`, счётчики тестов
  подставляются во все `*.html` + `assets/*.js` (glob вместо хардкода)
- `verify_site.py`: проверки `node --check`/`data-i18n`/`#id` по страницам,
  сравнение nav без `aria-current` (активный пункт у каждой страницы свой);
  внутренние `href`/`src` всех страниц резолвятся в реальные файлы `docs/`,
  цели legacy-маппинга якорей существуют

### Fixed
- **`auto.py` теперь видит ту же БД, что публикатор и портал.** Ферма брала путь
  только из `config.database.path`, а `publish_dash.py` и портал — из
  `PORTAL_DB`. С переменной окружения ферма писала в одну базу, а сайт
  получал кошельки из другой. Теперь источник пути один — `PORTAL_DB`
  (с откатом на конфиг)
- **Проба крана chainstack проверяет ключ, а не только MCP-сессию.** Сервер
  открывал сессию и с недействительным ключом, поэтому стратегия выглядела
  живой, а ферма уходила в тысячи заведомо падающих переводов: «Invalid API
  key» приходил только на `tools/call request_testnet_funds`. Теперь после
  handshake делается read-only вызов `get_organization` — он не тратит квоту
  крана, но честно отвечает «Invalid API key». Проба сообщает
  «недействительный API-ключ» вместо вводящего в заблуждение «MCP handshake ok»
- **Линт-режим всего репозитория убран в ноль** (было 60 замечаний ruff:
  51 неотсортированный импорт, 4 алиаса `asyncio.TimeoutError`, 2 «слепых»
  `assertRaises(Exception)`, 2 `raise` без `from` в `except`, 1 лишняя
  переменная цикла). Попутно тесты стали строже: `assertRaises` уточнён до
  конкретных типов (`SanitizationError`, `HTTPException` с проверкой кода 401),
  а проверка ответов бота теперь действительно ищет ожидаемый текст, а не
  просто «ответ непустой». `portal/api.py` больше не теряет цепочку исключений
  (`from None` / `from e`)
- **Причина отказа транзакции больше не теряется.** `send_transfer()` возвращал
  `None` в трёх разных случаях (нет ресипта / реверт / исключение), и причина
  уходила только в лог: в `actions_log` попадало голое `to=..., amount=...`.
  В живой БД так лежали все 3778 записей ото 16.10 — 100% отказов без единого
  объяснения. Теперь `send_transfer_with_reason()` возвращает `(tx_hash, reason)`,
  `actions.py` пишет `error=<причина>; ...` в `details`, а PnL-отчёт показывает
  раздел «Причины отказов»
- **Проба ключа БД больше не оставляет мусор в рабочей папке.**
  `tools/sync_wallets_blob._master_key()` подбирал кандидата через
  `resolve_master_key()`, а тот создаёт отсутствующий файл `master.key`.
  Публикатор каждые 30 минут оставлял в CWD случайный 32-байтный ключ, а ферма,
  запущенная из этой папки, подхватила бы его и зашифровала новые кошельки
  чужим ключом (старые в той же БД читались бы другим). Теперь файловые
  кандидаты только читаются (`_file_key()`), ключ проверяется пробной
  расшифровкой, как раньше
- `tools/shoot_site.py` ждал на «Демо» `#proView`, но по умолчанию открыт режим
  «Новичок» и `#proView` скрыт — съёмка падала по таймауту. Ждём `#modeNew`,
  видимый в обоих режимах (`.play-tabs` тоже не годится: это вкладки Про-режима)
- Скриншоты для README пересняты с многостраничного сайта: раньше они показывали
  старую ленту, а на `Демо` кадр «про-справочник» снимался с неверным состоянием
- **Пустой блоб кошельков больше не публикуется.** `sync_wallets_blob.sync()`
  возвращает `ok=True` и `count=0`, когда не расшифровал ни одного кошелька (не та
  БД через `PORTAL_DB`, не тот `master.key`, пустая таблица). `publish_dash.py`
  публиковал такой блоб как есть и молча оставлял облачного бота без кошельков
  (пустой `wallets.enc.json` в gh-pages). Теперь `count <= 0` — штатный пропуск
  (exit 2) с явным сообщением: старый блоб в gh-pages сохраняется
- `tools/shoot_site.py` после разбивки снимал несуществующие якоря (`#live`,
  `#app`, `#modePro`) на главной и ждал `navLinks == 11` при четырёх ссылках —
  матрица приёмки падала на каждом кадре. Теперь кадр задаёт свою страницу,
  готовность ждётся по узлу страницы (`#fieldGrid`/`#proView`/`#mainNav`),
  скролл/бургер/навигация/ошибки JS проверяются на всех 4 страницах
- `docs/terminal.html` вёл на `index.html#field` и прыгал на «Поле» вторым
  редиректом — теперь один хоп, прямо на `field.html#field`
- Линт: `except (OSError, IOError)` → `OSError` (`publish_dash.py`, ловится
  конфигом `[tool.ruff]`, но не правилом `--select=E,F,W` из CI)

### Fixed
- Кран: `ensure_balance` честно по `target_balance` (частичный долив —
  неуспех); `validate()` пишет разбор `{type,url,alive,reason}` в
  `_last_probe`/`probe_report()`; ноль живых — ERROR с причинами;
  auto.py печатает ok/dead построчно; фермер пропускает нулевой баланс
  даже с `force_farm` (больше нет шторма `have 0`)
- Breaker на ноду (`_breakers[url]`): ошибки A не блокируют B; auto-replay
  ротирует RPC только при росте `transport_failures()` (revert повторяется
  на той же ноде); nonce-гонка закрыта гейтом создания; баланс
  инвалидируется и при `receipt is None`; суммы через `Decimal`
- Дешифровка fail-closed (`decrypt_seed` без ключа — исключение, а не
  возврат шифротекста); метрики — свежие N; PID-файл с truncate;
  `_restrict_db_files` через `to_thread`; PG plain-`INSERT` без тихого
  `DO NOTHING`; netdiscovery с pooled-сессией и `expected_chain_id`;
  `.enc` с варнингом при неудалённом исходнике; `resolve_bundled` ищет
  и в `/app` (volume поверх `/app/data`)
- Сайт: мобильное меню — компактная сетка, подложка, закрытие по тапу мимо
  и Escape, бургер ≡/✕; `terminal.css` больше не сплющивает графики,
  не прячет колонки таблиц и не ломает `.grid2` на десктопе; NaN в YAML
  и `--wallets -5` в команде закрыты валидацией; сайт полностью RU/EN
  (статус, тосты, симулятор, валидатор); табы со стрелками, сетка поля
  со стрелками, все поля с подписями
- Портал: планировщик фронтенда переписан под `/api/scheduler/tasks`
  (было вечное 404); экспорт только POST; WebSocket-тикеты; 400 вместо
  404/500-молчаний; цвета групп валидируются; модалка scheduler
  с нормальными интервалами вместо cron-заглушки
- Сборки: `deploy/Dockerfile` — USER 1000, HEALTHCHECK по `$PORTAL_PORT`;
  `harvest.spec` + fastapi/uvicorn/pydantic (кнопка G в exe);
  `pip-audit` в CI (non-blocking); `.gitignore` — `.pip_ok`,
  `.dash-site.lock`; publish с flock, fetch свежего gh-pages,
  MotherDuck-выход 2; доки и README (57 файлов / 1458 тестов) синхронны

### Security
- Confirm/ws-токены больше не принимаются как HTTP-сессии
  (`auth.is_session_sub`); долгоживущая сессия в query отклоняется
- Редакция токена ловит и голый вид, чистит трейсбэки (`exc_text`),
  висит и на root-логгере; `botforge` на `DefaultBotProperties`
- Свободный ввод (группы, задачи планировщика) через `shield.sanitize`
  → 400; `tg/init` uniform-401; OAuth-куки стираются с `secure`

## [2.3.0] - 2026-10-16

### Added
- Telegram-экспорт с подтверждением (export:csv/json → export:yes:*), protect_content + автоудаление 60с
- Кнопки [ДОМОЙ] (menu:home) на всех экранах бота: stats/status/doctor/history/wallets/networks/links/ref
- Антифлуд тяжёлых операций бота (30с) и живой мониторинг с алертом остановки
- Защитный контур портала (shield): risk/limits/idem/sanitize/breaker/guard
- GitHub Pages site (docs/): live field with wallet grid, beginner/pro demo zone,
  MIT section, RU/EN, Paper/Ink/Moss themes
- Telegram bot ops-style texts + /cmd, /cfg, /check, /ref, FSM wizards
- Opt-in load tests (HARVEST_LOAD=1) and site verification tooling
- Design tokens (design/tokens.json) with WCAG AA contrast table

### Fixed
- Restored network configs, harvest.spec, build.bat, root publish_dash.py
- Fail-closed seed writes without master key
- RISK_MEDIUM env-дефолт 20.0 → 25.0 (синхрон с кодом); choke-лимиты отделены от login-лимитов

### Security
- Backup files get owner-only permissions before replace
- No wallet addresses published to gh-pages (ordinals only)

## [1.0.0] - 2026-10-09

### Added
- Initial release
- Analytics dashboard
- Metrics visualization
- Repository statistics
