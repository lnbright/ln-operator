"""
LN Operator — Agent Layer (10%)
Uses Claude API for judgement calls that pure Python can't handle well.
Receives pre-digested data, returns plain-English analysis.
"""

import json
import requests
from config import ANTHROPIC_API_KEY, ANTHROPIC_MODEL


def get_investment_summary(plan):
    """Send the structured plan to Claude for a human-readable summary.
    
    The agent receives a compact JSON summary (not raw API dumps)
    and returns plain-English investment advice.
    """
    if not ANTHROPIC_API_KEY:
        return _fallback_summary(plan)

    # Build a compact prompt — only the data Claude needs
    compact = _build_compact_prompt(plan)

    try:
        response = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "Content-Type": "application/json",
                "x-api-key": ANTHROPIC_API_KEY,
                "anthropic-version": "2023-06-01",
            },
            json={
                "model": ANTHROPIC_MODEL,
                "max_tokens": 1000,
                "system": AGENT_SYSTEM_PROMPT,
                "messages": [
                    {"role": "user", "content": compact}
                ],
            },
            timeout=30,
        )
        response.raise_for_status()
        data = response.json()

        # Extract text from response
        text = ""
        for block in data.get("content", []):
            if block.get("type") == "text":
                text += block.get("text", "")

        return text.strip() if text else _fallback_summary(plan)

    except Exception as e:
        print(f"[agent] Claude API call failed: {e}")
        return _fallback_summary(plan)


AGENT_SYSTEM_PROMPT = """You are a Lightning Network node advisor for a small home node operator.
You receive a structured investment plan from a Python engine and your job is to:

1. Write a brief, clear summary of what to do and why (3-5 sentences max).
2. Flag any risks or concerns the Python engine might have missed.
3. If the fee environment is bad, say so directly.
4. If a recommended peer is questionable, say so.
5. Be honest about expected returns — small nodes earn modest routing fees.

Keep it concise. No headers, no bullet points. Write like a knowledgeable friend
giving advice over a chat message. Use sats, not BTC. Mention specific peer names
when relevant."""


def _build_compact_prompt(plan):
    """Build a token-efficient prompt from the plan."""
    lines = []
    lines.append(f"Investment: {plan['total_sats']:,} sats")
    lines.append(f"Treasury: {plan['treasury_reserve']:,} sats ({plan['treasury_pct']:.0%})")
    lines.append(f"Deployable: {plan['deployable_sats']:,} sats")
    lines.append("")

    state = plan.get("current_state", {})
    lines.append(f"Current node: {state.get('num_channels', 0)} channels, "
                 f"{state.get('total_capacity', 0):,} sats capacity, "
                 f"overall ratio {state.get('overall_ratio', 0):.0%}, "
                 f"{state.get('num_active', 0)} active / {state.get('num_inactive', 0)} inactive")
    lines.append("")

    fee_env = plan.get("fee_environment", {})
    if fee_env:
        lines.append(f"On-chain fees: {fee_env.get('fastest_fee', '?')} sat/vB ({fee_env.get('assessment', '?')})")
    lines.append("")

    # Channel analysis summary
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
    lines.append("")

    # Recommended actions
    lines.append("Recommended actions:")
    for a in plan.get("actions", []):
        lines.append(f"  {a['type'].upper()}: {a['peer_alias']} — {a['amount_sats']:,} sats — {a.get('reason', '')}")
    lines.append("")

    # Not recommended
    if plan.get("not_recommended"):
        lines.append("Concerns:")
        for note in plan["not_recommended"]:
            lines.append(f"  - {note}")

    return "\n".join(lines)


def _fallback_summary(plan):
    """Generate a basic summary without Claude API (when key is missing or call fails)."""
    parts = []

    actions = plan.get("actions", [])
    if actions:
        opens = [a for a in actions if a["type"] == "open"]
        upsizes = [a for a in actions if a["type"] == "upsize"]

        if upsizes:
            parts.append(
                f"Upsize {len(upsizes)} undersized channel(s) first to improve existing routing."
            )
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


def get_followup_answer(plan, question):
    """Answer a follow-up question about an investment plan.
    
    Used for: "why this peer?", "what if I deploy half?", etc.
    """
    if not ANTHROPIC_API_KEY:
        return "Claude API key not configured. Set ANTHROPIC_API_KEY to enable follow-up Q&A."

    compact = _build_compact_prompt(plan)

    try:
        response = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "Content-Type": "application/json",
                "x-api-key": ANTHROPIC_API_KEY,
                "anthropic-version": "2023-06-01",
            },
            json={
                "model": ANTHROPIC_MODEL,
                "max_tokens": 500,
                "system": AGENT_SYSTEM_PROMPT,
                "messages": [
                    {"role": "user", "content": f"Plan context:\n{compact}\n\nQuestion: {question}"}
                ],
            },
            timeout=30,
        )
        response.raise_for_status()
        data = response.json()
        text = ""
        for block in data.get("content", []):
            if block.get("type") == "text":
                text += block.get("text", "")
        return text.strip()

    except Exception as e:
        return f"Could not reach Claude API: {e}"
