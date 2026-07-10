"""Penny — a personal-assistant Telegram bot with a Claude brain.
Run: python bot.py  (see README.md for full setup)"""
import asyncio
import base64
import logging
import os
from datetime import datetime

from telegram import Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

import brain
import gmail_watch
import maintenance
import memory
import scheduler
import voice
from config import (
    INSTANCE_DIR,
    ASSISTANT_NAME,
    DAILY_BUDGET_USD,
    EMAIL_POLL_MINUTES,
    EVENING_DIGEST,
    LOG_PATH,
    MORNING_DIGEST,
    PAIRING_CODE,
    TELEGRAM_TOKEN,
    TZ,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(name)s %(levelname)s %(message)s",
    # stdout only: under launchd, stdout already lands in penny.log — two handlers
    # writing the same file caused every line to appear twice.
    handlers=[logging.StreamHandler()],
)
logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger("penny")

WELCOME = f"""Hi, I'm {ASSISTANT_NAME} 💛 — your external brain.

Here's the deal: **anything** swirling around in your head, say it to me and stop carrying it. I'll sort it, remember it forever, and remind you at the right moment — and I won't let anything slip until you check it off.

The easiest way to talk to me: tap the little 🎤 mic on your keyboard and just ramble. Grocery lists, work stress, "I need to text Emma back", planning your weekend — all of it, in one messy breath. I'll untangle it.

A few things I can do:
• 📝 Keep the master list, prioritized, with satisfying ✓ buttons
• ⏰ Nudge you (and re-nudge you) until things are actually done
• 📅 Put things on your calendar and watch what's coming up
• 📧 Watch your personal inbox for stuff that needs you
• 🤔 Think things through with you — decisions, plans, what to do first
• ☀️ Morning game-plan + 🌙 evening wrap-up, every day

Try it right now: hit the mic and tell me everything on your mind. I've got it from here."""

HELP = f"""Talk to me like a person — the mic 🎤 on your keyboard is your best friend.

Things you can say:
• "I need to return the Amazon leggings, call the dentist, and text Sarah back about Saturday"
• "What's on my list?" / "What should I do first?"
• 🎙️ or just send a voice memo — I transcribe it myself
• "Remind me tomorrow at 8 to bring the charger"
• "Move my Thursday dinner to 7:30"
• "Done with the dentist thing!"
• "Help me decide: gym before or after brunch?"

Commands (optional — talking works for everything):
/list — your checklist with ✓ buttons
/help — this message

I send a morning game-plan at {MORNING_DIGEST} and an evening check-in at {EVENING_DIGEST}. Reminders keep nudging until you tap ✅ Done — that's the point 😌"""


def _owner() -> str | None:
    return memory.get_setting("owner_chat_id")


def _is_owner(update: Update) -> bool:
    return _owner() == str(update.effective_chat.id)


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if _owner() is None:
        await update.message.reply_text(
            f"Hi! I'm {ASSISTANT_NAME}, a private assistant. What's the magic word? 🔒"
        )
        return
    if _is_owner(update):
        await update.message.reply_text(HELP)


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if _is_owner(update):
        await update.message.reply_text(HELP)


async def cmd_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_owner(update):
        return
    items = memory.open_items()
    done = memory.completed_today()
    await update.message.reply_text(
        f"📊 Today: {len(done)} checked off, {len(items)} open.\n"
        f"💸 Estimated API spend today: ${brain.today_spend():.3f} "
        f"(soft cap ${DAILY_BUDGET_USD:.2f}/day)"
    )


async def cmd_list(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_owner(update):
        return
    items = memory.open_items()
    await update.message.reply_text(
        scheduler.render_list(items), reply_markup=scheduler.checklist_markup(items)
    )


async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = str(update.effective_chat.id)
    text = (update.message.text or "").strip()

    # pairing: the first person to say the magic word becomes the owner
    if _owner() is None:
        if text.lower() == PAIRING_CODE.lower():
            memory.set_setting("owner_chat_id", chat_id)
            log.info("paired with chat %s", chat_id)
            await update.message.reply_text(WELCOME, parse_mode="Markdown")
        else:
            await update.message.reply_text("What's the magic word? 🔒")
        return
    if chat_id != _owner():
        return  # silently ignore strangers

    # shortcut: bare "list" renders the tappable checklist
    if text.lower() in ("list", "my list", "show my list", "what's on my list", "whats on my list"):
        items = memory.open_items()
        await update.message.reply_text(
            scheduler.render_list(items), reply_markup=scheduler.checklist_markup(items)
        )
        return

    await update.effective_chat.send_action("typing")
    try:
        reply = await asyncio.to_thread(brain.respond, text)
    except Exception:
        log.exception("brain failed")
        reply = "😵 Something glitched on my end — say that again? (It's written in my logs, nothing is lost.)"
    await update.message.reply_text(reply)


async def on_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_owner(update):
        return
    await update.effective_chat.send_action("typing")
    try:
        photo = update.message.photo[-1]  # largest size
        f = await photo.get_file()
        data = await f.download_as_bytearray()
        b64 = base64.standard_b64encode(bytes(data)).decode()
        caption = update.message.caption or "(she sent this screenshot/photo — figure out what matters in it and capture anything actionable)"
        reply = await asyncio.to_thread(brain.respond, caption, b64, "image/jpeg")
    except Exception:
        log.exception("photo handling failed")
        reply = "😵 Couldn't read that image — try sending it again?"
    await update.message.reply_text(reply)


async def on_voice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_owner(update):
        return
    await update.effective_chat.send_action("typing")
    tmp = None
    try:
        v = update.message.voice or update.message.audio
        f = await v.get_file()
        tmp = str(INSTANCE_DIR / f"voice_{update.message.message_id}.oga")
        await f.download_to_drive(tmp)
        text = await asyncio.to_thread(voice.transcribe, tmp)
        if not text:
            await update.message.reply_text("Hmm, I couldn't make out any words in that one — try again?")
            return
        await update.effective_chat.send_action("typing")
        reply = await asyncio.to_thread(brain.respond, text)
        heard = text if len(text) <= 220 else text[:220] + "…"
        await update.message.reply_text(f"🎤 heard: \"{heard}\"\n\n{reply}")
    except Exception:
        log.exception("voice handling failed")
        await update.message.reply_text(
            "😵 That voice note tripped me up — send it again, or use the keyboard 🎤 and I'll catch it."
        )
    finally:
        if tmp and os.path.exists(tmp):
            os.remove(tmp)


async def on_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if _owner() != str(q.message.chat.id):
        await q.answer()
        return
    data = q.data or ""
    parts = data.split(":")
    action = parts[0]

    if action == "done" and len(parts) == 2:
        item = memory.get_item(int(parts[1]))
        if item:
            memory.complete_item(item["id"])
            await q.answer("Checked off! ✅")
            remaining = memory.open_items()
            if q.message.text and q.message.text.startswith("📋"):
                # re-render the checklist message in place
                await q.edit_message_text(
                    scheduler.render_list(remaining),
                    reply_markup=scheduler.checklist_markup(remaining),
                )
            else:
                await q.edit_message_text(f"✅ {item['title']} — done. {len(remaining)} left on the list.")
        else:
            await q.answer("Already gone!")
    elif action == "snooze" and len(parts) == 3:
        from datetime import timedelta
        nxt = (datetime.now(TZ) + timedelta(minutes=int(parts[2]))).isoformat(timespec="seconds")
        memory.update_item(int(parts[1]), next_remind_at=nxt)
        await q.answer("Snoozed ⏰")
        await q.edit_message_text(f"{q.message.text}\n\n⏰ Snoozed — I'll circle back in an hour.")
    elif action == "tmrw" and len(parts) == 2:
        from datetime import timedelta
        tomorrow9 = datetime.now(TZ).replace(hour=9, minute=0, second=0) + timedelta(days=1)
        memory.update_item(int(parts[1]), next_remind_at=tomorrow9.isoformat(timespec="seconds"))
        await q.answer("Moved to tomorrow 🌙")
        await q.edit_message_text(f"{q.message.text}\n\n🌙 Parked until tomorrow morning. Sleep easy.")
    else:
        await q.answer()


def main():
    if not TELEGRAM_TOKEN:
        raise SystemExit("TELEGRAM_TOKEN is missing — copy .env.example to .env and fill it in (see README.md).")
    memory.init()
    app = Application.builder().token(TELEGRAM_TOKEN).build()

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("list", cmd_list))
    app.add_handler(CommandHandler("stats", cmd_stats))
    app.add_handler(CallbackQueryHandler(on_button))
    app.add_handler(MessageHandler(filters.PHOTO, on_photo))
    app.add_handler(MessageHandler(filters.VOICE | filters.AUDIO, on_voice))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))

    jq = app.job_queue
    jq.run_repeating(scheduler.check_reminders, interval=60, first=15)
    jq.run_repeating(scheduler.digest_tick, interval=60, first=20)
    jq.run_repeating(maintenance.backup_tick, interval=3600, first=45)
    jq.run_repeating(maintenance.weekly_tick, interval=3600, first=50)
    if gmail_watch.enabled():
        jq.run_repeating(gmail_watch.poll, interval=EMAIL_POLL_MINUTES * 60, first=30)
        log.info("gmail watcher: ON (every %d min)", EMAIL_POLL_MINUTES)
    else:
        log.info("gmail/calendar: not connected yet (run setup_google.py to enable)")

    voice.warmup()  # download/load the transcription model before her first voice note
    log.info("%s is up. Waiting for messages…", ASSISTANT_NAME)
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
