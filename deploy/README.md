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

## 1. Быстрый старт

```bash
# 1. Клонировать репозиторий на сервер
git clone https://github.com/ssrjkk/harvest.git
cd harvest/deploy

# 2. Секреты
cp .env.example .env
#    - FARMER_MASTER_KEY: сгенерируйте 64-hex:
#        python3 -c "import secrets; print(secrets.token_hex(32))"
#      Это ключ шифрования БД И мастер-пароль входа в веб.
#      НЕ пропускайте: при пустом ключе (и без Google/TG) портал не стартует —
#      это fail-closed защита от «открытого» инстанса.
#    - PORTAL_SECRET: если пусто — создастся сам в /app/data/portal_secret.key.

# 3. Запуск
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

## 2. Добавить Telegram-бота (позже)

```bash
# 1. Токен — у @BotFather; свой user_id — у @userinfobot
# 2. В deploy/.env заполнить:
#      TELEGRAM_BOT_TOKEN=123456:ABCDEF...
#      TELEGRAM_ALLOW_IDS=<ваш числовой user_id>  (default-deny: бот отвечает только им)
docker compose up -d --build   # перезапуск с ботом
```
Бот работает на long-polling и НЕ требует публичного URL. Текстовые команды: `/start`, `/help`, `/doctor`, `/history`. Остальные действия (`/stats`, `/stop`, `/pause`, `/resume`, `/links`, сеть, страница) — inline-кнопки под сообщениями бота.

Сторожевой монитор шлёт вам push при проблемах: «сеть деградировала», «ферма стоит», «всплеск ошибок», «ферма остановилась» (+ восстановление). Настройки:
`PORTAL_WATCH_INTERVAL_S` (по умолчанию 300 c) и `PORTAL_HEARTBEAT_HOURS` (периодический «пульс», 0 = выкл) в `deploy/.env`.

## 3. Переключение сети фермера

Сменить `PORTAL_FARM_CONFIG` в `docker-compose.yml`:
- `/app/config_robinhood.yaml` — Robinhood Testnet (46630), по умолчанию
- `/app/config_flop.yaml` — Flop Labs
- `/app/config_arc.yaml` — Arc
- либо примонтировать свой `config.yaml` volume'ом и указать на него.

После смены сети сделайте свежую БД: `docker compose down && docker volume rm harvest_portal-data` (кошельки создадутся при старте фермы).

## 4. Публичный веб + HTTPS (Caddy) — позже

По умолчанию портал публикуется только на `127.0.0.1` и парольный вход разрешён по HTTP
(SSH-tunnel / localhost). Для публичного домена:

```bash
# install "deploy/caddy/Caddyfile.example" -> "deploy/caddy/Caddyfile", вписав домен
docker compose -f docker-compose.yml -f docker-compose.caddy.yml up -d --build
```

При этом:
- Caddy выпустит LetsEncrypt-сертификат и проксирует на портал;
- в `.env` установите `PORTAL_ALLOW_PASSWORD_HTTP=` (пусто) и раскомментируйте
  `PORTAL_TRUST_PROXY=1` (продакшен-режим: пароль только по HTTPS, cookies secure);
- добавьте `GOOGLE_CLIENT_ID/SECRET` для OAuth-входа, если нужен.
- Mini App (дашборд) живёт в портале: `/static/index.html` по `PORTAL_BASE_URL=https://ваш-домен`.

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