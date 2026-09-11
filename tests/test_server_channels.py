from __future__ import annotations

import pytest

fastapi = pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from assistant.channels.twilio import compute_signature  # noqa: E402
from assistant.config import Config  # noqa: E402
from assistant.server import create_app  # noqa: E402
from assistant.store import Store  # noqa: E402

WEBHOOK_URL = "https://example.com/webhooks/twilio"


class RecordingDaemon:
    def __init__(self) -> None:
        self.messages = []

    def handle_inbound(self, msg) -> str:
        self.messages.append(msg)
        return "ok"


def test_schedules_route_and_twilio_webhook(store: Store) -> None:
    config = Config(
        default_channel="twilio",
        twilio_account_sid="ACxxx",
        twilio_auth_token="tok",
        twilio_from="",
        user_phone="+14165550100",
        twilio_webhook_url=WEBHOOK_URL,
    )
    store.add_schedule(kind="custom", channel="twilio", chat_id="whatsapp:+14165550100",
                       next_run_utc="2099-01-01T12:00:00+00:00", tz="UTC", text="ping")
    daemon = RecordingDaemon()
    client = TestClient(create_app(store, config, daemon=daemon))

    rows = client.get("/api/schedules").json()
    assert [r["text"] for r in rows] == ["ping"]

    form = {"MessageSid": "SM77", "From": "whatsapp:+14165550100", "Body": "hey"}
    sig = compute_signature(WEBHOOK_URL, form, "tok")
    resp = client.post("/webhooks/twilio", data=form, headers={"X-Twilio-Signature": sig})
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/xml")
    assert "<Response></Response>" in resp.text

    import time

    deadline = time.time() + 5
    while not daemon.messages and time.time() < deadline:
        time.sleep(0.02)
    assert daemon.messages and daemon.messages[0].text == "hey"

    resp = client.post("/webhooks/twilio", data=form, headers={"X-Twilio-Signature": "bad"})
    assert resp.status_code == 403


def test_no_webhook_route_without_twilio(store: Store) -> None:
    client = TestClient(create_app(store, Config()))
    assert client.post("/webhooks/twilio", data={"Body": "x"}).status_code in {404, 405}
    assert client.get("/api/schedules").json() == []
