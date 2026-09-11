"""Google Calendar and Gmail access for the assistant.

Authentication uses a Google Cloud OAuth "Desktop app" client. The user puts the
downloaded client file at `<data_dir>/google_credentials.json` (or points
ASSISTANT_GOOGLE_CREDENTIALS at it) and runs `my-assistant google login` once;
the resulting token is stored at `<data_dir>/google_token.json`.

The Google client libraries are imported lazily so the rest of the assistant
works without them installed.
"""

from __future__ import annotations

import base64
import re
from datetime import datetime, timedelta, tzinfo
from email.message import EmailMessage
from email.utils import parseaddr
from pathlib import Path
from typing import Any

SCOPES = [
    "https://www.googleapis.com/auth/calendar.events",
    "https://www.googleapis.com/auth/gmail.modify",
]

_MISSING_LIBS_MSG = (
    "Google integration needs extra packages: pip install 'my-assistant[google]'"
)


class GoogleNotConnected(RuntimeError):
    """Raised when a Google tool is used before `my-assistant google login`."""


def _local_tz(tz: tzinfo | None = None) -> tzinfo:
    """The configured zone, or the OS-local zone when none was given."""
    return tz or datetime.now().astimezone().tzinfo  # type: ignore[return-value]


def _to_rfc3339(value: str, tz: tzinfo | None = None) -> str:
    """Accept 'YYYY-MM-DD', 'YYYY-MM-DDTHH:MM[:SS]' (wall time in `tz`) or a full RFC 3339 string."""
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=_local_tz(tz))
    return dt.isoformat()


def _event_time(value: str, tz: tzinfo | None = None) -> dict[str, str]:
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        return {"date": value}
    return {"dateTime": _to_rfc3339(value, tz)}


def _header(headers: list[dict[str, str]], name: str) -> str:
    for h in headers:
        if h.get("name", "").lower() == name.lower():
            return h.get("value", "")
    return ""


def _decode_body(data: str) -> str:
    return base64.urlsafe_b64decode(data.encode("ascii") + b"==").decode("utf-8", errors="replace")


def _extract_text(payload: dict[str, Any]) -> str:
    """Prefer text/plain; fall back to a crude strip of text/html."""
    plain: list[str] = []
    html: list[str] = []

    def walk(part: dict[str, Any]) -> None:
        mime = part.get("mimeType", "")
        body = part.get("body", {}) or {}
        data = body.get("data")
        if data and mime == "text/plain":
            plain.append(_decode_body(data))
        elif data and mime == "text/html":
            html.append(_decode_body(data))
        for child in part.get("parts", []) or []:
            walk(child)

    walk(payload)
    if plain:
        return "\n".join(plain)
    if html:
        text = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", "\n".join(html), flags=re.S | re.I)
        text = re.sub(r"<br\s*/?>|</p>|</div>", "\n", text, flags=re.I)
        text = re.sub(r"<[^>]+>", " ", text)
        return re.sub(r"[ \t]+", " ", text).strip()
    return ""


class GoogleClient:
    """Small, testable facade over the Calendar v3 and Gmail v1 APIs."""

    def __init__(
        self, calendar_service: Any, gmail_service: Any, email: str = "", tz: tzinfo | None = None
    ) -> None:
        self._calendar = calendar_service
        self._gmail = gmail_service
        self.email = email
        self.tz = tz  # None -> interpret naive times in the OS-local zone

    # ------------------------------------------------------------- auth
    @staticmethod
    def token_path(data_dir: Path) -> Path:
        return data_dir / "google_token.json"

    @staticmethod
    def is_connected(data_dir: Path) -> bool:
        return GoogleClient.token_path(data_dir).is_file()

    @classmethod
    def login(cls, data_dir: Path, credentials_path: Path, tz: tzinfo | None = None) -> "GoogleClient":
        """Run the browser OAuth flow and store the token."""
        try:
            from google_auth_oauthlib.flow import InstalledAppFlow
        except ImportError as exc:
            raise RuntimeError(_MISSING_LIBS_MSG) from exc
        if not credentials_path.is_file():
            raise FileNotFoundError(
                f"OAuth client file not found at {credentials_path}. Create a Desktop-app "
                "OAuth client in Google Cloud Console, download its JSON, and save it there."
            )
        flow = InstalledAppFlow.from_client_secrets_file(str(credentials_path), SCOPES)
        creds = flow.run_local_server(port=0)
        data_dir.mkdir(parents=True, exist_ok=True)
        token = cls.token_path(data_dir)
        token.write_text(creds.to_json(), encoding="utf-8")
        token.chmod(0o600)
        return cls.from_credentials(creds, tz=tz)

    @classmethod
    def load(cls, data_dir: Path, tz: tzinfo | None = None) -> "GoogleClient":
        """Load the stored token, refreshing it if needed."""
        try:
            from google.auth.transport.requests import Request
            from google.oauth2.credentials import Credentials
        except ImportError as exc:
            raise RuntimeError(_MISSING_LIBS_MSG) from exc
        token = cls.token_path(data_dir)
        if not token.is_file():
            raise GoogleNotConnected("Google is not connected. Run: my-assistant google login")
        creds = Credentials.from_authorized_user_file(str(token), SCOPES)
        if not creds.valid:
            if creds.expired and creds.refresh_token:
                creds.refresh(Request())
                token.write_text(creds.to_json(), encoding="utf-8")
            else:
                raise GoogleNotConnected("Google token is invalid. Run: my-assistant google login")
        return cls.from_credentials(creds, tz=tz)

    @classmethod
    def from_credentials(cls, creds: Any, tz: tzinfo | None = None) -> "GoogleClient":
        from googleapiclient.discovery import build

        calendar = build("calendar", "v3", credentials=creds, cache_discovery=False)
        gmail = build("gmail", "v1", credentials=creds, cache_discovery=False)
        client = cls(calendar, gmail, tz=tz)
        try:
            client.email = gmail.users().getProfile(userId="me").execute().get("emailAddress", "")
        except Exception:  # noqa: BLE001 - profile lookup is best-effort
            client.email = ""
        return client

    # --------------------------------------------------------- calendar
    def list_events(
        self, start: str | None = None, end: str | None = None, limit: int = 25
    ) -> list[dict[str, Any]]:
        now = datetime.now(_local_tz(self.tz))
        time_min = _to_rfc3339(start, self.tz) if start else now.isoformat()
        time_max = _to_rfc3339(end, self.tz) if end else (now + timedelta(days=14)).isoformat()
        result = (
            self._calendar.events()
            .list(
                calendarId="primary",
                timeMin=time_min,
                timeMax=time_max,
                singleEvents=True,
                orderBy="startTime",
                maxResults=limit,
            )
            .execute()
        )
        return [self._simplify_event(e) for e in result.get("items", [])]

    def create_event(
        self,
        title: str,
        start: str,
        end: str | None = None,
        location: str = "",
        description: str = "",
        attendees: list[str] | None = None,
    ) -> dict[str, Any]:
        start_block = _event_time(start, self.tz)
        if end:
            end_block = _event_time(end, self.tz)
        elif "date" in start_block:
            end_block = dict(start_block)
        else:
            end_dt = datetime.fromisoformat(start_block["dateTime"]) + timedelta(hours=1)
            end_block = {"dateTime": end_dt.isoformat()}
        body: dict[str, Any] = {"summary": title, "start": start_block, "end": end_block}
        if location:
            body["location"] = location
        if description:
            body["description"] = description
        if attendees:
            body["attendees"] = [{"email": a} for a in attendees]
        created = self._calendar.events().insert(calendarId="primary", body=body).execute()
        return self._simplify_event(created)

    def delete_event(self, event_id: str) -> None:
        self._calendar.events().delete(calendarId="primary", eventId=event_id).execute()

    @staticmethod
    def _simplify_event(e: dict[str, Any]) -> dict[str, Any]:
        start = e.get("start", {}) or {}
        end = e.get("end", {}) or {}
        return {
            "id": e.get("id"),
            "title": e.get("summary", "(no title)"),
            "start": start.get("dateTime") or start.get("date"),
            "end": end.get("dateTime") or end.get("date"),
            "all_day": "date" in start,
            "location": e.get("location", ""),
            "description": (e.get("description") or "")[:500],
            "attendees": [a.get("email") for a in e.get("attendees", []) or []],
            "link": e.get("htmlLink", ""),
        }

    # ------------------------------------------------------------ gmail
    def search_messages(self, query: str = "", limit: int = 10) -> list[dict[str, Any]]:
        listing = (
            self._gmail.users()
            .messages()
            .list(userId="me", q=query or None, maxResults=limit)
            .execute()
        )
        out: list[dict[str, Any]] = []
        for ref in listing.get("messages", []) or []:
            msg = (
                self._gmail.users()
                .messages()
                .get(
                    userId="me",
                    id=ref["id"],
                    format="metadata",
                    metadataHeaders=["From", "To", "Subject", "Date"],
                )
                .execute()
            )
            out.append(self._simplify_message(msg))
        return out

    def get_message(self, message_id: str, max_chars: int = 6000) -> dict[str, Any]:
        msg = self._gmail.users().messages().get(userId="me", id=message_id, format="full").execute()
        simple = self._simplify_message(msg)
        body = _extract_text(msg.get("payload", {}) or {})
        if len(body) > max_chars:
            body = body[:max_chars] + f"\n…[truncated, {len(body) - max_chars} more characters]"
        simple["body"] = body
        return simple

    def create_draft(
        self, to: list[str], subject: str, body: str, reply_to_message_id: str | None = None
    ) -> dict[str, Any]:
        raw, thread_id = self._build_raw(to, subject, body, reply_to_message_id)
        message: dict[str, Any] = {"raw": raw}
        if thread_id:
            message["threadId"] = thread_id
        draft = self._gmail.users().drafts().create(userId="me", body={"message": message}).execute()
        return {"draft_id": draft.get("id"), "to": to, "subject": subject}

    def send_message(
        self, to: list[str], subject: str, body: str, reply_to_message_id: str | None = None
    ) -> dict[str, Any]:
        raw, thread_id = self._build_raw(to, subject, body, reply_to_message_id)
        message: dict[str, Any] = {"raw": raw}
        if thread_id:
            message["threadId"] = thread_id
        sent = self._gmail.users().messages().send(userId="me", body=message).execute()
        return {"message_id": sent.get("id"), "to": to, "subject": subject}

    def mark_read(self, message_id: str) -> None:
        self._gmail.users().messages().modify(
            userId="me", id=message_id, body={"removeLabelIds": ["UNREAD"]}
        ).execute()

    def _build_raw(
        self, to: list[str], subject: str, body: str, reply_to_message_id: str | None
    ) -> tuple[str, str | None]:
        if not to:
            raise ValueError("at least one recipient is required")
        for addr in to:
            if "@" not in parseaddr(addr)[1]:
                raise ValueError(f"invalid email address: {addr}")
        msg = EmailMessage()
        msg["To"] = ", ".join(to)
        msg["Subject"] = subject
        if self.email:
            msg["From"] = self.email
        thread_id: str | None = None
        if reply_to_message_id:
            original = (
                self._gmail.users()
                .messages()
                .get(
                    userId="me",
                    id=reply_to_message_id,
                    format="metadata",
                    metadataHeaders=["Message-ID", "References"],
                )
                .execute()
            )
            headers = original.get("payload", {}).get("headers", []) or []
            original_id = _header(headers, "Message-ID")
            if original_id:
                msg["In-Reply-To"] = original_id
                refs = _header(headers, "References")
                msg["References"] = f"{refs} {original_id}".strip()
            thread_id = original.get("threadId")
        msg.set_content(body)
        raw = base64.urlsafe_b64encode(msg.as_bytes()).decode("ascii")
        return raw, thread_id

    @staticmethod
    def _simplify_message(msg: dict[str, Any]) -> dict[str, Any]:
        headers = msg.get("payload", {}).get("headers", []) or []
        labels = msg.get("labelIds", []) or []
        return {
            "id": msg.get("id"),
            "thread_id": msg.get("threadId"),
            "from": _header(headers, "From"),
            "to": _header(headers, "To"),
            "subject": _header(headers, "Subject"),
            "date": _header(headers, "Date"),
            "snippet": msg.get("snippet", ""),
            "unread": "UNREAD" in labels,
        }
