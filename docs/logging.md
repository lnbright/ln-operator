# Logging

```bash
tail -f logs/ln_operator.log    # rotating, 5MB × 5 backups
```

## Console vs file level

The file handler captures everything at `DEBUG`. The terminal (console) handler
is `WARNING`-only by default, so the 2h cron stays quiet and only surfaces
problems. To watch a run's full detail without the terminal, `tail -f` the log
file above.

The interactive `manual_rebalance` command is the exception: it bumps the
console to `INFO` for the duration of the run (via
`logging_config.set_console_level`) so the operator sees the run narrated live
in the terminal. The file handler is unaffected.

While the `/v2/router/send` stream tests routes, `manual_rebalance` prints a
lightweight `testing paths ....` line — one dot per route the router probes —
so a long search shows progress instead of a silent gap. The noisy per-HTLC
detail (hop count, quoted fee, elapsed-vs-timeout) is kept at DEBUG in the file
only. Driven by an `on_probe(event)` callback (`start`/`tick`/`end`); the cron
path passes none and stays silent.
