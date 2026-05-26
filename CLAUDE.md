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
- agent.py exists but plan command doesn't use it (pure local graph)
- Rebalance auto-chunks on failure (halves down to 100k min). Each successful
  chunk is its own success row in rebalance_log at its actual ppm.
- Fallback pairs: if source→target fails, tries source→alternative target
- Manual fee pins (`fee_overrides` table) suppress auto-fees per channel.
  `engine.update_all_fees` checks the table first; pinned channels use the
  stored ppm with reason `manual pin: N ppm` instead of `calculate_fee_ppm`.
  Set via `main.py set_fee`, cleared via `main.py clear_fee`, shown by `status`.
- **Single-signal budget + fee floor (no tiers)**: `last_refill_ppm` (most
  recent successful rebalance into a channel) drives BOTH the rebalance
  budget AND the outbound fee floor. Budget = `last_refill ×
  (1 + 0.20 × failures_since_last_success)`, capped at 5000.
  Outbound floor = `last_refill × 1.3`. Bootstrap from
  `REBALANCE_DEFAULT_BUDGET_PPM = 500` when no history; failure escalation
  handles both bootstrap and upward market drift. No PROVEN/DISCOVERY/DEADWEIGHT
  tiers, no `earned_ppm × revenue_ratio`, no median/window smoothing,
  no `adaptive_cap_ppm` or `rebalance_cost_floor_ppm` columns (those
  channel_signals columns are leftover and unused — kept to avoid migration).
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
- rebalance_log has payment_hash and triggered_by columns (migration added)
- fee_overrides table: chan_id (PK), pinned_ppm, set_at, note — manual fee pins

## Services
- Dashboard: systemd lnd-dashboard.service, port 4000
- Channel backup: systemd lnd-channel-backup.path (inotify on channel.backup) +
  lnd-channel-backup.timer (2h heartbeat), both triggering lnd-channel-backup@{path,timer}.service.
  Destination configured via BACKUP_* keys in .env. Attempts logged in backup_log table;
  dashboard shows freshness badge.
- Pipeline: cron every 2 hours

## Crontab
0 */2 * * * cd /home/pi/ln-operator && venv/bin/python3 main.py pipeline 2>&1
15 3 * * * cd /home/pi/ln-operator && venv/bin/python3 main.py recompute_signals >> logs/signals.log 2>&1
