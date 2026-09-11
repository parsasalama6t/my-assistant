# my-assistant

A personal assistant powered by Claude. It keeps your tasks, notes, calendar
events, and long-term memories in a local SQLite database, and you talk to it
in the terminal or in a small web UI.

What it can do:

- **Tasks** – add, list, update, complete, and delete to-dos with due dates and priorities.
- **Notes** – save and search notes with tags.
- **Google Calendar** – reads your real calendar, creates and deletes events, invites attendees.
- **Gmail** – searches and reads your inbox, drafts replies, and sends email when you ask it to.
- **Local calendar** – a fallback event list when Google isn't connected.
- **Memory** – remembers facts you tell it (preferences, people, routines) across conversations.
- **Briefing** – a daily rundown of what is due and what is coming up.
- **Web search** (optional) – look things up when you turn it on.
- **Conversation history** – picks up where you left off; start fresh any time.

## Setup

Requires Python 3.10+ and an Anthropic API key.

```bash
git clone https://github.com/parsasalama6t/my-assistant
cd my-assistant
python3 -m venv .venv
source .venv/bin/activate            # Windows: .venv\Scripts\activate
pip install -e ".[web,google,dev]"   # or just `pip install -e .` for the CLI only
cp .env.example .env                 # then put your ANTHROPIC_API_KEY in .env
```

On macOS the command is `python3`, not `python`. If `python3` is not found, install it with
`brew install python` or from [python.org](https://www.python.org/downloads/) and rerun the steps.

To add your key, open `.env` in a text editor (`open -e .env` on macOS) and replace `sk-ant-...`
on the `ANTHROPIC_API_KEY=` line. Alternatively export `ANTHROPIC_API_KEY` in your shell.

**Every time you use it** from a new terminal window, activate the environment first:

```bash
cd my-assistant
source .venv/bin/activate
my-assistant
```

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

## Connect Google Calendar and Gmail

The assistant talks to Google with your own OAuth client, so nothing goes through a third party.

1. In [Google Cloud Console](https://console.cloud.google.com/) create a project (or pick one) and
   enable the **Google Calendar API** and the **Gmail API**.
2. Under *APIs & Services → OAuth consent screen*, set up an External app and add your own Google
   account as a test user.
3. Under *Credentials*, create an **OAuth client ID** of type **Desktop app** and download the JSON.
4. Save it as `~/.my-assistant/google_credentials.json` (or point `ASSISTANT_GOOGLE_CREDENTIALS` at it).
5. Run the login once; a browser window opens for consent:

```bash
pip install -e ".[google]"
my-assistant google login
my-assistant google status     # shows the connected account
```

The token is stored at `~/.my-assistant/google_token.json` with owner-only permissions and refreshed
automatically. `my-assistant google logout` removes it. Scopes requested: calendar events
(read/write) and Gmail modify (read, draft, send, mark read). The assistant drafts email by default
and only sends when you explicitly ask.

Once connected, the assistant uses your Google Calendar for scheduling questions and your inbox for
email, and the briefing includes unread mail that looks like it needs action:

```
you> anything from Dana this week?
assistant> One unread email from Dana (Tue): "Kitchen quote" – she's asking whether Friday works
for the site visit. Want me to draft a reply?

you> yes, say Friday at 10 works and put it on my calendar
assistant> Drafted the reply to Dana and added "Site visit – Dana" Friday 10:00–11:00 to your
calendar. Say "send it" when you want the draft to go out.
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
| `ASSISTANT_GOOGLE` | on | Set to `0` to ignore a stored Google login. |
| `ASSISTANT_GOOGLE_CREDENTIALS` | `~/.my-assistant/google_credentials.json` | Path to the OAuth client JSON. |

## How it works

- `assistant/agent.py` – the loop: send the conversation, stream the reply, run any
  tools Claude asks for, feed results back, repeat until Claude is done. Uses adaptive
  thinking and prompt caching on the stable part of the system prompt.
- `assistant/tools.py` – tool definitions and handlers (tasks, notes, events, memory, time,
  and the Google Calendar/Gmail tools, which are only offered to the model when Google is connected).
- `assistant/google_client.py` – OAuth login/token handling and a small facade over the Calendar
  and Gmail APIs.
- `assistant/store.py` – SQLite storage, including full conversation history so
  sessions resume across runs.
- `assistant/prompts.py` – the system prompt; memories are injected every turn.
- `assistant/cli.py` – terminal interface. `assistant/server.py` + `assistant/web/` – web UI.

## Development

```bash
pip install -e ".[web,google,dev]"
pytest
```

Tests use a scripted fake Claude client and a fake Google client, so they run without any
credentials.
