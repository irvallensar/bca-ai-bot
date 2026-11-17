import json
import re
import time
import uuid
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from google import genai
from google.genai import types

from config import (
    DEFAULT_FX_RATES_TO_IDR,
    GEMINI_API_KEY,
    GEMINI_FALLBACK_MODELS,
    GEMINI_MODEL,
    MYBCA_CATEGORIES,
)
import database
from database import (
    IS_POSTGRES,
    fetch_financial_snapshot,
    get_db_mutation_version,
    init_db,
    run_read_only_sql,
    set_merchant_category_rule,
    upsert_transaction,
)

_SNAPSHOT_CACHE: Optional[Tuple[Tuple[Any, ...], str]] = None
_CLIENT_CACHE: Dict[Tuple[int, str, int], Any] = {}
_LAST_WORKING_MODEL: Optional[str] = None
_LAST_WORKING_TS: float = 0.0


def execute_sql_query(sql_query: str) -> str:
    """
    Executes a read-only SQL SELECT query against the user's `transactions` table
    and returns the results as a JSON string.

    Table schema: `transactions`
    - id (INTEGER)
    - tx_date (TEXT, format 'YYYY-MM-DD HH:MM:SS')
    - tx_month (TEXT, format 'YYYY-MM', e.g. '2026-11', '2026-10')
    - direction (TEXT: 'OUT' for spending/outgoing, 'IN' for income/earning)
    - source_channel (TEXT: 'myBCA Email', 'PasporBCA Email', 'PasporBCA ATM', 'Google Play Email', 'Google Payments Email', 'e-Statement PDF', 'Manual Telegram')
    - tx_type (TEXT: 'QRIS Payment', 'Transfer to BCA Account', 'Transfer to BCA Virtual Account', 'Flazz Top Up', 'Overseas Debit Card', 'Overseas ATM Withdrawal', 'Auto-renewing Subscription', 'Google Play Purchase', 'Google Payments Purchase', 'Manual Entry')
    - merchant_or_party (TEXT: merchant name, beneficiary name, or sender name)
    - beneficiary_account (TEXT: account number or VA number if applicable)
    - location (TEXT: city/region)
    - country (TEXT: 'INDONESIA', 'JAPAN', etc.)
    - category (TEXT: 'Food & Beverage', 'Groceries', 'Account Transfer', 'Transportation & Travel', 'Shopping', 'Business Expense', 'Cash Withdrawal', 'Bills & Utilities', 'Admin Fee', 'Donation', 'Gift', 'Miscellaneous', 'Income / Earning')
    - currency (TEXT: 'IDR', 'JPY', etc.)
    - amount_original (REAL: amount in original currency, e.g. 1450 JPY or 45000 IDR)
    - fx_rate_to_idr (REAL: exchange rate to IDR)
    - amount_idr (REAL: total amount in IDR — always sum this column for unified cashflow totals!)
    - remarks (TEXT: transfer notes or admin fee details)
    """
    try:
        rows = run_read_only_sql(sql_query)
        return json.dumps({"status": "ok", "row_count": len(rows), "rows": rows[:200]}, default=str)
    except Exception as e:
        return json.dumps({"status": "error", "error": str(e)})


def log_manual_transaction(
    direction: str,
    amount: float,
    merchant_or_party: str,
    category: str,
    currency: str = "IDR",
    remarks: str = "",
    tx_date: str = "",
) -> str:
    """
    Logs a manual income/earning or cash expense into the database when the user
    tells the bot things like '+500k from friend', 'Received 10jt salary', or 'Spent 25k cash'.
    - direction: 'IN' (for income/receiving money) or 'OUT' (for spending)
    - amount: numeric amount (e.g. 500000 for 500k)
    - merchant_or_party: who sent the money or where it was spent
    - category: one of the myBCA categories ('Income / Earning' for incoming money)
    - currency: default 'IDR' (or 'JPY', 'USD', etc.)
    - remarks: optional note
    - tx_date: optional 'YYYY-MM-DD HH:MM:SS' (leave empty for current time)
    """
    try:
        init_db()
        now_str = tx_date.strip() if tx_date.strip() else datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        if len(now_str) == 10:
            now_str += " 12:00:00"
        curr = currency.upper().strip() or "IDR"
        fx = DEFAULT_FX_RATES_TO_IDR.get(curr, 1.0)
        amt_idr = round(float(amount) * fx, 2)
        dir_clean = "IN" if direction.upper().startswith("I") else "OUT"
        cat_clean = "Income / Earning" if dir_clean == "IN" and category not in MYBCA_CATEGORIES else category

        tx = {
            "dedup_key": f"manual:{uuid.uuid4().hex[:16]}",
            "tx_date": now_str,
            "direction": dir_clean,
            "source_channel": "Manual Telegram",
            "tx_type": "Manual Income" if dir_clean == "IN" else "Manual Expense",
            "merchant_or_party": merchant_or_party,
            "country": "JAPAN" if curr == "JPY" else "INDONESIA",
            "category": cat_clean,
            "currency": curr,
            "amount_original": float(amount),
            "fx_rate_to_idr": fx,
            "amount_idr": amt_idr,
            "remarks": remarks,
        }
        inserted = upsert_transaction(tx)
        return json.dumps({
            "status": "ok",
            "inserted": inserted,
            "transaction": tx,
        })
    except Exception as e:
        return json.dumps({"status": "error", "error": str(e)})


def recategorize_merchant(merchant_pattern: str, new_category: str) -> str:
    """
    Updates the category for all past and future transactions matching `merchant_pattern`.
    Use this when the user says e.g. 'Categorize SOME_MERCHANT under Business Expense'.
    Allowed categories: Food & Beverage, Groceries, Account Transfer, Transportation & Travel,
    Shopping, Business Expense, Cash Withdrawal, Bills & Utilities, Admin Fee, Donation, Gift, Miscellaneous, Income / Earning.
    """
    try:
        updated_count = set_merchant_category_rule(merchant_pattern, new_category)
        return json.dumps({
            "status": "ok",
            "merchant_pattern": merchant_pattern,
            "new_category": new_category,
            "updated_transactions_count": updated_count,
        })
    except Exception as e:
        return json.dumps({"status": "error", "error": str(e)})


def get_financial_context_block() -> str:
    """
    Pre-fetches a compact SQL summary in a single DB connection and caches it in memory
    (invalidated immediately on DB writes or after 60s) so repeat questions need 0 DB
    round-trips and finish in a single fast Gemini call.
    """
    global _SNAPSHOT_CACHE
    try:
        cache_key = (database.DATABASE_URL, get_db_mutation_version(), int(time.monotonic() // 60))
        if _SNAPSHOT_CACHE is not None and _SNAPSHOT_CACHE[0] == cache_key:
            return _SNAPSHOT_CACHE[1]

        snap = fetch_financial_snapshot()
        block = (
            f"Total DB transactions: {snap['total_rows']}\n"
            f"Monthly Totals by Country/Currency (recent 28 groups): {json.dumps(snap['monthly_totals'])}\n"
            f"Monthly Totals by Category (recent 40 groups): {json.dumps(snap['category_totals'])}\n"
            f"Category & Merchant Breakdown (top 40 groups): {json.dumps(snap['merchant_breakdown'])}\n"
            f"Latest 25 Transactions: {json.dumps(snap['recent_tx'])}"
        )
        _SNAPSHOT_CACHE = (cache_key, block)
        return block
    except Exception:
        return "Database snapshot unavailable; use execute_sql_query."


def strip_markdown_syntax(text: str) -> str:
    """Normalizes any leftover Markdown syntax into clean plain text with Unicode bullets."""
    # Convert leading markdown bullets (- or *) into clean • bullets while keeping indentation
    cleaned = re.sub(r"^(\s*)[*-]\s+", r"\1• ", text, flags=re.MULTILINE)
    # Remove markdown heading markers (e.g. ### Title -> Title)
    cleaned = re.sub(r"^\s*#{1,6}\s+", "", cleaned, flags=re.MULTILINE)
    # Remove horizontal rule lines (---, ***, ___)
    cleaned = re.sub(r"^\s*[-*_]{3,}\s*$", "", cleaned, flags=re.MULTILINE)
    # Remove bold (**text**, __text__), italic (*text*), and inline code (`text`) wrappers
    cleaned = re.sub(r"\*\*(.+?)\*\*", r"\1", cleaned)
    cleaned = re.sub(r"__(.+?)__", r"\1", cleaned)
    cleaned = re.sub(r"(?<!\w)\*(?!\s)([^*\n]+?)(?<!\s)\*(?!\w)", r"\1", cleaned)
    cleaned = re.sub(r"`([^`\n]+?)`", r"\1", cleaned)
    # Collapse excessive blank lines left by removed --- dividers
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
    return cleaned.strip()


def _get_genai_client(timeout_ms: int = 18000) -> Any:
    """Reuses a warm Gemini HTTP client pool unless `genai.Client` is mocked in tests."""
    key = (id(genai.Client), GEMINI_API_KEY, timeout_ms)
    client = _CLIENT_CACHE.get(key)
    if client is None:
        client = genai.Client(api_key=GEMINI_API_KEY, http_options=types.HttpOptions(timeout=timeout_ms))
        if len(_CLIENT_CACHE) > 8:
            _CLIENT_CACHE.clear()
        _CLIENT_CACHE[key] = client
    return client


def _record_working_model(model_name: str) -> None:
    global _LAST_WORKING_MODEL, _LAST_WORKING_TS
    _LAST_WORKING_MODEL = model_name
    _LAST_WORKING_TS = time.monotonic()


def _model_candidates() -> List[str]:
    seen = set()
    ordered = []
    for m in [GEMINI_MODEL] + GEMINI_FALLBACK_MODELS:
        if m and m not in seen:
            seen.add(m)
            ordered.append(m)
    if (
        _LAST_WORKING_MODEL
        and _LAST_WORKING_MODEL in ordered
        and (time.monotonic() - _LAST_WORKING_TS) < 900.0
    ):
        ordered.remove(_LAST_WORKING_MODEL)
        ordered.insert(0, _LAST_WORKING_MODEL)
    return ordered


def _is_retryable_model_error(err: Exception) -> bool:
    msg = str(err).upper()
    return any(
        token in msg
        for token in (
            "429",
            "RESOURCE_EXHAUSTED",
            "404",
            "NOT_FOUND",
            "TIMEOUT",
            "READTIMEOUT",
            "DEADLINE_EXCEEDED",
            "500",
            "502",
            "503",
            "504",
            "UNAVAILABLE",
            "INTERNAL",
            "SERVERERROR",
            "OVERLOADED",
        )
    )


_AUDIO_MIME_ALIASES = {
    "audio/x-m4a": "audio/mp4",
    "audio/m4a": "audio/mp4",
    "audio/x-wav": "audio/wav",
    "audio/wave": "audio/wav",
    "audio/opus": "audio/ogg",
    "audio/mp3": "audio/mpeg",
    "application/ogg": "audio/ogg",
}

_AUDIO_EXT_TO_MIME = {
    ".ogg": "audio/ogg",
    ".oga": "audio/ogg",
    ".opus": "audio/ogg",
    ".mp3": "audio/mpeg",
    ".m4a": "audio/mp4",
    ".mp4": "audio/mp4",
    ".wav": "audio/wav",
    ".aac": "audio/aac",
    ".flac": "audio/flac",
}


def normalize_audio_mime_type(
    mime_type: str | None,
    filename: str = "",
    default: str = "audio/ogg",
) -> str:
    """Normalizes Telegram/iOS audio MIME types into formats accepted by Gemini."""
    clean = (mime_type or "").split(";")[0].strip().lower()
    if clean in _AUDIO_MIME_ALIASES:
        return _AUDIO_MIME_ALIASES[clean]
    if clean.startswith("audio/"):
        return clean
    lower_name = (filename or "").strip().lower()
    for ext, mapped in _AUDIO_EXT_TO_MIME.items():
        if lower_name.endswith(ext):
            return mapped
    return default


def transcribe_audio_with_gemini(
    audio_bytes: bytes,
    mime_type: str = "audio/ogg",
    filename: str = "",
) -> str:
    """Transcribes voice/audio recordings into plain text using Gemini multimodal fallback."""
    if not GEMINI_API_KEY:
        raise ValueError("GEMINI_API_KEY is not set.")
    if not audio_bytes:
        raise ValueError("Audio recording is empty.")

    clean_mime = normalize_audio_mime_type(mime_type, filename=filename)
    client = _get_genai_client(timeout_ms=25000)
    prompt = (
        "Transcribe this spoken audio verbatim in its original language (Indonesian or English), "
        "preserving numbers, currency amounts, dates, and merchant names. "
        "Output ONLY the plain-text transcription with no commentary or quotation marks."
    )

    last_err: Exception | None = None
    for model_name in _model_candidates():
        try:
            response = client.models.generate_content(
                model=model_name,
                contents=[
                    types.Part.from_bytes(data=audio_bytes, mime_type=clean_mime),
                    prompt,
                ],
                config=types.GenerateContentConfig(
                    thinking_config=types.ThinkingConfig(thinking_level="LOW"),
                    temperature=0.0,
                ),
            )
            transcript = strip_markdown_syntax(response.text or "")
            if not transcript:
                raise ValueError("Could not detect clear speech in the audio recording.")
            _record_working_model(model_name)
            return transcript
        except ValueError:
            raise
        except Exception as e:
            last_err = e
            if _is_retryable_model_error(e):
                continue
            raise

    raise RuntimeError(f"Audio transcription failed across Gemini models: {last_err}")


def answer_audio_question(
    audio_bytes: bytes,
    mime_type: str = "audio/ogg",
    chat_history: List[Dict[str, str]] | None = None,
    filename: str = "",
) -> Dict[str, str]:
    """Transcribes an audio recording and routes it through `ask_financial_ai`."""
    transcript = transcribe_audio_with_gemini(audio_bytes, mime_type=mime_type, filename=filename)
    answer = ask_financial_ai(transcript, chat_history)
    return {"transcript": transcript, "answer": answer}


def ask_financial_ai(user_question: str, chat_history: List[Dict[str, str]] | None = None) -> str:
    """
    Runs the Gemini Financial Advisor agent with pre-grounded SQL context,
    SQL Tool Calling, manual logging, recategorization, and automatic model fallback.
    """
    if not GEMINI_API_KEY:
        return (
            "⚠️ `GEMINI_API_KEY` is not set yet. Get a free API key from "
            "https://aistudio.google.com/apikey and add it to your `.env` file!"
        )

    init_db()
    sql_dialect = "PostgreSQL" if IS_POSTGRES else "SQLite"
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    db_context = get_financial_context_block()

    system_instruction = f"""
You are a personal myBCA & PasporBCA Financial AI Assistant.
Current local date/time: {now_str}
Database SQL Dialect: {sql_dialect}

Pre-loaded Live SQL Summary:
{db_context}

You also have tools: `execute_sql_query` (for deeper/older queries not covered above), `log_manual_transaction`, and `recategorize_merchant`.

Core Guidelines:
1. NEVER guess or hallucinate numbers. Answer directly from the Pre-loaded Live SQL Summary whenever all needed numbers are already there (so you respond in 1 fast call!). Call `execute_sql_query` ONLY if the question needs older history, custom filters, or details not in the summary above.
2. Strict Grounding: ONLY state categories, merchants, transaction counts, and amounts that explicitly appear in the pre-loaded SQL data or `execute_sql_query` results. Never extrapolate, split, or guess unqueried breakdowns.
3. For complex questions like "How can I reduce/save my spendings?" or "How can I reduce food/beverage spendings?":
   - Check both `Food & Beverage` and related convenience-store/snack purchases (e.g. `Groceries` like `SEVEN-ELEVEN` or `MIDI REGULER D`), plus merchant counts, exact IDR/JPY totals, and transaction timestamps.
   - Cite the exact merchants, exact transaction counts, and exact IDR/JPY amounts.
   - Give concrete, actionable monthly savings targets (e.g., "Cutting X from 4x/month to 2x/month saves IDR Y").
4. Remember that `amount_idr` holds the unified IDR amount for both local (`IDR`) and overseas (`JPY`) transactions, while `amount_original` and `currency` hold the original currency value. Mention both when discussing overseas/Japan transactions!
5. Formatting: Output clean PLAIN TEXT only. Do NOT use Markdown syntax (`**`, `*`, `#`, `###`, `---`, backticks, or Markdown tables). Instead, use clean line breaks, emoji/plain-text section headers, and `•` Unicode bullets for lists, keeping responses under 3,500 characters.
"""

    client = _get_genai_client(timeout_ms=18000)
    history = []
    if chat_history:
        for msg in chat_history[-6:]:
            role = "user" if msg["role"] == "user" else "model"
            history.append(types.Content(role=role, parts=[types.Part.from_text(text=msg["text"])]))

    config = types.GenerateContentConfig(
        system_instruction=system_instruction,
        tools=[execute_sql_query, log_manual_transaction, recategorize_merchant],
        thinking_config=types.ThinkingConfig(thinking_level="LOW"),
        automatic_function_calling=types.AutomaticFunctionCallingConfig(maximum_remote_calls=2),
        temperature=0.1,
    )

    last_err: Exception | None = None
    for model_name in _model_candidates():
        try:
            chat = client.chats.create(model=model_name, config=config, history=history or None)
            response = chat.send_message(user_question)
            raw_text = response.text or "I processed your request, but no text response was generated."
            _record_working_model(model_name)
            return strip_markdown_syntax(raw_text)
        except Exception as e:
            last_err = e
            if _is_retryable_model_error(e):
                continue
            raise

    return (
        f"⚠️ All configured Gemini free-tier models are currently rate-limited or unavailable.\n"
        f"Last error: {last_err}"
    )
