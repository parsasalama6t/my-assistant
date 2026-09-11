from __future__ import annotations

import json

import httpx2 as httpx
import pytest

from assistant.voice import TwilioVoice, VoiceError, build_twiml


def test_build_twiml_escapes_and_repeats() -> None:
    twiml = build_twiml("Wake up & go <now>", repeat=2)
    assert twiml.count("<Say") == 2 and '<Pause length="1"/>' in twiml
    assert "&amp; go &lt;now&gt;" in twiml
    assert build_twiml("hi", repeat=1).count("<Say") == 1
    with pytest.raises(ValueError):
        build_twiml("   ")


def _client(handler):
    return httpx.Client(transport=httpx.MockTransport(handler), auth=httpx.BasicAuth("AC1", "tok"))


def test_call_posts_inline_twiml_with_basic_auth() -> None:
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["auth"] = request.headers.get("authorization", "")
        seen["form"] = dict(httpx.QueryParams(request.content.decode()))
        return httpx.Response(201, json={"sid": "CA123", "status": "queued"})

    voice = TwilioVoice("AC1", "tok", "+14165550000", {"+14165550100"}, http=_client(handler))
    sid = voice.call("+14165550100", "Your flight boards in 90 minutes.")
    assert sid == "CA123"
    assert seen["url"].endswith("/Accounts/AC1/Calls.json")
    assert seen["auth"].startswith("Basic ")
    assert seen["form"]["To"] == "+14165550100" and seen["form"]["From"] == "+14165550000"
    assert "<Say" in seen["form"]["Twiml"] and "flight boards" in seen["form"]["Twiml"]


def test_call_strips_whatsapp_prefix_and_enforces_allowlist() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(201, json={"sid": "CA9"})

    voice = TwilioVoice("AC1", "tok", "+14165550000", {"+14165550100"}, http=_client(handler))
    assert voice.call("whatsapp:+14165550100", "hello") == "CA9"
    with pytest.raises(VoiceError, match="not an allowed"):
        voice.call("+15555550123", "hello")


def test_twilio_error_is_readable() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"code": 21219, "message": "The number is unverified."})

    voice = TwilioVoice("AC1", "tok", "+14165550000", {"+14165550100"}, http=_client(handler))
    with pytest.raises(VoiceError, match="Verified Caller IDs"):
        voice.call("+14165550100", "hello")


def test_requires_credentials() -> None:
    with pytest.raises(ValueError):
        TwilioVoice("", "tok", "+1", set())
