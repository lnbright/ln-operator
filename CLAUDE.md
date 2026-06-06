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
- **`last_refill_ppm` budget + fee floor, now profitability-gated (3 layers)**:
  `last_refill_ppm` (most recent successful rebalance into a channel) still
  anchors BOTH the rebalance budget and the outbound fee floor. Base budget =
  `last_refill × (1 + 0.20 × failures_since_last_success)`, capped at 5000;
  bootstrap `REBALANCE_DEFAULT_BUDGET_PPM = 500`. On top of that:
  - **Layer 1 — profitability gate** (`get_channel_rebalance_budget`): for
    channels with enough trailing OUT-volume to JUDGE (≥ `EARNED_PPM_MIN_VOLUME_SATS`),
    the budget is also capped at `earned_ppm × REBALANCE_PROFIT_HORIZON` — never
    pay more to refill than the channel earns back in ~horizon cycles. UNJUDGED
    channels (`db.get_channel_earned_ppm` returns None) keep full escalation
    untouched (capping them would kill price discovery). Budget dict carries
    `earned_ppm`, `profit_capped`, `structural`. `plan_rebalances` drops targets
    whose ladder verdict ≠ `rebalance`, so structural channels stop being ground.
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
    off by default `INBOUND_FEE_ENABLED=False`): a depleted channel that can't be
    profitably rebalanced is defended with a NEGATIVE inbound fee (discount) to pull
    organic refill — a rescue subsidy tapering to 0 by `INBOUND_DISCOUNT_CLEAR_RATIO`
    (out of danger, not full target), capped at `our_outbound − safety_margin`.
    `decide_channel_action` ladder: rebalance → inbound_discount → flag_structural
    (alerts, capital decision) → optional inbound_charge on heavy sinks. Inbound +
    outbound set in one `/v1/chanpolicy` POST; always send explicit inbound when
    enabled (LND 0.20 resets an omitted `inbound_fee`).
  No PROVEN/DISCOVERY/DEADWEIGHT tiers, no median/window smoothing. `channel_signals`
  holds `market_multiplier`, `last_fee_update_ts`, `last_local_ratio`,
  `signals_updated_ts`, plus (migration `_migrate_channel_signals_v2`)
  `floor_decay_anchor_ppm`, `floor_decay_started_ts`, `structural_flag_ts`,
  `inbound_fee_ppm`, `inbound_fee_set_ts`; `fee_updates` gained `new_inbound_ppm`.
  Knobs: `REBALANCE_DEFAULT_BUDGET_PPM`, `REBALANCE_MAX_BUDGET_PPM`,
  `REBALANCE_BUDGET_ESCALATION_STEP`, `REBALANCE_FEE_MARGIN`, `EARNED_PPM_WINDOW_DAYS`,
  `EARNED_PPM_MIN_VOLUME_SATS`, `EARNED_PPM_MAX_LOOKBACK_DAYS` (earned-ppm window
  widens 21→42→84→90d before declaring unjudged — kills the "unjudged cliff" where a
  quiet profit-capped channel re-entered planning at full escalation), `REBALANCE_PROFIT_HORIZON`,
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
- LND runs as lnd user, tool runs as pi user

## Database
- SQLite at /home/pi/ln-operator/ln_operator.db
- forwarding_log and rebalance_log store numeric scid as chan_id
- forward_fail_log: forwards we DROPPED (insufficient liquidity, fee too low, etc.),
  captured live by the htlc_monitor daemon. LND persists these nowhere — the event
  stream is live-only with no replay — so any daemon downtime is a data gap, not a
  clean day. chan_out = the channel that lacked capacity; failure_detail
  INSUFFICIENT_BALANCE = lost revenue + rebalance signal.
- rebalance_log has payment_hash and triggered_by columns (migration added)
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
0 */2 * * * cd /home/pi/ln-operator && ./ln-operator pipeline 2>&1
15 3 * * * cd /home/pi/ln-operator && ./ln-operator recompute_signals >> logs/signals.log 2>&1
