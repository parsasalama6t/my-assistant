from __future__ import annotations

import base64
import json
from email import message_from_bytes, policy
from typing import Any

import pytest

from assistant.agent import Assistant
from assistant.config import Config
from assistant.google_client import GoogleClient, _event_time, _extract_text
from assistant.store import Store
from assistant.tools import ToolContext, run_tool
from tests.conftest import FakeClient, make_message, text, tool_use


class FakeGoogle:
    """Implements the GoogleClient surface the tools use, recording calls."""

    email = "sam@example.com"

    def __init__(self) -> None:
        self.calls: list[tuple[str, Any]] = []
        self.sent: list[dict[str, Any]] = []

    def list_events(self, start=None, end=None, limit=25):
        self.calls.append(("list_events", (start, end, limit)))
        return [{"id": "ev1", "title": "Dentist", "start": "2026-09-12T09:00:00-04:00"}]

    def create_event(self, title, start, end=None, location="", description="", attendees=None):
        self.calls.append(("create_event", dict(title=title, start=start, end=end)))
        return {"id": "ev2", "title": title, "start": start, "end": end}

    def delete_event(self, event_id):
        self.calls.append(("delete_event", event_id))

    def search_messages(self, query="", limit=10):
        self.calls.append(("search_messages", (query, limit)))
        return [{"id": "m1", "subject": "Invoice", "from": "a@b.c", "unread": True}]

    def get_message(self, message_id, max_chars=6000):
        return {"id": message_id, "subject": "Invoice", "body": "Please pay."}

    def create_draft(self, to, subject, body, reply_to_message_id=None):
        self.calls.append(("create_draft", dict(to=to, subject=subject)))
        return {"draft_id": "d1", "to": to, "subject": subject}

    def send_message(self, to, subject, body, reply_to_message_id=None):
        self.sent.append(dict(to=to, subject=subject, body=body, reply=reply_to_message_id))
        return {"message_id": "s1", "to": to, "subject": subject}

    def mark_read(self, message_id):
        self.calls.append(("mark_read", message_id))


def test_calendar_tools_dispatch(store: Store) -> None:
    g = FakeGoogle()
    ctx = ToolContext(store, google=g)
    out, err = run_tool(ctx, "gcal_list_events", {"start": "2026-09-12", "end": "2026-09-13"})
    assert not err and json.loads(out)["events"][0]["title"] == "Dentist"
    out, err = run_tool(ctx, "gcal_create_event", {"title": "Lunch", "start": "2026-09-12T12:00"})
    assert not err and json.loads(out)["id"] == "ev2"
    out, err = run_tool(ctx, "gcal_delete_event", {"id": "ev1"})
    assert not err and ("delete_event", "ev1") in g.calls


def test_gmail_tools_dispatch(store: Store) -> None:
    g = FakeGoogle()
    ctx = ToolContext(store, google=g)
    out, err = run_tool(ctx, "gmail_search", {"query": "is:unread"})
    assert not err and json.loads(out)["count"] == 1
    out, err = run_tool(ctx, "gmail_read", {"id": "m1"})
    assert json.loads(out)["body"] == "Please pay."
    out, err = run_tool(ctx, "gmail_draft", {"to": ["a@b.c"], "subject": "Re", "body": "ok"})
    assert not err and json.loads(out)["draft_id"] == "d1"
    out, err = run_tool(ctx, "gmail_send", {"to": ["a@b.c"], "body": "ok"})
    assert not err and g.sent[0]["to"] == ["a@b.c"]
    out, err = run_tool(ctx, "gmail_mark_read", {"id": "m1"})
    assert not err and ("mark_read", "m1") in g.calls


def test_google_api_failure_becomes_tool_error(store: Store) -> None:
    class Broken(FakeGoogle):
        def list_events(self, *a, **k):
            raise ConnectionError("boom")

    out, err = run_tool(ToolContext(store, google=Broken()), "gcal_list_events", {})
    assert err and "boom" in out


def test_agent_exposes_google_tools_and_prompt_when_connected(store: Store, config: Config) -> None:
    g = FakeGoogle()
    client = FakeClient(
        [
            make_message([tool_use("gcal_list_events", {})], stop_reason="tool_use"),
            make_message([text("You have the dentist at 9.")]),
        ]
    )
    result = Assistant(store, config, client=client, google=g).chat("what's on tomorrow?")
    assert result.text == "You have the dentist at 9."
    names = {t["name"] for t in client.calls[0]["tools"]}
    assert "gcal_list_events" in names and "gmail_send" in names
    assert "sam@example.com" in client.calls[0]["system"][1]["text"]


def test_agent_hides_google_tools_when_not_connected(store: Store, config: Config) -> None:
    client = FakeClient([make_message([text("ok")])])
    Assistant(store, config, client=client).chat("hi")
    assert "gcal_list_events" not in {t["name"] for t in client.calls[0]["tools"]}
    assert "not connected" in client.calls[0]["system"][1]["text"]


# ---- pure helpers in google_client
def test_event_time_all_day_vs_timed() -> None:
    assert _event_time("2026-09-12") == {"date": "2026-09-12"}
    timed = _event_time("2026-09-12T09:30")
    assert timed["dateTime"].startswith("2026-09-12T09:30:00") and ("+" in timed["dateTime"] or "-" in timed["dateTime"][10:])


def test_extract_text_prefers_plain_and_strips_html() -> None:
    def b64(s: str) -> str:
        return base64.urlsafe_b64encode(s.encode()).decode()

    payload = {
        "mimeType": "multipart/alternative",
        "parts": [
            {"mimeType": "text/html", "body": {"data": b64("<p>Hi <b>there</b></p>")}},
            {"mimeType": "text/plain", "body": {"data": b64("Hi there")}},
        ],
    }
    assert _extract_text(payload) == "Hi there"
    html_only = {"mimeType": "text/html", "body": {"data": b64("<div>Hello</div><script>x()</script>")}}
    assert _extract_text(html_only).strip() == "Hello"


class _Exec:
    def __init__(self, value):
        self.value = value

    def execute(self):
        return self.value


class FakeGmailService:
    """Just enough of the googleapiclient resource chain for _build_raw / send."""

    def __init__(self) -> None:
        self.sent_bodies: list[dict[str, Any]] = []
        outer = self

        class Messages:
            def get(self, userId, id, format=None, metadataHeaders=None):
                return _Exec(
                    {
                        "id": id,
                        "threadId": "thr9",
                        "payload": {"headers": [{"name": "Message-ID", "value": "<orig@x>"}]},
                    }
                )

            def send(self, userId, body):
                outer.sent_bodies.append(body)
                return _Exec({"id": "sent1"})

        class Users:
            def messages(self):
                return Messages()

        self._users = Users()

    def users(self):
        return self._users


def test_send_message_threads_replies_and_builds_mime() -> None:
    gmail = FakeGmailService()
    client = GoogleClient(calendar_service=None, gmail_service=gmail, email="sam@example.com")
    out = client.send_message(["to@example.com"], "Re: hi", "Sounds good.", reply_to_message_id="m1")
    assert out["message_id"] == "sent1"
    body = gmail.sent_bodies[0]
    assert body["threadId"] == "thr9"
    mime = message_from_bytes(base64.urlsafe_b64decode(body["raw"]), policy=policy.default)
    assert mime["To"] == "to@example.com" and mime["From"] == "sam@example.com"
    assert mime["In-Reply-To"] == "<orig@x>" and mime["Subject"] == "Re: hi"
    assert mime.get_content().strip() == "Sounds good."


def test_send_rejects_bad_recipients() -> None:
    client = GoogleClient(None, FakeGmailService())
    with pytest.raises(ValueError):
        client.send_message(["not-an-email"], "s", "b")
    with pytest.raises(ValueError):
        client.send_message([], "s", "b")
