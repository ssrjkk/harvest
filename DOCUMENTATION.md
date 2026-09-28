# HARVEST — полная документация проекта и кода

Версия: **v2.3.0** · Python 3.12 · Windows/PowerShell (первичная цель — exe-дистрибутив)

HARVEST — автоматизированный фермер тестнет-сетей (EVM): массовая генерация кошельков,
получение токенов через краны, асинхронный фарминг транзакций по заданным шаблонам
действий, веб-портал с Telegram-ботом для дистанционного управления.

---

## 1. Обзор

### 1.1. Что умеет

- **Кошельки**: массовая генерация (BIP-39, ~60 шт/сек), импорт/экспорт (CSV/JSON),
  шифрование сид-фраз AES-256-GCM мастер-ключом, дедупликация.
- **Кран**: пополнение балансов через конфигурируемые стратегии (chainstack/direct),
  retries, rate-limit, pre-flight валидация.
- **Фарминг**: асинхронный воркер-пул поверх одного `NetworkManager`, чанковая
  обработка, crash recovery (`state.json`), динамическое масштабирование воркеров
  по здоровью RPC, adaptive-веса действий.
- **Анти-сибил**: детерминированные профили поведения на кошелёк (активность, паузы,
  суммы, газ, время суток, «отдых», staggered-старт).
- **Сети**: Robinhood Chain Testnet, Flop Labs Testnet, Arc Testnet (Minara.Fun) —
  выбор сети в стартовом меню или `--config`.
- **RPC-слой**: мульти-RPC (строка или список), EMA-скоринг нод, автоматическая
  ротация на лучшую живую ноду, circuit breaker, адаптивный rate-limiter,
  TTL-кэши (gas/balance/nonce), фоновая метрика.
- **Лицензия**: дистанционный доступ по паролю (license-hub на GitHub raw),
  PBKDF2-хэши, офлайн-кэш с grace-окном.
- **Портал**: aiohttp-веб + API + Telegram-бот + Mini App; сессии HMAC,
  Google OAuth, вход по мастер-ключу, default-deny доступ.
- **Безопасность**: шифрование секретов, redaction RPC-URL в логах/истории,
  single-instance лок, ограничение прав файлов, защита от CSV-инъекций,
  fail-closed везде, где это уместно.

### 1.2. Слои архитектуры

```
main.py (интерактивное меню, Rich)          auto.py (CLI-автомат)
         \                                     /
          +------ core/config.py (загрузка/валидация конфига)
                     core/license.py (гейт доступа)
                     core/performance.py (единые значения параллелизма)
                     |
      core/pool.py FarmerPool ── core/workpool.py WorkerPool (очередь задач)
            |                \
            |                 core/farmer.py Farmer (цикл одного кошелька)
            |                     core/actions.py ActionExecutor ── core/vibevibe.py
            |                     core/behavior.py WalletProfile (анти-сибил)
            |
      core/network.py NetworkManager (RPC, ноды, кэши, ретраи)
            |            core/faucet.py Faucet (кран)
            |
      core/database.py Database (SQLite/aiosqlite) ── core/batchwriter.py BatchWriter
      core/crypto.py (AES-256-GCM сид-фраз)           core/exporter.py (CSV/JSON/AES)
      core/utils.py / core/logger.py / core/ui.py / core/single_instance.py
      core/doctor.py (самодиагностика)                core/version.py

portal/__main__.py ── portal/api.py (aiohttp) ── portal/farm.py FarmDaemon
                       portal/auth.py (сессии/OAuth/TG)   portal/bot_telegram.py
                       portal/config.py (env-конфигурация)
```

---

## 2. Точки входа

### 2.1. `main.py` — интерактивное меню

Запуск **без аргументов** — интерактивный режим:

1. Баннер + `_find_configs()`: ищет `config*.yaml` в CWD и рядом с exe
   (`sys.frozen`). Ни одного — панель с инструкцией. Один — сразу в сессию.
   Несколько — экран `screen_testnet_select()` (выбор сети по номеру, `0`/`q` — выход).
2. `run_session(u, config_path)`:
   - `load_config()` (проверка секций, env-override, валидация);
   - `setup_logging(config)`;
   - **лицензия**: `enforce_license_async(config, interactive=True)`
     (env `LICENSE_PASSWORD` → `getpass`, 3 попытки; `LicenseError` → `SystemExit(2)`);
   - **БД**: `Database(path, master_key=resolve_master_key(config))`,
     `MasterKeyError` → `SystemExit(1)`; `db.init()`;
   - если кошельков 0 — панель «БЫСТРЫЙ СТАРТ»;
   - `signal.SIGINT/SIGTERM` → `stop_event`.
3. Меню:
   - `1` создание кошельков (`screen_make_wallets`, спиннер);
   - `2` один цикл фарма (`screen_farm_once`: single-instance guard,
     `FarmerPool.run_once`);
   - `3` live-консоль фарма (`screen_farm_live`: `pool.run_forever`, Rich live-таблица,
     клавиши `p` пауза / `q` стоп, отдельный producer обновляет топ-кошельков и
     `net.latency_probe()` каждые 10 с);
   - `4` монитор балансов (`screen_monitor`, семафор `effective_max_workers`,
     обновление каждые 5 с, `p`/`q`);
   - `5` кран на всех (`request_faucet_all`: `Faucet.validate()` → `request_batch`);
   - `6` статистика (`screen_stats`: счётчики, топ-10, журнал циклов);
   - `7` экспорт в JSON (`wallets.json`; если файл существует — с timestamp);
   - `8` импорт (`import_wallets`: пасс 1 — формат/дубликаты без криптографии,
     пасс 2 — вывод адреса из ключа в `ThreadPoolExecutor(gen_workers)`);
   - `C` история циклов (`screen_history`, `_redact_rpc_url` для RPC);
   - `9` диагностика RPC (`screen_rpc_diag`: `probe_all()` + `diagnostics()`);
   - `D` полная диагностика (`screen_doctor`);
   - `B` бэкапы БД (`screen_backups`);
   - `L` лицензия (`screen_license`);
   - `S` смена тестнета (возврат к выбору сети);
   - `H` справка; `0` выход к выбору тестнета.

Запуск **с аргументами** (`len(sys.argv) > 1`) → делегирует в `auto.main_auto()`
(main.py:942). Это же поведение у собранного exe: `harvest.exe` — меню,
`harvest.exe --wallets 3 ...` — авто-режим.

### 2.2. `auto.py` — авто-режим (CLI)

Один проход пайплайна = 4 фазы (`run_once()`):

```
python auto.py                                   # 50 кошельков, циклы бесконечно
python auto.py --wallets 200 --cycles 5
python auto.py --skip-faucet --skip-farm
python auto.py --doctor / --history 20 / --version
python auto.py --schedule 6                      # пайплайн каждые 6 часов
```

Флаги:
- `--wallets N` (0 = из конфига), `--cycles N` (0 = бесконечно),
  `--config FILE` (default `$FARMER_CONFIG` → `config.yaml`), `--skip-faucet` (ставит `advanced.force_farm=true` +
  `faucet.enabled=false`), `--skip-farm`, `--export NAME` (CSV+JSON),
  `--batch N` (пачка крана, default 50), `--workers N` / `--rpc-threads N`
  (переопределение конфига), `--fast` (минимальные задержки), `--dry-run`
  (`advanced.dry_run`), `--encrypt` (AES-256 экспортов), `--decrypt FILE`,
  `--version`, `--history [N]`, `--doctor`, `--schedule HOURS` (минимум 0.05 ч).

Фазы в `run_once`:
1. **Генерация/переиспользование**: `generate_if_missing=true` — добирает
   недостающих через `WalletManager.create_wallets()`; `false` — только
   существующие из БД.
2. **Кран**: `Faucet.validate()` (живые стратегии) → `request_batch(addresses,
   args.batch, pause=3.0)`. Все стратегии мертвы → `force_farm=true`.
3. **Фарм**: `FarmerPool.run_forever(stop_event)` при `cycles<=0`, иначе
   `run_once()` в цикле `1..cycles` с паузой 2–10 с; между циклами печать
   диагностики (`pool.network.diagnostics()`).
4. **Экспорт**: `export_csv` + `export_json` (`wallets_export.csv/.json`),
   опционально `--encrypt` (пароль дважды через `getpass`).

`run_schedule()` — обёртка: циклы по `--schedule HOURS` с обратным отсчётом
(TTY). `show_cycle_history()` — таблица последних циклов для `--history`.

Оба режима защищены `SingleInstance(default_lock_path(db_path))` — одна ферма
на одну БД.

---

## 3. Ядро `core/`

### 3.1. `config.py` — загрузка конфига

`load_config(path="config.yaml") -> dict | None` — единая точка для всех entry
points. Порядок: существование файла → `yaml.safe_load` → проверка, что это dict →
`apply_env_overrides()` (FARMER_*) → обязательные секции
`_REQUIRED_SECTIONS = [network, wallets, faucet, actions, farming, threading,
database]` → `validate_config()` (ловит `ConfigError` и любые
`AttributeError/TypeError/KeyError` — конфиг никогда не «сыплет», а печатает
причину). Ошибка — `print` + `None` (вызывающий делает `sys.exit(1)`).

### 3.2. `config_validate.py` — валидация

`ConfigError` — исключение с накопленным списком ошибок
(`"Ошибки конфигурации:\n  - ..."`).

Константы: `DEFAULT_MAX_WORKERS = 20`,
`VALID_ACTION_TYPES = {transfer, vibevibe_swap, vibevibe_mint, flop_compute,
flop_validate, flop_stake, arc_launch, arc_trade, arc_add_liquidity}`,
`_CONTRACT_ACTION_TYPES` — всё, кроме `transfer`.

Проверки:
- `network.rpc_url` — строка или непустой список строк; TLS обязателен
  (`_check_https_or_localhost`: `http://` допускается только на
  127.0.0.1/localhost/::1); `chain_id >= 1`.
- `wallets.count >= 0`.
- `faucet`: `min_balance <= target_balance`, `retries` 1..20,
  `max_concurrent` 1..64, каждая стратегия — dict с `url` (TLS).
- `actions`: каждый — dict с известным `type`; `weight >= 0`;
  `min_amount/max_amount >= 0` и `min <= max`; для `transfer` обязателен
  `target` (адрес или `"random_wallet"`); контрактные действия обязаны иметь
  `contract` (0x0-заглушка допустима) и `method`.
- `farming`: пары `[min, max]` для `actions_per_cycle`, `delay_between_actions`,
  `delay_between_cycles` (min <= max); `skip_cycle_probability` 0..1.
- `advanced`: `gas_limit >= 1`; если есть **реальный** контрактный action
  (не 0x0-заглушка) — `gas_limit >= 50000`; `min_gas_for_action >= 0`.
- `threading`: `max_workers` 1..1000, `timeout_per_wallet >= 0`,
  `gen_workers` 1..32, `rpc_threads` 1..256, `chunk_size >= 1`.
- `cache`: `rpc_rate_limit` 1..10000, `rpc_rate_floor` 0.05..1.
- `database.path` обязателен; пути без `..` (`_check_path`).
- `logging.level` — валидное имя уровня; `logging.file`/`license.cache_file` —
  без `..`.

`config_warnings(config) -> list[str]` — замечания о **заглушках** (не ошибки:
валидатор пропускает их намеренно, transfers работают и без контрактов):
- contract-действие с `0x0`-адресом → «фоллбэк на transfer, впишите реальный
  адрес»;
- `network.chain_id == 99999` (маркер-заглушка в `config_flop.yaml`).

Вывод: `--doctor` печатает жёлтые «Замечания»; в runtime `actions.py` дублирует
для 0x0-контрактов. Про то, как вписать реальные данные сети (RPC, адреса
контрактов, кран) — см. раздел «Tier 2» в конце документа.

### 3.3. `performance.py` — единый источник параллелизма

Все модули (network, wallet, pool, doctor, auto, main) читают значения отсюда.

- `RPC_THREAD_CAP = 128`, `GEN_WORKERS_CAP = 8`, `CHUNK_FACTOR = 4`,
  `RPC_IO_FACTOR = 3`.
- `auto_max_workers() = min(max(20, cpu*2), 128)`.
- `auto_rpc_threads() = min(max(8, cpu*4), 128)`.
- `auto_gen_workers() = min(8, max(2, cpu))`.
- `effective_max_workers(config)` — явное значение из `threading.*` или авто.
- `effective_rpc_threads(config)` — явное → иначе `max(4, min(max_workers*3, 128))`
  → иначе авто.
- `effective_gen_workers(config)`, `effective_chunk_size(config)` —
  `max(workers*4, 100)`.
- `summarize(config)` — сводка с флагами `*_auto` для шапки запуска/доктора.

### 3.4. `utils.py`

- `apply_env_overrides(config)` — `_ENV_MAP`: `FARMER_RPC_URL` (строка или список
  через `;`/запятую), `FARMER_CHAIN_ID`, `FARMER_WALLET_COUNT`, `FARMER_WORKERS`,
  `FARMER_GEN_WORKERS`, `FARMER_RPC_THREADS`, `FARMER_CHUNK_SIZE`,
  `FARMER_FAUCET_MAX_CONCURRENT`, `FARMER_RPC_RATE`, `FARMER_RPC_RATE_FLOOR`,
  `FARMER_MIN_BALANCE`, `FARMER_TARGET_BALANCE`, `FARMER_LOG_LEVEL`,
  `FARMER_DB_PATH`. `_coerce_env_value` парсит только ASCII-безопасные типы
  (защита от неожиданных типов из env).
- `restrict_file_permissions(path)` — ACL/chmod-ограничение файлов с секретами.
- `asleep(min, max)` — случайная async-задержка.
- `get_random_user_agent()`, `truncate_address(addr, chars=6)`,
  `resolve_bundled(rel)` — путь к данным внутри exe (PyInstaller `_MEIPASS`).

### 3.5. `logger.py`

- `RestrictedRotatingFileHandler` — RotatingFileHandler + ограничение прав при
  ротации.
- `ColoredFormatter` (цвета по уровню), `JSONFormatter` (машиночитаемые логи).
- `setup_logging(config)` — файл + консоль, уровни, backup_count, формат
  (цветной/JSON/plain), `restrict_file_permissions` для лог-файла.

### 3.6. `ui.py` — консольный UI (Rich)

Класс `UI` с автофоллбэком: `use_rich` = Rich доступен **и** TTY **и** не задан
`NO_COLOR`. `_safe()` подменяет символы через `_ASCII_MAP` для кодовых страниц
без юникода (cp1251). Основные методы: `banner`, `panel`, `menu_panel`, `table`,
`live_table` (Rich live с `_render`/`_cap`, `period`), `spinner`, `toast`,
`typewrite`, `hint`, `print`, `out_ok`, `menu_key`, `dot(latency_ms)`
(индикатор RPC), `bar`, `gradient`, `frame` (счётчик кадров), `divider`,
`poll_keys()` (неблокирующий ввод, Windows).

### 3.7. `single_instance.py`

`SingleInstance(lock_path)` — файловый лок (`msvcrt.locking` на Windows,
`fcntl` на POSIX, no-op при отсутствии). `default_lock_path(db_path) =
db_path + ".lock"`. Методы: `acquire() -> bool` (держит PID внутри файла),
`release()`, `holder_pid()`, контекстный менеджер. `ProcessLockedError` —
исключение при занятом локе (в коде предпочитается проверка `acquire()`,
чтобы не ронять приложение).

### 3.8. `crypto.py` — шифрование сид-фраз

- `resolve_master_key(config, ignore_env=False) -> bytes` — ключ БД:
  `FARMER_MASTER_KEY` env (64 hex) или файл `database.master_key`
  (`master.key`, с ограничением прав). Нет ключа и есть секреты →
  `MasterKeyError` (fail-closed).
- `encrypt_seed(key, plaintext) -> "enc:<nonce>:<ciphertext>"` —
  AES-256-GCM, nonce 12 байт, AAD = `b"wallet-seed"` (`_ENC_PREFIX = "enc:"`).
- `decrypt_seed(key, value) -> str | None` — обратно; мусор/неверный ключ → None.
- `_aesgcm()` — ленивый импорт `cryptography`, ошибка импорта кэшируется
  (`_AESGCM_IMPORT_FAILED`) — без пакета шифрование явно отключается.

### 3.9. `database.py` — SQLite

`Database(path="farming_state.db", master_key=None)`; `aiosqlite`, PRAGMA:
`journal_mode=WAL; synchronous=NORMAL; cache_size=-64000; busy_timeout=5000`.

- Схема (`_ensure_schema`): `wallets` (address PK, private_key, mnemonic,
  total_actions, total_attempts, last_cycle, last_action, created_at;
  миграция `total_attempts`), `actions_log` (address, action_type, tx_hash,
  success, timestamp, details; индекс по address), `nonces` (address PK,
  current_nonce), `cycle_history` (started_at, mode, duration_s, wallets,
  wallets_ok, actions_ok, errors, rpc_url + миграция колонок RPC-телеметрии
  rpc_calls/rpc_errors/rpc_latency_ms).
- `init()`: подключение, бэкап при порче (`_backup_if_needed`), восстановление
  (`_recover_corrupted_db`), VACUUM по расписанию (`_maybe_vacuum`), схема,
  миграция легаси-сидов (`_migrate_legacy_seeds` — перешифровка старых записей),
  ограничение прав файлов (`_restrict_db_files`), чистка бэкапов (`_purge_backups`).
- Кошельки: `save_wallets_batch`, `get_all_wallets`, `get_all_addresses`,
  `update_wallet_health_batch`, `any_seed_encrypted`.
- Nonce: `get_nonce` / `set_nonce` — постоянный счётчик в таблице `nonces`
  (сохраняется между циклами; `NetworkManager.claim_nonce` держит
  резервирование в памяти и возвращает nonce при отмене).
- Логи действий: `log_actions_batch`, `prune_actions_log(keep_latest=200000)`.
- Статистика: `get_stats`, `get_top_wallets`, `record_cycle`,
  `get_cycle_history`, `get_cycle_stats`.
- Бэкапы: `backup_now` (копия БД через SQLite backup API + ограничение прав
  каталога `_restrict_backup_dir`), `list_backups`, `restore_backup`,
  `delete_backup`.
- **Redaction**: `_redact_rpc_url(url)` — overload для `str | None` и
  `list[str]` (`_redact_one` на каждый элемент): стирает credentials
  (netloc до `@`) и query-string (`?api_key=...`),
  `https://user:pass@host/path?api_key=X` → `https://host/path`. Используется
  во всех логах, истории циклов, CLI-выводе.

### 3.10. `batchwriter.py` — буферизованная запись

`BatchWriter(db, flush_every=2.0, max_buffer=500, flush_on_action=False)` —
копит строки `actions_log` и инкременты здоровья, сбрасывает батчами
(`executemany`). Защиты:
- данные забираются под локом, запись в БД — вне лока;
- два коммита (actions, health) «вычёркиваются» порознь — при падении второго
  не задваиваются уже записанные;
- после 3 подряд сбоев БД батч отбрасывается, `dropped_count` растёт,
  cooldown 30 с (`_cooldown_until`), иначе буфер рос бы бесконечно;
- `stop()` — cancel фонового цикла + финальный `flush()`.

### 3.11. `wallet.py` — генерация кошельков

`WalletManager(config, db)`:
- `generate_wallet()` — BIP-39 мнемоника через `eth_account` (совместимость
  web3 6/7).
- `create_wallets(count)` — генерация в `ThreadPoolExecutor(gen_workers)`,
  вставка батчами `_WRITE_BATCH = 200`.
- `export_to_file(wallets, path)` (static) — JSON с address/private_key/mnemonic
  (через `core.exporter`).

### 3.12. `network.py` — RPC-слой (ключевой модуль)

`NetworkManager(config, db)` — **один на сессию** (singleton запрещён по
архитектурному решению: латентность, кэши и монитор общие).

- **Мульти-RPC**: `network.rpc_url` — строка или список; `_rpc_urls` внутренне
  всегда список; активный `rpc_url`.
- **`RpcNode`** (слоты: url, latency_ms, errors, successes, is_dead):
  - стартовая латентность 500 мс (пессимистично), `successes=1`;
  - `record(latency_ms, success)`: EMA `lat = lat*0.7 + fresh*0.3`;
    успех — `successes+1`, `errors-1`; провал — наоборот, и при
    `errors > _ZOMBIE_THRESHOLD (3)` нода помечается мёртвой;
  - **zombie-воскрешение**: один успешный пинг сразу снимает `is_dead`;
  - `score = latency_ms * (1 + error_rate*5)`; мёртвая → `inf`
    (низкий score = лучше).
- **Монитор**: `start_monitor()` (вызывается из `pool.run_once`) запускает
  `_monitor_rpc()` — каждые `_RPC_MONITOR_INTERVAL = 30.0` с проба
  `probe_all()`/пинг каждой ноды (`_PING_TIMEOUT_MS = 2500.0`), обновляет
  `_record_latency`; если лучшая нода лучше активной — `_switch_to(best)`.
  `monitor_started` — статус. `close()` отменяет задачу (идемпотентен).
  **С одним эндпоинтом монитор не запускается** (не из кого выбирать —
  не расходуем RPC-лимит на пинги): здоровье такой ноды питается от реальных
  бизнес-вызовов (см. Exec-модель).
- **Пробы**: `latency_probe(url=None)`/`probe_all()` — диагностический трафик
  мимо rate-limiter и метрик (не тратят токены, не искажают calls/avg_ms.
  Успешный реальный вызов в `run_in_executor` обновляет EMA-латентность
  активной ноды и снимает `is_dead` (zombie-воскрешение первым успехом).
- **Выбор ноды**: `best_rpc_url()` — минимальный score среди живых; при всех
  мёртвых возвращает наименее плохую (fallback), `node_health()` — список
  `{url, latency_ms, errors, successes, is_dead, score}` (URL отдаётся только
  в диагностику; наружу — redacted).
- **`_switch_to(url)`** — единая логика переключения под `asyncio.Lock`:
  сначала подключение к новому эндпоинту (если он мёртв — старый остаётся
  рабочим), затем замена `self.w3`, закрытие старого провайдера, сброс
  TTL-кэшей (nonce/balance/gas) и перенос остатка токенов rate-limiter
  (`recover_tokens`, против burst на новой ноде). `_switch_rpc()` — круговой
  failover по списку.
- **Ретраи**: `run_retry(fn, *args)` — `_RPC_RETRIES = 3` с классификацией
  `_is_retryable_error` (`_RETRY_HINTS`/`_NEVER_RETRY_HINTS` по тексту ошибки);
  между попытками `_switch_rpc()`; провал пишется в EMA-статистику ноды.
- **Circuit breaker**: `_CircuitBreaker(failure_threshold=5,
  recovery_timeout=30)` — OPEN блокирует вызовы до таймаута (metrics в
  `diagnostics()`); `reset()` обнуляет окно ошибок — вызывается при
  `_switch_to`, чтобы счётчик ошибок старого эндпоинта не блокировал свежий.
  `diagnostics()` отдаёт все URL redacted (credentials/query-token наружу
  не уходят).
- **Rate-limiter**: `_RPCRateLimiter(rate=50)` — token-bucket; адаптивная
  ставка `_adaptive_rate()` из `_effective_rpc_rate(base, health, floor=0.2)`;
  `recover_tokens` при переключении нод.
- **`concurrency_factor() -> float`** — контракт `0.15..1.0` для
  `WorkerPool.health_fn`: комбинация здоровья нод и circuit breaker; если все
  ноды мертвы — 0.3 (не ниже: иначе пул встал бы навсегда).
- **TTL-кэши**: `_TTLCache` для gas_price / balance / nonce (TTL из
  `config.cache.*`); `invalidate_balance(addr)`.
- **Exec-модель**: `run_in_executor` — web3-синхронные вызовы в
  `ThreadPoolExecutor(rpc_threads)`; после успешного вызова записывает
  `_record_latency(self.rpc_url, elapsed)` — реальная задержка и
  zombie-воскрешение без монитора.
- **API**: `latency_probe(url=None)`, `probe_all() -> [(url, ms|None)]`,
  `diagnostics()` (calls, errors, avg_ms, chain_id, active, cb_state,
  cb_failures, rpc_rate, nodes), `take_metrics()`, `get_account(pk)`,
  `get_balance(addr, refresh=False)`, `claim_nonce/release_nonce/
  rollback_nonce_if_free` (локальный счётчик nonce), `get_gas_price()`,
  `wait_for_receipt(tx_hash, timeout=None)`, `send_raw_transaction(raw)`,
  `send_transfer(...)` (sign → send → ожидание чека), `close()`
  (монитор + провайдеры).
- **Автохил при ожидании ресипта**: бюджет `advanced.receipt_timeout`
  (по умолчанию 30с вместо 120с). По истечении `wait_for_receipt` возвращает
  None (tx принята в mempool, nonce НЕ откатывается) — воркер не стоит в
  ступоре на медленной сети. 3+ ошибки поллинга подряд = нода молчит →
  штраф EMA и `_switch_rpc()` failover (поллинг идемпотентен, любой
  эндпоинт читает ресипт из блокчейна); single-node — поллинг продолжается.
- **Сторож монитора RPC** (`_monitor_supervisor`): фоновый `_monitor_rpc`
  запускается под супервизором — неожиданное завершение (баг вне try-блока,
  будущий рефакторинг) перезапускается с экспоненциальным backoff
  (1с → 2 … кап = `_RPC_MONITOR_INTERVAL`), так что ротация и сбор задержек
  не вымирают сами по себе. Отмена (close) прокидывается наверх без рестарта,
  вложенная задача гасится (не остаётся сиротой); из `_monitor_rpc`
  `CancelledError` теперь пробрасывается через `raise`, а не гасится `return`.

### 3.13. `workpool.py` — воркер-пул

`WorkerPool(max_workers, worker_func, health_fn)`; контракт
`worker_func(wallet, all_addresses, cycle_number) -> (address, actions_done)`.

- `submit(wallet, ..., urgent=False)` — asyncio.PriorityQueue.
- `_worker_loop` — N корутин-воркеров, `get()` с таймаутом `_GET_TIMEOUT=0.05`
  (позволяет воркеру заметить завершение).
- `_monitor()` — каждые `_MONITOR_INTERVAL = 5.0` читает
  `health_fn() -> float 0.15..1.0` (`_HEALTH_MIN/_HEALTH_MAX`) и подстраивает
  число воркеров (масштаб `round(max_workers * health)`, минимум 1).
- **Выключение без `task.cancel()`**: лишние воркеры только «уходят на пенсию»
  (retire) — задача завершается сама; это исключает гонку
  `Task.cancel()`/`Queue.get()` (wait_for + get не отменяемы безопасно).
- **Автохил воркеров**: аварийное падение (исключение вне внутреннего
  `try`) не прорежает пул — `_schedule_respawn()` воскрешает слот после
  backoff (`_WORKER_RESPAWN_DELAY`, геометрический рост при серии падений,
  сброс после мониторингового интервала), лишних воркеров не плодит
  (потолок = target по health). Stop гасит запланированное воскрешение;
  исключения завершившихся задач извлекаются (`_drain_exception`) — нет
  «Task exception was never retrieved».
- `start()` / `stop()` / `join()` → `results: {address: actions}`.

### 3.14. `pool.py` — FarmerPool (оркестратор фарма)

`FarmerPool(config, db, network=None)` — один NetworkManager на пул;
`executor = ActionExecutor`, `faucet = Faucet`, `writer = BatchWriter`.

- `_CycleState` (`_STATE_FILE = "state.json"`) — crash recovery: сохраняет
  `cycle_number`, `started_at`, `processed_addresses`; `load/save/mark_done/
  clear`.
- `run_once(wallets, all_addresses, cycle_number=0, stop_event=None)`:
  - pre-flight крана (`_faucet_validated`, один раз за прогон);
  - crash recovery: при совпадении номера цикла уже обработанные адреса
    пропускаются; несовпадение — сброс состояния;
  - `network.start_monitor()` (RPC-монитор + ротация работают сразу);
  - один `WorkerPool` на весь прогон: чанки по `chunk_size`
    (`effective_chunk_size`, по умолчанию `workers*4`, min 100) →
    `_run_chunk_with_pool` → `submit` всех кошельков чанка → `join`;
  - пауза (`pause`) ожидается внутри цикла чанков (`while self.paused`),
    `stop_event` проверяется перед каждым чанком и во время паузы —
    аккуратная остановка;
  - crash-state: `mark_done` по адресам с результатом > 0 + чанковая точка
    сохранения; дебаунс автосейва: файл O(n) переписывается не «каждые 50
    адресов», а не чаще раза в 2с при накопившемся батче ≥50 новых (mid-chunk
    окно закрывает чанковая точка, crash recovery не дырявит); при
    исключении/отмене прогресс сохраняется, `executor.flush()` досылает лог;
  - полный цикл → `_state.clear()` + `db.record_cycle(...)` (RPC-метрики
    через `take_metrics`); прерванный — состояние остаётся для продолжения;
  - в конце — `network.clear_accounts_cache()` (не держим ключи в памяти);
  - результат: `[(address, actions), ...]`, `live_stats()`
    (processed/actions/errors/paused/dropped/dyn_workers/health).
- `_run_one` — обработка одного кошелька: профиль «отдыха»
  (`_rest_until`), staggered-вход (`profile.start_delay`), `Farmer.run_cycle()`
  в `asyncio.wait_for(timeout)`.
- `run_forever(stop_event)` — бесконечные циклы с
  `delay_between_cycles` и профильными множителями; в конце — `record_cycle`
  в БД.
- `pause()/resume()`, `close()` (BatchWriter → executor → network,
  идемпотентно).

### 3.15. `farmer.py` — цикл одного кошелька

`Farmer(wallet, config, network, executor, faucet, all_addresses, profile)`:
- `_skip_probability()` — шанс пропуска цикла: `skip_cycle_probability` ×
  `activity` × `time_of_day_factor(daily_window_strength)`;
- `_actions_count()` — из коридора профиля (`actions_lo/width`),
  с `burst_prob/burst_mult` («всплеск» в 1.6 раза больше действий);
- `_action_delay_bounds()` — `delay_between_actions` × `delay_actions_scale`;
- `run_cycle() -> int` — проверка баланса (`force_farm` при нуле),
  кран при необходимости (`ensure_balance`, профильный `faucet_skip_prob`),
  затем N действий через `executor.execute_action` (передача, контракт);
  кап подряд идущих провалов (`max_consecutive_failures`); возвращает число
  выполненных действий.

### 3.16. `actions.py` — исполнение действий

`ActionExecutor(network, config, writer, all_addresses)`:
- `CONTRACT_ACTIONS` — типы контрактных действий (vibevibe/flop/arc);
- `_pick_action(profile)` — выбор по весам × профильным множителям
  (`action_multipliers`); **адаптивность**: `_adaptive_scores(weights,
  outcomes)` пересчитывает веса по окну `_OUTCOME_WINDOW = 20` исходов
  (минимум `_ADAPT_MIN_SAMPLES = 5` наблюдений) — падающие типы действий
  автоматически теряют вес;
- `transfer(wallet, to, amount, gas_mult)` — перевод (неконтрактный путь),
  `to == "random_wallet"` → случайный адрес из пула;
- `_contract_call(...)` — `vibevibe.call_method` (ABI-контракты); если
  контракт — 0x0-заглушка, фоллбэк на обычный transfer (не ломает тестовые
  конфиги);
- `execute_action(...)` — диспетчер по типу, `gas_multiplier(profile)`,
  jitter сумм (`amount_jitter`/`odd_amount_prob`), запись в `writer`
  (буферизованный лог), `flush()` при завершении.

### 3.17. `vibevibe.py` — ABI-интерфейс контрактов

`VibeVibeInterface(network, config)`: `w3` — свойство → живой
`network.w3`; `_load_abi()` — ABI из `config.network.abi_path`
(default `abi/vibevibe.json`), при отсутствии файла — `_PLACEHOLDER_ABI`
(минимальная заглушка); `_get_contract(address)`; `call_method(address,
method, amount, wallet, all_addresses, gas_mult, ...)` — build_transaction
(chain_id, gas price с множителем, nonce из `network.claim_nonce`),
подпись, отправка, ожидание receipt; gas_limit default 300000.

### 3.18. `behavior.py` — анти-сибил профили

`WalletProfile` — dataclass-множители (activity, rest_prob, rest_cycles,
delay_actions_scale, delay_cycles_scale, actions_lo/width, action_multipliers,
amount_min/max_mul, gas_deviation, odd_amount_prob, faucet_skip_prob,
daily_window_strength, burst_prob/burst_mult, start_delay, neutral).
`profile_for(address, config)` — **детерминированно** от
`sha256(address)[:16]` как seed `random.Random` (перезапуск не меняет
поведение); `behavior.enabled: false` → нейтральный профиль.
`time_of_day_factor(strength)` — днём (7–22) до −25%, ночью до +150% шанса
пропуска; `gas_multiplier(profile)` — отклонение газа ±gas_deviation.

### 3.19. `faucet.py` — кран

`Faucet(config)`: `strategies` — список dict (`type: chainstack|direct|web`,
`url`, `network_param`); `_REQUEST_TIMEOUT = 8`.
- `validate() -> int` — pre-flight: сколько стратегий живы. Для `chainstack` —
  реальный MCP handshake (`initialize` → `mcp-session-id` → кэш на пачку).
  Для остальных — HTTP-проба (404/410 = мертва, остальные статусы = жива).
- `_chainstack_request` — Chainstack MCP: `tools/call request_testnet_funds`
  с `Authorization: Bearer <CHAINSTACK_API_KEY>` (env, не config). Ключ берётся
  из `CHAINSTACK_API_KEY` (https://console.chainstack.com/user/settings/api-keys).
  Без ключа стратегия исключается на pre-flight с предупреждением.
- `request_tokens(address, retries)` — `_pick_proxy` (из `proxy.list`,
  `rotate: sequential|random`), `aiohttp` + random `delay_between_requests`,
  retries, `fake-useragent`;
- `ensure_balance(network, address)` — добрать до `target_balance`
  (не ниже `min_balance`);
- `concurrent_batch(workers)`, `request_batch(addresses, batch, network,
  progress, pause, max_concurrent)` — семфорный массовый запрос, возвращает
  `(ok, failed)`; `close()` — закрытие сессии и кэша MCP-сессий.

### 3.20. `exporter.py` — экспорт и файловое шифрование

- `dedup_export` — дедупликация по address/private_key.
- `export_csv` — колонки `address, private_key, mnemonic, actions`;
  `utf-8-sig` (BOM для Excel); `_csv_cell_safe` нейтрализует формульные
  инъекции (`=`, `+`, `-`, `@`, таб, CR → апостроф-префикс).
- `export_json`, `export_to_file` (CLI-меню, без счётчиков).
- `_atomic_write_restricted` — tmp → ограничение прав → `os.replace`.
- `encrypt_file(path, password)` — AES-256-CBC, PKCS7; формат v2:
  `[16B salt][16B IV][ciphertext][32B HMAC]`; PBKDF2-SHA256 100k итераций,
  dklen=64 → ключи шифрования и MAC разделены (key separation);
  `decrypt_file` проверяет HMAC (неверный пароль не пишет файл), читает
  легаси v1 (единый ключ) для совместимости.

### 3.21. `license.py` — лицензионный гейт

`LicenseManager(config)`: `enabled`, `deploy_url`, `deploy_salt`,
`grace_days=7`, `cache_file=.license_cache`, `timeout=8`.
- Владелец публикует `license.json` (`app`, `v>=2`, `status: active|revoked`,
  `pass` = `pbkdf2_sha256$210000$salt$hex`, `expires`) в GitHub raw;
- `_fetch()` — только https (или http на loopback); тело не больше
  `_MAX_LICENSE_BODY` (1 МБ);
- `require_activation(password=None) -> (ok, reason)`: онлайн-статус с hub
  всегда перекрывает кэш; `revoked` гасит копии мгновенно; `expires`
  проверяется; офлайн — HMAC-подписанный кэш активации, действует
  `grace_days`;
- легаси sha256-хэши читаются (с warning);
- `enforce_license_async(config, interactive, attempts=3)` — env
  `LICENSE_PASSWORD` → `getpass` → `LicenseError`;
- CLI владельца: `python -m core.license setpass ПАРОЛЬ [salt]`.

### 3.22. `doctor.py` — самодиагностика

`doctor(ui, config) -> bool` — последовательные проверки (emit ok/fail):
конфиг (валидность, redacted RPC), производительность
(`summarize`), master-ключ, БД (init/чтение/шифрование), RPC (latency,
chain_id, circuit breaker, ноды), кран (живые стратегии), лицензия.
`--doctor` в auto.py возвращает exit code.

### 3.23. `version.py`

`VERSION = "2.3.0"`, `version_line() -> "harvest v2.3.0"` (используется в
`--version` и баннерах).

---

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
| `PORTAL_DB` | путь к БД фермера (default `farming_state.db`) |
| `PORTAL_FARM_CONFIG` | конфиг фармера (`config.yaml`) |
| `PORTAL_LINKS` | `links.json` владельца |
| `PORTAL_BASE_URL` | публичный URL (для OAuth-редиректа/Mini App) |
| `GOOGLE_CLIENT_ID/SECRET` | Google OAuth 2.0 |
| `GOOGLE_ALLOW_EMAILS` | default-deny список email |
| `TELEGRAM_BOT_TOKEN` | токен бота (BotFather) |
| `TELEGRAM_ALLOW_IDS` | default-deny список user_id |
| `FARMER_MASTER_KEY` | мастер-ключ БД / PIN веб-входа |
| `PORTAL_OPEN_ACCESS=1` | сознательно открыть доступ любому Google/TG-аккаунту |
| `PORTAL_PASSWORD_LOGIN=1` | вход по мастер-ключу (default 1) |
| `PORTAL_ALLOW_PASSWORD_HTTP=1` | пароль по HTTP (только dev) |
| `PORTAL_COOKIE_SECURE` | Secure-кука (авто при https base_url) |
| `PORTAL_TRUST_PROXY=1` | доверять X-Forwarded-For/Proto |

Fail-closed при старте: bind не на loopback без единого способа входа
(Google/пароль/Telegram) → отказ запуска. `validate_master_key` —
строгие 64 hex (ключ БД) или PIN ≥ 8 символов (с предупреждением).

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
(stop не блокирует HTTP — задача в фоне), `/api/cycle-history`,
`/api/top-wallets`, `/api/tg/init` (только POST, лимит 16 КБ), `/static`.

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
  статистика циклов), `top_wallets()`, `cycle_history()`, `close()`.

### 4.5. `bot_telegram.py` — Telegram-бот (aiogram, long-polling)

- `_allowed` — default-deny по `TELEGRAM_ALLOW_IDS` (+`PORTAL_OPEN_ACCESS`).
- Кнопки: старт/стоп/пауза/резюм фермы, статистика (`_fmt_stats`), ссылки
  (`load_links`, санитизация, максимум 15 строк), Mini App (`WebAppInfo`
  → фронтенд шлёт initData в `/api/tg/init`).
- Все значения из daemon проходят `html.escape` (defense-in-depth);
  сбои callback пишутся в журнал и отвечают generic-текстом.

---

## 5. Конфигурация фермера

Обязательные секции: `network`, `wallets`, `faucet`, `actions`, `farming`,
`threading`, `database` (см. `_REQUIRED_SECTIONS`). Шаблон —
`config.example.yaml` (бандлится в exe в `_internal/`).

### 5.1. Три тестнета

| | Robinhood | Flop Labs | Arc (Minara.Fun) |
|---|---|---|---|
| Файл | `config_robinhood.yaml` | `config_flop.yaml` | `config_arc.yaml` |
| chain_id | 46630 | 99999 (заглушка) | 5042002 |
| Валюта | ETH | FLOP | ARC |
| Действия | transfer, vibevibe_swap/mint | flop_compute/validate/stake + transfer | arc_launch/trade/add_liquidity + transfer |
| farming | [3,8] действ., паузы [5,20] с, цикл 1–2 ч | [5,15], [3,15] с, 30–60 мин | [5,12], [2,10] с, 20–40 мин |
| workers | 20 | 30 | 30 |
| rpc_rate_limit | 50 | 80 | 80 |
| БД | `farming_state.db` / `master.key` | `farming_flop.db` / `master_flop.key` | `farming_arc.db` / `master_arc.key` |

Контрактные контракты в конфигах — 0x0-заглушки (рантайм фоллбэчит на
transfer); реальные адреса подставляются оператором.

### 5.2. Env-overrides (`FARMER_*`)

`FARMER_CONFIG` (путь к конфигу), `FARMER_RPC_URL` (строка или список),
`FARMER_CHAIN_ID`, `FARMER_WALLET_COUNT`, `FARMER_WORKERS`,
`FARMER_GEN_WORKERS`, `FARMER_RPC_THREADS`, `FARMER_CHUNK_SIZE`,
`FARMER_FAUCET_MAX_CONCURRENT`, `FARMER_RPC_RATE`, `FARMER_RPC_RATE_FLOOR`,
`FARMER_MIN_BALANCE`, `FARMER_TARGET_BALANCE`, `FARMER_LOG_LEVEL`,
`FARMER_DB_PATH`, `FARMER_MASTER_KEY` (64 hex), `LICENSE_PASSWORD`.

### 5.3. Поведение (анти-сибил) — `behavior:`

`enabled`, `activity [0.5,1.5]`, `rest_probability 0.05`, `rest_cycles [1,3]`,
`action_delay_scale [0.7,1.5]`, `cycle_delay_scale [0.8,1.8]`,
`action_mix 0.4`, `amount_jitter 0.25`, `gas_deviation 0.06`,
`odd_amount_prob 0.15`, `faucet_skip_prob 0.1`, `daily_window 0.3`,
`burst_prob 0.05`, `burst_multiplier 1.6`, `start_delay [0,3]` секунд.

---

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

---

## 7. Тесты

`tests/` — 31 файл (`pytest`, `pyproject.toml: testpaths=tests, -q`),
732 теста, все зелёные:

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
  `test_portal_api`, `test_portal_guard`, `test_bot_telegram`;
- покрытийные сквозные ветки: `test_cov_ui_actions_config`,
  `test_cov_portal_config_main_bot`, `test_cov_portal_api_auth_farm`,
  `test_cov_doctor_faucet_vibevibe`.

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

Референсы `config_robinhood.yaml`, `config_flop.yaml`, `config_arc.yaml` —
готовые каркасы с **заглушками**: validator их пропускает (transfers работают),
но `--doctor` печатает жёлтые «Замечания» и runtime предупреждает о 0x0.
Перед реальным фармом впишите данные сети:

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
