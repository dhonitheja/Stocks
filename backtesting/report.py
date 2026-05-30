"""Backtest result report with P&L metrics and Rich terminal display."""
from __future__ import annotations

import json
from dataclasses import dataclass, asdict
from datetime import date, datetime
from typing import Any

from rich.console import Console
from rich.table import Table
from rich.panel import Panel

from .backtester import SimTrade

console = Console()


@dataclass
class BacktestReport:
    start: str
    end: str
    initial_cash: float
    final_cash: float
    trades: list[SimTrade]
    symbols: list[str]

    # ── Computed properties ──────────────────────────────────────

    @property
    def total_return_pct(self) -> float:
        if self.initial_cash == 0:
            return 0.0
        return (self.final_cash - self.initial_cash) / self.initial_cash * 100

    @property
    def sell_trades(self) -> list[SimTrade]:
        return [t for t in self.trades if t.action == "SELL"]

    @property
    def win_rate(self) -> float:
        sells = self.sell_trades
        if not sells:
            return 0.0
        wins = sum(1 for t in sells if t.pnl > 0)
        return wins / len(sells)

    @property
    def total_pnl(self) -> float:
        return sum(t.pnl for t in self.sell_trades)

    @property
    def avg_pnl_per_trade(self) -> float:
        sells = self.sell_trades
        return self.total_pnl / len(sells) if sells else 0.0

    @property
    def max_win(self) -> float:
        sells = self.sell_trades
        return max((t.pnl for t in sells), default=0.0)

    @property
    def max_loss(self) -> float:
        sells = self.sell_trades
        return min((t.pnl for t in sells), default=0.0)

    @property
    def profit_factor(self) -> float:
        gross_profit = sum(t.pnl for t in self.sell_trades if t.pnl > 0)
        gross_loss = abs(sum(t.pnl for t in self.sell_trades if t.pnl < 0))
        return gross_profit / gross_loss if gross_loss > 0 else float("inf")

    # ── Display ──────────────────────────────────────────────────

    def print_summary(self) -> None:
        ret_color = "green" if self.total_return_pct >= 0 else "red"
        pnl_color = "green" if self.total_pnl >= 0 else "red"

        console.print(Panel.fit(
            f"[bold]Backtest:[/bold] {self.start} -> {self.end}\n"
            f"Symbols: {', '.join(self.symbols)}\n"
            f"Initial: [cyan]${self.initial_cash:.2f}[/cyan] -> Final: [cyan]${self.final_cash:.2f}[/cyan]\n"
            f"Return: [{ret_color}]{self.total_return_pct:+.2f}%[/{ret_color}]  "
            f"P&L: [{pnl_color}]${self.total_pnl:+.2f}[/{pnl_color}]",
            title="[bold cyan]Backtest Summary[/bold cyan]",
        ))

        table = Table(title="Trade Statistics", show_lines=True)
        table.add_column("Metric", style="cyan")
        table.add_column("Value", justify="right")

        table.add_row("Total trades", str(len(self.trades)))
        table.add_row("Closed positions", str(len(self.sell_trades)))
        table.add_row("Win rate", f"{self.win_rate * 100:.1f}%")
        table.add_row("Avg P&L / trade", f"${self.avg_pnl_per_trade:+.4f}")
        table.add_row("Max win", f"${self.max_win:+.4f}")
        table.add_row("Max loss", f"${self.max_loss:+.4f}")
        table.add_row("Profit factor", f"{self.profit_factor:.2f}" if self.profit_factor != float('inf') else "inf")
        table.add_row("Symbols tested", str(len(self.symbols)))

        console.print(table)

        if self.sell_trades:
            console.print("\n[bold]Trade Log (last 20):[/bold]")
            trade_table = Table(show_lines=True)
            trade_table.add_column("Date")
            trade_table.add_column("Symbol", style="cyan")
            trade_table.add_column("Action")
            trade_table.add_column("Price", justify="right")
            trade_table.add_column("Shares", justify="right")
            trade_table.add_column("P&L", justify="right")

            for t in self.sell_trades[-20:]:
                pnl_str = f"[green]${t.pnl:+.4f}[/green]" if t.pnl >= 0 else f"[red]${t.pnl:+.4f}[/red]"
                trade_table.add_row(
                    str(t.date), t.symbol, t.action,
                    f"${t.price:.2f}", f"{t.shares:.4f}", pnl_str,
                )
            console.print(trade_table)

    def to_json(self, path: str) -> None:
        data: dict[str, Any] = {
            "start": self.start,
            "end": self.end,
            "symbols": self.symbols,
            "initial_cash": self.initial_cash,
            "final_cash": self.final_cash,
            "total_return_pct": round(self.total_return_pct, 4),
            "total_pnl": round(self.total_pnl, 6),
            "win_rate": round(self.win_rate, 2),
            "avg_pnl_per_trade": round(self.avg_pnl_per_trade, 6),
            "max_win": round(self.max_win, 6),
            "max_loss": round(self.max_loss, 6),
            "profit_factor": round(self.profit_factor, 4) if self.profit_factor != float("inf") else None,
            "total_trades": len(self.trades),
            "trades": [
                {
                    "symbol": t.symbol,
                    "action": t.action,
                    "date": str(t.date),
                    "price": round(t.price, 4),
                    "shares": round(t.shares, 6),
                    "dollar_value": round(t.dollar_value, 4),
                    "pnl": round(t.pnl, 6),
                }
                for t in self.trades
            ],
            "generated_at": datetime.now().isoformat(),
        }
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        console.print(f"[dim]Report saved to {path}[/dim]")
