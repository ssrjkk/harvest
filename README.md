# harvest v2.3.0

[![CI](https://github.com/ssrjkk/harvest/actions/workflows/ci.yml/badge.svg)](https://github.com/ssrjkk/harvest/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)

**Автор:** [ssrjkk](https://t.me/ssrjkk_bot) · © 2026 ssrjkk

Автоматический фермер тестнет-сетей (EVM): массовая генерация кошельков,
получение токенов через краны, асинхронный фарминг транзакций по профилям
«человеческого» поведения (анти-сибил), веб-портал с Telegram-ботом.

Python 3.12 · Windows/PowerShell (первичная цель — exe-дистрибутив через
PyInstaller) · [полная документация](DOCUMENTATION.md)



## Сайт

Живой сайт проекта: **[ssrjkk.github.io/harvest](https://ssrjkk.github.io/harvest/)** —
поле фермы (метрики из `state.json`), полная документация и браузерная
демо-зона: режимы «Новичок» (мастер за 3 шага) и «Про» (конструктор конфига
и команд, генератор кошельков, симулятор фарма, проверка конфига,
справочник параметров). Темы Paper/Ink/Field, языки RU/EN.

## Screenshots

| Хиро | Поле |
|---|---|
| ![harvest — главная](docs/screenshots/shot-hero.png) | ![harvest — поле фермы](docs/screenshots/shot-live.png) |

| Мастер новичка | Про-справочник |
|---|---|
| ![harvest — мастер за 3 шага](docs/screenshots/shot-wizard.png) | ![harvest — справочник параметров](docs/screenshots/shot-pro.png) |

Метрики поля публикует `python publish_dash.py` с машины фермы в
изолированную ветку `gh-pages` (один amend-коммит, история `master`
не растёт): рядом со `state.json` публикуется шифрованный
`wallets.enc.json` (AES-256-GCM, ключ `WALLETS_KEY` только в env) —
облачный BotForge-бот читает кошельки оттуда без нового провайдера.
Проверка сайта одной командой: `python tools/verify_site.py`,
скриншоты: `python tools/shoot_site.py`.

## Быстрый старт

```powershell
pip install -r requirements.txt
python main.py                 # интерактивное меню (сеть → фарм)
python auto.py --wallets 3     # авто-режим: 3 кошелька, бесконечно (--cycles 5 — 5 циклов)
python auto.py --doctor        # самодиагностика
```

### Сети (тестнеты)

| Сеть | Конфиг | chain_id | Валюта |
|---|---|---|---|
| Robinhood Chain | [config_robinhood.yaml](config_robinhood.yaml) | 46630 | ETH |
| Flop Labs | [config_flop.yaml](config_flop.yaml) | 99999 (заглушка) | FLOP |
| Arc (Minara.Fun) | [config_arc.yaml](config_arc.yaml) | 5042002 | ARC |
| vibe/vibe (Robinhood testnet) | [config_vibevibe.yaml](config_vibevibe.yaml) | 46630 | ETH |

Пресет «только трансферы» для пачного фарма без контрактных адресов —
[config.simple.yaml](config.simple.yaml) (одна сеть Robinhood, отдельная БД
`farming_simple.db`, `gas_limit: 21000`):

```powershell
python auto.py --config config.simple.yaml --wallets 200 --cycles 2
```

vibe/vibe — permissionless token launchpad на Robinhood Chain testnet
(5% сапплая для тестнет-участников). Контракты верифицированы в Blockscout,
ABI — в `abi/vibevibe.json` (VibePassMarket, VibeLoungeRegistry, VibeShop,
VibeItems, VibeFuelStaking, VibeFuelRewards). Реализован EIP-712 флоу
`buy`/`sell` на VibePassMarket (подпись TradeIntent ключом `tradeSigner` из
`vibevibe.signer_key` / env `FARMER_VIBE_SIGNER_KEY`) и `registerLounge`
(только `REGISTRAR_ROLE` платформы). Без ключа подписи buy/sell пропускаются
адаптивными весами; всегда работают `transfer` / `claimRewards` / `stake`:

```powershell
python auto.py --config config_vibevibe.yaml --wallets 50 --cycles 5
```

Базовый шаблон для своего конфига — [config.example.yaml](config.example.yaml).

### Авто-режим (CLI)

```powershell
python auto.py                                # 50 кошельков, циклы бесконечно
python auto.py --wallets 200 --cycles 5
python auto.py --skip-faucet --skip-farm
python auto.py --schedule 6                   # пайплайн каждые 6 часов
python auto.py --history 20 / --doctor / --version
```

Полная таблица флагов, фазы пайплайна и поведение крана — в
[§2.2 DOCUMENTATION.md](DOCUMENTATION.md).

### Портал (веб + Telegram)

```powershell
pip install -r requirements-portal.txt
python -m portal                # aiohttp + API + Telegram-бот (env-конфигурация ниже)
```

Веб-дашборд и Telegram-бот: мониторинг метрик (циклы, действия, ошибки,
health, количество кошельков), старт/стоп/пауза фермы, история циклов,
топ кошельков и **экспорт всех кошельков с ключами** (CSV/JSON) — в вебе
кнопкой «Скачать кошельки», в боте `/export` или кнопкой меню.
Бот также умеет `/cmd` (готовая команда), `/cfg` (готовый YAML файлом),
`/check` (проверка YAML) и `/ref` (справочник) — в стиле сайта.

Локальная веб-панель (FastAPI + WebSocket-лог, без Telegram): пункт `G`
в меню `main.py` → http://127.0.0.1:8080 (`core/web_gui.py`).
Основной портал (aiohttp + Telegram-бот + Mini App) — тот же `127.0.0.1:8080`
(`portal/`): запускайте только одну панель за раз.

Секреты — только через env (`PORTAL_SECRET`, `GOOGLE_CLIENT_ID/SECRET`,
`TELEGRAM_BOT_TOKEN`, `FARMER_MASTER_KEY`, `WALLETS_KEY` и др.) — та же таблица env в
[§4.1 DOCUMENTATION.md](DOCUMENTATION.md).

### Сборка exe

```powershell
pip install -r requirements.txt
pyinstaller harvest.spec --noconfirm --clean   # либо build.bat
Unblock-File dist\harvest\harvest.exe
```

## Структура

```
auto.py        — CLI-автомат (4 фазы пайплайна)
main.py        — интерактивное меню (Rich)
config_*.yaml  — конфиги сетей (robinhood/flop/arc/vibevibe) + config.simple.yaml,
                 config.example.yaml — шаблон
core/          — ядро: config, network (мульти-RPC/EMA/1559-фоллбэк),
                 pool/farmer/workpool, vibevibe (ABI), crypto, database,
                 web_gui (локальная FastAPI-панель)
portal/        — aiohttp-портал + API + Telegram-бот + Mini App (+networks.py)
tests/         — pytest-набор (57 файлов; test_load_opt.py — нагрузка, opt-in)
docs/          — сайт (ssrjkk.github.io/harvest) + screenshots/
tools/         — verify_site.py (проверка сайта), shoot_site.py (скриншоты)
abi/           — ABI-контракты (vibevibe.json, flop.json, arc.json)
harvest.spec   — спецификация PyInstaller (сборка: build.bat)
publish_dash.py — публикация state.json + шифрованного wallets.enc.json поля в ветку gh-pages
```

Архитектура каждого модуля — [§3 DOCUMENTATION.md](DOCUMENTATION.md).

## Безопасность

Секреты шифруются (AES-256-GCM), запись seed-данных без master-ключа
запрещена (fail-closed), краснение RPC-URL, license-hub гейт,
portal default-deny + fail-closed. Подробно — [§6 DOCUMENTATION.md](DOCUMENTATION.md).

## Тесты

```powershell
python -m pytest tests -q
```

2034 теста, 67 файлов — полный зелёный прогон (включая контуры `wait_for_receipt`:
EMA-бюджет поллинга, RPC-failover при серии ошибок, деградация частоты опроса,
`None` по истечении бюджета без отката nonce, кросс-чек статуса на вторичной ноде).
CI-гейты: 100% portal, 100% core.

Нагрузка — opt-in, в обычном CI пропускается:

```powershell
$env:HARVEST_LOAD=1; python -m pytest tests/test_load_opt.py -q
```

## Развёртывание (VPS / Docker)

Готовый deploy-кит для Linux-сервера (Docker + compose, Telegram-бот на long-polling
без публичного URL, Caddy для домена+HTTPS позже) — в каталоге [`deploy/`](deploy/README.md):
`.env`-шаблон секретов, healthcheck, volume для данных, инструкция по добавлению бота
и переходу на публичный HTTPS.

## Автор и поддержка

Проект создан и поддерживается **ssrjkk**.

- Telegram-бот автора: [@ssrjkk_bot](https://t.me/ssrjkk_bot)
- Telegram-бот фермы: [@harvest_testnet_bot](https://t.me/harvest_testnet_bot)
- Репозиторий: [github.com/ssrjkk/harvest](https://github.com/ssrjkk/harvest)

Лицензия — [MIT](LICENSE), Copyright © 2026 ssrjkk.


## Installation

```bash
git clone https://github.com/ssrjkk/harvest.git
cd harvest
pip install -r requirements.txt
```

## Usage

```bash
python main.py
```
