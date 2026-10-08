---
title: Harvest
emoji: 🌾
colorFrom: green
colorTo: blue
sdk: docker
app_port: 8080
pinned: false
---

# HARVEST — Telegram farm control panel

Веб-панель + Telegram-бот для управления фермером тестнет-сетей (EVM).
Автор: [@ssrjkk_bot](https://t.me/ssrjkk_bot).

## Secrets (Settings → Variables and secrets)

Задайте в Space (значения — ваши, не коммитьте):

| Name | Обязательно | Значение |
|---|---|---|
| `FARMER_MASTER_KEY` | да | 64-hex: ключ шифрования БД **и** пароль входа в веб |
| `PORTAL_SECRET` | да | произвольная строка ≥16 символов (подпись сессий) |
| `PORTAL_DB` | да | DSN PostgreSQL: `postgres://user:pass@host:5432/db` (диск HF эфемерный — SQLite не сохранится) |
| `TELEGRAM_BOT_TOKEN` | опц. | токен от @BotFather (включает бота) |
| `TELEGRAM_ALLOW_IDS` | опц. | ваш Telegram user_id (default-deny) |
| `PORTAL_FARM_CONFIG` | опц. | `/app/config_vibevibe.yaml` (по умолчанию) |

`PORTAL_HOST=0.0.0.0`, `PORTAL_PORT=8080` уже заданы в Dockerfile.

## Заметки

- Бесплатный CPU Space засыпает после ~48 ч без запросов; `.github/workflows/keepalive.yml`
  пингует `/healthz` каждые 6 ч, чтобы держать его активным.
- Диск эфемерный: при перезапуске контейнера всё, кроме git, теряется — поэтому БД внешняя (PostgreSQL).
- Первый запрос после простоя может занять 30–60 с (пробуждение).
