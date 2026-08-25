"""Logging configuration.

Uvicorn configures only its own `uvicorn.*` loggers, so without this the
application's own messages — a failed nightly run, a dead job board, a
submission error — never reach the console or the journal. On a headless box
that turns a broken agent into a silent one.
"""

import logging

_configured = False


def configure_logging(verbose: bool = False) -> None:
    """Attach a handler to the root logger. Safe to call more than once."""
    global _configured
    level = logging.DEBUG if verbose else logging.INFO
    if _configured:
        logging.getLogger().setLevel(level)
        return

    handler = logging.StreamHandler()
    handler.setFormatter(
        logging.Formatter(
            "%(asctime)s %(levelname)-7s %(name)s: %(message)s", datefmt="%Y-%m-%d %H:%M:%S"
        )
    )
    root = logging.getLogger()
    root.addHandler(handler)
    root.setLevel(level)

    # These are chatty at INFO and say nothing useful about the agent's work.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    logging.getLogger("apscheduler.executors").setLevel(logging.WARNING)
    _configured = True
