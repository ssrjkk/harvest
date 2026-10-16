# HARVEST PORTAL — универсальный контейнер (HF Spaces / VPS).
# HF Spaces: Dockerfile в корне, app_port из README (8080).
# Сборка вручную: docker build -t harvest-portal .
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PYTHONPATH=/app \
    PORTAL_HOST=0.0.0.0 \
    PORTAL_PORT=8080 \
    PORTAL_TRUST_PROXY=1 \
    PORTAL_DB=/app/data/farming_state.db \
    PORTAL_FARM_CONFIG=/app/config_vibevibe.yaml \
    PORTAL_LINKS=/app/portal/links.json

WORKDIR /app

# Зависимости отдельным слоем (кэшируется при правках кода).
COPY requirements.txt requirements-portal.txt ./
RUN pip install --no-cache-dir -r requirements.txt -r requirements-portal.txt

# Код портала + ядро + ABI + шаблоны конфигов.
COPY core/ core/
COPY portal/ portal/
COPY abi/ abi/
COPY config_robinhood.yaml config_flop.yaml config_arc.yaml config_vibevibe.yaml config.simple.yaml config.example.yaml ./

# Данные (state.json, master.key, логи, SQLite) — в /app/data.
RUN mkdir -p /app/data && ln -s /app/abi /app/data/abi

# HF Spaces запускает контейнер от UID 1000: создаём такого пользователя
# и отдаём ему рабочие каталоги (иначе нет прав на запись).
RUN (useradd -m -u 1000 app || true) && chown -R 1000:1000 /app
USER 1000

EXPOSE 8080
WORKDIR /app/data

CMD ["python", "-m", "portal"]
