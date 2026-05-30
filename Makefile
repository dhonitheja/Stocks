.PHONY: install lint format type-check test test-cov backtest-regression clean run run-api

PYTHON  := python
PYTEST  := $(PYTHON) -m pytest
RUFF    := $(PYTHON) -m ruff
BLACK   := $(PYTHON) -m black
MYPY    := $(PYTHON) -m mypy

# ── Dependencies ──────────────────────────────────────────────────
install:
	pip install -r requirements.txt
	pip install ruff black mypy pytest pytest-cov

# ── Code Quality ──────────────────────────────────────────────────
lint:
	$(RUFF) check agent/ backtesting/ tests/

format:
	$(BLACK) agent/ backtesting/ tests/ scripts/
	$(RUFF) check --fix agent/ backtesting/ tests/

type-check:
	$(MYPY) agent/ backtesting/

# ── Tests ─────────────────────────────────────────────────────────
test:
	$(PYTEST) -q

test-cov:
	$(PYTEST) --cov=agent --cov=backtesting --cov-report=term-missing -q

# ── Backtest regression — must match golden within tolerance ──────
backtest-regression:
	$(PYTHON) tests/test_backtest_regression.py

# ── Run ───────────────────────────────────────────────────────────
run:
	PYTHONUTF8=1 $(PYTHON) -m agent.main

run-api:
	PYTHONUTF8=1 $(PYTHON) scripts/start_api.py

# ── Safety check — gate before live deploy ────────────────────────
pre-deploy-check:
	@echo "Running full pre-deploy suite..."
	$(MAKE) lint
	$(MAKE) type-check
	$(MAKE) test
	$(MAKE) backtest-regression
	@echo "All checks passed. Review deployment-engineer subagent before flipping live."

# ── Housekeeping ──────────────────────────────────────────────────
clean:
	find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
	find . -name "*.pyc" -delete 2>/dev/null || true
	rm -rf .mypy_cache .ruff_cache .pytest_cache htmlcov
