"""Logging setup with daily rotation and rich console output."""

import logging
import os
import sys
from datetime import datetime
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path


def setup_logger(name: str, log_dir: str = "logs", level: str = "INFO") -> logging.Logger:
    """Create a logger with both file (daily rotation) and console handlers."""
    Path(log_dir).mkdir(parents=True, exist_ok=True)

    logger = logging.getLogger(name)
    logger.setLevel(getattr(logging, level.upper(), logging.INFO))

    if logger.handlers:
        return logger

    fmt = logging.Formatter(
        fmt="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    # Daily rotating file handler
    log_file = os.path.join(log_dir, "pokemonitor.log")
    file_handler = TimedRotatingFileHandler(
        log_file,
        when="midnight",
        interval=1,
        backupCount=30,
        encoding="utf-8",
        utc=False,
    )
    file_handler.setFormatter(fmt)
    file_handler.setLevel(getattr(logging, level.upper(), logging.INFO))
    logger.addHandler(file_handler)

    # Console handler
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setFormatter(fmt)
    console_handler.setLevel(logging.DEBUG)
    logger.addHandler(console_handler)

    # Error-only file handler
    error_file = os.path.join(log_dir, "errors.log")
    error_handler = TimedRotatingFileHandler(
        error_file,
        when="midnight",
        interval=1,
        backupCount=30,
        encoding="utf-8",
    )
    error_handler.setFormatter(fmt)
    error_handler.setLevel(logging.ERROR)
    logger.addHandler(error_handler)

    return logger


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)
