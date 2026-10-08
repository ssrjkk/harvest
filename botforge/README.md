# HARVEST — автономный бот-пульт для BotForge

Бот работает **24/7 в облаке BotForge** (бесплатно, без твоего ПК), показывает
статус фермы и **кошельки с ключами и сид-фразами из облачной БД MotherDuck**.

## Деплой (5 минут)

1. Зарегистрируйся на **botforge.lol** через Discord, создай бота `harvest` (Python).
2. В **Files** загрузи архив `harvest-botforge.zip` (в нём `main.py` + `requirements.txt`).
   Либо создай `main.py` вручную и вставь код из `bot.py`.
3. **Settings** → Start Command: `python main.py` → Save.
4. В **Secrets** добавь переменные:
   - `BOT_TOKEN` = токен от @BotFather
   - `MOTHERDUCK_TOKEN` = токен MotherDuck (чтобы кошельки были видны без ПК)
   - `STATE_URL` = `https://ssrjkk.github.io/harvest/state.json`
   - `TELEGRAM_ALLOW_IDS` = твой user_id (узнать: @userinfobot) — **обязательно!**
5. Нажми **Start**.

> ⚠️ **Default-deny:** без `TELEGRAM_ALLOW_IDS` бот отказывает всем. Это защита —
> `/wallets` отдаёт приватные ключи. Не оставляй доступ открытым.

## Команды

| Команда | Что делает |
|---|---|
| `/start` | меню |
| `/status` | статус фермы + прогресс-бар |
| `/wallets` | кошельки: адрес, приватный ключ и сид-фраза (скрыты спойлером, листание ◀ ▶) |
| `/help` | справка |

## Как это устроено

| Часть | Где живёт | Работает без ПК |
|---|---|---|
| Бот | BotForge (облако) | ✅ всегда |
| Сайт | GitHub Pages | ✅ всегда |
| Кошельки/метрики | MotherDuck (облако) | ✅ всегда (синхронизирует ПК) |
| Ферма (фарм) | твой ПК | ❌ пока ПК выключен |

Данные в облако пишет `sync_motherduck.py` (запускается с ПК вместе с
`publish_dash.py` каждые 5 минут). Бот читает их из MotherDuck.
