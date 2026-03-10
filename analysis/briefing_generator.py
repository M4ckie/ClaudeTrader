"""
Briefing Generator — Reads from the database and builds structured,
LLM-ready briefings for each ticker on the watchlist.

A briefing is a rich text summary of everything we know about a stock:
price action, technical indicators, fundamentals, earnings, and recent news.
The strategy layer feeds these to Claude to make trading decisions.
"""

import logging
from datetime import date, datetime
from pathlib import Path
from typing import Optional

import sys
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config.settings import WATCHLIST
from config.settings import DEFAULT_SCENARIO
from data.database import (
    db_session,
    get_latest_indicators,
    get_latest_fundamentals,
    get_recent_news,
    get_next_earnings,
    get_recent_trades,
    get_price_dataframe,
)

logger = logging.getLogger(__name__)


def _fmt(val, fmt=".2f", fallback="N/A") -> str:
    """Format a numeric value, returning fallback if None."""
    if val is None:
        return fallback
    try:
        return format(float(val), fmt)
    except (TypeError, ValueError):
        return fallback


def _trend_label(close: float, sma_20: float, sma_50: float, sma_200: float) -> str:
    """Summarise price trend relative to moving averages."""
    above = []
    below = []
    for label, val in [("SMA20", sma_20), ("SMA50", sma_50), ("SMA200", sma_200)]:
        if val is None:
            continue
        if close > val:
            above.append(label)
        else:
            below.append(label)
    if not above and not below:
        return "trend unknown"
    parts = []
    if above:
        parts.append(f"above {', '.join(above)}")
    if below:
        parts.append(f"below {', '.join(below)}")
    return "; ".join(parts)


def _rsi_label(rsi: Optional[float]) -> str:
    if rsi is None:
        return "RSI N/A"
    if rsi >= 70:
        return f"RSI {rsi:.1f} (overbought)"
    if rsi <= 30:
        return f"RSI {rsi:.1f} (oversold)"
    return f"RSI {rsi:.1f} (neutral)"


def _macd_label(macd: Optional[float], signal: Optional[float], hist: Optional[float]) -> str:
    if macd is None or signal is None:
        return "MACD N/A"
    direction = "bullish" if hist and hist > 0 else "bearish"
    cross = "above" if macd > signal else "below"
    return f"MACD {_fmt(macd)} {cross} signal {_fmt(signal)} ({direction})"


def _price_change(df) -> str:
    """Calculate recent price change stats from price dataframe."""
    if df.empty or len(df) < 2:
        return "insufficient data"
    latest = df.iloc[-1]
    prev = df.iloc[-2]
    day_chg = (latest["close"] - prev["close"]) / prev["close"] * 100

    results = [f"1d: {day_chg:+.2f}%"]

    if len(df) >= 5:
        w_close = df.iloc[-5]["close"]
        w_chg = (latest["close"] - w_close) / w_close * 100
        results.append(f"5d: {w_chg:+.2f}%")

    if len(df) >= 20:
        m_close = df.iloc[-20]["close"]
        m_chg = (latest["close"] - m_close) / m_close * 100
        results.append(f"20d: {m_chg:+.2f}%")

    return ", ".join(results)


def build_ticker_briefing(conn, ticker: str) -> str:
    """
    Build a complete briefing for a single ticker.

    Returns a formatted string ready to be included in an LLM prompt.
    """
    lines = [f"=== {ticker} ==="]

    # ── Price data ──────────────────────────────────────────────────────
    df = get_price_dataframe(conn, ticker, days=200)
    if df.empty:
        lines.append("  [No price data available — skipping]")
        return "\n".join(lines)

    latest_price = df.iloc[-1]
    latest_date = str(latest_price["date"])[:10]

    lines.append(f"  Date:   {latest_date}")
    lines.append(f"  Price:  ${_fmt(latest_price['close'])}  "
                 f"(O:{_fmt(latest_price['open'])}  "
                 f"H:{_fmt(latest_price['high'])}  "
                 f"L:{_fmt(latest_price['low'])})  "
                 f"Vol: {int(latest_price['volume'] or 0):,}")
    lines.append(f"  Change: {_price_change(df)}")

    # ── Technical indicators ─────────────────────────────────────────────
    ind = get_latest_indicators(conn, ticker)
    if ind:
        close = float(latest_price["close"])
        lines.append("")
        lines.append("  Technical Indicators:")
        lines.append(f"    Trend:    {_trend_label(close, ind.get('sma_20'), ind.get('sma_50'), ind.get('sma_200'))}")
        lines.append(f"    Momentum: {_rsi_label(ind.get('rsi_14'))}")
        lines.append(f"    MACD:     {_macd_label(ind.get('macd'), ind.get('macd_signal'), ind.get('macd_hist'))}")
        lines.append(f"    ATR(14):  {_fmt(ind.get('atr_14'))}  (volatility proxy)")

        bb_upper = ind.get("bbands_upper")
        bb_lower = ind.get("bbands_lower")
        if bb_upper and bb_lower:
            bb_pos = "upper band" if close > bb_upper else ("lower band" if close < bb_lower else "within bands")
            lines.append(f"    BBands:   {_fmt(bb_lower)} – {_fmt(bb_upper)}  (price {bb_pos})")

        vol_ratio = ind.get("volume_ratio")
        if vol_ratio:
            vol_label = "high" if vol_ratio > 1.5 else ("low" if vol_ratio < 0.7 else "average")
            lines.append(f"    Volume:   {_fmt(vol_ratio)}x avg ({vol_label})")
    else:
        lines.append("  Technical Indicators: [not available]")

    # ── Fundamentals ─────────────────────────────────────────────────────
    fund = get_latest_fundamentals(conn, ticker)
    if fund:
        lines.append("")
        lines.append(f"  Fundamentals ({fund.get('period', 'latest')}):")
        lines.append(f"    Revenue:   ${_fmt(fund.get('revenue'), ',.0f')}")
        lines.append(f"    Net Income:${_fmt(fund.get('net_income'), ',.0f')}")
        lines.append(f"    EPS:       {_fmt(fund.get('eps'))}")
        lines.append(f"    P/E:       {_fmt(fund.get('pe_ratio'))}")
        lines.append(f"    P/B:       {_fmt(fund.get('pb_ratio'))}")
        lines.append(f"    ROE:       {_fmt(fund.get('roe'))}")
        lines.append(f"    D/E:       {_fmt(fund.get('debt_to_equity'))}")
        lines.append(f"    Div Yield: {_fmt(fund.get('dividend_yield'))}%")

    # ── Earnings ─────────────────────────────────────────────────────────
    earnings = get_next_earnings(conn, ticker)
    if earnings:
        lines.append("")
        lines.append(f"  Next Earnings: {earnings.get('earnings_date', 'N/A')}"
                     f"  (est. EPS: {_fmt(earnings.get('estimate_eps'))})")

    # ── News ─────────────────────────────────────────────────────────────
    news = get_recent_news(conn, ticker, limit=5)
    if news:
        lines.append("")
        lines.append("  Recent News:")
        for article in news:
            pub = str(article.get("published_at", ""))[:10]
            source = article.get("source", "")
            headline = article.get("headline", "")
            lines.append(f"    [{pub}] {source}: {headline}")
    else:
        lines.append("")
        lines.append("  Recent News: [none available]")

    return "\n".join(lines)


def _build_trade_history(conn, scenario: str, limit: int = 15) -> Optional[str]:
    """
    Build a compact recent trade history section for the briefing.

    Gives Claude continuity — it can see what it decided recently, the
    reasoning behind those decisions, and whether the thesis held up.
    """
    trades = get_recent_trades(conn, scenario, limit=limit)
    if not trades:
        return None

    lines = ["RECENT TRADE HISTORY (most recent first):"]
    for t in trades:
        date_str = str(t.get("executed_at", ""))[:10]
        action = t.get("action", "")
        ticker = t.get("ticker", "")
        qty = t.get("quantity", "")
        price = t.get("price")
        reasoning = (t.get("reasoning") or "").strip()
        price_str = f"@ ${price:.2f}" if price else ""
        reasoning_str = f'  — "{reasoning}"' if reasoning else ""
        lines.append(f"  {date_str}  {action:4s}  {ticker:6s}  {qty} shares {price_str}{reasoning_str}")

    return "\n".join(lines)


def build_market_briefing(
    tickers: Optional[list[str]] = None,
    portfolio_context: Optional[str] = None,
    scenario: str = DEFAULT_SCENARIO,
) -> str:
    """
    Build a full market briefing across all watched tickers.

    Args:
        tickers: Tickers to include. Defaults to WATCHLIST.
        portfolio_context: Optional string describing current portfolio state
                           (cash, open positions) to include in the briefing.

    Returns:
        A complete briefing string ready for Claude.
    """
    tickers = tickers or WATCHLIST
    today = date.today().isoformat()

    sections = [
        f"MARKET BRIEFING — {today}",
        "=" * 60,
        "",
        "You are a swing trading analyst. Review the following data for each ticker",
        "and decide whether to BUY, SELL (if held), or HOLD/PASS for each.",
        "",
    ]

    if portfolio_context:
        sections.append("CURRENT PORTFOLIO:")
        sections.append(portfolio_context)
        sections.append("")

    with db_session() as conn:
        trade_history = _build_trade_history(conn, scenario)
        if trade_history:
            sections.append(trade_history)
            sections.append("")

        sections.append("TICKER DATA:")
        sections.append("")

        for ticker in tickers:
            try:
                briefing = build_ticker_briefing(conn, ticker)
                sections.append(briefing)
                sections.append("")
            except Exception as e:
                logger.error("Failed to build briefing for %s [%s]: %s", ticker, type(e).__name__, e)
                sections.append(f"=== {ticker} === [Error: {e}]")
                sections.append("")

    logger.info("Built market briefing for %d tickers", len(tickers))
    return "\n".join(sections)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    briefing = build_market_briefing()
    print(briefing)
