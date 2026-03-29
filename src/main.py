"""MEXC Futures Trading Bot — Entry Point.

Initializes structured logging, loads configuration, and starts
the main trading loop. Handles top-level exceptions and ensures
graceful shutdown.
"""

from __future__ import annotations

import asyncio
import sys
import logging

import structlog

from src.core.bot import TradingBot
from src.core.config import get_config_manager


def setup_logging(log_level: str = "INFO", log_format: str = "json") -> None:
    """Configure structured logging with structlog.

    Sets up JSON-formatted logs for production and
    console-formatted logs for development.

    Args:
        log_level: Logging level (DEBUG, INFO, WARNING, ERROR).
        log_format: Output format ('json' or 'console').
    """
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.StackInfoRenderer(),
            structlog.dev.set_exc_info,
            structlog.processors.format_exc_info,
            structlog.processors.TimeStamper(fmt="iso"),
            (
                structlog.processors.JSONRenderer()
                if log_format == "json"
                else structlog.dev.ConsoleRenderer(colors=True)
            ),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(
            getattr(logging, log_level.upper(), logging.INFO)
        ),
        context_class=dict,
        logger_factory=structlog.PrintLoggerFactory(),
        cache_logger_on_first_use=True,
    )


async def main() -> int:
    """Main entry point.

    Loads config, initializes the bot, and runs the main loop.
    Returns exit code for sys.exit().

    Returns:
        0 on clean shutdown, 1 on error.
    """
    setup_logging()
    log = structlog.get_logger("main")

    try:
        log.info("loading_configuration")
        config_mgr = get_config_manager()
        app_config = config_mgr.to_app_config()

        # Mask sensitive values for logging
        masked = config_mgr.get_masked_config()
        log.info("config_loaded", environment=app_config.environment)

        bot = TradingBot(app_config)
        await bot.start()

    except KeyboardInterrupt:
        log.info("shutdown_requested", reason="keyboard_interrupt")
    except Exception:
        log.exception("fatal_error")
        return 1

    return 0


if __name__ == "__main__":
    exit_code = asyncio.run(main())
    sys.exit(exit_code)
