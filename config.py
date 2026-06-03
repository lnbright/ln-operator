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
#   4. floor  = last successful refill ppm × FEE_MARGIN (0 if never refilled)
#   5. target = clamp( max(base * (1+mult), floor), 0, FEE_HARD_CEILING_PPM )
#   6. Broadcast only if hysteresis allows (tolerance + cooldown + snap escape)
FEE_BASE_MSAT = 0       # base fee per HTLC (0 = best practice)

# Sigmoid curve — asymptotes of the liquidity-driven base fee
SIGMOID_MIN_PPM   = 25        # local_ratio → 1.0 (drain)
SIGMOID_MAX_PPM   = 750       # local_ratio → 0.0 (defend). Lifted from 250: with
                              # refill costs often ~1000+ ppm, a 250 ceiling can't
                              # let a draining channel defend with price before a
                              # paid rebalance fires. 750 ≈ 3× headroom, still well
                              # below refill cost / FEE_HARD_CEILING_PPM so the
                              # last-refill floor still does work above it.
SIGMOID_K         = 8.0       # steepness; higher = sharper midpoint, flatter edges.
                              # K=8 gives clean plateaus near 0% and 100% local while
                              # staying roughly linear-ish through the healthy middle.
                              # Hysteresis (not the curve) is what damps gossip spam.
SIGMOID_MIDPOINT  = 0.5       # local_ratio where curve is halfway between min and max

# Absolute cap on outbound fee. The floor can push the target above
# SIGMOID_MAX_PPM; this is the last line of defence against runaway data.
# Matched to REBALANCE_MAX_BUDGET_PPM so a channel can always charge enough
# outbound to recoup what we're willing to pay to refill it.
FEE_HARD_CEILING_PPM = 5000

# Hysteresis — when to actually broadcast a fee change
FEE_HYSTERESIS_TOLERANCE_PPM   = 10       # absolute floor on what counts as "changed"
FEE_HYSTERESIS_TOLERANCE_PCT   = 0.10     # also need ≥10% relative move
FEE_HYSTERESIS_COOLDOWN_SEC    = 6 * 3600 # don't update same channel within 6h
FEE_HYSTERESIS_SNAP_PPM        = 30       # delta this big escapes the cooldown
FEE_HYSTERESIS_EDGE_LOW        = 0.20     # crossing into/out of this also escapes
FEE_HYSTERESIS_EDGE_HIGH       = 0.80     # crossing into/out of this also escapes

# Market multiplier — slow-moving per-channel adjustment from observed demand
MARKET_MULT_STEP        = 0.15     # how much to nudge per nightly recompute.
                                   # 0.15 = full saturation (0→+1.0) in ~7 nights,
                                   # full deflation (0→-0.5) in ~4 nights.
MARKET_MULT_MIN         = -0.5     # never lower base by more than 50%
MARKET_MULT_MAX         = 1.0      # cap at 2× base (1 + 1.0). With SIGMOID_MAX_PPM=750
                                   # the demand-amplified outbound max is 750×2=1500.
MARKET_MULT_BUSY_HOURS  = 24       # forwards in last N hours → nudge up
MARKET_MULT_SILENT_DAYS = 3        # no forwards for N days → nudge down

# Fast-drain bump — the routine ±STEP drift above runs nightly (slow baseline).
# Separately, the 2h fee loop applies an UP-ONLY emergency bump when a depleted
# channel is dropping forwards for lack of liquidity (forward_fail_log
# INSUFFICIENT_BALANCE), so a fast drainer's resting fee climbs after the FIRST
# bad cycle instead of waiting days for the nightly drift. Up fast, down slow.
MARKET_MULT_FASTDRAIN_STEP = 0.40  # market_multiplier up-nudge on a fast-drain cycle

# ─── Soft outbound floor decay ───────────────────────────────────
# The last-refill floor (last_refill × REBALANCE_FEE_MARGIN) is a HARD floor only
# while the channel is forwarding. If a channel sits idle at/above the floor — the
# floor pricing it out of its own market — the effective floor decays toward the
# market-clearing fee so it can find a price that actually sells. Resets to the
# full hard floor the instant a forward lands or a fresh refill changes last_refill.
FLOOR_DECAY_HALFLIFE_DAYS = 7.0    # gap (hard_floor − clearing) halves every N idle days; 0 disables decay
FLOOR_DECAY_IDLE_SECONDS  = 3 * 86400  # only decay after this much silence (matches MARKET_MULT_SILENT_DAYS)
FLOOR_DECAY_MIN_PPM       = 25     # decay never drops the floor below this (absolute outbound floor)

# ─── Rebalancing Thresholds ──────────────────────────────────────
REBALANCE_LOW_THRESHOLD = 0.20    # below 20% local → depleted, needs sats in
REBALANCE_HIGH_THRESHOLD = 0.80   # above 80% local → overfull, can push sats out
REBALANCE_TARGET = 0.50           # aim for 50% after rebalance

# How much to move per attempt (% of channel capacity)
REBALANCE_MAX_AMOUNT_RATIO = 0.5  # 50% max — auto-chunks on failure

# ─── Rebalance Budget & Fee Coupling ─────────────────────────────
# Single-signal model: the last successful refill ppm for a channel drives
# both (a) the rebalance budget (what we'll pay to refill again) and
# (b) the outbound fee floor (what we charge to recoup + margin).
#
# Bootstrap: channels with no successful refill yet start at REBALANCE_DEFAULT_BUDGET_PPM.
# Drift / re-bootstrap: each consecutive failure since last success raises the
# budget by REBALANCE_BUDGET_ESCALATION_STEP (10% by default), so the system
# self-discovers price without any tier classifications.
REBALANCE_DEFAULT_BUDGET_PPM       = 500    # bootstrap budget when no refill history
REBALANCE_MAX_BUDGET_PPM           = 5000   # hard ceiling on what we'll ever pay
REBALANCE_BUDGET_ESCALATION_STEP   = 0.20   # per consecutive failure since last success
REBALANCE_FEE_MARGIN               = 1.1    # outbound fee floor = last_refill × this

# ─── Profitability gate (Layer 1) ────────────────────────────────
# Don't pay more to refill a channel than it can earn back. The escalation above
# still bootstraps/discovers price freely; this caps it for channels we have
# enough data to JUDGE. A judged channel's budget is capped at its trailing
# earned-ppm × horizon (≈ how many fill/drain cycles we'll wait to recoup).
# Unjudged channels (too little volume to trust the ratio) keep full escalation.
EARNED_PPM_WINDOW_DAYS             = 21         # trailing window for per-channel earned-ppm
EARNED_PPM_MIN_VOLUME_SATS         = 2_000_000  # min OUT-traffic to trust the ratio; below → unjudged
REBALANCE_PROFIT_HORIZON           = 1.25       # judged budget cap = earned_ppm × this.
                                                # ≈ break-even: only refill if demonstrated
                                                # willingness-to-pay (earned_ppm) roughly covers
                                                # the recoup price (refill × FEE_MARGIN). The 0.25
                                                # over 1.0 allows for earned_ppm being measured at
                                                # our older, lower outbound fees.
REBALANCE_STRUCTURAL_FAIL_THRESHOLD= 5          # consecutive fails while profit-capped → flag structural

# What counts as "balanced" for status reporting (no longer gates budget tiers)
REBALANCE_BALANCED_RATIO = 0.30      # local must be above 30%...
REBALANCE_BALANCED_RATIO_HIGH = 0.70 # ...and below 70% for time to count


# ─── Node-level inbound fees + liquidity ladder (Layer 3) ────────
# When a depleted channel can't be profitably rebalanced, defend it with a
# NEGATIVE inbound fee (a discount) to attract organic refill traffic — cheaper
# than a circular rebalance and it doesn't raise outbound / price out demand.
# The discount is a rescue subsidy: largest when most depleted, tapering to 0 by
# INBOUND_DISCOUNT_CLEAR_RATIO ("out of danger" — full refill to 50% stays the
# rebalancer's job). Negative inbound is backward-compatible (older senders just
# ignore it). POSITIVE inbound (a charge) is NOT — it breaks unupgraded senders
# and LND refuses it by default — so it stays off unless deliberately enabled.
INBOUND_FEE_ENABLED                = True   # master switch; negative-discount path only (INBOUND_CHARGE_PPM=0)
INBOUND_DISCOUNT_MAX_PPM           = 200    # largest discount, applied when most depleted
INBOUND_DISCOUNT_CLEAR_RATIO       = 0.35   # taper discount to 0 by here; engage zone is < this
INBOUND_DISCOUNT_SAFETY_MARGIN_PPM = 10     # discount ≤ our_outbound − this (keeps summed fee > 0)
INBOUND_CHARGE_PPM                 = 0      # positive inbound on heavy-sink sources; 0 = disabled (risky)
INBOUND_HYSTERESIS_PPM             = 25     # min inbound-fee move before re-broadcast (gossip damping)
INBOUND_DEFENSE_WINDOW_DAYS        = 14     # inbound-discount defense duration before flagging structural


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

# Candidate ranking is two-stage and tier-segmented — see advisor.py.
# Stage 1: centrality (channels + capacity, log-normalised) prefilters within each tier.
# Stage 2: diversity (% of candidate's peers not already in your graph) reranks the survivors.
# Fee is displayed but not scored — local graph fee data is unreliable.
# No weights — the stages are sequential, not blended.


# ─── External Fallbacks ─────────────────────────────────────────
MEMPOOL_API = "https://mempool.space/api"  # fee estimate fallback
ONEML_API = "https://1ml.com"              # alias enrichment
