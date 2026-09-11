"""Optional web UI: a small FastAPI app that streams replies over server-sent events."""

from __future__ import annotations

import json
import queue
import threading
from pathlib import Path
from typing import Any, Iterator
from urllib.parse import parse_qsl

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import HTMLResponse, StreamingResponse
from pydantic import BaseModel

from assistant.agent import Assistant, AssistantError
from assistant.config import Config
from assistant.store import Store

WEB_DIR = Path(__file__).parent / "web"


class ChatRequest(BaseModel):
    message: str
    session_id: str | None = None


def _sse(event: str, data: Any) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


def create_app(
    store: Store,
    config: Config,
    client: Any | None = None,
    google: Any | None = None,
    daemon: Any | None = None,
) -> FastAPI:
    """Build the app. With `daemon` (a `Daemon` or anything with `handle_inbound`),
    Twilio webhook posts are answered through it."""
    app = FastAPI(title="my-assistant")
    lock = threading.Lock()  # one turn at a time; the store is a single SQLite connection

    @app.get("/", response_class=HTMLResponse)
    def index() -> str:
        return (WEB_DIR / "index.html").read_text(encoding="utf-8")

    @app.get("/api/config")
    def get_config() -> dict[str, Any]:
        return {
            "model": config.model,
            "effort": config.effort,
            "user_name": config.user_name,
            "google": getattr(google, "email", "") or "" if google else None,
            "session_id": store.latest_session() or store.create_session(),
        }

    @app.post("/api/sessions")
    def new_session() -> dict[str, str]:
        return {"session_id": store.create_session()}

    @app.get("/api/sessions/{session_id}/messages")
    def session_messages(session_id: str) -> list[dict[str, Any]]:
        """Chat transcript as simple user/assistant text turns (tool traffic omitted)."""
        turns: list[dict[str, Any]] = []
        for msg in store.get_messages(session_id):
            content = msg["content"]
            if msg["role"] == "user" and isinstance(content, str):
                turns.append({"role": "user", "text": content})
            elif msg["role"] == "assistant":
                text = "".join(b.get("text", "") for b in content if b.get("type") == "text")
                if not text.strip():
                    continue
                if turns and turns[-1]["role"] == "assistant":
                    # One reply can span several assistant messages when tools run mid-turn.
                    turns[-1]["text"] += text
                else:
                    turns.append({"role": "assistant", "text": text})
        return turns

    @app.get("/api/tasks")
    def tasks(status: str = "open") -> list[dict[str, Any]]:
        return store.list_tasks(status)

    @app.get("/api/notes")
    def notes(q: str = "") -> list[dict[str, Any]]:
        return store.search_notes(q)

    @app.get("/api/events")
    def events() -> list[dict[str, Any]]:
        return store.list_events()

    @app.get("/api/memories")
    def memories() -> list[dict[str, Any]]:
        return store.list_memories()

    @app.get("/api/schedules")
    def schedules(all: bool = False) -> list[dict[str, Any]]:
        return store.list_schedules(enabled_only=not all, include_system=all)

    if config.twilio_account_sid and config.twilio_auth_token:
        from assistant.channels.twilio import TwilioChannel

        twilio = TwilioChannel(
            config.twilio_account_sid,
            config.twilio_auth_token,
            config.twilio_from,
            config.twilio_whatsapp_from,
            set(config.allowed_chat_ids("twilio")),
            config.twilio_webhook_url,
            store,
        )

        def _handle_inbound(msg: Any) -> None:
            if daemon is not None:
                daemon.handle_inbound(msg)
                return
            with lock:
                session_id = store.session_for_chat(msg.channel, msg.chat_id, config.session_idle_hours * 3600)
                assistant = Assistant(
                    store, config, client=client, session_id=session_id, google=google,
                    channel=msg.channel, chat_id=msg.chat_id,
                )
                try:
                    result = assistant.chat(msg.text)
                    reply = result.text or result.refusal or ""
                except AssistantError as exc:
                    reply = f"Sorry, something went wrong: {exc}"
            if reply:
                twilio.send(msg.chat_id, reply)

        @app.post("/webhooks/twilio")
        async def twilio_webhook(request: Request) -> Response:
            body = (await request.body()).decode("utf-8", errors="replace")
            form = dict(parse_qsl(body, keep_blank_values=True))
            public_url = config.twilio_webhook_url or str(request.url)
            status, text, message = twilio.handle_webhook(public_url, dict(request.headers), form)
            if message is not None:
                threading.Thread(target=_handle_inbound, args=(message,), daemon=True).start()
            media_type = "text/xml" if text.lstrip().startswith("<") else "text/plain"
            return Response(content=text, status_code=status, media_type=media_type)

    @app.post("/api/chat")
    def chat(req: ChatRequest) -> StreamingResponse:
        if not req.message.strip():
            raise HTTPException(status_code=400, detail="message is empty")

        q: queue.Queue[tuple[str, Any] | None] = queue.Queue()

        def worker() -> None:
            with lock:
                assistant = Assistant(
                    store, config, client=client, session_id=req.session_id, google=google
                )
                try:
                    result = assistant.chat(
                        req.message,
                        on_text=lambda t: q.put(("text", t)),
                        on_tool=lambda name, inp, out, err: q.put(
                            ("tool", {"name": name, "input": inp, "error": err})
                        ),
                    )
                    q.put(
                        (
                            "done",
                            {
                                "session_id": assistant.session_id,
                                "stop_reason": result.stop_reason,
                                "refusal": result.refusal,
                                "text": result.text,
                            },
                        )
                    )
                except AssistantError as exc:
                    q.put(("error", {"message": str(exc)}))
                except Exception as exc:  # noqa: BLE001 - surface unexpected errors to the UI
                    q.put(("error", {"message": f"Unexpected error: {exc}"}))
                finally:
                    q.put(None)

        threading.Thread(target=worker, daemon=True).start()

        def stream() -> Iterator[str]:
            while True:
                item = q.get()
                if item is None:
                    break
                yield _sse(*item)

        return StreamingResponse(stream(), media_type="text/event-stream")

    return app
