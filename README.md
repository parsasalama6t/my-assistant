# my-assistant

A personal assistant powered by Claude that reaches you first. It texts you a
briefing every morning, reminds you before appointments, does what you say in
passing ("text me at 3 to call mom"), works your Gmail and Google Calendar, and
checks in at night. You talk to it on your phone over Telegram (or WhatsApp/SMS
via Twilio), in the terminal, or in a small web UI.

**Demo before you connect anything:** run `my-assistant demo` (no keys needed) to
see the real scheduler and message routing with scripted replies.

## What it does

- **Morning briefing and evening review** at times you choose. Calendar, due tasks,
  emails that need a reply, what got done, what's first tomorrow.
- **Reminders that cost nothing.** A text before every calendar event and at every
  task's due time. These never call the model.
- **Say it once.** "Text me at 3 to call mom." "Every weekday 6am remind me to
  stretch." "Move standup to Monday." The assistant schedules, edits, and cancels.
- **It calls you when it matters.** Every reminder has a priority: *normal* texts;
  *important* texts, then phones you if you haven't replied in 10 minutes; *critical*
  calls right away. "Call me at 5:45 to wake me for the flight" just works.
- **Gmail and Google Calendar.** Search and read mail, draft replies, send only when
  you say so; create, move, and delete events with attendees.
- **Memory.** Remembers people, preferences, and routines across conversations.
- **Tasks and notes** kept locally in SQLite.
- **Always on.** A daemon runs the schedule and answers messages 24/7 on a $5 cloud
  server, or on your Mac while it's awake.

## Quick start (Telegram, about 10 minutes)

Requires Python 3.10+ and an Anthropic API key.

```bash
git clone https://github.com/parsasalama6t/my-assistant
cd my-assistant
python3 -m venv .venv
source .venv/bin/activate            # Windows: .venv\Scripts\activate
pip install -e ".[web,google]"
my-assistant setup                   # interactive: writes .env for you
```

The setup wizard asks for:

1. **Anthropic API key** from https://console.anthropic.com/ (the brain; billed by usage).
2. **Telegram bot token.** In Telegram, message **@BotFather**, send `/newbot`, follow the
   prompts, copy the token.
3. **Your chat id.** Send your new bot `/start`; the wizard captures the id (or run
   `my-assistant channels whoami`). Only that id is ever answered.
4. Your name, timezone (default America/Toronto), and briefing times.

Then:

```bash
my-assistant channels test           # you get a test message on your phone
my-assistant daemon                  # start the always-on assistant
```

Message your bot. Leave the daemon running (see [deploy/README.md](deploy/README.md)
for running it 24/7).

On macOS the command is `python3`, not `python`. If `python3` is not found, install it
with `brew install python` or from [python.org](https://www.python.org/downloads/).
In every new terminal window, `cd` into the folder and run `source .venv/bin/activate`
before using `my-assistant`.

## Commands

```bash
my-assistant                     # chat in the terminal (same as `my-assistant chat`)
my-assistant chat --new          # fresh conversation
my-assistant ask "what's on this week?"
my-assistant briefing [--send]   # print today's briefing, or text it to you
my-assistant serve               # web UI at http://127.0.0.1:8000
my-assistant demo                # scripted end-to-end demo, no credentials

my-assistant daemon [--once]     # always-on: answers messages and sends scheduled texts
my-assistant channels status     # what's configured, default target, timezone
my-assistant channels test       # send yourself a test message (--call to test a phone call)
my-assistant channels whoami     # discover your Telegram chat id

my-assistant schedule list [--all]
my-assistant schedule add "Call mom" --at 2026-09-12T15:00 [--repeat weekdays] [--priority important]
my-assistant schedule cancel ID
my-assistant schedule sync       # rebuild task/event reminders now
my-assistant schedule deliveries # what was sent, skipped, or failed

my-assistant google login|status|logout
my-assistant tasks [--all] · notes [query] · events · memories · sessions
```

On the phone: `/new` starts a fresh conversation, `/tasks`, `/schedule`, `/memories`
show quick lists, `/id` shows your chat id, `/help` lists commands.

## Connect Google Calendar and Gmail (optional, ~15 minutes)

1. In [Google Cloud Console](https://console.cloud.google.com/) create or pick a project
   and enable the **Google Calendar API** and **Gmail API**.
2. *APIs & Services → OAuth consent screen*: External, add your own Google account as a
   test user.
3. *Credentials → Create credentials → OAuth client ID → Desktop app*. Download the JSON
   and save it as `~/.my-assistant/google_credentials.json`.
4. `my-assistant google login` (a browser opens once), then `my-assistant google status`.

The token is stored at `~/.my-assistant/google_token.json` (owner-only) and refreshes
itself. Scopes: calendar events (read/write) and Gmail modify (read, draft, send, mark
read). The assistant drafts email by default and sends only when you explicitly ask.

## Phone calls (optional, needs Twilio)

Telegram cannot place calls, so voice goes through [Twilio](https://www.twilio.com/)
whichever chat channel you use. You need `TWILIO_ACCOUNT_SID`, `TWILIO_AUTH_TOKEN`, a
Twilio phone number in `TWILIO_VOICE_FROM` (or `TWILIO_FROM`), and your own number in
`USER_PHONE`. A free trial account can call your own verified number (with a short trial
notice at the start); upgrading removes it. Test with `my-assistant channels test --call`.

The call reads the message aloud twice and hangs up. Replying to the text that preceded an
*important* reminder cancels the pending call. Set the wait with
`ASSISTANT_ESCALATE_MINUTES` (default 10).

## WhatsApp or SMS instead of Telegram (optional)

Both go through [Twilio](https://www.twilio.com/). Set `TWILIO_ACCOUNT_SID`,
`TWILIO_AUTH_TOKEN`, `USER_PHONE` (your number, E.164), and for SMS a Twilio number in
`TWILIO_FROM`. WhatsApp uses the Twilio sandbox sender by default: in the Twilio console
join the sandbox from your phone (`join <word>`), then messages flow.

Incoming messages need a public HTTPS webhook (`TWILIO_WEBHOOK_URL`); see
[deploy/README.md](deploy/README.md). Two things to know:

- **WhatsApp 24-hour rule.** Free-form messages are only allowed within 24 hours of your
  last message to the bot. A 07:30 briefing needs an approved template if you did not
  text the day before; the daemon reports such failures in `schedule deliveries`.
- **Production WhatsApp** (your own number instead of the sandbox) requires Meta business
  verification, which takes days to weeks. Telegram has none of this.

## Running it 24/7

See [deploy/README.md](deploy/README.md): Docker on a ~$5/month server (recommended;
reminders arrive when your laptop is closed) or a macOS launch agent (free; only while
the Mac is awake).

## What it costs

Usage-based. Rough figures in USD for 20 messages a day, 2 briefings, and 8 fixed
reminders, with prompt caching on:

| Item | Per unit | Per day | Per week | Per month |
|---|---|---|---|---|
| Claude Opus 5 (default) | ~$0.025 per message, ~$0.11 per briefing | ~$0.75 | ~$5 | ~$22 |
| Claude Sonnet 5 (`ASSISTANT_MODEL=claude-sonnet-5`) | ~$0.010 per message | ~$0.30 | ~$2 | ~$9 |
| Telegram | free | $0 | $0 | $0 |
| WhatsApp via Twilio | $0.005 Twilio fee per message each way + Meta $0.004 per scheduled template | ~$0.19 | ~$1.30 | ~$5.70 |
| SMS (Canada) via Twilio | $0.0079 per 160-char segment each way + ~$1.15/month number | ~$0.55 | ~$3.80 | ~$17.50 |
| Phone calls via Twilio | ~$0.015 per minute (a reminder call rounds to 1 minute) + ~$1.15/month number | | | ~$1.25 for 10 calls |
| Cloud server | | | | ~$5 |
| Your Mac | | | | $0 |

Fixed reminders never call the model. Your real Claude spend shows in the Anthropic
console; `my-assistant demo` and all `schedule`/`channels` commands are free.

## Configuration

Everything is read from environment variables; a `.env` file in the working directory is
loaded automatically. `my-assistant setup` writes it. See `.env.example` for the full list.

| Variable | Default | Meaning |
|---|---|---|
| `ANTHROPIC_API_KEY` | – | Required. |
| `ASSISTANT_MODEL` | `claude-opus-5` | Model. `claude-sonnet-5` is the budget option. |
| `ASSISTANT_EFFORT` | `medium` | `low` … `max`. Higher is slower and costs more. |
| `ASSISTANT_USER_NAME` | – | How the assistant addresses you. |
| `ASSISTANT_TIMEZONE` | `America/Toronto` | IANA zone for all dates, reminders, and briefings. |
| `ASSISTANT_MORNING_BRIEFING` | `07:30` | Local time; empty disables. |
| `ASSISTANT_EVENING_REVIEW` | `21:00` | Local time; empty disables. |
| `ASSISTANT_EVENT_LEAD_MINUTES` | `15` | Text this long before calendar events. |
| `ASSISTANT_TASK_LEAD_MINUTES` | `0` | Text this long before a task's due time. |
| `ASSISTANT_CATCHUP_GRACE_MINUTES` | `120` | Missed recurring reminders older than this are skipped, not sent late. |
| `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` | – | Telegram bot and the only chat it answers. |
| `TWILIO_ACCOUNT_SID`, `TWILIO_AUTH_TOKEN`, `TWILIO_FROM`, `TWILIO_WHATSAPP_FROM`, `TWILIO_WEBHOOK_URL`, `USER_PHONE` | – | WhatsApp/SMS. |
| `TWILIO_VOICE_FROM`, `ASSISTANT_ESCALATE_MINUTES` | `TWILIO_FROM`, `10` | Phone calls and the text-to-call wait. |
| `ASSISTANT_DATA_DIR` | `~/.my-assistant` | Database and tokens. |
| `ASSISTANT_WEB_SEARCH` | off | `1` lets the assistant search the web. |
| `ASSISTANT_FALLBACKS` | on | Server-side refusal fallbacks; `0` disables. |

## How it works

- `assistant/agent.py` – the loop: send the conversation, stream the reply, run the tools
  Claude asks for, feed results back. Adaptive thinking, prompt caching on the stable
  system prompt, per-chat sessions.
- `assistant/scheduler.py` + `schedule_rules.py` – timezone-aware schedules in SQLite; a
  claim-then-send tick that cannot double-send; catch-up policy after downtime; reminders
  derived from tasks, local events, and Google Calendar.
- `assistant/daemon.py` – threads for channel polling, the scheduler, and a single worker
  that routes incoming messages to the agent.
- `assistant/channels/` – Telegram (long polling) and Twilio (REST + signed webhook);
  `assistant/voice.py` – outbound calls that read a message aloud.
- `assistant/tools.py` – tasks, notes, events, memory, time, scheduling, and the Google
  tools (offered to the model only when connected).
- `assistant/google_client.py` – OAuth and a small facade over Calendar and Gmail.
- `assistant/cli.py`, `assistant/server.py` + `assistant/web/` – terminal and web UIs.

## Development

```bash
pip install -e ".[web,google,dev]"
pytest
```

Tests use scripted fakes for Claude, Google, Telegram, and Twilio, so they run without
any credentials or network.
