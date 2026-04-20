# Physio Agent — Telegram Bot

A Telegram bot that tracks your physiotherapy exercises, collects pain/injury notes in natural language, and generates reports for your physio.

## What it does

- Sends you **4 reminders per day** via Telegram (morning, midday, evening, bedtime)
- Stops reminding once you confirm exercises are done
- Lets you describe symptoms naturally — "my left calf was tight this morning but eased after a shower"
- Uses AI (Claude) to extract structured data: body area, timing, triggers, what helped
- Builds a picture over time you can show your physio
- Generates a clinical-style report on demand

---

## Setup — 10 minutes

### 1. Create your Telegram bot

1. Open Telegram, search for **@BotFather**
2. Send `/newbot`
3. Give it a name (e.g. "My Physio Bot")
4. Give it a username ending in `bot` (e.g. `jamesphysio_bot`)
5. Copy the token it gives you — looks like `123456789:ABCdef...`

### 2. Get an Anthropic API key

1. Go to https://console.anthropic.com
2. Sign up / log in
3. Create an API key — free tier is sufficient for personal use

### 3. Install dependencies

```bash
pip install python-telegram-bot anthropic apscheduler
```

### 4. Run the bot

```bash
TELEGRAM_TOKEN=your_token_here ANTHROPIC_API_KEY=your_key_here python3 bot.py
```

### 5. Register with the bot

Open Telegram, find your bot, and send `/start`. This registers your chat ID so you receive reminders.

---

## Customise reminder times

Set these environment variables (default shown):

| Variable | Default | Description |
|---|---|---|
| `MORNING_HOUR` | `7` | Morning reminder (7:00am) |
| `CHECKIN_HOUR` | `13` | Midday pain check-in (1:00pm) |
| `EVENING_HOUR` | `18` | Evening reminder (6:00pm) |
| `BEDTIME_HOUR` | `22` | Bedtime check-in (10:00pm) |
| `TZ` | `Europe/London` | Your timezone |

Example with custom times:
```bash
TELEGRAM_TOKEN=xxx ANTHROPIC_API_KEY=yyy MORNING_HOUR=8 EVENING_HOUR=17 BEDTIME_HOUR=21 python3 bot.py
```

---

## Adding your exercise routine

In Telegram, send messages in this format:
```
ADD: Hip flexor stretch, 3, 30s hold, daily
ADD: Calf raises, 3, 15 reps, daily
ADD: Glute bridge, 3, 12 reps, daily
ADD: Hamstring curl, 2, 10 reps, 5x per week
```

To remove: `REMOVE: Calf raises`

View your routine: `/routine`

---

## Commands

| Command | Description |
|---|---|
| `/start` | Register with the bot |
| `/routine` | View or manage your exercise routine |
| `/done` | Log today's exercises as complete |
| `/checkin` | Prompted pain/injury note |
| `/status` | Today's summary |
| `/report` | Generate a full AI physio report |

You can also just **message naturally** at any time — the bot detects pain-related language and logs it automatically.

---

## Data

Everything is stored in `data.db` (SQLite) in the same directory as the bot. Tables:

- `exercises` — your routine
- `exercise_logs` — daily completion records
- `injury_logs` — pain notes with AI-extracted structure
- `users` — registered Telegram chat IDs

---

## Running continuously (optional)

To keep the bot running in the background on a Mac or Linux machine:

**Using nohup:**
```bash
nohup TELEGRAM_TOKEN=xxx ANTHROPIC_API_KEY=yyy python3 bot.py &
```

**Using a systemd service (Linux):**
Create `/etc/systemd/system/physio-bot.service` with your paths and credentials, then `systemctl enable --now physio-bot`.

---

## Privacy

All data stays on your machine. Nothing is sent externally except:
- Messages to Telegram's servers (standard bot API)
- Injury notes sent to Anthropic's API for extraction (anonymised parsing only)
