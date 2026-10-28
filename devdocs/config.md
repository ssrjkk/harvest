# Конфигурация фермера — harvest

> ← Назад: [DOCUMENTATION.md](../DOCUMENTATION.md)

## 5. Конфигурация фермера

Обязательные секции: `network`, `wallets`, `faucet`, `actions`, `farming`,
`threading`, `database` (см. `_REQUIRED_SECTIONS`). Шаблон —
`config.example.yaml` (бандлится в exe в `_internal/`).

### 5.1. Тестнеты

| | Robinhood | Flop Labs | Arc (Minara.Fun) | vibe/vibe |
|---|---|---|---|---|
| Файл | `config_robinhood.yaml` | `config_flop.yaml` | `config_arc.yaml` | `config_vibevibe.yaml` |
| chain_id | 46630 | 99999 (заглушка) | 5042002 | 46630 |
| Валюта | ETH | FLOP | ARC | ETH |
| Действия | transfer, vibevibe_swap/mint | flop_compute/validate/stake + transfer | arc_launch/trade/add_liquidity + transfer | transfer, vibevibe_mint (claimRewards/stake) |
| farming | [3,8] действ., паузы [5,20] с, цикл 1–2 ч | [5,15], [3,15] с, 30–60 мин | [5,12], [2,10] с, 20–40 мин | [3,8], [5,20] с, 1–2 ч |
| workers | 20 | 30 | 30 | 20 |
| rpc_rate_limit | 50 | 80 | 80 | 50 |
| БД | `farming_robinhood.db` / `master.key` | `farming_flop.db` / `master_flop.key` | `farming_arc.db` / `master_arc.key` | `farming_vibevibe.db` / `master.key` |

vibe/vibe — permissionless token launchpad на Robinhood Chain testnet
(5% сапплая для тестнет-участников). ABI (`abi/vibevibe.json`) содержит
верифицированные контракты: VibePassMarket (buy/sell на bonding curve),
VibeLoungeRegistry (registerLounge), VibeShop, VibeItems, VibeFuelStaking
(stake/unstake), VibeFuelRewards (claimRewards). Buy/sell требуют EIP-712
подпись TradeIntent ключом `tradeSigner` (config `vibevibe.signer_key` /
env `FARMER_VIBE_SIGNER_KEY`); без неё действия пропускаются адаптивными
весами. `registerLounge` доступен только `REGISTRAR_ROLE` платформы.
Всегда работают `transfer` / `claimRewards` / `stake`.

Контрактные контракты в конфигах — 0x0-заглушки (рантайм фоллбэчит на
transfer); реальные адреса подставляются оператором.

### 5.2. Env-overrides (`FARMER_*`)

`FARMER_CONFIG` (путь к конфигу), `FARMER_RPC_URL` (строка или список),
`FARMER_CHAIN_ID`, `FARMER_WALLET_COUNT`, `FARMER_WORKERS`,
`FARMER_GEN_WORKERS`, `FARMER_RPC_THREADS`, `FARMER_CHUNK_SIZE`,
`FARMER_FAUCET_MAX_CONCURRENT`, `FARMER_RPC_RATE`, `FARMER_RPC_RATE_FLOOR`,
`FARMER_MIN_BALANCE`, `FARMER_TARGET_BALANCE`, `FARMER_LOG_LEVEL`,
`FARMER_DB_PATH`, `FARMER_MASTER_KEY` (64 hex), `LICENSE_PASSWORD`.

### 5.3. Поведение (персоны нагрузки) — `behavior:`

`enabled`, `activity [0.5,1.5]`, `rest_probability 0.05`, `rest_cycles [1,3]`,
`action_delay_scale [0.7,1.5]`, `cycle_delay_scale [0.8,1.8]`,
`action_mix 0.4`, `amount_jitter 0.25`, `gas_deviation 0.06`,
`odd_amount_prob 0.15`, `faucet_skip_prob 0.1`, `daily_window 0.3`,
`burst_prob 0.05`, `burst_multiplier 1.6`, `start_delay [0,3]` секунд.
Любой knob вероятности/силы (`rest_probability`, `odd_amount_prob`,
`faucet_skip_prob`, `daily_window`, `burst_prob`, `burst_multiplier`,
`gas_deviation`, `amount_jitter`, `action_mix`) принимает пару `[lo, hi]` —
тогда значение разыгрывается на кошелёк детерминированно от адреса;
скаляр — как раньше (одно на всех). Верхняя граница пары используется
в бюджетах таймаутов пула (`cfg_hi`). Новые оси ритмов: `weekend_strength`
(выходные активнее: пропуск ×(1−0.3·s)), `day_start`/`day_width` — личное
окно активности (`hour_window_factor`: внутри ×0.85, снаружи ×1.8);
учитываются в пропуске цикла (`core/farmer.py`), нейтральный профиль — 1.0.

### 5.4. Сценарии QA-прогонов — как гонять чужой чейн

Мастер в демо-зоне и `/cmd`-визард собирают четыре готовых пакета
(масштаб задаётся там же; персона — пресетом `behavior`):

- **Smoke** — 5 кошельков, 2 круга, кран вкл: чейн вообще жив? Базовый
  ответ получают за минуты.
- **Load** — 200 кошельков, 5 кругов, кран вкл: пропускная способность
  RPC/мемпула, `health_factor` и `dyn_workers` в отчёте покажут, где чейн
  просел.
- **Soak** — 50 кошельков, 20 кругов, кран вкл: долгая стабильность,
  дрейф latency и накопление ошибок (`by_reason` в PnL-снапшоте).
- **Chaos** — 100 кошельков, 5 кругов, кран ВЫКЛ: негативный путь —
  как чейн и отчёт отвечают на заведомо неуспешные действия
  (`--skip-faucet`; пустые кошельки дают чистый останов с пробой, а не
  тысячи отказов).

Читать итог: блок «Поле» (health, действия/ошибки, `dropped`) +
`pnl_report.html` (success rate, `by_type`/`by_reason`, дельты балансов) +
`/doctor` (сеть, chain_id, деградация). Зелёный прогон Smoke →
Load → Soak — минимальный знак годности чейна к нагрузке.

---
