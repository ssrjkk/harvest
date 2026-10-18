# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added
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
