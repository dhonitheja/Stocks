"""
Telegram trade notification bot.

Sends alerts for: BUY, SELL, stop-loss hits, circuit breaker trips,
daily P&L summaries, and critical errors.

Requires:
  TELEGRAM_BOT_TOKEN  — from BotFather
  TELEGRAM_CHAT_ID    — your chat/group ID

Set notifications.telegram_enabled: true in config/settings.yaml to activate.
"""
from __future__ import annotations

import os
import threading
from datetime import datetime
from typing import Optional

import requests
from loguru import logger

from agent.config import CONFIG


class TelegramNotifier:
    """Fire-and-forget Telegram messages on a background thread."""

    def __init__(self) -> None:
        cfg = CONFIG.get("notifications", {})
        self._enabled: bool = (
            cfg.get("telegram_enabled", False)
            and bool(os.getenv("TELEGRAM_BOT_TOKEN"))
            and bool(os.getenv("TELEGRAM_CHAT_ID"))
        )
        self._token: str = os.getenv("TELEGRAM_BOT_TOKEN", "")
        self._chat_id: str = os.getenv("TELEGRAM_CHAT_ID", "")
        self._mode: str = CONFIG.get("execution", {}).get("mode", "paper").upper()

        if self._enabled:
            logger.info("[TelegramNotifier] enabled")
        else:
            logger.debug("[TelegramNotifier] disabled (set telegram_enabled: true + env vars to activate)")

    # ── Public API ──────────────────────────────────────────────

    def buy(self, symbol: str, amount: float, price: float, confidence: float) -> None:
        if not self._enabled:
            return
        self._send(
            f"🟢 *BUY* `{symbol}`\n"
            f"Amount: ${amount:.2f} @ ${price:.2f}\n"
            f"Confidence: {confidence:.0%}\n"
            f"Mode: {self._mode}"
        )

    def sell(self, symbol: str, price: float, pnl: float, reason: str = "AI signal") -> None:
        if not self._enabled:
            return
        emoji = "🟢" if pnl >= 0 else "🔴"
        self._send(
            f"{emoji} *SELL* `{symbol}`\n"
            f"Price: ${price:.2f} | P&L: ${pnl:+.2f}\n"
            f"Reason: {reason}\n"
            f"Mode: {self._mode}"
        )

    def stop_loss(self, symbol: str, price: float, loss: float) -> None:
        if not self._enabled:
            return
        self._send(
            f"🛑 *STOP-LOSS* `{symbol}`\n"
            f"Triggered @ ${price:.2f} | Loss: ${loss:+.2f}\n"
            f"Mode: {self._mode}"
        )

    def circuit_breaker(self, daily_loss: float, threshold: float) -> None:
        if not self._enabled:
            return
        self._send(
            f"⚡ *CIRCUIT BREAKER TRIGGERED*\n"
            f"Daily loss: ${daily_loss:.2f} exceeded threshold: ${threshold:.2f}\n"
            f"All trading halted for today.\n"
            f"Mode: {self._mode}"
        )

    def daily_summary(
        self,
        date: str,
        pnl: float,
        trades: int,
        open_positions: int,
        portfolio_value: float,
    ) -> None:
        if not self._enabled:
            return
        emoji = "📈" if pnl >= 0 else "📉"
        self._send(
            f"{emoji} *Daily Summary* — {date}\n"
            f"P&L: ${pnl:+.2f} | Trades: {trades}\n"
            f"Open positions: {open_positions}\n"
            f"Portfolio: ${portfolio_value:.2f}\n"
            f"Mode: {self._mode}"
        )

    def error(self, message: str) -> None:
        if not self._enabled:
            return
        self._send(f"❌ *ERROR*\n{message}\nMode: {self._mode}")

    def startup(self, fund: float, watchlist: list[str]) -> None:
        if not self._enabled:
            return
        self._send(
            f"🚀 *StockSage Started*\n"
            f"Fund: ${fund:.2f} | Mode: {self._mode}\n"
            f"Watching: {', '.join(watchlist[:8])}{'...' if len(watchlist) > 8 else ''}"
        )

    # ── Internal ────────────────────────────────────────────────

    def _send(self, text: str) -> None:
        """Send on a daemon thread so it never blocks the trading loop."""
        t = threading.Thread(target=self._post, args=(text,), daemon=True)
        t.start()

    def _post(self, text: str) -> None:
        url = f"https://api.telegram.org/bot{self._token}/sendMessage"
        try:
            resp = requests.post(
                url,
                json={
                    "chat_id": self._chat_id,
                    "text": text,
                    "parse_mode": "Markdown",
                },
                timeout=10,
            )
            if not resp.ok:
                logger.warning(f"[TelegramNotifier] HTTP {resp.status_code}: {resp.text[:100]}")
        except Exception as exc:
            logger.warning(f"[TelegramNotifier] send failed: {exc}")
