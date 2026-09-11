"""A scripted stand-in for the API client, used by `my-assistant demo`.

It answers a handful of phrasings with canned text and real tool calls (so the
schedule, task and memory tools actually run against the demo database), and
says so when it does not understand. No network, no credentials.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, tzinfo
from types import SimpleNamespace
from typing import Any

from anthropic.types import Message, TextBlock, ToolUseBlock, Usage

BANNER = """\
my-assistant demo
This runs the real daemon, scheduler and tools against a scratch database.
Replies are SCRIPTED until you set ANTHROPIC_API_KEY (see .env.example); no
messages leave this terminal. Try:
  text me in 1 minute to stretch      (a real schedule that fires while you watch)
  call me in 1 minute to leave        (a critical schedule: it texts AND "calls")
  call me and say the oven is on      (a phone call right now, printed here)
  add task call the dentist tomorrow
  tasks   ·   remember that I take Fridays off   ·   /schedule   ·   /help
Ctrl-D or /quit exits."""

_SCRIPTED_NOTE = (
    "(demo) I only understand a few phrasings without an API key. Try "
    "'text me in 2 minutes to stretch', 'call me in 1 minute to leave', 'add task buy milk', "
    "'tasks', or 'remember that I work out on Tuesdays'."
)

_IN_RE = re.compile(r"\bin\s+(\d+)\s*(min|mins|minute|minutes|hour|hours|h|m)\b", re.I)
_AT_RE = re.compile(r"\bat\s+(\d{1,2})(?::(\d{2}))?\s*(am|pm)?\b", re.I)
_ISO_RE = re.compile(r"\b(\d{4}-\d{2}-\d{2}T\d{2}:\d{2})\b")
_CALL_RE = re.compile(r"\bcall\s+me\b", re.I)
_TASK_RE = re.compile(r"(?:add (?:a )?task|todo|remind me to|i need to)[:,]?\s+(.+)", re.I)


def _message(blocks: list[Any], stop_reason: str = "end_turn") -> Message:
    return Message(
        id="msg_demo",
        type="message",
        role="assistant",
        model="scripted-demo",
        content=blocks,
        stop_reason=stop_reason,
        stop_sequence=None,
        usage=Usage(input_tokens=0, output_tokens=0),
    )


def _text(s: str) -> TextBlock:
    return TextBlock(type="text", text=s)


def _tool(name: str, tool_input: dict[str, Any]) -> ToolUseBlock:
    return ToolUseBlock(type="tool_use", id=f"toolu_demo_{name}", name=name, input=tool_input)


class _Stream:
    def __init__(self, message: Message) -> None:
        self.message = message

    def __enter__(self) -> "_Stream":
        return self

    def __exit__(self, *exc: Any) -> None:
        return None

    def __iter__(self):
        for block in self.message.content:
            if block.type == "text":
                yield SimpleNamespace(
                    type="content_block_delta",
                    delta=SimpleNamespace(type="text_delta", text=block.text),
                )

    def get_final_message(self) -> Message:
        return self.message


class _Messages:
    def __init__(self, client: "ScriptedClient") -> None:
        self.client = client

    def stream(self, **kwargs: Any) -> _Stream:
        self.client.calls.append(kwargs)
        return _Stream(self.client.respond(kwargs.get("messages") or []))


class ScriptedClient:
    """Looks like the SDK client to the agent loop; replies come from `respond`."""

    def __init__(self, tz: tzinfo) -> None:
        self.tz = tz
        self.calls: list[dict[str, Any]] = []
        self.messages = _Messages(self)
        self.beta = SimpleNamespace(messages=self.messages)

    # ---------------------------------------------------------- parsing
    def parse_when(self, text: str) -> str | None:
        """'in 5 minutes' / 'at 3pm' / ISO -> local YYYY-MM-DDTHH:MM, or None."""
        now = datetime.now(self.tz).replace(second=0, microsecond=0)
        m = _ISO_RE.search(text)
        if m:
            return m.group(1)
        m = _IN_RE.search(text)
        if m:
            n = int(m.group(1))
            unit = m.group(2).lower()
            delta = timedelta(hours=n) if unit.startswith("h") else timedelta(minutes=n)
            return (now + delta).strftime("%Y-%m-%dT%H:%M")
        m = _AT_RE.search(text)
        if m:
            hour = int(m.group(1))
            minute = int(m.group(2) or 0)
            ampm = (m.group(3) or "").lower()
            if ampm == "pm" and hour < 12:
                hour += 12
            if ampm == "am" and hour == 12:
                hour = 0
            if hour > 23 or minute > 59:
                return None
            when = now.replace(hour=hour, minute=minute)
            if when <= now:
                when += timedelta(days=1)
            return when.strftime("%Y-%m-%dT%H:%M")
        return None

    @staticmethod
    def _reminder_text(text: str) -> str:
        m = re.search(r"\bto\s+(.+)$", text, re.I)
        body = m.group(1).strip() if m else ""
        body = _IN_RE.sub("", body)
        body = _AT_RE.sub("", body).strip(" .,")
        return body or "This is your scheduled text from the demo."

    @staticmethod
    def _call_text(text: str) -> str:
        """'call me and say the oven is on' / 'call me about the oven' -> what to say."""
        stripped = _AT_RE.sub("", _IN_RE.sub("", text))
        m = re.search(r"\bcall\s+me\b[\s,]*(?:and\s+)?(?:say|tell me|about|that|to)?\s*(.+)$", stripped, re.I)
        body = m.group(1).strip(" .,") if m else ""
        return body or "This is your assistant calling from the demo."

    # -------------------------------------------------------- responses
    def _after_tool(self, history: list[dict[str, Any]]) -> Message:
        results = history[-1]["content"]
        tool_name = ""
        for block in reversed(history[-2]["content"]) if len(history) >= 2 else []:
            if isinstance(block, dict) and block.get("type") == "tool_use":
                tool_name = block.get("name", "")
                break
        output = results[0].get("content", "") if results else ""
        is_error = bool(results and results[0].get("is_error"))
        if is_error:
            return _message([_text(f"That didn't work: {output.replace('Error: ', '')}")])
        try:
            data = json.loads(output)
        except (ValueError, TypeError):
            data = {}
        if tool_name == "schedule_message":
            verb = {"critical": "call and text", "important": "text (and call if you don't reply)"}.get(
                data.get("priority", "normal"), "text"
            )
            return _message(
                [_text(f"Done. I'll {verb} you \"{data.get('text', '')}\" at {data.get('next_run', '?')}.")]
            )
        if tool_name == "call_me":
            return _message([_text("Calling you now.")])
        if tool_name == "add_task":
            due = f" (due {data['due']})" if data.get("due") else ""
            return _message([_text(f"Added \"{data.get('title', '')}\"{due}.")])
        if tool_name == "remember":
            return _message([_text("Noted, I'll remember that.")])
        if tool_name == "list_tasks":
            tasks = data.get("tasks") or []
            if not tasks:
                body = "You have no open tasks."
            else:
                body = "Open tasks:\n" + "\n".join(
                    f"- {t['title']}" + (f" (due {t['due']})" if t.get("due") else "") for t in tasks
                )
            if self._briefing_pending:
                self._briefing_pending = False
                body = (
                    "Good morning! Here's your day.\n"
                    "Calendar: nothing connected in the demo (Google would show here).\n"
                    f"{body}\n"
                    "Tip: say 'text me in 1 minute to stretch' to watch a schedule fire."
                )
            return _message([_text(body)])
        return _message([_text("Done.")])

    _briefing_pending = False

    def respond(self, history: list[dict[str, Any]]) -> Message:
        if not history:
            return _message([_text(_SCRIPTED_NOTE)])
        last = history[-1]
        if last.get("role") == "user" and isinstance(last.get("content"), list):
            return self._after_tool(history)
        text = str(last.get("content") or "").strip()
        low = text.lower()

        if low.startswith("give me my briefing") or low.startswith("give me my evening review"):
            self._briefing_pending = True
            return _message([_tool("list_tasks", {"status": "open"})], stop_reason="tool_use")
        if _CALL_RE.search(low) and not _TASK_RE.match(text):
            when = self.parse_when(text)
            if when is None:
                return _message(
                    [_text("Calling you.\n"), _tool("call_me", {"text": self._call_text(text)})],
                    stop_reason="tool_use",
                )
            return _message(
                [
                    _text("Scheduling a call.\n"),
                    _tool("schedule_message", {"text": self._call_text(text), "when": when, "priority": "critical"}),
                ],
                stop_reason="tool_use",
            )
        if re.search(r"\b(text|message|ping|remind)\s+me\b", low) and not low.startswith("remind me to "):
            when = self.parse_when(text)
            if when is None:
                return _message([_text("When should I text you? For example: 'text me in 1 minute to stretch'.")])
            return _message(
                [
                    _text("Scheduling that.\n"),
                    _tool("schedule_message", {"text": self._reminder_text(text), "when": when}),
                ],
                stop_reason="tool_use",
            )
        m = _TASK_RE.match(text)
        if m:
            title = m.group(1).strip(" .")
            due = None
            if "tomorrow" in low:
                due = (datetime.now(self.tz) + timedelta(days=1)).strftime("%Y-%m-%d")
                title = re.sub(r"\s*\btomorrow\b", "", title, flags=re.I).strip()
            payload: dict[str, Any] = {"title": title}
            if due:
                payload["due"] = due
            return _message([_tool("add_task", payload)], stop_reason="tool_use")
        m = re.match(r"remember (?:that )?(.+)", text, re.I)
        if m:
            return _message([_tool("remember", {"content": m.group(1).strip(" .")})], stop_reason="tool_use")
        if re.search(r"\b(tasks?|to-?do|what'?s on|plate)\b", low):
            return _message([_tool("list_tasks", {"status": "open"})], stop_reason="tool_use")
        if re.match(r"(hi|hello|hey|yo)\b", low):
            return _message([_text("Hi! I'm your assistant (scripted demo mode). What can I do for you?")])
        if low.startswith("thank"):
            return _message([_text("Any time.")])
        return _message([_text(_SCRIPTED_NOTE)])
