"""
Telegram GPU Monitor Bot
- Polls the API every 1 second for GPU 'busy' status
- Lets users subscribe to be notified when a specific GPU becomes NOT busy

Requirements:
- Python 3.10+
- python-telegram-bot (v20+)
- aiohttp

Install:
    pip install python-telegram-bot==20.6 aiohttp

Set environment variable TELEGRAM_TOKEN with your bot token before running.

Run:
    python telegram_gpu_monitor_bot.py

Notes:
- Subscriptions are persisted in subscriptions.json (simple JSON file).
- The bot polls the API once globally every 1 second to keep load low.

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
    CallbackContext,
    CallbackQueryHandler,
    CommandHandler,
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
async def start(update: Update, context: CallbackContext):
    await update.message.reply_text(
        "Hi! I will monitor GPUs and notify you when a selected GPU becomes available.\n"
        "Use /subscribe to choose GPUs to be notified about.\n"
        "Use /my_subs to see your subscriptions and /unsubscribe to remove them."
    )


async def list_gpus_command(update: Update, context: CallbackContext):
    # show current GPUs and status with quick fetch
    async with aiohttp.ClientSession() as session:
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


async def subscribe_command(update: Update, context: CallbackContext):
    """Send an inline keyboard letting user pick GPUs to subscribe to."""
    chat_id = update.effective_chat.id
    async with aiohttp.ClientSession() as session:
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


async def my_subs_command(update: Update, context: CallbackContext):
    chat_id = update.effective_chat.id
    my = [gpu for gpu, chats in subscriptions.items() if chat_id in chats]
    if not my:
        await update.message.reply_text("You have no subscriptions.")
        return
    lines = ["Your subscriptions:"]
    for g in my:
        lines.append(f"- {gpu_display_names.get(g, g)} ({g})")
    await update.message.reply_text("\n".join(lines))


async def unsubscribe_command(update: Update, context: CallbackContext):
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


async def status_command(update: Update, context: CallbackContext):
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
async def callback_handler(update: Update, context: CallbackContext):
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


# ------- Background poller task -------
async def poller_task(app: "telegram.ext.Application"):
    """Global polling loop that fetches GPU states every POLL_INTERVAL_SECONDS and notifies subscribers when a GPU becomes AVAILABLE."""
    global prev_states
    logger.info("Starting poller loop (interval=%ss)", POLL_INTERVAL_SECONDS)
    async with aiohttp.ClientSession() as session:
        while True:
            try:
                gpus = await fetch_gpus(session)
                if gpus is None:
                    await asyncio.sleep(POLL_INTERVAL_SECONDS)
                    continue

                # update display names map and build current states
                current_states: Dict[str, bool] = {}
                for g in gpus:
                    name = g.get("name")
                    disp = g.get("display_name") or name
                    busy = bool(g.get("busy"))
                    gpu_display_names[name] = disp
                    current_states[name] = busy

                # detect transitions: busy -> not busy
                for name, busy in current_states.items():
                    prev_busy = prev_states.get(name)
                    # if known previously and it changed from busy True to now False -> notify
                    if prev_busy is True and busy is False:
                        # notify subscribers for this GPU
                        targets = subscriptions.get(name, [])
                        if targets:
                            message = f"\U0001f6a8 {gpu_display_names.get(name, name)} is now AVAILABLE!\nGPU id: {name}"
                            for chat_id in targets.copy():
                                try:
                                    await app.bot.send_message(
                                        chat_id=chat_id, text=message
                                    )
                                except Exception as e:
                                    logger.warning(
                                        "Failed to send notification to %s: %s",
                                        chat_id,
                                        e,
                                    )
                    # store state
                prev_states = current_states

            except Exception as e:
                logger.exception("Poller error: %s", e)
            await asyncio.sleep(POLL_INTERVAL_SECONDS)


# ------- Main setup -------
async def main():
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

    # callback for inline buttons
    app.add_handler(CallbackQueryHandler(callback_handler))

    # start background poller as a task
    # create_task is the recommended way to run background async tasks
    app.create_task(poller_task(app))

    # run the bot (this will block)
    await app.run_polling()


if __name__ == "__main__":
    import asyncio
    import sys

    # On Windows, prefer the selector event loop policy for compatibility
    if sys.platform.startswith("win"):
        try:
            asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
        except Exception:
            pass

    try:
        # Normal run (works when no loop is already running)
        asyncio.run(main())
    except RuntimeError:
        # Fallback for environments that already have a running loop (e.g. Jupyter)
        # Install nest_asyncio if you haven't: pip install nest_asyncio
        try:
            import nest_asyncio

            nest_asyncio.apply()
            loop = asyncio.get_event_loop()
            if loop.is_running():
                # Run the main coroutine in the existing loop
                # create_task returns immediately, so ensure we keep the loop alive by awaiting main()
                loop.run_until_complete(main())
            else:
                loop.run_until_complete(main())
        except Exception as exc:
            import logging

            logging.exception("Failed fallback run: %s", exc)
            raise
