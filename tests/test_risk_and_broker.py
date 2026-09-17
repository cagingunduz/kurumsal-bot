from datetime import datetime, timedelta, timezone

import pytest

from src.execution.paper_broker import PaperBroker
from src.strategy.risk import DailyLossTracker, calc_stop_target, position_size


def test_stop_target_avwap_long():
    st = calc_stop_target("long", entry_price=60000, reward_risk_ratio=2.0, method="avwap", avwap_price=59700)
    assert st.stop < st.entry < st.target
    assert st.reward_per_unit == pytest.approx(st.risk_per_unit * 2.0)


def test_position_size_matches_risk_pct():
    st = calc_stop_target("long", entry_price=60000, reward_risk_ratio=2.0, method="avwap", avwap_price=59700)
    size = position_size(equity_usdt=10000, risk_per_trade_pct=0.5, stop_target=st)
    assert size["risk_amount_usdt"] == pytest.approx(50.0)


def test_paper_broker_stop_hit(tmp_path):
    st = calc_stop_target("long", entry_price=60000, reward_risk_ratio=2.0, method="avwap", avwap_price=59700)
    size = position_size(10000, 0.5, st)
    broker = PaperBroker(
        starting_equity=10000,
        trade_log_csv=str(tmp_path / "trades.csv"),
        equity_log_csv=str(tmp_path / "equity.csv"),
    )
    now = datetime(2026, 9, 17, 12, 0, tzinfo=timezone.utc)
    broker.open_position("long", st, size["qty"], size["notional_usdt"], now, ["test"])
    closed = broker.update_mark(st.stop - 1, now + timedelta(minutes=5))
    assert len(closed) == 1
    assert closed[0].exit_reason == "stop"
    assert closed[0].pnl_usdt < 0
    assert broker.equity == pytest.approx(10000 - 50.0, abs=0.5)


def test_daily_loss_tracker_blocks_after_limit():
    tracker = DailyLossTracker(starting_equity=10000, max_daily_loss_pct=2.0)
    now = datetime(2026, 9, 17, 12, 0, tzinfo=timezone.utc)
    tracker.register_closed_trade(now, 10000, -150)
    can, _ = tracker.can_trade(now, 9850)
    assert can is True
    tracker.register_closed_trade(now, 9850, -100)
    can2, reason = tracker.can_trade(now, 9750)
    assert can2 is False
    assert reason is not None
