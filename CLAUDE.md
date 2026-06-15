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
  `SHOW_PER_TIER` (10) per tier are surfaced. Both stages now read the cached graph
  (`graph_cache.load()` → `_candidates_from_digest` / `_enrich_from_digest`):
  no live `describe_graph` pull and no per-candidate `get_node_info` round-trips —
  the digest's adjacency serves diversity directly. Live pull/`get_node_info` stay
  as a fallback only when the cache is absent (pre-first-`refresh_graph`). Runs only
  in `ln-operator plan`, never in the 2h pipeline. Constants live in `advisor.py`.
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
    channels with enough trailing OUT-volume to CALIBRATE (≥ `EARNED_PPM_MIN_VOLUME_SATS`),
    the budget is also capped at `earned_ppm × REBALANCE_PROFIT_HORIZON` — never
    pay more to refill than the channel earns back in ~horizon cycles. CALIBRATING
    channels (`db.get_channel_earned_ppm` returns None) keep full escalation
    untouched (capping them would kill price discovery). Budget dict carries
    `earned_ppm`, `profit_capped`, `structural`, `accelerated`. `plan_rebalances`
    drops targets whose ladder verdict ≠ `rebalance`, so structural channels stop
    being ground. **Earn-ceiling accelerator**: a CALIBRATED channel whose anchor sits
    far below its earnings — e.g. one lucky-cheap refill pins `last_refill` to
    7 ppm on a channel earning 576 — would crawl `7→8→10→11→14…` and never route,
    sitting depleted with no alarm (it's profitable, so not structural). Instead
    each failed run closes `REBALANCE_BUDGET_ESCALATION_STEP` of the gap between
    the anchor and the affordable ceiling (`min(earned×horizon, MAX)`), reaching
    it in `1/STEP` (=5) runs (bfx 14→721). Reuses STEP (no new knob); only RAISES
    the budget (a `max()`); calibrated-only; climbs only UP TO the ceiling. Because
    rounding `gap_climb` up can land one ppm above a fractional `profit_cap`,
    `profit_capped` is measured against PLAIN escalation, not the accelerated
    value — so the accelerator can never spuriously strand the channel it rescues
    (accelerator-firing and `profit_capped` are mutually exclusive). Inert unless
    `earned×horizon > 2×anchor`; self-limiting (first success re-anchors).
    **Relationship to the QueryRoutes probe (below): the earn-ceiling accelerator is
    now its BLIND FALLBACK** — the probe reads the real clearing price via QueryRoutes
    and is the
    informed primary; this accelerator is the QueryRoutes-independent climb that
    still escapes a poisoned anchor when the probe can't read a price (off/unavailable).
    Both only raise toward the same ceiling via `max()`, so the higher of (blind
    climb, informed price) wins.
  - **Unified QueryRoutes probe** (`REBALANCE_QUERYROUTES_ENABLED` /
    `REBALANCE_QUERYROUTES_EARLYOUT_ENABLED`, `_queryroutes_probe` in
    `rebalance_planner.py`): ONE `lnd_client.query_routes` dry-run (no payment, same
    pathfinder + mission control as `SendPaymentV2`) **per overfull SOURCE** for each
    CALIBRATED depleted target, at the MINIMUM chunk (`REBALANCE_QUERYROUTES_MIN_CHUNK_SATS`)
    capped at the affordable ceiling (`affordable_ceiling_ppm` = `min(profit_cap, MAX)`
    calibrated / `MAX` calibrating). That single set of probes drives BOTH halves, and the
    sources are ranked cheapest-first on the way out so the executor pays the cheapest.
    **Each source's cost = the probe's end-to-end `fee_ppm` PLUS the target peer's fee
    to forward the final hop into our channel** (`_target_inbound_ppm`): the probe routes
    to `dest=target_peer`, so LND charges nothing for the hop into it (free destination
    hop), but the real circular rebalance (`us→source→…→target_peer→us`) pays that peer's
    outbound fee on the target channel — so it's added back via one `get_channel_edge`
    lookup (target peer's own policy, base amortised over the chunk), and the probe's
    `fee_limit` is shrunk by it so route+final-hop stays ≤ ceiling. This is the SAME
    omission peer_finder fixes with `_first_hop_ppm`, but on the LAST hop — without it
    direct neighbours read a deceptively low/0 ppm (their inbound fee invisible).
    - **pricing** (was "v1"): price the bid off the CHEAPEST feasible source — raise
      `max_fee_ppm` up to its live cost (bounded by the ceiling), so an affordable
      refill lands now AND via the cheapest source, instead of the ~5-run grind (bfx
      14→721) or paying the most-overfull source. Only ever RAISES (a `max()`), never
      above the ceiling (never overpays).
    - **early-out** (was "v2"): if EVERY source returns a definite no-route, refilling
      is a capital problem, not price discovery → drop the channel from planning AND,
      on a real run only, record a synthetic failed cycle (`QR_NO_AFFORDABLE_ROUTE`,
      fee 0, own `run_id`) that advances `count_failures_since_last_success` to the
      structural threshold — **the early-out replaces the wasted attempts, NOT the
      stranding they'd eventually trigger** (silently skipping would leave it in limbo,
      never flagged). Gated by `REBALANCE_QUERYROUTES_EARLYOUT_ENABLED`; off → the
      probe still prices/ranks but never strands.
    **Why probe every source, not just the most-overfull** (this superseded the old
    single-source v1/v2): feasibility is EXISTENTIAL (one working source proves it,
    and a cheaper source may exist) but infeasibility is UNIVERSAL (only ALL sources
    failing justifies the drop — and the drop is consequential, it advances stranding).
    A single source's no-route can't price the bid *or* strand the channel. **Why the
    min chunk for pricing too**: ppm is amount-dependent (a fixed base fee amortises
    over fewer sats), so the 100k price is the worst case → a safe upper bound for the
    cap that still lets larger/whole-amount routes settle under it, and one probe
    covers chunked refills a full-amount probe would miss (so no separate full-amount
    "v1" probe is needed). Safety rails: CALIBRATED-only in AUTO (`earned_ppm is None` → no
    probe; force probes calibrating for diagnostics only — see below);
    a probe that's UNAVAILABLE (LND down) is UNKNOWN, never no-route, so a transport
    blip can never strand; never moves `last_refill` (only a real success does); runs
    ONLY in the planner (`get_channel_rebalance_budget` stays call-free). **`force`
    (the `rebalance_channels --force <ratio>` operator command) runs the probe in
    DIAGNOSTIC mode** (`_queryroutes_probe(..., force=True)`): it still prices the bid
    + ranks sources cheapest-first AND probes CALIBRATING channels too (auto skips them),
    but NEVER strands (`drop` always False) and NEVER records a synthetic cycle — the
    operator is explicitly overriding the profit/structural gate, so the probe is for
    VISIBILITY, not gating. Force looks at ALL depleted channels — calibrating,
    calibrated AND stranded (the planner stamps each target's `target_state` from the
    budget dict: `structural` → stranded, `earned_ppm` present → calibrated, else
    calibrating). `cmd_rebalance_channels` prints the per-source intel
    (`probe_results`: status route/no_route/unavailable + clearing ppm, each target
    tagged `[state]`) before moving sats (`_show_queryroutes_intel`). **If EVERY probe
    that ran came back no-route** (`_any_feasible_route` False), it asks the operator
    `Try the rebalance anyway? [y/N]` with a 30s timeout defaulting to No
    (`_prompt_proceed_with_timeout`; non-interactive stdin → instant default No, never
    blocks cron). No / timeout → print the `manual_rebalance` commands and skip the
    auto-attempt entirely; Yes → attempt anyway. **Either way**, after a forced run any
    target that landed zero sats gets a ready-to-paste `manual_rebalance` command
    (`_print_manual_rebalance_hints`, pre-filled with the cheapest probed source + its
    observed ppm, falling back to the primary source + bid). Up to
    (#sources × #depleted targets) dry-run probes per run — cheap, both counts small.
    Returns `{drop, budget, source_order, probe_results}`; the planner threads
    `source_order` into both the primary and fallback plan loops and stashes
    `probe_results` + `target_state` on every plan dict.
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
  widens 21→42→84→90d before declaring calibrating — kills the "calibrating cliff" where a
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
- daily_findings table: key (PK), kind, entity, state, summary, first_seen,
  last_seen, status — deterministic dedup memory for the daily-check agent so it
  reports a finding once, re-surfaces only on a material `state` change, and notes
  resolution once. `db.reconcile_findings(current)` diffs the agent's current-run
  findings against the open set and returns new/changed/unchanged/resolved buckets
  (persisting the snapshot); a resolved key that reappears reopens as a fresh
  episode. Replaces the old "read the log and dedup by assessment" instruction.
- reconcile.py: `run_checks(window_days)` — the §2 data-integrity arithmetic the
  daily-check agent used to do by hand (an LLM gets SQLite arithmetic subtly wrong),
  now deterministic DB-only assertions returning [{check, severity, message}]. Covers
  ONLY runtime-only failure modes (things a unit test can't reproduce): missing
  payment_hash on a success row, fee_ppm > REBALANCE_MAX_BUDGET_PPM, **fee_ppm > the
  row's recorded `budget_ppm` ×1.1** (both = LND ignored the fee_limit), duplicate
  payment_hash, chunk-ppm spikes. The fee>budget check is an INVARIANT against the stored
  `budget_ppm` (the max_fee_ppm actually used), NOT a re-derivation of
  `get_channel_rebalance_budget`'s multi-layer math — mirroring engine internals in a
  checker false-positives the way a table-only hysteresis check would. Deliberately does
  NOT check **pure-logic invariants on our own code** — those are caught earlier by unit
  tests, so a runtime re-assertion adds nothing: budget ≤ REBALANCE_MAX_BUDGET_PPM (the
  clamp in `get_channel_rebalance_budget`; engine tests test_capped_at_max_budget /
  test_accelerator_never_exceeds_max_budget) and a pinned channel broadcasting exactly
  its pin (`update_all_fees`; engine test FeePinBroadcastTests) were REMOVED from
  reconcile once those tests existed. Also does NOT check the fee hysteresis rule (its
  cooldown escapes — snap Δ / edge-zone crossing — depend on engine state absent from
  `fee_updates`, so a table-only check false-positives on every legitimate floor-decay
  broadcast). The
  agent now does deep §2 analysis ONLY when `run_checks` reports a failure (clean → one
  line, no hand-arithmetic).
- closed_channels table: one row per closed channel (scid PK + remote_pubkey,
  alias, capacity, settled_balance, close_type, close_initiator, close_height,
  closing_tx_hash). A closed channel vanishes from LND's live channel list, so the
  dashboard flow / routing-events tables lost its alias and showed a raw scid;
  this snapshots scid→alias permanently so they render "LNBiG [Hub-3] (closed)"
  instead (`db.get_closed_channel_aliases()`, merged as the fallback BELOW the live
  alias in `dashboard/app.py`). Populated by pipeline Step 5
  (`main.detect_closed_channels`): pulls `/v1/channels/closed`, resolves each peer's
  alias via `get_node_info`, upserts via `db.record_closed_channel` (returns new?).
  A newly-detected close with `close_initiator == INITIATOR_REMOTE` (the peer closed
  on us) raises a `channel_closed_by_peer` alert. **First run SEEDS silently** — an
  empty table means every historical close would alert at once, so seeding only
  records; alerts fire only for closes detected after seeding.
- graph_snapshots table: one row per `refresh_graph` run — total_nodes/channels/
  capacity + our_channels/capacity/peers. Historical, so our network position
  (growing / going dark) is trendable. Finally given a writer (`db.save_graph_snapshot`).

## Graph cache
- `graph_cache.py` — `describe_graph()` is a multi-MB pull (26k nodes / 98k channels
  / ~16MB / ~30s on the Pi), so `ln-operator refresh_graph` (daily cron, 03:30) pulls
  it ONCE and writes a compact processed digest to `graph_cache.json` (next to the DB;
  gitignored; path overridable via `LN_OPERATOR_GRAPH_CACHE`). Atomic write (tmp +
  os.replace). The daily-check agent and the peer-finder call `graph_cache.load()`
  (instant) instead of re-pulling LND.
- Digest = per-node {alias, channels, capacity, avg_fee_ppm, neighbors[]} for every
  node with ≥1 public channel, plus our 2-hop reachable set and network stats.
  `neighbors` is the adjacency the peer-finder walks for reachability / "if I opened to Y, what
  does Y reach". `build_digest()` is pure (graph dict → digest), unit-tested without LND.
- LIQUIDITY-BLIND by design: announced topology + fee policy only, NEVER costed
  pathfinding (that's QueryRoutes, which sees real mission-control liquidity).
- Full re-pull each refresh (no incremental diff). describe_graph is read-only, run
  once daily off-peak — the same call `lncli describegraph` makes; not a hot path, no
  meaningful LND load. (A true incremental graph would use SubscribeChannelGraph's
  gossip stream, but that's an unjustified long-running daemon for a slowly-changing,
  liquidity-blind structural cache that only needs daily freshness.)

## Targeted peer-finder
- `peer_finder.py` / `ln-operator suggest_peers <alias|pubkey>` — turns "add a 2nd
  source" into NAMED, validated candidates for "which node to open toward so refills
  into this sink get cheaper". Two stages:
  - **Stage 1 (graph cache, free, liquidity-blind):** the target's neighbours (a
    channel to one gives a short us→Y→target refill path), minus self / existing peers
    / sub-`MIN_CANDIDATE_CHANNELS` noise, scored by hub quality (channels + capacity,
    low fee). `_stage1_candidates` is pure, unit-tested. Reports `diversity` = fraction
    of the candidate's neighbours OUTSIDE our 2-hop horizon (does opening to it expand
    reach or duplicate paths).
  - **Stage 2 (QueryRoutes, live, real liquidity):** for each finalist Y, price the REAL
    refill shape — `query_routes(dest=us, source_pubkey=Y, last_hop_pubkey=target)` —
    i.e. the route `Y → … → target → us` a refill takes AFTER we open us→Y (the first
    hop us→Y is our own near-free new channel, excluded since Y is the source). The
    target is an INTERMEDIATE forwarder that charges its fee, NOT the destination.
    **Earlier `query_routes(dest=target, source=Y)` was wrong**: it terminated the route
    AT the sink, so the final hop into the sink was free (destination doesn't charge) and
    every direct neighbour read a meaningless `0ppm/1h` — the ranking then fell entirely
    to the reach tiebreak and put the most EXPENSIVE candidates on top (e.g. WoS, whose
    own 5000ppm fee toward bfx was completely hidden). **First-hop fix**: `SendPaymentV2`/
    QueryRoutes never charge the SOURCE for its own outbound, so `probe.fee_ppm` covers
    every hop AFTER Y but omits Y's own first hop — which Y *does* pay once it's an
    intermediate post-open. `_first_hop_ppm` adds it back via one `get_channel_edge(hops[0])`
    lookup (a node's OWN policy — node1_policy if it is node1, else node2_policy — is its
    outbound fee, so direction is unambiguous), giving the TRUE end-to-end refill ppm for
    any path length; surfaced as `(peer-hop N)`. Degrades to probe-only cost on lookup
    failure / disabled edge. Drops candidates with no live route (announced-but-dead),
    ranks by full validated cost. An EMPTY result is the verdict: no cheap live route
    exists → resize/close, not open. `_policy_ppm` amortises base fee over the probe size.
- The daily-check agent calls `suggest_peers_for(<sink peer pubkey>)` to name peers in
  its §4 capital suggestions instead of hand-waving "add a source". `source_pubkey` is
  why the cache + peer-finder beat re-running the slow `plan` graph walk: cache narrows broad+free, then
  ~12 live probes validate — `plan` can't simulate a not-yet-open channel's path.

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
30 3 * * * cd /home/youruser/ln-operator && ./ln-operator refresh_graph >> logs/graph.log 2>&1
