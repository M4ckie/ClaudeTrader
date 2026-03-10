# ClaudeTrader — Autonomous Swing Trading Agent

An LLM-driven paper trading agent that monitors a stock watchlist, builds structured market briefings, and uses Claude to generate swing trade decisions — all enforced through a hard risk gate and tracked in a Streamlit dashboard.

> **Paper trading only.** No real money is moved. This is a research and learning project.

---

## How It Works

```
┌──────────────────────────────────────────────────────┐
│                    Scheduler                          │
│         (fires trading pipeline daily at close)       │
└──────────────────────┬───────────────────────────────┘
                       │
         ┌─────────────▼──────────────┐
         │     Data Collection        │
         │  yfinance · pandas-ta      │
         │  NewsAPI · SQLite          │
         └─────────────┬──────────────┘
                       │
         ┌─────────────▼──────────────┐
         │   Briefing Generator       │
         │  (price · technicals ·     │
         │   fundamentals · news)     │
         └─────────────┬──────────────┘
                       │
         ┌─────────────▼──────────────┐
         │   LLM Strategy (Claude)    │
         │  evaluates briefing →      │
         │  JSON trade proposals      │
         └─────────────┬──────────────┘
                       │
         ┌─────────────▼──────────────┐
         │      Risk Gate             │
         │  position limits · sector  │
         │  caps · circuit breakers   │
         └─────────────┬──────────────┘
                       │
         ┌─────────────▼──────────────┐
         │   Portfolio Simulator      │
         │  paper trades · journal    │
         │  snapshots · P&L tracking  │
         └────────────────────────────┘
                       │
         ┌─────────────▼──────────────┐
         │   Streamlit Dashboard      │
         │  equity curve · trades ·   │
         │  market data · news        │
         └────────────────────────────┘
```

Every weekday after market close, the agent:
1. Fetches fresh price data, technical indicators, fundamentals, and news
2. Builds a structured briefing for each ticker in the watchlist
3. Sends the briefing to Claude and receives trade proposals (BUY/SELL/PASS)
4. Runs every proposal through the risk gate (position sizing, sector limits, drawdown stops)
5. Executes approved trades in the paper portfolio
6. Saves a portfolio snapshot and logs everything

---

## Features

- **Multi-scenario portfolios** — Run independent portfolios with different risk profiles simultaneously (e.g. conservative $100k and aggressive $1k)
- **Claude-powered decisions** — Full market context (price action, technicals, fundamentals, news) sent to Claude; structured JSON trade proposals returned
- **Hard risk gate** — Position size caps, sector concentration limits, max exposure, daily loss circuit breakers — Claude's judgment cannot override these
- **Full audit trail** — Every trade logged with Claude's reasoning, confidence score, stop loss, and P&L
- **Streamlit dashboard** — Equity curve, trade journal with reasoning viewer, per-ticker price/indicator charts, news feed
- **Docker-ready** — Single `docker compose up` to run the full stack
- **Scheduler built-in** — Python `schedule` library fires the pipeline daily; no cron required

---

## Project Structure

```
ClaudeTrader/
├── config/
│   ├── settings.py              # Central config (watchlist, risk params, LLM model)
│   └── settings_local.py        # Your API keys — NOT committed (see Setup)
├── data/
│   ├── database.py              # SQLite schema, WAL mode, helpers
│   ├── price_collector.py       # yfinance OHLCV, incremental fetching
│   ├── indicator_engine.py      # pandas-ta: SMA, EMA, RSI, MACD, ATR, Bollinger
│   ├── fundamental_collector.py # yfinance: income stmt, ratios, earnings dates
│   └── news_collector.py        # NewsAPI: financial headlines per ticker
├── analysis/
│   └── briefing_generator.py    # Formats per-ticker briefings for the LLM
├── strategy/
│   └── llm_strategist.py        # Claude API: sends briefing, parses trade proposals
├── risk/
│   └── risk_gate.py             # Hard rule enforcement before any trade executes
├── execution/
│   └── simulator.py             # Paper trading engine, scenario-aware
├── utils/
│   └── logger.py                # Dual console + rotating file logging
├── dashboard.py                 # Streamlit web UI
├── main.py                      # CLI entry point / orchestrator
├── start.sh                     # Docker entrypoint (init → dashboard → scheduler)
├── Dockerfile
├── docker-compose.yml
└── requirements.txt
```

---

## Setup

### Option A — Local Python

```bash
# 1. Clone and create a virtualenv
git clone https://github.com/M4ckie/ClaudeTrader.git
cd ClaudeTrader
python -m venv .venv
source .venv/bin/activate      # Windows: .venv\Scripts\activate

# 2. Install dependencies
pip install -r requirements.txt
pip install streamlit

# 3. Add your API keys
cp .env.example config/settings_local.py
# Edit config/settings_local.py and fill in your keys

# 4. Initialise the database and collect data
python main.py init
python main.py collect

# 5. Run the dashboard
streamlit run dashboard.py

# 6. Run a dry-run trade to test the pipeline
python main.py trade --dry-run
```

### Option B — Docker

```bash
# 1. Clone
git clone https://github.com/M4ckie/ClaudeTrader.git
cd ClaudeTrader

# 2. Set your API keys
cp .env.example .env
# Edit .env and fill in your keys

# 3. Build and run
docker compose up -d

# Dashboard available at http://localhost:8501
```

---

## Configuration

All settings live in `config/settings.py`. Create `config/settings_local.py` to override without touching the main file:

```python
# config/settings_local.py
ANTHROPIC_API_KEY = "sk-ant-..."
NEWS_API_KEY = "your_key_here"
```

Or pass as environment variables (preferred for Docker):

```
ANTHROPIC_API_KEY=sk-ant-...
NEWS_API_KEY=your_key_here
```

### API Keys Required

| Key | Where to get it | Cost |
|-----|----------------|------|
| `ANTHROPIC_API_KEY` | [console.anthropic.com](https://console.anthropic.com) | Pay-per-use (~$1.20/month at daily cadence with Sonnet) |
| `NEWS_API_KEY` | [newsapi.org](https://newsapi.org) | Free tier: 100 req/day |

Price data and fundamentals use **yfinance** — no API key needed.

---

## CLI Commands

```bash
python main.py status          # Show data freshness and DB stats
python main.py collect         # Fetch all data (prices, indicators, fundamentals, news)
python main.py briefing        # Print the current market briefing (no trade)
python main.py trade           # Run full pipeline: collect → brief → decide → risk → execute
python main.py trade --dry-run # Same but print decisions without executing
python main.py schedule        # Run the daily scheduler (fires at market close)

# Scenario flags (run independent portfolios)
python main.py trade --scenario small
python main.py briefing --scenario small
python main.py schedule --time 21:30
```

---

## Scenarios

Two portfolio configurations run independently with separate trade histories:

| Scenario | Capital | Max positions | Max position size | Stop loss | Daily loss limit |
|----------|---------|---------------|-------------------|-----------|-----------------|
| `default` | $100,000 | 8 | 15% | 5% | 3% |
| `small` | $1,000 | 3 | 30% | 10% | 6% |

The `small` scenario instructs Claude to prefer high-volatility, high-conviction trades and avoid ETFs.

---

## Watchlist

Default: `AAPL MSFT GOOGL NVDA META SPY QQQ IWM JPM UNH XOM`

Edit `WATCHLIST` in `config/settings.py` to customise.

---

## Risk Gate Rules

Claude's trade proposals must pass all of these before execution:

- Max position size (% of portfolio value)
- Max open positions
- Max total portfolio exposure
- Max sector concentration
- Daily loss circuit breaker (halts all trading if daily P&L drops below threshold)
- Stop loss cap per scenario

---

## Dashboard

The Streamlit dashboard has four pages:

- **Portfolio Overview** — Equity curve, daily P&L, open positions, performance stats
- **Trade Journal** — Full trade history with Claude's reasoning and confidence scores
- **Market Data** — Per-ticker price chart with SMA overlay, volume, RSI, fundamentals
- **News Feed** — Latest headlines per ticker

---

## Disclaimer

This project is for **educational and research purposes only**. It does not constitute financial advice. All trading is simulated (paper trading). Past performance of any strategy does not guarantee future results.
