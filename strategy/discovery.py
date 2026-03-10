"""
LLM Discovery — Asks Claude to suggest new tickers to research.

Sends recent market headlines as context and asks Claude to surface
stocks worth investigating beyond the static watchlist. Results are
merged into the day's research pool — not permanently added to WATCHLIST.
"""

import json
import logging
import re
from pathlib import Path
from typing import Optional

import sys
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import anthropic

from config.settings import ANTHROPIC_API_KEY, LLM_MODEL, WATCHLIST
from data.database import db_session

logger = logging.getLogger(__name__)

_SYSTEM_PROMPT = """\
You are a stock market research assistant helping a swing trading bot discover opportunities.
Suggest stocks that have notable catalysts today: earnings surprises, analyst upgrades/downgrades,
M&A news, product launches, or strong technical setups.

Rules:
- No ETFs, no mutual funds, no penny stocks (under $10)
- No tickers from the provided exclusion list
- US-listed stocks only
- Return ONLY a JSON array of ticker symbols, e.g. ["TSLA", "AMZN", "NFLX"]
- If nothing compelling stands out, return an empty array: []
"""


def discover_tickers(
    exclude_tickers: Optional[list[str]] = None,
    max_suggestions: int = 5,
) -> list[str]:
    """
    Ask Claude to suggest tickers worth researching today.

    Uses recent news stored in the DB as context so Claude has
    something concrete to reason about rather than just its training data.

    Args:
        exclude_tickers: Tickers already on the watchlist (Claude won't repeat these).
        max_suggestions: Maximum number of suggestions to accept.

    Returns:
        List of ticker symbols suggested by Claude.
    """
    if not ANTHROPIC_API_KEY:
        logger.warning("ANTHROPIC_API_KEY not set — skipping LLM discovery")
        return []

    exclude = set(t.upper() for t in (exclude_tickers or WATCHLIST))

    # Pull recent headlines from DB as context
    with db_session() as conn:
        rows = conn.execute(
            """
            SELECT ticker, headline, published_at
            FROM news
            WHERE date(published_at) >= date('now', '-3 days')
            ORDER BY published_at DESC
            LIMIT 40
            """,
        ).fetchall()

    if rows:
        news_lines = "\n".join(
            f"  [{r['ticker']}] {r['headline']}" for r in rows
        )
        news_section = f"Recent news from watchlist:\n{news_lines}"
    else:
        news_section = "(no recent news in database)"

    prompt = (
        f"Suggest up to {max_suggestions} US stocks for swing trading research today.\n\n"
        f"EXCLUDE these tickers (already on watchlist): {', '.join(sorted(exclude))}\n\n"
        f"{news_section}\n\n"
        f"Return ONLY a JSON array of ticker symbols."
    )

    try:
        client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
        message = client.messages.create(
            model=LLM_MODEL,
            max_tokens=150,
            system=_SYSTEM_PROMPT,
            messages=[{"role": "user", "content": prompt}],
        )
        text = message.content[0].text.strip()
        logger.info("Claude discovery raw response: %s", text)

        match = re.search(r'\[.*?\]', text, re.DOTALL)
        if not match:
            logger.warning("Could not parse JSON array from discovery response")
            return []

        raw = json.loads(match.group(0))
        tickers = [
            str(t).upper().strip()
            for t in raw
            if isinstance(t, str) and t.strip()
        ]
        tickers = [t for t in tickers if t not in exclude and 1 <= len(t) <= 5]
        tickers = tickers[:max_suggestions]

        logger.info(
            "Claude suggested %d ticker(s): %s  (used %d tokens)",
            len(tickers), tickers,
            message.usage.input_tokens + message.usage.output_tokens,
        )
        return tickers

    except Exception as e:
        logger.error("LLM discovery failed: %s", e)
        return []


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    suggestions = discover_tickers()
    print(f"Claude suggested: {suggestions}")
