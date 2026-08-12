"""Unified logging for Trove.

Provides get_logger() as the single way to obtain a logger.
Direct use of print() or logging.getLogger() in application
code is prohibited per project conventions.
"""

from __future__ import annotations

import logging
import sys


def get_logger(name: str) -> logging.Logger:
    """Return a configured logger for the given module name.

    All Trove loggers are children of "trove" and inherit
    the same formatting and level configuration.

    Args:
        name: Usually __name__ from the calling module.

    Returns:
        A logging.Logger instance.
    """
    logger = logging.getLogger(name)

    # Only configure if the root trove logger hasn't been set up yet
    if not logging.getLogger("trove").handlers:
        _setup_root_logger()

    return logger


def _setup_root_logger() -> None:
    """Configure the root 'trove' logger with console handler."""
    root = logging.getLogger("trove")
    root.setLevel(logging.INFO)

    handler = logging.StreamHandler(sys.stderr)
    handler.setLevel(logging.INFO)

    formatter = logging.Formatter(
        fmt="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    handler.setFormatter(formatter)

    root.addHandler(handler)
    root.propagate = False
