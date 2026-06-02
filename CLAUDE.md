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
- Manual fee pins (`fee_overrides` table) suppress auto-fees per channel.
  `engine.update_all_fees` checks the table first; pinned channels use the
  stored ppm with reason `manual pin: N ppm` instead of `calculate_fee_ppm`.
  Set via `ln-operator overwrite_fee`, cleared via `ln-operator clear_fee`, shown by `status`.
- **Single-signal budget + fee floor (no tiers)**: `last_refill_ppm` (most
  recent successful rebalance into a channel) drives BOTH the rebalance
  budget AND the outbound fee floor. Budget = `last_refill ×
  (1 + 0.20 × failures_since_last_success)`, capped at 5000.
  Outbound floor = `last_refill × REBALANCE_FEE_MARGIN`. Bootstrap from
  `REBALANCE_DEFAULT_BUDGET_PPM = 500` when no history; failure escalation
  handles both bootstrap and upward market drift. No PROVEN/DISCOVERY/DEADWEIGHT
  tiers, no `earned_ppm × revenue_ratio`, no median/window smoothing. The old
  `adaptive_cap_*` / `rebalance_cost_floor_*` channel_signals columns were
  dropped (db.py `_migrate_drop_unused_columns`); channel_signals now holds
  only `market_multiplier`, `last_fee_update_ts`, `last_local_ratio`,
  `signals_updated_ts`.
  Knobs: `REBALANCE_DEFAULT_BUDGET_PPM`, `REBALANCE_MAX_BUDGET_PPM`,
  `REBALANCE_BUDGET_ESCALATION_STEP`, `REBALANCE_FEE_MARGIN`.

## LND Access
- REST: https://127.0.0.1:9000
- Cert: /home/lnd/tls.cert  
- Macaroon: /home/lnd/data/chain/bitcoin/mainnet/admin.macaroon
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
