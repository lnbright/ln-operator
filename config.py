"""
LN Operator — Configuration
All tuneable settings in one place.
"""

import os
from pathlib import Path

# Load .env file if it exists — so you don't need to `source .env` manually
try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).parent / ".env")
except ImportError:
    pass  # dotenv not installed — fall back to environment variables

# ─── LND connection ───────────────────────────────────────────────
LND_REST_URL = os.getenv("LND_REST_URL", "https://127.0.0.1:9000")
LND_CERT = os.getenv("LND_CERT", "/home/lnd/tls.cert")
LND_MACAROON = os.getenv("LND_MACAROON", "/home/lnd/data/chain/bitcoin/mainnet/admin.macaroon")

# ─── Anthropic API (for the 10% agent layer) ─────────────────────
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
ANTHROPIC_MODEL = os.getenv("ANTHROPIC_MODEL", "claude-sonnet-4-6")

# ─── Telegram alerts ─────────────────────────────────────────────
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

# ─── Database ────────────────────────────────────────────────────
DB_PATH = os.getenv("LN_OPERATOR_DB", os.path.join(os.path.dirname(__file__), "ln_operator.db"))

# ─── Channel management thresholds ───────────────────────────────
# Rebalancing triggers
REBALANCE_LOW_THRESHOLD = 0.20   # local ratio below this → needs rebalancing up
REBALANCE_HIGH_THRESHOLD = 0.80  # local ratio above this → needs rebalancing down
REBALANCE_TARGET = 0.50          # target local ratio after rebalance

# Fee policy (dynamic, based on local balance ratio)
FEE_BASE_MSAT = 0                # base fee in millisats (0 is modern best practice)
FEE_MIN_PPM = 50                 # floor fee rate when channel is full (local high)
FEE_MAX_PPM = 500                # ceiling fee rate when channel is depleted (local low)

# Rebalancing cost limits (per-channel, adaptive)
REBALANCE_MAX_AMOUNT_RATIO = 0.5      # never rebalance more than 50% of capacity in one go
REBALANCE_HARD_CAP_PPM = 1000         # absolute ceiling — never pay more than this, ever
REBALANCE_REVENUE_RATIO = 0.5         # for proven channels: max fee = earned_ppm × this ratio
REBALANCE_DISCOVERY_PPM = 1000        # for new/unproven channels: budget to discover if they route
REBALANCE_DEADWEIGHT_PPM = 150        # for channels that had a chance and earned nothing
REBALANCE_DISCOVERY_DAYS = 30         # how many days of balanced time before judging a channel
REBALANCE_BALANCED_RATIO = 0.30       # channel counts as "balanced" when local ratio is above this
REBALANCE_BALANCED_RATIO_HIGH = 0.70  # ... and below this

# ─── Anchor reserve settings ─────────────────────────────────────
# LND reserves 10,000 sats per anchor channel for emergency force-close fee bumping.
# Capped at 100,000 sats regardless of channel count (LND's built-in cap).
# This is deducted from deployable sats when calculating investment allocation.
ANCHOR_RESERVE_PER_CHANNEL = 10_000  # sats reserved per new anchor channel
ANCHOR_RESERVE_MAX = 100_000         # LND's hard cap on total anchor reserve

# ─── Investment advisor settings ─────────────────────────────────
# Treasury reserve
TREASURY_MIN_RATIO = 0.025       # always keep at least 2.5% of investment as reserve
TREASURY_MONTHS_RESERVE = 3      # or 3 months of avg rebalancing costs, whichever is higher

# Channel sizing
MIN_CHANNEL_SIZE_SATS = 1_000_000        # absolute minimum channel size (1M)
PREFERRED_CHANNEL_SIZE_SATS = 3_000_000  # preferred minimum (3M)
MAX_CHANNEL_SIZE_SATS = 16_777_215       # LND wumbo channel max without additional config

# Peer scoring weights (must sum to 1.0)
PEER_SCORE_WEIGHTS = {
    "diversity":       0.40,  # % of their peers new to you — most important for a small node
    "centrality":      0.30,  # proxy for network importance (channels + capacity normalised)
    "low_fee":         0.30,  # lower avg outbound fee = cheaper routing & rebalancing
}

# ─── External data sources ───────────────────────────────────────
MEMPOOL_API = "https://mempool.space/api"
ONEML_API = "https://1ml.com"

