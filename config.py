"""
LN Operator — Configuration
All tuneable settings in one place.
"""

import os
from pathlib import Path

try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).parent / ".env")
except ImportError:
    pass

# ─── LND connection ───────────────────────────────────────────────
LND_REST_URL = os.getenv("LND_REST_URL", "https://127.0.0.1:9000")
LND_CERT = os.getenv("LND_CERT", "/home/lnd/tls.cert")
LND_MACAROON = os.getenv("LND_MACAROON", "/home/lnd/data/chain/bitcoin/mainnet/admin.macaroon")

# ─── Anthropic API ────────────────────────────────────────────────
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
ANTHROPIC_MODEL = os.getenv("ANTHROPIC_MODEL", "claude-sonnet-4-6")

# ─── Telegram ─────────────────────────────────────────────────────
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

# ─── Database ─────────────────────────────────────────────────────
DB_PATH = os.getenv("LN_OPERATOR_DB", os.path.join(os.path.dirname(__file__), "ln_operator.db"))

# ─── Channel management thresholds ───────────────────────────────
REBALANCE_LOW_THRESHOLD = 0.20
REBALANCE_HIGH_THRESHOLD = 0.80
REBALANCE_TARGET = 0.50

FEE_BASE_MSAT = 0
FEE_MIN_PPM = 50
FEE_MAX_PPM = 500

REBALANCE_MAX_AMOUNT_RATIO = 0.5
REBALANCE_HARD_CAP_PPM = 500
REBALANCE_REVENUE_RATIO = 0.5
REBALANCE_DISCOVERY_PPM = 150
REBALANCE_DEADWEIGHT_PPM = 50
REBALANCE_DISCOVERY_DAYS = 30
REBALANCE_BALANCED_RATIO = 0.30
REBALANCE_BALANCED_RATIO_HIGH = 0.70

# ─── Investment advisor settings ─────────────────────────────────
TREASURY_MIN_RATIO = 0.10
TREASURY_MONTHS_RESERVE = 3

MIN_CHANNEL_SIZE_SATS = 1_000_000
PREFERRED_CHANNEL_SIZE_SATS = 3_000_000
MAX_CHANNEL_SIZE_SATS = 16_777_215

PEER_SCORE_WEIGHTS = {
    "capacity": 0.20,
    "channels": 0.15,
    "uptime": 0.20,
    "centrality": 0.25,
    "diversity": 0.20,
}

# ─── External data sources ───────────────────────────────────────
MEMPOOL_API = "https://mempool.space/api"
ONEML_API = "https://1ml.com"

# ─── Cron schedule defaults ──────────────────────────────────────
FEE_UPDATE_INTERVAL_MINUTES = 30
REBALANCE_CHECK_INTERVAL_MINUTES = 60
MONITOR_INTERVAL_MINUTES = 15
