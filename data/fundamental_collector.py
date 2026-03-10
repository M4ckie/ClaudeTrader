"""
Fundamental Collector — Fetches financial statements, ratios, and
earnings calendar via yfinance (no API key required).

yfinance provides income statements, balance sheets, key ratios, and
earnings history directly from Yahoo Finance for free.
"""

import logging
from datetime import datetime
from pathlib import Path
from typing import Optional

import yfinance as yf

import sys
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from data.utils import suppress_output as _suppress_output
from config.settings import WATCHLIST
from data.database import db_session, init_database

logger = logging.getLogger(__name__)


def _safe_float(val) -> Optional[float]:
    try:
        return float(val) if val is not None else None
    except (TypeError, ValueError):
        return None


def fetch_fundamentals(ticker: str) -> Optional[dict]:
    """
    Fetch fundamentals for a ticker via yfinance.

    Returns a dict with income, ratio, and earnings data, or None on failure.
    """
    try:
        with _suppress_output():
            t = yf.Ticker(ticker)
            info = t.info or {}
            income_stmt = t.quarterly_income_stmt
            balance_sheet = t.quarterly_balance_sheet

        revenue = None
        net_income = None
        eps = None
        period_str = "latest"

        if income_stmt is not None and not income_stmt.empty:
            col = income_stmt.columns[0]  # most recent quarter
            dt = col if isinstance(col, datetime) else datetime.strptime(str(col)[:10], "%Y-%m-%d")
            quarter = (dt.month - 1) // 3 + 1
            period_str = f"{dt.year}-Q{quarter}"

            revenue = _safe_float(income_stmt.loc["Total Revenue", col]) if "Total Revenue" in income_stmt.index else None
            net_income = _safe_float(income_stmt.loc["Net Income", col]) if "Net Income" in income_stmt.index else None

        # EPS from info (trailing twelve months)
        eps = _safe_float(info.get("trailingEps"))

        # Ratios from info
        pe_ratio = _safe_float(info.get("trailingPE"))
        pb_ratio = _safe_float(info.get("priceToBook"))
        roe = _safe_float(info.get("returnOnEquity"))
        dividend_yield = _safe_float(info.get("dividendYield"))
        if dividend_yield:
            dividend_yield *= 100  # convert to percentage

        # Debt to equity from balance sheet or info
        debt_to_equity = _safe_float(info.get("debtToEquity"))

        # Free cash flow per share
        fcf = _safe_float(info.get("freeCashflow"))
        shares = _safe_float(info.get("sharesOutstanding"))
        free_cash_flow_per_share = (fcf / shares) if fcf and shares else None

        return {
            "period": period_str,
            "revenue": revenue,
            "net_income": net_income,
            "eps": eps,
            "pe_ratio": pe_ratio,
            "pb_ratio": pb_ratio,
            "debt_to_equity": debt_to_equity,
            "roe": roe,
            "free_cash_flow": free_cash_flow_per_share,
            "dividend_yield": dividend_yield,
        }

    except Exception as e:
        logger.error("Failed to fetch fundamentals for %s [%s]: %s", ticker, type(e).__name__, e)
        return None


def fetch_earnings_calendar(ticker: str) -> list[dict]:
    """
    Fetch upcoming and recent earnings dates via yfinance.

    Returns list of dicts with: date, estimate_eps, actual_eps, surprise_pct
    """
    try:
        with _suppress_output():
            t = yf.Ticker(ticker)
            earnings = t.earnings_dates

        if earnings is None or earnings.empty:
            return []

        results = []
        for date_idx, row in earnings.head(5).iterrows():
            date_str = str(date_idx)[:10]
            estimate = _safe_float(row.get("EPS Estimate"))
            actual = _safe_float(row.get("Reported EPS"))
            surprise = _safe_float(row.get("Surprise(%)"))

            results.append({
                "date": date_str,
                "estimate_eps": estimate,
                "actual_eps": actual,
                "surprise_pct": surprise,
            })

        return results

    except Exception as e:
        logger.error("Failed to fetch earnings calendar for %s [%s]: %s", ticker, type(e).__name__, e)
        return []


def save_fundamentals(conn, ticker: str, data: dict):
    """Save fundamental data to DB."""
    conn.execute(
        """
        INSERT INTO fundamentals
            (ticker, period, revenue, net_income, eps, pe_ratio, pb_ratio,
             debt_to_equity, roe, free_cash_flow, dividend_yield)
        VALUES
            (:ticker, :period, :revenue, :net_income, :eps, :pe_ratio, :pb_ratio,
             :debt_to_equity, :roe, :free_cash_flow, :dividend_yield)
        ON CONFLICT(ticker, period) DO UPDATE SET
            revenue = excluded.revenue,
            net_income = excluded.net_income,
            eps = excluded.eps,
            pe_ratio = excluded.pe_ratio,
            pb_ratio = excluded.pb_ratio,
            debt_to_equity = excluded.debt_to_equity,
            roe = excluded.roe,
            free_cash_flow = excluded.free_cash_flow,
            dividend_yield = excluded.dividend_yield,
            fetched_at = datetime('now')
        """,
        {"ticker": ticker, **data},
    )


def save_earnings_calendar(conn, ticker: str, earnings: list[dict]):
    """Save earnings calendar entries."""
    for entry in earnings:
        conn.execute(
            """
            INSERT INTO earnings_calendar
                (ticker, earnings_date, estimate_eps, actual_eps, surprise_pct)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(ticker, earnings_date) DO UPDATE SET
                estimate_eps = excluded.estimate_eps,
                actual_eps = excluded.actual_eps,
                surprise_pct = excluded.surprise_pct,
                fetched_at = datetime('now')
            """,
            (ticker, entry["date"], entry["estimate_eps"], entry["actual_eps"], entry["surprise_pct"]),
        )


def collect_fundamentals(tickers: Optional[list[str]] = None):
    """
    Main entry point: fetch and store fundamentals for all watched tickers.

    Args:
        tickers: List of tickers. Defaults to WATCHLIST.
    """
    tickers = tickers or WATCHLIST
    init_database()

    with db_session() as conn:
        for ticker in tickers:
            logger.info(f"Fetching fundamentals for {ticker}")

            data = fetch_fundamentals(ticker)
            if data:
                save_fundamentals(conn, ticker, data)

            earnings = fetch_earnings_calendar(ticker)
            if earnings:
                save_earnings_calendar(conn, ticker, earnings)

            logger.info(f"Saved fundamentals for {ticker}")

    logger.info(f"Fundamental collection complete for {len(tickers)} tickers")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    collect_fundamentals()
