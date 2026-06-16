"""Central logging: a rotating file under logs/ plus the console.

Call setup() once at startup (CLI / UI / JobManager all do). Everything logs through the
"acdl" logger tree, so app activity and errors land in logs/acdl.log with timestamps while
the console stays clean (message-only).
"""
from __future__ import annotations
import logging
import os
from logging.handlers import RotatingFileHandler

_configured = False


def setup(log_dir: str = "logs", level: int = logging.INFO, console: bool = True) -> logging.Logger:
    """Idempotently configure the 'acdl' logger. Returns it."""
    global _configured
    logger = logging.getLogger("acdl")
    if _configured:
        return logger
    logger.setLevel(level)
    logger.propagate = False
    try:
        os.makedirs(log_dir, exist_ok=True)
        fh = RotatingFileHandler(os.path.join(log_dir, "acdl.log"),
                                 maxBytes=2_000_000, backupCount=5, encoding="utf-8")
        fh.setFormatter(logging.Formatter("%(asctime)s  %(levelname)-7s %(name)s  %(message)s"))
        logger.addHandler(fh)
    except Exception:
        pass  # never let logging setup crash the app
    if console:
        ch = logging.StreamHandler()
        ch.setFormatter(logging.Formatter("%(message)s"))
        logger.addHandler(ch)
    _configured = True
    return logger


def get(name: str = "acdl") -> logging.Logger:
    return logging.getLogger(name)
