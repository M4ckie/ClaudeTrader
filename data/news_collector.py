"""
News Collector — Fetches financial news headlines from NewsAPI.

Free tier: 100 requests/day, so we're strategic about queries.
Headlines are stored raw — the LLM handles interpretation in the
briefing layer rather than us doing NLP preprocessing.
"""

import logging
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

import requests

import sys
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from config.settings import NEWS_API_KEY, NEWS_MAX_ARTICLES, NEWS_LOOKBACK_DAYS, WATCHLIST
from data.database import db_session, init_database

logger = logging.getLogger(__name__)

NEWS_API_URL = "https://newsapi.org/v2/everything"


def fetch_news(
    query: str,
    from_date: Optional[str] = None,
    max_articles: int = NEWS_MAX_ARTICLES,
) -> list[dict]:
    """
    Fetch news articles from NewsAPI.

    Args:
        query: Search query (usually ticker + company name)
        from_date: Earliest date (YYYY-MM-DD). Defaults to NEWS_LOOKBACK_DAYS ago.
        max_articles: Max number of articles to return.

    Returns:
        List of article dicts with: title, source, url, publishedAt
    """
    if not NEWS_API_KEY:
        logger.error("NEWS_API_KEY not set — skipping news collection")
        return []

    if from_date is None:
        from_date = (
            datetime.now() - timedelta(days=NEWS_LOOKBACK_DAYS)
        ).strftime("%Y-%m-%d")

    params = {
        "q": query,
        "from": from_date,
        "sortBy": "relevancy",
        "pageSize": max_articles,
        "language": "en",
        "apiKey": NEWS_API_KEY,
    }

    try:
        resp = requests.get(NEWS_API_URL, params=params, timeout=15)
        resp.raise_for_status()
        data = resp.json()

        if data.get("status") != "ok":
            logger.warning(f"NewsAPI returned status: {data.get('status')}")
            return []

        articles = data.get("articles", [])
        return [
            {
                "title": a.get("title", ""),
                "source": a.get("source", {}).get("name", ""),
                "url": a.get("url", ""),
                "published_at": a.get("publishedAt", ""),
            }
            for a in articles
            if a.get("title")  # skip articles with no title
        ]

    except requests.RequestException as e:
        logger.error(f"NewsAPI request failed for '{query}': {e}")
        return []


def build_search_query(ticker: str, company_name: str = "") -> str:
    """
    Build a search query that finds relevant articles.

    Using just a ticker like "AAPL" can return noise. Combining
    with the company name improves relevance.
    """
    if company_name:
        return f'"{ticker}" OR "{company_name}"'
    return f'"{ticker}" stock'


def save_news(conn, ticker: str, articles: list[dict]):
    """Save news articles, avoiding duplicates based on URL."""
    for article in articles:
        # Check for duplicate by URL
        existing = conn.execute(
            "SELECT id FROM news WHERE url = ?", (article["url"],)
        ).fetchone()

        if existing:
            continue

        conn.execute(
            """
            INSERT INTO news (ticker, headline, source, url, published_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                ticker,
                article["title"],
                article["source"],
                article["url"],
                article["published_at"],
            ),
        )


def collect_news(tickers: Optional[list[str]] = None):
    """
    Main entry point: fetch and store news for all watched tickers.

    Note: With 100 req/day on free tier and ~10 tickers, we use ~10 requests
    per run. Running twice daily uses ~20, leaving plenty of headroom.

    Args:
        tickers: List of tickers. Defaults to WATCHLIST.
    """
    if not NEWS_API_KEY:
        logger.warning(
            "NEWS_API_KEY not configured. Set it in config/settings_local.py. "
            "Skipping news collection."
        )
        return

    tickers = tickers or WATCHLIST
    init_database()

    with db_session() as conn:
        for ticker in tickers:
            # Get company name from stocks table for better search
            row = conn.execute(
                "SELECT name FROM stocks WHERE ticker = ?", (ticker,)
            ).fetchone()
            company_name = row["name"] if row else ""

            query = build_search_query(ticker, company_name)
            logger.info(f"Fetching news for {ticker} (query: {query})")

            articles = fetch_news(query)
            save_news(conn, ticker, articles)

            logger.info(f"Saved {len(articles)} articles for {ticker}")

    logger.info(f"News collection complete for {len(tickers)} tickers")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    collect_news()
