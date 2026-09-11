from __future__ import annotations

import http.client
import json
import threading
from urllib.parse import parse_qsl, urlencode

import httpx2
import pytest

from assistant.channels.base import ChannelError, ConflictError, InboundMessage, split_message
from assistant.channels.telegram import OFFSET_KEY, TelegramChannel
from assistant.channels.twilio import TWIML_EMPTY, TwilioChannel, compute_signature
from assistant.channels.webhook import WebhookServer
from assistant.store import Store

TOKEN = "123:abc"
WEBHOOK_URL = "https://example.com/webhooks/twilio"


# ----------------------------------------------------------- split_message
def test_split_message_prefers_paragraphs_then_lines_then_spaces() -> None:
    text = "para one\n\npara two line a\nline b\n\nword " * 3
    chunks = split_message(text, 30)
    assert all(len(c) <= 30 for c in chunks)
    assert "".join(c.replace("\n", "").replace(" ", "") for c in chunks) == text.replace("\n", "").replace(" ", "")
    assert chunks[0] == "para one"

    assert split_message("x" * 10, 4) == ["xxxx", "xxxx", "xx"]
    assert split_message("short", 100) == ["short"]
    assert split_message("   ", 10) == []


# ----------------------------------------------------------------- telegram
def _telegram(handler, store: Store, allowed=("111",)) -> TelegramChannel:
    http = httpx2.Client(transport=httpx2.MockTransport(handler))
    return TelegramChannel(TOKEN, set(allowed), store, http=http)


def test_telegram_poll_parses_updates_and_persists_offset(store: Store) -> None:
    requests: list[dict] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        payload = json.loads(request.content)
        requests.append({"url": str(request.url), "json": payload})
        assert request.url.path == f"/bot{TOKEN}/getUpdates"
        return httpx2.Response(
            200,
            json={
                "ok": True,
                "result": [
                    {
                        "update_id": 500,
                        "message": {
                            "message_id": 1,
                            "text": "hello",
                            "chat": {"id": 111},
                            "from": {"username": "sam", "first_name": "Sam"},
                        },
                    },
                    {"update_id": 501, "message": {"message_id": 2, "chat": {"id": 111}, "photo": []}},
                    {
                        "update_id": 502,
                        "message": {"message_id": 3, "text": "hi", "chat": {"id": 222}, "from": {"first_name": "Eve"}},
                    },
                ],
            },
        )

    tg = _telegram(handler, store)
    messages = tg.poll(timeout=5)
    assert [(m.chat_id, m.text, m.message_id, m.sender) for m in messages] == [
        ("111", "hello", "500", "sam"),
        ("222", "hi", "502", "Eve"),
    ]
    assert all(m.channel == "telegram" for m in messages)
    assert store.get_state(OFFSET_KEY) == "503"
    assert requests[0]["json"]["timeout"] == 5
    assert requests[0]["json"]["allowed_updates"] == ["message"]
    assert "offset" not in requests[0]["json"]

    tg.poll(timeout=5)
    assert requests[1]["json"]["offset"] == 503
    assert tg.is_allowed("111") and not tg.is_allowed("222")


def test_telegram_send_splits_long_messages_and_returns_last_id(store: Store) -> None:
    sent: list[dict] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        payload = json.loads(request.content)
        assert request.url.path == f"/bot{TOKEN}/sendMessage"
        sent.append(payload)
        return httpx2.Response(200, json={"ok": True, "result": {"message_id": len(sent)}})

    tg = _telegram(handler, store)
    text = "\n".join(f"line {i} " + "x" * 100 for i in range(60))
    assert len(text) > 4096
    message_id = tg.send("111", text)
    assert len(sent) == 2 and message_id == "2"
    assert all(len(p["text"]) <= 4096 for p in sent)
    assert all(p["chat_id"] == "111" and p["disable_web_page_preview"] is True for p in sent)
    assert "parse_mode" not in sent[0]


def test_telegram_errors(store: Store) -> None:
    def conflict(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(409, json={"ok": False, "description": "Conflict"})

    with pytest.raises(ConflictError, match="another daemon"):
        _telegram(conflict, store).poll(1)

    def bad(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(400, json={"ok": False, "description": "Bad Request: chat not found"})

    with pytest.raises(ChannelError, match="chat not found"):
        _telegram(bad, store).send("111", "hi")

    def down(request: httpx2.Request) -> httpx2.Response:
        raise httpx2.ConnectError("boom")

    with pytest.raises(ChannelError):
        _telegram(down, store).poll(1)


# ------------------------------------------------------------------- twilio
def _twilio(handler, store: Store, sms_from="+15550001111") -> TwilioChannel:
    http = httpx2.Client(transport=httpx2.MockTransport(handler))
    return TwilioChannel(
        "ACxxx", "tok", sms_from, "whatsapp:+14155238886", {"+14165550100"}, WEBHOOK_URL, store, http=http
    )


def test_twilio_send_picks_whatsapp_sender_and_basic_auth(store: Store) -> None:
    seen: list[tuple[str, dict, str]] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        form = dict(parse_qsl(request.content.decode()))
        seen.append((str(request.url), form, request.headers.get("authorization", "")))
        return httpx2.Response(201, json={"sid": "SM123"})

    tw = _twilio(handler, store)
    assert tw.send("whatsapp:+14165550100", "hey") == "SM123"
    assert tw.send("+14165550100", "hey") == "SM123"
    url, form, auth = seen[0]
    assert url == "https://api.twilio.com/2010-04-01/Accounts/ACxxx/Messages.json"
    assert form == {"To": "whatsapp:+14165550100", "From": "whatsapp:+14155238886", "Body": "hey"}
    assert auth.startswith("Basic ")
    assert seen[1][1]["From"] == "+15550001111"
    assert tw.is_allowed("whatsapp:+14165550100") and tw.is_allowed("+14165550100")
    assert not tw.is_allowed("+15550009999")


def test_twilio_send_reports_whatsapp_window_error(store: Store) -> None:
    def handler(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(400, json={"code": 63016, "message": "Failed to send freeform message"})

    with pytest.raises(ChannelError, match="24 hours"):
        _twilio(handler, store).send("whatsapp:+14165550100", "hey")

    def other(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(401, json={"code": 20003, "message": "Authenticate"})

    with pytest.raises(ChannelError, match="20003"):
        _twilio(other, store).send("+14165550100", "hey")


def _signed_form(form: dict[str, str], token: str = "tok", url: str = WEBHOOK_URL) -> dict[str, str]:
    return {"X-Twilio-Signature": compute_signature(url, form, token)}


def test_twilio_webhook_signature_accept_reject_and_dedupe(store: Store) -> None:
    tw = _twilio(lambda r: httpx2.Response(500), store)
    form = {"MessageSid": "SM1", "From": "whatsapp:+14165550100", "Body": "hello there"}

    status, body, msg = tw.handle_webhook(WEBHOOK_URL, _signed_form(form), form)
    assert status == 200 and body == TWIML_EMPTY
    assert isinstance(msg, InboundMessage)
    assert (msg.channel, msg.chat_id, msg.text, msg.message_id) == ("twilio", "whatsapp:+14165550100", "hello there", "SM1")

    # Same MessageSid again: acknowledged but not processed.
    status, body, msg = tw.handle_webhook(WEBHOOK_URL, _signed_form(form), form)
    assert status == 200 and msg is None

    status, _, msg = tw.handle_webhook(WEBHOOK_URL, _signed_form(form, token="wrong"), form)
    assert status == 403 and msg is None
    status, _, msg = tw.handle_webhook(WEBHOOK_URL, {}, form)
    assert status == 403 and msg is None

    stranger = {"MessageSid": "SM2", "From": "+15550009999", "Body": "hi"}
    status, body, msg = tw.handle_webhook(WEBHOOK_URL, _signed_form(stranger), stranger)
    assert status == 200 and body == TWIML_EMPTY and msg is None


def test_compute_signature_known_vector() -> None:
    # Twilio's documented example.
    url = "https://mycompany.com/myapp.php?foo=1&bar=2"
    form = {
        "CallSid": "CA1234567890ABCDE",
        "Caller": "+12349013030",
        "Digits": "1234",
        "From": "+12349013030",
        "To": "+18005551212",
    }
    assert compute_signature(url, form, "12345") == "0/KCTR6DLpKmkAf8muzZqo1nDgQ="


def test_webhook_server_end_to_end(store: Store) -> None:
    tw = _twilio(lambda r: httpx2.Response(500), store)
    received: list[InboundMessage] = []
    got = threading.Event()

    def on_message(msg: InboundMessage) -> None:
        received.append(msg)
        got.set()

    server = WebhookServer(0, "/webhooks/twilio", WEBHOOK_URL, tw.handle_webhook, on_message, host="127.0.0.1")
    server.start()
    try:
        form = {"MessageSid": "SM9", "From": "whatsapp:+14165550100", "Body": "ping"}
        body = urlencode(form)
        conn = http.client.HTTPConnection("127.0.0.1", server.port, timeout=5)
        conn.request(
            "POST",
            "/webhooks/twilio",
            body=body,
            headers={"Content-Type": "application/x-www-form-urlencoded", **_signed_form(form)},
        )
        resp = conn.getresponse()
        assert resp.status == 200
        assert resp.getheader("Content-Type", "").startswith("text/xml")
        assert resp.read().decode() == TWIML_EMPTY
        assert got.wait(5)
        assert received[0].text == "ping"

        conn.request("POST", "/webhooks/twilio", body=body, headers={"Content-Type": "application/x-www-form-urlencoded"})
        assert conn.getresponse().status == 403

        conn.request("GET", "/healthz")
        resp = conn.getresponse()
        assert resp.status == 200 and resp.read() == b"ok"
        conn.close()
    finally:
        server.stop()
