"""
LN Operator — Agent Layer (10%)

Uses the Anthropic Claude API for the judgement calls that pure Python
formulas can't handle well — like "is this peer actually a good choice?"
or "should I worry about concentrating in one corridor?"

How it works:
- The Python engine (advisor.py) builds the full investment plan as a structured dict
- This module sends a COMPACT summary of that plan to Claude (not raw API dumps)
- Claude uses web search to research recommended peers before responding
- Claude returns a plain-English analysis with findings from the web
- If the API key is missing or the call fails, a fallback summary is generated
  locally in Python — the tool works fine without the API, just less nuanced

Web search:
- Enabled via the web_search_20250305 tool in the API call
- The agent is instructed to search for each recommended peer by name/pubkey
- It looks for: node reputation, uptime history, community feedback, recent activity
- This turns the agent from a summariser into an active researcher

Also supports follow-up questions: after seeing the plan, the operator can
ask "why this peer?" or "find me alternatives" and the agent searches + answers.
"""

import json
import requests
from config import ANTHROPIC_API_KEY, ANTHROPIC_MODEL
import config as _config
from logging_config import get_logger

log = get_logger("agent")


# ─── System prompt ────────────────────────────────────────────────

AGENT_SYSTEM_PROMPT = """You are a Lightning Network node advisor. Be concise and direct.

You receive a shortlist of 10 candidate nodes from the Python engine. Your job:

1. Search Amboss and 1ML to find real avg channel size for each candidate.
   Use ONE search per candidate: search "{alias} site:amboss.space OR site:1ml.com"
   or search all 10 aliases in 2-3 batched searches to be efficient.

2. Cross-check: if Amboss and 1ML agree on capacity → use it. If they differ → use lower.

3. Recommend the top 3 by combining engine score (topology/diversity) with
   real avg channel size (quality). Higher avg channel size = better routing partner.

4. Also suggest the final budget allocation: given the deployable sats and
   minimum channel size shown in the plan below, how many channels and at what size?

Output — plain prose only, zero markdown, no asterisks, no bold, no dashes, no headers.
Three lines maximum:
Line 1: "Open [name1] (avg X sats/ch, [one reason]) and [name2] (avg X sats/ch, [one reason])."
Line 2: "[X] sats each from the [Y] sats deployable. [Disqualify unsuitable candidates in one clause if needed.]"
Line 3: "Fees at X sat/vB — [good/bad] timing."
Note: the treasury and anchor reserves are already deducted — do not mention them again.
Max 60 words total. Use sats not BTC."""


# ─── Agentic call with web search ────────────────────────────────

def _run_agentic_call(system_prompt, user_message, max_tokens=2000, max_turns=5):
    """Run a Claude API call with web search enabled.

    Web search turns this into an agentic loop:
    1. We send the message to Claude
    2. Claude responds with tool_use blocks (search requests)
    3. We send tool_result blocks back
    4. Claude searches, gets results, may search again
    5. When Claude stops with stop_reason="end_turn" it's done

    The actual web searches happen on Anthropic's infrastructure — we just
    relay the tool_use/tool_result turns. max_turns prevents infinite loops.

    Handles the agentic loop: Claude may make multiple web search tool calls
    before producing a final text response. We keep sending tool results back
    until Claude returns a response with no more tool_use blocks.

    Returns the final text response, or None on failure.
    """
    messages = [{"role": "user", "content": user_message}]

    for turn in range(max_turns):
        response = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "Content-Type": "application/json",
                "x-api-key": ANTHROPIC_API_KEY,
                "anthropic-version": "2023-06-01",
            },
            json={
                "model": ANTHROPIC_MODEL,
                "max_tokens": max_tokens,
                "system": system_prompt,
                "tools": [
                    {
                        "type": "web_search_20260209",
                        "name": "web_search",
                    }
                ],
                "messages": messages,
            },
            timeout=60,
        )
        if not response.ok:
            log.error("Claude API error %d: %s", response.status_code, response.text[:500])
        response.raise_for_status()
        data = response.json()

        content_blocks = data.get("content", [])
        stop_reason = data.get("stop_reason", "")

        log.debug("agent turn %d: stop_reason=%s, blocks=%d",
                  turn + 1, stop_reason, len(content_blocks))

        # Append assistant response to message history
        messages.append({"role": "assistant", "content": content_blocks})

        # If Claude is done (no more tool calls), extract the final text.
        # Use only the LAST text block — intermediate thinking text appears in
        # earlier blocks before tool calls; the final answer is the last one.
        if stop_reason == "end_turn":
            last_text = ""
            for block in content_blocks:
                if block.get("type") == "text" and block.get("text", "").strip():
                    last_text = block.get("text", "")
            return last_text.strip() if last_text else None

        # Web search is server-side — Anthropic handles it automatically.
        # server_tool_use and web_search_tool_result blocks appear in the response
        # but we don't need to send anything back. Just loop and call again
        # with the updated message history until we get end_turn.
        if stop_reason == "tool_use" or stop_reason == "end_turn":
            # Log any search queries for debugging
            for block in content_blocks:
                if block.get("type") == "server_tool_use":
                    q = block.get("input", {}).get("query", "")
                    if q:
                        log.debug("agent web search: %s", q)
            if stop_reason == "end_turn":
                # Already handled above — shouldn't reach here
                break
            # Continue the loop — next iteration will send the updated history
            continue

        # Unexpected stop reason
        log.warning("agent unexpected stop_reason: %s", stop_reason)
        break

    # If we exhausted turns, extract whatever text we have
    for block in content_blocks:
        if block.get("type") == "text" and block.get("text"):
            return block["text"].strip()
    return None


# ─── Public API ──────────────────────────────────────────────────

def get_investment_summary(plan):
    """Research recommended peers and produce a plain-English investment summary.

    Uses web search to look up each peer by name before giving advice.
    Falls back to a local summary if the API key is missing or call fails.
    """
    if not ANTHROPIC_API_KEY:
        return _fallback_summary(plan)

    compact = _build_compact_prompt(plan)

    try:
        log.info("agent: researching %d candidate(s) via web search",
                 len(plan.get("actions", [])))
        text = _run_agentic_call(AGENT_SYSTEM_PROMPT, compact, max_tokens=2000, max_turns=10)

        if text:
            log.info("agent summary received (%d chars)", len(text))
            return text
        else:
            log.warning("agent returned no text — using fallback")
            return _fallback_summary(plan)

    except Exception as e:
        log.error("Claude API call failed: %s", e)
        return _fallback_summary(plan)


def get_followup_answer(plan, question):
    """Answer a follow-up question about an investment plan.

    Web search enabled — the agent can search for more info to answer the question.
    Used for: "find better alternatives", "why this peer?", "what if I split?", etc.
    """
    if not ANTHROPIC_API_KEY:
        return "Claude API key not configured. Set ANTHROPIC_API_KEY to enable follow-up Q&A."

    compact = _build_compact_prompt(plan)
    user_message = f"Plan context:\n{compact}\n\nQuestion: {question}"

    try:
        text = _run_agentic_call(AGENT_SYSTEM_PROMPT, user_message, max_tokens=1500)
        return text or "No response from agent."
    except Exception as e:
        log.error("follow-up agent call failed: %s", e)
        return f"Could not reach Claude API: {e}"


# ─── Prompt builder ──────────────────────────────────────────────

def _build_compact_prompt(plan):
    """Build a token-efficient prompt from the plan.

    Includes enough context for the agent to research peers intelligently:
    - Which peers are recommended and why (so it knows what to search for)
    - Current node state (so it understands what kind of peer would help)
    - Fee environment and concerns
    """
    lines = []
    lines.append(f"Investment: {plan['total_sats']:,} sats")
    lines.append(f"Min channel size: {_config.MIN_CHANNEL_SIZE_SATS:,} sats | Preferred: {_config.PREFERRED_CHANNEL_SIZE_SATS:,} sats")
    lines.append(f"Anchor reserve: {_config.ANCHOR_RESERVE_PER_CHANNEL:,} sats per new anchor channel (max {_config.ANCHOR_RESERVE_MAX:,} sats total) — already deducted from treasury")
    lines.append(f"Treasury: {plan['treasury_reserve']:,} sats ({plan['treasury_pct']:.0%})")
    lines.append(f"Deployable: {plan['deployable_sats']:,} sats")
    lines.append("")

    state = plan.get("current_state", {})
    lines.append(
        f"Current node: {state.get('num_channels', 0)} channels, "
        f"{state.get('total_capacity', 0):,} sats capacity, "
        f"overall ratio {state.get('overall_ratio', 0):.0%}, "
        f"{state.get('num_active', 0)} active / {state.get('num_inactive', 0)} inactive"
    )
    lines.append("")

    fee_env = plan.get("fee_environment", {})
    if fee_env:
        lines.append(
            f"On-chain fees: {fee_env.get('fastest_fee', '?')} sat/vB "
            f"({fee_env.get('assessment', '?')}) — {fee_env.get('note', '')}"
        )
    lines.append("")

    # Channel issues
    analysis = plan.get("channel_analysis", {})
    if analysis.get("undersized"):
        lines.append(f"Undersized channels: {len(analysis['undersized'])}")
        for ch in analysis["undersized"][:3]:
            lines.append(f"  - {ch['peer_alias']}: {ch['capacity']:,} sats")
    if analysis.get("inactive"):
        lines.append(f"Inactive channels: {len(analysis['inactive'])}")
        for ch in analysis["inactive"][:3]:
            lines.append(f"  - {ch['peer_alias']}: {ch['capacity']:,} sats locked")
    if analysis.get("unprofitable"):
        lines.append(f"Unprofitable channels: {len(analysis['unprofitable'])}")
        for ch in analysis["unprofitable"][:3]:
            lines.append(f"  - {ch['peer_alias']}: {ch.get('reason', '')}")

    # Shortlisted candidates — all 10 for agent to research and rank
    lines.append("")
    lines.append("Shortlisted candidates (research each on Amboss for real capacity + avg channel size):")
    lines.append("NOTE: capacity shown is from local LND graph — may be incomplete. Use Amboss for real numbers.")
    lines.append("")
    for i, a in enumerate(plan.get("actions", []), 1):
        gd = a.get("graph_data") or {}
        lines.append(
            f"  {i}. {a['peer_alias']} "
            f"(pubkey: {a.get('peer_pubkey', 'unknown')}) "
            f"rank {a.get('network_rank', '?')} | "
            f"score {a.get('score', '?')} | "
            f"{a.get('channel_count', 0)} channels | "
            f"{a.get('capacity', 0):,} sats local capacity"
        )
        if gd:
            lines.append(
                f"     local avg fee: {gd.get('avg_fee_ppm', '?')} ppm | "
                f"diversity: {gd.get('diversity_score', 0):.0%} new peers | "
                f"clearnet: {gd.get('has_clearnet', '?')}"
            )
    lines.append("")
    # The agent searches Amboss and 1ML to find real capacity and avg channel size.
    # Local graph capacity is unreliable for distant nodes (depends on gossip
    # propagation quality — improves as you add more channels). The agent
    # triangulates between sources for confidence.
    lines.append("Search Amboss for each to find real capacity and avg channel size, then recommend top 3.")
    lines.append("")

    if plan.get("not_recommended"):
        lines.append("Concerns from engine:")
        for note in plan["not_recommended"]:
            lines.append(f"  - {note}")

    return "\n".join(lines)


# ─── Fallback ────────────────────────────────────────────────────

def _fallback_summary(plan):
    """Generate a basic summary without Claude API (when key is missing or call fails)."""
    parts = []

    actions = plan.get("actions", [])
    if actions:
        opens = [a for a in actions if a["type"] == "open"]
        upsizes = [a for a in actions if a["type"] == "upsize"]
        if upsizes:
            parts.append(f"Upsize {len(upsizes)} undersized channel(s) first.")
        if opens:
            names = ", ".join(a["peer_alias"] for a in opens)
            parts.append(f"Open {len(opens)} new channel(s) to: {names}.")

    parts.append(
        f"Treasury reserve: {plan['treasury_reserve']:,} sats "
        f"({plan['treasury_pct']:.0%}) for rebalancing and emergencies."
    )

    fee_env = plan.get("fee_environment", {})
    if fee_env.get("assessment") in ("high", "very_high"):
        parts.append(f"Warning: {fee_env.get('note', 'on-chain fees are elevated')}")

    not_rec = plan.get("not_recommended", [])
    if not_rec:
        parts.append(f"Note: {not_rec[0]}")

    return " ".join(parts)
