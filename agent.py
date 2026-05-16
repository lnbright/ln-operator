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
from logging_config import get_logger

log = get_logger("agent")


# ─── System prompt ────────────────────────────────────────────────

AGENT_SYSTEM_PROMPT = """You are a Lightning Network node advisor. Be concise and direct.

For each recommended peer, research it on amboss.space, 1ml.com and via web search,
then give ONE sentence per peer in plain prose (no markdown, no bullets, no headers):

"Recommend [node name] because [score reason] and [reputation finding from research]."

Follow with one sentence on timing (on-chain fees) and one sentence if a better
alternative exists from the candidate list.

No markdown formatting. No bullet points. No headers. No generic advice.
Plain prose only. Max 100 words total. Use sats not BTC."""


# ─── Agentic call with web search ────────────────────────────────

def _run_agentic_call(system_prompt, user_message, max_tokens=2000, max_turns=5):
    """Run a Claude API call with web search enabled.

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
                        "type": "web_search_20250305",
                        "name": "web_search",
                    }
                ],
                "messages": messages,
            },
            timeout=60,
        )
        response.raise_for_status()
        data = response.json()

        content_blocks = data.get("content", [])
        stop_reason = data.get("stop_reason", "")

        log.debug("agent turn %d: stop_reason=%s, blocks=%d",
                  turn + 1, stop_reason, len(content_blocks))

        # Append assistant response to message history
        messages.append({"role": "assistant", "content": content_blocks})

        # If Claude is done (no more tool calls), extract the final text
        if stop_reason == "end_turn":
            text = ""
            for block in content_blocks:
                if block.get("type") == "text":
                    text += block.get("text", "")
            return text.strip() if text else None

        # If Claude wants to use tools, collect the tool_use blocks and send results
        if stop_reason == "tool_use":
            tool_results = []
            for block in content_blocks:
                if block.get("type") == "tool_use":
                    tool_id = block.get("id", "")
                    tool_name = block.get("name", "")
                    # Web search results are returned directly in the next turn
                    # We send back a placeholder — the API handles the actual search
                    tool_results.append({
                        "type": "tool_result",
                        "tool_use_id": tool_id,
                        "content": f"[Search results for: {block.get('input', {}).get('query', '')}]"
                    })
                    log.debug("agent web search: %s", block.get("input", {}).get("query", ""))

            if tool_results:
                messages.append({"role": "user", "content": tool_results})
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
        text = _run_agentic_call(AGENT_SYSTEM_PROMPT, compact, max_tokens=500)

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

    # Recommended actions — include pubkeys and graph data so agent can research
    lines.append("")
    lines.append("Recommended actions (please research each peer before advising):")
    for a in plan.get("actions", []):
        lines.append(
            f"  {a['type'].upper()}: {a['peer_alias']} "
            f"(pubkey: {a.get('peer_pubkey', 'unknown')}) "
            f"— {a['amount_sats']:,} sats — score {a.get('score', '?')}"
        )
        # Include graph data if available
        gd = a.get("graph_data")
        if gd:
            lines.append(
                f"    Graph: {a.get('channel_count',0)} channels, "
                f"{a.get('capacity',0):,} sats capacity, "
                f"avg fee {gd.get('avg_fee_ppm', '?')} ppm, "
                f"diversity {gd.get('diversity_score', '?'):.0%} new peers, "
                f"clearnet: {gd.get('has_clearnet', '?')}"
            )
        lines.append(f"    Engine reason: {a.get('reason', '')}")
    lines.append("")

    # Top scored candidates not in actions (for alternatives)
    candidates = plan.get("peer_candidates", [])
    if candidates:
        lines.append("Other top-scored candidates (for alternatives if needed):")
        for c in candidates[:5]:
            gd = c.get("graph_data")
            line = (
                f"  - {c.get('alias', '?')} "
                f"(pubkey: {c.get('pubkey', '')}) "
                f"score {c.get('score', '?')}, "
                f"{c.get('channel_count', 0)} channels, "
                f"{c.get('capacity', 0):,} sats"
            )
            if gd:
                line += (
                    f", avg fee {gd.get('avg_fee_ppm','?')} ppm, "
                    f"diversity {gd.get('diversity_score',0):.0%}"
                )
            lines.append(line)
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
