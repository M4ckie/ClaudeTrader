"""
SwingTrader AI — Main Orchestrator

This is the entry point that ties all layers together.
Run with: python main.py [command]

Commands:
    collect     Fetch all data (prices, indicators, fundamentals, news)
    prices      Fetch price data only
    indicators  Compute technical indicators
    fundamentals Fetch fundamental data
    news        Fetch news headlines
    init        Initialize database only
    status      Show database status and data freshness
    briefing    Print the current market briefing (no trade)
    trade       Run the full pipeline: collect → brief → strategy → risk → execute
    schedule    Run the scheduler (fires trade pipeline daily at market close)
"""

import argparse
import logging
import sys
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from config.settings import WATCHLIST, DB_PATH, SCENARIOS, DEFAULT_SCENARIO
from data.database import init_database, db_session
from data.price_collector import collect_prices
from data.indicator_engine import compute_and_store
from data.fundamental_collector import collect_fundamentals
from data.news_collector import collect_news
from utils.logger import setup_logging


logger = setup_logging()


def cmd_briefing(scenario: str = DEFAULT_SCENARIO):
    """Print the current market briefing without making any trades."""
    from analysis.briefing_generator import build_market_briefing
    from execution.simulator import Simulator
    import yfinance as yf

    sim = Simulator.load(scenario=scenario)
    prices = {}
    for ticker in WATCHLIST:
        try:
            t = yf.Ticker(ticker)
            hist = t.fast_info
            prices[ticker] = float(hist.last_price or 0)
        except Exception:
            pass

    portfolio_context = sim.summary(prices) if prices else None
    briefing = build_market_briefing(portfolio_context=portfolio_context)
    print(briefing)


def cmd_trade(dry_run: bool = False, scenario: str = DEFAULT_SCENARIO):
    """
    Run the full trading pipeline:
      1. Collect latest data
      2. Build market briefing
      3. Send to Claude for decisions
      4. Run decisions through risk gate
      5. Execute approved trades (or print them in dry-run mode)
      6. Save portfolio snapshot
    """
    import yfinance as yf
    from analysis.briefing_generator import build_market_briefing
    from strategy.llm_strategist import run_strategy
    from risk.risk_gate import evaluate_all
    from execution.simulator import Simulator

    scenario_cfg = SCENARIOS.get(scenario, SCENARIOS[DEFAULT_SCENARIO])
    logger.info("=" * 60)
    logger.info("Starting trading pipeline%s  [scenario: %s — %s]",
                " (DRY RUN)" if dry_run else "", scenario, scenario_cfg["label"])
    logger.info("=" * 60)

    # 1. Collect fresh data
    logger.info("[1/5] Collecting data...")
    collect_prices()
    compute_and_store()
    collect_fundamentals()
    collect_news()

    # 2. Load portfolio and get current prices
    sim = Simulator.load(scenario=scenario)
    current_prices = {}
    ticker_sectors = {}
    import os, sys as _sys
    for ticker in WATCHLIST:
        try:
            # Suppress yfinance 502/error output
            with open(os.devnull, "w") as devnull:
                old_out, old_err = _sys.stdout, _sys.stderr
                _sys.stdout, _sys.stderr = devnull, devnull
                try:
                    t = yf.Ticker(ticker)
                    price = t.fast_info.last_price
                    info = t.info or {}
                finally:
                    _sys.stdout, _sys.stderr = old_out, old_err
            if price:
                current_prices[ticker] = float(price)
            ticker_sectors[ticker] = info.get("sector", "")
        except Exception:
            pass

    # Fall back to latest DB price for any ticker yfinance couldn't reach
    with db_session() as conn:
        for ticker in WATCHLIST:
            if ticker not in current_prices or current_prices[ticker] == 0:
                row = conn.execute(
                    "SELECT close FROM daily_prices WHERE ticker = ? ORDER BY date DESC LIMIT 1",
                    (ticker,)
                ).fetchone()
                if row:
                    current_prices[ticker] = float(row["close"])
                    logger.info("Using last DB price for %s: %.2f", ticker, current_prices[ticker])

            if ticker not in ticker_sectors:
                row = conn.execute(
                    "SELECT sector FROM stocks WHERE ticker = ?", (ticker,)
                ).fetchone()
                if row:
                    ticker_sectors[ticker] = row["sector"] or ""

    sim.set_start_of_day(current_prices)
    portfolio_state = sim.portfolio_state(current_prices)
    portfolio_context = sim.summary(current_prices)

    # 3. Run LLM strategy
    logger.info("[2/5] Running LLM strategy...")
    proposals, raw_response = run_strategy(portfolio_context=portfolio_context, scenario=scenario)

    if not proposals:
        logger.info("No trade proposals from Claude — nothing to do.")
        sim.snapshot(current_prices)
        return

    # Inject current prices into proposals so the risk gate can size positions
    for p in proposals:
        if not p.price and p.ticker in current_prices:
            p.price = current_prices[p.ticker]

    # 4. Risk gate
    logger.info("[3/5] Running risk gate on %d proposal(s)...", len(proposals))
    decisions = evaluate_all(proposals, portfolio_state, ticker_sectors, scenario=scenario)

    approved = [d for d in decisions if d.approved]
    rejected = [d for d in decisions if not d.approved]

    logger.info("Risk gate: %d approved, %d rejected", len(approved), len(rejected))
    for d in rejected:
        logger.info("  REJECTED %s: %s", d.proposal.ticker, d.rejection_reason)
    for w in [w for d in approved for w in d.warnings]:
        logger.warning("  WARNING: %s", w)

    # 5. Execute
    logger.info("[4/5] Executing %d approved trade(s)%s...", len(approved), " (DRY RUN)" if dry_run else "")
    for decision in approved:
        ticker = decision.proposal.ticker
        price = current_prices.get(ticker, 0)
        if price <= 0:
            logger.warning("No current price for %s — skipping", ticker)
            continue

        if dry_run:
            logger.info(
                "  DRY RUN: %s %s  qty=%s  price=%.2f  stop=%.1f%%  conf=%.0f%%",
                decision.proposal.action, ticker,
                decision.final_quantity, price,
                decision.stop_loss_pct,
                (decision.proposal.confidence or 0) * 100,
            )
            logger.info("  Reasoning: %s", decision.proposal.reasoning)
        else:
            sim.execute(
                decision,
                current_price=price,
                reasoning=decision.proposal.reasoning,
                ticker_sector=ticker_sectors.get(ticker, ""),
            )

    # 6. Snapshot
    logger.info("[5/5] Saving portfolio snapshot...")
    if not dry_run:
        sim.snapshot(current_prices)

    logger.info("=" * 60)
    logger.info("Trading pipeline complete!")
    logger.info("=" * 60)
    print()
    print(sim.summary(current_prices))


def cmd_schedule(scenario: str = DEFAULT_SCENARIO, run_time: str = "21:30"):
    """
    Run the daily scheduler. Fires the full trade pipeline once per weekday
    at the configured time (default 21:30 UTC = 4:30pm ET, after market close).

    Runs all scenarios unless a specific one is passed.
    Keeps running until killed (Ctrl+C or Docker stop).
    """
    import schedule

    scenarios_to_run = [scenario] if scenario != DEFAULT_SCENARIO else list(SCENARIOS.keys())

    def job():
        # Skip weekends
        if datetime.now().weekday() >= 5:
            logger.info("Scheduler: weekend — skipping trade run")
            return
        logger.info("Scheduler: market close — running trade pipeline for %d scenario(s)", len(scenarios_to_run))
        for s in scenarios_to_run:
            try:
                cmd_trade(dry_run=False, scenario=s)
            except Exception as e:
                logger.error("Scheduler: trade pipeline failed for scenario '%s': %s", s, e)

    schedule.every().day.at(run_time).do(job)

    logger.info("Scheduler started — will run trade pipeline daily at %s UTC (weekdays only)", run_time)
    logger.info("Scenarios: %s", ", ".join(f"{s} ({SCENARIOS[s]['label']})" for s in scenarios_to_run))
    logger.info("Press Ctrl+C to stop.")

    # Run immediately on startup so you don't have to wait until tomorrow
    logger.info("Running initial trade pipeline on startup...")
    job()

    while True:
        schedule.run_pending()
        time.sleep(60)


def cmd_init():
    """Initialize the database."""
    logger.info("Initializing database...")
    init_database()
    logger.info(f"Database ready at {DB_PATH}")


def cmd_collect():
    """Run full data collection pipeline."""
    logger.info("=" * 60)
    logger.info("Starting full data collection pipeline")
    logger.info(f"Watchlist: {', '.join(WATCHLIST)}")
    logger.info("=" * 60)

    logger.info("[1/4] Collecting price data...")
    collect_prices()

    logger.info("[2/4] Computing technical indicators...")
    compute_and_store()

    logger.info("[3/4] Collecting fundamental data...")
    collect_fundamentals()

    logger.info("[4/4] Collecting news...")
    collect_news()

    logger.info("=" * 60)
    logger.info("Data collection pipeline complete!")
    logger.info("=" * 60)


def cmd_status():
    """Show what data we have and how fresh it is."""
    init_database()

    with db_session() as conn:
        print("\n📊 SwingTrader AI — Data Status")
        print("=" * 55)

        # Stock metadata
        stock_count = conn.execute("SELECT COUNT(*) as c FROM stocks").fetchone()["c"]
        print(f"\n  Stocks tracked: {stock_count}")

        # Price data
        price_stats = conn.execute("""
            SELECT
                COUNT(DISTINCT ticker) as tickers,
                COUNT(*) as rows,
                MIN(date) as earliest,
                MAX(date) as latest
            FROM daily_prices
        """).fetchone()
        print(f"\n  📈 Price Data:")
        print(f"     Tickers: {price_stats['tickers']}")
        print(f"     Rows:    {price_stats['rows']}")
        print(f"     Range:   {price_stats['earliest']} → {price_stats['latest']}")

        # Indicators
        ind_stats = conn.execute("""
            SELECT COUNT(DISTINCT ticker) as tickers, MAX(date) as latest
            FROM indicators
        """).fetchone()
        print(f"\n  📉 Indicators:")
        print(f"     Tickers: {ind_stats['tickers']}")
        print(f"     Latest:  {ind_stats['latest']}")

        # Fundamentals
        fund_count = conn.execute(
            "SELECT COUNT(DISTINCT ticker) as c FROM fundamentals"
        ).fetchone()["c"]
        print(f"\n  💰 Fundamentals:")
        print(f"     Tickers: {fund_count}")

        # News
        news_stats = conn.execute("""
            SELECT COUNT(*) as total, MAX(published_at) as latest
            FROM news
        """).fetchone()
        print(f"\n  📰 News:")
        print(f"     Articles: {news_stats['total']}")
        print(f"     Latest:   {news_stats['latest']}")

        # Per-ticker breakdown
        print(f"\n  Per-ticker latest price date:")
        rows = conn.execute("""
            SELECT ticker, MAX(date) as latest, COUNT(*) as days
            FROM daily_prices
            GROUP BY ticker
            ORDER BY ticker
        """).fetchall()
        for row in rows:
            print(f"     {row['ticker']:6s}  {row['latest']}  ({row['days']} days)")

        print()


def main():
    parser = argparse.ArgumentParser(description="SwingTrader AI")
    parser.add_argument(
        "command",
        nargs="?",
        default="status",
        choices=["collect", "prices", "indicators", "fundamentals", "news", "init", "status", "briefing", "trade", "schedule"],
        help="Command to run (default: status)",
    )
    parser.add_argument(
        "--full-refresh",
        action="store_true",
        help="Re-fetch full history instead of incremental update",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="For 'trade': show decisions without executing them",
    )
    parser.add_argument(
        "--scenario",
        default=DEFAULT_SCENARIO,
        choices=list(SCENARIOS.keys()),
        help=f"Portfolio scenario to use (default: {DEFAULT_SCENARIO})",
    )
    parser.add_argument(
        "--time",
        default="21:30",
        help="For 'schedule': UTC time to run daily trade pipeline (default: 21:30 = 4:30pm ET)",
    )

    args = parser.parse_args()

    commands = {
        "init": cmd_init,
        "collect": cmd_collect,
        "prices": lambda: collect_prices(full_refresh=args.full_refresh),
        "indicators": compute_and_store,
        "fundamentals": collect_fundamentals,
        "news": collect_news,
        "status": cmd_status,
        "briefing": lambda: cmd_briefing(scenario=args.scenario),
        "trade": lambda: cmd_trade(dry_run=args.dry_run, scenario=args.scenario),
        "schedule": lambda: cmd_schedule(scenario=args.scenario, run_time=args.time),
    }

    commands[args.command]()


if __name__ == "__main__":
    main()
