# my-assistant

A personal assistant powered by Claude. It keeps your tasks, notes, calendar
events, and long-term memories in a local SQLite database, and you talk to it
in the terminal or in a small web UI.

What it can do:

- **Tasks** – add, list, update, complete, and delete to-dos with due dates and priorities.
- **Notes** – save and search notes with tags.
- **Calendar** – add and list events and appointments.
- **Memory** – remembers facts you tell it (preferences, people, routines) across conversations.
- **Briefing** – a daily rundown of what is due and what is coming up.
- **Web search** (optional) – look things up when you turn it on.
- **Conversation history** – picks up where you left off; start fresh any time.

## Setup

Requires Python 3.10+ and an Anthropic API key.

```bash
git clone https://github.com/parsasalama6t/my-assistant
cd my-assistant
python -m venv .venv && source .venv/bin/activate
pip install -e ".[web,dev]"      # or just `pip install -e .` for the CLI only
cp .env.example .env             # then put your ANTHROPIC_API_KEY in .env
```

Alternatively export `ANTHROPIC_API_KEY` in your shell instead of using `.env`.

## Use it

```bash
my-assistant                     # interactive chat (same as `my-assistant chat`)
my-assistant chat --new          # start a fresh conversation
my-assistant ask "what's on my plate this week?"
my-assistant briefing            # today's briefing
my-assistant serve               # web UI at http://127.0.0.1:8000
```

Quick views that don't call the API:

```bash
my-assistant tasks [--all]
my-assistant notes [search terms]
my-assistant events
my-assistant memories
my-assistant sessions
```

Inside chat, `/tasks`, `/notes`, `/events`, `/memories`, `/new`, `/help`, and
`/quit` work as shortcuts.

Example conversation:

```
you> remind me to renew my passport by the end of the month, it's high priority
assistant> Added "Renew passport", due September 30, high priority.

you> I usually work out Tuesday and Thursday mornings
assistant> Got it, I'll keep that in mind when we plan things.

you> what's on for tomorrow?
assistant> Tomorrow (Friday, Sept 11):
- 10:00 Standup
- Renew passport is still open, due Sept 30
```

## Configuration

Everything is read from environment variables (a `.env` file in the working
directory is loaded automatically). See `.env.example`.

| Variable | Default | Meaning |
|---|---|---|
| `ANTHROPIC_API_KEY` | – | Required. |
| `ASSISTANT_MODEL` | `claude-opus-5` | Model to use. |
| `ASSISTANT_EFFORT` | `medium` | `low`, `medium`, `high`, `xhigh`, or `max`. Higher is slower and costs more. |
| `ASSISTANT_USER_NAME` | – | How the assistant addresses you. |
| `ASSISTANT_DATA_DIR` | `~/.my-assistant` | Where the database lives. |
| `ASSISTANT_WEB_SEARCH` | off | Set to `1` to enable web search. |
| `ASSISTANT_FALLBACKS` | on | Server-side refusal fallbacks: if the model declines a request for safety reasons, the API retries it on a fallback model in the same call. Set to `0` to disable. |
| `ASSISTANT_MAX_TOKENS` | `16000` | Max output tokens per response. |

## How it works

- `assistant/agent.py` – the loop: send the conversation, stream the reply, run any
  tools Claude asks for, feed results back, repeat until Claude is done. Uses adaptive
  thinking and prompt caching on the stable part of the system prompt.
- `assistant/tools.py` – tool definitions and handlers (tasks, notes, events, memory, time).
- `assistant/store.py` – SQLite storage, including full conversation history so
  sessions resume across runs.
- `assistant/prompts.py` – the system prompt; memories are injected every turn.
- `assistant/cli.py` – terminal interface. `assistant/server.py` + `assistant/web/` – web UI.

## Development

```bash
pip install -e ".[web,dev]"
pytest
```

Tests use a scripted fake client, so they run without an API key.
