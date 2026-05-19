"""
LN Operator — Configuration

All tuneable settings in one place. The pipeline reads these on every run,
so changes take effect on the next cycle. No restart needed.
"""

import os
from pathlib import Path

# Load .env if present — keeps secrets out of this file
try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).parent / ".env")
except ImportError:
    pass


# ─── LND Connection ──────────────────────────────────────────────
LND_REST_URL = os.getenv("LND_REST_URL", "https://127.0.0.1:9000")
LND_CERT = os.getenv("LND_CERT", "/home/lnd/tls.cert")
LND_MACAROON = os.getenv("LND_MACAROON", "/home/lnd/data/chain/bitcoin/mainnet/admin.macaroon")

# ─── Claude API (optional — powers investment advisor peer research) ──
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
ANTHROPIC_MODEL = os.getenv("ANTHROPIC_MODEL", "claude-sonnet-4-6")

# ─── Telegram (optional — pipeline notifications and alerts) ─────
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

# ─── Database ────────────────────────────────────────────────────
DB_PATH = os.getenv("LN_OPERATOR_DB", os.path.join(os.path.dirname(__file__), "ln_operator.db"))


# ─── Fee Policy ──────────────────────────────────────────────────
# Dynamic per-channel fee from local balance ratio:
#   ppm = MIN + (MAX - MIN) × (1 - local_ratio)
# Full channel → MIN (attract routing). Depleted → MAX (protect liquidity).
FEE_BASE_MSAT = 0       # base fee per HTLC (0 = best practice)
FEE_MIN_PPM = 50         # floor: channel is full, want to drain
FEE_MAX_PPM = 500        # ceiling: channel is depleted, protect what's left


# ─── Rebalancing Thresholds ──────────────────────────────────────
REBALANCE_LOW_THRESHOLD = 0.20    # below 20% local → depleted, needs sats in
REBALANCE_HIGH_THRESHOLD = 0.80   # above 80% local → overfull, can push sats out
REBALANCE_TARGET = 0.50           # aim for 50% after rebalance

# How much to move per attempt (% of channel capacity)
REBALANCE_MAX_AMOUNT_RATIO = 0.5  # 50% max — auto-chunks on failure

# ─── Rebalance Budget (3-tier system) ────────────────────────────
# Each channel gets a fee budget based on its track record.
# Prevents spending more on rebalancing than a channel earns.
REBALANCE_DISCOVERY_PPM = 1000      # new channels: generous budget while proving themselves
REBALANCE_HARD_CAP_PPM = 1000       # proven channels: absolute ceiling
REBALANCE_REVENUE_RATIO = 0.5       # proven channels: budget = earned_ppm × 0.5
REBALANCE_DEADWEIGHT_PPM = 150      # zero-revenue channels: minimal budget
REBALANCE_DISCOVERY_DAYS = 15       # balanced days before a channel is judged

# What counts as "balanced" for the discovery clock
REBALANCE_BALANCED_RATIO = 0.30      # local must be above 30%...
REBALANCE_BALANCED_RATIO_HIGH = 0.70 # ...and below 70% for time to count


# ─── Channel Planning (plan command) ─────────────────────────────
# Treasury — operational buffer kept in wallet
TREASURY_MIN_RATIO = 0.025          # 2.5% of wallet balance
TREASURY_MONTHS_RESERVE = 3         # or 3 months of avg rebalance costs, whichever higher

# Channel sizing
MIN_CHANNEL_SIZE_SATS = 1_000_000        # absolute floor (1M)
PREFERRED_CHANNEL_SIZE_SATS = 3_000_000  # default min, overridable via --min-channel
MAX_CHANNEL_SIZE_SATS = 16_777_215       # LND wumbo max

# LND anchor reserve — locked per channel for emergency force-close fee bumping
ANCHOR_RESERVE_PER_CHANNEL = 10_000
ANCHOR_RESERVE_MAX = 100_000             # LND's hard cap

# Candidate scoring (must sum to 1.0)
# Fee is displayed but not scored — local graph fee data is unreliable.
PEER_SCORE_WEIGHTS = {
    "diversity":  0.50,   # new reach: % of their peers not already in your graph
    "centrality": 0.50,   # network importance: channels + capacity (log-normalised)
}


# ─── External Fallbacks ─────────────────────────────────────────
MEMPOOL_API = "https://mempool.space/api"  # fee estimate fallback
ONEML_API = "https://1ml.com"              # alias enrichment
