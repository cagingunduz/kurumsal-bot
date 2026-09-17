"""Backtest performans metrikleri."""
from __future__ import annotations

import numpy as np
import pandas as pd

from src.execution.paper_broker import ClosedTrade


def compute_metrics(trades: list[ClosedTrade], final_equity: float, starting_equity: float) -> dict:
    if not trades:
        return {
            "n_trades": 0, "win_rate_pct": None, "profit_factor": None,
            "total_return_pct": (final_equity - starting_equity) / starting_equity * 100.0,
            "max_drawdown_pct": 0.0, "avg_win_usdt": None, "avg_loss_usdt": None,
            "expectancy_usdt": None,
        }

    pnls = np.array([t.pnl_usdt for t in trades])
    wins = pnls[pnls > 0]
    losses = pnls[pnls < 0]
    win_rate = len(wins) / len(pnls) * 100.0
    gross_profit = wins.sum() if len(wins) else 0.0
    gross_loss = -losses.sum() if len(losses) else 0.0
    profit_factor = (gross_profit / gross_loss) if gross_loss > 0 else float("inf") if gross_profit > 0 else None

    equity_series = starting_equity + np.cumsum(pnls)
    running_max = np.maximum.accumulate(np.concatenate([[starting_equity], equity_series]))[1:]
    drawdown_pct = (equity_series - running_max) / running_max * 100.0
    max_dd = drawdown_pct.min() if len(drawdown_pct) else 0.0

    return {
        "n_trades": len(trades),
        "win_rate_pct": round(win_rate, 2),
        "profit_factor": round(profit_factor, 3) if profit_factor not in (None, float("inf")) else profit_factor,
        "total_return_pct": round((final_equity - starting_equity) / starting_equity * 100.0, 3),
        "max_drawdown_pct": round(float(max_dd), 3),
        "avg_win_usdt": round(float(wins.mean()), 2) if len(wins) else 0.0,
        "avg_loss_usdt": round(float(losses.mean()), 2) if len(losses) else 0.0,
        "expectancy_usdt": round(float(pnls.mean()), 2),
        "gross_profit_usdt": round(float(gross_profit), 2),
        "gross_loss_usdt": round(float(gross_loss), 2),
    }
