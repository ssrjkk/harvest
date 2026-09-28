"""Coverage tests for core/config.py, core/actions.py, core/ui.py"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_config(actions=None, dry_run=False, gas_limit=21000, flush_on_action=False):
    if actions is None:
        actions = [{"type": "transfer", "target": "random_wallet", "min_amount": 0.0001, "max_amount": 0.001}]
    return {
        "actions": actions,
        "advanced": {
            "gas_limit": gas_limit,
            "dry_run": dry_run,
            "flush_on_action": flush_on_action,
        },
    }


def _make_wallet(addr="0x" + "a" * 40, pk="0x" + "b" * 64):
    return {"address": addr, "private_key": pk}


# ===================================================================
# core/config.py tests
# ===================================================================

class TestConfig:
    def test_file_not_found(self, tmp_path):
        from core.config import load_config
        result = load_config(str(tmp_path / "nope.yaml"))
        assert result is None

    def test_invalid_yaml(self, tmp_path):
        from core.config import load_config
        p = tmp_path / "bad.yaml"
        p.write_text(": : : invalid {{{", encoding="utf-8")
        result = load_config(str(p))
        assert result is None

    def test_empty_file(self, tmp_path):
        from core.config import load_config
        p = tmp_path / "empty.yaml"
        p.write_text("", encoding="utf-8")
        result = load_config(str(p))
        assert result is None

    def test_missing_required_keys(self, tmp_path):
        from core.config import load_config
        p = tmp_path / "partial.yaml"
        p.write_text("network:\n  rpc_url: http://x\n", encoding="utf-8")
        result = load_config(str(p))
        assert result is None

    @patch("core.config.apply_env_overrides", side_effect=RuntimeError("boom"))
    def test_env_override_error(self, mock_env, tmp_path):
        from core.config import load_config
        cfg = {
            "network": {"rpc_url": "http://x"},
            "wallets": {"count": 1},
            "faucet": True,
            "actions": [{"type": "transfer"}],
            "farming": {},
            "threading": {},
            "database": {},
        }
        import yaml
        p = tmp_path / "c.yaml"
        p.write_text(yaml.dump(cfg), encoding="utf-8")
        result = load_config(str(p))
        assert result is None

    @patch("core.config.validate_config", side_effect=Exception("TypeError"))
    def test_validate_config_generic_exception(self, mock_val, tmp_path):
        from core.config import load_config
        cfg = {
            "network": {"rpc_url": "http://x"},
            "wallets": {"count": 1},
            "faucet": True,
            "actions": [{"type": "transfer"}],
            "farming": {},
            "threading": {},
            "database": {},
        }
        import yaml
        p = tmp_path / "c.yaml"
        p.write_text(yaml.dump(cfg), encoding="utf-8")
        result = load_config(str(p))
        assert result is None

    def test_valid_config(self, tmp_path):
        from core.config import load_config
        cfg = {
            "network": {"rpc_url": "https://rpc.example.com", "chain_id": 1},
            "wallets": {"count": 1, "generate_if_missing": True},
            "faucet": {"enabled": False, "strategies": []},
            "actions": [{"type": "transfer", "target": "random_wallet", "min_amount": 0.0001, "max_amount": 0.001}],
            "farming": {"cycles": 1},
            "threading": {"max_workers": 1},
            "database": {"path": str(tmp_path / "db.sqlite")},
        }
        import yaml
        p = tmp_path / "c.yaml"
        p.write_text(yaml.dump(cfg), encoding="utf-8")
        result = load_config(str(p))
        assert result is not None
        assert "network" in result


# ===================================================================
# core/actions.py tests
# ===================================================================

class TestActionHelpers:
    def test_is_zero_empty(self):
        from core.actions import _is_zero
        assert _is_zero("") is True
        assert _is_zero(None) is True

    def test_is_zero_real(self):
        from core.actions import _is_zero
        assert _is_zero("0x" + "0" * 40) is True
        assert _is_zero("0x" + "a" * 40) is False

    def test_is_zero_bad_hex(self):
        from core.actions import _is_zero
        assert _is_zero("not_hex") is False

    def test_is_valid_contract(self):
        from core.actions import _is_valid_contract
        assert _is_valid_contract("") is False
        assert _is_valid_contract("0x" + "0" * 40) is False
        assert _is_valid_contract("0x" + "a" * 40) is True
        assert _is_valid_contract("garbage") is False

    def test_normalize_action_non_contract(self):
        from core.actions import _normalize_action
        a = {"type": "transfer", "target": "0x" + "c" * 40}
        assert _normalize_action(a) is a

    def test_normalize_action_valid_contract(self):
        from core.actions import _normalize_action
        a = {"type": "vibevibe_swap", "contract": "0x" + "a" * 40}
        assert _normalize_action(a) is a

    def test_normalize_action_zero_contract_becomes_transfer(self):
        from core.actions import _normalize_action
        a = {"type": "vibevibe_swap", "contract": "0x" + "0" * 40, "min_amount": 0.001, "max_amount": 0.01}
        result = _normalize_action(a)
        assert result["type"] == "transfer"
        assert result["min_amount"] == 0.001

    def test_adaptive_scores_empty_history(self):
        from core.actions import _adaptive_scores
        scores = _adaptive_scores([1.0, 2.0], [[], []])
        assert scores == [1.0, 2.0]

    def test_adaptive_scores_all_success(self):
        from core.actions import _adaptive_scores
        scores = _adaptive_scores([1.0], [[True, True, True]])
        assert scores[0] > 1.0

    def test_adaptive_scores_all_fail(self):
        from core.actions import _adaptive_scores
        # >=5 попыток и ни одного успеха → вес обнуляется, сумма<=0 → ресет в равные
        scores = _adaptive_scores([1.0], [[False, False, False, False, False]])
        assert scores == [1.0]

    def test_adaptive_scores_all_zero_resets(self):
        from core.actions import _adaptive_scores
        scores = _adaptive_scores([0.0], [[False] * 10])
        assert scores == [1.0]

    def test_adaptive_scores_mixed(self):
        from core.actions import _adaptive_scores
        scores = _adaptive_scores([1.0], [[True, False, True]])
        assert 0.0 < scores[0] < 1.5


class TestActionExecutor:
    def _make_executor(self, actions=None, dry_run=False):
        from core.actions import ActionExecutor
        cfg = _make_config(actions=actions, dry_run=dry_run)
        net = MagicMock()
        vibevibe = AsyncMock()
        db = MagicMock()
        return ActionExecutor(net, vibevibe, db, cfg), net, vibevibe, db

    def test_init_inverted_range(self):
        from core.actions import ActionExecutor
        cfg = _make_config(actions=[
            {"type": "transfer", "target": "0x" + "c" * 40, "min_amount": 0.01, "max_amount": 0.001}
        ])
        ex = ActionExecutor(MagicMock(), AsyncMock(), MagicMock(), cfg)
        a = ex.actions_conf[0]
        assert a["min_amount"] == 0.001
        assert a["max_amount"] == 0.01

    def test_pick_action_with_profile(self):
        from core.behavior import WalletProfile
        ex, _, _, _ = self._make_executor()
        prof = WalletProfile(activity=1.0, action_multipliers={"transfer": 2.0})
        idx, conf = ex._pick_action(prof)
        assert 0 <= idx < len(ex.actions_conf)

    def test_pick_action_no_weights(self):
        from core.actions import ActionExecutor
        cfg = _make_config(actions=[
            {"type": "transfer", "target": "0x" + "c" * 40, "weight": -1.0},
        ])
        ex = ActionExecutor(MagicMock(), AsyncMock(), MagicMock(), cfg)
        idx, conf = ex._pick_action()
        assert conf["type"] == "transfer"

    def test_record_type(self):
        ex, _, _, _ = self._make_executor()
        ex._record_type(0, True)
        assert True in ex._outcomes[0]
        ex._record_type(999, True)

    def test_has_valid_contract(self):
        ex, _, _, _ = self._make_executor()
        assert ex._has_valid_contract({"contract": "0x" + "a" * 40}) is True
        assert ex._has_valid_contract({}) is False

    @pytest.mark.asyncio
    async def test_buffer_log_and_flush(self):
        ex, _, _, db = self._make_executor()
        await ex.buffer_log("0x" + "a" * 40, "transfer", "0x" + "f" * 64, True, "ok")
        await ex.flush()

    @pytest.mark.asyncio
    async def test_transfer_dry_run(self):
        ex, net, vb, _ = self._make_executor(dry_run=True)
        wallet = _make_wallet()
        result = await ex.transfer(wallet, "0x" + "c" * 40, 0.001)
        assert result is True

    @pytest.mark.asyncio
    async def test_transfer_success(self):
        ex, net, vb, _ = self._make_executor()
        net.send_transfer = AsyncMock(return_value="0x" + "f" * 64)
        wallet = _make_wallet()
        result = await ex.transfer(wallet, "0x" + "c" * 40, 0.001, gas_mult=1.5)
        assert result is True

    @pytest.mark.asyncio
    async def test_transfer_returns_hash(self):
        ex, net, vb, _ = self._make_executor()
        net.send_transfer = AsyncMock(return_value="")
        wallet = _make_wallet()
        result = await ex.transfer(wallet, "0x" + "c" * 40, 0.001)
        assert result is False

    @pytest.mark.asyncio
    async def test_transfer_exception(self):
        ex, net, vb, _ = self._make_executor()
        net.send_transfer = AsyncMock(side_effect=RuntimeError("net err"))
        wallet = _make_wallet()
        result = await ex.transfer(wallet, "0x" + "c" * 40, 0.001)
        assert result is False

    @pytest.mark.asyncio
    async def test_contract_call_dry_run(self):
        ex, net, vb, _ = self._make_executor(dry_run=True)
        wallet = _make_wallet()
        result = await ex._contract_call(
            wallet, {"type": "vibevibe_swap", "method": "swap", "contract": "0x" + "a" * 40}, amount=0.01
        )
        assert result is True

    @pytest.mark.asyncio
    async def test_contract_call_success(self):
        ex, net, vb, _ = self._make_executor()
        net.w3 = MagicMock()
        net.w3.to_wei.return_value = 1000000
        vb.call_method = AsyncMock(return_value="0x" + "f" * 64)
        wallet = _make_wallet()
        result = await ex._contract_call(
            wallet, {"type": "vibevibe_swap", "method": "swap", "contract": "0x" + "a" * 40},
            amount=0.01, gas_mult=1.2
        )
        assert result is True

    @pytest.mark.asyncio
    async def test_contract_call_no_hash(self):
        ex, net, vb, _ = self._make_executor()
        net.w3 = MagicMock()
        net.w3.to_wei.return_value = 1000000
        vb.call_method = AsyncMock(return_value="")
        wallet = _make_wallet()
        result = await ex._contract_call(
            wallet, {"type": "vibevibe_swap", "method": "swap", "contract": "0x" + "a" * 40}, amount=0.01
        )
        assert result is False

    @pytest.mark.asyncio
    async def test_contract_call_exception(self):
        ex, net, vb, _ = self._make_executor()
        net.w3 = MagicMock()
        net.w3.to_wei.return_value = 1000000
        vb.call_method = AsyncMock(side_effect=RuntimeError("vb err"))
        wallet = _make_wallet()
        result = await ex._contract_call(
            wallet, {"type": "vibevibe_swap", "method": "swap", "contract": "0x" + "a" * 40}, amount=0.01
        )
        assert result is False

    @pytest.mark.asyncio
    async def test_contract_call_no_amount(self):
        ex, net, vb, _ = self._make_executor()
        vb.call_method = AsyncMock(return_value="0x" + "f" * 64)
        wallet = _make_wallet()
        result = await ex._contract_call(
            wallet, {"type": "vibevibe_mint", "method": "mint", "contract": "0x" + "a" * 40}
        )
        assert result is True

    @pytest.mark.asyncio
    async def test_execute_action_transfer_random(self):
        ex, net, vb, _ = self._make_executor()
        net.send_transfer = AsyncMock(return_value="0x" + "f" * 64)
        wallet = _make_wallet()
        addrs = [wallet["address"], "0x" + "c" * 40]
        result = await ex.execute_action(wallet, addrs)
        assert result is True

    @pytest.mark.asyncio
    async def test_execute_action_transfer_target(self):
        actions = [{"type": "transfer", "target": "0x" + "d" * 40}]
        ex, net, vb, _ = self._make_executor(actions=actions)
        net.send_transfer = AsyncMock(return_value="0x" + "f" * 64)
        wallet = _make_wallet()
        result = await ex.execute_action(wallet, [wallet["address"]])
        assert result is True

    @pytest.mark.asyncio
    async def test_execute_action_transfer_empty_target(self):
        actions = [{"type": "transfer", "target": ""}]
        ex, net, vb, _ = self._make_executor(actions=actions)
        wallet = _make_wallet()
        result = await ex.execute_action(wallet, [wallet["address"]])
        assert result is False

    @pytest.mark.asyncio
    async def test_execute_action_transfer_no_candidates(self):
        actions = [{"type": "transfer", "target": "random_wallet"}]
        ex, net, vb, _ = self._make_executor(actions=actions)
        wallet = _make_wallet()
        result = await ex.execute_action(wallet, [wallet["address"]])
        assert result is False

    @pytest.mark.asyncio
    async def test_execute_action_contract_with_amount(self):
        actions = [
            {"type": "vibevibe_swap", "method": "swap", "contract": "0x" + "a" * 40,
             "min_amount": 0.001, "max_amount": 0.01}
        ]
        ex, net, vb, _ = self._make_executor(actions=actions)
        net.w3 = MagicMock()
        net.w3.to_wei.return_value = 1000000
        vb.call_method = AsyncMock(return_value="0x" + "f" * 64)
        wallet = _make_wallet()
        result = await ex.execute_action(wallet, [])
        assert result is True

    @pytest.mark.asyncio
    async def test_execute_action_contract_no_amount(self):
        actions = [{"type": "vibevibe_mint", "method": "mint", "contract": "0x" + "a" * 40}]
        ex, net, vb, _ = self._make_executor(actions=actions)
        vb.call_method = AsyncMock(return_value="0x" + "f" * 64)
        wallet = _make_wallet()
        result = await ex.execute_action(wallet, [])
        assert result is True

    @pytest.mark.asyncio
    async def test_execute_action_fallback_no_contract(self):
        actions = [{"type": "vibevibe_swap", "method": "swap", "contract": "0x" + "0" * 40}]
        ex, net, vb, _ = self._make_executor(actions=actions)
        net.send_transfer = AsyncMock(return_value="0x" + "f" * 64)
        wallet = _make_wallet()
        addrs = [wallet["address"], "0x" + "c" * 40]
        result = await ex.execute_action(wallet, addrs)
        assert result is True

    @pytest.mark.asyncio
    async def test_execute_action_unknown_type(self):
        actions = [{"type": "something_weird"}]
        ex, net, vb, _ = self._make_executor(actions=actions)
        wallet = _make_wallet()
        result = await ex.execute_action(wallet, [])
        assert result is False

    @pytest.mark.asyncio
    async def test_execute_action_with_profile(self):
        from core.behavior import WalletProfile
        ex, net, vb, _ = self._make_executor()
        net.send_transfer = AsyncMock(return_value="0x" + "f" * 64)
        wallet = _make_wallet()
        prof = WalletProfile(
            activity=1.0, amount_min_mul=1.0, amount_max_mul=1.0,
            odd_amount_prob=0.5, action_multipliers={}
        )
        result = await ex.execute_action(wallet, [wallet["address"], "0x" + "c" * 40], profile=prof)
        assert result is True


# ===================================================================
# core/ui.py tests
# ===================================================================

class TestUISafeAndHelpers:
    def test_safe_wide(self):
        import core.ui as ui_mod
        with patch.object(ui_mod, "_WIDE", True):
            assert ui_mod._safe("hello") == "hello"

    def test_safe_narrow(self):
        import core.ui as ui_mod
        with patch.object(ui_mod, "_WIDE", False):
            result = ui_mod._safe("\u2588hello\u2591")
            assert "=" in result

    def test_ansi_with_style(self):
        import core.ui as ui_mod
        result = ui_mod._ansi("RED BOLD", "test")
        assert "test" in result
        assert "\x1b[" in result

    def test_ansi_no_style(self):
        import core.ui as ui_mod
        assert ui_mod._ansi(None, "hello") == "hello"

    def test_gradient_plain_not_tty(self):
        import core.ui as ui_mod
        with patch.object(ui_mod, "_TTY", False):
            assert ui_mod._gradient_plain("abc") == "abc"

    def test_gradient_plain_empty(self):
        import core.ui as ui_mod
        with patch.object(ui_mod, "_TTY", True):
            assert ui_mod._gradient_plain("") == ""

    def test_gradient_plain(self):
        import core.ui as ui_mod
        with patch.object(ui_mod, "_TTY", True):
            result = ui_mod._gradient_plain("ab")
            assert "\x1b[38;5;" in result


class TestUIRich:
    def _make_ui_rich(self):
        import core.ui as ui_mod
        u = ui_mod.UI(use_rich=True)
        u.console = MagicMock()
        return u

    def _make_ui_plain(self):
        import core.ui as ui_mod
        u = ui_mod.UI(use_rich=False)
        return u

    def test_print_rich(self):
        u = self._make_ui_rich()
        u.print("hello", style="bold")
        u.console.print.assert_called_once()

    def test_print_plain(self, capsys):
        u = self._make_ui_plain()
        u.print("hello", style="RED")
        out = capsys.readouterr().out
        assert "hello" in out

    def test_print_plain_no_style(self, capsys):
        u = self._make_ui_plain()
        u.print("hello")
        assert "hello" in capsys.readouterr().out

    def test_out_ok_rich(self):
        u = self._make_ui_rich()
        u.out_ok("done")

    def test_out_ok_plain(self, capsys):
        u = self._make_ui_plain()
        u.out_ok("done")
        assert "done" in capsys.readouterr().out

    def test_hint_rich(self):
        u = self._make_ui_rich()
        u.hint("tip")

    def test_hint_plain(self, capsys):
        u = self._make_ui_plain()
        u.hint("tip")
        assert "tip" in capsys.readouterr().out

    def test_toast_rich_ok(self):
        u = self._make_ui_rich()
        u.toast("msg", "ok")

    def test_toast_rich_err(self):
        u = self._make_ui_rich()
        u.toast("msg", "err")

    def test_toast_rich_warn(self):
        u = self._make_ui_rich()
        u.toast("msg", "warn")

    def test_toast_rich_default(self):
        u = self._make_ui_rich()
        u.toast("msg", "other")

    def test_toast_plain(self, capsys):
        u = self._make_ui_plain()
        u.toast("msg", "ok")
        assert "msg" in capsys.readouterr().out

    def test_toast_plain_err(self, capsys):
        u = self._make_ui_plain()
        u.toast("msg", "err")
        assert "msg" in capsys.readouterr().out

    def test_toast_plain_warn(self, capsys):
        u = self._make_ui_plain()
        u.toast("msg", "warn")
        assert "msg" in capsys.readouterr().out

    def test_toast_plain_default(self, capsys):
        u = self._make_ui_plain()
        u.toast("msg")
        assert "msg" in capsys.readouterr().out

    def test_menu_panel_rich(self):
        u = self._make_ui_rich()
        u.menu_panel("Title", "Body")

    def test_menu_panel_plain(self, capsys):
        u = self._make_ui_plain()
        u.menu_panel("Title", "Line1\nLine2")
        assert "Title" in capsys.readouterr().out

    def test_panel_rich(self):
        u = self._make_ui_rich()
        u.panel("Title", "Body", color="cyan", width=70)

    def test_panel_plain(self, capsys):
        u = self._make_ui_plain()
        u.panel("Title", "Line1\nLine2", width=70)
        assert "Title" in capsys.readouterr().out

    def test_frame(self):
        u = self._make_ui_plain()
        f = u.frame()
        assert isinstance(f, str)

    def test_gradient_rich(self):
        u = self._make_ui_rich()
        result = u.gradient("abc")
        assert result is not None

    def test_gradient_rich_custom_ramp(self):
        u = self._make_ui_rich()
        result = u.gradient("ab", ramp_rich=["red", "blue"])
        assert result is not None

    def test_gradient_plain(self):
        import core.ui as ui_mod
        u = self._make_ui_plain()
        with patch.object(ui_mod, "_TTY", False):
            result = u.gradient("abc")
            assert result == "abc"

    def test_dot_green(self):
        u = self._make_ui_plain()
        assert "\x1b[" in u.dot(100)

    def test_dot_yellow(self):
        u = self._make_ui_plain()
        assert "\x1b[" in u.dot(800)

    def test_dot_red_high(self):
        u = self._make_ui_plain()
        assert "\x1b[" in u.dot(2000)

    def test_dot_red_negative(self):
        u = self._make_ui_plain()
        assert "\x1b[" in u.dot(-1)

    def test_dot_none(self):
        u = self._make_ui_plain()
        assert "\x1b[" in u.dot(None)

    def test_bar(self):
        u = self._make_ui_plain()
        b = u.bar(0.5)
        assert "%" in b

    def test_bar_full(self):
        u = self._make_ui_plain()
        assert "100.0%" in u.bar(1.5)

    def test_bar_zero(self):
        u = self._make_ui_plain()
        assert "0.0%" in u.bar(-0.5)

    def test_divider(self):
        u = self._make_ui_plain()
        u.divider()

    def test_divider_rich(self):
        u = self._make_ui_rich()
        u.divider()

    def test_menu_key_rich(self):
        u = self._make_ui_rich()
        result = u.menu_key("Start", "S")
        assert "S" in result

    def test_menu_key_plain(self):
        u = self._make_ui_plain()
        result = u.menu_key("Start", "S")
        assert "(S)" in result


class TestUIBanner:
    def test_banner_rich(self):
        import core.ui as ui_mod
        u = ui_mod.UI(use_rich=True)
        u.console = MagicMock()
        u.banner("TITLE", "sub\nline")

    def test_banner_rich_no_sub(self):
        import core.ui as ui_mod
        u = ui_mod.UI(use_rich=True)
        u.console = MagicMock()
        u.banner("TITLE")

    def test_banner_plain(self, capsys):
        import core.ui as ui_mod
        u = ui_mod.UI(use_rich=False)
        u.banner("TITLE", "sub\nline")
        assert "TITLE" in capsys.readouterr().out

    def test_banner_plain_no_sub(self, capsys):
        import core.ui as ui_mod
        u = ui_mod.UI(use_rich=False)
        u.banner("TITLE")
        assert "TITLE" in capsys.readouterr().out


class TestUITypewrite:
    def test_typewrite_tty(self):
        import core.ui as ui_mod
        u = ui_mod.UI(use_rich=False)
        with patch.object(ui_mod, "_TTY", True):
            u.typewrite("hello", speed=0.0001)

    def test_typewrite_non_tty(self, capsys):
        import core.ui as ui_mod
        u = ui_mod.UI(use_rich=False)
        with patch.object(ui_mod, "_TTY", False):
            u.typewrite("hello")
        assert "hello" in capsys.readouterr().out


class TestUISpinner:
    @pytest.mark.asyncio
    async def test_spinner_rich(self):
        import core.ui as ui_mod
        u = ui_mod.UI(use_rich=True)
        u.console = MagicMock()

        async def work():
            return 42

        mock_status = MagicMock()
        mock_status.__enter__ = MagicMock(return_value=mock_status)
        mock_status.__exit__ = MagicMock(return_value=False)

        with patch("rich.status.Status", return_value=mock_status):
            result = await u.spinner("loading", work())
        assert result == 42

    @pytest.mark.asyncio
    async def test_spinner_plain_tty(self):
        import core.ui as ui_mod
        u = ui_mod.UI(use_rich=False)

        async def work():
            return 99

        with patch.object(ui_mod, "_TTY", True), patch("time.monotonic", return_value=0.5):
            result = await u.spinner("load", work())
        assert result == 99

    @pytest.mark.asyncio
    async def test_spinner_plain_non_tty(self):
        import core.ui as ui_mod
        u = ui_mod.UI(use_rich=False)

        async def work():
            return 7

        with patch.object(ui_mod, "_TTY", False), patch("time.monotonic", return_value=0.5):
            result = await u.spinner("wait", work())
        assert result == 7


class TestUITable:
    def test_table_rich(self):
        import core.ui as ui_mod
        u = ui_mod.UI(use_rich=True)
        u.console = MagicMock()
        u.table("T", ["A", "B"], [["1", "2"]])

    def test_table_plain(self, capsys):
        import core.ui as ui_mod
        u = ui_mod.UI(use_rich=False)
        u.table("T", ["A", "B"], [["1", "2"]])
        out = capsys.readouterr().out
        assert "T" in out
        assert "1" in out


class TestUILiveTable:
    @pytest.mark.asyncio
    async def test_live_table_rich(self):
        import core.ui as ui_mod
        u = ui_mod.UI(use_rich=True)
        u.console = MagicMock()
        call_count = 0
        stop_flag = [False]

        def render():
            return [["row1"]]

        def stop():
            nonlocal call_count
            call_count += 1
            if call_count > 1:
                stop_flag[0] = True
                return True
            return False

        mock_live = MagicMock()
        mock_live.__enter__ = MagicMock(return_value=mock_live)
        mock_live.__exit__ = MagicMock(return_value=False)

        with patch("rich.live.Live", return_value=mock_live), \
             patch.object(ui_mod, "asyncio_sleep", new_callable=AsyncMock):
            await u.live_table("T", render, ["H"], period=0.01, stop=stop, caption=lambda: "cap")

    @pytest.mark.asyncio
    async def test_live_table_plain_non_tty(self):
        import core.ui as ui_mod
        u = ui_mod.UI(use_rich=False)
        call_count = 0

        def render():
            return [["r"]]

        def stop():
            nonlocal call_count
            call_count += 1
            return call_count > 1

        with patch.object(ui_mod, "_TTY", False), \
             patch.object(ui_mod, "asyncio_sleep", new_callable=AsyncMock):
            await u.live_table("T", render, ["H"], period=0.01, stop=stop)

    @pytest.mark.asyncio
    async def test_live_table_plain_tty(self):
        import core.ui as ui_mod
        u = ui_mod.UI(use_rich=False)
        call_count = 0

        def render():
            return [["r"]]

        def stop():
            nonlocal call_count
            call_count += 1
            return call_count > 1

        with patch.object(ui_mod, "_TTY", True), \
             patch.object(ui_mod, "asyncio_sleep", new_callable=AsyncMock):
            await u.live_table("T", render, ["H"], period=0.01, stop=stop, caption="status")

    @pytest.mark.asyncio
    async def test_live_table_callable_caption(self):
        import core.ui as ui_mod
        u = ui_mod.UI(use_rich=False)
        call_count = 0

        def render():
            return [["r"]]

        def stop():
            nonlocal call_count
            call_count += 1
            return call_count > 1

        with patch.object(ui_mod, "_TTY", False), \
             patch.object(ui_mod, "asyncio_sleep", new_callable=AsyncMock):
            await u.live_table("T", render, ["H"], period=0.01, stop=stop, caption=lambda: "dynamic")

    @pytest.mark.asyncio
    async def test_live_table_no_caption(self):
        import core.ui as ui_mod
        u = ui_mod.UI(use_rich=False)
        call_count = 0

        def render():
            return [["r"]]

        def stop():
            nonlocal call_count
            call_count += 1
            return call_count > 1

        with patch.object(ui_mod, "_TTY", False), \
             patch.object(ui_mod, "asyncio_sleep", new_callable=AsyncMock):
            await u.live_table("T", render, ["H"], period=0.01, stop=stop, caption=None)


class TestUIPollKeys:
    def test_poll_keys_non_tty(self):
        import core.ui as ui_mod
        u = ui_mod.UI(use_rich=False)
        with patch.object(ui_mod, "_TTY", False):
            assert u.poll_keys() == ""

    def test_poll_keys_tty_windows(self):
        import core.ui as ui_mod
        u = ui_mod.UI(use_rich=False)
        mock_msvcrt = MagicMock()
        mock_msvcrt.kbhit.side_effect = [True, False]
        mock_msvcrt.getch.return_value = b"a"
        with patch.object(ui_mod, "_TTY", True), \
             patch.dict("sys.modules", {"msvcrt": mock_msvcrt}):
            result = u.poll_keys()
            assert "a" in result

    def test_poll_keys_tty_windows_special_key(self):
        import core.ui as ui_mod
        u = ui_mod.UI(use_rich=False)
        mock_msvcrt = MagicMock()
        call_count = [0]
        def kbhit_side():
            call_count[0] += 1
            return call_count[0] <= 2
        mock_msvcrt.kbhit.side_effect = kbhit_side
        mock_msvcrt.getch.side_effect = [b"\x00", b"\x48"]
        with patch.object(ui_mod, "_TTY", True), \
             patch.dict("sys.modules", {"msvcrt": mock_msvcrt}):
            result = u.poll_keys()
            assert isinstance(result, str)

    def test_poll_keys_tty_posix(self):
        import core.ui as ui_mod
        u = ui_mod.UI(use_rich=False)
        mock_select = MagicMock()
        mock_select.select.return_value = ([True], [], [])
        mock_stdin = MagicMock()
        mock_stdin.read.return_value = "x"
        with patch.object(ui_mod, "_TTY", True), \
             patch.dict("sys.modules", {"msvcrt": None, "select": mock_select}), \
             patch.object(ui_mod, "sys", MagicMock(stdin=mock_stdin)):
            result = u.poll_keys()
            assert "x" in result

    def test_poll_keys_tty_posix_no_data(self):
        import core.ui as ui_mod
        u = ui_mod.UI(use_rich=False)
        mock_select = MagicMock()
        mock_select.select.return_value = ([], [], [])
        with patch.object(ui_mod, "_TTY", True), \
             patch.dict("sys.modules", {"msvcrt": None, "select": mock_select}):
            result = u.poll_keys()
            assert result == ""

    def test_poll_keys_exception(self):
        import core.ui as ui_mod
        u = ui_mod.UI(use_rich=False)
        mock_msvcrt = MagicMock()
        mock_msvcrt.kbhit.side_effect = ImportError("no msvcrt")
        mock_select = MagicMock()
        mock_select.select.side_effect = OSError("no select")
        with patch.object(ui_mod, "_TTY", True), \
             patch.dict("sys.modules", {"msvcrt": mock_msvcrt, "select": mock_select}):
            result = u.poll_keys()
            assert result == ""


class TestUIAsyncioSleep:
    @pytest.mark.asyncio
    async def test_asyncio_sleep(self):
        import core.ui as ui_mod
        await ui_mod.asyncio_sleep(0)


class TestWideOk:
    def test_wide_ok_tty(self):
        import core.ui as ui_mod
        with patch.object(ui_mod, "_TTY", True):
            assert ui_mod._wide_ok() is True

    def test_wide_ok_encoding(self):
        import core.ui as ui_mod
        mock_stdout = MagicMock(encoding="utf-8")
        with patch.object(ui_mod, "_TTY", False), \
             patch("sys.stdout", mock_stdout):
            assert ui_mod._wide_ok() is True

    def test_wide_ok_bad_encoding(self):
        import core.ui as ui_mod
        mock_stdout = MagicMock(encoding="cp1251")
        with patch.object(ui_mod, "_TTY", False), \
             patch("sys.stdout", mock_stdout):
            assert ui_mod._wide_ok() is False


class TestUIAvailable:
    def test_available(self):
        import core.ui as ui_mod
        with patch.object(ui_mod, "_TTY", True), \
             patch.object(ui_mod, "_HAS_RICH", True):
            assert ui_mod.available() is True

    def test_is_tty(self):
        import core.ui as ui_mod
        with patch.object(ui_mod, "_TTY", True):
            assert ui_mod._is_tty() is True


class TestUISpinnerFrames:
    def test_braille_frames(self):
        import core.ui as ui_mod
        with patch.object(ui_mod, "_TTY", True), \
             patch.object(ui_mod, "_WIDE", True):
            b = "\u280b\u2819\u2839\u2838\u283c\u2834\u2826\u2827\u280f\u280e"
            frames = list(b)
            assert len(frames) == 10


class TestUIGradientRich:
    def test_gradient_plain_narrow_text(self):
        import core.ui as ui_mod
        u = ui_mod.UI(use_rich=True)
        with patch.object(ui_mod, "_TTY", True):
            result = u.gradient("a")
            assert result is not None

    def test_gradient_plain_uses_rich_branch(self):
        import core.ui as ui_mod
        u = ui_mod.UI(use_rich=True)
        result = u.gradient("")
        assert result is not None


class TestUIPanelEscapes:
    def test_panel_rich_escapes(self):
        import core.ui as ui_mod
        u = ui_mod.UI(use_rich=True)
        u.console = MagicMock()
        u.panel("T", "body with [brackets]", color="red", width=80)

    def test_menu_panel_plain_wide(self, capsys):
        import core.ui as ui_mod
        with patch.object(ui_mod, "_TTY", False):
            u = ui_mod.UI(use_rich=False)
            u.menu_panel("Title", "body")
