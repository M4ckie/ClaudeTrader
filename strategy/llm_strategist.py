"""
LLM Strategist — Sends market briefings to Claude and parses trade decisions.

Claude receives a structured briefing of each ticker's price action,
technicals, fundamentals, and news, then returns a JSON list of trade
proposals with reasoning and confidence scores.
"""

import json
import logging
import re
from pathlib import Path
from typing import Optional

import sys
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import anthropic

from config.settings import ANTHROPIC_API_KEY, LLM_MODEL, LLM_MAX_TOKENS, WATCHLIST, SCENARIOS, DEFAULT_SCENARIO
from analysis.briefing_generator import build_market_briefing
from risk.risk_gate import TradeProposal

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = """You are a disciplined swing trading analyst managing a paper portfolio.
Your job is to review market data briefings and produce structured trade decisions.

TRADING STYLE:
- Swing trading: hold positions for 2–10 days, not day trading
- Seek asymmetric setups: strong risk/reward (aim for 2:1 or better)
- Respect the trend: don't fight strong momentum
- Be selective: it's fine to PASS on most tickers. Capital preservation matters.

DECISION FRAMEWORK:
For each ticker, consider:
1. Trend: Is price above/below key moving averages? Is the trend your friend?
2. Momentum: RSI overbought/oversold? MACD crossover?
3. Volatility: ATR for sizing, Bollinger Bands for extremes
4. Fundamentals: Is valuation reasonable? Any upcoming earnings risk?
5. Sentiment: What is the news saying? Any catalysts or headwinds?
6. Portfolio context: What do we already hold? Avoid doubling up sectors.

OUTPUT FORMAT:
Respond with a JSON array of trade proposals. Only include tickers where you have
a clear conviction. It is correct and good to return an empty array if nothing looks compelling.

Each proposal must have:
{
  "ticker": "AAPL",
  "action": "BUY" | "SELL" | "PASS",
  "quantity": null,           // null = let risk gate size it by position limit
  "stop_loss_pct": 5.0,       // how far below entry to place stop (BUY only)
  "confidence": 0.75,         // 0.0 to 1.0
  "reasoning": "Brief explanation of the trade rationale and key factors."
}

Only include BUY or SELL decisions — omit PASS entries entirely to keep the response concise.
Wrap the JSON in a ```json code block."""


def _extract_json(text: str) -> Optional[list]:
    """Extract and parse the JSON array from Claude's response."""
    # Try to find a ```json block first
    match = re.search(r"```json\s*(.*?)```", text, re.DOTALL)
    if match:
        raw = match.group(1).strip()
    else:
        # Fall back: find the first [ ... ] block
        match = re.search(r"\[.*\]", text, re.DOTALL)
        if not match:
            return None
        raw = match.group(0)

    try:
        return json.loads(raw)
    except json.JSONDecodeError as e:
        logger.error(f"Failed to parse JSON from Claude response: {e}")
        logger.debug(f"Raw text: {text[:500]}")
        return None


def _parse_proposals(raw_decisions: list) -> list[TradeProposal]:
    """Convert raw JSON decisions into TradeProposal objects."""
    proposals = []
    for item in raw_decisions:
        action = str(item.get("action", "")).upper()
        if action not in ("BUY", "SELL"):
            continue

        ticker = item.get("ticker", "").upper().strip()
        if not ticker:
            continue

        proposals.append(TradeProposal(
            ticker=ticker,
            action=action,
            quantity=item.get("quantity"),
            stop_loss_pct=item.get("stop_loss_pct"),
            confidence=item.get("confidence"),
            reasoning=item.get("reasoning", ""),
        ))

    return proposals


def _scenario_prompt(scenario: str) -> str:
    """Return extra system prompt instructions for a given scenario."""
    cfg = SCENARIOS.get(scenario, SCENARIOS[DEFAULT_SCENARIO])
    if scenario == DEFAULT_SCENARIO:
        return ""
    return (
        f"\nPORTFOLIO MODE: {cfg['label']} — AGGRESSIVE\n"
        f"This is a small, high-risk account. Prioritise high-conviction, high-reward setups.\n"
        f"- Concentrate into {cfg['max_open_positions']} positions max — only take the BEST setup\n"
        f"- Wider stops ({cfg['default_stop_loss_pct']}% default) to let winners run\n"
        f"- Prefer high-momentum, high-volatility names over defensive plays\n"
        f"- Do NOT recommend ETFs or low-volatility stocks — this account needs big moves\n"
        f"- A single strong conviction trade is better than 3 mediocre ones\n"
    )


def run_strategy(
    tickers: Optional[list[str]] = None,
    portfolio_context: Optional[str] = None,
    scenario: str = DEFAULT_SCENARIO,
) -> tuple[list[TradeProposal], str]:
    """
    Run the LLM strategy on the current market data.

    Args:
        tickers: Tickers to analyse. Defaults to WATCHLIST.
        portfolio_context: String describing current portfolio state.

    Returns:
        Tuple of (list of TradeProposals, raw Claude response text)
    """
    if not ANTHROPIC_API_KEY:
        logger.error("ANTHROPIC_API_KEY not configured — cannot run strategy")
        return [], ""

    tickers = tickers or WATCHLIST

    # Build the briefing
    logger.info("Building market briefing for %d tickers...", len(tickers))
    briefing = build_market_briefing(tickers=tickers, portfolio_context=portfolio_context, scenario=scenario)
    logger.debug("Briefing length: %d chars", len(briefing))

    # Call Claude
    logger.info("Sending briefing to %s...", LLM_MODEL)
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

    try:
        message = client.messages.create(
            model=LLM_MODEL,
            max_tokens=LLM_MAX_TOKENS,
            system=SYSTEM_PROMPT + _scenario_prompt(scenario),
            messages=[
                {"role": "user", "content": briefing}
            ],
        )
    except anthropic.APIError as e:
        logger.error("Anthropic API error: %s", e)
        return [], ""

    response_text = message.content[0].text
    logger.info(
        "Claude responded (%d chars, %d input tokens, %d output tokens)",
        len(response_text),
        message.usage.input_tokens,
        message.usage.output_tokens,
    )
    logger.debug("Claude response:\n%s", response_text)

    # Parse decisions
    raw_decisions = _extract_json(response_text)
    if raw_decisions is None:
        logger.error(
            "JSON extraction failed — Claude's response contained no parseable array. "
            "This is a parsing error, NOT Claude choosing to pass. Raw response:\n%s",
            response_text,
        )
        return [], response_text

    if not raw_decisions:
        logger.info("Claude returned an empty proposal list — no compelling setups found.")
        return [], response_text

    proposals = _parse_proposals(raw_decisions)
    logger.info(
        "Parsed %d trade proposal(s): %s",
        len(proposals),
        [(p.ticker, p.action) for p in proposals]
    )

    return proposals, response_text


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    proposals, response = run_strategy()
    print("\n--- Claude's Response ---")
    print(response)
    print(f"\n--- Parsed {len(proposals)} proposal(s) ---")
    for p in proposals:
        print(f"  {p.action:4s} {p.ticker:6s}  confidence={p.confidence}  stop={p.stop_loss_pct}%")
        print(f"       {p.reasoning}")
