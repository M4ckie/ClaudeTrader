"""
Database layer — SQLite setup, schema, and helper functions.

All data flows through here. Each collector writes to its table,
and the analysis layer reads from here to build briefings.
"""

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from datetime import datetime, date
from typing import Optional

import pandas as pd

import sys
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from config.settings import DB_PATH


def get_connection(db_path: Optional[Path] = None) -> sqlite3.Connection:
    """Get a database connection with row_factory enabled."""
    path = db_path or DB_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


@contextmanager
def db_session(db_path: Optional[Path] = None):
    """Context manager for database transactions."""
    conn = get_connection(db_path)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_database(db_path: Optional[Path] = None):
    """Create all tables if they don't exist, and apply any needed migrations."""
    with db_session(db_path) as conn:
        conn.executescript(SCHEMA_SQL)
        _migrate(conn)


def _migrate(conn: sqlite3.Connection):
    """Apply schema migrations for existing databases."""
    # Add scenario column to trades if missing
    cols = {r[1] for r in conn.execute("PRAGMA table_info(trades)").fetchall()}
    if "scenario" not in cols:
        conn.execute("ALTER TABLE trades ADD COLUMN scenario TEXT NOT NULL DEFAULT 'default'")

    # Recreate portfolio_snapshots with composite PK if it still has the old single-column PK
    snap_cols = {r[1] for r in conn.execute("PRAGMA table_info(portfolio_snapshots)").fetchall()}
    if "scenario" not in snap_cols:
        conn.executescript("""
            ALTER TABLE portfolio_snapshots RENAME TO portfolio_snapshots_old;
            CREATE TABLE portfolio_snapshots (
                scenario        TEXT NOT NULL DEFAULT 'default',
                date            TEXT NOT NULL,
                cash            REAL,
                positions_value REAL,
                total_value     REAL,
                daily_pnl       REAL,
                daily_pnl_pct   REAL,
                positions_json  TEXT,
                PRIMARY KEY (scenario, date)
            );
            INSERT INTO portfolio_snapshots SELECT 'default', date, cash, positions_value,
                total_value, daily_pnl, daily_pnl_pct, positions_json
            FROM portfolio_snapshots_old;
            DROP TABLE portfolio_snapshots_old;
        """)


# ── Schema ──────────────────────────────────────────────────────────────

SCHEMA_SQL = """
-- Watchlist & stock metadata
CREATE TABLE IF NOT EXISTS stocks (
    ticker      TEXT PRIMARY KEY,
    name        TEXT,
    sector      TEXT,
    industry    TEXT,
    market_cap  REAL,
    added_at    TEXT DEFAULT (datetime('now')),
    active      INTEGER DEFAULT 1
);

-- Daily OHLCV price data
CREATE TABLE IF NOT EXISTS daily_prices (
    ticker      TEXT NOT NULL,
    date        TEXT NOT NULL,    -- YYYY-MM-DD
    open        REAL,
    high        REAL,
    low         REAL,
    close       REAL,
    volume      INTEGER,
    adj_close   REAL,
    PRIMARY KEY (ticker, date),
    FOREIGN KEY (ticker) REFERENCES stocks(ticker)
);

-- Computed technical indicators (one row per ticker per date)
CREATE TABLE IF NOT EXISTS indicators (
    ticker      TEXT NOT NULL,
    date        TEXT NOT NULL,
    sma_20      REAL,
    sma_50      REAL,
    sma_200     REAL,
    ema_12      REAL,
    ema_26      REAL,
    rsi_14      REAL,
    macd        REAL,
    macd_signal REAL,
    macd_hist   REAL,
    atr_14      REAL,
    bbands_upper REAL,
    bbands_mid   REAL,
    bbands_lower REAL,
    volume_sma_20 REAL,
    volume_ratio  REAL,   -- current volume / volume_sma
    PRIMARY KEY (ticker, date),
    FOREIGN KEY (ticker) REFERENCES stocks(ticker)
);

-- Fundamental data (quarterly snapshots)
CREATE TABLE IF NOT EXISTS fundamentals (
    ticker          TEXT NOT NULL,
    period          TEXT NOT NULL,    -- e.g. "2024-Q3"
    revenue         REAL,
    net_income      REAL,
    eps             REAL,
    pe_ratio        REAL,
    pb_ratio        REAL,
    debt_to_equity  REAL,
    roe             REAL,
    free_cash_flow  REAL,
    dividend_yield  REAL,
    fetched_at      TEXT DEFAULT (datetime('now')),
    PRIMARY KEY (ticker, period),
    FOREIGN KEY (ticker) REFERENCES stocks(ticker)
);

-- Earnings calendar
CREATE TABLE IF NOT EXISTS earnings_calendar (
    ticker          TEXT NOT NULL,
    earnings_date   TEXT NOT NULL,    -- YYYY-MM-DD
    estimate_eps    REAL,
    actual_eps      REAL,
    surprise_pct    REAL,
    fetched_at      TEXT DEFAULT (datetime('now')),
    PRIMARY KEY (ticker, earnings_date),
    FOREIGN KEY (ticker) REFERENCES stocks(ticker)
);

-- News headlines
CREATE TABLE IF NOT EXISTS news (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker          TEXT NOT NULL,
    headline        TEXT NOT NULL,
    source          TEXT,
    url             TEXT,
    published_at    TEXT,
    fetched_at      TEXT DEFAULT (datetime('now')),
    FOREIGN KEY (ticker) REFERENCES stocks(ticker)
);
CREATE INDEX IF NOT EXISTS idx_news_ticker_date
    ON news(ticker, published_at);

-- Trade journal (filled by execution layer, included here for completeness)
CREATE TABLE IF NOT EXISTS trades (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    scenario        TEXT NOT NULL DEFAULT 'default',
    ticker          TEXT NOT NULL,
    action          TEXT NOT NULL,    -- BUY, SELL
    quantity        INTEGER,
    price           REAL,
    total_value     REAL,
    slippage        REAL,
    commission      REAL,
    reasoning       TEXT,             -- LLM's reasoning
    confidence      REAL,
    executed_at     TEXT DEFAULT (datetime('now')),
    FOREIGN KEY (ticker) REFERENCES stocks(ticker)
);

-- Portfolio snapshots (end-of-day state)
CREATE TABLE IF NOT EXISTS portfolio_snapshots (
    scenario        TEXT NOT NULL DEFAULT 'default',
    date            TEXT NOT NULL,
    cash            REAL,
    positions_value REAL,
    total_value     REAL,
    daily_pnl       REAL,
    daily_pnl_pct   REAL,
    positions_json  TEXT,             -- JSON blob of current positions
    PRIMARY KEY (scenario, date)
);
""";


# ── Query Helpers ───────────────────────────────────────────────────────

def get_latest_price_date(conn: sqlite3.Connection, ticker: str) -> Optional[str]:
    """Return the most recent date we have price data for a ticker."""
    row = conn.execute(
        "SELECT MAX(date) as max_date FROM daily_prices WHERE ticker = ?",
        (ticker,)
    ).fetchone()
    return row["max_date"] if row and row["max_date"] else None


def get_price_dataframe(
    conn: sqlite3.Connection,
    ticker: str,
    days: int = 200
) -> pd.DataFrame:
    """Load recent price data as a DataFrame, sorted by date ascending."""
    df = pd.read_sql_query(
        """
        SELECT * FROM daily_prices
        WHERE ticker = ?
        ORDER BY date DESC
        LIMIT ?
        """,
        conn,
        params=(ticker, days),
    )
    if not df.empty:
        df = df.sort_values("date").reset_index(drop=True)
        df["date"] = pd.to_datetime(df["date"])
    return df


def get_latest_indicators(
    conn: sqlite3.Connection, ticker: str
) -> Optional[dict]:
    """Get the most recent indicator row for a ticker."""
    row = conn.execute(
        """
        SELECT * FROM indicators
        WHERE ticker = ?
        ORDER BY date DESC LIMIT 1
        """,
        (ticker,)
    ).fetchone()
    return dict(row) if row else None


def get_latest_fundamentals(
    conn: sqlite3.Connection, ticker: str
) -> Optional[dict]:
    """Get the most recent fundamental data for a ticker."""
    row = conn.execute(
        """
        SELECT * FROM fundamentals
        WHERE ticker = ?
        ORDER BY period DESC LIMIT 1
        """,
        (ticker,)
    ).fetchone()
    return dict(row) if row else None


def get_recent_news(
    conn: sqlite3.Connection, ticker: str, limit: int = 10
) -> list[dict]:
    """Get recent news headlines for a ticker."""
    rows = conn.execute(
        """
        SELECT headline, source, url, published_at
        FROM news
        WHERE ticker = ?
        ORDER BY published_at DESC
        LIMIT ?
        """,
        (ticker, limit)
    ).fetchall()
    return [dict(r) for r in rows]


def get_next_earnings(
    conn: sqlite3.Connection, ticker: str
) -> Optional[dict]:
    """Get the next upcoming earnings date for a ticker."""
    today = date.today().isoformat()
    row = conn.execute(
        """
        SELECT * FROM earnings_calendar
        WHERE ticker = ? AND earnings_date >= ?
        ORDER BY earnings_date ASC LIMIT 1
        """,
        (ticker, today)
    ).fetchone()
    return dict(row) if row else None


if __name__ == "__main__":
    print(f"Initializing database at {DB_PATH}...")
    init_database()
    print("Done. Tables created.")
