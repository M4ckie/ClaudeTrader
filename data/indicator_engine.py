"""
Indicator Engine — Computes technical indicators from price data.

Uses pandas-ta to calculate indicators defined in config/settings.py,
then stores results in the indicators table for use in briefings.
"""

import logging
from pathlib import Path
from typing import Optional

import pandas as pd
import pandas_ta as ta

import sys
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from config.settings import INDICATORS, WATCHLIST
from data.database import db_session, get_price_dataframe, init_database

logger = logging.getLogger(__name__)


def compute_indicators(df: pd.DataFrame) -> pd.DataFrame:
    """
    Compute all configured indicators on a price DataFrame.

    Args:
        df: DataFrame with columns: date, open, high, low, close, volume

    Returns:
        DataFrame with indicator columns added
    """
    if df.empty or len(df) < 50:
        logger.warning("Not enough data to compute indicators (need >= 50 rows)")
        return pd.DataFrame()

    result = df.copy()

    # ── Simple Moving Averages ──────────────────────────────────────
    for period in INDICATORS.get("sma", []):
        col_name = f"sma_{period}"
        result[col_name] = ta.sma(result["close"], length=period)

    # ── Exponential Moving Averages ─────────────────────────────────
    for period in INDICATORS.get("ema", []):
        col_name = f"ema_{period}"
        result[col_name] = ta.ema(result["close"], length=period)

    # ── RSI ─────────────────────────────────────────────────────────
    rsi_cfg = INDICATORS.get("rsi", {})
    rsi_len = rsi_cfg.get("length", 14) if isinstance(rsi_cfg, dict) else 14
    result["rsi_14"] = ta.rsi(result["close"], length=rsi_len)

    # ── MACD ────────────────────────────────────────────────────────
    macd_cfg = INDICATORS.get("macd", {})
    macd_df = ta.macd(
        result["close"],
        fast=macd_cfg.get("fast", 12),
        slow=macd_cfg.get("slow", 26),
        signal=macd_cfg.get("signal", 9),
    )
    if macd_df is not None and not macd_df.empty:
        # pandas-ta returns columns like MACD_12_26_9, MACDh_12_26_9, MACDs_12_26_9
        macd_cols = macd_df.columns.tolist()
        result["macd"] = macd_df[macd_cols[0]].values
        result["macd_hist"] = macd_df[macd_cols[1]].values
        result["macd_signal"] = macd_df[macd_cols[2]].values

    # ── ATR ─────────────────────────────────────────────────────────
    atr_cfg = INDICATORS.get("atr", {})
    atr_len = atr_cfg.get("length", 14) if isinstance(atr_cfg, dict) else 14
    result["atr_14"] = ta.atr(
        result["high"], result["low"], result["close"], length=atr_len
    )

    # ── Bollinger Bands ─────────────────────────────────────────────
    bb_cfg = INDICATORS.get("bbands", {})
    bb_df = ta.bbands(
        result["close"],
        length=bb_cfg.get("length", 20),
        std=bb_cfg.get("std", 2.0),
    )
    if bb_df is not None and not bb_df.empty:
        bb_cols = bb_df.columns.tolist()
        # pandas-ta returns: BBL, BBM, BBU, BBB, BBP
        result["bbands_lower"] = bb_df[bb_cols[0]].values
        result["bbands_mid"] = bb_df[bb_cols[1]].values
        result["bbands_upper"] = bb_df[bb_cols[2]].values

    # ── Volume Moving Average & Ratio ───────────────────────────────
    vol_period = INDICATORS.get("volume_sma", 20)
    result["volume_sma_20"] = ta.sma(result["volume"].astype(float), length=vol_period)
    result["volume_ratio"] = result["volume"] / result["volume_sma_20"]

    return result


def save_indicators(conn, ticker: str, df: pd.DataFrame):
    """Store computed indicators in the database."""
    if df.empty:
        return

    indicator_cols = [
        "sma_20", "sma_50", "sma_200", "ema_12", "ema_26",
        "rsi_14", "macd", "macd_signal", "macd_hist",
        "atr_14", "bbands_upper", "bbands_mid", "bbands_lower",
        "volume_sma_20", "volume_ratio",
    ]
    all_cols = ["ticker", "date"] + indicator_cols
    placeholders = ", ".join(f":{k}" for k in all_cols)
    columns = ", ".join(all_cols)
    updates = ", ".join(f"{k} = excluded.{k}" for k in indicator_cols)

    sql = f"""
        INSERT INTO indicators ({columns})
        VALUES ({placeholders})
        ON CONFLICT(ticker, date) DO UPDATE SET {updates}
    """

    rows = []
    for _, row in df.iterrows():
        date_str = row["date"]
        if hasattr(date_str, "strftime"):
            date_str = date_str.strftime("%Y-%m-%d")
        values = {"ticker": ticker, "date": date_str}
        for col in indicator_cols:
            val = row.get(col)
            values[col] = float(val) if pd.notna(val) else None
        rows.append(values)

    conn.executemany(sql, rows)


def compute_and_store(tickers: Optional[list[str]] = None):
    """
    Main entry point: compute indicators for all tickers and save.

    Args:
        tickers: List of tickers. Defaults to WATCHLIST.
    """
    tickers = tickers or WATCHLIST
    init_database()

    with db_session() as conn:
        for ticker in tickers:
            logger.info(f"Computing indicators for {ticker}")
            df = get_price_dataframe(conn, ticker, days=300)

            if df.empty:
                logger.warning(f"No price data for {ticker}, skipping indicators")
                continue

            indicators_df = compute_indicators(df)
            save_indicators(conn, ticker, indicators_df)
            logger.info(f"Saved indicators for {ticker} ({len(indicators_df)} rows)")

    logger.info(f"Indicator computation complete for {len(tickers)} tickers")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    compute_and_store()
