# Known Limitations

- **Local graph capacity** is unreliable for distant nodes. Channel count is reliable.
- **Inbound fees** not visible in local graph — check Amboss before opening channels.
- **Dashboard has no auth** — bind it to a Tailscale/LAN-only IP via
  `DASHBOARD_BIND_IP` (defaults to `127.0.0.1`); never bind to `0.0.0.0` on a
  WAN. For exposure beyond a tailnet, front it with a reverse proxy that adds
  auth — see the README's Security section.
- **Channel opens are manual** — plan recommends, you execute via `lncli`.
