# Known Limitations

- **Local graph capacity** is unreliable for distant nodes. Channel count is reliable.
- **Inbound fees** not visible in local graph — check Amboss before opening channels.
- **The graph cache is liquidity-blind** — announced topology + fee policy only, so
  `suggest_peers` and `plan` produce a ranked *shortlist for a human*, not a precise
  cost. Real routability is validated separately via QueryRoutes (mission-control
  liquidity); announced fees are never scored. See [graph-cache.md](graph-cache.md).
- **Graph cache is refreshed daily, not live** — `refresh_graph` runs nightly, so the
  cached topology can be up to a day stale. Fine for slowly-changing structure; run
  `refresh_graph` manually if you need it current before a capital decision.
- **Dashboard has no auth** — bind it to a Tailscale/LAN-only IP via
  `DASHBOARD_BIND_IP` (defaults to `127.0.0.1`); never bind to `0.0.0.0` on a
  WAN. For exposure beyond a tailnet, front it with a reverse proxy that adds
  auth — see the README's Security section.
- **Channel opens are manual** — plan recommends, you execute via `lncli`.
