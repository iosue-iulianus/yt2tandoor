#!/usr/bin/env python3
"""
yt2tandoor Telegram bot.

Send a cooking video URL and get a structured recipe published to Tandoor.
Supports YouTube, Instagram Reels, and TikTok.
"""

import asyncio
import logging
import os
import re
import signal
import sys

from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from pipeline import (
    PipelineCallbacks,
    PipelineConfig,
    process_video,
)

logging.basicConfig(
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger("yt2tandoor")

# ---------------------------------------------------------------------------
# Config from environment
# ---------------------------------------------------------------------------

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TANDOOR_URL = os.environ.get("TANDOOR_URL", "http://recipes.lan")
TANDOOR_API_KEY = os.environ.get("TANDOOR_API_KEY", "")
WHISPER_MODEL = os.environ.get("WHISPER_MODEL", "medium")
ALLOWED_CHATS: set[int] = set()

_allowed_raw = os.environ.get("ALLOWED_TELEGRAM_CHATS", "")
logger.info("ALLOWED_TELEGRAM_CHATS env raw: %r", _allowed_raw)
if _allowed_raw:
    for part in _allowed_raw.split(","):
        part = part.strip()
        if part:
            try:
                ALLOWED_CHATS.add(int(part))
            except ValueError:
                logger.warning("Ignoring invalid chat ID: %s", part)

# ---------------------------------------------------------------------------
# URL patterns
# ---------------------------------------------------------------------------

VIDEO_URL_RE = re.compile(
    r"https?://(?:"
    r"(?:www\.)?youtube\.com/(?:watch\?.*v=|shorts/)"
    r"|youtu\.be/"
    r"|(?:www\.)?instagram\.com/(?:reel|p)/"
    r"|(?:www\.)?tiktok\.com/@[\w.]+/video/"
    r"|vm\.tiktok\.com/"
    r")[\w/?=&%-]+",
    re.IGNORECASE,
)

# ---------------------------------------------------------------------------
# Job queue (sequential processing)
# ---------------------------------------------------------------------------

MAX_QUEUE = 3

_job_queue: asyncio.Queue[tuple[str, Update, ContextTypes.DEFAULT_TYPE]] = asyncio.Queue(maxsize=MAX_QUEUE)
_processing = False
_worker_task: asyncio.Task | None = None


def _is_allowed(chat_id: int) -> bool:
    if not ALLOWED_CHATS:
        return True  # No restriction if env var not set
    if chat_id not in ALLOWED_CHATS:
        logger.warning("Unauthorized chat_id: %s", chat_id)
        return False
    return True


# ---------------------------------------------------------------------------
# Command handlers
# ---------------------------------------------------------------------------

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_allowed(update.effective_chat.id):
        return

    await update.message.reply_text(
        "Hi! Send me a cooking video link (YouTube, Instagram, TikTok) "
        "and I'll extract the recipe and publish it to Tandoor.\n\n"
        "Just paste a URL or use /recipe <url>"
    )


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_allowed(update.effective_chat.id):
        return

    await update.message.reply_text(
        "Send a video URL and I'll convert it to a recipe.\n\n"
        "Commands:\n"
        "/recipe <url> - Process a video URL\n"
        "/status - Check if a job is running\n"
        "/help - This message\n\n"
        "Supported: YouTube, Instagram Reels, TikTok"
    )


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_allowed(update.effective_chat.id):
        return

    qsize = _job_queue.qsize()
    if _processing:
        msg = "A recipe is currently being processed."
        if qsize > 0:
            msg += f"\n{qsize} job(s) queued."
    else:
        msg = "No jobs running."
    await update.message.reply_text(msg)


async def cmd_recipe(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_allowed(update.effective_chat.id):
        return

    if not context.args:
        await update.message.reply_text("Usage: /recipe <video_url>")
        return

    url = context.args[0]
    await _enqueue(url, update, context)


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Auto-detect video URLs in plain messages."""
    chat_id = update.effective_chat.id
    logger.info("Message from chat %s: %s", chat_id, (update.message.text or "")[:80])

    if not _is_allowed(chat_id):
        return

    text = update.message.text or ""
    match = VIDEO_URL_RE.search(text)
    if match:
        logger.info("URL detected: %s", match.group(0))
        await _enqueue(match.group(0), update, context)


# ---------------------------------------------------------------------------
# Queue management
# ---------------------------------------------------------------------------

async def _enqueue(url: str, update: Update, context: ContextTypes.DEFAULT_TYPE):
    if _job_queue.full():
        await update.message.reply_text(
            "Queue is full (max 3 pending). Try again later."
        )
        return

    qsize = _job_queue.qsize()
    if qsize > 0 or _processing:
        await update.message.reply_text(
            f"Queued! Position: {qsize + 1}. I'll get to it soon."
        )

    await _job_queue.put((url, update, context))


async def _worker():
    """Background worker that processes jobs sequentially."""
    global _processing

    while True:
        url, update, context = await _job_queue.get()
        _processing = True
        try:
            await _process_job(url, update, context)
        except Exception as e:
            logger.exception("Job failed: %s", e)
            try:
                await update.message.reply_text(f"Something went wrong: {e}")
            except Exception:
                pass
        finally:
            _processing = False
            _job_queue.task_done()


SPINNER = [".", "..", "..."]


async def _process_job(url: str, update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Run the pipeline for a single URL with progress updates."""
    chat_id = update.effective_chat.id
    status_msg = await context.bot.send_message(
        chat_id=chat_id,
        text="Processing... this takes 2-5 minutes",
    )

    loop = asyncio.get_event_loop()
    _last_text = ""
    _spin_idx = 0

    async def _edit(text: str):
        nonlocal _last_text
        if text == _last_text:
            return
        _last_text = text
        try:
            await status_msg.edit_text(text)
        except Exception:
            pass

    def on_downloading():
        asyncio.run_coroutine_threadsafe(_edit("Downloading audio..."), loop)

    def on_transcribing():
        asyncio.run_coroutine_threadsafe(_edit("Transcribing..."), loop)

    def on_extracting():
        asyncio.run_coroutine_threadsafe(_edit("Extracting recipe..."), loop)

    def on_publishing():
        asyncio.run_coroutine_threadsafe(_edit("Publishing to Tandoor..."), loop)

    def on_progress(text: str):
        nonlocal _spin_idx
        frames = ["\u23f3", "\u231b"]  # hourglass not done / hourglass done
        frame = frames[_spin_idx % len(frames)]
        _spin_idx += 1
        elapsed = ""
        if "(" in text:
            elapsed = " (" + text.split("(")[-1]
        asyncio.run_coroutine_threadsafe(
            _edit(f"{frame} Transcribing...{elapsed}"), loop)

    callbacks = PipelineCallbacks(
        on_downloading=on_downloading,
        on_transcribing=on_transcribing,
        on_extracting=on_extracting,
        on_publishing=on_publishing,
        on_progress=on_progress,
    )

    config = PipelineConfig(
        tandoor_url=TANDOOR_URL,
        tandoor_api_key=TANDOOR_API_KEY,
        whisper_model=WHISPER_MODEL,
    )

    # Run pipeline in a thread (it's synchronous / CPU-bound)
    result = await loop.run_in_executor(
        None,
        lambda: process_video(url, config, callbacks),
    )

    if result.success:
        logger.info("Recipe published: %s -> %s", result.recipe_name, result.recipe_url)
        caption = f"{result.recipe_name}\n{result.recipe_url}"

        # Try to send thumbnail photo
        sent_photo = False
        if result.thumbnail_path:
            try:
                with open(result.thumbnail_path, "rb") as photo:
                    await context.bot.send_photo(
                        chat_id=chat_id,
                        photo=photo,
                        caption=caption,
                    )
                sent_photo = True
            except Exception:
                pass

        if not sent_photo:
            await _edit(f"Done! {caption}")
        else:
            try:
                await status_msg.delete()
            except Exception:
                pass
    else:
        logger.error("Pipeline failed: %s", result.error_message)
        await _edit(f"Failed: {result.error_message}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    if not TELEGRAM_BOT_TOKEN:
        print("Error: TELEGRAM_BOT_TOKEN environment variable is required.")
        sys.exit(1)

    if not TANDOOR_API_KEY:
        print("Error: TANDOOR_API_KEY environment variable is required.")
        sys.exit(1)

    logger.info("Starting yt2tandoor bot")
    logger.info("Tandoor: %s", TANDOOR_URL)
    if ALLOWED_CHATS:
        logger.info("Allowed chats: %s", ALLOWED_CHATS)
    else:
        logger.info("No chat restrictions (ALLOWED_TELEGRAM_CHATS not set)")

    app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("recipe", cmd_recipe))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    # Start the background worker after the event loop is running
    async def post_init(application):
        global _worker_task
        _worker_task = asyncio.create_task(_worker())

    app.post_init = post_init

    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
