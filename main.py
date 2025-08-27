"""
Telegram GPU Monitor Bot
- Polls the API every 1 second for GPU 'busy' status
- Lets users subscribe to be notified when a specific GPU becomes NOT busy

Requirements:
- Python 3.10+
- python-telegram-bot (v20+)
- aiohttp
"""

import asyncio
import json
import logging
import os
from pathlib import Path
from typing import Dict, List, Optional

import aiohttp
from dotenv import load_dotenv
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
)

load_dotenv()

# ------- Configuration -------
API_URL = "https://api.ferdowsi.cloud/api/v2/sm/mhd-fum1/flavors/gpus"
POLL_INTERVAL_SECONDS = 1  # per your request
SUBSCRIPTIONS_FILE = Path("subscriptions.json")
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")

# ------- Logging -------
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


# ------- Persistence helpers -------
def load_subscriptions() -> Dict[str, List[int]]:
    """Return mapping gpu_name -> list of chat_ids"""
    if not SUBSCRIPTIONS_FILE.exists():
        return {}
    try:
        with SUBSCRIPTIONS_FILE.open("r", encoding="utf-8") as f:
            data = json.load(f)
            # ensure lists of ints
            return {k: [int(x) for x in v] for k, v in data.items()}
    except Exception as e:
        logger.exception("Failed to load subscriptions: %s", e)
        return {}


def save_subscriptions(subs: Dict[str, List[int]]) -> None:
    try:
        with SUBSCRIPTIONS_FILE.open("w", encoding="utf-8") as f:
            json.dump(subs, f, indent=2)
    except Exception as e:
        logger.exception("Failed to save subscriptions: %s", e)


# ------- Global state (in-memory) -------
subscriptions: Dict[str, List[int]] = load_subscriptions()
prev_states: Dict[str, bool] = {}  # gpu_name -> busy
gpu_display_names: Dict[str, str] = {}  # gpu_name -> display_name


# ------- Helper to manage two sessions (proxy vs no-proxy) -------
def get_session_for_app(app, use_proxy: bool) -> aiohttp.ClientSession:
    """
    Lazily create and return a session stored in app.bot_data.
    - use_proxy True -> session with trust_env=True (respects HTTP_PROXY)
    - use_proxy False -> session with trust_env=False (ignores env proxy)
    """
    key = "http_session_proxy" if use_proxy else "http_session_noproxy"
    session = app.bot_data.get(key)
    if session is None:
        session = aiohttp.ClientSession(trust_env=True if use_proxy else False)
        app.bot_data[key] = session
    return session


# ------- API fetch -------
async def fetch_gpus(session: aiohttp.ClientSession) -> Optional[list]:
    try:
        async with session.get(API_URL, timeout=10) as resp:
            resp.raise_for_status()
            data = await resp.json()
            return data.get("data", [])
    except Exception as e:
        logger.warning("Failed to fetch GPUs: %s", e)
        return None


# ------- Telegram command handlers -------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Hi! I will monitor GPUs and notify you when a selected GPU becomes available.\n"
        "Use /subscribe to choose GPUs to be notified about.\n"
        "Use /my_subs to see your subscriptions and /unsubscribe to remove them."
    )


async def list_gpus_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # use the NO-PROXY session for Ferdowsi API
    app = context.application
    session = get_session_for_app(app, use_proxy=False)
    gpus = await fetch_gpus(session)
    if not gpus:
        await update.message.reply_text("Failed to fetch GPU list. Try again later.")
        return

    text_lines = ["GPUs (name - status):"]
    for g in gpus:
        name = g.get("name")
        disp = g.get("display_name") or name
        busy = g.get("busy")
        status = "BUSY" if busy else "AVAILABLE"
        text_lines.append(f"{disp} — {status}")
    await update.message.reply_text("\n".join(text_lines))


async def subscribe_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Send an inline keyboard letting user pick GPUs to subscribe to."""
    chat_id = update.effective_chat.id
    app = context.application
    session = get_session_for_app(app, use_proxy=False)  # NO-PROXY for Ferdowsi API
    gpus = await fetch_gpus(session)
    if not gpus:
        await update.message.reply_text("Failed to fetch GPU list. Try again later.")
        return

    # store display names map
    keyboard = []
    for g in gpus:
        name = g.get("name")
        disp = g.get("display_name") or name
        busy = g.get("busy")
        emoji = "🔴" if busy else "🟢"
        gpu_display_names[name] = disp
        keyboard.append(
            [InlineKeyboardButton(f"{disp} {emoji}", callback_data=f"sub|{name}")]
        )
    keyboard.append([InlineKeyboardButton("Cancel", callback_data="cancel")])
    reply_markup = InlineKeyboardMarkup(keyboard)
    await update.message.reply_text(
        "Select a GPU to be notified when it becomes AVAILABLE:",
        reply_markup=reply_markup,
    )


async def my_subs_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    my = [gpu for gpu, chats in subscriptions.items() if chat_id in chats]
    if not my:
        await update.message.reply_text("You have no subscriptions.")
        return
    lines = ["Your subscriptions:"]
    for g in my:
        lines.append(f"- {gpu_display_names.get(g, g)} ({g})")
    await update.message.reply_text("\n".join(lines))


async def unsubscribe_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # show inline keyboard of user's subscriptions
    chat_id = update.effective_chat.id
    my = [gpu for gpu, chats in subscriptions.items() if chat_id in chats]
    if not my:
        await update.message.reply_text(
            "You have no subscriptions to unsubscribe from."
        )
        return
    keyboard = [
        [InlineKeyboardButton(gpu_display_names.get(g, g), callback_data=f"unsub|{g}")]
        for g in my
    ]
    keyboard.append([InlineKeyboardButton("Cancel", callback_data="cancel")])
    await update.message.reply_text(
        "Select a subscription to remove:", reply_markup=InlineKeyboardMarkup(keyboard)
    )


async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # show last known states (from prev_states)
    if not prev_states:
        await update.message.reply_text(
            "No status known yet. Wait a moment for the bot to poll the API."
        )
        return
    lines = ["Last known GPU status:"]
    for name, busy in prev_states.items():
        disp = gpu_display_names.get(name, name)
        lines.append(f"{disp} — {'BUSY' if busy else 'AVAILABLE'}")
    await update.message.reply_text("\n".join(lines))


# ------- Callback query handler for inline buttons -------
async def callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data
    chat_id = query.message.chat.id

    if data == "cancel":
        await query.edit_message_text("Cancelled.")
        return

    try:
        action, gpu_name = data.split("|", 1)
    except Exception:
        await query.edit_message_text("Unknown action.")
        return

    if action == "sub":
        # add subscription
        subs = subscriptions.get(gpu_name, [])
        if chat_id not in subs:
            subs.append(chat_id)
            subscriptions[gpu_name] = subs
            save_subscriptions(subscriptions)
            await query.edit_message_text(
                f"You will be notified when {gpu_display_names.get(gpu_name, gpu_name)} becomes AVAILABLE."
            )
        else:
            await query.edit_message_text("You are already subscribed to this GPU.")
    elif action == "unsub":
        subs = subscriptions.get(gpu_name, [])
        if chat_id in subs:
            subs.remove(chat_id)
            if subs:
                subscriptions[gpu_name] = subs
            else:
                subscriptions.pop(gpu_name, None)
            save_subscriptions(subscriptions)
            await query.edit_message_text(
                f"Unsubscribed from {gpu_display_names.get(gpu_name, gpu_name)}."
            )
        else:
            await query.edit_message_text("You were not subscribed to that GPU.")
    else:
        await query.edit_message_text("Unknown action.")


# --- Poller job (runs inside JobQueue) ---
async def poller_job(context: ContextTypes.DEFAULT_TYPE):
    """JobQueue callback: polls API and notifies subscribers on busy->not-busy transition."""
    global prev_states

    app = context.application
    # NO-PROXY session to call Ferdowsi API
    session = get_session_for_app(app, use_proxy=False)

    try:
        gpus = await fetch_gpus(session)
        if gpus is None:
            return

        current_states: Dict[str, bool] = {}
        for g in gpus:
            name = g.get("name")
            disp = g.get("display_name") or name
            busy = bool(g.get("busy"))
            gpu_display_names[name] = disp
            current_states[name] = busy

        # detect transitions busy -> not busy
        for name, busy in current_states.items():
            prev_busy = prev_states.get(name)
            if prev_busy is True and busy is False:
                targets = subscriptions.get(name, [])
                if targets:
                    message = f"\U0001f6a8 {gpu_display_names.get(name, name)} is now AVAILABLE!\nGPU id: {name}"
                    for chat_id in targets.copy():
                        try:
                            await app.bot.send_message(chat_id=chat_id, text=message)
                        except Exception as e:
                            logger.warning(
                                "Failed to send notification to %s: %s", chat_id, e
                            )

        prev_states = current_states

    except Exception:
        logger.exception("Poller job error")


# --- main() that registers handlers and job queue ---
def main():
    if not TELEGRAM_TOKEN:
        logger.error("TELEGRAM_TOKEN environment variable not set. Exiting.")
        return

    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()

    # Commands
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("list", list_gpus_command))
    app.add_handler(CommandHandler("subscribe", subscribe_command))
    app.add_handler(CommandHandler("my_subs", my_subs_command))
    app.add_handler(CommandHandler("unsubscribe", unsubscribe_command))
    app.add_handler(CommandHandler("status", status_command))
    app.add_handler(CallbackQueryHandler(callback_handler))

    # schedule the poller via JobQueue (runs in the application's event loop)
    app.job_queue.run_repeating(poller_job, interval=POLL_INTERVAL_SECONDS, first=0)

    try:
        app.run_polling()
    finally:
        # cleanup: try to close both aiohttp sessions if they were created
        sess_keys = ["http_session_proxy", "http_session_noproxy"]
        for key in sess_keys:
            session = app.bot_data.get(key)
            if session is not None:
                try:
                    # after run_polling() returns there should be no running loop, so safe to run a new one to close
                    asyncio.run(session.close())
                except Exception as e:
                    logger.warning("Failed to close session %s: %s", key, e)


if __name__ == "__main__":
    main()
