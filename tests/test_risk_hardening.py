"""Tests for the risk-layer hardening: drawdown halt, broker reconciliation, exit rules,
Eastern-time reset, exit processing, and the PreToolUse guard hook. No network."""
import json
import subprocess
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock

import pytest

ROOT = Path(__file__).parent.parent
GUARD = ROOT / ".claude" / "hooks" / "guard.py"


@pytest.fixture
def rm(tmp_path):
    from agent.core.risk_manager import RiskManager
    return RiskManager(state_path=tmp_path / "state.json")


# ── drawdown halt ───────────────────────────────────────────────

class TestDrawdownHalt:
    def test_no_halt_on_small_loss(self, rm):
        rm.open_position("AAPL", 10.0, 1.0)
        assert rm.check_drawdown({"AAPL": 9.0}) is False

    def test_halts_when_equity_falls_limit_below_peak(self, rm):
        rm.open_position("AAPL", 10.0, 5.0)  # $50 of $100 invested
        # position drops 40% -> equity 100 - 20 = 80 = 20% below the $100 peak
        assert rm.check_drawdown({"AAPL": 6.0}) is True
        assert rm.drawdown_halted

    def test_halt_blocks_new_buys(self, rm):
        rm.open_position("AAPL", 10.0, 5.0)
        rm.check_drawdown({"AAPL": 6.0})
        allowed, reason, amount = rm.can_buy("MSFT", 100.0, 0.99)
        assert not allowed and "drawdown" in reason.lower() and amount == 0

    def test_peak_ratchets_up_so_drawdown_is_from_peak_not_start(self, rm):
        rm.open_position("AAPL", 10.0, 5.0)
        rm.check_drawdown({"AAPL": 20.0})           # equity 150, new peak
        assert rm.peak_equity == pytest.approx(150.0)
        assert rm.check_drawdown({"AAPL": 11.0}) is True  # equity 105 = 30% below 150

    def test_halt_survives_restart(self, tmp_path):
        from agent.core.risk_manager import RiskManager
        path = tmp_path / "state.json"
        first = RiskManager(state_path=path)
        first.open_position("AAPL", 10.0, 5.0)
        first.check_drawdown({"AAPL": 6.0})
        second = RiskManager(state_path=path)  # simulated restart
        assert second.drawdown_halted
        assert second.can_buy("MSFT", 100.0, 0.99)[0] is False

    def test_emergency_resume_does_not_clear_drawdown_halt(self, rm):
        rm.open_position("AAPL", 10.0, 5.0)
        rm.check_drawdown({"AAPL": 6.0})
        rm.deactivate_emergency_stop()
        assert rm.drawdown_halted

    def test_reset_is_explicit_and_persists(self, tmp_path):
        from agent.core.risk_manager import RiskManager
        path = tmp_path / "state.json"
        rm1 = RiskManager(state_path=path)
        rm1.open_position("AAPL", 10.0, 5.0)
        rm1.check_drawdown({"AAPL": 6.0})
        rm1.reset_drawdown_halt(new_peak=80.0)
        assert not RiskManager(state_path=path).drawdown_halted

    def test_corrupt_state_file_fails_safe(self, tmp_path):
        from agent.core.risk_manager import RiskManager
        path = tmp_path / "state.json"
        path.write_text("{not json", encoding="utf-8")
        assert RiskManager(state_path=path).drawdown_halted is True

    def test_metrics_report_halt(self, rm):
        rm.open_position("AAPL", 10.0, 5.0)
        rm.check_drawdown({"AAPL": 6.0})
        m = rm.get_metrics({"AAPL": 6.0})
        assert not m.can_trade and "drawdown" in m.block_reason.lower()

    def test_config_value_is_15pct(self):
        from agent.config import CONFIG
        assert CONFIG["risk"]["max_drawdown_pct"] == 0.15

    def test_schema_defaults_when_key_missing(self):
        from agent.config_schema import RiskConfig
        base = dict(total_fund=100, max_position_pct=0.1, max_sector_pct=0.4, min_cash_reserve_pct=0.2,
                    max_daily_loss_pct=0.05, max_trade_loss_pct=0.02, trailing_stop_pct=0.01,
                    min_confidence_score=0.4, max_open_positions=5, min_holding_minutes=5,
                    circuit_breaker_enabled=True)
        assert RiskConfig(**base).max_drawdown_pct == 0.15


# ── reconciliation ──────────────────────────────────────────────

class TestReconcile:
    def test_drops_position_when_broker_holds_others_and_restores_cash(self, rm):
        rm.open_position("AAPL", 10.0, 2.0)
        rm.open_position("MSFT", 10.0, 1.0)
        cash_before = rm.cash
        out = rm.reconcile_positions([{"symbol": "MSFT", "qty": 1.0}])
        assert out["dropped"] == ["AAPL"] and "AAPL" not in rm.positions
        assert rm.cash == pytest.approx(cash_before + 20.0)

    def test_empty_broker_list_never_wipes_tracked_positions(self, rm):
        rm.open_position("AAPL", 10.0, 2.0)
        out = rm.reconcile_positions([])
        assert out["blocked_empty"] == ["AAPL"] and out["dropped"] == []
        assert "AAPL" in rm.positions

    def test_symbols_are_normalized(self, rm):
        rm.open_position("AAPL", 10.0, 2.0)
        out = rm.reconcile_positions([{"symbol": " aapl ", "qty": 2.0}])
        assert out == {"dropped": [], "resized": [], "untracked": [], "blocked_empty": []}

    def test_broker_quantity_wins(self, rm):
        rm.open_position("AAPL", 10.0, 2.0)
        out = rm.reconcile_positions([{"symbol": "AAPL", "qty": 1.5}])
        assert out["resized"] == ["AAPL"] and rm.positions["AAPL"].quantity == 1.5

    def test_untracked_broker_positions_are_reported_not_adopted(self, rm):
        out = rm.reconcile_positions([{"symbol": "TSLA", "qty": 3.0}])
        assert out["untracked"] == ["TSLA"] and "TSLA" not in rm.positions

    def test_matching_positions_untouched(self, rm):
        rm.open_position("AAPL", 10.0, 2.0)
        out = rm.reconcile_positions([{"symbol": "AAPL", "qty": 2.0}])
        assert out == {"dropped": [], "resized": [], "untracked": [], "blocked_empty": []}


# ── restart / state robustness ──────────────────────────────────

class TestStateRobustness:
    def test_realized_losses_persist_across_restart_and_count_toward_halt(self, tmp_path):
        from agent.core.risk_manager import RiskManager
        path = tmp_path / "state.json"
        a = RiskManager(state_path=path)
        for sym in ("A", "B", "C"):            # three stop-outs of $6 each = $18 realized
            a.open_position(sym, 10.0, 2.0)
            a.close_position(sym, 7.0, "stop_loss")
        assert a.realized_pnl == pytest.approx(-18.0)
        b = RiskManager(state_path=path)       # restart: positions gone, cash must reflect losses
        assert b.cash == pytest.approx(82.0)
        assert b.equity() == pytest.approx(82.0)
        assert b.check_drawdown({}) is True    # 18% below the $100 peak

    def test_gains_then_restart_is_not_a_spurious_halt(self, tmp_path):
        from agent.core.risk_manager import RiskManager
        path = tmp_path / "state.json"
        a = RiskManager(state_path=path)
        a.open_position("A", 10.0, 5.0)
        a.close_position("A", 14.0, "target_reached")   # +$20
        a.check_drawdown({})
        b = RiskManager(state_path=path)
        assert b.equity() == pytest.approx(120.0)
        assert b.check_drawdown({}) is False

    @pytest.mark.parametrize("content", [
        "[]", "null", '{"peak_equity": null}', '{"peak_equity": NaN}', '{"peak_equity": -5}',
        '{"peak_equity": 0}', '{"peak_equity": 100, "realized_pnl": Infinity}', "{bad",
    ])
    def test_bad_state_files_halt_instead_of_crashing_or_failing_open(self, tmp_path, content):
        from agent.core.risk_manager import RiskManager
        path = tmp_path / "state.json"
        path.write_text(content, encoding="utf-8")
        rm_ = RiskManager(state_path=path)
        assert rm_.drawdown_halted is True

    def test_missing_state_file_starts_fresh_without_halt(self, tmp_path):
        from agent.core.risk_manager import RiskManager
        assert RiskManager(state_path=tmp_path / "none.json").drawdown_halted is False

    def test_save_failure_is_flagged_not_silent(self, rm, monkeypatch):
        import os as _os
        monkeypatch.setattr(_os, "replace", lambda *a, **k: (_ for _ in ()).throw(OSError("disk full")))
        rm.open_position("AAPL", 10.0, 5.0)
        rm.check_drawdown({"AAPL": 6.0})
        assert rm.drawdown_halted and rm.state_save_failed

    def test_missing_price_uses_last_mark_not_entry(self, rm):
        rm.open_position("AAPL", 10.0, 5.0)
        rm.equity({"AAPL": 6.0})                       # records a mark
        assert rm.equity({}) == pytest.approx(rm.cash + 5.0 * 6.0)   # feed outage: keep the loss

    def test_missing_state_is_flagged_once_then_file_exists(self, tmp_path):
        from agent.core.risk_manager import RiskManager
        path = tmp_path / "s.json"
        assert RiskManager(state_path=path).state_missing is True
        assert path.exists()
        assert RiskManager(state_path=path).state_missing is False

    def test_reconcile_seeds_marks_from_broker_prices(self, rm):
        rm.open_position("AAPL", 10.0, 5.0)
        rm.reconcile_positions([{"symbol": "AAPL", "qty": 5.0, "current_price": 6.0}])
        assert rm.equity({}) == pytest.approx(rm.cash + 30.0)   # loss visible with no live ticks

    @pytest.mark.parametrize("bad", [float("nan"), float("inf"), 0.0, -1.0])
    def test_reset_rejects_bad_peak(self, rm, bad):
        with pytest.raises(ValueError):
            rm.reset_drawdown_halt(new_peak=bad)

    def test_concurrent_saves_leave_valid_json(self, rm):
        import threading
        ts = [threading.Thread(target=rm._save_state) for _ in range(20)]
        [t.start() for t in ts]
        [t.join() for t in ts]
        data = json.loads(rm._state_path.read_text(encoding="utf-8"))
        assert data["peak_equity"] > 0


# ── exit rules and time ─────────────────────────────────────────

class TestExitRulesAndTime:
    def test_min_hold_exempt_only_for_exact_stop_reasons(self, rm):
        rm.open_position("AAPL", 10.0, 1.0)
        assert rm.can_sell("AAPL", "stop_loss")[0]
        assert rm.can_sell("AAPL", "trailing_stop_loss")[0]
        assert not rm.can_sell("AAPL", "AI signal about stop_loss levels")[0]
        assert not rm.can_sell("AAPL", "target_reached")[0]

    def test_daily_reset_follows_eastern_date_not_utc(self, rm):
        import pytz
        et = pytz.timezone("America/New_York")
        # 23:30 ET on Jan 10 is already Jan 11 in UTC
        rm._now = lambda: et.localize(datetime(2026, 1, 10, 23, 30))
        assert rm._today() == date(2026, 1, 10)
        rm._last_reset_date = date(2026, 1, 10)
        rm.daily_pnl = -3.0
        rm.get_metrics()
        assert rm.daily_pnl == -3.0          # still the same ET trading day: no reset
        rm._now = lambda: et.localize(datetime(2026, 1, 11, 0, 5))
        rm.get_metrics()
        assert rm.daily_pnl == 0.0           # ET midnight passed: reset


# ── main loop exit processing ───────────────────────────────────

@pytest.fixture
def agent(monkeypatch):
    for mod in ("yfinance", "alpaca", "alpaca.trading", "alpaca.trading.client",
                "alpaca.data", "transformers", "torch"):
        monkeypatch.setitem(sys.modules, mod, MagicMock())
    from agent.main import StockSageAgent  # a missing dependency must fail, not silently skip
    a = StockSageAgent.__new__(StockSageAgent)
    a.risk_manager = MagicMock()
    a.execution = MagicMock()
    a.notifier = MagicMock()
    a._persist_positions = MagicMock()
    a._exit_alerted = set()
    return a


class TestProcessExit:
    def test_failed_sell_is_not_recorded_as_closed_and_alerts_once(self, agent):
        agent.risk_manager.positions = {"AAPL": object()}
        agent.execution.sell.return_value = MagicMock(status="error", error_msg="timeout")
        assert agent._process_exit("AAPL", 9.0, "stop_loss") is False
        assert agent._process_exit("AAPL", 9.0, "stop_loss") is False
        agent.risk_manager.close_position.assert_not_called()
        assert agent.notifier.error.call_count == 1          # no alert spam every cycle

    def test_rejected_sell_is_not_recorded_as_closed(self, agent):
        agent.risk_manager.positions = {"AAPL": object()}
        agent.execution.sell.return_value = MagicMock(status="rejected", error_msg="no")
        assert agent._process_exit("AAPL", 9.0, "stop_loss") is False
        agent.risk_manager.close_position.assert_not_called()

    def test_successful_sell_closes_and_persists(self, agent):
        agent.risk_manager.positions = {"AAPL": object()}
        agent.execution.sell.return_value = MagicMock(status="filled", error_msg="")
        agent.risk_manager.close_position.return_value = -1.0
        assert agent._process_exit("AAPL", 9.0, "stop_loss") is True
        agent.risk_manager.close_position.assert_called_once_with("AAPL", 9.0, "stop_loss")
        agent._persist_positions.assert_called_once()
        agent.notifier.stop_loss.assert_called_once()

    def test_position_already_closed_elsewhere_is_skipped(self, agent):
        agent.risk_manager.positions = {}
        assert agent._process_exit("AAPL", 9.0, "stop_loss") is False
        agent.execution.sell.assert_not_called()

    def test_position_gone_at_broker_is_cleared_only_after_confirmation(self, agent):
        agent.risk_manager.positions = {"AAPL": object()}
        agent.execution.sell.return_value = MagicMock(
            status="error", error_msg="position does not exist")
        agent.execution.get_positions_strict.return_value = [{"symbol": "MSFT", "qty": 1}]
        assert agent._process_exit("AAPL", 9.0, "stop_loss") is True
        agent.risk_manager.close_position.assert_called_once_with("AAPL", 9.0, "closed_at_broker")

    def test_error_text_alone_does_not_clear_a_position_still_held(self, agent):
        agent.risk_manager.positions = {"AAPL": object()}
        agent.execution.sell.return_value = MagicMock(
            status="error", error_msg="position does not exist")
        agent.execution.get_positions_strict.return_value = [{"symbol": "AAPL", "qty": 1}]
        assert agent._process_exit("AAPL", 9.0, "stop_loss") is False
        agent.risk_manager.close_position.assert_not_called()

    def test_unrelated_not_found_errors_never_clear_a_position(self, agent):
        agent.risk_manager.positions = {"AAPL": object()}
        agent.execution.sell.return_value = MagicMock(status="error", error_msg="asset not found")
        agent.execution.get_positions_strict.return_value = []
        assert agent._process_exit("AAPL", 9.0, "stop_loss") is False
        agent.risk_manager.close_position.assert_not_called()

    def test_unconfirmable_absence_does_not_clear(self, agent):
        agent.risk_manager.positions = {"AAPL": object()}
        agent.execution.sell.return_value = MagicMock(
            status="error", error_msg="position does not exist")
        agent.execution.get_positions_strict.side_effect = RuntimeError("429")
        assert agent._process_exit("AAPL", 9.0, "stop_loss") is False
        agent.risk_manager.close_position.assert_not_called()


class TestReconcileWithBroker:
    def test_failed_positions_call_does_not_touch_tracking(self, agent):
        agent.execution.get_account.return_value = {"equity": 1}
        agent.execution.get_positions_strict.side_effect = RuntimeError("429")
        agent._reconcile_with_broker()
        agent.risk_manager.reconcile_positions.assert_not_called()
        agent._persist_positions.assert_not_called()

    def test_unreachable_broker_skips_reconcile(self, agent):
        agent.execution.get_account.return_value = {}
        agent._reconcile_with_broker()
        agent.risk_manager.reconcile_positions.assert_not_called()

    def test_blocked_empty_raises_alert_and_does_not_persist_empty_set(self, agent):
        agent.execution.get_account.return_value = {"equity": 1}
        agent.execution.get_positions_strict.return_value = []
        agent.risk_manager.reconcile_positions.return_value = {
            "dropped": [], "resized": [], "untracked": [], "blocked_empty": ["AAPL"]}
        agent._reconcile_with_broker()
        agent.notifier.error.assert_called_once()
        assert "verify manually" in agent.notifier.error.call_args[0][0]


class TestCheckDrawdown:
    def test_alerts_once_when_halt_trips(self, agent):
        tick = MagicMock(ltp=6.0)
        agent.price_feed = MagicMock()
        agent.price_feed.get_all_ticks.return_value = {"AAPL": tick}
        agent.risk_manager = MagicMock(drawdown_halted=False, max_drawdown_pct=0.15,
                                       state_save_failed=False)
        agent.risk_manager.check_drawdown.return_value = True
        agent._check_drawdown()
        agent.notifier.error.assert_called_once()

    def test_no_alert_when_already_halted(self, agent):
        agent.price_feed = MagicMock()
        agent.price_feed.get_all_ticks.return_value = {}
        agent.risk_manager = MagicMock(drawdown_halted=True, max_drawdown_pct=0.15)
        agent.risk_manager.check_drawdown.return_value = True
        agent._check_drawdown()
        agent.notifier.error.assert_not_called()


class TestBuyRecheck:
    def test_halt_during_order_delay_cancels_the_buy(self, agent):
        agent.cfg = {"risk": {"total_fund": 100.0, "max_position_pct": 0.15,
                              "min_cash_reserve_pct": 0.2}}
        agent.decision_engine = MagicMock()
        rm_ = MagicMock(drawdown_halted=False, emergency_stop=False, cash=100.0)
        rm_.trading_allowed.return_value = False   # halt tripped while "sleeping"
        agent.risk_manager = rm_
        decision = MagicMock(action="BUY", symbol="AAPL", price=10.0, confidence=0.9)
        agent._execute_decision(decision)
        agent.decision_engine.apply_human_delay.assert_called_once()
        agent.execution.buy.assert_not_called()


# ── guard hook ──────────────────────────────────────────────────

def run_guard(tool, **tool_input):
    payload = json.dumps({"tool_name": tool, "tool_input": tool_input})
    return subprocess.run([sys.executable, "-I", str(GUARD)], input=payload,
                          capture_output=True, text=True)


class TestGuardHook:
    @pytest.mark.parametrize("tool,args", [
        ("Read", {"file_path": "/repo/.env"}),
        ("Read", {"file_path": "/repo/.env.production"}),
        ("Read", {"file_path": "/home/x/server.pem"}),
        ("Edit", {"file_path": "/repo/.env", "new_string": "x"}),
        ("Grep", {"pattern": "KEY", "path": "/repo/.env"}),
        ("Bash", {"command": "cat .env"}),
        ("Bash", {"command": "grep ALPACA .env"}),
        ("Bash", {"command": "source .env && echo hi"}),
        ("Bash", {"command": "printenv"}),
        ("Bash", {"command": "TRADING_MODE=live python -m agent.main"}),
        ("Bash", {"command": "python -m agent.main --i-understand-the-risk"}),
        ("Bash", {"command": "git push --force origin master"}),
        ("Bash", {"command": "git push -f"}),
        ("Write", {"file_path": "/repo/run.py", "content": "TRADING_MODE = 'live'"}),
        ("Edit", {"file_path": "/repo/a.py", "new_string": 'os.environ["TRADING_MODE"]="live"'}),
        ("Bash", {"command": "cat .env.production"}),
        ("Bash", {"command": "cp .env /tmp/x"}),
        ("Bash", {"command": "python -c \"print(open('.env').read())\""}),
        ("Bash", {"command": "git push origin +main"}),
        ("NotebookEdit", {"notebook_path": "/repo/n.ipynb", "new_source": "TRADING_MODE='live'"}),
        ("Read", {"file_path": "/repo/config/secrets.yaml"}),
    ])
    def test_blocks(self, tool, args):
        r = run_guard(tool, **args)
        assert r.returncode == 2, (tool, args, r.stdout, r.stderr)
        assert "BLOCKED" in r.stderr

    @pytest.mark.parametrize("tool,args", [
        ("Read", {"file_path": "/repo/.env.example"}),
        ("Read", {"file_path": "/repo/agent/main.py"}),
        ("Edit", {"file_path": "/repo/a.py", "new_string": "x = 1"}),
        ("Bash", {"command": "python -m pytest -q"}),
        ("Bash", {"command": "git push -u origin feat/x"}),
        ("Bash", {"command": "grep -rn risk agent/"}),
        ("Bash", {"command": "cat .env.example"}),
        ("Grep", {"pattern": "x", "path": "/repo/agent"}),
        ("Bash", {"command": "git push --force-with-lease origin feat/x"}),
        ("Bash", {"command": 'git commit -m "docs: mention TRADING_MODE=live and DROP TABLE"'}),
        ("Edit", {"file_path": "/repo/CLAUDE.md", "new_string": "Set TRADING_MODE=live only by hand"}),
        ("Read", {"file_path": "/repo/agent/secrets_helper.py"}),
        ("Bash", {"command": "cat .env.example"}),
    ])
    def test_allows(self, tool, args):
        r = run_guard(tool, **args)
        assert r.returncode == 0, (tool, args, r.stderr)

    def test_garbage_stdin_does_not_block(self):
        r = subprocess.run([sys.executable, "-I", str(GUARD)], input="not json",
                           capture_output=True, text=True)
        assert r.returncode == 0

    @pytest.mark.parametrize("payload", ["[1]", '{"tool_name": "Bash", "tool_input": "oops"}', "null"])
    def test_malformed_payloads_do_not_crash_into_a_nonblocking_error(self, payload):
        r = subprocess.run([sys.executable, "-I", str(GUARD)], input=payload,
                           capture_output=True, text=True)
        assert r.returncode in (0, 2)   # never 1 (non-blocking error)

    @staticmethod
    def _launcher_cmd():
        cfg = json.loads((ROOT / ".claude" / "settings.json").read_text(encoding="utf-8"))
        return next(h["command"] for g in cfg["hooks"]["PreToolUse"] for h in g["hooks"]
                    if "guard.py" in h["command"])

    def _run_launcher(self, payload, path=None):
        import os
        env = {"CLAUDE_PROJECT_DIR": str(ROOT), "PATH": path if path is not None else os.environ["PATH"]}
        return subprocess.run(["/bin/sh", "-c", self._launcher_cmd()], input=json.dumps(payload),
                              capture_output=True, text=True, env=env)

    def test_launcher_preserves_a_block_exit_code(self):
        """The exact settings.json command must exit 2 on a blocked call (an earlier version
        fell through to a second interpreter with empty stdin and exited 0)."""
        r = self._run_launcher({"tool_name": "Bash", "tool_input": {"command": "cat .env"}})
        assert r.returncode == 2, r.stderr
        assert "BLOCKED" in r.stderr

    def test_launcher_allows_a_safe_call(self):
        r = self._run_launcher({"tool_name": "Bash", "tool_input": {"command": "pytest -q"}})
        assert r.returncode == 0, r.stderr

    def test_launcher_fails_closed_without_python(self, tmp_path):
        r = self._run_launcher({"tool_name": "Bash", "tool_input": {"command": "ls"}},
                               path=str(tmp_path))
        assert r.returncode == 2, r.stderr

    def test_settings_registers_the_guard(self):
        cfg = json.loads((ROOT / ".claude" / "settings.json").read_text(encoding="utf-8"))
        hooks = cfg["hooks"]["PreToolUse"]
        cmds = [h["command"] for g in hooks for h in g["hooks"]]
        matchers = " ".join(g["matcher"] for g in hooks)
        assert any("guard.py" in c and "CLAUDE_PROJECT_DIR" in c for c in cmds)
        for t in ("Bash", "Edit", "Write", "Read", "Grep"):
            assert t in matchers
