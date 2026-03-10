"""Unit tests for the paper trading simulator."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest
from execution.simulator import Simulator, Position
from risk.risk_gate import TradeProposal, RiskDecision


# ── Helpers ───────────────────────────────────────────────────────────────────

def make_simulator(cash=100_000.0) -> Simulator:
    return Simulator(cash=cash, scenario="default")


def make_buy_decision(ticker="AAPL", price=150.0, quantity=10, stop_loss_pct=5.0) -> RiskDecision:
    proposal = TradeProposal(
        ticker=ticker,
        action="BUY",
        price=price,
        quantity=quantity,
        stop_loss_pct=stop_loss_pct,
        confidence=0.8,
        reasoning="test buy",
    )
    return RiskDecision(
        approved=True,
        proposal=proposal,
        adjusted_quantity=quantity,
        stop_loss_pct=stop_loss_pct,
    )


def make_sell_decision(ticker="AAPL", quantity=10) -> RiskDecision:
    proposal = TradeProposal(
        ticker=ticker,
        action="SELL",
        quantity=quantity,
        reasoning="test sell",
    )
    return RiskDecision(
        approved=True,
        proposal=proposal,
        adjusted_quantity=quantity,
        stop_loss_pct=5.0,
    )


# ── Position ──────────────────────────────────────────────────────────────────

def test_position_current_value():
    pos = Position(ticker="AAPL", quantity=10, avg_price=100.0)
    assert pos.current_value(150.0) == 1500.0


def test_position_unrealized_pnl():
    pos = Position(ticker="AAPL", quantity=10, avg_price=100.0)
    assert pos.unrealized_pnl(110.0) == 100.0
    assert pos.unrealized_pnl(90.0) == -100.0


def test_position_unrealized_pnl_pct():
    pos = Position(ticker="AAPL", quantity=10, avg_price=100.0)
    assert pos.unrealized_pnl_pct(110.0) == pytest.approx(10.0)
    assert pos.unrealized_pnl_pct(95.0) == pytest.approx(-5.0)


def test_position_unrealized_pnl_pct_zero_avg_price():
    pos = Position(ticker="AAPL", quantity=10, avg_price=0.0)
    assert pos.unrealized_pnl_pct(100.0) == 0.0


# ── BUY execution ─────────────────────────────────────────────────────────────

def test_buy_reduces_cash():
    sim = make_simulator(cash=100_000)
    decision = make_buy_decision(price=100.0, quantity=10)
    sim.execute(decision, current_price=100.0)
    # 10 shares * $100 * (1 + slippage) + commission < $100k
    assert sim.cash < 100_000


def test_buy_creates_position():
    sim = make_simulator()
    decision = make_buy_decision(ticker="AAPL", price=100.0, quantity=10)
    sim.execute(decision, current_price=100.0)
    assert "AAPL" in sim.positions
    assert sim.positions["AAPL"].quantity == 10


def test_buy_sets_stop_loss_price():
    sim = make_simulator()
    decision = make_buy_decision(price=100.0, quantity=10, stop_loss_pct=5.0)
    sim.execute(decision, current_price=100.0)
    pos = sim.positions["AAPL"]
    # stop should be approx 5% below fill price (fill includes slippage)
    assert pos.stop_loss_price is not None
    assert pos.stop_loss_price < 100.0


def test_buy_averages_down_existing_position():
    sim = make_simulator(cash=100_000)
    # First buy at $100
    d1 = make_buy_decision(ticker="AAPL", price=100.0, quantity=10)
    sim.execute(d1, current_price=100.0)
    first_avg = sim.positions["AAPL"].avg_price

    # Second buy at $90 (average down)
    d2 = make_buy_decision(ticker="AAPL", price=90.0, quantity=10)
    sim.execute(d2, current_price=90.0)

    pos = sim.positions["AAPL"]
    assert pos.quantity == 20
    assert pos.avg_price < first_avg  # avg price went down


def test_stop_loss_recalculated_on_average_down():
    sim = make_simulator(cash=100_000)
    # Buy at $100 → stop at ~$95 (5%)
    d1 = make_buy_decision(ticker="AAPL", price=100.0, quantity=10, stop_loss_pct=5.0)
    sim.execute(d1, current_price=100.0)
    original_stop = sim.positions["AAPL"].stop_loss_price

    # Average down at $80 → new avg ~$90 → stop should move to ~$85.5
    d2 = make_buy_decision(ticker="AAPL", price=80.0, quantity=10, stop_loss_pct=5.0)
    sim.execute(d2, current_price=80.0)

    new_stop = sim.positions["AAPL"].stop_loss_price
    assert new_stop is not None
    # Stop should be lower than original (avg came down)
    assert new_stop < original_stop
    # Stop should be ~5% below new avg price
    new_avg = sim.positions["AAPL"].avg_price
    assert new_stop == pytest.approx(new_avg * 0.95, rel=0.01)


def test_buy_skipped_when_not_approved():
    sim = make_simulator()
    proposal = TradeProposal(ticker="AAPL", action="BUY", price=100.0, quantity=10)
    decision = RiskDecision(approved=False, proposal=proposal, rejection_reason="test")
    result = sim.execute(decision, current_price=100.0)
    assert result is None
    assert "AAPL" not in sim.positions


def test_buy_insufficient_cash_adjusts_quantity():
    sim = make_simulator(cash=500)
    decision = make_buy_decision(price=100.0, quantity=10)
    result = sim.execute(decision, current_price=100.0)
    if result:  # may skip if can't afford even 1 share after slippage/commission
        assert sim.positions["AAPL"].quantity < 10
    else:
        assert "AAPL" not in sim.positions


# ── SELL execution ────────────────────────────────────────────────────────────

def test_sell_removes_position():
    sim = make_simulator()
    buy = make_buy_decision(price=100.0, quantity=10)
    sim.execute(buy, current_price=100.0)

    sell = make_sell_decision(quantity=10)
    sim.execute(sell, current_price=110.0)
    assert "AAPL" not in sim.positions


def test_sell_increases_cash():
    sim = make_simulator(cash=50_000)
    buy = make_buy_decision(price=100.0, quantity=10)
    sim.execute(buy, current_price=100.0)
    cash_after_buy = sim.cash

    sell = make_sell_decision(quantity=10)
    sim.execute(sell, current_price=110.0)
    assert sim.cash > cash_after_buy


def test_sell_partial_reduces_quantity():
    sim = make_simulator()
    buy = make_buy_decision(price=100.0, quantity=20)
    sim.execute(buy, current_price=100.0)

    sell = make_sell_decision(quantity=10)
    sim.execute(sell, current_price=100.0)
    assert "AAPL" in sim.positions
    assert sim.positions["AAPL"].quantity == 10


def test_sell_not_in_portfolio_returns_none():
    sim = make_simulator()
    sell = make_sell_decision(ticker="AAPL")
    result = sim.execute(sell, current_price=100.0)
    assert result is None


# ── Stop loss check ───────────────────────────────────────────────────────────

def test_stop_loss_triggers_below_stop_price():
    sim = make_simulator()
    buy = make_buy_decision(price=100.0, quantity=10, stop_loss_pct=5.0)
    sim.execute(buy, current_price=100.0)

    pos = sim.positions["AAPL"]
    stop = pos.stop_loss_price
    triggered = sim.check_stop_losses({"AAPL": stop - 1.0})
    assert "AAPL" in triggered


def test_stop_loss_not_triggered_above_stop():
    sim = make_simulator()
    buy = make_buy_decision(price=100.0, quantity=10, stop_loss_pct=5.0)
    sim.execute(buy, current_price=100.0)

    triggered = sim.check_stop_losses({"AAPL": 200.0})
    assert triggered == []


# ── Portfolio state ───────────────────────────────────────────────────────────

def test_portfolio_state_cash_only():
    sim = make_simulator(cash=100_000)
    state = sim.portfolio_state({})
    assert state.cash == 100_000
    assert state.total_value == 100_000
    assert state.invested_value == 0


def test_portfolio_state_with_position():
    sim = make_simulator(cash=90_000)
    sim.positions["AAPL"] = Position(ticker="AAPL", quantity=100, avg_price=100.0)
    state = sim.portfolio_state({"AAPL": 110.0})
    assert state.total_value == pytest.approx(90_000 + 11_000)


def test_daily_pnl_calculation():
    sim = make_simulator(cash=100_000)
    sim.set_start_of_day({"AAPL": 100.0})
    sim.positions["AAPL"] = Position(ticker="AAPL", quantity=100, avg_price=100.0)
    state = sim.portfolio_state({"AAPL": 110.0})
    # started at $100k, now $110k in positions + original cash change
    assert state.daily_pnl_pct != 0.0
