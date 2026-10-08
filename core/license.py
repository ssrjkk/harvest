"""Лицензионный гейт: пароль с дистанционным управлением через license-hub.

Идея:
  Владелец публикует license.json в своём репозитории на GitHub (raw URL) и в
  любой момент может дистанционно:
    * сменить пароль     — пересчитать hash и обновить поле "pass";
    * отозвать все копии — выставить "status": "revoked";
    * задать срок        — поле "expires".

Пример license.json:
  {
    "app": "harvest",
    "v": 2,
    "status": "active",
    "pass": "<pbkdf2_sha256$210000$salt$hex — вместо устаревшего sha256(salt+пароль)>",
    "expires": null
  }

Клиент сверяет pbkdf2_sha256(deploy_salt, пароль) с полем "pass". Когда
владелец меняет файл на GitHub, все существующие клиенты при следующем
онлайн-запросе увидят новый hash и примут только новый пароль (активация
слетает "сами" — старый пароль больше не подходит). "revoked" убивает копии
сразу, в т.ч. офлайн через кэш активации.

Офлайн-режим:
  после успешной активации пишется подписанный (HMAC) кэш .license_cache,
  действующий grace_days суток. После этого без связи с hub доступ блокируется
  до новой активации.

Как владельцу получить hash нового пароля:
    python -m core.license setpass НОВЫЙ_ПАРОЛЬ [deploy_salt]

Почему PBKDF2 (а раньше был sha256 без итераций):
  голый sha256(salt+пароль) брутится офлайн за секунды, а deploy_salt по
  умолчанию одинаковый у всех — пригодны rainbow-таблицы. PBKDF2 с
  210_000 итерациями — медленный: минимум 2-3 тысячи попыток/сек на CPU,
  плюс стоимость растёт от соли. Ставьте СВОЙ случайный deploy_salt в
  config.yaml у каждого дистрибутива — иначе дефолт сводит половину выгоды.

Честное предупреждение:
  Это сдерживающий механизм, а НЕ защита от копирования: клиент — обычный
  Python-скрипт, и любой пользователь с исходниками может вырезать проверку.
  Гейт защищает от бесконтрольного использования без вашего согласия и даёт
  рычаг смены пароля/отзыва на дистанции.
"""

import asyncio
import hashlib
import hmac
import json
import logging
import os
import time
import urllib.parse
import urllib.request
from datetime import UTC, datetime
from pathlib import Path

logger = logging.getLogger(__name__)

_DEFAULTS = {
    "enabled": False,
    "deploy_url": "",
    "deploy_salt": "harvest-deploy-salt-v1",
    "grace_days": 7,
    "cache_file": ".license_cache",
    "timeout": 8,
}

# Итераций PBKDF2-SHA256 для поля "pass". На CPU — ~2000+ попыток/сек,
# это минимум 3 порядка дороже голого sha256. Собственная случайная
# deploy_salt в конфиге делает офлайн-перебор нецелесообразным.
_PBKDF2_ITERATIONS = 210_000
_HASH_PREFIX = "pbkdf2_sha256$"
# Верхняя граница тела ответа license-hub (1 МБ): защита от OOM на
# скомпрометированном/некорректном сервере.
_MAX_LICENSE_BODY = 1 << 20


class LicenseError(Exception):
    """Лицензия не пройдена: работа без активации невозможна."""


class LicenseManager:
    def __init__(self, config: dict | None = None) -> None:
        lc = (config or {}).get("license", {}) or {}
        self.enabled = bool(lc.get("enabled", _DEFAULTS["enabled"]))
        self.url = lc.get("deploy_url", _DEFAULTS["deploy_url"])
        self.salt = lc.get("deploy_salt", _DEFAULTS["deploy_salt"])
        self.grace_days = float(lc.get("grace_days", _DEFAULTS["grace_days"]))
        self.cache_file = lc.get("cache_file", _DEFAULTS["cache_file"])
        self.timeout = float(lc.get("timeout", _DEFAULTS["timeout"]))
        if self.enabled and self.salt == _DEFAULTS["deploy_salt"]:
            logger.warning(
                "deploy_salt не переопределён: используется общий дефолт. "
                "Задайте СВОЙ случайный salt в config.yaml (license.deploy_salt) "
                "и перевыпустите license.json — иначе офлайн-перебор упрощается общими таблицами."
            )

    # ---------- helpers ----------

    @staticmethod
    def make_hash(password: str, salt: str, iterations: int = _PBKDF2_ITERATIONS) -> str:
        """Hash пароля для публикации в license.json как "pass".

        Формат: pbkdf2_sha256$<итерации>$<salt>$<hex>. Старые позиции
        (голый sha256(salt+password)) принимаются _verify_password для
        обратной совместимости с ранее выпущенными лицензиями.
        """
        dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt.encode("utf-8"), iterations)
        return f"{_HASH_PREFIX}{iterations}${salt}${dk.hex()}"

    def _analyze(self, payload: dict | None) -> tuple[bool, str]:
        """Проверяет структуру/статус/срок лицензии. (ok, reason)."""
        if not isinstance(payload, dict) or not payload:
            return False, "повреждённый или пустой ответ сервера лицензий"
        if payload.get("app") not in (None, "harvest"):
            return False, "чужая лицензия (не обработчик для harvest)"
        v = payload.get("v")
        if v is not None:
            try:
                v_int = int(v)
            except (TypeError, ValueError):
                return False, "некорректная версия формата лицензии"
            if v_int < 2:
                return False, "устаревший формат лицензии (нужен v2+)"
        status = payload.get("status")
        if status == "revoked":
            return False, "лицензия отозвана владельцем"
        if status not in (None, "active"):
            return False, f"неизвестный статус лицензии: {status!r}"
        if not payload.get("pass"):
            return False, "в лицензии отсутствует поле pass"
        exp = payload.get("expires")
        if exp:
            try:
                d = exp.replace("Z", "+00:00")
                if datetime.now(UTC) > datetime.fromisoformat(d):
                    return False, "срок действия лицензии истёк"
            except ValueError:
                return False, "некорректная дата expires в лицензии"
        return True, "ok"

    def _verify_password(self, payload: dict, password: str) -> bool:
        expected = payload.get("pass", "")
        if not expected:
            return False
        if expected.startswith(_HASH_PREFIX):
            try:
                _prefix, iters_s, salt, digest = expected.split("$", 3)
                iters = int(iters_s)
                # Верхняя граница — защита от CPU-DoS: скомпрометированный
                # license-hub не должен заставлять клиента жечь CPU на 10M итераций.
                if not (10_000 <= iters <= 1_000_000):
                    return False
                dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt.encode("utf-8"), iters)
                return hmac.compare_digest(dk.hex(), digest)
            except (ValueError, AttributeError):
                return False
        # Легаси: sha256(salt + password) из старых license.json.
        legacy = hashlib.sha256((self.salt + password).encode("utf-8")).hexdigest()
        if not getattr(self, "_legacy_warned", False):
            self._legacy_warned = True
            logger.warning(
                "license.json использует устаревший sha256-хэш пароля (без итераций). "
                "Перевыпустите лицензию с pbkdf2 (python -m core.license setpass)"
            )
        return hmac.compare_digest(legacy, expected)

    # ---------- кэш активации ----------

    def _cache_write(self, payload: dict, pw_hash: str) -> None:
        stamp = {"payload": payload, "ts": time.time(), "pw": pw_hash}
        raw = json.dumps(stamp, ensure_ascii=False, sort_keys=True)
        # Подпись — HMAC на deploy_salt (публичное значение из конфига): защищает
        # только от случайной порчи кэша, не от подделки. Полная защита невозможна —
        # клиент исполняет локальный код (см. честное предупреждение в шапке модуля).
        sig = hmac.new(self.salt.encode(), raw.encode(), hashlib.sha256).hexdigest()
        cache_path = Path(self.cache_file)
        raw_out = json.dumps({"raw": raw, "sig": sig}, ensure_ascii=False)
        # Атомарная запись tmp->restrict->replace: crash между truncation и записью
        # не оставляет битый кэш, а права ограничиваем ДО подмены файла.
        tmp = cache_path.with_suffix(cache_path.suffix + ".tmp")
        try:
            tmp.write_text(raw_out, encoding="utf-8")
            from core.utils import restrict_file_permissions

            restrict_file_permissions(str(tmp))
            tmp.replace(cache_path)
        except Exception as e:
            logger.warning(f"Не удалось сохранить кэш активации: {e}")
            try:
                tmp.unlink()
            except OSError:
                pass
        # Кэш — не главный секрет (hash публичен в license.json), но ограничиваем
        # права на чтение: меньше следов для локальных пользователей машины.
        try:
            from core.utils import restrict_file_permissions

            restrict_file_permissions(str(cache_path))
        except Exception:  # noqa: BLE001
            pass
        logger.info("Активация сохранена (офлайн-кэш)")

    def _cache_read(self) -> dict | None:
        try:
            data = json.loads(Path(self.cache_file).read_text(encoding="utf-8"))
            raw, sig = data["raw"], data["sig"]
            expected = hmac.new(self.salt.encode(), raw.encode(), hashlib.sha256).hexdigest()
            if not hmac.compare_digest(sig, expected):
                logger.warning("Подпись кэша активации не совпала — игнорирую")
                return None
            return json.loads(raw)
        except Exception:
            return None

    # ---------- сетевая часть ----------

    def _fetch(self) -> dict | None:
        if not self.url:
            return None
        try:
            parts = urllib.parse.urlsplit(self.url)
        except Exception:
            return None
        scheme = (parts.scheme or "").lower()
        host = (parts.hostname or "").lower()
        if scheme != "https" and not (scheme == "http" and host in ("127.0.0.1", "localhost", "::1")):
            # Лицензионный статус нельзя передавать по открытому каналу: MITM
            # подменил бы revoked на active. Допускаем http только на loopback.
            logger.warning("license.deploy_url должен быть https (или http на loopback) — запрос к серверу не выполнен")
            return None
        try:
            req = urllib.request.Request(self.url, headers={"User-Agent": "harvest-license/2.0"})
            # Схема проверена выше (только https или http на loopback) — B310 неактуален.
            with urllib.request.urlopen(req, timeout=self.timeout) as r:  # nosec B310
                if r.status != 200:
                    return None
                data = r.read(_MAX_LICENSE_BODY)
                if len(data) >= _MAX_LICENSE_BODY:
                    logger.warning("license hub ответил слишком большим телом — игнорирую")
                    return None
                payload = json.loads(data.decode("utf-8"))
            return payload if isinstance(payload, dict) else None
        except Exception as e:
            logger.debug(f"license hub fetch error: {e}")
            return None

    # ---------- главная точка ----------

    async def require_activation(self, password: str | None = None) -> tuple[bool, str]:
        """Проверяет и активирует лицензию. Возвращает (ok, reason).

        password: пароль (из env LICENSE_PASSWORD или после ввода в CLI).
        Логика: онлайн-статус с hub всегда перекрывает локальный кэш; если
        владелец сменил пароль на hub'е, старая активация перестаёт быть
        «текущей» и нужно ввести новый пароль один раз.
        """
        if not self.enabled:
            return True, "ok"
        if not self.url:
            return (
                False,
                "license.deploy_url не задан — без сервера лицензий запуск закрыт",
            )

        payload = await asyncio.to_thread(self._fetch)
        if payload is not None:
            ok, reason = self._analyze(payload)
            if not ok:
                return False, reason
            cache = self._cache_read()
            if cache and hmac.compare_digest(cache.get("pw", ""), payload.get("pass", "")):
                # уже активированы текущим паролем hub'а — ок
                return True, "ok"
            if password is not None and self._verify_password(payload, password):
                self._cache_write(payload, payload.get("pass", ""))
                return True, "ok"
            return False, "требуется пароль активации"

        # Нет связи с hub — опираемся на подписанный кэш (grace-окно)
        cache = self._cache_read()
        if not cache:
            return False, "нет связи с сервером лицензий и нет кэша активации"
        ok, reason = self._analyze(cache["payload"])
        if not ok:
            return False, reason
        age = time.time() - cache.get("ts", 0)
        if age > self.grace_days * 86400:
            return (
                False,
                f"офлайн-кэш протух ({self.grace_days} дн.) — нужен доступ к серверу лицензий",
            )
        if cache.get("pw") and hmac.compare_digest(cache.get("pw", ""), cache["payload"].get("pass", "")):
            return True, "ok"
        if password is not None and self._verify_password(cache["payload"], password):
            return True, "ok"
        return False, "требуется пароль активации"


async def enforce_license_async(config: dict, interactive: bool = True, attempts: int = 3) -> LicenseManager:
    """Асинхронная принудительная проверка (для auto.py / main.py).

    Сначала пробует env LICENSE_PASSWORD, затем интерактивный ввод (getpass).
    Raises LicenseError при неудаче.
    """
    lm = LicenseManager(config)
    if not lm.enabled:
        return lm

    env_pw = os.environ.get("LICENSE_PASSWORD")
    if env_pw:
        ok, reason = await lm.require_activation(env_pw)
        if ok:
            return lm
        raise LicenseError(reason)

    if not interactive:
        ok, reason = await lm.require_activation()
        if not ok:
            raise LicenseError(reason)
        return lm

    import getpass

    from colorama import Fore, Style

    for attempt in range(attempts):
        try:
            pw = getpass.getpass("  Пароль активации: ").strip()
        except (EOFError, OSError):
            raise LicenseError("Нет пароля (неинтерактивный режим). Укажите LICENSE_PASSWORD env.") from None
        if not pw:
            print(f"  {Fore.YELLOW}Пустой пароль, попробуйте ещё раз{Style.RESET_ALL}")
            continue
        ok, reason = await lm.require_activation(pw)
        if ok:
            return lm
        left = attempts - attempt - 1
        tail = f" Осталось попыток: {left}." if left else ""
        print(f"  {Fore.RED}{reason}.{tail}{Style.RESET_ALL}")
    raise LicenseError("Активация не выполнена")


if __name__ == "__main__":
    import argparse

    p = argparse.ArgumentParser(
        prog="core.license",
        description="Служебные команды владельца лицензий HARVEST",
    )
    sub = p.add_subparsers(dest="cmd", required=True)
    sp = sub.add_parser("setpass", help="Вычислить hash пароля для license.json")
    sp.add_argument("password", help="Новый пароль")
    sp.add_argument(
        "salt",
        nargs="?",
        default=_DEFAULTS["deploy_salt"],
        help=(
            f"deploy_salt из config.yaml (по умолчанию {_DEFAULTS['deploy_salt']!r}). "
            "Рекомендую свой случайный salt у каждого дистрибутива."
        ),
    )
    args = p.parse_args()
    if args.cmd == "setpass":
        print(LicenseManager.make_hash(args.password, args.salt))
