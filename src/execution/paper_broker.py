"""
Paper (simüle) trading broker. GERÇEK PARA/EMİR KULLANMAZ.

Sinyal + risk hesaplamasından gelen bilgiyle sanal bir pozisyon açar, her
fiyat güncellemesinde stop/target'a çarpıp çarpmadığını kontrol eder, kapanan
işlemleri CSV'ye loglar ve equity eğrisini günceller.

Bu sınıf hem backtest hem canlı paper-trading main loop tarafından aynı
şekilde kullanılır -- tek fark verinin tarihsel mi yoksa WebSocket'ten mi
geldiği (main_backtest.py vs main_live.py).
"""
from __future__ import annotations

import csv
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Optional

from src.strategy.risk import StopTarget


@dataclass
class Position:
    id: int
    direction: str  # "long" | "short"
    entry_price: float
    stop: float
    target: float
    qty: float
    notional_usdt: float
    opened_at: datetime
    reasons: list[str] = field(default_factory=list)


@dataclass
class ClosedTrade:
    id: int
    direction: str
    entry_price: float
    exit_price: float
    qty: float
    opened_at: datetime
    closed_at: datetime
    exit_reason: str  # "stop" | "target" | "manual"
    pnl_usdt: float
    pnl_pct: float
    reasons: list[str] = field(default_factory=list)


class PaperBroker:
    def __init__(
        self, starting_equity: float, trade_log_csv: str = "logs/paper_trades.csv",
        equity_log_csv: str = "logs/equity_curve.csv", max_concurrent_positions: int = 1,
    ):
        self.equity = starting_equity
        self.starting_equity = starting_equity
        self.max_concurrent_positions = max_concurrent_positions
        self.open_positions: list[Position] = []
        self.closed_trades: list[ClosedTrade] = []
        self._next_id = 1

        self.trade_log_csv = Path(trade_log_csv)
        self.equity_log_csv = Path(equity_log_csv)
        self.trade_log_csv.parent.mkdir(parents=True, exist_ok=True)
        self.equity_log_csv.parent.mkdir(parents=True, exist_ok=True)
        self._init_csv(self.trade_log_csv, [
            "id", "direction", "entry_price", "exit_price", "qty",
            "opened_at", "closed_at", "exit_reason", "pnl_usdt", "pnl_pct", "reasons",
        ])
        self._init_csv(self.equity_log_csv, ["timestamp", "price", "equity", "open_positions"])

    @staticmethod
    def _init_csv(path: Path, header: list[str]) -> None:
        if not path.exists():
            with open(path, "w", newline="") as f:
                csv.writer(f).writerow(header)

    def can_open_new(self) -> bool:
        return len(self.open_positions) < self.max_concurrent_positions

    def open_position(
        self, direction: str, stop_target: StopTarget, qty: float, notional_usdt: float,
        opened_at: datetime, reasons: list[str],
    ) -> Position:
        if not self.can_open_new():
            raise RuntimeError("max_concurrent_positions doldu, yeni pozisyon açılamaz")
        pos = Position(
            id=self._next_id, direction=direction, entry_price=stop_target.entry,
            stop=stop_target.stop, target=stop_target.target, qty=qty,
            notional_usdt=notional_usdt, opened_at=opened_at, reasons=reasons,
        )
        self._next_id += 1
        self.open_positions.append(pos)
        return pos

    def _pnl(self, pos: Position, exit_price: float) -> tuple[float, float]:
        if pos.direction == "long":
            pnl_usdt = (exit_price - pos.entry_price) * pos.qty
        else:
            pnl_usdt = (pos.entry_price - exit_price) * pos.qty
        pnl_pct = pnl_usdt / pos.notional_usdt * 100.0 if pos.notional_usdt else 0.0
        return pnl_usdt, pnl_pct

    def _close(self, pos: Position, exit_price: float, closed_at: datetime, reason: str) -> ClosedTrade:
        pnl_usdt, pnl_pct = self._pnl(pos, exit_price)
        self.equity += pnl_usdt
        trade = ClosedTrade(
            id=pos.id, direction=pos.direction, entry_price=pos.entry_price, exit_price=exit_price,
            qty=pos.qty, opened_at=pos.opened_at, closed_at=closed_at, exit_reason=reason,
            pnl_usdt=pnl_usdt, pnl_pct=pnl_pct, reasons=pos.reasons,
        )
        self.closed_trades.append(trade)
        self.open_positions.remove(pos)
        with open(self.trade_log_csv, "a", newline="") as f:
            w = csv.writer(f)
            w.writerow([
                trade.id, trade.direction, trade.entry_price, trade.exit_price, trade.qty,
                trade.opened_at.isoformat(), trade.closed_at.isoformat(), trade.exit_reason,
                f"{trade.pnl_usdt:.4f}", f"{trade.pnl_pct:.4f}", " | ".join(trade.reasons),
            ])
        return trade

    def force_close(self, pos: Position, exit_price: float, closed_at: datetime, reason: str) -> ClosedTrade:
        """Belirli bir pozisyonu, verilen fiyattan kapatan public API (ör. backtest'te
        bar içi high/low ile stop/target kontrolü yaparken kullanılır)."""
        return self._close(pos, exit_price, closed_at, reason)

    def update_mark(self, current_price: float, now: datetime) -> list[ClosedTrade]:
        """Açık pozisyonları günceller; stop/target'a değenleri kapatır. Kapanan işlemleri döner."""
        closed: list[ClosedTrade] = []
        for pos in list(self.open_positions):
            hit_stop = (current_price <= pos.stop) if pos.direction == "long" else (current_price >= pos.stop)
            hit_target = (current_price >= pos.target) if pos.direction == "long" else (current_price <= pos.target)
            if hit_stop:
                closed.append(self._close(pos, pos.stop, now, "stop"))
            elif hit_target:
                closed.append(self._close(pos, pos.target, now, "target"))
        self.log_equity(current_price, now)
        return closed

    def close_all(self, current_price: float, now: datetime, reason: str = "manual") -> list[ClosedTrade]:
        closed = []
        for pos in list(self.open_positions):
            closed.append(self._close(pos, current_price, now, reason))
        self.log_equity(current_price, now)
        return closed

    def unrealized_pnl(self, current_price: float) -> float:
        total = 0.0
        for pos in self.open_positions:
            pnl, _ = self._pnl(pos, current_price)
            total += pnl
        return total

    def log_equity(self, current_price: float, now: datetime) -> None:
        mark_equity = self.equity + self.unrealized_pnl(current_price)
        with open(self.equity_log_csv, "a", newline="") as f:
            csv.writer(f).writerow([now.isoformat(), current_price, f"{mark_equity:.4f}", len(self.open_positions)])
