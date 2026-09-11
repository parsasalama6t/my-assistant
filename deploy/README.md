# Running the assistant 24/7

The daemon (`my-assistant daemon`) is what texts you. It has to be running for
scheduled messages and reminders to go out. Two ways to keep it running:

## Option A: a small cloud server (recommended, ~$5/month)

Any 1 vCPU / 1 GB box works (Hetzner CX22, DigitalOcean Basic, Fly.io shared).
Reminders arrive even when your laptop is closed.

1. On the server, install Docker (`curl -fsSL https://get.docker.com | sh`).
2. Copy the repo and your `.env` file to the server (`scp -r my-assistant user@server:`).
3. If you use Google: run `my-assistant google login` on your laptop first (it needs a
   browser), then copy `~/.my-assistant/google_token.json` to `my-assistant/data/` on the server.
4. Start it:

   ```bash
   cd my-assistant/deploy
   docker compose up -d --build
   docker compose logs -f        # you should see "polling telegram"
   ```

5. Send your bot a message. Updates: `git pull && docker compose up -d --build`.

For WhatsApp/SMS via Twilio the server also needs a public HTTPS URL for the webhook.
Simplest: install Caddy and add to `/etc/caddy/Caddyfile`:

```
assistant.yourdomain.com {
    reverse_proxy 127.0.0.1:8081
}
```

then set `TWILIO_WEBHOOK_URL=https://assistant.yourdomain.com/webhooks/twilio` in `.env`
and paste the same URL into the Twilio console (Messaging → your sender → "When a message
comes in"). For a quick test without a domain, `ngrok http 8081` gives a temporary URL.

## Option B: your Mac (free, only while the Mac is awake)

1. Edit `deploy/com.my-assistant.daemon.plist`: replace `YOUR_USERNAME` with your macOS
   username (run `whoami`) and fix the path if the project is not in `~/Documents`.
2. Install and start it:

   ```bash
   cp deploy/com.my-assistant.daemon.plist ~/Library/LaunchAgents/
   launchctl load -w ~/Library/LaunchAgents/com.my-assistant.daemon.plist
   tail -f ~/Library/Logs/my-assistant.log
   ```

3. To stop: `launchctl unload -w ~/Library/LaunchAgents/com.my-assistant.daemon.plist`.

A sleeping Mac does not run the daemon. Reminders missed while asleep are sent when it
wakes if they are less than two hours late (see `ASSISTANT_CATCHUP_GRACE_MINUTES`);
older ones are skipped rather than sent in a burst. If morning briefings matter, use Option A.

## Cron alternative

`my-assistant daemon --once` runs one scheduler pass and exits. Any cron that runs it
every minute will deliver scheduled texts; it will not answer incoming messages, though.
