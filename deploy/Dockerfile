# harvest PORTAL — контейнер для Linux VPS.
# Сборка: docker build -f deploy/Dockerfile -t harvest-portal:latest .
# Python зафиксирован на 3.12 (код и ruff-target — py312; Dependabot-обновления
# мажора только после проверки зависимостей).
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PYTHONPATH=/app

WORKDIR /app

# Зависимости отдельным слоем (кэшируется при правках кода).
COPY requirements.txt requirements-portal.txt ./
RUN pip install --no-cache-dir -r requirements.txt -r requirements-portal.txt
# Шрифты для PNG-карточки статуса (DejaVu: кириллица в карточке).
RUN apt-get update && apt-get install -y --no-install-recommends fonts-dejavu-core \
    && rm -rf /var/lib/apt/lists/*

# Код портала + ядро + ABI + шаблоны конфигов.
COPY core/ core/
COPY portal/ portal/
COPY abi/ abi/
COPY config_robinhood.yaml config_flop.yaml config_arc.yaml config_vibevibe.yaml config.simple.yaml config.example.yaml ./

# Данные (БД, ключи, state.json, логи) — в именованный volume /app/data
RUN mkdir -p /app/data

EXPOSE 8080

# Рабочий каталог = /app/data: state.json/logs/master.key по умолчанию
# ложатся в персистентный volume (сам код лежит в /app).
WORKDIR /app/data
# ABI (и любые бандл-ресурсы) резолвятся относительно рабочего каталога:
# симлинк связывает /app/data/abi с /app/abi, где лежат реальные файлы.
RUN mkdir -p /app/data && ln -s /app/abi /app/data/abi

CMD ["python", "-m", "portal"]