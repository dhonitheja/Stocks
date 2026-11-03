"""Tests for the risk-layer hardening: drawdown halt, broker reconciliation, exit rules,
Eastern-time reset, exit processing, and the PreToolUse guard hook. No network."""
import json
import subprocess
import threading
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
        assert out["dropped"] == [] and out["resized"] == [] and out["untracked"] == [] \
            and out["blocked_empty"] == []

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
        assert out["dropped"] == [] and out["resized"] == [] and out["untracked"] == [] \
            and out["blocked_empty"] == []


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
    a._exit_lock = threading.Lock()
    a._last_reconcile_sig = None
    a._monitor_tick = 0
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
            "dropped": [], "resized": [], "untracked": [], "blocked_empty": ["AAPL"],
            "unbooked": [], "skipped_new": []}
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
        agent._check_drawdown = MagicMock()
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


# ═══ review round 3 ═════════════════════════════════════════════════════════

class TestDropBookingAndGrace:
    def test_dropped_position_books_loss_at_last_mark(self, rm):
        rm.open_position("AAPL", 10.0, 5.0)
        rm.open_position("MSFT", 10.0, 1.0)
        rm.equity({"AAPL": 6.0, "MSFT": 10.0})           # marks: AAPL 6.0
        out = rm.reconcile_positions([{"symbol": "MSFT", "qty": 1.0}])
        assert out["dropped"] == ["AAPL"] and out["unbooked"] == []
        assert rm.realized_pnl == pytest.approx(-20.0)    # (6-10)*5 booked, not hidden
        assert rm.daily_pnl == pytest.approx(-20.0)

    def test_drop_without_a_mark_is_booked_at_cost_and_flagged(self, rm):
        rm.open_position("AAPL", 10.0, 5.0)
        rm.open_position("MSFT", 10.0, 1.0)
        out = rm.reconcile_positions([{"symbol": "MSFT", "qty": 1.0}])
        assert out["unbooked"] == ["AAPL"] and rm.realized_pnl == 0.0

    def test_booked_loss_counts_toward_the_halt_after_restart(self, tmp_path):
        from agent.core.risk_manager import RiskManager
        path = tmp_path / "s.json"
        a = RiskManager(state_path=path)
        a.open_position("AAPL", 10.0, 5.0)
        a.open_position("MSFT", 10.0, 1.0)
        a.equity({"AAPL": 6.0, "MSFT": 10.0})
        a.reconcile_positions([{"symbol": "MSFT", "qty": 1.0}])
        b = RiskManager(state_path=path)
        assert b.realized_pnl == pytest.approx(-20.0)
        assert b.check_drawdown({}) is True               # 20% below the $100 peak

    def test_just_opened_positions_get_a_grace_period(self, rm):
        rm.open_position("AAPL", 10.0, 5.0)
        rm.open_position("MSFT", 10.0, 1.0)
        out = rm.reconcile_positions([{"symbol": "MSFT", "qty": 1.0}], grace_seconds=180)
        assert out["dropped"] == [] and "AAPL" in out["skipped_new"]
        assert "AAPL" in rm.positions

    def test_old_positions_are_not_protected_by_grace(self, rm):
        rm.open_position("AAPL", 10.0, 5.0)
        rm.open_position("MSFT", 10.0, 1.0)
        rm.positions["AAPL"].entry_time = datetime.now() - timedelta(minutes=10)
        out = rm.reconcile_positions([{"symbol": "MSFT", "qty": 1.0}], grace_seconds=180)
        assert out["dropped"] == ["AAPL"]

    def test_lowercase_tracked_keys_are_not_wrongly_dropped(self, rm):
        rm.open_position("aapl", 10.0, 2.0)
        out = rm.reconcile_positions([{"symbol": "AAPL", "qty": 2.0}])
        assert out["dropped"] == [] and out["untracked"] == []


class TestCorruptStateIsPreserved:
    def test_corrupt_file_is_moved_aside_and_halt_persists(self, tmp_path):
        from agent.core.risk_manager import RiskManager
        path = tmp_path / "state.json"
        path.write_text('{"peak_equity": 150, "realized_pnl": -30, BROKEN', encoding="utf-8")
        a = RiskManager(state_path=path)
        assert a.drawdown_halted
        kept = list(tmp_path.glob("state.json.corrupt-*"))
        assert len(kept) == 1 and "BROKEN" in kept[0].read_text(encoding="utf-8")
        assert RiskManager(state_path=path).drawdown_halted     # restart still halted


class TestResetScript:
    SCRIPT = str(ROOT / "scripts" / "reset_drawdown_halt.py")

    def _halted_state(self, tmp_path):
        p = tmp_path / "s.json"
        p.write_text(json.dumps({"peak_equity": 100.0, "drawdown_halted": True, "realized_pnl": -20.0}))
        return p

    def _run(self, *args):
        return subprocess.run([sys.executable, self.SCRIPT, *args], capture_output=True, text=True,
                              stdin=subprocess.DEVNULL, cwd=ROOT,
                              env={**__import__("os").environ, "PYTHONPATH": str(ROOT)})

    def test_requires_new_peak(self, tmp_path):
        r = self._run("--state-path", str(self._halted_state(tmp_path)))
        assert r.returncode != 0 and "new-peak" in r.stderr

    def test_refuses_non_interactive_and_leaves_halt_in_place(self, tmp_path):
        p = self._halted_state(tmp_path)
        r = self._run("--new-peak", "80", "--state-path", str(p))
        assert r.returncode == 2 and "non-interactively" in r.stderr
        assert json.loads(p.read_text())["drawdown_halted"] is True

    @pytest.mark.parametrize("bad", ["nan", "inf", "0", "-5"])
    def test_rejects_bad_peak(self, tmp_path, bad):
        r = self._run("--new-peak", bad, "--state-path", str(self._halted_state(tmp_path)))
        assert r.returncode != 0


class TestExitConcurrencyAndBooking:
    def _agent_with_real_rm(self, agent, tmp_path):
        from agent.core.risk_manager import RiskManager
        agent.risk_manager = RiskManager(state_path=tmp_path / "s.json")
        agent.risk_manager.open_position("AAPL", 10.0, 5.0)
        return agent

    def test_two_threads_selling_the_same_symbol_sell_once(self, agent, tmp_path):
        agent = self._agent_with_real_rm(agent, tmp_path)
        calls = []

        def slow_sell(symbol, reason=""):
            calls.append(symbol)
            import time as _t
            _t.sleep(0.05)
            return MagicMock(status="filled", filled_price=9.0, error_msg="")

        agent.execution.sell.side_effect = slow_sell
        results = []
        ts = [threading.Thread(target=lambda: results.append(agent._process_exit("AAPL", 9.5, "stop_loss")))
              for _ in range(4)]
        [t.start() for t in ts]
        [t.join() for t in ts]
        assert calls == ["AAPL"] and results.count(True) == 1
        assert "AAPL" not in agent.risk_manager.positions

    def test_exit_is_booked_at_the_broker_fill_price(self, agent, tmp_path):
        agent = self._agent_with_real_rm(agent, tmp_path)
        agent.execution.sell.return_value = MagicMock(status="filled", filled_price=8.0, error_msg="")
        agent._process_exit("AAPL", 9.5, "stop_loss")          # trigger 9.5, actually filled 8.0
        assert agent.risk_manager.realized_pnl == pytest.approx((8.0 - 10.0) * 5.0)

    def test_falls_back_to_trigger_price_when_no_fill_reported(self, agent, tmp_path):
        agent = self._agent_with_real_rm(agent, tmp_path)
        agent.execution.sell.return_value = MagicMock(status="pending", filled_price=0, error_msg="")
        agent._process_exit("AAPL", 9.5, "stop_loss")
        assert agent.risk_manager.realized_pnl == pytest.approx((9.5 - 10.0) * 5.0)

    def test_ai_sell_after_monitor_already_closed_does_not_sell_again(self, agent, tmp_path):
        agent = self._agent_with_real_rm(agent, tmp_path)
        agent.decision_engine = MagicMock()
        agent.risk_manager.positions["AAPL"].entry_time = datetime.now() - timedelta(hours=1)
        agent.execution.sell.return_value = MagicMock(status="filled", filled_price=9.0, error_msg="")

        def monitor_wins_during_delay():
            agent._process_exit("AAPL", 9.0, "stop_loss")

        agent.decision_engine.apply_human_delay.side_effect = monitor_wins_during_delay
        agent._execute_decision(MagicMock(action="SELL", symbol="AAPL", price=9.2, confidence=0.9))
        assert agent.execution.sell.call_count == 1
        agent.notifier.sell.assert_not_called()                 # no phantom "SOLD +0.00"


class TestRealRiskManagerIntegration:
    def test_open_loss_restart_reconcile_equity_stays_consistent(self, agent, tmp_path):
        from agent.core.risk_manager import RiskManager
        path = tmp_path / "s.json"
        agent.risk_manager = RiskManager(state_path=path)
        agent.risk_manager.open_position("AAPL", 10.0, 5.0)
        agent.execution.sell.return_value = MagicMock(status="filled", filled_price=8.0, error_msg="")
        agent._process_exit("AAPL", 9.0, "stop_loss")           # realized -10
        fresh = RiskManager(state_path=path)                    # restart
        assert fresh.cash == pytest.approx(90.0) and fresh.equity() == pytest.approx(90.0)
        fresh.open_position("MSFT", 20.0, 1.0)                  # recovered from DB with no marks
        fresh.reconcile_positions([{"symbol": "MSFT", "qty": 1.0, "current_price": 18.0}])
        assert fresh.equity({}) == pytest.approx(90.0 - 20.0 + 18.0)

    def test_halted_state_with_empty_positions_still_blocks_buys(self, tmp_path):
        from agent.core.risk_manager import RiskManager
        path = tmp_path / "s.json"
        path.write_text(json.dumps({"peak_equity": 100.0, "drawdown_halted": True, "realized_pnl": 0.0}))
        rm_ = RiskManager(state_path=path)
        assert rm_.positions == {} and rm_.can_buy("MSFT", 10.0, 0.99)[0] is False

    def test_state_realized_pnl_and_recovered_positions_combine(self, tmp_path):
        from agent.core.risk_manager import RiskManager
        path = tmp_path / "s.json"
        path.write_text(json.dumps({"peak_equity": 100.0, "drawdown_halted": False, "realized_pnl": -12.0}))
        rm_ = RiskManager(state_path=path)
        rm_.open_position("AAPL", 10.0, 2.0)                    # DB-recovered position
        assert rm_.cash == pytest.approx(100.0 - 12.0 - 20.0)
        assert rm_.equity({"AAPL": 10.0}) == pytest.approx(88.0)


class TestFirstDrawdownCheckAndPeriodicReconcile:
    def test_buy_path_evaluates_drawdown_before_anything_else(self, agent):
        order = []
        agent._check_drawdown = MagicMock(side_effect=lambda: order.append("check"))
        rm_ = MagicMock(drawdown_halted=True, emergency_stop=False)
        agent.risk_manager = rm_
        agent._execute_decision(MagicMock(action="BUY", symbol="AAPL", price=10.0, confidence=0.9))
        assert order == ["check"] and agent.execution.buy.call_count == 0

    def test_periodic_reconcile_uses_a_grace_period(self, agent):
        agent.execution.get_account.return_value = {"equity": 1}
        agent.execution.get_positions_strict.return_value = [{"symbol": "X", "qty": 1}]
        agent.risk_manager.reconcile_positions.return_value = {
            "dropped": [], "resized": [], "untracked": [], "blocked_empty": [],
            "unbooked": [], "skipped_new": []}
        agent._reconcile_with_broker(grace_seconds=180)
        assert agent.risk_manager.reconcile_positions.call_args.kwargs["grace_seconds"] == 180

    def test_repeated_identical_findings_alert_once(self, agent):
        agent.execution.get_account.return_value = {"equity": 1}
        agent.execution.get_positions_strict.return_value = [{"symbol": "TSLA", "qty": 1}]
        agent.risk_manager.reconcile_positions.return_value = {
            "dropped": [], "resized": [], "untracked": ["TSLA"], "blocked_empty": [],
            "unbooked": [], "skipped_new": []}
        for _ in range(3):
            agent._reconcile_with_broker(grace_seconds=180)
        assert agent.notifier.error.call_count == 1


class TestHookRound3:
    @pytest.mark.parametrize("tool,args", [
        ("Bash", {"command": 'git commit -m "$(cat .env)"'}),
        ("Bash", {"command": 'echo "$(cat .env)"'}),
        ("Bash", {"command": 'echo "`cat .env`"'}),
        ("Bash", {"command": "rm logs/risk_state.json"}),
        ("Bash", {"command": "echo '{}' > logs/risk_state.json"}),
        ("Bash", {"command": "mv logs/risk_state.json /tmp/x"}),
        ("Bash", {"command": 'python -c "from agent.core.risk_manager import RiskManager; '
                             'RiskManager().reset_drawdown_halt()"'}),
        ("Edit", {"file_path": "/repo/logs/risk_state.json", "new_string": "{}"}),
        ("Write", {"file_path": "/repo/logs/risk_state.json", "content": "{}"}),
        ("Read", {"file_path": "/repo/config/api_keys.json"}),
        ("Read", {"file_path": "/repo/keys.txt"}),
    ])
    def test_blocks(self, tool, args):
        r = run_guard(tool, **args)
        assert r.returncode == 2, (tool, args, r.stderr)

    @pytest.mark.parametrize("tool,args", [
        ("Bash", {"command": 'git commit -m "fix: tidy reset_drawdown_halt docs"'}),
        ("Bash", {"command": "cat logs/risk_state.json"}),
        ("Bash", {"command": "python scripts/reset_drawdown_halt.py --new-peak 90"}),
        ("Read", {"file_path": "/repo/logs/risk_state.json"}),
        ("Read", {"file_path": "/repo/agent/keyboard_helper.py"}),
        ("Read", {"file_path": "/repo/docs/api_keys.md"}),
    ])
    def test_allows(self, tool, args):
        r = run_guard(tool, **args)
        assert r.returncode == 0, (tool, args, r.stderr)

    def _launcher(self, payload, project_dir=None, path=None):
        import os
        cfg = json.loads((ROOT / ".claude" / "settings.json").read_text(encoding="utf-8"))
        cmd = next(h["command"] for g in cfg["hooks"]["PreToolUse"] for h in g["hooks"]
                   if "guard.py" in h["command"])
        env = {"CLAUDE_PROJECT_DIR": str(project_dir or ROOT), "PATH": path or os.environ["PATH"]}
        return subprocess.run(["/bin/sh", "-c", cmd], input=json.dumps(payload), capture_output=True,
                              text=True, env=env)

    def test_launcher_blocks_when_the_guard_script_crashes(self, tmp_path):
        hooks = tmp_path / ".claude" / "hooks"
        hooks.mkdir(parents=True)
        (hooks / "guard.py").write_text("raise SystemExit(1)\n")        # exit 1 = non-blocking in Claude Code
        r = self._launcher({"tool_name": "Bash", "tool_input": {"command": "ls"}}, project_dir=tmp_path)
        assert r.returncode == 2 and "failing closed" in r.stderr

    def test_launcher_falls_back_when_python3_is_a_broken_stub(self, tmp_path):
        import os
        bindir = tmp_path / "bin"
        bindir.mkdir()
        stub = bindir / "python3"
        stub.write_text("#!/bin/sh\nexit 9\n")
        stub.chmod(0o755)
        (bindir / "python").symlink_to(sys.executable)
        path = f"{bindir}:{os.environ['PATH']}"
        ok = self._launcher({"tool_name": "Bash", "tool_input": {"command": "ls"}}, path=path)
        assert ok.returncode == 0, ok.stderr
        blocked = self._launcher({"tool_name": "Bash", "tool_input": {"command": "cat .env"}}, path=path)
        assert blocked.returncode == 2


# ═══ review round 4 ═════════════════════════════════════════════════════════

class TestColdStartBooking:
    def test_loss_while_down_is_booked_from_persisted_marks_after_restart(self, tmp_path):
        from agent.core.risk_manager import RiskManager
        path = tmp_path / "s.json"
        a = RiskManager(state_path=path)
        a.open_position("AAPL", 10.0, 5.0)
        a.open_position("MSFT", 10.0, 1.0)
        a.check_drawdown({"AAPL": 6.0, "MSFT": 10.0})        # marks persisted by the minute check
        b = RiskManager(state_path=path)                     # agent restarts; AAPL was stopped out
        b.open_position("AAPL", 10.0, 5.0, restored=True)    # DB recovery keeps saved marks
        b.open_position("MSFT", 10.0, 1.0, restored=True)
        out = b.reconcile_positions([{"symbol": "MSFT", "qty": 1.0}])   # cold start: no live marks
        assert out["dropped"] == ["AAPL"] and out["unbooked"] == []
        assert b.realized_pnl == pytest.approx(-20.0)

    def test_marks_survive_in_the_state_file(self, tmp_path):
        from agent.core.risk_manager import RiskManager
        path = tmp_path / "s.json"
        a = RiskManager(state_path=path)
        a.open_position("AAPL", 10.0, 5.0)
        a.check_drawdown({"AAPL": 7.25})
        saved = json.loads(path.read_text())["marks"]["AAPL"]
        assert saved["px"] == pytest.approx(7.25) and saved["ts"] > 0

    def test_junk_marks_in_state_file_are_ignored_not_fatal(self, tmp_path):
        from agent.core.risk_manager import RiskManager
        path = tmp_path / "s.json"
        path.write_text(json.dumps({"peak_equity": 100.0, "drawdown_halted": False,
                                    "marks": {"A": "x", "B": -1, "C": 5.0}}))
        a = RiskManager(state_path=path)
        assert a._last_marks == {"C": 5.0} and not a.drawdown_halted

    def test_marks_are_cleared_when_a_position_closes_and_reopens(self, rm):
        rm.open_position("AAPL", 10.0, 5.0)
        rm.equity({"AAPL": 6.0})
        rm.close_position("AAPL", 6.0, "stop_loss")
        rm.open_position("AAPL", 20.0, 1.0)
        assert rm.equity({}) == pytest.approx(rm.cash + 20.0)      # entry price, not the old 6.0 mark

    def test_extra_shares_at_the_broker_weight_the_entry_price(self, rm):
        rm.open_position("AAPL", 10.0, 2.0)
        rm.equity({"AAPL": 14.0})
        rm.reconcile_positions([{"symbol": "AAPL", "qty": 4.0, "current_price": 14.0}])
        pos = rm.positions["AAPL"]
        assert pos.quantity == 4.0 and pos.entry_price == pytest.approx((10 * 2 + 14 * 2) / 4)


class TestGraceBeforeBlockedEmpty:
    def test_only_young_positions_and_empty_broker_is_not_a_false_alarm(self, rm):
        rm.open_position("AAPL", 10.0, 2.0)                       # first buy of the day, not listed yet
        out = rm.reconcile_positions([], grace_seconds=180)
        assert out["blocked_empty"] == [] and "AAPL" in out["skipped_new"]
        assert "AAPL" in rm.positions

    def test_old_position_with_empty_broker_is_still_blocked(self, rm):
        rm.open_position("AAPL", 10.0, 2.0)
        rm.positions["AAPL"].entry_time = datetime.now() - timedelta(minutes=30)
        out = rm.reconcile_positions([], grace_seconds=180)
        assert out["blocked_empty"] == ["AAPL"] and "AAPL" in rm.positions


class TestLockingAroundReconcileAndAlerts:
    def test_reconcile_waits_for_an_in_flight_sell(self, agent):
        agent.execution.get_account.return_value = {"equity": 1}
        agent.execution.get_positions_strict.return_value = [{"symbol": "X", "qty": 1}]
        agent.risk_manager.reconcile_positions.return_value = {
            "dropped": [], "resized": [], "untracked": [], "blocked_empty": [],
            "unbooked": [], "skipped_new": []}
        agent._exit_lock.acquire()                                # a sell is mid-flight
        t = threading.Thread(target=agent._reconcile_with_broker)
        t.start()
        t.join(0.3)
        assert t.is_alive() and agent.risk_manager.reconcile_positions.call_count == 0
        agent._exit_lock.release()
        t.join(2)
        assert agent.risk_manager.reconcile_positions.call_count == 1

    def test_notifier_is_called_after_the_lock_is_released(self, agent):
        seen = []
        agent.risk_manager.positions = {"AAPL": object()}
        agent.execution.sell.return_value = MagicMock(status="error", error_msg="timeout")
        agent.notifier.error.side_effect = lambda *a, **k: seen.append(agent._exit_lock.locked())
        agent._process_exit("AAPL", 9.0, "stop_loss")
        agent.risk_manager.close_position.return_value = 0.0
        agent.execution.sell.return_value = MagicMock(status="filled", filled_price=9.0, error_msg="")
        agent.notifier.stop_loss.side_effect = lambda *a, **k: seen.append(agent._exit_lock.locked())
        agent._exit_alerted.clear()
        agent._process_exit("AAPL", 9.0, "stop_loss")
        assert seen == [False, False]


class TestHookRound4:
    @pytest.mark.parametrize("tool,args", [
        ("Bash", {"command": "pytest -k risk_state 2>&1 | tail"}),
        ("Bash", {"command": "cat logs/risk_state.json 2>/dev/null"}),
        ("Bash", {"command": "echo hi > /tmp/x; cat tests/test_risk_state.py"}),
        ("Bash", {"command": "python scripts/run_backtest.py > out.txt; echo risk_state"}),
        ("Bash", {"command": "cd x && git commit -m \"mention TRADING_MODE=live docs\""}),
        ("Bash", {"command": "git commit -m \"$(cat <<'EOF'\nfix: tidy reset_drawdown_halt() docs\n\n"
                             "Mentions TRADING_MODE=live in prose.\nEOF\n)\""}),
        ("Bash", {"command": "gh pr create --body \"$(cat <<'EOF'\nUses reset_drawdown_halt() safely\nEOF\n)\""}),
        ("Edit", {"file_path": "/repo/agent/core/risk_state.py", "new_string": "x = 1"}),
        ("Read", {"file_path": "/repo/data/key_metrics.csv"}),
        ("Bash", {"command": "cat data/key_levels.json"}),
    ])
    def test_allows_normal_work(self, tool, args):
        r = run_guard(tool, **args)
        assert r.returncode == 0, (tool, args, r.stderr)

    @pytest.mark.parametrize("tool,args", [
        ("Bash", {"command": "cat config/api_keys.json"}),
        ("Bash", {"command": "cat secrets_manager.yaml"}),
        ("Bash", {"command": "head -5 keys.txt"}),
        ("Bash", {"command": "python -c \"open('logs/risk_state.json','w').write('{}')\""}),
        ("Bash", {"command": "python -c \"import json;json.dump({}, open('logs/risk_state.json','w'))\""}),
        ("Bash", {"command": "bash <<'EOF'\nTRADING_MODE=live python -m agent.main\nEOF"}),
        ("Bash", {"command": "eval \"$(cat <<'EOF'\nTRADING_MODE=live python x.py\nEOF\n)\""}),
        ("Bash", {"command": "git commit -m 'x'; TRADING_MODE=live python y.py"}),
        ("Bash", {"command": "echo ok && cat .env"}),
        ("Bash", {"command": "git commit -m \"$(cat .env)\""}),
    ])
    def test_still_blocks(self, tool, args):
        r = run_guard(tool, **args)
        assert r.returncode == 2, (tool, args, r.stdout, r.stderr)



# ═══ review round 5 ═════════════════════════════════════════════════════════
# Probe commands from the round-5 review (bypasses must block; legitimate work must pass).

ROUND5_BLOCK = [
    [
        "baseline live",
        "TRADING_MODE=live python -m agent.main"
    ],
    [
        "baseline bash -c",
        "bash -c 'TRADING_MODE=live python -m agent.main --i-understand-the-risk'"
    ],
    [
        "echo | bash",
        "echo 'TRADING_MODE=live python -m agent.main --i-understand-the-risk' | bash"
    ],
    [
        "echo & bash -c",
        "echo hi & bash -c 'TRADING_MODE=live python -m agent.main --i-understand-the-risk'"
    ],
    [
        "echo $(bash -c)",
        "echo $(bash -c 'TRADING_MODE=live python -m agent.main --i-understand-the-risk')"
    ],
    [
        "echo backslash-dq ; bash",
        "echo \\\"; bash -c 'TRADING_MODE=live python -m agent.main --i-understand-the-risk'"
    ],
    [
        "echo \"a\\\\\" ; bash",
        "echo \"a\\\\\" ; bash -c 'TRADING_MODE=live python -m agent.main --i-understand-the-risk'"
    ],
    [
        "git commit unquoted heredoc subst",
        "git commit -m \"$(cat <<EOF\n$(bash -c 'TRADING_MODE=live python -m agent.main --i-understand-the-risk')\nEOF\n)\""
    ],
    [
        "echo | python risk_state",
        "echo 'import json;json.dump({},open(\"logs/risk_state.json\",\"w\"))' | python"
    ],
    [
        "python os.remove risk_state",
        "python -c \"import os; os.remove('logs/risk_state.json')\""
    ],
    [
        "python Path.unlink",
        "python -c \"from pathlib import Path; Path('logs/risk_state.json').unlink()\""
    ],
    [
        "python os.rename",
        "python -c \"import os; os.rename('/tmp/x.json','logs/risk_state.json')\""
    ],
    [
        "rm -rf logs",
        "rm -rf logs"
    ],
    [
        "find -delete",
        "find logs -name 'risk_state*' -delete"
    ],
    [
        "git clean logs",
        "git clean -fdx logs"
    ],
    [
        "rm risk_state baseline",
        "rm logs/risk_state.json"
    ],
    [
        "redirect baseline",
        "echo {} > logs/risk_state.json"
    ],
    [
        "secret baseline",
        "cat config/api_keys.json"
    ],
    [
        "secret glob",
        "cat config/api_k?ys.json"
    ],
    [
        "secret brace",
        "cat config/api_key{s,}.json"
    ],
    [
        "secret jq",
        "jq . config/api_keys.json"
    ],
    [
        "secret sudo cat",
        "sudo cat config/api_keys.json"
    ],
    [
        "secret python open",
        "python -c \"print(open('config/api_keys.json').read())\""
    ],
    [
        "aws credentials",
        "cat ~/.aws/credentials"
    ],
    [
        "ssh key",
        "cat ~/.ssh/id_ed25519"
    ],
    [
        "secret unbalanced quote",
        "cat \"config/api_keys.json"
    ],
    [
        "env glob",
        "cat .en?"
    ],
    [
        "env baseline",
        "cat .env"
    ],
    [
        "backslash-newline",
        "bash -c \\\n'TRADING_MODE=live python -m agent.main'"
    ],
    [
        "echo in single |",
        "printf '%s\\n' 'TRADING_MODE=live' | xargs -I{} env {} python -m agent.main"
    ],
    [
        "quoted heredoc, cmd after first EOF",
        "git commit -m \"$(cat <<'EOF'\nfix: msg\nEOF\nbash -c 'TRADING_MODE=live python -m agent.main --i-understand-the-risk'\nEOF\n)\""
    ],
    [
        "quoted heredoc, rm after EOF",
        "git commit -m \"$(cat <<'EOF'\nmsg\nEOF\nrm logs/risk_state.json\nEOF\n)\""
    ]
]

ROUND5_ALLOW = [
    [
        "git commit quoted heredoc (legit)",
        "git commit -m \"$(cat <<'EOF'\nfix: do not set TRADING_MODE=live; rm logs/risk_state.json is blocked\n\nCo-Authored-By: x\nEOF\n)\""
    ],
    [
        "git commit heredoc w/ dquote (legit)",
        "git commit -m \"$(cat <<'EOF'\nfix: the \"rm logs/risk_state.json\" rule\nTRADING_MODE=live is human-only\nEOF\n)\""
    ],
    [
        "git commit -F heredoc (legit)",
        "git commit -F - <<'EOF'\nfix: block rm of risk_state.json\nEOF"
    ],
    [
        "FP 2>&1",
        "python -m pytest -q 2>&1 | grep risk_state.json"
    ],
    [
        "FP grep write_text && cat state",
        "grep -n write_text agent/core/risk_manager.py && cat logs/risk_state.json"
    ],
    [
        "FP grep risk_state && grep os.replace",
        "grep -rn risk_state agent/ ; grep -rn os.replace agent/"
    ],
    [
        "FP cp backup risk_state",
        "cp logs/risk_state.json /tmp/backup.json"
    ],
    [
        "FP grep api_key identifier",
        "grep -rn api_key agent/"
    ],
    [
        "FP grep SECRET_KEY",
        "grep -rn ALPACA_SECRET_KEY agent/ config/"
    ],
    [
        "FP cat secrets_helper.py",
        "cat agent/secrets_helper.py"
    ],
    [
        "reset script direct",
        "python scripts/reset_drawdown_halt.py --new-peak 1000"
    ],
    [
        "gh pr body legit",
        "gh pr create --title 'x' --body 'Blocks TRADING_MODE=live and rm logs/risk_state.json'"
    ]
]


class TestHookRound5Probes:
    @pytest.mark.parametrize("name,cmd", ROUND5_BLOCK, ids=[n for n, _ in ROUND5_BLOCK])
    def test_bypasses_are_blocked(self, name, cmd):
        r = run_guard("Bash", command=cmd)
        assert r.returncode == 2, (name, cmd, r.stderr)

    @pytest.mark.parametrize("name,cmd", ROUND5_ALLOW, ids=[n for n, _ in ROUND5_ALLOW])
    def test_legitimate_work_is_allowed(self, name, cmd):
        r = run_guard("Bash", command=cmd)
        assert r.returncode == 0, (name, cmd, r.stderr)

    @pytest.mark.parametrize("cmd", [
        "git commit -m \"$(cat <<'EOF'\nmsg\nEOF   \n)\"",                 # trailing spaces on EOF
        "git commit -m \"$(cat <<\"EOF\"\nmentions reset_drawdown_halt()\nEOF\n)\"",
        "cat logs/risk_state.json > /tmp/backup.json",
        "find . -name '*.pyc' -delete",
        "git clean -fd",
        "rm -rf build dist",
        "jq .peak_equity logs/risk_state.json",
    ])
    def test_more_legitimate_commands(self, cmd):
        r = run_guard("Bash", command=cmd)
        assert r.returncode == 0, (cmd, r.stderr)

    @pytest.mark.parametrize("cmd", [
        "git commit -m \"$(cat <<'EOF'\nmsg\nEOF\n)\" ; rm logs/risk_state.json",
        "git commit -m \"$(cat <<'EOF'\nmsg\nEOF\n)\" && TRADING_MODE=live python x.py",
        "git commit -F - <<EOF\n$(cat .env)\nEOF",                             # unquoted: expands
        "cat ~/.netrc",
        "rm logs/*",
        "rm -r ./logs/",
        "mv logs/risk_state.json /tmp/",
        "cp /tmp/forged.json logs/risk_state.json",
        "nice -n 5 cat config/api_keys.json",
        "env FOO=1 cat .env",
    ])
    def test_more_bypasses_blocked(self, cmd):
        r = run_guard("Bash", command=cmd)
        assert r.returncode == 2, (cmd, r.stderr)


class TestMarksRound5:
    def test_fresh_buy_never_inherits_a_saved_mark(self, tmp_path):
        from agent.core.risk_manager import RiskManager
        path = tmp_path / "s.json"
        a = RiskManager(state_path=path)
        a.open_position("XYZ", 50.0, 1.0)
        a.check_drawdown({"XYZ": 50.0})
        b = RiskManager(state_path=path)
        b.prune_marks()                                     # XYZ was not restored from the DB
        b.open_position("XYZ", 120.0, 10.0)                 # fresh buy
        assert b.equity({}) == pytest.approx(b.cash + 1200.0)
        b.positions["XYZ"].entry_time = datetime.now() - timedelta(hours=1)
        b.open_position("OTHER", 10.0, 1.0)
        b.positions["OTHER"].entry_time = datetime.now() - timedelta(hours=1)
        out = b.reconcile_positions([{"symbol": "OTHER", "qty": 1.0}])
        assert out["dropped"] == ["XYZ"] and b.realized_pnl == pytest.approx(0.0)   # no phantom -700

    def test_fresh_buy_without_prune_still_does_not_inherit(self, tmp_path):
        from agent.core.risk_manager import RiskManager
        path = tmp_path / "s.json"
        a = RiskManager(state_path=path)
        a.open_position("XYZ", 50.0, 1.0)
        a.check_drawdown({"XYZ": 50.0})
        b = RiskManager(state_path=path)
        b.open_position("XYZ", 120.0, 10.0)                 # restored=False drops the stale mark
        assert b.equity({}) == pytest.approx(b.cash + 1200.0)

    def test_restored_position_keeps_its_mark(self, tmp_path):
        from agent.core.risk_manager import RiskManager
        path = tmp_path / "s.json"
        a = RiskManager(state_path=path)
        a.open_position("XYZ", 50.0, 1.0)
        a.check_drawdown({"XYZ": 40.0})
        b = RiskManager(state_path=path)
        b.open_position("XYZ", 50.0, 1.0, restored=True)
        b.prune_marks()
        assert b.equity({}) == pytest.approx(b.cash + 40.0)

    def test_no_save_churn_from_orphan_or_mixed_case_marks(self, rm, monkeypatch):
        saves = []
        orig = rm._save_state
        monkeypatch.setattr(rm, "_save_state", lambda *a, **k: saves.append(1) or orig(*a, **k))
        rm._last_marks["ORPHAN"] = 5.0                      # not a tracked position
        rm.open_position("aapl", 10.0, 1.0)
        rm.check_drawdown({"aapl": 10.0})
        n = len(saves)
        for _ in range(3):
            rm.check_drawdown({"aapl": 10.0})
        assert len(saves) == n                              # idle ticks do not rewrite the file

    def test_failed_save_does_not_mark_marks_as_saved(self, rm, monkeypatch):
        import os as _os
        rm.open_position("AAPL", 10.0, 1.0)
        monkeypatch.setattr(_os, "replace", lambda *a, **k: (_ for _ in ()).throw(OSError("full")))
        rm.check_drawdown({"AAPL": 9.0})
        assert rm.state_save_failed and rm._saved_marks.get("AAPL") is None
        monkeypatch.undo()
        rm.check_drawdown({"AAPL": 9.0})                  # retried on the next tick
        assert not rm.state_save_failed and rm._saved_marks.get("AAPL") == pytest.approx(9.0)

    def test_reset_script_path_preserves_saved_marks(self, tmp_path):
        from agent.core.risk_manager import RiskManager
        path = tmp_path / "s.json"
        a = RiskManager(state_path=path)
        a.open_position("QQQ", 100.0, 1.0)
        a.check_drawdown({"QQQ": 90.0})
        op = RiskManager(state_path=path)                   # standalone operator process
        op.reset_drawdown_halt(new_peak=95.0)
        assert json.loads(path.read_text())["marks"]["QQQ"]["px"] == pytest.approx(90.0)

    def test_drop_reports_the_age_of_the_mark_used(self, tmp_path):
        from agent.core.risk_manager import RiskManager
        path = tmp_path / "s.json"
        a = RiskManager(state_path=path)
        a.open_position("AAPL", 10.0, 5.0)
        a.open_position("MSFT", 10.0, 1.0)
        a.check_drawdown({"AAPL": 6.0, "MSFT": 10.0})
        data = json.loads(path.read_text())
        data["marks"]["AAPL"]["ts"] -= 7200                 # pretend it is two hours old
        path.write_text(json.dumps(data))
        b = RiskManager(state_path=path)
        b.open_position("AAPL", 10.0, 5.0, restored=True)
        b.open_position("MSFT", 10.0, 1.0, restored=True)
        out = b.reconcile_positions([{"symbol": "MSFT", "qty": 1.0}])
        assert 1.9 < out["booked_at_mark_age_h"]["AAPL"] < 2.1

    @pytest.mark.parametrize("qty", [0, 0.0, -3, None, "x"])
    def test_zero_negative_or_bad_broker_qty_drops_the_position(self, rm, qty):
        rm.open_position("AAPL", 10.0, 5.0)
        out = rm.reconcile_positions([{"symbol": "AAPL", "qty": qty, "current_price": 9.0}])
        assert "AAPL" not in rm.positions and out["dropped"] == ["AAPL"]
        if isinstance(qty, (int, float)) and qty < 0:
            assert out["negative_qty"] == ["AAPL"]

    def test_extra_shares_use_broker_average_and_reset_stops(self, rm):
        rm.open_position("AAPL", 100.0, 1.0)
        out = rm.reconcile_positions([{"symbol": "AAPL", "qty": 3.0, "avg_entry": 120.0,
                                        "current_price": 130.0}])
        pos = rm.positions["AAPL"]
        assert out["adopted_excess"] == ["AAPL"] and pos.quantity == 3.0
        assert pos.entry_price == pytest.approx(120.0)
        assert pos.stop_loss == pytest.approx(120.0 * (1 - rm.max_trade_loss_pct), rel=1e-4)
        assert pos.target_price > 120.0 and pos.target_price == pytest.approx(
            120.0 * (1 + 2 * rm.max_trade_loss_pct), rel=1e-4)
        assert rm.check_stop_losses({"AAPL": 121.0}) == []      # no instant target/stop exit

    def test_just_closed_symbol_is_not_reported_untracked(self, rm):
        rm.open_position("AAPL", 10.0, 1.0)
        rm.close_position("AAPL", 11.0, "AI signal")
        out = rm.reconcile_positions([{"symbol": "AAPL", "qty": 1.0}], grace_seconds=180)
        assert out["untracked"] == []


class TestRound5Misc:
    def test_can_buy_is_safe_while_another_thread_mutates_positions(self, rm):
        errors, stop = [], threading.Event()

        def churn():
            k = 0
            while not stop.is_set():
                rm.open_position(f"S{k % 50}", 1.0, 0.01)
                rm.close_position(f"S{(k + 25) % 50}", 1.0, "x")
                k += 1

        t = threading.Thread(target=churn)
        t.start()
        try:
            for _ in range(2000):
                try:
                    rm.can_buy("ZZZ", 10.0, 0.99)
                except RuntimeError as e:          # "dictionary changed size during iteration"
                    errors.append(e)
        finally:
            stop.set()
            t.join()
        assert errors == []

    def test_strict_positions_tolerate_missing_price(self):
        from agent.core.execution_engine import ExecutionEngine
        eng = ExecutionEngine.__new__(ExecutionEngine)
        eng._ensure_connected = lambda: None
        p = MagicMock(symbol="AAPL", qty="2", avg_entry_price="10", current_price=None,
                      market_value=None, unrealized_pl=None, unrealized_plpc=None)
        eng._trading_client = MagicMock()
        eng._trading_client.get_all_positions.return_value = [p]
        out = eng.get_positions_strict()
        assert out[0]["qty"] == 2.0 and out[0]["current_price"] is None

    def test_strict_positions_refuse_a_missing_quantity(self):
        from agent.core.execution_engine import ExecutionEngine
        eng = ExecutionEngine.__new__(ExecutionEngine)
        eng._ensure_connected = lambda: None
        eng._trading_client = MagicMock()
        eng._trading_client.get_all_positions.return_value = [MagicMock(symbol="AAPL", qty=None)]
        with pytest.raises(ValueError):
            eng.get_positions_strict()

    def test_buy_amount_never_exceeds_what_can_buy_approved(self, agent):
        agent.cfg = {"risk": {"total_fund": 100.0, "max_position_pct": 0.15,
                              "min_cash_reserve_pct": 0.2}}
        agent.decision_engine = MagicMock()
        agent._check_drawdown = MagicMock()
        agent.risk_manager = MagicMock(drawdown_halted=False, emergency_stop=False, cash=100.0)
        agent.risk_manager.trading_allowed.return_value = True
        agent.execution.buy.return_value = MagicMock(status="filled", filled_price=10.0, quantity=0.8)
        decision = MagicMock(action="BUY", symbol="AAPL", price=10.0, quantity=0.8, confidence=0.9)
        agent._execute_decision(decision)
        assert agent.execution.buy.call_args.kwargs["dollar_amount"] == pytest.approx(8.0)


class TestHeredocHelpersDirectly:
    """Unit tests of the two heredoc helpers, so neither can regress behind the other."""

    @staticmethod
    def guard():
        import importlib.util
        spec = importlib.util.spec_from_file_location("guard_mod", GUARD)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod

    def test_quoted_heredoc_with_only_whitespace_before_paren_is_scrubbed(self):
        g = self.guard()
        seg = "git commit -m \"$(cat <<'EOF'\nTRADING_MODE=live is human-only\nEOF\n)\""
        # the original pair of quotes around $(...) plus the empty string that replaced the body
        assert g.strip_safe_heredocs(seg) == "git commit -m " + '"' + '""' + '"'
        assert "TRADING_MODE" not in g.strip_safe_heredocs(seg)

    def test_text_after_the_first_delimiter_is_never_scrubbed(self):
        g = self.guard()
        seg = "git commit -m \"$(cat <<'EOF'\nmsg\nEOF\nrm logs/risk_state.json\nEOF\n)\""
        assert "rm logs/risk_state.json" in g.strip_safe_heredocs(seg)

    def test_unquoted_delimiter_is_never_scrubbed(self):
        g = self.guard()
        seg = "git commit -m \"$(cat <<EOF\n$(bash -c 'x')\nEOF\n)\""
        assert g.strip_safe_heredocs(seg) == seg

    def test_command_after_the_closing_paren_is_kept(self):
        g = self.guard()
        seg = "echo \"$(cat <<'EOF'\nmsg\nEOF\n) ; rm logs/risk_state.json\""
        assert "rm logs/risk_state.json" in g.strip_safe_heredocs(seg)

    def test_stdin_heredoc_helper_keeps_everything_after_the_first_delimiter(self):
        g = self.guard()
        cmd = "git commit -m \"$(cat <<'EOF'\nmsg\nEOF\nrm logs/risk_state.json\nEOF\n)\""
        assert "rm logs/risk_state.json" in g.strip_message_stdin_heredocs(cmd)

    def test_stdin_heredoc_helper_only_applies_to_quoted_delimiters(self):
        g = self.guard()
        cmd = "git commit -F - <<EOF\n$(cat .env)\nEOF"
        assert g.strip_message_stdin_heredocs(cmd) == cmd


class TestCanBuyLockDeterministic:
    def test_can_buy_waits_for_the_position_lock(self, rm):
        done = threading.Event()
        t = threading.Thread(target=lambda: (rm.can_buy("ZZZ", 10.0, 0.99), done.set()))
        rm._lock.acquire()                    # the monitor thread is mid-reconcile
        t.start()
        assert not done.wait(0.3)             # can_buy must not read positions meanwhile
        rm._lock.release()
        assert done.wait(2)
        t.join()
