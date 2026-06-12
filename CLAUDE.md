# LN Operator — Project Context

## Infrastructure
- Primary host runs Bitcoin Core + LND + LN Operator + dashboards (bound to a Tailscale IP, see .env)
- Backup host receives channel.backup over SSH (see BACKUP_* keys in .env)
- Specific host/node details intentionally kept out of the repo — query live state from LND or check .env

## Key Design Decisions
- REST API chan_id from LND = numeric scid (not hex) — DB stores this format
- Rebalance costs attributed to target channel only (not source)
- Manual rebalances synced from LND payments by detecting circular self-payments
- Channel open time used as floor to prevent old payment misattribution
- Fee scoring removed from candidates — local graph fee data too unreliable
- **Peer ranking is two-stage and tier-segmented (no weighted blending)**:
  candidates are bucketed by absolute channel count — hub (≥100 ch),
  mid-tier (30-99), small (10-29). Sub-SMALL nodes dropped as noise.
  Within each tier: centrality (log-normalised channels + capacity) acts
  as a cheap prefilter to pick the top `ENRICH_PER_TIER` (30); then
  `get_node_info` is called per candidate to compute diversity (% of
  their peers that sit **outside our 2-hop reachable set** — i.e. would
  actually expand our graph horizon, not just add another edge into
  nodes already reachable through any existing peer). The 2-hop set is
  built once from the local `describe_graph()` edges in
  `_fetch_candidates_from_graph` and stashed on `state["reachable_2hop"]`.
  Using only direct peers (`state["existing_peers"]`) instead skewed the
  metric heavily toward hubs when our channel count was low — almost
  every candidate's peers were "new" by that definition. Top
  `SHOW_PER_TIER` (10) per tier are surfaced. Live LND calls make this
  slow (~90 round-trips) — runs only in `ln-operator plan`, never in the 2h
  pipeline. Constants live in `advisor.py`.
- Plan command is pure local graph — no Claude API agent layer (removed)
- Rebalance auto-chunks on failure (halves down to 100k min). Each successful
  chunk is its own success row in rebalance_log at its actual ppm.
- **`ln-operator manual_rebalance <src> <tgt> <amount_sats> <max_ppm>`** pins ONE
  operator-chosen source→target pair (alias substring or scid), bypassing both the
  auto planner's ratio-based pair selection AND the profit/structural ladder gate —
  the only way to refill a channel the gate has flagged STRANDED. Builds a plan with
  `triggered_by='manual'` (threaded through `execute_rebalance` →
  `save_rebalance_attempt`, default still `'auto'`) so the row is tagged manual like
  sync-detected ones; `run_id=None` (own episode). A success still re-anchors
  `last_refill_ppm` and clears the failure count like any rebalance. Same chunking +
  sibling landing-channel attribution as the auto path. `cmd_manual_rebalance` in main.py.
- Rebalance executor (`main.execute_rebalance_plans`) carries two ledgers:
  `target_deficits` (sats each depleted target still needs) and `source_remaining`
  (sats each overfull source can still send). Every plan is capped at
  `min(plan amount, target deficit, source remaining)` and skipped entirely
  once either drops below 50k. Fallbacks aren't gated by "did the primary
  fail" — they're just later plan entries that fire when their target still
  has a positive deficit. Partial primary success leaves the deficit open
  for fallbacks; sources exhausted by an earlier success self-skip the rest
  of their plans. Planner attaches `target_total_deficit` and
  `source_total_surplus` to each plan dict so the executor needs no extra
  LND calls.
- **Sibling channels (2+ channels to one peer) are handled by landing-channel
  attribution, not route pinning**: `SendPaymentV2` can pin the last hop only by
  pubkey, and LND's non-strict forwarding pools sibling liquidity at forward time
  anyway — so the executor resolves where each chunk actually settled via
  `lnd_client.get_invoice_landing_chan` (invoice HTLC records = ground truth) and
  writes the rebalance_log row / decrements the deficit ledger against that
  channel. `sync_rebalances` resolves targets from the route's last-hop chan_id
  (`resolve_target_chan`), falling back to the peer map only when the peer has a
  single channel; ambiguous → skip rather than misattribute. Dashboard tags
  duplicate-alias channels with a short scid suffix.
  `engine.update_all_fees` checks the table first; pinned channels use the
  stored ppm with reason `manual pin: N ppm` instead of `calculate_fee_ppm`.
  Set via `ln-operator overwrite_fee`, cleared via `ln-operator clear_fee`, shown by `status`.
- **Pipeline order is rebalance → fees** (`main.cmd_run`): the executor writes
  each chunk's `rebalance_log` row (and thus `last_refill_ppm`) as it settles, so
  running rebalance FIRST lets `update_all_fees` floor every refilled channel off
  the cost it actually paid THIS run, not the previous cycle's anchor. (Was
  fees-first to bump a depleted channel's defensive price before refilling; the
  later fee step still bumps a channel that fails to rebalance, since it still
  reads as depleted.)
- **`last_refill_ppm` budget + fee floor, now profitability-gated (3 layers)**:
  `last_refill_ppm` (most recent successful rebalance into a channel) still
  anchors BOTH the rebalance budget and the outbound fee floor. Base budget =
  `last_refill × (1 + 0.20 × failures_since_last_success)`, capped at 5000;
  bootstrap `REBALANCE_DEFAULT_BUDGET_PPM = 500`. **`failures_since_last_success`
  counts failed pipeline RUNS, not attempts**: one run fans out a primary plan
  plus fallbacks at the same channel, each writing its own failure row, so they
  share a `run_id` (stamped in `execute_rebalance_plans`) and
  `count_failures_since_last_success` counts distinct failed `run_id`s — a run
  that landed any sats (a success row under that `run_id`) is a partial refill,
  not a failed cycle, and is excluded; NULL-`run_id` rows (legacy/manual) count
  per-row. Without this a freshly-opened channel's primary+fallback fan-out in
  ONE run crossed `REBALANCE_STRUCTURAL_FAIL_THRESHOLD` and got stranded in
  under an hour. `TIMEOUT`/`NO_ROUTE` still count (a fee-capped `SendPaymentV2`
  reports them when the only route exceeds the cap — real price evidence).
  Column added + historical rows backfilled (time-clustering, >1h gap = new run)
  by `_migrate_rebalance_run_id`. On top of that:
  - **Layer 1 — profitability gate** (`get_channel_rebalance_budget`): for
    channels with enough trailing OUT-volume to JUDGE (≥ `EARNED_PPM_MIN_VOLUME_SATS`),
    the budget is also capped at `earned_ppm × REBALANCE_PROFIT_HORIZON` — never
    pay more to refill than the channel earns back in ~horizon cycles. UNJUDGED
    channels (`db.get_channel_earned_ppm` returns None) keep full escalation
    untouched (capping them would kill price discovery). Budget dict carries
    `earned_ppm`, `profit_capped`, `structural`, `accelerated`. `plan_rebalances`
    drops targets whose ladder verdict ≠ `rebalance`, so structural channels stop
    being ground. **Earn-ceiling accelerator**: a JUDGED channel whose anchor sits
    far below its earnings — e.g. one lucky-cheap refill pins `last_refill` to
    7 ppm on a channel earning 576 — would crawl `7→8→10→11→14…` and never route,
    sitting depleted with no alarm (it's profitable, so not structural). Instead
    each failed run closes `REBALANCE_BUDGET_ESCALATION_STEP` of the gap between
    the anchor and the affordable ceiling (`min(earned×horizon, MAX)`), reaching
    it in `1/STEP` (=5) runs (bfx 14→721). Reuses STEP (no new knob); only RAISES
    the budget (a `max()`); judged-only; climbs only UP TO the ceiling. Because
    rounding `gap_climb` up can land one ppm above a fractional `profit_cap`,
    `profit_capped` is measured against PLAIN escalation, not the accelerated
    value — so the accelerator can never spuriously strand the channel it rescues
    (accelerator-firing and `profit_capped` are mutually exclusive). Inert unless
    `earned×horizon > 2×anchor`; self-limiting (first success re-anchors).
  - **B8 — QueryRoutes budget acceleration** (`REBALANCE_QUERYROUTES_ENABLED`,
    `_accelerate_budget_with_queryroutes` in `rebalance_planner.py`): the escalation
    ladder discovers the clearing price by FAILING over several runs; a
    `lnd_client.query_routes` dry-run (no payment, same pathfinder + mission control
    as `SendPaymentV2`) reads it directly. For each depleted target the planner
    probes the primary pair (most-overfull source → target, pinned via
    `outgoing_chan_id`, at the size it would attempt) capped at the affordable
    ceiling (`affordable_ceiling_ppm` = `min(profit_cap, MAX)` judged / `MAX`
    unjudged). If a route exists between the current escalated bid and the ceiling,
    THIS run's `max_fee_ppm` jumps straight to that live cost — an affordable refill
    lands now instead of after ~5 escalations (the bfx 14→721 grind). Conservative
    by construction: only ever RAISES the bid (a max), never above the ceiling the
    profit gate already permits (never overpays), never skips an attempt, never
    writes a failure row or moves `last_refill`. Runs ONLY in the planner
    (`get_channel_rebalance_budget` stays call-free — fees/monitor call it per
    channel every run); any probe error/None leaves the budget untouched, so a flaky
    probe can't break planning or change spend. Inert when profit-capped/structural
    (already at ceiling → no headroom → no probe). The case-3/4 *early-out* (skip an
    infeasible attempt + drive structural flagging off the QueryRoutes verdict
    instead of the failure count) is deliberately NOT here yet — it must substitute
    for the failure-count-driven stranding, a separate design.
  - **Layer 2 — soft outbound floor + raised ceiling** (`compute_fee_target`):
    `SIGMOID_MAX_PPM` is 750 (was 250) so a draining channel can defend with price.
    The `last_refill × REBALANCE_FEE_MARGIN` floor is HARD while forwarding but
    DECAYS toward the market-clearing fee once a channel sits idle
    (`FLOOR_DECAY_*`), so a priced-out channel can find a sellable price; resets on
    the next forward / fresh refill. Idle = true silence: recent
    INSUFFICIENT_BALANCE drops gate decay like forwards do (a drop = sender
    accepted the advertised fee but the channel was empty — demand at the current
    price; decaying it would discount the eventual refill and drag earned_ppm /
    the budget cap down). Drops hold the floor, never restore a decayed level. A separate up-only fast-drain market-mult bump
    (`MARKET_MULT_FASTDRAIN_STEP`, fired in the 2h loop on `forward_fail_log`
    INSUFFICIENT_BALANCE) raises the resting fee after the first bad cycle; the
    routine ±`MARKET_MULT_STEP` drift stays nightly.
  - **Layer 3 — node-level inbound fees + ladder** (`engine/liquidity_policy.py`,
    enabled `INBOUND_FEE_ENABLED=True`): a depleted channel that can't be
    profitably rebalanced is defended with a NEGATIVE inbound fee (discount) to pull
    organic refill — a rescue subsidy largest near 0% local, tapering to 0 by
    `INBOUND_DISCOUNT_CLEAR_RATIO` (out of danger, not full target), capped at
    `our_outbound − safety_margin`. `decide_channel_action` ladder (channel below
    LOW): rebalance ONLY when not structural AND an overfull source exists → else
    inbound_discount → flag_structural once the discount has defended for
    `INBOUND_DEFENSE_WINDOW_DAYS` past `structural_flag_ts` without recovery
    (organic demand absent → capital decision: splice/close, not a fee problem);
    optional inbound_charge on overfull heavy sinks is off (`INBOUND_CHARGE_PPM=0`).
    With `INBOUND_FEE_ENABLED=False` the ladder emits no inbound fee and non-rebalance
    depleted channels collapse to "none", but the Layer-1 rebalance/skip grind-stop
    is unchanged. Inbound + outbound set in one `/v1/chanpolicy` POST; always send
    explicit inbound when enabled (LND 0.20 resets an omitted `inbound_fee`).
  No PROVEN/DISCOVERY/DEADWEIGHT tiers, no median/window smoothing. `channel_signals`
  holds `market_multiplier`, `last_fee_update_ts`, `last_local_ratio`,
  `signals_updated_ts`, plus (migration `_migrate_channel_signals_v2`)
  `floor_decay_anchor_ppm`, `floor_decay_started_ts`, `structural_flag_ts`,
  `inbound_fee_ppm`, `inbound_fee_set_ts`; `fee_updates` gained `new_inbound_ppm`.
  Knobs: `REBALANCE_DEFAULT_BUDGET_PPM`, `REBALANCE_MAX_BUDGET_PPM`,
  `REBALANCE_BUDGET_ESCALATION_STEP`, `REBALANCE_FEE_MARGIN`, `EARNED_PPM_WINDOW_DAYS`,
  `EARNED_PPM_MIN_VOLUME_SATS`, `EARNED_PPM_MAX_LOOKBACK_DAYS` (earned-ppm window
  widens 21→42→84→90d before declaring unjudged — kills the "unjudged cliff" where a
  quiet profit-capped channel re-entered planning at full escalation; rebalance
  FAILURES expire on the same 90d clock — capped failures only ever tested the cap
  price, so a re-entering channel resumes at last_refill × 1.0 instead of a
  phantom-inflated bid, and a persistent structural flag re-probes ~quarterly), `REBALANCE_PROFIT_HORIZON`,
  `REBALANCE_STRUCTURAL_FAIL_THRESHOLD`, `SIGMOID_MAX_PPM`, `FLOOR_DECAY_*`,
  `MARKET_MULT_FASTDRAIN_STEP`, `INBOUND_*`.

## LND Access
- REST: https://127.0.0.1:9000
- Cert: /home/lnd/tls.cert
- Macaroons (baked with `lncli bakemacaroon`, stored in ~/.lnd-macaroons/, mode 600):
  - Main tool (`LND_MACAROON` in .env): least-privilege —
    `info:read offchain:read offchain:write onchain:read address:write invoices:read invoices:write peers:read`.
    No onchain:write / open / close / macaroon admin.
  - Dashboard (`DASHBOARD_LND_MACAROON`) and daily-check (`DAILY_CHECK_LND_MACAROON`):
    read-only — `info:read offchain:read onchain:read peers:read invoices:read`.
  - admin.macaroon is only needed to bake the above; not used at runtime.
- LND runs as the `lnd` user; the tool runs as the operator user (a non-root,
  non-`lnd` login — `youruser` in the shipped systemd units, swapped at install)

## Database
- SQLite at `<repo>/ln_operator.db` (derived from file location in config.py /
  dashboard; override with `LN_OPERATOR_DB`)
- forwarding_log and rebalance_log store numeric scid as chan_id
- forward_fail_log: forwards we DROPPED (insufficient liquidity, fee too low, etc.),
  captured live by the htlc_monitor daemon. LND persists these nowhere — the event
  stream is live-only with no replay — so any daemon downtime is a data gap, not a
  clean day. chan_out = the channel that lacked capacity; failure_detail
  INSUFFICIENT_BALANCE = lost revenue + rebalance signal.
- rebalance_log has payment_hash, triggered_by, budget_ppm and run_id columns
  (migrations added). run_id groups all plans executed in one pipeline run so
  failure counting is per-cycle, not per-attempt (see Layer-1 note above).
- fee_overrides table: chan_id (PK), pinned_ppm, set_at, note — manual fee pins

## Services
- Dashboard: systemd lnd-dashboard.service, port 4000. Unit file at services/lnd-dashboard.service.
- Channel backup: systemd lnd-channel-backup.path (inotify on channel.backup) +
  lnd-channel-backup.timer (2h heartbeat), both triggering lnd-channel-backup@{path,timer}.service.
  Unit files at services/lnd-channel-backup.{path,timer,@.service}.
  Destination configured via BACKUP_* keys in .env. Attempts logged in backup_log table;
  dashboard shows freshness badge.
- HTLC failure monitor: systemd lnd-htlc-monitor.service (always-on, Restart=always).
  Subscribes to LND's SubscribeHtlcEvents stream and records dropped forwards into
  forward_fail_log. Unit file at services/lnd-htlc-monitor.service. Run loop in
  htlc_monitor.py, entrypoint `ln-operator monitor_htlcs`.
- Pipeline: cron every 2 hours

## Crontab
0 */2 * * * cd /home/youruser/ln-operator && ./ln-operator pipeline 2>&1
15 3 * * * cd /home/youruser/ln-operator && ./ln-operator recompute_signals >> logs/signals.log 2>&1
