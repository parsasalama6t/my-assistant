"""The agent loop: send the conversation to Claude, run tools, repeat until done."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

import anthropic

from assistant.config import Config
from assistant.prompts import build_system
from assistant.store import Store
from assistant.tools import run_tool, tool_definitions

TextCallback = Callable[[str], None]
ToolCallback = Callable[[str, dict[str, Any], str, bool], None]

# Server-side refusal fallbacks: the API re-runs a declined request on a
# fallback model inside the same call. Only some models accept the parameter.
FALLBACK_BETA = "server-side-fallback-2026-07-01"
FALLBACK_MODELS = {"claude-opus-5", "claude-fable-5-1", "claude-fable-5"}

MAX_TOOL_ROUNDS = 25


class AssistantError(RuntimeError):
    """Raised when a turn cannot be completed."""


@dataclass
class TurnResult:
    text: str
    stop_reason: str | None
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    refusal: str | None = None


def _serialize_blocks(content: list[Any]) -> list[dict[str, Any]]:
    """Turn response content blocks into plain dicts we can store and replay.

    Thinking blocks (with their signatures) are kept so the conversation can be
    continued on the same model; the API drops them silently on other models.
    """
    return [block.model_dump(mode="json", exclude_none=True) for block in content]


class Assistant:
    def __init__(
        self,
        store: Store,
        config: Config,
        client: Any | None = None,
        session_id: str | None = None,
    ) -> None:
        self.store = store
        self.config = config
        self.client = client or anthropic.Anthropic()
        self.session_id = session_id or store.latest_session() or store.create_session()

    # ------------------------------------------------------------ sessions
    def new_session(self, title: str = "") -> str:
        self.session_id = self.store.create_session(title)
        return self.session_id

    def history(self) -> list[dict[str, Any]]:
        return self.store.get_messages(self.session_id)

    # ------------------------------------------------------------- request
    def _uses_fallbacks(self) -> bool:
        return self.config.fallbacks and self.config.model in FALLBACK_MODELS

    def _request(self, messages: list[dict[str, Any]]):
        kwargs: dict[str, Any] = dict(
            model=self.config.model,
            max_tokens=self.config.max_tokens,
            system=build_system(self.config.user_name, self.store.list_memories()),
            tools=tool_definitions(web_search=self.config.web_search),
            thinking={"type": "adaptive"},
            output_config={"effort": self.config.effort},
            messages=messages,
        )
        if self._uses_fallbacks():
            return self.client.beta.messages.stream(
                betas=[FALLBACK_BETA], fallbacks="default", **kwargs
            )
        return self.client.messages.stream(**kwargs)

    # ---------------------------------------------------------------- chat
    def chat(
        self,
        user_text: str,
        on_text: TextCallback | None = None,
        on_tool: ToolCallback | None = None,
    ) -> TurnResult:
        """Send one user message and drive the tool loop to completion.

        `on_text` receives streamed text fragments as they arrive; `on_tool`
        is called after each tool execution with (name, input, result, is_error).
        """
        user_text = user_text.strip()
        if not user_text:
            raise AssistantError("message is empty")

        messages = self.history()
        messages.append({"role": "user", "content": user_text})
        self.store.append_message(self.session_id, "user", user_text)

        result = TurnResult(text="", stop_reason=None)
        collected: list[str] = []

        for _ in range(MAX_TOOL_ROUNDS):
            try:
                with self._request(messages) as stream:
                    for event in stream:
                        if (
                            event.type == "content_block_delta"
                            and event.delta.type == "text_delta"
                        ):
                            collected.append(event.delta.text)
                            if on_text:
                                on_text(event.delta.text)
                    response = stream.get_final_message()
            except anthropic.AuthenticationError as exc:
                raise AssistantError(
                    "Authentication failed. Set ANTHROPIC_API_KEY (see .env.example)."
                ) from exc
            except TypeError as exc:
                # The SDK raises TypeError when no credential source resolves at all.
                if "authentication" not in str(exc).lower():
                    raise
                raise AssistantError(
                    "No API key found. Set ANTHROPIC_API_KEY in your environment or .env file."
                ) from exc
            except anthropic.RateLimitError as exc:
                retry_after = exc.response.headers.get("retry-after", "a bit")
                raise AssistantError(f"Rate limited; try again in {retry_after} seconds.") from exc
            except anthropic.APIStatusError as exc:
                raise AssistantError(f"API error ({exc.status_code}): {exc.message}") from exc
            except anthropic.APIConnectionError as exc:
                raise AssistantError("Could not reach the Claude API. Check your network.") from exc

            usage = response.usage
            result.input_tokens += usage.input_tokens or 0
            result.output_tokens += usage.output_tokens or 0
            result.cache_read_tokens += getattr(usage, "cache_read_input_tokens", 0) or 0

            assistant_blocks = _serialize_blocks(response.content)
            messages.append({"role": "assistant", "content": assistant_blocks})
            self.store.append_message(self.session_id, "assistant", assistant_blocks)
            result.stop_reason = response.stop_reason

            if response.stop_reason == "refusal":
                details = getattr(response, "stop_details", None)
                result.refusal = (
                    getattr(details, "explanation", None) or "The model declined this request."
                )
                break

            if response.stop_reason == "pause_turn":
                # A server-side tool paused mid-turn; resend to let it continue.
                continue

            if response.stop_reason != "tool_use":
                break

            tool_results: list[dict[str, Any]] = []
            for block in response.content:
                if block.type != "tool_use":
                    continue
                output, is_error = run_tool(self.store, block.name, dict(block.input or {}))
                result.tool_calls.append(
                    {"name": block.name, "input": block.input, "output": output, "error": is_error}
                )
                if on_tool:
                    on_tool(block.name, dict(block.input or {}), output, is_error)
                entry: dict[str, Any] = {
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": output,
                }
                if is_error:
                    entry["is_error"] = True
                tool_results.append(entry)

            if not tool_results:
                break
            messages.append({"role": "user", "content": tool_results})
            self.store.append_message(self.session_id, "user", tool_results)
        else:
            raise AssistantError("Stopped after too many tool calls in one turn.")

        result.text = "".join(collected).strip()
        if result.refusal and not result.text:
            result.text = result.refusal
        return result
