import asyncio
import logging
import os
import sys
from contextlib import asynccontextmanager
from typing import Dict, List

import uvicorn
from fastapi import FastAPI
from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from ai_agent import ask_financial_ai
from config import (
    ALLOWED_TELEGRAM_USER_IDS,
    EMAIL_SYNC_INTERVAL_SECONDS,
    GMAIL_APP_PASSWORD,
    TELEGRAM_BOT_TOKEN,
)
from database import init_db, run_read_only_sql
from gmail_sync import sync_from_gmail_imap
from statement_parser import parse_and_ingest_statement_file

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("google_genai").setLevel(logging.WARNING)
logger = logging.getLogger("mybca-bot")

# Simple in-memory conversation memory per Telegram user
USER_HISTORY: Dict[int, List[Dict[str, str]]] = {}


def is_authorized(user_id: int) -> bool:
    if not ALLOWED_TELEGRAM_USER_IDS:
        return True
    return user_id in ALLOWED_TELEGRAM_USER_IDS


async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if not is_authorized(user.id):
        await update.message.reply_text("⛔ Unauthorized user.")
        return

    msg = (
        f"👋 Halo *{user.first_name}*! Your *myBCA & PasporBCA Financial AI* is ready.\n\n"
        f"🔑 Your Telegram User ID: `{user.id}`\n\n"
        "💬 *What you can ask or do:*\n"
        "• _\"Show my November 2026 cashflow breakdown by category\"_\n"
        "• _\"How can I reduce my food & beverage spendings?\"_\n"
        "• _\"How much did I spend in Japan vs Indonesia in October?\"_\n"
        "• _\"+5,000,000 salary from company\"_ (logs incoming money)\n"
        "• _\"Categorize GOPAY TOPUP as Transportation & Travel\"_\n"
        "• 📄 *Send a myBCA e-Statement PDF or receipt photo* to sync incoming transfers & exact FX rates!\n\n"
        "🛠 *Commands:*\n"
        "`/summary` — Current month spending & income summary\n"
        "`/sync` — Pull latest BCA & PasporBCA emails from Gmail\n"
        "`/clear` — Reset chat memory"
    )
    await update.message.reply_text(msg, parse_mode="Markdown")


def split_telegram_text(text: str, limit: int = 4000) -> List[str]:
    """Splits long AI responses cleanly so they never exceed Telegram's 4096-char limit."""
    if len(text) <= limit:
        return [text]
    chunks: List[str] = []
    remaining = text
    while remaining:
        if len(remaining) <= limit:
            chunks.append(remaining)
            break
        split_at = remaining.rfind("\n", 0, limit)
        if split_at == -1:
            split_at = remaining.rfind(" ", 0, limit)
        if split_at <= 0:
            split_at = limit
        chunks.append(remaining[:split_at].rstrip())
        remaining = remaining[split_at:].lstrip()
    return chunks


async def edit_with_long_response(thinking_msg, update: Update, text: str) -> None:
    """Edits the thinking placeholder with the first chunk and sends follow-up messages if >4096 chars."""
    chunks = split_telegram_text(text)
    await thinking_msg.edit_text(chunks[0])
    for extra in chunks[1:]:
        await update.message.reply_text(extra)


async def summary_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_authorized(update.effective_user.id):
        return
    thinking_msg = await update.message.reply_text("💭 Generating monthly cashflow summary...")
    try:
        answer = await asyncio.to_thread(
            ask_financial_ai,
            "Give me a clean summary of my latest month's cashflow (total spending, total income if any, and breakdown by category with percentages, plus top 5 largest expenses).",
        )
        await edit_with_long_response(thinking_msg, update, answer)
    except Exception as e:
        await thinking_msg.edit_text(f"⚠️ Error: {e}")


async def sync_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_authorized(update.effective_user.id):
        return
    status_msg = await update.message.reply_text("🔄 Syncing BCA & PasporBCA emails from Gmail...")
    try:
        stats = await asyncio.to_thread(sync_from_gmail_imap, 100)
        await status_msg.edit_text(
            f"✅ *Gmail Sync Complete!*\n"
            f"• Emails scanned: `{stats['scanned']}`\n"
            f"• New transactions added: `{stats['inserted']}`\n"
            f"• Duplicates skipped: `{stats['skipped_duplicates']}`",
            parse_mode="Markdown",
        )
    except Exception as e:
        await status_msg.edit_text(f"⚠️ Sync error: {e}")


async def clear_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    USER_HISTORY.pop(update.effective_user.id, None)
    await update.message.reply_text("🧹 Chat memory cleared!")


async def handle_document_or_photo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handles myBCA e-Statement PDF uploads or transfer/receipt screenshots."""
    if not is_authorized(update.effective_user.id):
        return

    status_msg = await update.message.reply_text("📄 Analyzing your myBCA document/screenshot with Gemini AI...")
    try:
        if update.message.document:
            doc = update.message.document
            mime_type = doc.mime_type or "application/pdf"
            tg_file = await doc.get_file()
        else:
            photo = update.message.photo[-1]
            mime_type = "image/jpeg"
            tg_file = await photo.get_file()

        file_bytes = bytes(await tg_file.download_as_bytearray())
        result = await asyncio.to_thread(parse_and_ingest_statement_file, file_bytes, mime_type)

        await status_msg.edit_text(
            f"✅ *Statement / Receipt Synced!*\n"
            f"• Transactions detected: `{result['extracted_count']}`\n"
            f"• New rows inserted: `{result['inserted']}`\n"
            f"• Already in DB (skipped): `{result['skipped_duplicates']}`\n"
            f"• New Income added: `IDR {result['new_income_idr']:,.0f}`\n"
            f"• New Spending added: `IDR {result['new_spending_idr']:,.0f}`",
            parse_mode="Markdown",
        )
    except Exception as e:
        await status_msg.edit_text(f"⚠️ Could not parse file: {e}")


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    uid = update.effective_user.id
    if not is_authorized(uid):
        await update.message.reply_text("⛔ Unauthorized user.")
        return

    question = update.message.text.strip()
    history = USER_HISTORY.setdefault(uid, [])

    thinking_msg = await update.message.reply_text("💭 Analyzing your transactions...")
    try:
        answer = await asyncio.to_thread(ask_financial_ai, question, history)
        history.append({"role": "user", "text": question})
        history.append({"role": "model", "text": answer})
        if len(history) > 12:
            del history[:-12]
        await edit_with_long_response(thinking_msg, update, answer)
    except Exception as e:
        await thinking_msg.edit_text(f"⚠️ Error: {e}")


async def periodic_gmail_sync_task() -> None:
    """Background task that polls Gmail IMAP every N minutes when GMAIL_APP_PASSWORD is set."""
    while True:
        if GMAIL_APP_PASSWORD:
            try:
                stats = await asyncio.to_thread(sync_from_gmail_imap, 40)
                if stats["inserted"] > 0:
                    logger.info("Auto-synced %d new BCA transactions from Gmail.", stats["inserted"])
            except Exception as e:
                logger.warning("Background Gmail sync warning: %s", e)
        await asyncio.sleep(EMAIL_SYNC_INTERVAL_SECONDS)


telegram_app: Application | None = None
bg_sync_task: asyncio.Task | None = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global telegram_app, bg_sync_task
    init_db()
    bg_sync_task = asyncio.create_task(periodic_gmail_sync_task())

    if TELEGRAM_BOT_TOKEN:
        telegram_app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()
        telegram_app.add_handler(CommandHandler("start", start_cmd))
        telegram_app.add_handler(CommandHandler("summary", summary_cmd))
        telegram_app.add_handler(CommandHandler("sync", sync_cmd))
        telegram_app.add_handler(CommandHandler("clear", clear_cmd))
        telegram_app.add_handler(MessageHandler(filters.Document.ALL | filters.PHOTO, handle_document_or_photo))
        telegram_app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

        await telegram_app.initialize()
        await telegram_app.start()
        await telegram_app.updater.start_polling(drop_pending_updates=True)
        logger.info("Telegram bot started polling!")
    else:
        logger.warning("TELEGRAM_BOT_TOKEN not set yet. Running web server only.")

    yield

    if bg_sync_task:
        bg_sync_task.cancel()
    if telegram_app:
        await telegram_app.updater.stop()
        await telegram_app.stop()
        await telegram_app.shutdown()


app = FastAPI(title="myBCA & PasporBCA Financial AI Bot", lifespan=lifespan)


@app.get("/")
@app.get("/health")
def health_check():
    """Health check endpoint for Render / Koyeb / UptimeRobot."""
    rows = run_read_only_sql("SELECT COUNT(*) as total_tx FROM transactions;")
    return {
        "status": "ok",
        "service": "mybca-ai-bot",
        "transactions_count": rows[0]["total_tx"] if rows else 0,
    }


def run_cli_chat():
    """Interactive terminal mode so you can test the AI locally before setting up Telegram."""
    init_db()
    rows = run_read_only_sql("SELECT COUNT(*) as c FROM transactions;")
    count = rows[0]["c"] if rows else 0
    print(f"\n=======================================================")
    print(f"🏦 myBCA & PasporBCA Financial AI — Local CLI Mode")
    print(f"📊 Loaded {count} transactions in database.")
    print(f"Type your question (or 'exit' to quit):")
    print(f"=======================================================\n")

    history: List[Dict[str, str]] = []
    while True:
        try:
            q = input("You > ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\nGoodbye!")
            break
        if not q:
            continue
        if q.lower() in ("exit", "quit"):
            break
        try:
            ans = ask_financial_ai(q, history)
            history.append({"role": "user", "text": q})
            history.append({"role": "model", "text": ans})
            print(f"\nAI >\n{ans}\n")
        except Exception as e:
            print(f"\n⚠️ Error: {e}\n")


if __name__ == "__main__":
    if "--cli" in sys.argv:
        logging.getLogger().setLevel(logging.WARNING)
        run_cli_chat()
    else:
        port = int(os.getenv("PORT", "8000"))
        uvicorn.run("bot:app", host="0.0.0.0", port=port, reload=False)
