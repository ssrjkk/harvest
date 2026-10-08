"""Догоняем покрытие до 100%: portal/config.py, portal/__main__.py, portal/bot_telegram.py.

Только новые тесты (прод-код и существующие tests/test_portal_*.py не трогаем).
Дублируют стиль test_cov_portal_api_auth_farm.py, но добивают ранее не покрытые
ветки config/__main__/bot_telegram.
"""

import asyncio
import contextlib
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import portal.__main__ as pm
from portal import config
from portal.__main__ import _graceful_shutdown
from portal.__main__ import main as portal_main
from portal.config import PortalConfig, load_links

try:
    from portal import bot_telegram

    HAS_BOT = True
except Exception:  # noqa: BLE001
    bot_telegram = None
    HAS_BOT = False


_MAIN_FILE = Path(__file__).resolve().parent.parent / "portal" / "__main__.py"


def _async_result(result=None, *, exc=None):
    async def _fn(*args, **kwargs):
        if exc is not None:
            raise exc
        return result

    return _fn


@contextlib.contextmanager
def _env(**kwargs):
    keys = list(kwargs)
    prev = {k: os.environ.get(k) for k in keys}
    try:
        for k, v in kwargs.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        yield
    finally:
        for k in keys:
            if prev[k] is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = prev[k]


def _make_cfg() -> PortalConfig:
    prev = {
        k: os.environ.get(k)
        for k in ("PORTAL_SECRET", "FARMER_MASTER_KEY", "PORTAL_COOKIE_SECURE", "PORTAL_TRUST_PROXY")
    }
    os.environ["PORTAL_SECRET"] = "s" * 32
    os.environ["FARMER_MASTER_KEY"] = "k" * 64
    os.environ.pop("PORTAL_COOKIE_SECURE", None)
    os.environ.pop("PORTAL_TRUST_PROXY", None)
    cfg = PortalConfig()
    cfg.public_base_url = ""
    cfg.allow_insecure_password = False
    cfg.cookie_secure = False
    cfg.password_login = True
    cfg.trust_proxy = False
    for k, v in prev.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v
    return cfg


class _ImmediateEvent:
    """keep_alive.wait() в main() должен вернуться мгновенно."""

    def set(self) -> None:
        pass

    async def wait(self) -> bool:
        return False


class TestConfig(unittest.TestCase):
    def test_env_flag_truthy_values(self):
        with _env(PORTAL_TRUST_PROXY="1"):
            self.assertTrue(config._env_flag("PORTAL_TRUST_PROXY"))
        with _env(PORTAL_TRUST_PROXY="yes"):
            self.assertTrue(config._env_flag("PORTAL_TRUST_PROXY"))
        with _env(PORTAL_TRUST_PROXY="off"):
            self.assertFalse(config._env_flag("PORTAL_TRUST_PROXY"))

    def test_resolve_secret_prefers_existing_file(self):
        content = "this-is-a-long-enough-secret-value"
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "secret.key"
            p.write_text(content, encoding="utf-8")
            with _env(PORTAL_SECRET=None, PORTAL_SECRET_FILE=str(p)):
                self.assertEqual(config._resolve_secret(), content)

    def test_resolve_secret_generates_and_writes_when_missing(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "new.key"
            with _env(PORTAL_SECRET=None, PORTAL_SECRET_FILE=str(p)):
                value = config._resolve_secret()
            self.assertGreaterEqual(len(value), 16)
            self.assertTrue(p.exists())
            self.assertIn(value, p.read_text(encoding="utf-8"))

    def test_resolve_secret_regenerates_short_file(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "short.key"
            p.write_text("abc", encoding="utf-8")
            with _env(PORTAL_SECRET=None, PORTAL_SECRET_FILE=str(p)):
                value = config._resolve_secret()
            self.assertGreaterEqual(len(value), 16)
            self.assertIn(value, p.read_text(encoding="utf-8"))

    def test_resolve_secret_read_error_returns_empty(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "broken.key"
            p.write_text("whatever", encoding="utf-8")
            with _env(PORTAL_SECRET=None, PORTAL_SECRET_FILE=str(p)):
                with mock.patch("builtins.open", side_effect=OSError("read boom")), mock.patch.object(
                    config.os, "open", side_effect=OSError("write boom")
                ):
                    self.assertEqual(config._resolve_secret(), "")

    def test_load_yaml_overrides_skips_missing_file(self):
        with _env(PORTAL_SECRET="s" * 32, PORTAL_HOST=None, PORTAL_CONFIG="definitely_missing_config.yaml"):
            cfg = PortalConfig()
        self.assertEqual(cfg.host, "127.0.0.1")

    def test_load_yaml_overrides_applies_values(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "portal_config.yaml"
            p.write_text(
                json.dumps(
                    {
                        "host": "0.0.0.0",
                        "port": 9999,
                        "public_base_url": "https://app.example",
                        "db": "custom.db",
                        "farm_config": "config_arc.yaml",
                        "links": "portal/links2.json",
                    }
                ),
                encoding="utf-8",
            )
            with _env(PORTAL_SECRET="s" * 32, PORTAL_HOST=None, PORTAL_CONFIG=str(p)):
                cfg = PortalConfig()
            self.assertEqual(cfg.host, "0.0.0.0")
            self.assertEqual(cfg.port, 9999)
            self.assertEqual(cfg.public_base_url, "https://app.example")
            self.assertEqual(cfg.db_path, "custom.db")
            self.assertEqual(cfg.farm_config, "config_arc.yaml")
            self.assertEqual(cfg.links_path, "portal/links2.json")

    def test_load_yaml_overrides_respects_explicit_env(self):
        # env (PORTAL_DB/PORTAL_FARM_CONFIG/PORTAL_LINKS) имеет приоритет над yaml.
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "portal_config.yaml"
            p.write_text(
                json.dumps({"db": "yaml.db", "farm_config": "yaml.yaml", "links": "yaml_links.json"}),
                encoding="utf-8",
            )
            with _env(
                PORTAL_SECRET="s" * 32,
                PORTAL_HOST=None,
                PORTAL_CONFIG=str(p),
                PORTAL_DB="env.db",
                PORTAL_FARM_CONFIG="env.yaml",
                PORTAL_LINKS="env_links.json",
            ):
                cfg = PortalConfig()
            self.assertEqual(cfg.db_path, "env.db")
            self.assertEqual(cfg.farm_config, "env.yaml")
            self.assertEqual(cfg.links_path, "env_links.json")

    def test_telegram_enabled_true(self):
        cfg = _make_cfg()
        cfg.telegram_token = "wow-token"
        self.assertTrue(cfg.telegram_enabled())

    def test_open_access_allows_any_google_email(self):
        cfg = _make_cfg()
        cfg.open_access = True
        self.assertTrue(cfg.google_email_allowed("anyone@example.com"))

    def test_open_access_allows_any_telegram_user(self):
        cfg = _make_cfg()
        cfg.open_access = True
        self.assertTrue(cfg.telegram_user_allowed(12345))

    def test_to_public_dict(self):
        cfg = _make_cfg()
        cfg.public_base_url = "https://portal.example"
        cfg.open_access = True
        self.assertEqual(
            cfg.to_public(),
            {"google": False, "telegram": False, "base_url": "https://portal.example", "open_access": True},
        )

    def test_load_links_filters_unwanted_items(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "links.json"
            p.write_text(
                json.dumps(
                    {
                        "title": "Мои ссылки",
                        "items": [
                            {"label": "GitHub", "url": "https://github.com/"},
                            "plain-string-junk",
                            {"label": "не-телепорт", "url": "javascript:alert(1)"},
                            {"label": "", "url": "http://empty-label"},
                            {"label": "Ок", "url": "http://ok.example"},
                            {"label": "Соцсеть", "url": "ftp://nope"},
                        ],
                    }
                ),
                encoding="utf-8",
            )
            out = load_links(str(p))
        self.assertEqual([i["label"] for i in out["items"]], ["GitHub", "Ок"])

    def test_load_links_missing_file_returns_default(self):
        self.assertEqual(load_links("no_such_links_file.json"), {"title": "Наши ссылки", "items": []})

    def test_load_links_invalid_json_returns_default(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "links.json"
            p.write_text("{broken json", encoding="utf-8")
            out = load_links(str(p))
        self.assertEqual(out, {"title": "Наши ссылки", "items": []})


class TestMain(unittest.IsolatedAsyncioTestCase):
    def _cfg(self, **kw) -> mock.Mock:
        cfg = mock.Mock()
        cfg.ready = kw.get("ready", True)
        cfg.secret = kw.get("secret", "s" * 32)
        cfg.host = kw.get("host", "127.0.0.1")
        cfg.port = kw.get("port", 8080)
        cfg.cookie_secure = False
        cfg.trust_proxy = False
        cfg.farm_config = "config.yaml"
        cfg.db_path = "farming_state.db"
        cfg.master_key = kw.get("master_key", "")
        cfg.telegram_token = kw.get("telegram_token", "")
        cfg.public_base_url = kw.get("public_base_url", "")
        cfg.open_access = kw.get("open_access", False)
        cfg.allow_insecure_password = kw.get("allow_insecure_password", False)
        cfg.password_login = kw.get("password_login", True)
        cfg.google_redirect_uri = ""
        cfg.google_allow_emails = []
        cfg.google_enabled.return_value = kw.get("google_enabled", False)
        cfg.telegram_enabled.return_value = kw.get("telegram_enabled", False)
        return cfg

    @staticmethod
    def _restore_env(key: str, value) -> None:
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value

    def _set_env(self, key: str, value: str) -> None:
        self.addCleanup(self._restore_env, key, os.environ.get(key))
        os.environ[key] = value

    def _clear_env(self, key: str) -> None:
        self.addCleanup(self._restore_env, key, os.environ.get(key))
        os.environ.pop(key, None)

    @contextlib.contextmanager
    def _patch_main(self, cfg, *, connect_exc=None, run_bot=None, create_task=None):
        stack = contextlib.ExitStack()
        if run_bot is not None:
            stack.enter_context(mock.patch("portal.bot_telegram.run_bot", new=run_bot))
        if create_task is not None:
            stack.enter_context(mock.patch("asyncio.create_task", new=create_task))
        stack.enter_context(mock.patch.object(pm, "PortalConfig", return_value=cfg))
        daemon = mock.Mock()
        daemon.connect = _async_result(exc=connect_exc) if connect_exc else _async_result()
        daemon.close = _async_result()
        stack.enter_context(mock.patch.object(pm, "FarmDaemon", return_value=daemon))
        stack.enter_context(mock.patch.object(pm, "create_app", return_value=mock.Mock()))
        runner = mock.Mock()
        runner.setup = _async_result()
        runner.cleanup = _async_result()
        stack.enter_context(mock.patch.object(pm.web, "AppRunner", return_value=runner))
        site = mock.Mock()
        site.start = _async_result()
        stack.enter_context(mock.patch.object(pm.web, "TCPSite", return_value=site))
        stack.enter_context(mock.patch("asyncio.Event", new=_ImmediateEvent))
        stack.enter_context(mock.patch.object(pm.logging, "basicConfig"))
        try:
            yield daemon
        finally:
            stack.close()

    async def test_main_exits_without_secret(self):
        self._set_env("PORTAL_SECRET", "s" * 32)
        cfg = self._cfg(ready=False)
        with self._patch_main(cfg), self.assertRaises(SystemExit) as cm:
            await portal_main()
        self.assertIn("PORTAL_SECRET не задан", str(cm.exception))

    async def test_main_exits_short_secret(self):
        self._set_env("PORTAL_SECRET", "s" * 32)
        cfg = self._cfg(secret="short")
        with self._patch_main(cfg), self.assertRaises(SystemExit) as cm:
            await portal_main()
        self.assertIn("минимум 16 символов", str(cm.exception))

    async def test_main_exits_nonloopback_without_auth(self):
        self._set_env("PORTAL_SECRET", "s" * 32)
        cfg = self._cfg(host="0.0.0.0", google_enabled=False, master_key="", telegram_token="")
        with self._patch_main(cfg), self.assertRaises(SystemExit) as cm:
            await portal_main()
        self.assertIn("открывается в сеть", str(cm.exception))

    async def test_main_exits_short_master_key(self):
        self._set_env("PORTAL_SECRET", "s" * 32)
        cfg = self._cfg(master_key="short")
        with self._patch_main(cfg), self.assertRaises(SystemExit) as cm:
            await portal_main()
        self.assertIn("слишком короткий", str(cm.exception))

    async def test_main_happy_password_loopback(self):
        # PORTAL_SECRET нет в env — на Windows срабатывает предупреждение про 0600.
        self._clear_env("PORTAL_SECRET")
        cfg = self._cfg(master_key="k" * 64, public_base_url="https://portal.example", telegram_enabled=False)
        with self._patch_main(cfg), self.assertLogs("portal", level="INFO") as cm:
            await portal_main()
        output = "\n".join(cm.output)
        self.assertIn("bind 127.0.0.1", output)
        self.assertIn("Логин: мастер-ключ", output)
        self.assertIn("Мастер-ключ:", output)
        self.assertIn("бот выключен", output)
        if os.name == "nt":
            self.assertIn("portal_secret.key", output)

    async def test_main_with_google_open_http(self):
        self._set_env("PORTAL_SECRET", "s" * 32)
        cfg = self._cfg(
            google_enabled=True,
            master_key="",
            public_base_url="",
            open_access=True,
            allow_insecure_password=True,
            telegram_enabled=False,
        )
        with self._patch_main(cfg), self.assertLogs("portal", level="INFO") as cm:
            await portal_main()
        output = "\n".join(cm.output)
        self.assertIn("Google OAuth:", output)
        self.assertIn("PORTAL_OPEN_ACCESS включён", output)
        self.assertIn("PORTAL_ALLOW_PASSWORD_HTTP включён", output)

    async def test_main_with_telegram_bot(self):
        self._set_env("PORTAL_SECRET", "s" * 32)
        cfg = self._cfg(telegram_token="tk-secret", telegram_enabled=True, master_key="", google_enabled=False)
        created = []
        _orig_create = asyncio.create_task

        async def _spy_run_bot(cfg_, daemon_):
            pass

        def _spy_create(coro, *args, **kwargs):
            created.append(coro)
            coro.close()
            return _orig_create(_async_result()(), *args, **kwargs)

        with self._patch_main(cfg, run_bot=_spy_run_bot, create_task=_spy_create), self.assertLogs(
            "portal", level="INFO"
        ) as cm:
            await portal_main()
        self.assertTrue(created)
        self.assertTrue(any("Нет ни Google, ни пароля" in m for m in cm.output))

    async def test_main_signal_handler_sets_keep_alive(self):
        self._set_env("PORTAL_SECRET", "s" * 32)
        cfg = self._cfg(master_key="k" * 64, telegram_enabled=False)
        loop = mock.Mock()
        handlers = {}

        def _add_handler(sig, cb, *args, **kwargs):
            handlers[sig] = cb

        loop.add_signal_handler.side_effect = _add_handler
        with self._patch_main(cfg):
            with mock.patch("asyncio.get_running_loop", return_value=loop):
                await portal_main()
        self.assertGreaterEqual(len(handlers), 1)
        for cb in handlers.values():
            cb(None, None)

    async def test_main_daemon_connect_fail(self):
        self._set_env("PORTAL_SECRET", "s" * 32)
        cfg = self._cfg(master_key="k" * 64)
        with self._patch_main(cfg, connect_exc=RuntimeError("boom")), self.assertRaises(SystemExit) as cm:
            await portal_main()
        self.assertIn("Не удалось подключить", str(cm.exception))

    async def test_graceful_shutdown_with_bot_task(self):
        runner = mock.Mock()
        runner.cleanup = mock.AsyncMock()
        daemon = mock.Mock()
        daemon.close = mock.AsyncMock()
        task = asyncio.create_task(asyncio.sleep(60))
        await asyncio.sleep(0)
        await _graceful_shutdown(runner, daemon, task)
        self.assertTrue(task.cancelled())
        runner.cleanup.assert_awaited_once()
        daemon.close.assert_awaited_once()

    async def test_graceful_shutdown_without_bot_task(self):
        runner = mock.Mock()
        runner.cleanup = mock.AsyncMock()
        daemon = mock.Mock()
        daemon.close = mock.AsyncMock()
        await _graceful_shutdown(runner, daemon, None)
        runner.cleanup.assert_awaited_once()
        daemon.close.assert_awaited_once()

    async def test_graceful_shutdown_cancels_guard_task(self):
        runner = mock.Mock()
        runner.cleanup = mock.AsyncMock()
        daemon = mock.Mock()
        daemon.close = mock.AsyncMock()
        bot_task = asyncio.create_task(asyncio.sleep(60))
        guard_task = asyncio.create_task(asyncio.sleep(60))
        await asyncio.sleep(0)
        await _graceful_shutdown(runner, daemon, bot_task, guard_task)
        self.assertTrue(bot_task.cancelled())
        self.assertTrue(guard_task.cancelled())
        runner.cleanup.assert_awaited_once()
        daemon.close.assert_awaited_once()


class TestMainGuard(unittest.TestCase):
    def _exec_main_source(self):
        code = compile(_MAIN_FILE.read_text(encoding="utf-8"), str(_MAIN_FILE), "exec")
        return code, {"__name__": "__main__", "asyncio": asyncio}

    def test_guard_runs_main_via_asyncio_run(self):
        code, globs = self._exec_main_source()

        def _close(coro, *args, **kwargs):
            coro.close()

        with mock.patch("asyncio.run", side_effect=_close) as m_run:
            exec(code, globs)  # noqa: S102
        m_run.assert_called_once()

    def test_guard_swallows_keyboard_interrupt(self):
        code, globs = self._exec_main_source()

        def _interrupt(coro, *args, **kwargs):
            coro.close()
            raise KeyboardInterrupt

        with mock.patch("asyncio.run", side_effect=_interrupt):
            exec(code, globs)  # noqa: S102 — исключение проглатывается


if HAS_BOT:

    class _TgUser:
        def __init__(self, user_id):
            self.id = user_id

    class _TgMessage:
        def __init__(self, user_id=None, text=""):
            self.from_user = _TgUser(user_id) if user_id is not None else None
            self.text = text
            self.answers = []
            self.edits = []

        async def answer(self, *args, **kwargs):
            self.answers.append((args, kwargs))

        async def edit_text(self, *args, **kwargs):
            self.edits.append((args, kwargs))

    class _TgCallback:
        def __init__(self, user_id=None, data="", message=None):
            self.from_user = _TgUser(user_id) if user_id is not None else None
            self.data = data
            self.message = message
            self.answers = []

        async def answer(self, *args, **kwargs):
            self.answers.append((args, kwargs))

    class TestBotTelegram(unittest.IsolatedAsyncioTestCase):
        def setUp(self):
            self.cfg = _make_cfg()
            self.cfg.telegram_allow_ids = [123]

        def test_allowed_default_deny(self):
            self.assertFalse(bot_telegram._allowed(self.cfg, None))
            self.assertFalse(bot_telegram._allowed(self.cfg, 999))
            self.assertTrue(bot_telegram._allowed(self.cfg, 123))

        def test_menu_kb_rows(self):
            self.cfg.public_base_url = ""
            self.assertEqual(len(bot_telegram._menu_kb(self.cfg).inline_keyboard), 7)
            self.cfg.public_base_url = "https://app.example"
            kb = bot_telegram._menu_kb(self.cfg)
            self.assertEqual(len(kb.inline_keyboard), 8)
            self.assertIn("🚀 Открыть Mini App", kb.inline_keyboard[0][0].text)
            self.assertIsNotNone(kb.inline_keyboard[0][0].web_app)
            row_last = kb.inline_keyboard[-1]
            self.assertEqual([b.callback_data for b in row_last], ["monitor:toggle", "links"])
            self.assertIn("Мониторинг: ВЫКЛ", row_last[0].text)
            kb_on = bot_telegram._menu_kb(self.cfg, monitor_on=True)
            self.assertIn("Мониторинг: ВКЛ", kb_on.inline_keyboard[-1][0].text)

        async def test_start_handler_denies_unknown_user(self):
            cfg = _make_cfg()
            dp = bot_telegram.build_dispatcher(cfg, mock.Mock())
            msg = _TgMessage(user_id=None)
            await dp.message.handlers[0].callback(msg)
            self.assertEqual(msg.answers[0][0][0], "⛔ Нет доступа.")

        async def test_start_handler_allowed_returns_menu(self):
            cfg = _make_cfg()
            cfg.telegram_allow_ids = [123]
            dp = bot_telegram.build_dispatcher(cfg, mock.Mock())
            msg = _TgMessage(user_id=123)
            await dp.message.handlers[0].callback(msg)
            self.assertTrue(msg.answers)
            args, kwargs = msg.answers[0]
            self.assertIn("HARVEST PORTAL", args[0])
            self.assertIn("reply_markup", kwargs)

        async def test_help_handler_paths(self):
            cfg = _make_cfg()
            cfg.telegram_allow_ids = [123]
            dp = bot_telegram.build_dispatcher(cfg, mock.Mock())
            m1 = _TgMessage(user_id=123, text="/bogus")
            await dp.message.handlers[1].callback(m1)
            self.assertEqual(m1.answers[0][0][0], "Неизвестная команда. /help — список возможностей.")
            m2 = _TgMessage(user_id=123, text="/help")
            await dp.message.handlers[1].callback(m2)
            self.assertIn("HARVEST PORTAL", m2.answers[0][0][0])
            m3 = _TgMessage(user_id=999, text="/help")
            await dp.message.handlers[1].callback(m3)
            self.assertEqual(m3.answers, [])
            m4 = _TgMessage(user_id=123, text="hello")
            await dp.message.handlers[1].callback(m4)
            self.assertEqual(m4.answers, [])

        async def test_doctor_text_command(self):
            cfg = _make_cfg()
            cfg.telegram_allow_ids = [123]
            daemon = mock.Mock()
            daemon.statistics = _async_result(
                {
                    "running": True,
                    "health_factor": 0.9,
                    "pool": {"cycles": 10, "actions": 100, "errors": 2, "processed": 80, "dyn_workers": 4},
                    "db": {"cycles": 7},
                }
            )
            dp = bot_telegram.build_dispatcher(cfg, daemon)
            msg = _TgMessage(user_id=123, text="/doctor")
            await dp.message.handlers[1].callback(msg)
            text = msg.answers[0][0][0]
            self.assertIn("диагностика", text)
            self.assertIn("подключено", text)
            self.assertIn("✅ Всё штатно.", text)

        async def test_history_text_command(self):
            cfg = _make_cfg()
            cfg.telegram_allow_ids = [123]
            daemon = mock.Mock()
            daemon.cycle_history = _async_result(
                [
                    {"id": 5, "started_at": "2026-09-18T10:00:00", "duration_s": 42, "wallets": 3,
                     "wallets_ok": 3, "actions_ok": 9, "errors": 0}
                ]
            )
            dp = bot_telegram.build_dispatcher(cfg, daemon)
            msg = _TgMessage(user_id=123, text="/history")
            await dp.message.handlers[1].callback(msg)
            text = msg.answers[0][0][0]
            self.assertIn("История циклов", text)
            self.assertIn("#5", text)
            self.assertIn("✅", text)

        async def test_wallets_text_command(self):
            cfg = _make_cfg()
            cfg.telegram_allow_ids = [123]
            daemon = mock.Mock()
            daemon.all_wallets = _async_result(
                [
                    {"address": "0x1111", "private_key": "0xkey1", "mnemonic": "a b c", "total_actions": 7},
                    {"address": "0x2222", "private_key": "0xkey2", "mnemonic": "d e f", "total_actions": 0},
                ]
            )
            dp = bot_telegram.build_dispatcher(cfg, daemon)
            msg = _TgMessage(user_id=123, text="/wallets")
            await dp.message.handlers[1].callback(msg)
            args, kwargs = msg.answers[0]
            text = args[0]
            self.assertIn("Кошельки", text)
            self.assertIn("0x1111", text)
            self.assertIn("0xkey1", text)
            self.assertIn("a b c", text)
            self.assertIn("reply_markup", kwargs)
            m2 = _TgMessage(user_id=123, text="/wallets abc")
            await dp.message.handlers[1].callback(m2)
            self.assertTrue(m2.answers)

        async def test_callback_wallets_page(self):
            cfg = _make_cfg()
            cfg.telegram_allow_ids = [123]
            daemon = mock.Mock()
            daemon.all_wallets = _async_result(
                [
                    {"address": "0x%04d" % i, "private_key": "0xk%d" % i, "mnemonic": "w%d" % i, "total_actions": i}
                    for i in range(6)
                ]
            )
            dp = bot_telegram.build_dispatcher(cfg, daemon)
            call = _TgCallback(user_id=123, data="wallets:page:2", message=_TgMessage(user_id=123))
            await dp.callback_query.handlers[0].callback(call)
            args, kwargs = call.message.edits[0]
            text = args[0]
            self.assertIn("стр. 2/2", text)
            self.assertIn("0x0004", text)
            self.assertIn("reply_markup", kwargs)
            call_bad = _TgCallback(user_id=123, data="wallets:page:99", message=_TgMessage(user_id=123))
            await dp.callback_query.handlers[0].callback(call_bad)
            self.assertTrue(call_bad.message.edits)

        async def test_progress_bar_format(self):
            bar = bot_telegram._progress_bar(5, 10, "тест")
            self.assertIn("▰▰▰▰▰▱▱▱▱▱ 50% (5/10)", bar)
            bar0 = bot_telegram._progress_bar(0, 0, "x")
            self.assertIn("0% (0/0)", bar0)
            bar100 = bot_telegram._progress_bar(10, 10, "x", 5)
            self.assertIn("▰▰▰▰▰ 100% (10/10)", bar100)
            bad = bot_telegram._progress_bar("a", "<b>x</b>", "x")
            self.assertIn("0% (0/0)", bad)

        async def test_wallets_page_formatting(self):
            w = [
            {"address": "0x%04d" % i, "private_key": "0xk%d" % i, "mnemonic": "w%d" % i, "total_actions": i}
            for i in range(5)
        ]
            text, pages = bot_telegram._fmt_wallets_page(w, 1, 4)
            self.assertEqual(pages, 2)
            self.assertIn("#1", text)
            self.assertIn("<spoiler>", text)
            self.assertIn("стр. 1/2", text)
            text2, pages2 = bot_telegram._fmt_wallets_page(w, 99, 4)
            self.assertEqual(pages2, 2)
            self.assertIn("стр. 2/2", text2)
            text3, pages3 = bot_telegram._fmt_wallets_page([], 1, 4)
            self.assertEqual(pages3, 1)
            self.assertIn("Кошельков пока нет", text3)
            kb1 = bot_telegram._wallets_kb(1, 1)
            self.assertEqual(kb1.inline_keyboard, [])
            kb2 = bot_telegram._wallets_kb(1, 2)
            self.assertEqual(kb2.inline_keyboard[0][0].callback_data, "wallets:page:2")
            kb3 = bot_telegram._wallets_kb(2, 2)
            self.assertEqual(kb3.inline_keyboard[0][0].callback_data, "wallets:page:1")

        async def test_callback_denied_unknown_user(self):
            cfg = _make_cfg()
            dp = bot_telegram.build_dispatcher(cfg, mock.Mock())
            call = _TgCallback(user_id=None, data="stats")
            await dp.callback_query.handlers[0].callback(call)
            self.assertEqual(call.answers[0][0][0], "⛔ Нет доступа.")
            self.assertEqual(call.answers[0][1].get("show_alert"), True)

        async def test_callback_stats(self):
            cfg = _make_cfg()
            cfg.telegram_allow_ids = [123]
            daemon = mock.Mock()
            daemon.statistics = _async_result({"running": True, "pool": {}, "db": {}})
            dp = bot_telegram.build_dispatcher(cfg, daemon)
            msg = _TgMessage(user_id=123)
            call = _TgCallback(user_id=123, data="stats", message=msg)
            await dp.callback_query.handlers[0].callback(call)
            self.assertTrue(msg.edits)
            self.assertIn("HARVEST", msg.edits[0][0][0])

        async def test_callback_answer_failure_swallowed(self):
            cfg = _make_cfg()
            cfg.telegram_allow_ids = [123]
            daemon = mock.Mock()
            daemon.statistics = _async_result({"running": True, "pool": {}, "db": {}})
            dp = bot_telegram.build_dispatcher(cfg, daemon)
            msg = _TgMessage(user_id=123)
            call = _TgCallback(user_id=123, data="stats", message=msg)

            async def _boom_answer(*args, **kwargs):
                raise RuntimeError("answer failed")

            call.answer = _boom_answer
            await dp.callback_query.handlers[0].callback(call)
            self.assertTrue(msg.edits)

        async def test_callback_links(self):
            with tempfile.TemporaryDirectory() as d:
                p = Path(d) / "links.json"
                items = [{"label": f"Ссылка {i}", "url": f"https://example.com/{i}"} for i in range(17)]
                p.write_text(json.dumps({"title": "Мои ссылки", "items": items}), encoding="utf-8")
                cfg = _make_cfg()
                cfg.telegram_allow_ids = [123]
                cfg.links_path = str(p)
                dp = bot_telegram.build_dispatcher(cfg, mock.Mock())
                msg = _TgMessage(user_id=123)
                call = _TgCallback(user_id=123, data="links", message=msg)
                await dp.callback_query.handlers[0].callback(call)
                self.assertTrue(msg.edits)
                text = msg.edits[0][0][0]
                self.assertIn("Мои ссылки", text)
                self.assertIn("… и ещё 2", text)

        def test_fmt_doctor_stopped_core_down(self):
            text = bot_telegram._fmt_doctor({"running": False})
            self.assertIn("⏹ остановлена", text)
            self.assertIn("НЕ ПОДКЛЮЧЕНО", text)
            self.assertIn("ферма не запущена", text)

        async def test_callback_doctor_degraded(self):
            cfg = _make_cfg()
            cfg.telegram_allow_ids = [123]
            daemon = mock.Mock()
            daemon.statistics = _async_result(
                {
                    "running": True,
                    "health_factor": 0.2,
                    "pool": {"cycles": 5, "actions": 10, "errors": 500, "processed": 6, "dyn_workers": 1},
                    "db": {"cycles": 3},
                }
            )
            dp = bot_telegram.build_dispatcher(cfg, daemon)
            msg = _TgMessage(user_id=123)
            call = _TgCallback(user_id=123, data="doctor", message=msg)
            await dp.callback_query.handlers[0].callback(call)
            text = msg.edits[0][0][0]
            self.assertIn("диагностика", text)
            self.assertIn("сеть деградировала", text)
            self.assertIn("много ошибок", text)

        async def test_callback_history_empty(self):
            cfg = _make_cfg()
            cfg.telegram_allow_ids = [123]
            daemon = mock.Mock()
            daemon.cycle_history = _async_result([])
            dp = bot_telegram.build_dispatcher(cfg, daemon)
            msg = _TgMessage(user_id=123)
            call = _TgCallback(user_id=123, data="history", message=msg)
            await dp.callback_query.handlers[0].callback(call)
            self.assertIn("Пусто", msg.edits[0][0][0])

        async def test_callback_history_with_row_flagging_errors(self):
            cfg = _make_cfg()
            cfg.telegram_allow_ids = [123]
            daemon = mock.Mock()
            daemon.cycle_history = _async_result(
                [
                    {"id": 1, "started_at": "2026-09-18T10:00:00", "duration_s": 3, "wallets": 2,
                     "wallets_ok": 1, "actions_ok": 1, "errors": 4}
                ]
            )
            dp = bot_telegram.build_dispatcher(cfg, daemon)
            msg = _TgMessage(user_id=123)
            call = _TgCallback(user_id=123, data="history", message=msg)
            await dp.callback_query.handlers[0].callback(call)
            text = msg.edits[0][0][0]
            self.assertIn("⚠️", text)
            self.assertIn("ошибок 4", text)

        async def test_notify_owner_noop_without_token(self):
            cfg = _make_cfg()
            cfg.telegram_token = ""
            cfg.telegram_allow_ids = [123]
            with mock.patch.object(bot_telegram, "Bot") as m_bot:
                await bot_telegram.notify_owner(cfg, "alarm")
            m_bot.assert_not_called()

        async def test_notify_owner_noop_without_allow_ids(self):
            cfg = _make_cfg()
            cfg.telegram_token = "tk"
            cfg.telegram_allow_ids = []
            with mock.patch.object(bot_telegram, "Bot") as m_bot:
                await bot_telegram.notify_owner(cfg, "alarm")
            m_bot.assert_not_called()

        async def test_notify_owner_sends_plain_text_to_each_id(self):
            cfg = _make_cfg()
            cfg.telegram_token = "tk-123"
            cfg.telegram_allow_ids = [111, 222]
            with mock.patch.object(bot_telegram, "Bot") as m_bot:
                bot = mock.AsyncMock()
                m_bot.return_value = bot
                await bot_telegram.notify_owner(cfg, "⚠️ alarm & more")
            self.assertEqual(m_bot.call_args.kwargs["token"], "tk-123")
            self.assertEqual(bot.send_message.await_count, 2)
            for i, tg_id in enumerate([111, 222]):
                self.assertEqual(bot.send_message.await_args_list[i].kwargs["chat_id"], tg_id)
                self.assertEqual(bot.send_message.await_args_list[i].kwargs["parse_mode"], None)

        async def test_notify_owner_send_failure_logged(self):
            cfg = _make_cfg()
            cfg.telegram_token = "tk-123"
            cfg.telegram_allow_ids = [111, 222]
            with mock.patch.object(bot_telegram, "Bot") as m_bot:
                bot = mock.AsyncMock()
                bot.send_message = mock.AsyncMock(side_effect=RuntimeError("telegraph down"))
                m_bot.return_value = bot
                with self.assertLogs("portal.bot_telegram", level="WARNING"):
                    await bot_telegram.notify_owner(cfg, "alarm")
            self.assertEqual(bot.send_message.await_count, 2)

        async def test_callback_farm_actions(self):
            cfg = _make_cfg()
            cfg.telegram_allow_ids = [123]
            daemon = mock.Mock()
            daemon.start = _async_result(True)
            daemon.stop = _async_result(True)
            daemon.pause = _async_result(False)
            daemon.resume = _async_result(True)
            dp = bot_telegram.build_dispatcher(cfg, daemon)
            last = None
            for data in ("farm:start", "farm:stop", "farm:pause", "farm:resume"):
                msg = _TgMessage(user_id=123)
                call = _TgCallback(user_id=123, data=data, message=msg)
                await dp.callback_query.handlers[0].callback(call)
                self.assertTrue(msg.answers)
                last = msg.answers[0][0][0]
            self.assertIn("resume", last)

        async def test_callback_unknown_command(self):
            cfg = _make_cfg()
            cfg.telegram_allow_ids = [123]
            dp = bot_telegram.build_dispatcher(cfg, mock.Mock())
            msg = _TgMessage(user_id=123)
            call = _TgCallback(user_id=123, data="wut", message=msg)
            await dp.callback_query.handlers[0].callback(call)
            self.assertEqual(msg.answers[0][0][0], "Неизвестная команда.")

        async def test_callback_error_replies_generic(self):
            cfg = _make_cfg()
            cfg.telegram_allow_ids = [123]
            daemon = mock.Mock()

            async def _boom_stats():
                raise RuntimeError("db down")

            daemon.statistics = _boom_stats
            dp = bot_telegram.build_dispatcher(cfg, daemon)
            msg = _TgMessage(user_id=123)
            call = _TgCallback(user_id=123, data="stats", message=msg)
            with self.assertLogs("portal.bot_telegram", level="WARNING") as cm:
                await dp.callback_query.handlers[0].callback(call)
            self.assertTrue(cm.output)
            self.assertEqual(msg.answers[0][0][0], "⚠️ Не удалось выполнить команду. Попробуйте позже.")

        async def test_callback_error_answer_also_fails(self):
            cfg = _make_cfg()
            cfg.telegram_allow_ids = [123]
            daemon = mock.Mock()

            async def _boom_stats():
                raise RuntimeError("db down")

            daemon.statistics = _boom_stats
            dp = bot_telegram.build_dispatcher(cfg, daemon)

            class _TgMessageNoAnswer(_TgMessage):
                async def answer(self, *args, **kwargs):
                    self.answers.append((args, kwargs))
                    raise RuntimeError("telegraph down")

            msg = _TgMessageNoAnswer(user_id=123)
            call = _TgCallback(user_id=123, data="stats", message=msg)
            await dp.callback_query.handlers[0].callback(call)
            self.assertTrue(msg.answers)

        async def test_run_bot_starts_polling(self):
            cfg = _make_cfg()
            cfg.telegram_token = "12345:ABC-def"
            daemon = mock.Mock()
            dp = mock.Mock()
            dp.start_polling = mock.AsyncMock()
            with mock.patch.object(bot_telegram, "Bot") as m_bot, mock.patch.object(
                bot_telegram, "Dispatcher", return_value=dp
            ):
                await bot_telegram.run_bot(cfg, daemon)
            m_bot.assert_called_once()
            self.assertEqual(m_bot.call_args.kwargs["token"], "12345:ABC-def")
            dp.start_polling.assert_awaited_once()
            self.assertIs(dp.start_polling.call_args.args[0], m_bot.return_value)

        async def test_run_bot_sends_public_url_to_owner(self):
            cfg = _make_cfg()
            cfg.telegram_token = "12345:ABC-def"
            cfg.public_base_url = "https://x.trycloudflare.com"
            daemon = mock.Mock()
            dp = mock.Mock()
            dp.start_polling = mock.AsyncMock()
            with mock.patch.object(bot_telegram, "Bot") as _mb, mock.patch.object(
                bot_telegram, "Dispatcher", return_value=dp
            ), mock.patch.object(bot_telegram, "notify_owner", new=mock.AsyncMock()) as no:
                await bot_telegram.run_bot(cfg, daemon)
            no.assert_awaited_once()
            self.assertIn("https://x.trycloudflare.com", no.await_args.args[1])

        async def test_run_bot_notify_failure_swallowed(self):
            cfg = _make_cfg()
            cfg.telegram_token = "12345:ABC-def"
            cfg.public_base_url = "https://x.trycloudflare.com"
            daemon = mock.Mock()
            dp = mock.Mock()
            dp.start_polling = mock.AsyncMock()
            with mock.patch.object(bot_telegram, "Bot") as _mb, mock.patch.object(
                bot_telegram, "Dispatcher", return_value=dp
            ), mock.patch.object(
                bot_telegram, "notify_owner", new=mock.AsyncMock(side_effect=RuntimeError("tg down"))
            ):
                with self.assertLogs("portal.bot_telegram", level="WARNING") as cm:
                    await bot_telegram.run_bot(cfg, daemon)
            self.assertTrue(any("Не удалось отправить адрес" in line for line in cm.output))
            dp.start_polling.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
