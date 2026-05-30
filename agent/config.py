"""
StockSage AI Trading Agent
==========================
Core configuration loader and shared utilities.
"""
import json
import os
import sys
import uuid
import yaml
from pathlib import Path
from dotenv import load_dotenv
from loguru import logger

# ── Load environment variables ────────────────────────────────
load_dotenv()

# ── Paths ─────────────────────────────────────────────────────
ROOT_DIR = Path(__file__).parent.parent
CONFIG_FILE = ROOT_DIR / "config" / "settings.yaml"
LOGS_DIR = ROOT_DIR / "logs"
LOGS_DIR.mkdir(parents=True, exist_ok=True)


def load_config() -> dict:
    """Load, validate, and return the YAML configuration. Fails fast on bad config."""
    with open(CONFIG_FILE, "r", encoding="utf-8") as f:
        raw = yaml.safe_load(f)

    try:
        from agent.config_schema import validate_config
        validate_config(raw)  # raises pydantic.ValidationError if invalid
    except ImportError:
        pass  # pydantic not installed — skip validation (won't happen in prod)

    return raw


CONFIG = load_config()


# ── Logger setup ─────────────────────────────────────────────
def setup_logger():
    """Configure structured logging with file rotation."""
    logger.remove()  # Remove default handler

    log_level = os.getenv("LOG_LEVEL", "INFO")

    # Console output — colored and readable
    logger.add(
        sys.stderr,
        format="<green>{time:HH:mm:ss}</green> | <level>{level: <8}</level> | <cyan>{name}</cyan> — <level>{message}</level>",
        level=log_level,
        colorize=True,
    )

    # File output — full detail with rotation
    logger.add(
        LOGS_DIR / "agent_{time:YYYY-MM-DD}.log",
        format="{time:YYYY-MM-DD HH:mm:ss.SSS} | {level: <8} | {name}:{function}:{line} — {message}",
        level="DEBUG",
        rotation="1 day",
        retention="30 days",
        compression="gz",
    )

    # Structured JSON log — machine-readable, for monitoring/alerting pipelines
    def _json_sink(message) -> None:
        record = message.record
        payload = {
            "ts": record["time"].isoformat(),
            "level": record["level"].name,
            "logger": record["name"],
            "msg": record["message"],
            "correlation_id": record["extra"].get("correlation_id", ""),
        }
        if record["exception"]:
            import traceback
            payload["exc"] = traceback.format_exception(*record["exception"])
        sys.stdout.write(json.dumps(payload) + "\n")
        sys.stdout.flush()

    if os.getenv("LOG_FORMAT", "text").lower() == "json":
        logger.add(_json_sink, level=log_level, format="{message}")

    # Separate trade log — only BUY/SELL actions
    logger.add(
        LOGS_DIR / "trades_{time:YYYY-MM-DD}.log",
        format="{time:YYYY-MM-DD HH:mm:ss.SSS} | {message}",
        level="INFO",
        filter=lambda record: "TRADE" in record["message"],
        rotation="1 day",
        retention="90 days",
    )

    return logger


# Correlation ID helper — bind to logger context per trading cycle
def bind_correlation_id(cid: str | None = None) -> str:
    cid = cid or uuid.uuid4().hex[:12]
    logger.configure(extra={"correlation_id": cid})
    return cid


setup_logger()
