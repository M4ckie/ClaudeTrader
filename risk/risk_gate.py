"""
Risk Gate — Hard rule enforcement before any trade is executed.

All proposed trades from the LLM must pass through here first.
These are non-negotiable rules that override the LLM's judgment.

Rules enforced:
  - Max position size (% of portfolio)
  - Max sector concentration
  - Max total portfolio exposure
  - Max daily loss stop
  - Max open positions
  - Stop loss placement
"""

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import sys
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config.settings import (
    SCENARIOS,
    DEFAULT_SCENARIO,
)


def _scenario_cfg(scenario: str) -> dict:
    return SCENARIOS.get(scenario, SCENARIOS[DEFAULT_SCENARIO])

logger = logging.getLogger(__name__)


@dataclass
class TradeProposal:
    """A trade proposed by the LLM strategist."""
    ticker: str
    action: str          # "BUY" or "SELL"
    quantity: Optional[int] = None
    price: Optional[float] = None
    stop_loss_pct: Optional[float] = None
    confidence: Optional[float] = None
    reasoning: str = ""


@dataclass
class PortfolioState:
    """Current state of the portfolio passed to the risk gate."""
    cash: float
    total_value: float
    positions: dict = field(default_factory=dict)  # {ticker: {"quantity": int, "avg_price": float, "sector": str}}
    daily_pnl_pct: float = 0.0

    @property
    def invested_value(self) -> float:
        return self.total_value - self.cash

    @property
    def exposure_pct(self) -> float:
        if self.total_value == 0:
            return 0.0
        return (self.invested_value / self.total_value) * 100

    def sector_exposure_pct(self, sector: str) -> float:
        """Return % of portfolio in a given sector."""
        if self.total_value == 0:
            return 0.0
        sector_value = sum(
            pos["quantity"] * pos.get("current_price", pos["avg_price"])
            for pos in self.positions.values()
            if pos.get("sector") == sector
        )
        return (sector_value / self.total_value) * 100

    def position_value(self, ticker: str, price: float) -> float:
        """Current market value of a position."""
        pos = self.positions.get(ticker)
        if not pos:
            return 0.0
        return pos["quantity"] * price


@dataclass
class RiskDecision:
    """Result of running a trade proposal through the risk gate."""
    approved: bool
    proposal: TradeProposal
    adjusted_quantity: Optional[int] = None
    stop_loss_pct: float = 5.0
    rejection_reason: str = ""
    warnings: list = field(default_factory=list)

    @property
    def final_quantity(self) -> Optional[int]:
        return self.adjusted_quantity if self.adjusted_quantity is not None else self.proposal.quantity


def _max_buy_quantity(proposal: TradeProposal, portfolio: PortfolioState, max_position_pct: float) -> int:
    if not proposal.price or proposal.price <= 0:
        return 0
    max_position_value = portfolio.total_value * (max_position_pct / 100)
    max_by_position = int(max_position_value / proposal.price)
    max_by_cash = int(portfolio.cash / proposal.price)
    return min(max_by_position, max_by_cash)


def evaluate(
    proposal: TradeProposal,
    portfolio: PortfolioState,
    ticker_sector: str = "",
    scenario: str = DEFAULT_SCENARIO,
) -> RiskDecision:
    """
    Evaluate a trade proposal against all risk rules.

    Args:
        proposal: The proposed trade from the LLM.
        portfolio: Current portfolio state.
        ticker_sector: Sector of the ticker (for sector concentration check).
        scenario: Which scenario's risk parameters to apply.

    Returns:
        RiskDecision with approved/rejected status and any adjustments.
    """
    cfg = _scenario_cfg(scenario)
    max_position_pct      = cfg["max_position_pct"]
    max_open_positions    = cfg["max_open_positions"]
    default_stop_loss_pct = cfg["default_stop_loss_pct"]
    max_daily_loss_pct    = cfg["max_daily_loss_pct"]
    max_total_exposure    = cfg["max_total_exposure_pct"]
    max_sector_pct        = cfg["max_sector_pct"]

    decision = RiskDecision(
        approved=True,
        proposal=proposal,
        stop_loss_pct=proposal.stop_loss_pct or default_stop_loss_pct,
    )

    # ── Daily loss circuit breaker ────────────────────────────────────────
    if portfolio.daily_pnl_pct <= -max_daily_loss_pct:
        decision.approved = False
        decision.rejection_reason = (
            f"Daily loss limit reached ({portfolio.daily_pnl_pct:.2f}% vs -{max_daily_loss_pct}% limit). "
            "No new trades today."
        )
        logger.warning(f"[RISK] {proposal.ticker} REJECTED — daily loss circuit breaker")
        return decision

    if proposal.action == "BUY":
        # ── Max open positions ────────────────────────────────────────────
        open_count = len(portfolio.positions)
        if proposal.ticker not in portfolio.positions and open_count >= max_open_positions:
            decision.approved = False
            decision.rejection_reason = (
                f"Max open positions reached ({open_count}/{max_open_positions}). "
                "Close a position before opening a new one."
            )
            logger.warning(f"[RISK] {proposal.ticker} REJECTED — max positions")
            return decision

        # ── Max total exposure ────────────────────────────────────────────
        if portfolio.exposure_pct >= max_total_exposure:
            decision.approved = False
            decision.rejection_reason = (
                f"Portfolio exposure {portfolio.exposure_pct:.1f}% is at max "
                f"({max_total_exposure}% limit). Need to free up cash first."
            )
            logger.warning(f"[RISK] {proposal.ticker} REJECTED — max exposure")
            return decision

        # ── Sector concentration ──────────────────────────────────────────
        if ticker_sector:
            sector_pct = portfolio.sector_exposure_pct(ticker_sector)
            if sector_pct >= max_sector_pct:
                decision.approved = False
                decision.rejection_reason = (
                    f"Sector '{ticker_sector}' already at {sector_pct:.1f}% of portfolio "
                    f"(limit: {max_sector_pct}%)."
                )
                logger.warning(f"[RISK] {proposal.ticker} REJECTED — sector concentration")
                return decision

        # ── Position size cap ─────────────────────────────────────────────
        if proposal.price:
            max_qty = _max_buy_quantity(proposal, portfolio, max_position_pct)

            if max_qty == 0:
                decision.approved = False
                decision.rejection_reason = (
                    f"Insufficient cash (${portfolio.cash:,.2f}) or position limit would be exceeded."
                )
                logger.warning(f"[RISK] {proposal.ticker} REJECTED — no buying power")
                return decision

            if proposal.quantity and proposal.quantity > max_qty:
                decision.warnings.append(
                    f"Quantity reduced from {proposal.quantity} to {max_qty} to stay within "
                    f"{max_position_pct}% position limit."
                )
                decision.adjusted_quantity = max_qty
                logger.info(f"[RISK] {proposal.ticker} quantity capped: {proposal.quantity} → {max_qty}")
            else:
                if not proposal.quantity:
                    decision.adjusted_quantity = max_qty

        # ── Stop loss cap (scaled per scenario) ───────────────────────────
        stop_cap = default_stop_loss_pct * 2
        if decision.stop_loss_pct > stop_cap:
            decision.warnings.append(
                f"Stop loss {decision.stop_loss_pct}% is very wide. Capping at {stop_cap}%."
            )
            decision.stop_loss_pct = stop_cap

    elif proposal.action == "SELL":
        if proposal.ticker not in portfolio.positions:
            decision.approved = False
            decision.rejection_reason = f"Cannot SELL {proposal.ticker} — not in portfolio."
            logger.warning(f"[RISK] {proposal.ticker} SELL REJECTED — not held")
            return decision

        if not proposal.quantity:
            held_qty = portfolio.positions[proposal.ticker]["quantity"]
            decision.adjusted_quantity = held_qty

    if decision.approved:
        logger.info(
            f"[RISK] {proposal.ticker} {proposal.action} APPROVED — "
            f"qty={decision.final_quantity}, stop={decision.stop_loss_pct:.1f}%"
        )

    return decision


def evaluate_all(
    proposals: list[TradeProposal],
    portfolio: PortfolioState,
    ticker_sectors: Optional[dict[str, str]] = None,
    scenario: str = DEFAULT_SCENARIO,
) -> list[RiskDecision]:
    ticker_sectors = ticker_sectors or {}
    return [
        evaluate(p, portfolio, ticker_sectors.get(p.ticker, ""), scenario)
        for p in proposals
    ]
