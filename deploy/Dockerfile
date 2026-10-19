# harvest PORTAL — контейнер для Linux VPS.
# Сборка: docker build -f deploy/Dockerfile -t harvest-portal:latest .
# Python зафиксирован на 3.12 (код и ruff-target — py312; Dependabot-обновления
# мажора только после проверки зависимостей).
FROM python:3.14-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PYTHONPATH=/app \
    PORTAL_HOST=0.0.0.0 \
    PORTAL_PORT=8080 \
    PORTAL_TRUST_PROXY=0 \
    PORTAL_DB=/app/data/farming_state.db \
    PORTAL_FARM_CONFIG=/app/config_vibevibe.yaml \
    PORTAL_LINKS=/app/portal/links.json

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

# Данные (БД, ключи, state.json, логи) — в именованный volume /app/data.
# Каталог создаём и отдаём пользователю ДО USER, иначе volume примонтируется
# root'ом и портал не сможет писать.
RUN mkdir -p /app/data && (useradd -m -u 1000 app || true) && chown -R 1000:1000 /app
# Непривилегированный пользователь: процесс портала не должен быть root.
USER 1000

EXPOSE 8080

# Рабочий каталог = /app/data: state.json/logs/master.key по умолчанию
# ложатся в персистентный volume (сам код лежит в /app).
WORKDIR /app/data
# ABI резолвится через core.utils.resolve_bundled (/app при volume поверх
# /app/data), симлинк оставлен для старых сборок.
RUN mkdir -p /app/data && ln -s /app/abi /app/data/abi || true

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD-SHELL "python -c \"import os,urllib.request,sys;sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:'+os.environ.get('PORTAL_PORT','8080')+'/healthz',timeout=4).status==200 else 1)\""

CMD ["python", "-m", "portal"]