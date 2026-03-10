"""Unit tests for LLM strategist JSON parsing (no API calls required)."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest
from strategy.llm_strategist import _extract_json, _parse_proposals
from risk.risk_gate import TradeProposal


# ── _extract_json ─────────────────────────────────────────────────────────────

def test_extract_json_code_block():
    text = '```json\n[{"ticker": "AAPL", "action": "BUY"}]\n```'
    result = _extract_json(text)
    assert result == [{"ticker": "AAPL", "action": "BUY"}]


def test_extract_json_fallback_bare_array():
    text = 'Some text [{"ticker": "MSFT", "action": "SELL"}] more text'
    result = _extract_json(text)
    assert result == [{"ticker": "MSFT", "action": "SELL"}]


def test_extract_json_empty_array():
    text = "```json\n[]\n```"
    result = _extract_json(text)
    assert result == []


def test_extract_json_returns_none_when_no_json():
    result = _extract_json("Claude says nothing interesting here.")
    assert result is None


def test_extract_json_returns_none_on_malformed():
    text = "```json\n[{bad json here}\n```"
    result = _extract_json(text)
    assert result is None


def test_extract_json_multiline():
    text = """Sure, here are my picks:
```json
[
  {
    "ticker": "NVDA",
    "action": "BUY",
    "confidence": 0.85,
    "stop_loss_pct": 5.0,
    "reasoning": "Strong momentum"
  }
]
```
Let me know if you need more detail."""
    result = _extract_json(text)
    assert result is not None
    assert len(result) == 1
    assert result[0]["ticker"] == "NVDA"


# ── _parse_proposals ──────────────────────────────────────────────────────────

def test_parse_proposals_buy():
    raw = [{"ticker": "aapl", "action": "BUY", "stop_loss_pct": 5.0, "confidence": 0.9, "reasoning": "test"}]
    proposals = _parse_proposals(raw)
    assert len(proposals) == 1
    p = proposals[0]
    assert p.ticker == "AAPL"
    assert p.action == "BUY"
    assert p.stop_loss_pct == 5.0
    assert p.confidence == 0.9


def test_parse_proposals_sell():
    raw = [{"ticker": "MSFT", "action": "sell", "reasoning": "taking profits"}]
    proposals = _parse_proposals(raw)
    assert len(proposals) == 1
    assert proposals[0].action == "SELL"


def test_parse_proposals_skips_pass():
    raw = [
        {"ticker": "AAPL", "action": "BUY"},
        {"ticker": "TSLA", "action": "PASS"},
    ]
    proposals = _parse_proposals(raw)
    assert len(proposals) == 1
    assert proposals[0].ticker == "AAPL"


def test_parse_proposals_skips_unknown_action():
    raw = [{"ticker": "AAPL", "action": "HOLD"}]
    proposals = _parse_proposals(raw)
    assert proposals == []


def test_parse_proposals_skips_missing_ticker():
    raw = [{"action": "BUY", "stop_loss_pct": 5.0}]
    proposals = _parse_proposals(raw)
    assert proposals == []


def test_parse_proposals_empty_list():
    assert _parse_proposals([]) == []


def test_parse_proposals_normalises_ticker_case():
    raw = [{"ticker": "nvda", "action": "BUY"}]
    proposals = _parse_proposals(raw)
    assert proposals[0].ticker == "NVDA"


def test_parse_proposals_returns_trade_proposal_objects():
    raw = [{"ticker": "SPY", "action": "BUY"}]
    proposals = _parse_proposals(raw)
    assert all(isinstance(p, TradeProposal) for p in proposals)


def test_parse_proposals_multiple():
    raw = [
        {"ticker": "AAPL", "action": "BUY", "confidence": 0.8},
        {"ticker": "META", "action": "SELL"},
        {"ticker": "XOM", "action": "PASS"},
    ]
    proposals = _parse_proposals(raw)
    assert len(proposals) == 2
    tickers = [p.ticker for p in proposals]
    assert "AAPL" in tickers
    assert "META" in tickers
    assert "XOM" not in tickers
