"""
LN Operator — Logging Configuration
Single place to set up logging for the whole application.
"""

import logging
import logging.handlers
from pathlib import Path

def setup_logging(name="ln_operator", log_dir=None):
    """Set up logging to both terminal and a rotating log file.
    
    - INFO and above goes to terminal (clean, readable)
    - DEBUG and above goes to log file (full detail)
    - Log file rotates at 5MB, keeps 5 backups
    """
    if log_dir is None:
        log_dir = Path(__file__).parent / "logs"
    
    log_dir = Path(log_dir)
    log_dir.mkdir(exist_ok=True)
    log_file = log_dir / "ln_operator.log"

    logger = logging.getLogger(name)
    
    # Don't add handlers if already set up (prevents duplicate logs)
    if logger.handlers:
        return logger

    logger.setLevel(logging.DEBUG)

    # ─── Terminal handler (INFO+) ─────────────────────────────────
    console = logging.StreamHandler()
    console.setLevel(logging.WARNING)
    console.setFormatter(logging.Formatter(
        "%(asctime)s %(levelname)-8s %(message)s",
        datefmt="%H:%M:%S"
    ))

    # ─── File handler (DEBUG+, rotating) ─────────────────────────
    file_handler = logging.handlers.RotatingFileHandler(
        log_file,
        maxBytes=5 * 1024 * 1024,  # 5MB
        backupCount=5,
        encoding="utf-8",
    )
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(logging.Formatter(
        "%(asctime)s %(levelname)-8s [%(module)s.%(funcName)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S"
    ))

    logger.addHandler(console)
    logger.addHandler(file_handler)

    return logger


def set_console_level(level, name="ln_operator"):
    """Raise/lower the terminal handler's level at runtime (file stays DEBUG).

    The default console handler is WARNING-only so the 2h cron path stays quiet.
    Interactive, foreground commands (e.g. manual_rebalance) call this with
    logging.INFO so the operator sees the engine's step-by-step logs live —
    route attempts, per-chunk outcomes, landing channel, final summary."""
    logger = logging.getLogger(name)
    for h in logger.handlers:
        # the console handler is the StreamHandler that is NOT a FileHandler
        if isinstance(h, logging.StreamHandler) and not isinstance(h, logging.FileHandler):
            h.setLevel(level)


def get_logger(module_name):
    """Get a child logger for a specific module."""
    return logging.getLogger(f"ln_operator.{module_name}")
