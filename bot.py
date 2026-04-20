"""
Physio Agent - Telegram Bot
Tracks exercises, collects injury notes, and builds a recovery picture.
Uses PostgreSQL for persistent storage (Railway-compatible).
"""

import json
import os
import re
from contextlib import contextmanager
from datetime import datetime, date
from zoneinfo import ZoneInfo

import anthropic
import psycopg2
import psycopg2.extras
from psycopg2 import pool as pg_pool
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

# ── Config ────────────────────────────────────────────────────────────────────

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "")
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
DATABASE_URL = os.environ.get("DATABASE_URL", "")
TIMEZONE = ZoneInfo(os.environ.get("TZ", "Europe/London"))

MORNING_HOUR = int(os.environ.get("MORNING_HOUR", "7"))
EVENING_HOUR = int(os.environ.get("EVENING_HOUR", "18"))
BEDTIME_HOUR = int(os.environ.get("BEDTIME_HOUR", "22"))
CHECKIN_HOUR = int(os.environ.get("CHECKIN_HOUR", "13"))

# ── Database ──────────────────────────────────────────────────────────────────

_pool = None

def get_pool():
    global _pool
    if _pool is None:
        _pool = pg_pool.SimpleConnectionPool(1, 5, DATABASE_URL)
    return _pool

@contextmanager
def db():
    pool = get_pool()
    con = pool.getconn()
    con.autocommit = False
    try:
        yield con
        con.commit()
    except Exception:
        con.rollback()
        raise
    finally:
        pool.putconn(con)


def init_db():
    with db() as con:
        cur = con.cursor()
        cur.execute("""
            CREATE TABLE IF NOT EXISTS users (
                chat_id     BIGINT PRIMARY KEY,
                username    TEXT,
                created_at  TIMESTAMPTZ DEFAULT NOW()
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS exercises (
                id          SERIAL PRIMARY KEY,
                chat_id     BIGINT,
                name        TEXT,
                sets        INTEGER,
                reps        TEXT,
                frequency   TEXT,
                notes       TEXT,
                active      BOOLEAN DEFAULT TRUE,
                created_at  TIMESTAMPTZ DEFAULT NOW()
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS exercise_logs (
                id          SERIAL PRIMARY KEY,
                chat_id     BIGINT,
                logged_date DATE,
                logged_at   TIMESTAMPTZ DEFAULT NOW(),
                note        TEXT
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS injury_logs (
                id          SERIAL PRIMARY KEY,
                chat_id     BIGINT,
                logged_at   TIMESTAMPTZ DEFAULT NOW(),
                raw_message TEXT,
                body_areas  TEXT,
                severity    TEXT,
                timing      TEXT,
                triggers    TEXT,
                what_helped TEXT,
                extra_notes TEXT
            )
        """)
    print("Database initialised.")


def get_exercises(chat_id: int) -> list[dict]:
    with db() as con:
        cur = con.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute(
            "SELECT id, name, sets, reps, frequency, notes FROM exercises WHERE chat_id=%s AND active=TRUE",
            (chat_id,)
        )
        return [dict(r) for r in cur.fetchall()]


def has_done_exercises_today(chat_id: int) -> bool:
    today = date.today()
    with db() as con:
        cur = con.cursor()
        cur.execute(
            "SELECT id FROM exercise_logs WHERE chat_id=%s AND logged_date=%s",
            (chat_id, today)
        )
        return cur.fetchone() is not None


def log_exercises_done(chat_id: int, note: str = ""):
    today = date.today()
    with db() as con:
        cur = con.cursor()
        cur.execute(
            "INSERT INTO exercise_logs (chat_id, logged_date, note) VALUES (%s, %s, %s)",
            (chat_id, today, note)
        )


def get_all_chat_ids() -> list[int]:
    with db() as con:
        cur = con.cursor()
        cur.execute("SELECT chat_id FROM users")
        return [r[0] for r in cur.fetchall()]


def register_user(chat_id: int, username: str):
    with db() as con:
        cur = con.cursor()
        cur.execute(
            "INSERT INTO users (chat_id, username) VALUES (%s, %s) ON CONFLICT (chat_id) DO NOTHING",
            (chat_id, username)
        )


def save_injury_log(chat_id: int, raw: str, parsed: dict):
    with db() as con:
        cur = con.cursor()
        cur.execute("""
            INSERT INTO injury_logs
                (chat_id, raw_message, body_areas, severity, timing, triggers, what_helped, extra_notes)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
        """, (
            chat_id, raw,
            json.dumps(parsed.get("body_areas") or []),
            parsed.get("severity") or "",
            parsed.get("timing") or "",
            parsed.get("triggers") or "",
            parsed.get("what_helped") or "",
            parsed.get("extra_notes") or "",
        ))


# ── AI helpers ────────────────────────────────────────────────────────────────

ai = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)


def extract_injury_data(message: str) -> dict:
    """Use Claude to parse a natural-language injury note into structured fields."""
    prompt = f"""You are a physiotherapy assistant. Extract structured information from this patient's injury/pain update.

Patient message: "{message}"

Respond ONLY with a JSON object with these keys (use null if not mentioned):
- body_areas: list of body areas mentioned (e.g. ["left groin", "right calf"])
- severity: overall severity description (e.g. "mild", "moderate", "sharp", or a 1-10 if given)
- timing: when the pain occurred (e.g. "morning on waking", "during work", "after walking")
- triggers: what seemed to cause or worsen it (e.g. "sitting for long periods", "walking uphill")
- what_helped: what reduced the pain (e.g. "warm shower", "rest", "stretching")
- extra_notes: any other relevant details

Return ONLY the JSON, no explanation."""

    response = ai.messages.create(
        model="claude-sonnet-4-20250514",
        max_tokens=500,
        messages=[{"role": "user", "content": prompt}]
    )
    text = response.content[0].text.strip()
    text = re.sub(r"^```json\s*|```$", "", text, flags=re.MULTILINE).strip()
    try:
        return json.loads(text)
    except Exception:
        return {}


def generate_physio_report(chat_id: int) -> str:
    exercises = get_exercises(chat_id)

    with db() as con:
        cur = con.cursor()
        cur.execute(
            "SELECT logged_date, note FROM exercise_logs WHERE chat_id=%s ORDER BY logged_date DESC LIMIT 30",
            (chat_id,)
        )
        logs = cur.fetchall()
        cur.execute(
            "SELECT logged_at, raw_message, body_areas, severity, timing, triggers, what_helped FROM injury_logs WHERE chat_id=%s ORDER BY logged_at DESC LIMIT 50",
            (chat_id,)
        )
        injuries = cur.fetchall()

    ex_text = "\n".join(f"- {e['name']}: {e['sets']} sets x {e['reps']} ({e['frequency']})" for e in exercises)
    log_text = "\n".join(f"[{l[0]}] Done. {l[1] or ''}" for l in logs) or "No exercise logs yet."
    inj_text = "\n".join(
        f"[{i[0]}] {i[1]} | Areas: {i[2]} | Severity: {i[3]} | When: {i[4]} | Triggers: {i[5]} | Helped: {i[6]}"
        for i in injuries
    ) or "No injury notes yet."

    prompt = f"""You are writing a clinical physiotherapy progress report for a patient to share with their physiotherapist.

PRESCRIBED EXERCISES:
{ex_text}

EXERCISE COMPLETION LOG (last 30 days):
{log_text}

PATIENT INJURY/PAIN NOTES:
{inj_text}

Write a clear, professional report covering:
1. Exercise adherence summary
2. Recurring pain patterns and body areas affected
3. Identified triggers (activities, times of day, postures)
4. What appears to help
5. Notable trends or changes over time
6. Key questions or concerns for the physiotherapist to address

Use plain clinical language. Be concise and factual."""

    response = ai.messages.create(
        model="claude-sonnet-4-20250514",
        max_tokens=1500,
        messages=[{"role": "user", "content": prompt}]
    )
    return response.content[0].text.strip()


# ── Bot command handlers ───────────────────────────────────────────────────────

async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    username = update.effective_user.username or update.effective_user.first_name
    register_user(chat_id, username)
    await update.message.reply_text(
        "👋 *Physio Agent active!*\n\n"
        "I'll help you stay on top of your exercises and track how your body's feeling.\n\n"
        "*Commands:*\n"
        "• /routine — view or update your exercise routine\n"
        "• /done — log that you've done today's exercises\n"
        "• /checkin — tell me how you're feeling right now\n"
        "• /report — generate a report for your physio\n"
        "• /status — see today's summary\n\n"
        "You can also just message me naturally any time — if you say something like "
        "_\"my knee's been bad today\"_, I'll log it automatically.\n\n"
        "Start by setting up your routine with /routine",
        parse_mode="Markdown"
    )


async def cmd_routine(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    exercises = get_exercises(chat_id)
    if not exercises:
        await update.message.reply_text(
            "You don't have any exercises set up yet.\n\n"
            "Send me your routine like this:\n\n"
            "*ADD:* name, sets, reps, frequency\n"
            "Example: `ADD: Hip flexor stretch, 3, 30s hold, daily`\n\n"
            "Or list several:\n"
            "`ADD: Calf raises, 3, 15 reps, daily`\n"
            "`ADD: Glute bridge, 3, 12 reps, daily`",
            parse_mode="Markdown"
        )
    else:
        lines = ["*Your current routine:*\n"]
        for e in exercises:
            lines.append(f"• *{e['name']}* — {e['sets']} sets × {e['reps']} | {e['frequency']}")
            if e["notes"]:
                lines.append(f"  _{e['notes']}_")
        lines.append("\nSend `ADD: name, sets, reps, frequency` to add more.")
        lines.append("Send `REMOVE: exercise name` to remove one.")
        await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


async def cmd_done(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if has_done_exercises_today(chat_id):
        await update.message.reply_text("✅ You've already logged your exercises for today. Great work!")
    else:
        log_exercises_done(chat_id)
        await update.message.reply_text(
            "✅ *Exercises logged for today!* Well done.\n\n"
            "How did it feel? Any tightness or discomfort during the session? "
            "(Just reply naturally — I'll log it)",
            parse_mode="Markdown"
        )


async def cmd_checkin(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🩺 *How's your body feeling right now?*\n\n"
        "Just tell me in your own words — what's sore, when it started, "
        "what made it better or worse. No need for formal language.",
        parse_mode="Markdown"
    )


async def cmd_status(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    done = has_done_exercises_today(chat_id)
    exercises = get_exercises(chat_id)

    with db() as con:
        cur = con.cursor()
        cur.execute(
            "SELECT logged_at, raw_message FROM injury_logs WHERE chat_id=%s ORDER BY logged_at DESC LIMIT 3",
            (chat_id,)
        )
        recent_injuries = cur.fetchall()

    lines = [f"*Today — {date.today().strftime('%A %d %B')}*\n"]
    lines.append(f"{'✅' if done else '❌'} Exercises {'done' if done else 'not yet logged'}")
    if exercises:
        lines.append(f"\n*Routine ({len(exercises)} exercises):*")
        for e in exercises:
            lines.append(f"• {e['name']} — {e['sets']}×{e['reps']}")
    if recent_injuries:
        lines.append("\n*Recent notes:*")
        for ts, msg in recent_injuries:
            t = ts.strftime("%H:%M") if hasattr(ts, 'strftime') else str(ts)[:5]
            lines.append(f"• [{t}] {msg[:80]}{'…' if len(msg) > 80 else ''}")
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


async def cmd_report(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    await update.message.reply_text("⏳ Generating your physio report… this may take a moment.")
    try:
        report = generate_physio_report(chat_id)
        if len(report) > 4000:
            for chunk in [report[i:i+4000] for i in range(0, len(report), 4000)]:
                await update.message.reply_text(chunk)
        else:
            await update.message.reply_text(report)
        await update.message.reply_text("📋 That's your report. You can copy this and share it with your physio.")
    except Exception as e:
        await update.message.reply_text(f"Sorry, couldn't generate the report: {e}")


# ── Message handler ───────────────────────────────────────────────────────────

ADD_PATTERN = re.compile(r"^ADD:\s*(.+),\s*(\d+),\s*(.+),\s*(.+)$", re.IGNORECASE)
REMOVE_PATTERN = re.compile(r"^REMOVE:\s*(.+)$", re.IGNORECASE)
DONE_PATTERN = re.compile(r"\b(done|finished|completed|did them|all done|just did)\b", re.IGNORECASE)
INJURY_KEYWORDS = re.compile(
    r"\b(sore|pain|ache|tight|stiff|hurts?|hurting|burning|throbbing|swollen|tender|pulled|strain|twinge|discomfort|numb|weak)\b",
    re.IGNORECASE
)


async def handle_message(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    text = update.message.text.strip()
    register_user(chat_id, update.effective_user.username or "")

    m = ADD_PATTERN.match(text)
    if m:
        name, sets, reps, freq = m.group(1).strip(), int(m.group(2)), m.group(3).strip(), m.group(4).strip()
        with db() as con:
            cur = con.cursor()
            cur.execute(
                "INSERT INTO exercises (chat_id, name, sets, reps, frequency) VALUES (%s, %s, %s, %s, %s)",
                (chat_id, name, sets, reps, freq)
            )
        await update.message.reply_text(f"✅ Added: *{name}* — {sets} sets × {reps} | {freq}", parse_mode="Markdown")
        return

    m = REMOVE_PATTERN.match(text)
    if m:
        name = m.group(1).strip()
        with db() as con:
            cur = con.cursor()
            cur.execute(
                "UPDATE exercises SET active=FALSE WHERE chat_id=%s AND name ILIKE %s",
                (chat_id, f"%{name}%")
            )
        await update.message.reply_text(f"🗑️ Removed exercise matching: *{name}*", parse_mode="Markdown")
        return

    if DONE_PATTERN.search(text) and not has_done_exercises_today(chat_id):
        log_exercises_done(chat_id, note=text)
        await update.message.reply_text("✅ Logged — exercises done for today. Nice work!\n\nHow did it feel?")
        return

    if INJURY_KEYWORDS.search(text) or len(text) > 30:
        await update.message.reply_text("📝 Got it, logging that…")
        parsed = extract_injury_data(text)
        save_injury_log(chat_id, text, parsed)
        areas = ", ".join(parsed.get("body_areas") or []) or "noted"
        helped = parsed.get("what_helped")
        triggers = parsed.get("triggers")
        reply = f"✅ Logged your note. I picked up: *{areas}*."
        if triggers:
            reply += f"\nPossible trigger: _{triggers}_"
        if helped:
            reply += f"\nWhat helped: _{helped}_"
        reply += "\n\nKeep noting how you feel — it all builds the picture."
        await update.message.reply_text(reply, parse_mode="Markdown")
        return

    await update.message.reply_text(
        "Got your message. If you're describing pain or discomfort, try to include a bit more detail "
        "so I can log it properly — e.g. _'my left calf has been tight since this morning'_.\n\n"
        "Or use /checkin for a prompted update.",
        parse_mode="Markdown"
    )


# ── Scheduled reminders ───────────────────────────────────────────────────────

async def send_exercise_reminder(app: Application, reminder_type: str):
    messages = {
        "morning": (
            "☀️ *Good morning!*\n\n"
            "Don't forget your physio exercises today. Tap /done once you're finished, or just tell me.\n\n"
            "How are you feeling this morning?"
        ),
        "evening": (
            "🏠 *Welcome home!*\n\n"
            "Good time to get your exercises in if you haven't yet.\n\n"
            "Let me know with /done when you're done, or just say _'done'_."
        ),
        "bedtime": (
            "🌙 *Before you sleep…*\n\n"
            "Did you get your exercises done today? Tap /done if so!\n\n"
            "Also — how did your body feel today? Any areas playing up?"
        ),
        "checkin": (
            "🩺 *Midday check-in*\n\n"
            "How's your body feeling today? Any areas sore or tight? "
            "Just reply in your own words — I'll log it."
        ),
    }
    msg = messages.get(reminder_type, "")
    for chat_id in get_all_chat_ids():
        if reminder_type in ("morning", "evening", "bedtime") and has_done_exercises_today(chat_id):
            if reminder_type == "bedtime":
                await app.bot.send_message(
                    chat_id,
                    "🌙 *Great work today!* Exercises are logged. Sleep well. 💪",
                    parse_mode="Markdown"
                )
            continue
        try:
            await app.bot.send_message(chat_id, msg, parse_mode="Markdown")
        except Exception as e:
            print(f"Failed to message {chat_id}: {e}")


# ── Entry point ───────────────────────────────────────────────────────────────

def main():
    if not TELEGRAM_TOKEN:
        print("ERROR: Set TELEGRAM_TOKEN environment variable")
        return
    if not ANTHROPIC_API_KEY:
        print("ERROR: Set ANTHROPIC_API_KEY environment variable")
        return
    if not DATABASE_URL:
        print("ERROR: Set DATABASE_URL environment variable")
        return

    init_db()

    async def post_init(application: Application):
        scheduler = AsyncIOScheduler(timezone=TIMEZONE)
        scheduler.add_job(send_exercise_reminder, CronTrigger(hour=MORNING_HOUR, minute=0), args=[application, "morning"])
        scheduler.add_job(send_exercise_reminder, CronTrigger(hour=CHECKIN_HOUR, minute=0), args=[application, "checkin"])
        scheduler.add_job(send_exercise_reminder, CronTrigger(hour=EVENING_HOUR, minute=0), args=[application, "evening"])
        scheduler.add_job(send_exercise_reminder, CronTrigger(hour=BEDTIME_HOUR, minute=0), args=[application, "bedtime"])
        scheduler.start()
        print(f"Scheduler started. Reminders at {MORNING_HOUR}:00, {CHECKIN_HOUR}:00, {EVENING_HOUR}:00, {BEDTIME_HOUR}:00")

    app = Application.builder().token(TELEGRAM_TOKEN).post_init(post_init).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("routine", cmd_routine))
    app.add_handler(CommandHandler("done", cmd_done))
    app.add_handler(CommandHandler("checkin", cmd_checkin))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("report", cmd_report))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    print("Bot polling…")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
