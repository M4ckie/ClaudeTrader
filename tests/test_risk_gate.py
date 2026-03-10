"""Unit tests for the risk gate."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest
from risk.risk_gate import (
    TradeProposal,
    PortfolioState,
    RiskDecision,
    evaluate,
    evaluate_all,
    _max_buy_quantity,
)


# ── Helpers ───────────────────────────────────────────────────────────────────

def make_portfolio(
    cash=100_000.0,
    total_value=100_000.0,
    positions=None,
    daily_pnl_pct=0.0,
) -> PortfolioState:
    return PortfolioState(
        cash=cash,
        total_value=total_value,
        positions=positions or {},
        daily_pnl_pct=daily_pnl_pct,
    )


def make_buy(ticker="AAPL", price=150.0, quantity=None, stop_loss_pct=5.0, confidence=0.8) -> TradeProposal:
    return TradeProposal(
        ticker=ticker,
        action="BUY",
        price=price,
        quantity=quantity,
        stop_loss_pct=stop_loss_pct,
        confidence=confidence,
        reasoning="test",
    )


def make_sell(ticker="AAPL", quantity=None) -> TradeProposal:
    return TradeProposal(ticker=ticker, action="SELL", quantity=quantity, reasoning="test")


# ── _max_buy_quantity ─────────────────────────────────────────────────────────

def test_max_buy_quantity_limited_by_position_pct():
    portfolio = make_portfolio(cash=100_000, total_value=100_000)
    proposal = make_buy(price=100.0)
    # default scenario: max_position_pct = 15%  → $15,000 → 150 shares
    qty = _max_buy_quantity(proposal, portfolio, max_position_pct=15.0)
    assert qty == 150


def test_max_buy_quantity_limited_by_cash():
    portfolio = make_portfolio(cash=5_000, total_value=100_000)
    proposal = make_buy(price=100.0)
    # 15% of $100k = $15k, but only $5k cash → 50 shares
    qty = _max_buy_quantity(proposal, portfolio, max_position_pct=15.0)
    assert qty == 50


def test_max_buy_quantity_zero_price():
    portfolio = make_portfolio()
    proposal = make_buy(price=0.0)
    qty = _max_buy_quantity(proposal, portfolio, max_position_pct=15.0)
    assert qty == 0


# ── Daily loss circuit breaker ────────────────────────────────────────────────

def test_daily_loss_circuit_breaker_rejects():
    portfolio = make_portfolio(daily_pnl_pct=-4.0)  # default limit is 3%
    proposal = make_buy()
    decision = evaluate(proposal, portfolio, scenario="default")
    assert not decision.approved
    assert "Daily loss limit" in decision.rejection_reason


def test_daily_loss_not_triggered_below_limit():
    portfolio = make_portfolio(daily_pnl_pct=-2.0)
    proposal = make_buy()
    decision = evaluate(proposal, portfolio, scenario="default")
    assert decision.approved


# ── Max open positions ────────────────────────────────────────────────────────

def test_max_positions_rejects_new_ticker():
    existing = {f"TICK{i}": {"quantity": 10, "avg_price": 100.0, "sector": ""} for i in range(8)}
    portfolio = make_portfolio(positions=existing)
    proposal = make_buy(ticker="NEW")
    decision = evaluate(proposal, portfolio, scenario="default")
    assert not decision.approved
    assert "Max open positions" in decision.rejection_reason


def test_max_positions_allows_adding_to_existing():
    existing = {f"TICK{i}": {"quantity": 10, "avg_price": 100.0, "sector": ""} for i in range(8)}
    portfolio = make_portfolio(positions=existing)
    proposal = make_buy(ticker="TICK0")  # already held
    decision = evaluate(proposal, portfolio, scenario="default")
    # Should not be rejected on position count
    assert "Max open positions" not in decision.rejection_reason


# ── Max total exposure ────────────────────────────────────────────────────────

def test_max_exposure_rejects_when_fully_invested():
    # $10k cash, $91k invested → 91% exposure (limit is 90%)
    portfolio = make_portfolio(cash=9_000, total_value=100_000)
    proposal = make_buy()
    decision = evaluate(proposal, portfolio, scenario="default")
    assert not decision.approved
    assert "exposure" in decision.rejection_reason


# ── Sector concentration ──────────────────────────────────────────────────────

def test_sector_concentration_rejects():
    # 42% of portfolio in Technology already (limit is 40%)
    positions = {
        "MSFT": {"quantity": 120, "avg_price": 350.0, "current_price": 350.0, "sector": "Technology"},
    }
    portfolio = make_portfolio(
        cash=58_000,
        total_value=100_000,
        positions=positions,
    )
    proposal = make_buy(ticker="GOOGL", price=150.0)
    decision = evaluate(proposal, portfolio, ticker_sector="Technology", scenario="default")
    assert not decision.approved
    assert "Sector" in decision.rejection_reason


# ── Position size cap ─────────────────────────────────────────────────────────

def test_position_size_caps_quantity():
    portfolio = make_portfolio(cash=100_000, total_value=100_000)
    # Request 200 shares at $100 = $20k (exceeds 15% = $15k limit)
    proposal = make_buy(price=100.0, quantity=200)
    decision = evaluate(proposal, portfolio, scenario="default")
    assert decision.approved
    assert decision.adjusted_quantity == 150  # capped at 15%
    assert any("reduced" in w for w in decision.warnings)


def test_position_size_sets_quantity_when_none():
    portfolio = make_portfolio(cash=100_000, total_value=100_000)
    proposal = make_buy(price=100.0, quantity=None)
    decision = evaluate(proposal, portfolio, scenario="default")
    assert decision.approved
    assert decision.final_quantity == 150


# ── Stop loss cap ─────────────────────────────────────────────────────────────

def test_stop_loss_capped_when_too_wide():
    portfolio = make_portfolio()
    proposal = make_buy(stop_loss_pct=20.0)  # 20% > 2x default (5%) = 10%
    decision = evaluate(proposal, portfolio, scenario="default")
    assert decision.stop_loss_pct == 10.0
    assert any("Stop loss" in w for w in decision.warnings)


# ── SELL rules ────────────────────────────────────────────────────────────────

def test_sell_not_held_rejects():
    portfolio = make_portfolio()
    proposal = make_sell(ticker="AAPL")
    decision = evaluate(proposal, portfolio, scenario="default")
    assert not decision.approved
    assert "not in portfolio" in decision.rejection_reason


def test_sell_sets_quantity_from_position():
    positions = {"AAPL": {"quantity": 50, "avg_price": 150.0, "sector": ""}}
    portfolio = make_portfolio(positions=positions)
    proposal = make_sell(ticker="AAPL", quantity=None)
    decision = evaluate(proposal, portfolio, scenario="default")
    assert decision.approved
    assert decision.final_quantity == 50


# ── evaluate_all ──────────────────────────────────────────────────────────────

def test_evaluate_all_returns_all_decisions():
    portfolio = make_portfolio()
    proposals = [make_buy("AAPL"), make_buy("MSFT")]
    decisions = evaluate_all(proposals, portfolio, scenario="default")
    assert len(decisions) == 2
    assert all(isinstance(d, RiskDecision) for d in decisions)


def test_evaluate_all_uses_ticker_sectors():
    # 45% in Technology (limit is 40%) — should reject
    positions = {
        "MSFT": {"quantity": 150, "avg_price": 300.0, "current_price": 300.0, "sector": "Technology"},
    }
    portfolio = make_portfolio(cash=55_000, total_value=100_000, positions=positions)
    proposals = [make_buy("GOOGL", price=150.0)]
    decisions = evaluate_all(proposals, portfolio, ticker_sectors={"GOOGL": "Technology"}, scenario="default")
    assert not decisions[0].approved
