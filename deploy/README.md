# Deploy HARVEST Portal (Linux VPS / Docker)

Портал = веб-дашборд + API + Telegram-бот (aiogram, long-polling) + Mini App.

## 0. Подготовка сервера (с чистого VPS)

```bash
# 1. Обновить систему
apt update && apt upgrade -y

# 2. Docker Engine (официальный репозиторий)
curl -fsSL https://get.docker.com | sh
systemctl enable --now docker

# 3. Docker Compose (plugin идёт вместе с get.docker; проверить)
docker compose version

# 4. Файрвол: SSH + ничего лишнего. Порт 8080 НЕ открываем наружу —
#    доступ к дашборду через SSH-tunnel или Caddy позже.
ufw allow OpenSSH
ufw enable
ufw status            # 22 разрешён, всё остальное закрыто

# 5. Swap (рекомендую для VPS до 4GB RAM): 2GB
fallocate -l 2G /swapfile && chmod 600 /swapfile
mkswap /swapfile && swapon /swapfile
echo '/swapfile none swap sw 0 0' >> /etc/fstab

# 6. Резервный каталог для бэкапов
mkdir -p /root/backups
```

Деплой кода: репозиторий приватный — проще не `git clone`, а rsync с твоей машины:

```bash
# локально (Windows: убедись, что rsync есть или используй WinSCP для папки deploy)
rsync -avz --exclude='.git' deploy/ user@server:/opt/harvest/deploy/
rsync -avz --exclude='.git' core/ portal/ abi/ requirements.txt \
  requirements-portal.txt config_*.yaml config.example.yaml user@server:/opt/harvest/
# либо один каталог: rsync -avz --exclude='.git' --exclude='tests' ./ user@server:/opt/harvest/
```

Дальше — раздел 1 (секреты и запуск), но с абсолютным путём:

```bash
cd /opt/harvest/deploy
cp .env.example .env   # заполнить, см. ниже
docker compose up -d --build
```

## 1. Быстрый старт (на сервере)

```bash
# 1. Скопировать проект на сервер (rsync, см. раздел 0; или git clone)
# 2. Секреты — уже готовы в deploy/.env (мастер-ключ, PORTAL_SECRET, токен бота).
#    Если нужны свои — перегенерируйте:
#        python3 -c "import secrets; print(secrets.token_hex(32))"   # FARMER_MASTER_KEY
#    FARMER_MASTER_KEY = ключ шифрования БД И мастер-пароль входа в веб.
#    Пусто = парольный вход выключен (fail-closed: только Google/TG).

# 3. Локальный запуск (только SSH-tunnel; публичный HTTPS — см. раздел 4)
cd /opt/harvest/deploy
docker compose up -d --build
docker compose ps            # статус
docker compose logs -f       # логи
```

Проверка:
```bash
curl http://127.0.0.1:8080/healthz   # -> {"status": "ok", "ready": true}
```
Доступ к дашборду с локальной машины — SSH-tunnel:
```bash
ssh -L 8080:127.0.0.1:8080 user@host   # затем открой http://localhost:8080
```

Для **публичного доступа** перейдите сразу к разделу 4 (домен + Caddy + HTTPS).

## 2. Telegram-бот

Токен и ваш user_id — в `deploy/.env` (заполните своими значениями, не коммитьте!):
```bash
# deploy/.env
TELEGRAM_BOT_TOKEN=<свой токен от @BotFather>
TELEGRAM_ALLOW_IDS=<ваш числовой user_id>   # default-deny: бот отвечает только вам
```
После `docker compose up -d --build` бот поднимется автоматически.

Бот работает на long-polling и НЕ требует публичного URL. Команды: `/start`, `/help`,
`/doctor`, `/history`, `/status` (развёрнутый статус), `/monitor` (живой мониторинг
с алертами), `/export` (кошельки). Остальные действия (`/stats`, `/stop`, `/pause`,
`/resume`, `/links`, сеть, страница, мониторинг, экспорт) — inline-кнопки под
сообщениями бота.

Сторожевой монитор шлёт push при проблемах: «сеть деградировала», «ферма стоит»,
«всплеск ошибок», «ферма остановилась» (+ восстановление). Настройки:
`PORTAL_WATCH_INTERVAL_S` (по умолчанию 300 c) и `PORTAL_HEARTBEAT_HOURS`
(периодический «пульс», 0 = выкл) в `deploy/.env`.

## 3. Переключение сети фермера

Сменить `FARM_CONFIG` в `deploy/.env` (docker-compose.yml читает его через `PORTAL_FARM_CONFIG`):
- `config_vibevibe.yaml` — vibe/vibe (Robinhood testnet), **по умолчанию**
- `config_robinhood.yaml` — Robinhood Testnet (46630)
- `config_flop.yaml` — Flop Labs
- `config_arc.yaml` — Arc
- `config.simple.yaml` — только трансферы, отдельная БД

```bash
# deploy/.env
FARM_CONFIG=/app/config_robinhood.yaml
docker compose up -d --build
```

> При переключении сети сделайте свежую БД, если в ней кошельки другой сети:
> `docker compose down && docker volume rm harvest_portal-data` (кошельки создадутся при старте фермы).

## 4. Публичный веб + HTTPS (Caddy)

Готовый `.env` уже лежит в `deploy/.env` (мастер-ключ, PORTAL_SECRET, токен бота).
Осталось только подставить **домен**:

1. На DNS создайте A-запись `ваш-домен` → IP сервера (порты 80/443 открыты в firewall).
2. В `deploy/caddy/Caddyfile` замените `your-domain.com` на реальный домен.
3. В `deploy/.env` замените `PORTAL_BASE_URL=https://harvest.example.com` на ваш домен.
4. Запуск с Caddy:

```bash
cd /opt/harvest/deploy
docker compose -f docker-compose.yml -f docker-compose.caddy.yml up -d --build
```

Caddy автоматически выпустит LetsEncrypt-сертификат и проксирует HTTPS на портал.
В `.env` уже выставлено: `PORTAL_ALLOW_PASSWORD_HTTP=` (пусто) и `PORTAL_TRUST_PROXY=1`
(пароль только по HTTPS, cookies secure, корректное определение HTTPS за прокси).

Проверка:
```bash
curl https://ваш-домен/healthz          # {"status":"ok","ready":true}
# открыть https://ваш-домен  → вход мастер-ключом из deploy/.env
```

Mini App (дашборд в Telegram) открывается по `PORTAL_BASE_URL` — после деплоя
кнопка «🚀 Открыть Mini App» в боте заработает сразу.

## 4.5. Бесплатно без карты — Hugging Face Spaces (Docker)

Портал слушает **8080**. HF Space даёт публичный HTTPS без карты. Файлы для HF —
в [`huggingface/`](huggingface/): `README.md` (карточка Space) и `deploy.ps1`.

1. huggingface.co → **New Space** → SDK **Docker (Blank)**, CPU basic (free), Public.
2. Клонировать Space и залить код:
   ```powershell
   git clone https://huggingface.co/spaces/ВАШ_НИК/harvest C:\hf-harvest
   powershell -ExecutionPolicy Bypass -File deploy\huggingface\deploy.ps1 -SpaceDir C:\hf-harvest
   ```
   (пароль при push — write-токен: Settings → Access Tokens → Write)
3. **Settings → Variables and secrets** (имена читает код):
   - `FARMER_MASTER_KEY` — 64-hex (ключ шифрования БД И пароль веб-входа);
   - `PORTAL_SECRET` — ≥16 символов;
   - `PORTAL_DB` — DSN внешней PostgreSQL (`postgres://user:pass@host:5432/db`),
     т.к. диск Space **эфемерный** — SQLite не сохранится;
   - `TELEGRAM_BOT_TOKEN` + `TELEGRAM_ALLOW_IDS` — чтобы включить бота;
   - `PORTAL_BASE_URL` — `https://ВАШ_НИК-harvest.hf.space` (для Mini App).
4. Открыть `https://ВАШ_НИК-harvest.hf.space` (HTTPS уже есть).

**Держать активным:** Space засыпает после ~48 ч без запросов. Workflow
[`.github/workflows/keepalive.yml`](../.github/workflows/keepalive.yml) пингует
`/healthz` каждые 6 ч (замените URL на свой). Первый запрос после сна — 30–60 с.

> Кошельки: HF-контейнер их не генерирует. Создайте их локально в ту же
> PostgreSQL (запустите портал с тем же `PORTAL_DB`) или через `auto.py`.

## 5. Обновление

```bash
cd harvest && git pull
cd deploy && docker compose up -d --build
```

Для приватного репозитория (без токена на сервере) — rsync:

```bash
rsync -avz --delete --exclude='.git' --exclude='data' ./ user@server:/opt/harvest/
ssh user@server 'cd /opt/harvest/deploy && docker compose up -d --build'
```

Данные в volume `harvest_portal-data` не трогаются (в образе `/app/data` пустой,
volume подмонтирован поверх) — обновление безопасно.

## Операционные заметки

- `restart: unless-stopped` + healthcheck (`/healthz`) — контейнер самовосстанавливается.
- Данные (БД, `state.json`, `master.key`, логи, secret-файлы) — в volume `harvest_portal-data`.
- Бэкап: `docker run --rm -v harvest_portal-data:/data -v $PWD:/backup alpine tar czf /backup/portal-data-$(date +%F).tgz -C /data .`
- Логи в контейнере: `/app/data/logs/`.
- Кран `fake-useragent` один раз качает базу UA из интернета; для офлайн-серверов
  возможна задержка первого HTTP-ответа крана — это норм, крадущийся ретраит.