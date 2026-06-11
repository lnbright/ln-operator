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
`logging_config.set_console_level`) so the operator sees each chunk attempt, the
landing channel, and the final summary stream live in the terminal. The file
handler is unaffected.
