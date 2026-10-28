# Безопасность, тесты, эксплуатация — harvest

> ← Назад: [DOCUMENTATION.md](../DOCUMENTATION.md)

## 6. Безопасность

- **Секреты в БД**: приватные ключи/мнемоники шифруются AES-256-GCM
  (мастер-ключ из `FARMER_MASTER_KEY` или `master.key`; файл создаётся с
  ограниченными правами). Без ключа доступ закрывается fail-closed.
- **Redaction**: `_redact_rpc_url` стирает credentials и query-string с
  API-токенами из RPC-URL в логах, `cycle_history`, CLI, `diagnostics()`
  и экране `screen_rpc_diag` (list-безопасный overload).
- **Файлы**: tmp → chmod → atomic replace (БД, бэкапы, экспорты, кэш
  лицензии, `revoked_sessions.json`); `restrict_file_permissions` на Windows
  и POSIX.
- **CSV-инъекции**: `_csv_cell_safe` нейтрализует формулы.
- **Экспорт**: AES-256-CBC + HMAC key-separation (v2), чтение легаси v1.
- **Лицензия**: PBKDF2 210k итераций, только https, лимит тела, revoked
  действует офлайн через кэш.
- **Портал**: default-deny, сессии HMAC + revoke, CSRF origin-guard,
  CSP/nosniff/HSTS, пароль только по HTTPS, rate-limits (per-IP + глобальные),
  nonce/PKCE в OAuth, audit-лог.
- **Один пул на БД**: `SingleInstance`-лок (`*.lock` рядом с БД).
- **Конфиг**: обязательный TLS для RPC/крана (кроме loopback), запрет `..`
  в путях, валидация типов без падений.
- В exe бандлятся только шаблоны (abi + `config.example.yaml`); боевые
  конфиги и секреты — рядом с exe / через env.
- **Защитный контур портала** (`portal/shield/`, включается по умолчанию,
  выключается `PORTAL_GUARD_ENABLED=0`): decay-скоринг риска
  (LOW/MEDIUM-tarpit/HIGH-shadowban), иерархический троттлинг
  (global + per-actor read/write + penalty box), идемпотентность мутаций
  (`@idempotent`), санитизация входящих, CircuitBreaker + ретраи на
  `google_exchange`, throttle WS-подключений (лимит клиентов),
  correlation_id, security-аудит с маскированием (`/api/guard`).
  Хранилище in-memory с TTL-сборкой; переход на Redis — замена
  `AsyncStore`/`MemoryStore`. Нагрузочные тесты: `tests/test_shield_*.py`.

---

## 7. Тесты

`tests/` — 62 файла (`pytest`, `pyproject.toml: testpaths=tests, -q`),
1554 теста, все зелёные (+ `test_load_opt.py` — нагрузка, opt-in
через `HARVEST_LOAD=1`, в обычном CI пропускается).
CI-гейты: 100% portal (`--cov=portal --cov-fail-under=100`),
100% core (`--cov=core --cov-fail-under=100`):

- ядро: `test_workpool`, `test_pool`, `test_farmer`, `test_actions`,
  `test_behavior`, `test_faucet`, `test_batchwriter`, `test_wallet`,
  `test_database`, `test_crypto`, `test_exporter`, `test_utils`,
  `test_performance`, `test_license`;
- RPC: `test_network_infra` — в т.ч. `TestRpcNode` (EMA-скоринг,
  zombie-воскрешение), `TestCircuitBreaker` (open/half-open/reset) и
  `TestRpcRotation` (best_rpc_prefers_low_score,
  best_rpc_skips_dead_node, all_dead_falls_back_to_least_bad,
  пробы мимо rate-limiter/метрик, воскрешение ноды бизнес-вызовом,
  monitor_switches_to_best_alive, switch_resets_circuit_breaker,
  close_idempotent, monitor_skipped_single_endpoint,
  node_health_redacts_url, diagnostics_redacts_urls);
- UI/инфраструктура: `test_ui`, `test_logger`, `test_single_instance`,
  `test_main_select`, `test_import_wallets`, `test_config_validate`
  (в т.ч. `TestPlaceholderWarnings` — `config_warnings()` про 0x0 и
  chain_id-99999);
- портал: `test_portal_main`, `test_portal_farm`, `test_portal_auth`,
  `test_portal_api`, `test_portal_guard`, `test_bot_telegram`, `test_bot_render`,
  `test_bot_home`, `test_miniapp`,
  `test_portal_wallets_export`, `test_wallets_create`, `test_groups`,
  `test_scheduler`, `test_publish_dash`, `test_bot_monitor`, `test_botforge_bot`,
  `test_netdiscovery`, `test_networks_switch`, `test_network_infra`;
- щит: `test_shield_audit`, `test_shield_breaker`, `test_shield_idem`,
  `test_shield_integration`, `test_shield_limits`, `test_shield_middleware`,
  `test_shield_risk`, `test_shield_sanitize`;
- покрытийные сквозные ветки: `test_cov_ui_actions_config`,
  `test_cov_portal_config_main_bot`, `test_cov_portal_api_auth_farm`,
  `test_cov_doctor_faucet_vibevibe`, `test_cov_100` (shield/мелкие модули),
  `test_cov_100_api` (portal/api), `test_cov_100_bot` (bot_telegram/render),
  `test_cov_100_farm` (farm/miniapp).

Запуск: `python -m pytest tests`. Линтеры: `ruff check` (E,F,W,I,B,UP,
игнор E501), `ruff format --check` (line-length 120), `flake8` (120,
игнор E203/E402/W503), `mypy` (py312, follow_imports=skip; известная
пред-существующая ошибка `tests/test_main_select.py:26` — `Module has no
attribute "frozen"`).

---

## 8. Сборка exe и дистрибуция

`build.bat`:
1. `pip install -r requirements.txt`
2. `pyinstaller harvest.spec --noconfirm --clean`
3. `Unblock-File dist\harvest\harvest.exe` (против блокировки Device Guard).

`harvest.spec`: entry `main.py`; `datas`: `abi/`, `config.example.yaml`,
wordlist'ы `eth_account`; `hiddenimports`: web3, aiohttp, aiosqlite,
cryptography, rich, fake_useragent, yaml и пр.; `excludes`: tkinter, numpy,
pandas, pytest и пр.; `upx=False` (ложные срабатывания AV/EDR);
`console=True`; результат `dist/harvest/harvest.exe` (переносить папку
целиком).

Дистрибутив:
- `dist\harvest\harvest.exe` — запуск без аргументов → меню с выбором
  тестнета (находит `config*.yaml` рядом с exe);
- боевой `config.yaml` кладётся рядом с exe; секреты — через env;
- `_internal\` — abi + шаблон конфига (уже в бандле).

Портал: `pip install -r requirements.txt -r requirements-portal.txt` →
`python -m portal` (env-конфигурация выше).

---

## 9. Эксплуатация

- **Быстрый старт**: `[1]` кошельки → `[5]` кран → `[3]` live-фарм.
- **Автомат**: `python auto.py --wallets 200 --cycles 5`; по расписанию:
  `--schedule 6`.
- **Передеплой портала (Windows)**: `deploy/windows/portal-run.ps1` —
  веб + бот из git-клона одним вызовом; секреты только из User-env
  (`TELEGRAM_BOT_TOKEN`, `TELEGRAM_ALLOW_IDS`), pid-guard против второго
  инстанса (антагонист `409 Conflict`), логи — `portal.log` с ротацией.
  Проверка живости: `GET /healthz` → `{"status": "ok", "ready": true}`.
- **Диагностика**: `--doctor` (конфиг/ключ/БД/RPC/кран/лицензия), меню `9`
  (зонды нод, circuit breaker), `--history 20`.
- **Smoke-проверка exe**: рабочий каталог — отдельная папка, в конфиге
  абсолютный `database.path`, файлы UTF-8 без BOM; боевые артефакты
  (`farming_state.db`, `master.key`, `logs/`, `license.json`, `config*.yaml`,
  `.backup/`) не трогать. Пример: `harvest.exe --config config.yaml
  --wallets 3 --cycles 1 --skip-faucet` → EXIT=0, в логе только ожидаемые
  WARNING (failover/circuit breaker при мёртвых RPC), рабочих файлов
  не создаёт.
- **Лицензия**: владелец — `python -m core.license setpass ПАРОЛЬ`,
  обновление `pass` в license.json на GitHub; `status: revoked` гасит копии.

---

## 9.5. Tier 2 — подключение реальных сетей (вписать свои данные)

Референсы `config_robinhood.yaml`, `config_flop.yaml`, `config_arc.yaml`,
`config_vibevibe.yaml` — готовые каркасы с **заглушками**: validator их
пропускает (transfers работают), но `--doctor` печатает жёлтые «Замечания»
и runtime предупреждает о 0x0. Перед реальным фармом впишите данные сети:

1. **Скопировать каркас в боевой файл**: `cp config_robinhood.yaml config.yaml`
   (или запускать `--config config_robinhood.yaml`).
2. **RPC** `network.rpc_url` (строка или `;`-разделённый список failover'ов):
   https-URL, без токенов в query; секреты выносить в env
   (`FARMER_RPC_URL`). В логах/`diagnostics()` адрес всегда redacted.
3. **`network.chain_id`** — реальный id сети (не 99999): doctor сверяет его
   с RPC-нодой (`eth_chainId`), несовпадение → RPC-fail.
4. **Контракты** — заменить `0x0...0` на реальные адреса (lowercase hex).
   Контрактные действия требуют `advanced.gas_limit >= 50000`; после вписки
   гана подберите по факту (insufficient gas → короткий цикл + log).
5. **Кран** — `faucet.strategies[].url` (https; http разрешён только для
   `127.0.0.1`/localhost-кранов).
6. **`portal/links.json`** — реальные ссылки (explorer/faucet/страницы),
   они отдаются в `/api/links` и в боте.
7. **Проверка перед первым боем**:
   `python auto.py --doctor --config <файл>` → все пункты зелёные, раздел
   «Замечания» пуст; затем smoke: `python auto.py --config <файл> --wallets 3
   --cycles 5 --skip-faucet` → EXIT=0, в логе без 0x0-предупреждений.
8. **Деплой**: секреты через `deploy/.env.example` (см. `deploy/README.md`),
   `master.key` сгенерировать, `license.json` — пароль владельца.

`portal/links.json` наполняется так: `[ { "label": "<что>", "url": "<https://…>" } ]` —
и проверяется `GET /api/links` портала.

---

## 9.6. Релиз (чеклист поднятия версии)

Версия живёт в одном месте — `core/version.py`. Остальное подтягивается
автоматически, кроме картинок. Порядок:

1. **Номер**: `core/version.py` → `VERSION = "X.Y.Z"`. Футеры бота,
   статика портала (`{{HARVEST_VERSION}}`), сайт (константы в `common.js` /
   `field.js`) подхватят сами — проверяет `verify_site` (`single site version`,
   `no stale version copies`, `renders version`).
2. **Кэш SW**: `docs/sw.js` → `CACHE = "harvest-vX.Y.Z"` (+1 к прошлой).
3. **OG-картинка**: `python tools/make_og.py` (Pillow; на Windows — Impact/Consolas,
   на Linux — дефолтный шрифт, вид будет отличаться — генерировать на Windows).
4. **Скриншоты README**: `python tools/shoot_site.py --url http://127.0.0.1:8765`
   (сервер: `python -m http.server 8765 --directory docs`), матрица:
   `python tools/shoot_site.py --url ... --matrix` → `fails=0`.
5. **Архив бота**: `python tools/build_botforge_zip.py` (детерминированный;
   `verify_site` сравнивает байты, mtime не важен).
6. **Эндпоинты**: `python tools/check_endpoints.py` с машины с интернетом —
   RPC `eth_chainId` обязан совпасть с `chain_id` конфигов.
7. **Проверки**: `python tools/verify_site.py` → 0 FAIL;
   `python -m pytest tests -q` → зелёный (гейт 100% покрытия core/portal).
8. **CHANGELOG**: перенести раздел `[Unreleased]` в новый `## [X.Y.Z] - <дата>`,
   завести пустой `[Unreleased]` сверху.

---

## 10. Глоссарий ключевых соглашений кода

- **Один `NetworkManager` на сессию** — общий кэш/метрики/монитор; не
  создавать дубликаты.
- **`health_fn` контракт** — float `0.15..1.0`; источник: `network.
  concurrency_factor()`.
- **Сворачивание воркер-пула** — только retire (без `task.cancel()`);
  `pool.close()` необратим.
- **Crash recovery** — `state.json` с номером цикла; несовпадение номера →
  сброс.
- **Zombie-воскрешение ноды** — один успешный пинг снимает `is_dead`.
- **Redaction** — RPC-URL наружу только через `_redact_rpc_url`.
- **Атомарные записи секретов** — tmp → restrict → `os.replace`.
- **Fail-closed** — портал, мастер-ключ, TLS, конфиг-валидация.
