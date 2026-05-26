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


# ─── Off-site channel.backup upload ──────────────────────────────
# Pushed by backup.py over rsync/ssh whenever LND rewrites the file
# and on a 2h heartbeat timer. Set BACKUP_SSH_HOST in .env to enable.
BACKUP_SOURCE_PATH = os.getenv("BACKUP_SOURCE_PATH", "/home/lnd/lnd-backup/channel.backup")
BACKUP_SSH_HOST    = os.getenv("BACKUP_SSH_HOST", "")    # e.g. "backup-host"
BACKUP_SSH_USER    = os.getenv("BACKUP_SSH_USER", "")    # e.g. "backup-user"
BACKUP_SSH_PORT    = int(os.getenv("BACKUP_SSH_PORT", "22"))
BACKUP_DEST_DIR    = os.getenv("BACKUP_DEST_DIR", "")    # e.g. "/path/on/remote/"


# ─── Fee Policy ──────────────────────────────────────────────────
# The fee target is computed in layers, in this order:
#   1. Pin (fee_overrides table) wins outright.
#   2. base   = sigmoid(local_ratio) between SIGMOID_MIN_PPM and SIGMOID_MAX_PPM
#   3. mult   = market_multiplier (slow, demand-derived, asymmetric at low local)
#   4. floor  = rebalance-cost floor (what refilling actually costs us)
#   5. target = clamp( max(base * (1+mult), floor), 0, FEE_HARD_CEILING_PPM )
#   6. Broadcast only if hysteresis allows (tolerance + cooldown + snap escape)
FEE_BASE_MSAT = 0       # base fee per HTLC (0 = best practice)

# Sigmoid curve — asymptotes of the liquidity-driven base fee
SIGMOID_MIN_PPM   = 25        # local_ratio → 1.0 (drain)
SIGMOID_MAX_PPM   = 250       # local_ratio → 0.0 (defend)
SIGMOID_K         = 8.0       # steepness; higher = sharper midpoint, flatter edges.
                              # K=8 gives clean plateaus near 0% and 100% local while
                              # staying roughly linear-ish through the healthy middle.
                              # Hysteresis (not the curve) is what damps gossip spam.
SIGMOID_MIDPOINT  = 0.5       # local_ratio where curve is halfway between min and max

# Absolute cap. The floor can push the target above SIGMOID_MAX_PPM; the
# hard ceiling is the last line of defence against runaway floor data.
FEE_HARD_CEILING_PPM = 2000

# Hysteresis — when to actually broadcast a fee change
FEE_HYSTERESIS_TOLERANCE_PPM   = 10       # absolute floor on what counts as "changed"
FEE_HYSTERESIS_TOLERANCE_PCT   = 0.10     # also need ≥10% relative move
FEE_HYSTERESIS_COOLDOWN_SEC    = 6 * 3600 # don't update same channel within 6h
FEE_HYSTERESIS_SNAP_PPM        = 30       # delta this big escapes the cooldown
FEE_HYSTERESIS_EDGE_LOW        = 0.20     # crossing into/out of this also escapes
FEE_HYSTERESIS_EDGE_HIGH       = 0.80     # crossing into/out of this also escapes

# Market multiplier — slow-moving per-channel adjustment from observed demand
MARKET_MULT_STEP        = 0.08     # how much to nudge per nightly recompute.
                                   # 0.08 = full saturation (0→+2.0) in ~25 nights,
                                   # full deflation (0→-0.5) in ~7 nights.
MARKET_MULT_MIN         = -0.5     # never lower base by more than 50%
MARKET_MULT_MAX         = 2.0      # cap at 3× base (1 + 2.0)
MARKET_MULT_BUSY_HOURS  = 24       # forwards in last N hours → nudge up
MARKET_MULT_SILENT_DAYS = 3        # no forwards for N days → nudge down

# Rebalance-cost floor — "don't sell outbound below what refilling costs us"
REBALANCE_FLOOR_WINDOW_DAYS    = 30
REBALANCE_FLOOR_MIN_SAMPLES    = 5     # below this, fall back to manual data
REBALANCE_FLOOR_MULTIPLIER     = 1.5   # floor = median(rebalance_ppm) × this
REBALANCE_FLOOR_DEFAULT_PPM    = 0     # no rebalance data → no floor; sigmoid alone
                                       # decides. The floor only kicks in once we have
                                       # real refill-cost evidence for the channel.

# Deprecated linear-curve constants. Kept as aliases so old call sites keep
# working until they're cleaned up. New code should use the SIGMOID_* names.
FEE_MIN_PPM = SIGMOID_MIN_PPM
FEE_MAX_PPM = SIGMOID_MAX_PPM


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
REBALANCE_REVENUE_RATIO = 0.5       # proven channels: budget = earned_ppm × 0.5
REBALANCE_DEADWEIGHT_PPM = 150      # zero-revenue channels: minimal budget
REBALANCE_DISCOVERY_DAYS = 15       # balanced days before a channel is judged

# Adaptive per-channel rebalance cap. Replaces the old global hard cap.
#   cap = clamp( median(successful_rebalance_ppm, 30d, this target) × MULTIPLIER,
#                REBALANCE_CAP_MIN_PPM, REBALANCE_CAP_MAX_PPM )
# A channel with no data gets REBALANCE_CAP_DEFAULT_PPM. Stored in
# channel_signals.adaptive_cap_ppm by the nightly recompute job.
REBALANCE_CAP_DEFAULT_PPM = 1000   # used when no rebalance data exists
REBALANCE_CAP_MIN_PPM     = 500    # adaptive cap never lower than this
REBALANCE_CAP_MAX_PPM     = 5000   # adaptive cap never higher than this
REBALANCE_CAP_MULTIPLIER  = 1.5    # cap = observed median × this

# Kept as alias for any caller still using the old constant — equal to the
# adaptive default so behaviour for new/dataless channels is unchanged.
REBALANCE_HARD_CAP_PPM = REBALANCE_CAP_DEFAULT_PPM

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
