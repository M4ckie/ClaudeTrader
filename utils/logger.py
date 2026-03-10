"""
Logging setup — configures consistent logging across all modules.
"""

import logging
from pathlib import Path

import sys
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from config.settings import LOG_LEVEL, LOG_FORMAT, LOG_DIR


def setup_logging(name: str = "swingtrader") -> logging.Logger:
    """
    Configure and return a logger instance.

    Logs to both console and a daily log file.
    """
    LOG_DIR.mkdir(parents=True, exist_ok=True)

    logger = logging.getLogger(name)
    logger.setLevel(getattr(logging, LOG_LEVEL, logging.INFO))

    # Avoid adding duplicate handlers
    if logger.handlers:
        return logger

    # Console handler
    console = logging.StreamHandler()
    console.setLevel(logging.INFO)
    console.setFormatter(logging.Formatter(LOG_FORMAT))
    logger.addHandler(console)

    # File handler
    from datetime import date
    log_file = LOG_DIR / f"swingtrader_{date.today().isoformat()}.log"
    file_handler = logging.FileHandler(log_file)
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(logging.Formatter(LOG_FORMAT))
    logger.addHandler(file_handler)

    return logger
