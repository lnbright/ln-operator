"""
LN Operator — Telegram Notifications

Sends alerts and the once-a-day exec summary to a Telegram chat. Routine
per-run pings (fee updates, rebalance reports) were removed by design — the
daily summary aggregates them and the dashboard has live state. Telegram is
reserved for things you'd actually want to look at on your phone.

What gets sent:
- Health alerts (depleted channels, offline peers, repeated rebalance failures)
- Daily exec summary (see scripts/daily-check)

Handles Telegram's 4096 character limit by splitting long messages.
If formatting fails (Markdown issues), retries without formatting.
If no bot token or chat ID is configured, silently skips — nothing breaks.
"""

import requests
from config import TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID


def send_message(text, parse_mode="Markdown"):
    """Send a message to the configured Telegram chat.
    
    Splits long messages (Telegram limit is 4096 chars).
    Returns True if all parts sent successfully.
    """
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("[telegram] No bot token or chat ID configured, skipping.")
        return False

    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    chunks = _split_message(text, 4000)
    success = True

    for chunk in chunks:
        try:
            r = requests.post(url, json={
                "chat_id": TELEGRAM_CHAT_ID,
                "text": chunk,
                "parse_mode": parse_mode,
                "disable_web_page_preview": True,
            }, timeout=10)
            if not r.ok:
                # Retry without parse_mode in case of formatting issues
                r2 = requests.post(url, json={
                    "chat_id": TELEGRAM_CHAT_ID,
                    "text": chunk,
                    "disable_web_page_preview": True,
                }, timeout=10)
                if not r2.ok:
                    print(f"[telegram] Failed to send: {r2.text}")
                    success = False
        except Exception as e:
            print(f"[telegram] Error: {e}")
            success = False

    return success


def _split_message(text, max_len=4000):
    """Split a message into chunks respecting line boundaries."""
    if len(text) <= max_len:
        return [text]

    chunks = []
    current = ""
    for line in text.split("\n"):
        if len(current) + len(line) + 1 > max_len:
            chunks.append(current)
            current = line
        else:
            current = f"{current}\n{line}" if current else line
    if current:
        chunks.append(current)
    return chunks


def format_alert(alert_type, details):
    """Format a monitoring alert."""
    emojis = {
        "channel_depleted": "🔴",
        "channel_saturated": "🟡",
        "peer_offline": "⚫",
        "rebalance_needed": "🔄",
        "high_fees_environment": "⛓",
    }
    emoji = emojis.get(alert_type, "⚠️")
    return f"{emoji} *Alert: {alert_type.replace('_', ' ').title()}*\n{details}"
