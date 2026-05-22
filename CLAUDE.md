# LN Operator — Project Context

## Infrastructure
- operator-host (10.0.0.1): Bitcoin Core + LND + LN Operator + dashboards
- backup-host (10.0.0.2): Backup target for channel.backup
- Node pubkey: NODE_PUBKEY
- Node alias: example.node

## Current Channels
- ACINQ: opened 2023, 11.8M capacity
- Kraken: 3.25M capacity
- LNBiG Hub-3: 3.39M capacity, hard to rebalance (high intermediate fees ~3000ppm)

## Key Design Decisions
- REST API chan_id from LND = numeric scid (not hex) — DB stores this format
- Rebalance costs attributed to target channel only (not source)
- Manual rebalances synced from LND payments by detecting circular self-payments
- Channel open time used as floor to prevent old payment misattribution
- Fee scoring removed from candidates — local graph fee data too unreliable
- agent.py exists but plan command doesn't use it (pure local graph)
- Rebalance auto-chunks on failure (halves down to 100k min)
- Fallback pairs: if source→target fails, tries source→alternative target

## LND Access
- REST: https://127.0.0.1:9000
- Cert: /home/lnd/tls.cert  
- Macaroon: /home/lnd/data/chain/bitcoin/mainnet/admin.macaroon
- LND runs as lnd user, tool runs as pi user

## Database
- SQLite at /home/pi/ln-operator/ln_operator.db
- forwarding_log and rebalance_log store numeric scid as chan_id
- rebalance_log has payment_hash and triggered_by columns (migration added)

## Services
- Dashboard: systemd lnd-dashboard.service, port 4000
- Channel backup: systemd lnd-channel-backup.path (inotify on channel.backup) +
  lnd-channel-backup.timer (2h heartbeat), both triggering lnd-channel-backup@{path,timer}.service.
  Destination configured via BACKUP_* keys in .env. Attempts logged in backup_log table;
  dashboard shows freshness badge.
- Pipeline: cron every 2 hours

## Crontab
0 */2 * * * cd /home/pi/ln-operator && venv/bin/python3 main.py pipeline 2>&1
