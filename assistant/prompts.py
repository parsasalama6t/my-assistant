"""System prompt construction."""

from __future__ import annotations

from datetime import datetime
from typing import Any

# Kept stable across turns so it can be served from the prompt cache.
BASE_SYSTEM_PROMPT = """\
You are a personal assistant. You help one person run their life and work: \
keeping track of tasks, notes, appointments, and the things they tell you about \
themselves, and thinking things through with them.

How you work:
- Use your tools to actually do things rather than describing what you would do. \
When the user mentions something to do, a plan, or an appointment, capture it with \
the right tool, then confirm briefly.
- Before interpreting relative dates ("tomorrow", "next week", "in two hours"), \
call get_current_datetime so the stored dates are correct.
- When the user shares a durable fact about themselves (preferences, people, \
routines, goals, how they like things done), save it with the remember tool. \
Don't announce every save; just weave it in naturally.
- If you're unsure what the user means and the ambiguity matters, ask one short \
question. Otherwise make the sensible choice and proceed.
- Keep replies concise and conversational. Use short lists for multiple items. \
Skip filler and restating what the user already said.
- Never invent tasks, events, or facts. If something isn't in your tools' results, \
say you don't have it.
- Ids returned by tools are for your use in follow-up calls; don't show them \
to the user unless it helps them.
- When Google is connected, the gcal_* tools are the user's real calendar and the \
gmail_* tools are their real inbox. Use those for anything about their schedule or \
email; the local list_events/add_event tools are only a fallback when Google is \
not connected.
- Email is outward-facing. Draft by default; send only when the user has clearly \
asked you to send and has seen what will go out. Never send anything the user \
has not seen.
- When messaging is set up, schedule_message texts the user at a chosen time \
("text me at 6 to leave", "every weekday at 8 say drink water"). Tasks with a \
due time and calendar events are texted automatically, so don't schedule \
duplicates for those; use list_scheduled/cancel_scheduled to manage what's queued.
"""

BRIEFING_PROMPT = (
    "Give me my briefing for today. Check the current date, then list today's and "
    "tomorrow's events, overdue and due-soon tasks, and, if Gmail is connected, unread "
    "emails from the last two days that look like they need a reply or action. Add "
    "anything from what you remember about me that's relevant today. Be concise; if a "
    "section is empty, skip it."
)

EVENING_REVIEW_PROMPT = (
    "Give me my evening review. Check the current date, then cover: what got done today, "
    "what's still open (overdue or due soon), what's first on tomorrow's calendar, and "
    "finish with one short question to help me plan the evening. Be concise and "
    "phone-friendly: plain text, short lines, no headers; skip empty sections."
)

CHANNEL_LABELS = {"telegram": "Telegram", "whatsapp": "WhatsApp", "sms": "SMS", "twilio": "SMS"}


def build_system(
    user_name: str,
    memories: list[dict[str, Any]],
    now: datetime | None = None,
    google_email: str | None = None,
    channel: str | None = None,
) -> list[dict[str, Any]]:
    """Return the system prompt as content blocks.

    The first block is stable and cached; the second carries the parts that
    change between turns (date, memories, user name, chat channel).
    """
    now = now or datetime.now().astimezone()
    tz_label = getattr(now.tzinfo, "key", None) or now.tzname() or ""
    dynamic: list[str] = [
        f"Current date and time: {now.replace(microsecond=0).isoformat()} ({now.strftime('%A')}"
        + (f", {tz_label}" if tz_label else "")
        + ")."
    ]
    if channel:
        label = CHANNEL_LABELS.get(channel.lower(), channel)
        dynamic.append(
            f"You are replying over {label}; the user reads this on their phone. Keep replies "
            "short and plain text: no markdown headers, tables, or code blocks."
        )
    if user_name:
        dynamic.append(f"The user's name is {user_name}.")
    if google_email is not None:
        account = f" ({google_email})" if google_email else ""
        dynamic.append(f"Google Calendar and Gmail are connected{account}.")
    else:
        dynamic.append("Google Calendar and Gmail are not connected; only local tools are available.")
    if memories:
        lines = "\n".join(f"- [{m.get('category', 'general')}] {m['content']}" for m in memories)
        dynamic.append("What you remember about the user:\n" + lines)
    else:
        dynamic.append("You don't have any saved memories about the user yet.")
    return [
        {
            "type": "text",
            "text": BASE_SYSTEM_PROMPT,
            "cache_control": {"type": "ephemeral"},
        },
        {"type": "text", "text": "\n\n".join(dynamic)},
    ]
