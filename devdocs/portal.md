# Портал `portal/` — harvest

> ← Назад: [DOCUMENTATION.md](../DOCUMENTATION.md)

## 4. Портал `portal/`

### 4.1. Запуск и конфигурация (`__main__.py`, `config.py`)

`python -m portal` поднимает aiohttp (веб + API) и, при токене, Telegram-бота.

Env-переменные (`PortalConfig`; секреты — только через env,
`portal_config.yaml` переопределяет несекретное: host/port/public_base_url/
db/farm_config/links):

| Переменная | Назначение |
|---|---|
| `PORTAL_SECRET` / `portal_secret.key` | секрет подписи сессий (без него портал не стартует; минимум 16 символов; автогенерация в файл 0600) |
| `PORTAL_HOST` / `PORTAL_PORT` | bind (default `127.0.0.1:8080`) |
| `PORTAL_DB` | путь к БД фермера (default `farming_state.db`). Читают **все** три: портал, публикатор (`publish_dash.py`) и сама ферма (`auto.py`) — иначе они смотрели бы в разные базы |
| `PORTAL_FARM_CONFIG` | конфиг фармера (`config.yaml`) |
| `PORTAL_LINKS` | `links.json` владельца |
| `PORTAL_BASE_URL` | публичный URL (для OAuth-редиректа/Mini App) |
| `GOOGLE_CLIENT_ID/SECRET` | Google OAuth 2.0 |
| `GOOGLE_ALLOW_EMAILS` | default-deny список email |
| `TELEGRAM_BOT_TOKEN` | токен бота (BotFather) |
| `TELEGRAM_ALLOW_IDS` | default-deny список user_id |
| `TELEGRAM_OWNER_ID` | user_id владельца, панель `/admin` (default `397602448`; в allowlist тоже должен быть) |
| `FARMER_MASTER_KEY` | мастер-ключ БД / PIN веб-входа |
| `WALLETS_KEY` | 64-hex ключ шифрованного блоба `wallets.enc.json` (publish_dash → gh-pages → BotForge-бот); генерация: `python -c "import secrets;print(secrets.token_hex(32))"`; без него с обеих сторон — штатный пропуск кошельков |
| `PORTAL_OPEN_ACCESS=1` | сознательно открыть доступ любому Google/TG-аккаунту |
| `PORTAL_PASSWORD_LOGIN=1` | вход по мастер-ключу (default 1) |
| `PORTAL_ALLOW_PASSWORD_HTTP=1` | пароль по HTTP (только dev) |
| `PORTAL_COOKIE_SECURE` | Secure-кука (авто при https base_url) |
| `PORTAL_TRUST_PROXY=1` | доверять X-Forwarded-For/Proto |

Fail-closed при старте: bind не на loopback без единого способа входа
(Google/пароль/Telegram) → отказ запуска. `validate_master_key` —
строгие 64 hex (ключ БД) или PIN ≥ 8 символов (с предупреждением).

Рабочие файлы — отдельно от кода: БД (`farming_*.db`), `master.key`,
`portal_secret.key`, логи и `state.json` держите в каталоге запуска
(например, `PORTAL_DB=/data/farming_state.db`), а не в клоне репозитория.
В репозитории им не место (всё это в `.gitignore`): случайный коммит или
потеря каталога = потеря кошельков. `master.key` и `WALLETS_KEY` бэкапьте
отдельно (офлайн-копия); без них ни БД, ни `wallets.enc.json` не расшифровать.

### 4.2. `auth.py` — аутентификация (чистые функции)

- `sign_token/read_token` — сессии HMAC-SHA256 (payload: sub/name/jti/exp,
  TTL 7 дней); jti позволяет revoke.
- `check_master_key` — 64-hex сравнивается case-insensitive (hex), PIN —
  строго case-sensitive; всегда `compare_digest`.
- `validate_telegram_init_data` — проверка подписи Mini App
  (HMAC `WebAppData` + bot_token), окно `auth_date` 10 минут.
- `google_exchange` — обмен кода на id_token (PKCE code_verifier, nonce);
  `_parse_id_token` — aud/iss/exp/nbf/iat/email_verified.
- `csrf_state/verify_csrf_state`, `pkce_pair` (S256).

### 4.3. `api.py` — aiohttp-приложение

Middleware (порядок: `security_headers` → `error_handler` → `origin_guard`):
- `security_headers`: CSP (`default-src 'self'`, `frame-ancestors` —
  Telegram для Mini App, `'none'` для логина), nosniff, Referrer-Policy,
  HSTS только на реальном HTTPS;
- `error_handler`: 500 без внутренностей;
- `origin_guard`: CSRF fail-closed — мутирующие запросы только с ожидаемого
  Origin (`_effective_base`: `PORTAL_BASE_URL` или адрес запроса).

Доступ — default-deny: `/api/stats`, `/api/farm/{action}`, `/api/links`,
`/api/cycle-history`, `/api/top-wallets` требуют сессии (`_require_user`,
отказ пишется в audit). Сессия — httponly-кука `harvest_session`
(`__Host-` префикс при Secure); logout — revoke jti с персистенцией
`revoked_sessions.json` (атомарно через mkstemp, RLock).

Rate-limit:
- логин: per-IP 5/10 мин + глобальный 20/10 мин (против ботнета со сменой IP);
- неаутентифицированные CPU/сеть-хендлеры (`/api/tg/init`,
  `/auth/google/callback`): per-IP 30/мин + глобальный 20/10 мин
  (`_choke_*`).

Эндпоинты: `/`, `/login`, `/healthz`, `/api/me`, `/api/login/password`
(только HTTPS), `/api/logout`, `/auth/google`, `/auth/google/callback`,
`/api/links`, `/api/stats`, `/api/farm/{start|stop|pause|resume}`
(неизвестное действие — 400, stop не блокирует HTTP — задача в фоне),
`/api/cycle-history`, `/api/top-wallets`, `POST /api/wallets/export`
(только POST: GET-роут удалён — top-level навигация несла бы SameSite=Lax
куку; скачивание всех кошельков с ключами; auth + HTTPS + no-store),
`POST /api/ws/token` (короткий WS-тикет 60с в query; сессия — только cookie),
`/ws` (в query — только ws-тикеты, сессионные/confirm отклоняются; кап
проверяется до `prepare` — 503 без траты FD; revoke перепроверяется в цикле,
logout кикает живой сокет), `/api/tg/init` (только POST, лимит 16 КБ,
uniform-401: неверная подпись и чужой allowlist неразличимы),
`/api/faucet` (разбор pre-flight для виджета), `POST /api/faucet/drip`
(ручной долив фоном, single-flight 202/409),
`/app/` (Mini App), `/api/miniapp/*` (см. 4.6), `/static`.
Свободный ввод (имена групп, задачи планировщика) идёт через
`shield.sanitize` (`_guard_check` → 400 при отказе): хранимые строки больше
не держатся только на `esc()` потребителей. Неизвестные
`kind`/`action` и мусор в `limit` — 400, а не молчаливый фолбэк;
`create_wallets` без БД — 503.

### 4.6. `miniapp.py` + `/app/` — Mini App (этап 3)

- Авторизация на каждом запросе: `Authorization: tma <initData>`, подпись
  тем же `auth.validate_telegram_init_data` (HMAC WebAppData, compare_digest,
  окно 10 мин, будущее и >16 КБ — отказ). Отказ везде одинаковый:
  401 `{"error": "unauthorized"}` — подпись/давность/allowlist неразличимы.
  initData и токен в логи не пишутся.
- Эндпоинты: `GET /api/miniapp/state` (ETag/304, без адресов — только клетки),
  `GET /api/miniapp/history` (limit ≤ 50), `GET /api/miniapp/networks`
  (сети + пресеты мастера), `POST /api/miniapp/farm` (идемпотентный,
  rate 30/мин на user_id), `POST /api/miniapp/command` (тот же
  `bot_ops.build_command/build_config_yaml/check_config_text`, включая
  `cfg_send` — YAML файлом в чат), `POST /api/miniapp/export/token` →
  `POST /api/miniapp/export/request` (одноразовый confirm-токен, кулдаун
  1/мин общий с ботом, аудит без ключей, файл — тем же
  `bot_telegram.send_wallets_file` в чат с protect + удаление 60с;
  форматы `enc-csv`/`enc-json` — тот же `_send_encrypted`, что у EncWiz
  бота: пароль только в памяти, в логи не пишется, min 4 символа),
  `GET/POST /api/miniapp/settings` (общие с ботом `bot_settings`),
  `GET /api/miniapp/alerts` + `POST /api/miniapp/alerts` (мониторинг балансов:
  threshold 0..100, `enabled: false`/`"off"` — выкл), `GET /api/miniapp/rpc`
  (здоровье нод), `GET /api/miniapp/faucet` (pre-flight, rate 15с),
  `POST /api/miniapp/network/switch` (`{slug}` → `switch_to`, 404 только
  на неизвестную сеть). WS: `POST /api/ws/token` (тикет 60с в query;
  долгоживущая сессия — только в cookie), сокет `/ws` подтверждает revoke
  в цикле (logout кикает) и проверяет кап до `prepare` (503, без FD).
  Портал: `GET /api/faucet` — тот же разбор для виджета «Кран» в RPC Health.
- Клиент (`portal/static/miniapp/`): без фреймворков, те же vars из
  `/static/app.css`, сетка iwoer теми же глифами, опрос state 5с + ETag,
  пауза при hidden, MainButton/BackButton/haptic/safe-area с проверкой
  версий и молчаливой деградацией, скелетон/empty/retry/session-expired,
  вне Telegram — «Открой из бота», ноль запросов.
- Публикация (п.8 ТЗ): портал на 127.0.0.1; `PORTAL_MINIAPP_PUBLIC=1` —
  снаружи только `/app/*`, `/api/miniapp/*`, `/static/*`, `/healthz`
  (остальное 404, тест); Caddy — только эти пути (`deploy/caddy/`);
  кнопка Mini App — `PORTAL_MINIAPP_URL` (дефолт base + `/app/`).

### 4.4. `farm.py` — FarmDaemon

Единый инстанс `FarmerPool` под управлением сервера.
- `connect()` — идемпотентно: конфиг, ключ БД (`_db_master_key`: 64-hex
  override или `resolve_master_key(ignore_env=True)` — PIN не может быть
  ключом БД), `Database.init`, `FarmerPool`.
- `start()` — SingleInstance-лок (`default_lock_path(db_path)`; если
  main.py/auto.py уже фармит — отказ, дашборд продолжает жить), фоновая
  задача `pool.run_forever(self._stop)`.
- `stop()` — выставляет стоп и ждёт задачу (штатно; после стопа пул
  пересоздаётся при следующем `start` — `pool.close()` необратим).
- `pause()/resume()`, `statistics()` (live_stats + `concurrency_factor` +
  статистика циклов + `wallet_count`), `top_wallets()`, `cycle_history()`,
  `wallet_count()`, `export_wallets(fmt)` (CSV/JSON со всеми расшифрованными
  ключами — только авторизованным владельцам), `close()`.

### 4.5. `bot_telegram.py` — Telegram-бот (aiogram, long-polling)

- `_allowed` — default-deny по `TELEGRAM_ALLOW_IDS` (+`PORTAL_OPEN_ACCESS`).
- Кнопки: старт/стоп/пауза/резюм фермы, статистика (`_fmt_stats`, включает
  счётчик кошельков), ссылки (`load_links`, санитизация, максимум 15 строк),
  экспорт кошельков (`export:csv`/`export:json` → подтверждение → `export:yes:*` → файл
  с `protect_content` + автоудаление 60с), Mini App
  (`WebAppInfo` → фронтенд шлёт initData в `/api/tg/init`), конструкторы
  (`cmd:usage`, `cfg:usage`) и справочник (`ref`). Все экраны — с `[ДОМОЙ]`
  (`menu:home`), антифлуд тяжёлых операций 30с.
- v1-навигация (этап 2 ТЗ): домашнее «ПОЛЕ» (`harvest // ПОЛЕ`, сетка iwoer
  10 в ряд макс. 100 + `+N`, рендер `portal/bot_render.py`), правка на месте,
  колбэки `v1:экран:действие[:арг]` ≤ 64 байт, stale-версия → «Экран устарел»
  + перерисовка Поля, Запуск/Стоп не рядом (Стоп только в RUN с подтверждением),
  Live 10 мин (правка 5с, автостоп), мастер 3 шага (сеть → масштаб → профиль
  тихий/обычный/быстрый → кран; TTL 15 мин, сброс по /start), режимы
  Новичок/Про + язык RU/EN (`portal/bot_settings.py`, таблица bot_settings),
  Опасная зона (экспорт CSV/JSON/шифрованный .enc первым вариантом, ключи
  текстом — с подтверждением, protect, удаление 60с, кулдаун 1/мин, аудит),
  адреса без ключей — сокращённые. Токен чистится из логов
  (`bot<id>:<secret>` → `bot<TOKEN>`), 409 — backoff + одно уведомление,
  `bot: CONFLICT|ok` в /doctor, setMyCommands RU.
- Команды: `/start` — главное меню, `/menu` — все разделы, `/status` — живое поле,
  `/monitor` — мониторинг с алертом остановки, `/doctor` (+ `/diag` подробно),
  `/history`, `/balance`, `/pnl`, `/network`, `/profiles`, `/scheduler`,
  `/wallets`, `/export [json]` — скачивание всех
  кошельков с ключами (владельцу); `/cmd сеть [кошельки] [циклы]` — готовая
  команда auto.py; `/cfg сеть [кошельки]` — YAML shipped-конфига файлом
  (count подставлен, прогнан настоящим валидатором); `/check` + YAML
  следующими строками — проверка `validate_config` (вход не цитируется,
  секреты вычищаются); `/ref` — сжатый справочник параметров;
  `/pause` / `/resume` / `/stop` — управление фармом; `/rpc`,
  `/faucet` — диагностика; `/admin` — панель владельца (`TELEGRAM_OWNER_ID`:
  версия, allowlist, флаги входов, статус 409, тест алерта; остальным — тишина);
  `/help` — эта справка.
- Мастера (aiogram FSM, `CmdWizard`/`CfgWizard` в `bot_telegram.py`):
  кнопки `[CMD]`/`[CFG]` ведут по шагам (сеть → кошельки → циклы → кран),
  свободный ввод чисел — прямо в чат, `/cancel` и кнопка `[ОТМЕНА]` закрывают
  в любой момент, любая другая команда выходит из мастера и выполняется
  обычным путём. YAML можно прислать и файлом (лимит 16 КБ, только
  `.yaml`/`.yml`/`.txt`) — проверяется тем же валидатором.
- Стиль сообщений — ops-board сайта (`portal/bot_ops.py`): секции
  `harvest // X`, home-статус с эмодзи-маркерами, остальные экраны текстом, `[ok]`/`[warn]`.
- Все значения из daemon проходят `html.escape` (defense-in-depth);
  сбои callback пишутся в журнал и отвечают generic-текстом.

---
