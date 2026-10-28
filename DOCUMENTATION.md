# harvest — полная документация проекта и кода

Версия: **v2.4.1** · Python 3.12 · Windows/PowerShell (первичная цель — exe-дистрибутив)

**Автор:** [ssrjkk](https://t.me/ssrjkk_bot) · © 2026 ssrjkk · Лицензия [MIT](LICENSE)

harvest — автоматизированный фермер тестнет-сетей (EVM): массовая генерация кошельков,
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
- **Персоны нагрузки**: детерминированные поведенческие профили на кошелёк
  (активность, паузы, суммы, газ, время суток, «отдых», staggered-старт) —
  флот выглядит как живые пользователи, а не как один скрипт.
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
            |                     core/behavior.py WalletProfile (персоны нагрузки)
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
  `--version`, `--history [N]`, `--doctor`, `--schedule HOURS` (минимум 0.05 ч),
  `--pnl-sample N` / `--pnl-out PATH` / `--skip-pnl` (PnL-отчёт по прогону),
  `--force-farm` (фермить даже без крана и без денег на кошельках).

`run_once()` возвращает код: 0 — цикл выполнен, 1 — остановлен (например,
кран недоступен и ни один кошелёк не имеет средств). Код доходит до кода
возврата процесса; в режиме `--schedule` цикл повторяется по расписанию.

**Кран мёртв, денег нет — фарм не идёт.** Если кран недоступен и на выборке
кошельков (5 шт.) нет средств, прогон останавливается до отправки
транзакций: каждый перевод тогда заведомо падает («insufficient funds»),
сеть сожжёт лимит, а лог забьётся бесполезными отказами. Если средства
заведены вручную — фарм идёт. Обход: `--force-farm`.

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

## Читать дальше

- [Ядро `core/` — модули, сеть, пул, кран](devdocs/core.md) (§3)
- [Портал `portal/` — веб, API, бот, Mini App](devdocs/portal.md) (§4)
- [Конфигурация фермера — сети, env, behavior, сценарии](devdocs/config.md) (§5)
- [Безопасность, тесты, сборка, эксплуатация](devdocs/ops.md) (§6–§10)
