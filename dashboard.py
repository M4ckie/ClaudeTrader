"""
SwingTrader AI — Streamlit Dashboard

Run with: .venv/bin/streamlit run dashboard.py
"""

import json
import sys
from pathlib import Path

import pandas as pd
import streamlit as st

sys.path.insert(0, str(Path(__file__).resolve().parent))

from config.settings import SCENARIOS, DEFAULT_SCENARIO
from data.database import db_session, init_database

init_database()

# ── Page config ─────────────────────────────────────────────────────────────
st.set_page_config(
    page_title="SwingTrader AI",
    page_icon="📈",
    layout="wide",
    initial_sidebar_state="expanded",
)

# ── Sidebar nav ──────────────────────────────────────────────────────────────
st.sidebar.title("SwingTrader AI")

scenario_labels = {k: v["label"] for k, v in SCENARIOS.items()}
selected_scenario = st.sidebar.selectbox(
    "Scenario",
    options=list(scenario_labels.keys()),
    format_func=lambda k: scenario_labels[k],
)

page = st.sidebar.radio(
    "Navigate",
    ["Portfolio Overview", "Trade Journal", "Market Data", "News Feed"],
)
st.sidebar.divider()
if st.sidebar.button("Refresh Data"):
    st.cache_data.clear()
    st.rerun()


# ── Data loaders (cached) ────────────────────────────────────────────────────

@st.cache_data(ttl=60)
def load_portfolio_snapshots(scenario: str = DEFAULT_SCENARIO):
    with db_session() as conn:
        df = pd.read_sql_query(
            "SELECT * FROM portfolio_snapshots WHERE scenario = ? ORDER BY date ASC",
            conn, params=(scenario,)
        )
    return df


@st.cache_data(ttl=60)
def load_trades(scenario: str = DEFAULT_SCENARIO):
    with db_session() as conn:
        df = pd.read_sql_query(
            "SELECT * FROM trades WHERE scenario = ? ORDER BY executed_at DESC",
            conn, params=(scenario,)
        )
    return df


@st.cache_data(ttl=60)
def load_latest_snapshot(scenario: str = DEFAULT_SCENARIO):
    with db_session() as conn:
        row = conn.execute(
            "SELECT * FROM portfolio_snapshots WHERE scenario = ? ORDER BY date DESC LIMIT 1",
            (scenario,)
        ).fetchone()
    return dict(row) if row else None


@st.cache_data(ttl=60)
def load_prices(ticker: str, days: int = 90):
    with db_session() as conn:
        df = pd.read_sql_query(
            """
            SELECT date, open, high, low, close, volume
            FROM daily_prices
            WHERE ticker = ?
            ORDER BY date DESC
            LIMIT ?
            """,
            conn,
            params=(ticker, days),
        )
    df = df.sort_values("date").reset_index(drop=True)
    df["date"] = pd.to_datetime(df["date"])
    return df


@st.cache_data(ttl=60)
def load_indicators(ticker: str):
    with db_session() as conn:
        df = pd.read_sql_query(
            """
            SELECT * FROM indicators
            WHERE ticker = ?
            ORDER BY date DESC
            LIMIT 90
            """,
            conn,
            params=(ticker,),
        )
    df = df.sort_values("date").reset_index(drop=True)
    df["date"] = pd.to_datetime(df["date"])
    return df


@st.cache_data(ttl=60)
def load_watchlist():
    with db_session() as conn:
        rows = conn.execute(
            "SELECT ticker, name, sector FROM stocks ORDER BY ticker"
        ).fetchall()
    return [dict(r) for r in rows]


@st.cache_data(ttl=60)
def load_news(ticker: str = None, limit: int = 30):
    with db_session() as conn:
        if ticker:
            df = pd.read_sql_query(
                """
                SELECT ticker, headline, source, published_at
                FROM news WHERE ticker = ?
                ORDER BY published_at DESC LIMIT ?
                """,
                conn, params=(ticker, limit),
            )
        else:
            df = pd.read_sql_query(
                """
                SELECT ticker, headline, source, published_at
                FROM news ORDER BY published_at DESC LIMIT ?
                """,
                conn, params=(limit,),
            )
    return df


@st.cache_data(ttl=60)
def load_fundamentals(ticker: str):
    with db_session() as conn:
        row = conn.execute(
            "SELECT * FROM fundamentals WHERE ticker = ? ORDER BY period DESC LIMIT 1",
            (ticker,)
        ).fetchone()
    return dict(row) if row else None


# ── Helper ───────────────────────────────────────────────────────────────────

def fmt_currency(val):
    if val is None:
        return "—"
    return f"${val:,.2f}"

def fmt_pct(val, decimals=2):
    if val is None:
        return "—"
    return f"{val:+.{decimals}f}%"

def color_pct(val):
    if val is None:
        return "—"
    color = "green" if val >= 0 else "red"
    return f":{color}[{val:+.2f}%]"


# ════════════════════════════════════════════════════════════════════════════
# PAGE: Portfolio Overview
# ════════════════════════════════════════════════════════════════════════════
if page == "Portfolio Overview":
    st.title(f"Portfolio Overview — {scenario_labels[selected_scenario]}")

    snap = load_latest_snapshot(selected_scenario)
    snapshots_df = load_portfolio_snapshots(selected_scenario)

    if snap is None:
        st.info("No portfolio data yet. Run `python main.py trade` to get started.")
    else:
        # ── Top metrics ──────────────────────────────────────────────────────
        col1, col2, col3, col4 = st.columns(4)
        col1.metric("Total Value", fmt_currency(snap["total_value"]))
        col2.metric("Cash", fmt_currency(snap["cash"]))
        col3.metric("Invested", fmt_currency(snap["positions_value"]))
        daily_pnl = snap.get("daily_pnl_pct", 0) or 0
        col4.metric("Daily P&L", fmt_pct(daily_pnl), delta=f"{daily_pnl:+.2f}%")

        st.divider()

        # ── Equity curve ────────────────────────────────────────────────────
        if not snapshots_df.empty:
            st.subheader("Equity Curve")
            snapshots_df["date"] = pd.to_datetime(snapshots_df["date"])
            st.line_chart(
                snapshots_df.set_index("date")[["total_value", "cash"]],
                height=300,
            )

            # Daily P&L bar chart
            st.subheader("Daily P&L %")
            pnl_df = snapshots_df.set_index("date")[["daily_pnl_pct"]].dropna()
            st.bar_chart(pnl_df, height=200)

        # ── Open positions ───────────────────────────────────────────────────
        st.subheader("Open Positions")
        positions_json = snap.get("positions_json")
        if positions_json:
            positions = json.loads(positions_json)
            if positions:
                rows = []
                for ticker, pos in positions.items():
                    current = pos.get("current_price", pos["avg_price"])
                    pnl_pct = (current - pos["avg_price"]) / pos["avg_price"] * 100
                    pnl_dollar = (current - pos["avg_price"]) * pos["quantity"]
                    rows.append({
                        "Ticker": ticker,
                        "Qty": pos["quantity"],
                        "Avg Cost": pos["avg_price"],
                        "Current": current,
                        "P&L %": round(pnl_pct, 2),
                        "P&L $": round(pnl_dollar, 2),
                        "Stop Loss": pos.get("stop_loss_price"),
                        "Sector": pos.get("sector", ""),
                    })
                pos_df = pd.DataFrame(rows)
                st.dataframe(pos_df, use_container_width=True, hide_index=True)
            else:
                st.info("No open positions.")
        else:
            st.info("No open positions.")

        # ── Performance stats ────────────────────────────────────────────────
        if len(snapshots_df) > 1:
            st.subheader("Performance")
            initial_capital = SCENARIOS[selected_scenario]["initial_capital"]
            total_return = (snap["total_value"] - initial_capital) / initial_capital * 100
            best_day = snapshots_df["daily_pnl_pct"].max()
            worst_day = snapshots_df["daily_pnl_pct"].min()
            win_days = (snapshots_df["daily_pnl_pct"] > 0).sum()
            total_days = snapshots_df["daily_pnl_pct"].notna().sum()

            c1, c2, c3, c4 = st.columns(4)
            c1.metric("Total Return", fmt_pct(total_return))
            c2.metric("Best Day", fmt_pct(best_day))
            c3.metric("Worst Day", fmt_pct(worst_day))
            c4.metric("Win Days", f"{win_days}/{total_days}")


# ════════════════════════════════════════════════════════════════════════════
# PAGE: Trade Journal
# ════════════════════════════════════════════════════════════════════════════
elif page == "Trade Journal":
    st.title(f"Trade Journal — {scenario_labels[selected_scenario]}")

    trades_df = load_trades(selected_scenario)

    if trades_df.empty:
        st.info("No trades yet. Run `python main.py trade` to execute trades.")
    else:
        # ── Summary stats ────────────────────────────────────────────────────
        buys = trades_df[trades_df["action"] == "BUY"]
        sells = trades_df[trades_df["action"] == "SELL"]

        c1, c2, c3 = st.columns(3)
        c1.metric("Total Trades", len(trades_df))
        c2.metric("Buys", len(buys))
        c3.metric("Sells", len(sells))

        st.divider()

        # ── Filter ───────────────────────────────────────────────────────────
        col1, col2 = st.columns(2)
        tickers = ["All"] + sorted(trades_df["ticker"].unique().tolist())
        selected_ticker = col1.selectbox("Filter by ticker", tickers)
        selected_action = col2.selectbox("Filter by action", ["All", "BUY", "SELL"])

        filtered = trades_df.copy()
        if selected_ticker != "All":
            filtered = filtered[filtered["ticker"] == selected_ticker]
        if selected_action != "All":
            filtered = filtered[filtered["action"] == selected_action]

        # ── Trade table ──────────────────────────────────────────────────────
        display_cols = ["executed_at", "ticker", "action", "quantity", "price",
                        "total_value", "confidence"]
        st.dataframe(
            filtered[display_cols].rename(columns={
                "executed_at": "Time",
                "ticker": "Ticker",
                "action": "Action",
                "quantity": "Qty",
                "price": "Price",
                "total_value": "Total",
                "confidence": "Confidence",
            }),
            use_container_width=True,
            hide_index=True,
        )

        # ── Reasoning viewer ─────────────────────────────────────────────────
        st.subheader("Claude's Reasoning")
        if not filtered.empty:
            trade_options = [
                f"{row['executed_at'][:16]}  {row['action']} {row['ticker']}"
                for _, row in filtered.iterrows()
            ]
            selected_trade = st.selectbox("Select trade to inspect", trade_options)
            idx = trade_options.index(selected_trade)
            trade_row = filtered.iloc[idx]

            st.markdown(f"**{trade_row['action']} {trade_row['ticker']}** "
                        f"— {trade_row['quantity']} shares @ {fmt_currency(trade_row['price'])}")
            if trade_row.get("confidence"):
                st.progress(float(trade_row["confidence"]),
                            text=f"Confidence: {trade_row['confidence']:.0%}")
            reasoning = trade_row.get("reasoning", "")
            if reasoning:
                st.info(reasoning)
            else:
                st.caption("No reasoning recorded.")


# ════════════════════════════════════════════════════════════════════════════
# PAGE: Market Data
# ════════════════════════════════════════════════════════════════════════════
elif page == "Market Data":
    st.title("Market Data")

    watchlist = load_watchlist()
    tickers = [s["ticker"] for s in watchlist]
    ticker_map = {s["ticker"]: s for s in watchlist}

    selected = st.selectbox("Select ticker", tickers)

    if selected:
        info = ticker_map.get(selected, {})
        st.caption(f"{info.get('name', '')}  ·  {info.get('sector', '')}")

        prices_df = load_prices(selected, days=90)
        ind_df = load_indicators(selected)
        fund = load_fundamentals(selected)

        if prices_df.empty:
            st.warning("No price data available.")
        else:
            latest = prices_df.iloc[-1]
            prev = prices_df.iloc[-2] if len(prices_df) > 1 else latest
            day_chg = (latest["close"] - prev["close"]) / prev["close"] * 100

            c1, c2, c3, c4 = st.columns(4)
            c1.metric("Price", fmt_currency(latest["close"]),
                      delta=f"{day_chg:+.2f}%")
            c2.metric("High", fmt_currency(latest["high"]))
            c3.metric("Low", fmt_currency(latest["low"]))
            c4.metric("Volume", f"{int(latest['volume'] or 0):,}")

            # Price chart
            st.subheader("Price (90 days)")
            chart_df = prices_df.set_index("date")[["close"]]
            if not ind_df.empty:
                ind_chart = ind_df.set_index("date")[["sma_20", "sma_50"]].dropna()
                chart_df = chart_df.join(ind_chart, how="left")
            st.line_chart(chart_df, height=300)

            # Volume
            st.subheader("Volume")
            st.bar_chart(prices_df.set_index("date")[["volume"]], height=150)

        # ── Technical indicators ─────────────────────────────────────────────
        if not ind_df.empty:
            st.subheader("Technical Indicators (latest)")
            latest_ind = ind_df.iloc[-1]

            c1, c2, c3 = st.columns(3)
            c1.metric("RSI (14)", f"{latest_ind['rsi_14']:.1f}" if latest_ind['rsi_14'] else "—")
            c2.metric("MACD", f"{latest_ind['macd']:.2f}" if latest_ind['macd'] else "—")
            c3.metric("ATR (14)", f"{latest_ind['atr_14']:.2f}" if latest_ind['atr_14'] else "—")

            c1, c2, c3 = st.columns(3)
            c1.metric("SMA 20", fmt_currency(latest_ind["sma_20"]))
            c2.metric("SMA 50", fmt_currency(latest_ind["sma_50"]))
            c3.metric("SMA 200", fmt_currency(latest_ind["sma_200"]))

            # RSI chart
            if "rsi_14" in ind_df.columns:
                st.subheader("RSI (14)")
                rsi_df = ind_df.set_index("date")[["rsi_14"]].dropna()
                st.line_chart(rsi_df, height=150)

        # ── Fundamentals ─────────────────────────────────────────────────────
        if fund:
            st.subheader(f"Fundamentals ({fund.get('period', 'latest')})")
            c1, c2, c3, c4 = st.columns(4)
            c1.metric("P/E", f"{fund['pe_ratio']:.1f}" if fund.get("pe_ratio") else "—")
            c2.metric("P/B", f"{fund['pb_ratio']:.2f}" if fund.get("pb_ratio") else "—")
            c3.metric("ROE", f"{fund['roe']:.1%}" if fund.get("roe") else "—")
            c4.metric("Div Yield", f"{fund['dividend_yield']:.2f}%" if fund.get("dividend_yield") else "—")


# ════════════════════════════════════════════════════════════════════════════
# PAGE: News Feed
# ════════════════════════════════════════════════════════════════════════════
elif page == "News Feed":
    st.title("News Feed")

    watchlist = load_watchlist()
    tickers = ["All"] + [s["ticker"] for s in watchlist]
    selected_ticker = st.selectbox("Filter by ticker", tickers)

    news_df = load_news(
        ticker=None if selected_ticker == "All" else selected_ticker,
        limit=50,
    )

    if news_df.empty:
        st.info("No news articles yet. Run `python main.py news` to fetch headlines.")
    else:
        for _, row in news_df.iterrows():
            pub = str(row.get("published_at", ""))[:10]
            with st.container():
                col1, col2 = st.columns([1, 8])
                col1.markdown(f"**{row['ticker']}**  \n`{pub}`")
                col2.markdown(f"{row['headline']}  \n*{row.get('source', '')}*")
            st.divider()
