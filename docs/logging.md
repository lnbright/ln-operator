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

The `/v2/router/send` stream is narrated per-HTLC: as LND dispatches and
resolves each route attempt it logs `htlc N: probing route via H hop(s), quoted
fee F sats [+Es/Ts]` and, on resolution, the failure code and which hop failed
(or `settled ✓`). The `[+Es/Ts]` tag is elapsed-vs-timeout seconds, so a long
route search shows progress against the expiry instead of a silent gap. These
lines log on every rebalance (so the file always has them); only the manual
command surfaces them on the console.
