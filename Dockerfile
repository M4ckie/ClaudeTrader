FROM python:3.12-slim

WORKDIR /app

# Install system dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    && rm -rf /var/lib/apt/lists/*

# Install Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt streamlit lxml

# Copy application code
COPY . .

# Create data and log directories
RUN mkdir -p data logs journal

# Expose Streamlit dashboard port
EXPOSE 8501

# Default: run scheduler + dashboard together via start.sh
CMD ["/bin/bash", "/app/start.sh"]
