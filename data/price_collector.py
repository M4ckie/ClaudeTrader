"""
Price Collector — Fetches OHLCV data from Yahoo Finance via yfinance.

Supports both initial historical load and incremental daily updates.
Caches everything in SQLite to avoid redundant API calls.
"""

import logging
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

import pandas as pd
import yfinance as yf

import sys
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from data.utils import suppress_output as _suppress_output


def _has_trading_days(start_date: str, end_date: str) -> bool:
    """Return True if there are any weekdays between start and end date."""
    bdays = pd.bdate_range(start=start_date, end=end_date)
    return len(bdays) > 0
from config.settings import PRICE_HISTORY_DAYS, WATCHLIST
from data.database import db_session, get_latest_price_date, init_database

logger = logging.getLogger(__name__)


def fetch_stock_metadata(ticker: str) -> dict:
    """Fetch basic stock info (name, sector, market cap) from yfinance."""
    try:
        info = yf.Ticker(ticker).info
        return {
            "ticker": ticker,
            "name": info.get("shortName", ""),
            "sector": info.get("sector", ""),
            "industry": info.get("industry", ""),
            "market_cap": info.get("marketCap", 0),
        }
    except Exception as e:
        logger.warning("Could not fetch metadata for %s [%s]: %s", ticker, type(e).__name__, e)
        return {
            "ticker": ticker,
            "name": "",
            "sector": "",
            "industry": "",
            "market_cap": 0,
        }


def upsert_stock_metadata(conn, metadata: dict):
    """Insert or update stock metadata."""
    conn.execute(
        """
        INSERT INTO stocks (ticker, name, sector, industry, market_cap)
        VALUES (:ticker, :name, :sector, :industry, :market_cap)
        ON CONFLICT(ticker) DO UPDATE SET
            name = excluded.name,
            sector = excluded.sector,
            industry = excluded.industry,
            market_cap = excluded.market_cap
        """,
        metadata,
    )


def fetch_price_history(
    ticker: str,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
) -> pd.DataFrame:
    """
    Fetch OHLCV data from yfinance.

    Args:
        ticker: Stock ticker symbol
        start_date: Start date (YYYY-MM-DD). Defaults to PRICE_HISTORY_DAYS ago.
        end_date: End date (YYYY-MM-DD). Defaults to today.

    Returns:
        DataFrame with columns: date, open, high, low, close, volume, adj_close
    """
    if start_date is None:
        start_date = (
            datetime.now() - timedelta(days=PRICE_HISTORY_DAYS)
        ).strftime("%Y-%m-%d")
    if end_date is None:
        end_date = datetime.now().strftime("%Y-%m-%d")

    logger.info("Fetching %s prices from %s to %s", ticker, start_date, end_date)

    try:
        with _suppress_output():
            df = yf.download(
                ticker,
                start=start_date,
                end=end_date,
                progress=False,
                auto_adjust=False,
            )
    except Exception as e:
        logger.error("yfinance download failed for %s [%s]: %s", ticker, type(e).__name__, e)
        return pd.DataFrame()

    if df.empty:
        logger.warning("No price data returned for %s", ticker)
        return pd.DataFrame()

    # Normalize column names (yfinance sometimes returns MultiIndex)
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)

    df = df.reset_index()
    df = df.rename(columns={
        "Date": "date",
        "Open": "open",
        "High": "high",
        "Low": "low",
        "Close": "close",
        "Volume": "volume",
        "Adj Close": "adj_close",
    })

    # Ensure date is a string
    df["date"] = pd.to_datetime(df["date"]).dt.strftime("%Y-%m-%d")
    df["ticker"] = ticker

    # Select and order columns
    cols = ["ticker", "date", "open", "high", "low", "close", "volume", "adj_close"]
    available = [c for c in cols if c in df.columns]
    df = df[available]

    logger.info("Fetched %d rows for %s", len(df), ticker)
    return df


def save_prices(conn, df: pd.DataFrame):
    """Upsert price data into the database."""
    if df.empty:
        return

    rows = [
        (
            row["ticker"],
            row["date"],
            row.get("open"),
            row.get("high"),
            row.get("low"),
            row.get("close"),
            row.get("volume"),
            row.get("adj_close"),
        )
        for _, row in df.iterrows()
    ]
    conn.executemany(
        """
        INSERT INTO daily_prices (ticker, date, open, high, low, close, volume, adj_close)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(ticker, date) DO UPDATE SET
            open = excluded.open,
            high = excluded.high,
            low = excluded.low,
            close = excluded.close,
            volume = excluded.volume,
            adj_close = excluded.adj_close
        """,
        rows,
    )


def collect_prices(tickers: Optional[list[str]] = None, full_refresh: bool = False):
    """
    Main entry point: fetch and store prices for all watched tickers.

    Args:
        tickers: List of tickers to fetch. Defaults to WATCHLIST.
        full_refresh: If True, re-fetch full history. Otherwise incremental.
    """
    tickers = tickers or WATCHLIST
    init_database()

    with db_session() as conn:
        for ticker in tickers:
            # Upsert metadata
            meta = fetch_stock_metadata(ticker)
            upsert_stock_metadata(conn, meta)

            # Determine start date for incremental fetch
            start_date = None
            if not full_refresh:
                latest = get_latest_price_date(conn, ticker)
                if latest:
                    # Fetch from the day after our latest data
                    start_date = (
                        datetime.strptime(latest, "%Y-%m-%d") + timedelta(days=1)
                    ).strftime("%Y-%m-%d")

                            # Skip if no trading days have occurred since last fetch
                    today = datetime.now().strftime("%Y-%m-%d")
                    if not _has_trading_days(start_date, today):
                        logger.info("%s is up to date (latest: %s)", ticker, latest)
                        continue

            # Fetch and save
            df = fetch_price_history(ticker, start_date=start_date)
            save_prices(conn, df)

    logger.info("Price collection complete for %d tickers", len(tickers))


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    collect_prices()
