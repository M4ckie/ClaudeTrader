"""Shared utilities for the data layer."""

import os
import sys as _sys
from contextlib import contextmanager


@contextmanager
def suppress_output():
    """Suppress stdout/stderr (silences yfinance print statements)."""
    with open(os.devnull, "w") as devnull:
        old_stdout, old_stderr = _sys.stdout, _sys.stderr
        _sys.stdout, _sys.stderr = devnull, devnull
        try:
            yield
        finally:
            _sys.stdout, _sys.stderr = old_stdout, old_stderr
