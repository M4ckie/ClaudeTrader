"""
Execution Simulator — Paper trading engine.

Maintains a virtual portfolio: cash, positions, and trade history.
All trades are simulated with configurable slippage.
Writes to the trades and portfolio_snapshots tables.
"""

import json
import logging
from dataclasses import dataclass, field, asdict
from datetime import date, datetime
from pathlib import Path
from typing import Optional

import sys
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config.settings import (
    INITIAL_CAPITAL,
    SLIPPAGE_PCT,
    COMMISSION_PER_TRADE,
    SCENARIOS,
    DEFAULT_SCENARIO,
)
from data.database import db_session, init_database
from risk.risk_gate import PortfolioState, RiskDecision

logger = logging.getLogger(__name__)


@dataclass
class Position:
    ticker: str
    quantity: int
    avg_price: float
    sector: str = ""
    stop_loss_price: Optional[float] = None

    def current_value(self, price: float) -> float:
        return self.quantity * price

    def unrealized_pnl(self, price: float) -> float:
        return (price - self.avg_price) * self.quantity

    def unrealized_pnl_pct(self, price: float) -> float:
        if self.avg_price == 0:
            return 0.0
        return (price - self.avg_price) / self.avg_price * 100


class Simulator:
    """
    Paper trading portfolio simulator.

    Usage:
        sim = Simulator.load()                    # default scenario
        sim = Simulator.load(scenario="small")    # named scenario
        state = sim.portfolio_state(current_prices)
        sim.execute(risk_decision, current_price, reasoning)
        sim.snapshot(current_prices)
    """

    def __init__(
        self,
        cash: float = INITIAL_CAPITAL,
        positions: Optional[dict] = None,
        scenario: str = DEFAULT_SCENARIO,
    ):
        self.cash = cash
        self.scenario = scenario
        self.positions: dict[str, Position] = positions or {}
        self._start_of_day_value: Optional[float] = None

    # ── Persistence ──────────────────────────────────────────────────────

    @classmethod
    def load(cls, scenario: str = DEFAULT_SCENARIO) -> "Simulator":
        """Load latest portfolio state from DB for a scenario, or create fresh."""
        init_database()
        scenario_cfg = SCENARIOS.get(scenario, SCENARIOS[DEFAULT_SCENARIO])
        initial_capital = scenario_cfg["initial_capital"]

        with db_session() as conn:
            row = conn.execute(
                "SELECT * FROM portfolio_snapshots WHERE scenario = ? ORDER BY date DESC LIMIT 1",
                (scenario,)
            ).fetchone()

            if not row:
                logger.info(
                    "No saved portfolio for scenario '%s' — starting fresh with $%.2f",
                    scenario, initial_capital
                )
                return cls(cash=initial_capital, scenario=scenario)

            positions = {}
            if row["positions_json"]:
                raw = json.loads(row["positions_json"])
                for ticker, data in raw.items():
                    positions[ticker] = Position(
                        ticker=ticker,
                        quantity=data["quantity"],
                        avg_price=data["avg_price"],
                        sector=data.get("sector", ""),
                        stop_loss_price=data.get("stop_loss_price"),
                    )

            sim = cls(cash=row["cash"], positions=positions, scenario=scenario)
            logger.info(
                "Loaded portfolio '%s': cash=$%.2f, positions=%d (from %s)",
                scenario, sim.cash, len(sim.positions), row["date"]
            )
            return sim

    def portfolio_state(self, current_prices: dict[str, float]) -> PortfolioState:
        """Build a PortfolioState for use with the risk gate."""
        positions_dict = {}
        invested = 0.0

        for ticker, pos in self.positions.items():
            price = current_prices.get(ticker, pos.avg_price)
            value = pos.current_value(price)
            invested += value
            positions_dict[ticker] = {
                "quantity": pos.quantity,
                "avg_price": pos.avg_price,
                "current_price": price,
                "sector": pos.sector,
            }

        total_value = self.cash + invested

        # Calculate daily P&L
        daily_pnl_pct = 0.0
        if self._start_of_day_value and self._start_of_day_value > 0:
            daily_pnl_pct = (total_value - self._start_of_day_value) / self._start_of_day_value * 100

        return PortfolioState(
            cash=self.cash,
            total_value=total_value,
            positions=positions_dict,
            daily_pnl_pct=daily_pnl_pct,
        )

    # ── Trading ──────────────────────────────────────────────────────────

    def execute(
        self,
        decision: RiskDecision,
        current_price: float,
        reasoning: str = "",
        ticker_sector: str = "",
    ) -> Optional[dict]:
        """
        Execute an approved risk decision.

        Returns a dict of the executed trade, or None if not executed.
        """
        if not decision.approved:
            logger.info(
                "Skipping %s %s — not approved: %s",
                decision.proposal.action, decision.proposal.ticker, decision.rejection_reason
            )
            return None

        proposal = decision.proposal
        qty = decision.final_quantity
        if not qty or qty <= 0:
            logger.warning("No quantity for %s — skipping", proposal.ticker)
            return None

        # Apply slippage: BUY pays more, SELL gets less
        if proposal.action == "BUY":
            fill_price = current_price * (1 + SLIPPAGE_PCT / 100)
        else:
            fill_price = current_price * (1 - SLIPPAGE_PCT / 100)

        total_value = qty * fill_price + COMMISSION_PER_TRADE

        if proposal.action == "BUY":
            if total_value > self.cash:
                # Adjust quantity to what we can afford
                affordable = int((self.cash - COMMISSION_PER_TRADE) / fill_price)
                if affordable <= 0:
                    logger.warning("Insufficient cash for %s BUY — skipping", proposal.ticker)
                    return None
                qty = affordable
                total_value = qty * fill_price + COMMISSION_PER_TRADE

            self.cash -= total_value

            if proposal.ticker in self.positions:
                # Average up/down
                pos = self.positions[proposal.ticker]
                new_qty = pos.quantity + qty
                pos.avg_price = (pos.quantity * pos.avg_price + qty * fill_price) / new_qty
                pos.quantity = new_qty
            else:
                stop_price = fill_price * (1 - decision.stop_loss_pct / 100)
                self.positions[proposal.ticker] = Position(
                    ticker=proposal.ticker,
                    quantity=qty,
                    avg_price=fill_price,
                    sector=ticker_sector,
                    stop_loss_price=stop_price,
                )

            logger.info(
                "BUY  %s: %d shares @ $%.2f = $%.2f  (cash remaining: $%.2f)",
                proposal.ticker, qty, fill_price, total_value, self.cash
            )

        elif proposal.action == "SELL":
            if proposal.ticker not in self.positions:
                logger.warning("Cannot SELL %s — not in portfolio", proposal.ticker)
                return None

            pos = self.positions[proposal.ticker]
            sell_qty = min(qty, pos.quantity)
            proceeds = sell_qty * fill_price - COMMISSION_PER_TRADE
            realized_pnl = (fill_price - pos.avg_price) * sell_qty

            self.cash += proceeds

            if sell_qty >= pos.quantity:
                del self.positions[proposal.ticker]
            else:
                pos.quantity -= sell_qty

            total_value = proceeds
            logger.info(
                "SELL %s: %d shares @ $%.2f = $%.2f  PnL: $%.2f  (cash: $%.2f)",
                proposal.ticker, sell_qty, fill_price, proceeds, realized_pnl, self.cash
            )
            qty = sell_qty

        # Persist to DB
        trade_record = {
            "ticker": proposal.ticker,
            "action": proposal.action,
            "quantity": qty,
            "price": fill_price,
            "total_value": total_value,
            "slippage": abs(fill_price - current_price) * qty,
            "commission": COMMISSION_PER_TRADE,
            "reasoning": reasoning,
            "confidence": proposal.confidence,
        }
        self._save_trade(trade_record)
        return trade_record

    def check_stop_losses(self, current_prices: dict[str, float]) -> list[str]:
        """
        Check all positions against their stop loss prices.

        Returns list of tickers that hit their stop.
        """
        triggered = []
        for ticker, pos in list(self.positions.items()):
            if pos.stop_loss_price is None:
                continue
            price = current_prices.get(ticker)
            if price and price <= pos.stop_loss_price:
                logger.warning(
                    "STOP LOSS triggered: %s @ $%.2f (stop: $%.2f)",
                    ticker, price, pos.stop_loss_price
                )
                triggered.append(ticker)
        return triggered

    # ── Snapshots ────────────────────────────────────────────────────────

    def snapshot(self, current_prices: dict[str, float], snap_date: Optional[str] = None):
        """Save end-of-day portfolio snapshot to DB."""
        state = self.portfolio_state(current_prices)
        today = snap_date or date.today().isoformat()

        positions_json = {
            ticker: {
                "quantity": pos.quantity,
                "avg_price": pos.avg_price,
                "sector": pos.sector,
                "stop_loss_price": pos.stop_loss_price,
                "current_price": current_prices.get(ticker, pos.avg_price),
            }
            for ticker, pos in self.positions.items()
        }

        daily_pnl = state.total_value - (self._start_of_day_value or state.total_value)
        daily_pnl_pct = state.daily_pnl_pct

        with db_session() as conn:
            conn.execute(
                """
                INSERT INTO portfolio_snapshots
                    (scenario, date, cash, positions_value, total_value, daily_pnl, daily_pnl_pct, positions_json)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(scenario, date) DO UPDATE SET
                    cash = excluded.cash,
                    positions_value = excluded.positions_value,
                    total_value = excluded.total_value,
                    daily_pnl = excluded.daily_pnl,
                    daily_pnl_pct = excluded.daily_pnl_pct,
                    positions_json = excluded.positions_json
                """,
                (
                    self.scenario,
                    today,
                    self.cash,
                    state.invested_value,
                    state.total_value,
                    daily_pnl,
                    daily_pnl_pct,
                    json.dumps(positions_json),
                ),
            )

        logger.info(
            "Snapshot saved: total=$%.2f, cash=$%.2f, invested=$%.2f, daily P&L=%.2f%%",
            state.total_value, self.cash, state.invested_value, daily_pnl_pct
        )

    def set_start_of_day(self, current_prices: dict[str, float]):
        """Record start-of-day value for daily P&L calculation."""
        state = self.portfolio_state(current_prices)
        self._start_of_day_value = state.total_value

    def summary(self, current_prices: dict[str, float]) -> str:
        """Return a human-readable portfolio summary string."""
        state = self.portfolio_state(current_prices)
        lines = [
            f"Portfolio Summary — {date.today().isoformat()}",
            f"  Total Value:   ${state.total_value:>12,.2f}",
            f"  Cash:          ${state.cash:>12,.2f}",
            f"  Invested:      ${state.invested_value:>12,.2f}  ({state.exposure_pct:.1f}% exposure)",
            f"  Daily P&L:     {state.daily_pnl_pct:+.2f}%",
            "",
        ]

        if self.positions:
            lines.append("  Open Positions:")
            for ticker, pos in self.positions.items():
                price = current_prices.get(ticker, pos.avg_price)
                pnl_pct = pos.unrealized_pnl_pct(price)
                pnl_dollar = pos.unrealized_pnl(price)
                stop = f"  stop=${pos.stop_loss_price:.2f}" if pos.stop_loss_price else ""
                lines.append(
                    f"    {ticker:6s}  {pos.quantity:>5} shares @ ${pos.avg_price:.2f}"
                    f"  now=${price:.2f}  P&L={pnl_pct:+.1f}% (${pnl_dollar:+,.0f}){stop}"
                )
        else:
            lines.append("  No open positions.")

        return "\n".join(lines)

    # ── Private helpers ──────────────────────────────────────────────────

    def _save_trade(self, trade: dict):
        """Persist a trade record to the database."""
        with db_session() as conn:
            conn.execute(
                """
                INSERT INTO trades
                    (scenario, ticker, action, quantity, price, total_value, slippage,
                     commission, reasoning, confidence)
                VALUES
                    (:scenario, :ticker, :action, :quantity, :price, :total_value, :slippage,
                     :commission, :reasoning, :confidence)
                """,
                {"scenario": self.scenario, **trade},
            )


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    sim = Simulator.load()
    # Example: show portfolio with mock prices
    mock_prices = {"AAPL": 175.0, "MSFT": 380.0}
    print(sim.summary(mock_prices))
