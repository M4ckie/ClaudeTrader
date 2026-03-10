"""
Stock Screener — Finds trade candidates beyond the static watchlist.

Pulls from Yahoo Finance's pre-built screeners (most active, top gainers,
top losers) and applies basic quality filters. Results are merged with the
static WATCHLIST before each trading run.

Also provides news-based ticker extraction: scans stored headlines for
$TICKER mentions and parenthetical symbols to surface trending names.
"""

import logging
import re
from pathlib import Path
from typing import Optional

import requests

import sys
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config.settings import (
    SCREENER_ENABLED,
    SCREENER_MAX_CANDIDATES,
    SCREENER_MIN_PRICE,
    SCREENER_MIN_VOLUME,
    SCREENER_SCREENS,
    WATCHLIST,
)
from data.database import db_session

logger = logging.getLogger(__name__)

_YAHOO_SCREENER_URL = "https://query1.finance.yahoo.com/v1/finance/screener/predefined/saved"
_HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; ClaudeTrader/1.0)"}

# Keywords that indicate an ETF/fund rather than a stock
_ETF_KEYWORDS = {
    "ETF", "FUND", "TRUST", "INDEX", "SHARES", "ISHARES",
    "SPDR", "VANGUARD", "INVESCO", "PROSHARES",
}

# Common uppercase words that appear in headlines but aren't tickers
_HEADLINE_NOISE = {
    "THE", "AND", "FOR", "ARE", "BUT", "NOT", "YOU", "ALL", "CAN",
    "HER", "WAS", "ONE", "OUR", "OUT", "DAY", "GET", "HAS", "HIM",
    "HIS", "HOW", "ITS", "MAY", "NEW", "NOW", "OLD", "SEE", "TWO",
    "WHO", "DID", "LET", "PUT", "SAY", "SHE", "TOO", "USE", "CEO",
    "CFO", "CTO", "IPO", "ETF", "USD", "GDP", "FED", "SEC", "NYSE",
    "DJIA", "ESG", "EPS", "LLC", "INC", "LTD", "PLC", "USA", "U.S",
    "AI", "Q1", "Q2", "Q3", "Q4", "YOY", "QOQ", "TTM",
}


def _fetch_yahoo_screen(screen_id: str, count: int = 30) -> list[dict]:
    """Fetch quotes from a Yahoo Finance predefined screener."""
    params = {"formatted": "false", "scrIds": screen_id, "count": count}
    try:
        resp = requests.get(
            _YAHOO_SCREENER_URL, params=params, headers=_HEADERS, timeout=15
        )
        resp.raise_for_status()
        data = resp.json()
        results = data.get("finance", {}).get("result") or []
        return results[0].get("quotes", []) if results else []
    except Exception as e:
        logger.warning("Yahoo screener '%s' failed: %s", screen_id, e)
        return []


def _is_etf(quote: dict) -> bool:
    qtype = (quote.get("quoteType") or "").upper()
    if qtype in ("ETF", "MUTUALFUND", "INDEX"):
        return True
    name = (quote.get("shortName") or quote.get("longName") or "").upper()
    return any(kw in name for kw in _ETF_KEYWORDS)


def screen_candidates(
    exclude_tickers: Optional[list[str]] = None,
    max_candidates: int = SCREENER_MAX_CANDIDATES,
    min_price: float = SCREENER_MIN_PRICE,
    min_volume: int = SCREENER_MIN_VOLUME,
) -> list[str]:
    """
    Run configured screeners and return a filtered list of candidate tickers.

    Args:
        exclude_tickers: Tickers to skip (e.g. existing WATCHLIST).
        max_candidates: Maximum number of new tickers to return.
        min_price: Skip stocks below this price (filters penny stocks).
        min_volume: Skip stocks below this volume (filters illiquid names).

    Returns:
        List of ticker symbols, deduplicated and filtered.
    """
    if not SCREENER_ENABLED:
        return []

    exclude = set(t.upper() for t in (exclude_tickers or []))
    candidates: dict[str, dict] = {}

    for screen_id in SCREENER_SCREENS:
        logger.info("Running screener: %s", screen_id)
        quotes = _fetch_yahoo_screen(screen_id, count=max_candidates * 3)

        for q in quotes:
            ticker = (q.get("symbol") or "").upper().strip()
            if not ticker or "." in ticker:
                continue
            if ticker in exclude or ticker in candidates:
                continue
            if _is_etf(q):
                continue

            price = float(q.get("regularMarketPrice") or 0)
            volume = int(q.get("regularMarketVolume") or 0)

            if price < min_price or volume < min_volume:
                continue

            candidates[ticker] = q
            logger.debug("  Candidate: %s  $%.2f  vol=%d", ticker, price, volume)

        if len(candidates) >= max_candidates:
            break

    result = list(candidates.keys())[:max_candidates]
    logger.info("Screener found %d candidate(s): %s", len(result), result)
    return result


def extract_news_tickers(
    exclude_tickers: Optional[list[str]] = None,
    limit_days: int = 3,
    max_results: int = 8,
) -> list[str]:
    """
    Scan recently stored news headlines for stock ticker symbols.

    Looks for $TICKER patterns and parenthetical symbols like (NVDA).
    Filters out common words and existing watchlist tickers to surface
    genuinely novel names being discussed in the news.

    Args:
        exclude_tickers: Tickers to skip (e.g. WATCHLIST).
        limit_days: How many days of news to scan.
        max_results: Max tickers to return.

    Returns:
        Tickers sorted by mention frequency (most mentioned first).
    """
    exclude = set(t.upper() for t in (exclude_tickers or WATCHLIST))

    dollar_re = re.compile(r'\$([A-Z]{1,5})\b')
    paren_re = re.compile(r'\(([A-Z]{2,5})\)')

    mention_count: dict[str, int] = {}

    with db_session() as conn:
        rows = conn.execute(
            """
            SELECT headline FROM news
            WHERE date(published_at) >= date('now', ?)
            """,
            (f"-{limit_days} days",),
        ).fetchall()

    for row in rows:
        headline = row[0] or ""
        for t in dollar_re.findall(headline):
            mention_count[t] = mention_count.get(t, 0) + 2  # $TICKER weighted higher
        for t in paren_re.findall(headline):
            mention_count[t] = mention_count.get(t, 0) + 1

    novel = {
        t: c for t, c in mention_count.items()
        if t not in exclude and t not in _HEADLINE_NOISE and 2 <= len(t) <= 5
    }

    ranked = sorted(novel, key=lambda t: novel[t], reverse=True)
    result = ranked[:max_results]
    logger.info("News extraction found %d novel ticker(s): %s", len(result), result)
    return result


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    print("Screener candidates:", screen_candidates())
    print("News tickers:", extract_news_tickers())
