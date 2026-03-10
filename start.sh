#!/bin/bash
# SwingTrader AI — container startup script
# Runs the trade scheduler and Streamlit dashboard as background processes.

set -e

echo "=== SwingTrader AI ==="
echo "Initializing database..."
python main.py init

echo "Starting Streamlit dashboard on port 8501..."
streamlit run dashboard.py \
    --server.port 8501 \
    --server.address 0.0.0.0 \
    --server.headless true \
    --browser.gatherUsageStats false \
    &

echo "Starting trade scheduler (runs daily at ${TRADE_TIME:-21:30} UTC)..."
python main.py schedule --time "${TRADE_TIME:-21:30}"
