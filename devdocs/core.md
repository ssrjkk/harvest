# Ядро `core/` — harvest

> ← Назад: [DOCUMENTATION.md](../DOCUMENTATION.md)

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

- `resolve_master_key(config, ignore_env=False, db_path=None) -> bytes` — ключ БД.
  Кандидаты по приоритету: `FARMER_MASTER_KEY` env (64 hex) → файл
  `database.master_key` (относительно CWD) → `master.key` **рядом с БД**
  (`database.path`). Если кандидатов несколько, выбирается тот, который
  реально расшифровывает первый seed этой БД (`_sample_encrypted_seed` +
  `decrypt_seed`): ферма могла стартовать из другой папки, и «тот самый»
  файл в CWD может оказаться чужим. Нет ключа и есть секреты →
  `MasterKeyError` (fail-closed). Файл создаётся автоматически (атомарно,
  права 0600), если не найден.
- `encrypt_seed(key, plaintext) -> "enc:<nonce>:<ciphertext>"` —
  AES-256-GCM, nonce 12 байт, AAD = `b"wallet-seed"` (`_ENC_PREFIX = "enc:"`).
- `decrypt_seed(key, value) -> str | None` — обратно; мусор/неверный ключ → None.
- `_aesgcm()` — ленивый импорт `cryptography`, ошибка импорта кэшируется
  (`_AESGCM_IMPORT_FAILED`) — без пакета шифрование явно отключается.

### 3.9. `database.py` — SQLite / PostgreSQL

`Database(path="farming_state.db", master_key=None)`.

- **Два бэкенда**: по умолчанию **SQLite** (`aiosqlite`, PRAGMA:
  `journal_mode=WAL; synchronous=NORMAL; cache_size=-64000; busy_timeout=5000`);
  **PostgreSQL** — если `path` задан DSN `postgres://user:pass@host:5432/db`
  (или `postgresql://`) → `asyncpg`.
- Адаптер `_PgAdapter` имитирует интерфейс aiosqlite (`execute` возвращает
  awaitable-курсор `_PgCursor`, работает `await db.execute(...)` и
  `async with db.execute(...)`); `_translate_pg` переводит диалект:
  `?`→`$N`, `datetime('now')`→`now()`, `AUTOINCREMENT`→`GENERATED ... IDENTITY`,
  `INSERT OR REPLACE/IGNORE`→`ON CONFLICT DO NOTHING`, `PRAGMA`/`VACUUM`→no-op.
- Для PG пропускаются файловые шаги (бэкап/VACUUM/права) и `PRAGMA table_info`
  заменяется на `information_schema.columns` (миграция колонок RPC-телеметрии).

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
  (Всё файловое — только для SQLite.)
- Кошельки: `save_wallets_batch`, `get_all_wallets`, `get_all_addresses`,
  `update_wallet_health_batch`, `any_seed_encrypted`.
- Nonce: `get_nonce` / `set_nonce` — постоянный счётчик в таблице `nonces`
  (сохраняется между циклами; `NetworkManager.claim_nonce` держит
  резервирование в памяти и возвращает nonce при отмене).
- Логи действий: `log_actions_batch`, `prune_actions_log(keep_latest=200000)`.
- Статистика: `get_stats`, `get_top_wallets`, `record_cycle`,
  `get_cycle_history`, `get_cycle_stats`.
- Снапшоты PnL: `save_pnl_snapshot(snap)` / `get_last_pnl_snapshot()` —
  таблица `pnl_snapshots`. Ферма кладёт туда агрегаты последнего прогона
  (без адресов), публикатор копирует их в `state.json["pnl"]`, сайт рисует
  блок PnL на «Поле».
- Бэкапы (SQLite): `backup_now` (копия БД через SQLite backup API +
  ограничение прав каталога `_restrict_backup_dir`), `list_backups`,
  `restore_backup`, `delete_backup`.
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
- **Circuit breaker — на ноду**: `_breakers: dict[url, _CircuitBreaker]` с
  ленивым созданием (`_breaker()`); ошибки ноды A больше не блокируют
  здоровую ноду B — у каждой своё окно (`failure_threshold=5`,
  `recovery_timeout=30`). `reset()` при `_switch_to` не нужен и убран
  (состояние старой ноды честно досиживает таймаут). Для совместимости
  `_circuit_breaker` — property на breaker активной ноды;
  `transport_failures()` отдаёт счётчик текущего окна (нужен auto-replay).
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
- `run_cycle() -> int` — проверка баланса (`force_farm` при нуле — пропуск,
  иначе RPC-шторм `have 0`),
  кран при необходимости (`ensure_balance`, профильный `faucet_skip_prob`),
  затем N действий через `executor.execute_action` (передача, контракт);
  кап подряд идущих провалов (`max_consecutive_failures`); **auto-replay**:
  повтор только при росте `network.transport_failures()` — с ротацией RPC;
  логический фейл (revert/конфиг) повторяется на той же ноде без ротации;
  возвращает число выполненных действий.

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
Для VibePassMarket добавлен EIP-712 флоу: `sign_trade_intent()` (подпись
TradeIntent ключом tradeSigner → authorization bytes), `call_trade()`
(buy — payable value=amount; sell — no-value) и `call_register()`
(registerLounge, REGISTRAR_ROLE). `read_authorization_epoch()` тянет
текущий epoch из контракта для digest.

### 3.17a. `pnl.py` — PnL-отчёт по прогону

`build_report(tracker, db, network, currency, explorer, out_path, since_id)` —
пишет самодостаточный HTML (`pnl_report.html`, без внешних файлов) и
**сохраняет агрегаты** в БД (`pnl_snapshots`). «PnL» на тестнете — не прибыль,
а движение нативного баланса: Δ по крану и по фарму, разбивка по типам
действий и по **причинам отказов**.

- `PnlTracker` — снимает балансы выборки кошельков (`--pnl-sample`, 0 = все)
  в ключевых точках прогона: `pre_faucet` → `pre_farm` → `post_run`.
  1000 кошельков × 2 снимка — это 2000 RPC-вызовов ради отчёта, поэтому
  выборка по умолчанию 25 и равномерная (не «топ по активности»)
- `since_id` — окно отчёта: только действия текущего прогона (`max(id)`
  в actions_log на момент старта)
- **Адреса кошельков — только в локальном отчёте.** В публичную ветку уходят
  счётчики: `state.json["pnl"]` → блок PnL на «Поле», `/api/stats` → дашборд
  портала, `/api/miniapp/state` → мини-апп, `/pnl` → боты, PNG-карточка статуса

### 3.18. `behavior.py` — персоны нагрузки (поведенческие профили)
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

`Faucet(config)`: `strategies` — список dict (`type: chainstack|direct|web|donor`,
`url`, `network_param`); `_REQUEST_TIMEOUT = 12`.
- `validate() -> int` — pre-flight: сколько стратегий живы. Для `chainstack` —
  реальный MCP handshake (`initialize` → `mcp-session-id` → кэш на пачку) ПЛЮС
  read-only вызов `get_organization`: сервер поднимает сессию и с
  недействительным ключом, а `tools/call` без побочных эффектов честно
  отвечает «Invalid API key» — квота крана при этом не тратится. Без этой
  проверки стратегия выглядела живой, и ферма уходила в тысячи заведомо
  падающих переводов.
  Для `donor` — проверка ключа без сети (трансфер пойдёт через `ensure_balance`).
  Для остальных — HTTP-проба (404/410 = мертва, остальные статусы = жива).
  Разбор — в `self._last_probe` (`[{type, url(redacted), alive, reason}]`),
  копия — `probe_report()` (auto.py печатает, `/faucet` показывает).
  Ноль живых — `logger.error` с причинами (авария снабжения, не штатно).
  Без `CHAINSTACK_API_KEY` в окружении chainstack-стратегия исключается
  с явной причиной «экспортируйте ключ и перезапустите задачу».
- `_chainstack_request` — Chainstack MCP: `tools/call request_testnet_funds`
  с `Authorization: Bearer <CHAINSTACK_API_KEY>` (env, не config). Ключ берётся
  из `CHAINSTACK_API_KEY` (https://console.chainstack.com/user/settings/api-keys).
  Без ключа стратегия исключается на pre-flight с предупреждением.
- Spam-защита Chainstack (`0.08 ETH on mainnet`): отказ с таким текстом
  детерминирован для адреса — ретраи по нему пропускаются (`_SpamGateError`),
  gated-стратегия исключается из попыток, остальные пробуются (fallback).
  В лог пишется подсказка про авто-клейм https://console.chainstack.com/faucet.
- `donor` (Hub & Spoke): мастер-кошелёк раздаёт тестовый ETH нативным
  трансфером — без mainnet-гейтов и квот. Ключ только через
  `private_key_env` (по умолчанию `FAUCET_DONOR_KEY`), сам донор пополняется
  авто-клеймом консоли (Saved Wallets + recurring auto-claim). В
  `ensure_balance` донор идёт первым, внешние краны — fallback.
- `request_tokens(address, retries)` — `_pick_proxy` (из `proxy.list`,
  `rotate: sequential|random`), `aiohttp` + random `delay_between_requests`,
  retries, `fake-useragent`;
- `ensure_balance(network, address)` — честно до `target_balance`
  (частичный долив ниже target — неуспех, а не «по min сойдёт»);
- `concurrent_batch(workers)`, `request_batch(addresses, batch, network,
  progress, pause, max_concurrent)` — семфорный массовый запрос, возвращает
  `(ok, failed)`; `close()` — закрытие сессии и кэша MCP-сессий.
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

`VERSION = "2.4.1"`, `version_line() -> "harvest v2.4.1"` (используется в
`--version` и баннерах).

---
