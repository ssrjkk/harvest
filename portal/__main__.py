"""Запуск портала: python -m portal

Поднимает aiohttp-сервер (веб + API) и, при наличии токена, Telegram-бота.
Доступ: Google-логин или пароль = мастер-ключ (FARMER_MASTER_KEY).
"""

from __future__ import annotations

import asyncio
import functools
import logging
import os
import signal

from aiohttp import web

from portal.api import create_app
from portal.config import PortalConfig, validate_master_key
from portal.farm import FarmDaemon

logger = logging.getLogger("portal")


async def _acute_alert(cfg: PortalConfig, text: str) -> None:
    """Пуш-алерт владельцу через Telegram-бота (колбэк демона фермы)."""
    from portal.bot_telegram import notify_owner

    await notify_owner(cfg, text)


def _is_loopback_address(host: str) -> bool:
    """127.0.0.0/8, ::1, localhost — петлевые адреса."""
    try:
        import ipaddress

        return ipaddress.ip_address(host.strip()).is_loopback
    except ValueError:
        return host.strip().lower() in {"localhost", "::1", "[::1]"}


def _log_bot_crash(bot_task: asyncio.Task, cfg: PortalConfig | None = None) -> None:
    """Телеграм-бот упал — пишем крупно в журнал, но портал НЕ роняем.

    Тихий выход бота оставил бы владельца без пульта при работающем сервере.
    Владельцу дополнительно шлётся пуш-алерт (если бот/токен настроены).
    """
    if not bot_task.done():
        return
    try:
        bot_task.result()
    except asyncio.CancelledError:
        pass
    except Exception as exc:  # noqa: BLE001
        logger.exception("Telegram-бот остановился с ошибкой — веб/API продолжают работать")
        if cfg is not None and cfg.telegram_token:
            try:
                from portal.bot_telegram import notify_owner

                asyncio.create_task(
                    notify_owner(
                        cfg,
                        f"⚠️ Telegram-бот упал: {type(exc).__name__}: {str(exc)[:200]}\n"
                        "Веб/API продолжают работать. /doctor — диагностика",
                    )
                )
            except Exception:  # noqa: BLE001
                logger.warning("Не удалось сформировать алерт о сбое бота", exc_info=True)


async def _graceful_shutdown(runner: web.AppRunner, daemon: FarmDaemon, bot_task: asyncio.Task | None) -> None:
    logger.info("Останавливаю портал…")
    if bot_task is not None:
        bot_task.cancel()
        await asyncio.gather(bot_task, return_exceptions=True)
    await runner.cleanup()
    await daemon.close()
    logger.info("Портал остановлен")


async def main() -> None:
    # basicConfig ДО первых logger-вызовов: предупреждения о конфигурации
    # (public_base_url, open_access, HTTP-пароль...) обязаны попасть в журнал.
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    cfg = PortalConfig()
    if not cfg.ready:
        raise SystemExit("PORTAL_SECRET не задан — без него портал не стартует.")
    if len(cfg.secret) < 16:
        raise SystemExit("PORTAL_SECRET слишком короткий (минимум 16 символов).")
    if not _is_loopback_address(cfg.host) and not (cfg.google_enabled() or cfg.master_key or cfg.telegram_token):
        # Fail-closed: портал, открытый в сеть, обязан иметь хоть один способ
        # входа (Google/пароль/Telegram). Иначе это «открытый» инстанс с
        # доступными default-deny-страницами, но без единого метода логина.
        raise SystemExit(
            f"PORTAL_HOST={cfg.host!r}: портал открывается в сеть, но не настроен ни один "
            "способ входа. Задайте GOOGLE_CLIENT_ID/GOOGLE_CLIENT_SECRET, "
            "FARMER_MASTER_KEY или TELEGRAM_BOT_TOKEN — либо bind на 127.0.0.1."
        )
    if (cfg.google_enabled() or cfg.master_key) and not cfg.public_base_url:
        logger.warning("public_base_url не задан: вход/Google-re-direct и Mini App будут работать не полностью")
    if cfg.open_access:
        logger.warning("PORTAL_OPEN_ACCESS включён: любой Google/TG-аккаунт получит доступ к ферме!")
    if cfg.allow_insecure_password:
        logger.warning("PORTAL_ALLOW_PASSWORD_HTTP включён: мастер-ключ принимается поверх HTTP!")
    if cfg.password_login and cfg.master_key:
        _mkey = validate_master_key(cfg.master_key)
        if not _mkey.ok:
            raise SystemExit(_mkey.reason)
        if _mkey.reason:
            logger.warning("Мастер-ключ: %s", _mkey.reason)
    if os.name == "nt" and os.environ.get("PORTAL_SECRET") is None:
        logger.warning(
            "portal_secret.key создан на Windows: права 0600 здесь не работают — "
            "убедитесь, что рабочий каталог не в синхронизируемой папке"
        )

    # bind может прийти из portal_config.yaml (host:) — всегда логируем факт,
    # чтобы молчаливое открытие портала в сеть (0.0.0.0) не прошло незамеченным.
    logger.info(
        "Портал: bind %s:%s, куки только-HTTPS=%s, trust_proxy=%s, вход=%s",
        cfg.host,
        cfg.port,
        cfg.cookie_secure,
        cfg.trust_proxy,
        "google" if cfg.google_enabled() else ("password" if cfg.master_key else "none"),
    )

    if cfg.google_enabled():
        redirect = cfg.google_redirect_uri or f"{cfg.public_base_url}/auth/google/callback"
        logger.info("Google OAuth: redirect_uri=%s, allow=%s", redirect, cfg.google_allow_emails or "any")
    elif cfg.master_key:
        logger.info("Логин: мастер-ключ (пароль)")
    else:
        logger.warning("Нет ни Google, ни пароля — вход в веб не настроен")

    daemon = FarmDaemon(
        cfg.farm_config,
        cfg.db_path,
        cfg.master_key or None,
        alert=functools.partial(_acute_alert, cfg),
    )
    try:
        await daemon.connect()
    except Exception as exc:  # noqa: BLE001
        raise SystemExit(f"Не удалось подключить ядро фермы: {exc}") from None
    logger.info("Ядро фермы подключено: %s", cfg.farm_config)

    app = create_app(cfg, daemon)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, cfg.host, cfg.port)
    await site.start()
    logger.info("Веб/API: http://%s:%s", cfg.host, cfg.port)

    bot_task: asyncio.Task | None = None
    if cfg.telegram_enabled():
        from portal.bot_telegram import run_bot

        bot_task = asyncio.create_task(run_bot(cfg, daemon))
        bot_task.add_done_callback(lambda t: _log_bot_crash(t, cfg))
    else:
        logger.warning("TELEGRAM_BOT_TOKEN не задан — бот выключен")

    keep_alive = asyncio.Event()

    def _on_signal(_signum=None, _frame=None) -> None:
        keep_alive.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            asyncio.get_running_loop().add_signal_handler(sig, _on_signal)
        except (NotImplementedError, RuntimeError):
            pass

    await keep_alive.wait()
    await _graceful_shutdown(runner, daemon, bot_task)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
