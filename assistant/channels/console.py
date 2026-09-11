"""A terminal channel used by `my-assistant demo`: stdin in, stdout out.

`ConsoleVoice` is the matching stand-in for phone calls: it prints what the
assistant would have said on the call instead of dialling anything.
"""

from __future__ import annotations

import queue
import sys
import threading
from typing import Callable, TextIO

from assistant.channels.base import Channel, InboundMessage

CHAT_ID = "you"
QUIT_COMMANDS = {"/quit", "/exit", "/q"}


class ConsoleChannel(Channel):
    name = "console"
    max_len = 4000

    def __init__(
        self,
        stdin: TextIO | None = None,
        stdout: TextIO | None = None,
        on_eof: Callable[[], None] | None = None,
        prompt: str = "you> ",
    ) -> None:
        self.stdin = stdin or sys.stdin
        self.stdout = stdout or sys.stdout
        self.on_eof = on_eof
        self.prompt = prompt
        self.sent: list[tuple[str, str]] = []
        self._counter = 0
        self._lock = threading.Lock()
        self._lines: "queue.Queue[str | None]" = queue.Queue()
        self._reader: threading.Thread | None = None
        self._eof = False

    def send(self, chat_id: str, text: str) -> str:
        with self._lock:
            self._counter += 1
            self.sent.append((chat_id, text))
            self.stdout.write(f"\r[assistant → you] {text}\n{self.prompt}")
            self.stdout.flush()
            return f"console-{self._counter}"

    # A reader thread is the only portable way to wait on stdin with a timeout
    # (select() cannot see lines already buffered inside the text wrapper).
    def _read_loop(self) -> None:
        for line in iter(self.stdin.readline, ""):
            self._lines.put(line)
        self._lines.put(None)

    def _ensure_reader(self) -> None:
        if self._reader is None:
            self._reader = threading.Thread(target=self._read_loop, name="console-stdin", daemon=True)
            self._reader.start()

    def _finish(self) -> None:
        self._eof = True
        if self.on_eof is not None:
            self.on_eof()

    def poll(self, timeout: float = 25) -> list[InboundMessage]:
        if self._eof:
            threading.Event().wait(min(timeout, 1))
            return []
        self._ensure_reader()
        try:
            line = self._lines.get(timeout=timeout)
        except queue.Empty:
            return []
        if line is None:
            self._finish()
            return []
        text = line.strip()
        if not text:
            self.stdout.write(self.prompt)
            self.stdout.flush()
            return []
        if text.lower() in QUIT_COMMANDS:
            self._finish()
            return []
        with self._lock:
            self._counter += 1
            message_id = f"in-{self._counter}"
        return [InboundMessage(channel=self.name, chat_id=CHAT_ID, text=text, message_id=message_id)]

    def is_allowed(self, chat_id: str) -> bool:
        return chat_id == CHAT_ID


class ConsoleVoice:
    """Prints "[assistant CALLS you] ..." where the real daemon would place a call."""

    def __init__(self, stdout: TextIO | None = None, prompt: str = "you> ") -> None:
        self.stdout = stdout or sys.stdout
        self.prompt = prompt
        self.calls: list[tuple[str, str]] = []
        self._lock = threading.Lock()

    def call(self, to_number: str, text: str, repeat: int = 2) -> str:
        with self._lock:
            self.calls.append((to_number, text))
            self.stdout.write(f"\r[assistant CALLS you] \u260e {text}\n{self.prompt}")
            self.stdout.flush()
            return f"console-call-{len(self.calls)}"

    def close(self) -> None:
        return None
