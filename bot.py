import asyncio
import hashlib
import hmac
import logging
import os
import sys
from contextlib import asynccontextmanager
from typing import Dict, List, Set

import uvicorn
from fastapi import FastAPI, HTTPException, Request
from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from ai_agent import answer_audio_question, ask_financial_ai
from config import (
    ALLOWED_TELEGRAM_USER_IDS,
    EMAIL_SYNC_INTERVAL_SECONDS,
    GMAIL_APP_PASSWORD,
    SHORTCUT_API_TOKEN,
    TELEGRAM_BOT_TOKEN,
    WEBHOOK_URL,
)
from database import init_db, run_read_only_sql
from gmail_sync import sync_from_gmail_imap
from statement_parser import (
    detect_statement_mime_type,
    format_statement_sync_summary,
    parse_and_ingest_statement_file,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("google_genai").setLevel(logging.WARNING)
logger = logging.getLogger("mybca-bot")

# Simple in-memory conversation memory per Telegram user or iOS Shortcut session
USER_HISTORY: Dict[int | str, List[Dict[str, str]]] = {}
SYNC_LOCK = asyncio.Lock()


def _append_history(session_key: int | str, question: str, answer: str) -> None:
    history = USER_HISTORY.setdefault(session_key, [])
    history.append({"role": "user", "text": question})
    history.append({"role": "model", "text": answer})
    if len(history) > 12:
        del history[:-12]


def _shortcut_api_token() -> str:
    """Returns the bearer token protecting `/api/ask` for iPhone Shortcuts."""
    if SHORTCUT_API_TOKEN:
        return SHORTCUT_API_TOKEN
    if TELEGRAM_BOT_TOKEN and ALLOWED_TELEGRAM_USER_IDS:
        return hmac.new(
            TELEGRAM_BOT_TOKEN.encode("utf-8"),
            b"ios-shortcut-api-v1",
            hashlib.sha256,
        ).hexdigest()[:32]
    return ""


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
        "• 📄 *Send a myBCA e-Statement PDF or receipt photo* to sync incoming transfers & exact FX rates!\n"
        "• 🎙 *Send a voice recording / audio note* to ask questions or log expenses hands-free!\n\n"
        "🛠 *Commands:*\n"
        "`/summary` — Current month spending & income summary\n"
        "`/sync` — Pull BCA, PasporBCA & Google receipt emails from Gmail\n"
        "`/shortcut` — iPhone Shortcuts quick automation setup\n"
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
    limit: int | None = None
    if context.args:
        try:
            parsed_limit = int(context.args[0])
            if parsed_limit > 0:
                limit = parsed_limit
        except ValueError:
            pass

    status_msg = await update.message.reply_text(
        "🔄 Syncing BCA, PasporBCA & Google receipt emails from Gmail...(this may take a while)"
    )
    try:
        async with SYNC_LOCK:
            stats = await asyncio.to_thread(sync_from_gmail_imap, limit)
        logger.info(
            "Manual Telegram /sync (limit=%s): scanned=%d inserted=%d skipped=%d",
            limit if limit is not None else "all",
            stats["scanned"],
            stats["inserted"],
            stats["skipped_duplicates"],
        )
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


async def shortcut_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Displays authenticated endpoint details for iPhone Apple Shortcuts setup."""
    if not is_authorized(update.effective_user.id):
        await update.message.reply_text("⛔ Unauthorized user.")
        return
    token = _shortcut_api_token()
    if not token:
        await update.message.reply_text(
            "⚠️ Set `ALLOWED_TELEGRAM_USER_IDS` or `SHORTCUT_API_TOKEN` in Render Environment first "
            "to enable the iPhone Shortcut endpoint securely.",
            parse_mode="Markdown",
        )
        return

    base_url = WEBHOOK_URL or "https://your-render-service.onrender.com"
    endpoint = f"{base_url}/api/ask"
    msg = (
        "📱 *iPhone Shortcuts Automation Setup*\n\n"
        f"• URL: `{endpoint}`\n"
        "• Method: `POST`\n"
        f"• Header `Authorization`: `{token}`\n\n"
        "1️⃣ *Ask AI (Voice / Text):*\n"
        "• Request Body: `JSON` (`question`: `Provided Input` / `Dictated Text`)\n"
        "• `Get Value` for `answer` -> `Show Result`\n\n"
        "2️⃣ *1-Tap Screenshot Sync (Incoming Money / Mutasi):*\n"
        "• Action 1: `Take Screenshot` (while viewing myBCA Uang Masuk / Mutasi)\n"
        "• Action 2: `Get Contents of URL` (`POST`, Header `Authorization`, Request Body: `File` -> `Screenshot`)\n"
        "• Action 3: `Get Value` for `answer` -> `Show Result`\n\n"
        "💡 _Tip: Pair with Back Tap, Action Button, or Control Center!_"
    )
    await update.message.reply_text(msg, parse_mode="Markdown")


async def handle_voice_or_audio(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Transcribes Telegram voice messages / audio files with Gemini and answers via the AI agent."""
    uid = update.effective_user.id
    if not is_authorized(uid):
        await update.message.reply_text("⛔ Unauthorized user.")
        return

    media = update.message.voice or update.message.audio
    if not media:
        return
    if media.file_size and media.file_size > 20 * 1024 * 1024:
        await update.message.reply_text("⚠️ Audio file is larger than 20 MB.")
        return

    mime_type = getattr(media, "mime_type", None) or "audio/ogg"
    filename = getattr(media, "file_name", "") or ""
    thinking_msg = await update.message.reply_text("🎙 Listening to your voice message...")
    try:
        tg_file = await media.get_file()
        audio_bytes = bytes(await tg_file.download_as_bytearray())
        history = list(USER_HISTORY.get(uid, []))
        result = await asyncio.to_thread(
            answer_audio_question,
            audio_bytes,
            mime_type,
            history,
            filename,
        )
        _append_history(uid, result["transcript"], result["answer"])
        reply_text = f"🎙 Heard: \"{result['transcript']}\"\n\n{result['answer']}"
        await edit_with_long_response(thinking_msg, update, reply_text)
    except Exception as e:
        logger.exception("Voice/audio processing error")
        await thinking_msg.edit_text(f"⚠️ Could not process audio: {e}")


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
        summary_md = format_statement_sync_summary(result, markdown=True)
        await status_msg.edit_text(summary_md, parse_mode="Markdown")
    except Exception as e:
        await status_msg.edit_text(f"⚠️ Could not parse file: {e}")


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    uid = update.effective_user.id
    if not is_authorized(uid):
        await update.message.reply_text("⛔ Unauthorized user.")
        return

    question = update.message.text.strip()
    history = list(USER_HISTORY.get(uid, []))

    thinking_msg = await update.message.reply_text("💭 Analyzing your transactions...")
    try:
        answer = await asyncio.to_thread(ask_financial_ai, question, history)
        _append_history(uid, question, answer)
        await edit_with_long_response(thinking_msg, update, answer)
    except Exception as e:
        await thinking_msg.edit_text(f"⚠️ Error: {e}")


def _webhook_secret() -> str:
    """Derives a stable Telegram-compatible secret token ([A-Za-z0-9_-]) from the bot token."""
    if not TELEGRAM_BOT_TOKEN:
        return ""
    return hashlib.sha256(TELEGRAM_BOT_TOKEN.encode("utf-8")).hexdigest()[:32]


def _register_handlers(app_instance: Application) -> None:
    app_instance.add_handler(CommandHandler("start", start_cmd))
    app_instance.add_handler(CommandHandler("summary", summary_cmd))
    app_instance.add_handler(CommandHandler("sync", sync_cmd))
    app_instance.add_handler(CommandHandler("shortcut", shortcut_cmd))
    app_instance.add_handler(CommandHandler("clear", clear_cmd))
    app_instance.add_handler(MessageHandler(filters.VOICE | filters.AUDIO, handle_voice_or_audio))
    app_instance.add_handler(MessageHandler(filters.Document.ALL | filters.PHOTO, handle_document_or_photo))
    app_instance.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))


async def periodic_gmail_sync_task() -> None:
    """Background task that backfills full history on startup and polls recent Gmail every N minutes."""
    is_first_run = True
    while True:
        if GMAIL_APP_PASSWORD:
            try:
                limit: int | None = None if is_first_run else 50
                async with SYNC_LOCK:
                    stats = await asyncio.to_thread(sync_from_gmail_imap, limit)
                if is_first_run or stats["inserted"] > 0:
                    logger.info(
                        "Gmail sync (limit=%s): scanned=%d inserted=%d skipped=%d",
                        limit if limit is not None else "all",
                        stats["scanned"],
                        stats["inserted"],
                        stats["skipped_duplicates"],
                    )
            except Exception as e:
                logger.warning("Background Gmail sync warning: %s", e)
            is_first_run = False
        await asyncio.sleep(EMAIL_SYNC_INTERVAL_SECONDS)


telegram_app: Application | None = None
bg_sync_task: asyncio.Task | None = None
PENDING_WEBHOOK_TASKS: Set[asyncio.Task] = set()


async def _process_webhook_update(update: Update) -> None:
    try:
        if telegram_app:
            await telegram_app.process_update(update)
    except Exception:
        logger.exception("Error processing Telegram webhook update")


@asynccontextmanager
async def lifespan(app: FastAPI):
    global telegram_app, bg_sync_task
    init_db()
    bg_sync_task = asyncio.create_task(periodic_gmail_sync_task())

    if TELEGRAM_BOT_TOKEN:
        builder = Application.builder().token(TELEGRAM_BOT_TOKEN)
        if WEBHOOK_URL:
            # Webhook mode for Render / cloud hosting: wakes sleeping free-tier containers and avoids polling conflicts
            telegram_app = builder.updater(None).build()
            _register_handlers(telegram_app)
            await telegram_app.initialize()
            await telegram_app.start()
            webhook_target = f"{WEBHOOK_URL}/telegram-webhook"
            await telegram_app.bot.set_webhook(
                url=webhook_target,
                secret_token=_webhook_secret(),
                drop_pending_updates=False,
            )
            logger.info("Telegram bot started in WEBHOOK mode: %s", webhook_target)
        else:
            telegram_app = builder.build()
            _register_handlers(telegram_app)
            await telegram_app.initialize()
            await telegram_app.start()
            await telegram_app.bot.delete_webhook(drop_pending_updates=False)
            await telegram_app.updater.start_polling(drop_pending_updates=True)
            logger.info("Telegram bot started in POLLING mode.")
    else:
        logger.warning("TELEGRAM_BOT_TOKEN not set yet. Running web server only.")

    yield

    if bg_sync_task:
        bg_sync_task.cancel()
        try:
            await bg_sync_task
        except asyncio.CancelledError:
            pass
    if PENDING_WEBHOOK_TASKS:
        await asyncio.wait(list(PENDING_WEBHOOK_TASKS), timeout=20.0)
    if telegram_app:
        if telegram_app.updater and telegram_app.updater.running:
            await telegram_app.updater.stop()
        await telegram_app.stop()
        await telegram_app.shutdown()


app = FastAPI(title="myBCA & PasporBCA Financial AI Bot", lifespan=lifespan)


@app.post("/telegram-webhook")
async def telegram_webhook(request: Request):
    """Receives Telegram webhook POSTs cleanly and dispatches processing in the background."""
    if not telegram_app:
        raise HTTPException(status_code=503, detail="Telegram bot is not configured.")
    expected_secret = _webhook_secret()
    if expected_secret:
        header_secret = request.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
        if not hmac.compare_digest(header_secret, expected_secret):
            raise HTTPException(status_code=403, detail="Invalid webhook secret.")

    payload = await request.json()
    update = Update.de_json(payload, telegram_app.bot)
    if update is not None:
        task = asyncio.create_task(_process_webhook_update(update))
        PENDING_WEBHOOK_TASKS.add(task)
        task.add_done_callback(PENDING_WEBHOOK_TASKS.discard)
    return {"ok": True}


def _extract_bearer_token(request: Request) -> str:
    auth_header = request.headers.get("Authorization", "").strip()
    if auth_header.lower().startswith("bearer "):
        return auth_header[7:].strip()
    return auth_header or request.headers.get("X-Api-Key", "").strip()


@app.post("/api/ask")
@app.post("/api/ingest")
async def api_ask(request: Request):
    """
    Authenticated HTTP endpoint for iPhone Apple Shortcuts & quick automation:
    - JSON: {"question": "...", "session_id": "iphone"} (or {"action": "sync"})
    - Raw image / PDF (Content-Type: image/*, application/pdf, or auto-detected octet-stream):
      extracts incoming (IN) & outgoing (OUT) transactions with smart deduplication
    - Raw audio (Content-Type: audio/* or application/octet-stream): binary audio body
    """
    expected_token = _shortcut_api_token()
    if not expected_token:
        raise HTTPException(
            status_code=503,
            detail="Shortcut API is disabled until SHORTCUT_API_TOKEN or ALLOWED_TELEGRAM_USER_IDS is configured.",
        )

    provided_token = _extract_bearer_token(request)
    if not provided_token or not hmac.compare_digest(provided_token, expected_token):
        raise HTTPException(status_code=401, detail="Invalid or missing Shortcut API token.")

    content_type = (request.headers.get("Content-Type") or "application/json").split(";")[0].strip().lower()
    session_id = request.query_params.get("session_id") or "default"
    filename = request.headers.get("X-Filename") or request.headers.get("X-Audio-Filename") or ""

    is_binary_upload = (
        content_type.startswith("image/")
        or content_type == "application/pdf"
        or content_type.startswith("audio/")
        or content_type == "application/octet-stream"
    )
    if is_binary_upload:
        raw_bytes = await request.body()
        if not raw_bytes:
            raise HTTPException(status_code=400, detail="Empty upload body.")
        if len(raw_bytes) > 20 * 1024 * 1024:
            raise HTTPException(status_code=413, detail="Upload body exceeds 20 MB limit.")

        doc_or_img_mime = detect_statement_mime_type(raw_bytes, mime_type=content_type, filename=filename)
        if doc_or_img_mime:
            try:
                result = await asyncio.to_thread(parse_and_ingest_statement_file, raw_bytes, doc_or_img_mime)
                answer = format_statement_sync_summary(result, markdown=False)
                if telegram_app and ALLOWED_TELEGRAM_USER_IDS:
                    try:
                        await telegram_app.bot.send_message(
                            chat_id=ALLOWED_TELEGRAM_USER_IDS[0],
                            text=f"📱 *Synced via iPhone Shortcut*\n\n{format_statement_sync_summary(result, markdown=True)}",
                            parse_mode="Markdown",
                        )
                    except Exception as notify_err:
                        logger.warning("Optional Telegram shortcut notification skipped: %s", notify_err)
                return {"ok": True, "action": "ingest", "result": result, "answer": answer}
            except ValueError as e:
                raise HTTPException(status_code=400, detail=str(e))

        history_key = f"shortcut:{session_id}"
        history = list(USER_HISTORY.get(history_key, []))
        try:
            result = await asyncio.to_thread(
                answer_audio_question,
                raw_bytes,
                "audio/mp4" if content_type == "application/octet-stream" and not filename else content_type,
                history,
                filename,
            )
            _append_history(history_key, result["transcript"], result["answer"])
            return {"ok": True, "transcript": result["transcript"], "answer": result["answer"]}
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))

    try:
        payload = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Expected JSON body or audio/* payload.")

    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="JSON body must be an object.")

    raw_session = str(payload.get("session_id") or session_id)
    history_key = f"shortcut:{raw_session}"
    action = str(payload.get("action") or "").strip().lower()
    question = str(payload.get("question") or "").strip()

    if action == "sync" or question.lower() == "/sync":
        async with SYNC_LOCK:
            stats = await asyncio.to_thread(sync_from_gmail_imap, None)
        answer = (
            f"Gmail Sync Complete: scanned {stats['scanned']} emails, "
            f"added {stats['inserted']} new transactions, skipped {stats['skipped_duplicates']} duplicates."
        )
        return {"ok": True, "action": "sync", "stats": stats, "answer": answer}

    if not question:
        raise HTTPException(status_code=400, detail="Missing 'question' in JSON body.")

    history = list(USER_HISTORY.get(history_key, []))
    answer = await asyncio.to_thread(ask_financial_ai, question, history)
    _append_history(history_key, question, answer)
    return {"ok": True, "question": question, "answer": answer}


@app.get("/")
@app.get("/health")
def health_check():
    """Health check endpoint for Render / UptimeRobot (also keeps Supabase active)."""
    mode = "webhook" if WEBHOOK_URL else ("polling" if TELEGRAM_BOT_TOKEN else "web_only")
    try:
        rows = run_read_only_sql("SELECT COUNT(*) as total_tx FROM transactions;")
        return {
            "status": "ok",
            "service": "mybca-ai-bot",
            "mode": mode,
            "db_connected": True,
            "transactions_count": rows[0]["total_tx"] if rows else 0,
        }
    except Exception as e:
        logger.warning("Health check DB warning: %s", e)
        return {
            "status": "ok",
            "service": "mybca-ai-bot",
            "mode": mode,
            "db_connected": False,
            "error": str(e),
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
