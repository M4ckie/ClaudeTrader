"""
SwingTrader AI — Central Configuration

Copy this to settings_local.py and fill in your API keys.
The app loads settings_local.py first, falling back to this file.
"""

from pathlib import Path
from datetime import datetime

# ── Paths ───────────────────────────────────────────────────────────────
PROJECT_ROOT = Path(__file__).resolve().parent.parent
DB_PATH = PROJECT_ROOT / "data" / "trading.db"
LOG_DIR = PROJECT_ROOT / "logs"
TRADE_JOURNAL_DIR = PROJECT_ROOT / "journal"

# ── API Keys ─────────────────────────────────────────────────────────────
# Read from environment variables first, fall back to settings_local.py
import os as _os
NEWS_API_KEY = _os.environ.get("NEWS_API_KEY", "")
ANTHROPIC_API_KEY = _os.environ.get("ANTHROPIC_API_KEY", "")

# ── Watchlist ────────────────────────────────────────────────────────────
# Tickers the agent monitors. Start small, expand as confidence grows.
WATCHLIST = [
    # Large-cap tech
    "AAPL", "MSFT", "GOOGL", "NVDA", "META",
    # ETFs
    "SPY", "QQQ", "IWM",
    # Diversifiers
    "JPM", "UNH", "XOM",
]

# ── Data Collection ─────────────────────────────────────────────────────
# How many days of historical price data to fetch on initial load
PRICE_HISTORY_DAYS = 365

# Technical indicators to compute (used by indicator_engine.py)
INDICATORS = {
    "sma": [20, 50, 200],
    "ema": [12, 26],
    "rsi": {"length": 14},
    "macd": {"fast": 12, "slow": 26, "signal": 9},
    "atr": {"length": 14},
    "bbands": {"length": 20, "std": 2.0},
    "volume_sma": 20,  # volume moving average period
}

# Max news headlines per ticker per fetch
NEWS_MAX_ARTICLES = 10
NEWS_LOOKBACK_DAYS = 7  # how far back to search for news

# ── Fundamental Data ────────────────────────────────────────────────────
# Which financial statements to pull
FUNDAMENTAL_STATEMENTS = ["income", "balance", "ratios", "earnings_calendar"]

# ── Screener ─────────────────────────────────────────────────────────────
# Yahoo Finance pre-built screens to pull candidates from each run.
# Valid IDs: most_actives, day_gainers, day_losers, undervalued_growth_stocks
SCREENER_ENABLED = True
SCREENER_SCREENS = ["most_actives", "day_gainers"]
SCREENER_MAX_CANDIDATES = 10   # max new tickers to add per run
SCREENER_MIN_PRICE = 10.0      # ignore penny stocks
SCREENER_MIN_VOLUME = 500_000  # ignore illiquid stocks

# LLM Discovery — Claude suggests additional tickers based on news context
LLM_DISCOVERY_ENABLED = True
LLM_DISCOVERY_MAX = 5          # max tickers to request from Claude

# ── LLM Strategy ────────────────────────────────────────────────────────
LLM_MODEL = "claude-sonnet-4-20250514"
LLM_MAX_TOKENS = 1500

# ── Scenarios ────────────────────────────────────────────────────────────
# Each scenario runs an independent portfolio with its own capital and history.
# Usage: python main.py trade --scenario small
SCENARIOS = {
    "default": {
        "initial_capital":      100_000.00,
        "label":                "$100k Portfolio",
        "max_position_pct":     15.0,
        "max_open_positions":   8,
        "default_stop_loss_pct": 5.0,
        "max_daily_loss_pct":   3.0,
        "max_total_exposure_pct": 90.0,
        "max_sector_pct":       40.0,
    },
    "small": {
        "initial_capital":      1_000.00,
        "label":                "$1k Aggressive",
        "max_position_pct":     30.0,   # up to $300 per trade
        "max_open_positions":   3,      # concentrate into 3 positions max
        "default_stop_loss_pct": 10.0,  # wider stops, more room to run
        "max_daily_loss_pct":   6.0,    # looser circuit breaker
        "max_total_exposure_pct": 95.0, # stay nearly fully invested
        "max_sector_pct":       60.0,   # allow sector concentration
    },
}
DEFAULT_SCENARIO = "default"

# ── Risk Management ─────────────────────────────────────────────────────
INITIAL_CAPITAL = 100_000.00  # overridden at runtime by active scenario
MAX_POSITION_PCT = 15.0        # max % of portfolio in one stock
MAX_SECTOR_PCT = 40.0          # max % of portfolio in one sector
MAX_TOTAL_EXPOSURE_PCT = 90.0  # max % of portfolio invested (keep cash)
MAX_DAILY_LOSS_PCT = 3.0       # stop trading if daily loss exceeds this
DEFAULT_STOP_LOSS_PCT = 5.0    # default stop loss if LLM doesn't specify
MAX_OPEN_POSITIONS = 8         # max concurrent positions

# ── Simulation ──────────────────────────────────────────────────────────
SLIPPAGE_PCT = 0.05            # simulated slippage per trade
COMMISSION_PER_TRADE = 0.00    # most brokers are zero-commission now

# ── Logging ─────────────────────────────────────────────────────────────
LOG_LEVEL = "INFO"
LOG_FORMAT = "%(asctime)s | %(name)-20s | %(levelname)-7s | %(message)s"

# ── Local overrides (API keys, personal config) ──────────────────────────
# settings_local.py can override any name defined above (API keys, watchlist,
# model, etc.). It is gitignored and never committed. In Docker, use env vars
# instead. Any name exported from settings_local takes precedence.
import logging as _log
try:
    from config.settings_local import *  # noqa: F401, F403, E402
    _log.getLogger(__name__).debug("settings_local.py loaded — local overrides active")
except ImportError:
    _log.getLogger(__name__).debug("No settings_local.py found — using defaults and env vars")
del _log
