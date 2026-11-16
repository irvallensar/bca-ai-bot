import json
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List

from google import genai
from google.genai import types

from config import GEMINI_API_KEY, GEMINI_FALLBACK_MODELS, GEMINI_MODEL, MYBCA_CATEGORIES
from database import init_db, reconcile_and_upsert_statement_transactions
from email_parser import guess_category

WIB_TZ = timezone(timedelta(hours=7))

_IMAGE_OR_PDF_EXT_MAP = {
    ".pdf": "application/pdf",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".heic": "image/heic",
    ".heif": "image/heif",
}


def detect_statement_mime_type(file_bytes: bytes, mime_type: str = "", filename: str = "") -> str:
    """
    Detects whether `file_bytes` / `mime_type` / `filename` is a supported PDF or image
    (PNG, JPEG, WEBP, HEIC/HEIF). Returns the normalized MIME string, or '' if not an image/PDF.
    """
    if file_bytes:
        if file_bytes.startswith(b"%PDF-"):
            return "application/pdf"
        if file_bytes.startswith(b"\x89PNG\r\n\x1a\n"):
            return "image/png"
        if file_bytes.startswith(b"\xff\xd8\xff"):
            return "image/jpeg"
        if len(file_bytes) >= 12 and file_bytes[:4] == b"RIFF" and file_bytes[8:12] == b"WEBP":
            return "image/webp"
        if len(file_bytes) >= 12 and file_bytes[4:12] in (
            b"ftypheic",
            b"ftypheix",
            b"ftyphevc",
            b"ftypmif1",
            b"ftypmsf1",
        ):
            return "image/heic"

    clean = (mime_type or "").split(";")[0].strip().lower()
    if clean == "image/jpg":
        return "image/jpeg"
    if clean == "application/pdf" or clean.startswith("image/"):
        return clean

    lower_name = (filename or "").strip().lower()
    for ext, mapped in _IMAGE_OR_PDF_EXT_MAP.items():
        if lower_name.endswith(ext):
            return mapped
    return ""


def build_statement_extraction_prompt() -> str:
    today_wib = datetime.now(WIB_TZ).strftime("%Y-%m-%d")
    return f"""
You are a precision financial parser for Bank Central Asia (BCA / myBCA / BCA mobile) e-Statement PDFs, Mutasi Rekening screens, and transaction/notification screenshots.
Current local date (WIB): {today_wib}

Extract ALL individual transactions from the attached document/image into a JSON array of objects.

Important rules:
1. Direction ('IN' vs 'OUT'):
   - Rows or notifications marked 'CR' (Credit), 'Uang Masuk', 'Dana Masuk', 'Kredit', '+' (green/incoming amount), or incoming transfers into the user's account (e.g. BI-FAST CR, TRSF E-BANKING CR, SWITCHING CR, BUNGA, salary/incoming payout) MUST have `direction: "IN"` and `category: "Income / Earning"`.
   - Rows marked 'DB' (Debit), 'Uang Keluar', 'Debit', '-', or outgoing payments (e.g. QRIS, TRSF E-BANKING DB, KARTU DEBIT, BIAYA ADM, PAJAK BUNGA, TARIK TUNAI) have `direction: "OUT"`.
   - Do NOT extract summary header/footer balances (e.g. 'Saldo Awal', 'Saldo Akhir', 'Total Uang Masuk', 'Total Mutasi Kredit/Debet'); only extract actual transaction items.
2. Currency & Amounts:
   - For overseas debit/ATM transactions on e-Statements, BCA lists the exact IDR amount debited from the account and often includes the original currency (e.g. JPY) in the description. Extract `amount_idr` as the exact IDR amount debited, and if JPY/foreign currency is shown, fill `currency` and `amount_original` too (otherwise set `currency: "IDR"` and `amount_original` equal to `amount_idr`).
3. Categories:
   - Choose `category` strictly from this list:
     {json.dumps(MYBCA_CATEGORIES)}
4. Dates:
   - Format `tx_date` as "YYYY-MM-DD HH:MM:SS" (if only the date is shown, use "YYYY-MM-DD 12:00:00").
   - If the screenshot uses relative dates like 'Hari ini'/'Today', use `{today_wib}`. If a month/day is shown without a year (e.g. '16 Nov'), infer the year from the statement header or `{today_wib[:4]}`.

Return ONLY a valid JSON array where each element has:
{{
  "tx_date": "YYYY-MM-DD HH:MM:SS",
  "direction": "IN" or "OUT",
  "tx_type": "e.g. Incoming Transfer, QRIS Payment, Overseas Debit, Admin Fee, etc.",
  "merchant_or_party": "Clean sender/counterparty or merchant name",
  "country": "INDONESIA or JAPAN or OVERSEAS",
  "category": "One of the allowed myBCA categories",
  "currency": "IDR or JPY or USD etc.",
  "amount_original": 45000.0,
  "amount_idr": 45000.0,
  "remarks": "Any extra details or description from the row"
}}
"""


STATEMENT_EXTRACTION_PROMPT = build_statement_extraction_prompt()


def format_statement_sync_summary(result: Dict[str, Any], markdown: bool = False) -> str:
    """Formats the statement/screenshot ingestion result for Telegram or iPhone Shortcuts."""
    b = "*" if markdown else ""
    c = "`" if markdown else ""
    lines = [
        f"✅ {b}Statement / Screenshot Synced!{b}",
        f"• Transactions detected: {c}{result['extracted_count']}{c}",
        f"• New rows inserted: {c}{result['inserted']}{c}",
        f"• Already in DB (skipped): {c}{result['skipped_duplicates']}{c}",
        f"• New Income added: {c}IDR {result['new_income_idr']:,.0f}{c}",
        f"• New Spending added: {c}IDR {result['new_spending_idr']:,.0f}{c}",
    ]
    if result.get("fx_updated", 0) > 0:
        lines.append(f"• Foreign card FX rates updated: {c}{result['fx_updated']}{c}")

    inserted_items = result.get("inserted_items") or []
    if inserted_items:
        lines.append("")
        lines.append(f"{b}New entries logged:{b}")
        for tx in inserted_items[:8]:
            sign = "+" if tx.get("direction") == "IN" else "-"
            tag = "IN" if tx.get("direction") == "IN" else "OUT"
            day = str(tx.get("tx_date", ""))[:10]
            party = tx.get("merchant_or_party", "Unknown")
            amt = float(tx.get("amount_idr") or 0.0)
            lines.append(f"• [{tag}] {day} — {party}: {c}{sign}IDR {amt:,.0f}{c}")
        if len(inserted_items) > 8:
            lines.append(f"• ...and {len(inserted_items) - 8} more")
    return "\n".join(lines)


def parse_and_ingest_statement_file(file_bytes: bytes, mime_type: str = "application/pdf") -> Dict[str, Any]:
    """
    Sends a myBCA e-Statement PDF or banking/receipt screenshot to Gemini,
    extracts all transactions (both IN and OUT), and reconciles/upserts them into
    the database with cross-source deduplication against existing email records.
    """
    if not GEMINI_API_KEY:
        raise ValueError("GEMINI_API_KEY is not set in environment/.env.")

    init_db()
    resolved_mime = detect_statement_mime_type(file_bytes, mime_type=mime_type) or mime_type or "application/pdf"
    prompt = build_statement_extraction_prompt()
    client = genai.Client(api_key=GEMINI_API_KEY, http_options=types.HttpOptions(timeout=60000))

    candidates = []
    for m in [GEMINI_MODEL] + GEMINI_FALLBACK_MODELS:
        if m and m not in candidates:
            candidates.append(m)

    response = None
    last_err: Exception | None = None
    for model_name in candidates:
        try:
            response = client.models.generate_content(
                model=model_name,
                contents=[
                    types.Part.from_bytes(data=file_bytes, mime_type=resolved_mime),
                    prompt,
                ],
                config=types.GenerateContentConfig(
                    response_mime_type="application/json",
                    thinking_config=types.ThinkingConfig(thinking_level="LOW"),
                    temperature=0.1,
                ),
            )
            break
        except Exception as e:
            last_err = e
            msg = str(e).upper()
            if any(t in msg for t in ("429", "RESOURCE_EXHAUSTED", "404", "NOT_FOUND", "TIMEOUT", "503")):
                continue
            raise

    if response is None:
        raise RuntimeError(f"All Gemini models failed or hit quota limits: {last_err}")

    parsed_json = json.loads(response.text or "[]")
    items: List[Dict[str, Any]] = parsed_json if isinstance(parsed_json, list) else []

    tx_records: List[Dict[str, Any]] = []
    source_channel = "e-Statement PDF" if "pdf" in resolved_mime else "Screenshot Upload"
    for item in items:
        if not isinstance(item, dict):
            continue
        amt_idr = float(item.get("amount_idr") or 0.0)
        amt_orig = float(item.get("amount_original") or amt_idr)
        if amt_idr <= 0:
            continue
        fx = round(amt_idr / amt_orig, 4) if amt_orig > 0 else 1.0
        direction = str(item.get("direction", "OUT")).upper()
        if direction not in ("IN", "OUT"):
            direction = "OUT"
        merchant = str(item.get("merchant_or_party") or "Unknown").strip()
        tx_type = str(item.get("tx_type") or ("Incoming Transfer" if direction == "IN" else "Statement Entry")).strip()
        remarks = str(item.get("remarks") or "").strip()

        raw_cat = str(item.get("category") or "").strip()
        if direction == "IN":
            category = "Income / Earning"
        elif raw_cat in MYBCA_CATEGORIES and raw_cat != "Miscellaneous":
            category = raw_cat
        else:
            category = guess_category(merchant, tx_type, remarks)

        tx_date = str(item.get("tx_date") or datetime.now(WIB_TZ).strftime("%Y-%m-%d 12:00:00")).strip()
        if len(tx_date) == 10:
            tx_date += " 12:00:00"

        tx_records.append({
            "reference_no": None,
            "email_message_id": None,
            "tx_date": tx_date,
            "direction": direction,
            "source_channel": source_channel,
            "tx_type": tx_type,
            "merchant_or_party": merchant,
            "location": str(item.get("country") or "INDONESIA"),
            "country": str(item.get("country") or "INDONESIA").upper(),
            "category": category,
            "currency": str(item.get("currency") or "IDR").upper(),
            "amount_original": amt_orig,
            "fx_rate_to_idr": fx,
            "amount_idr": amt_idr,
            "remarks": remarks,
            "raw_excerpt": json.dumps(item),
        })

    reconcile_res = reconcile_and_upsert_statement_transactions(tx_records)
    inserted_items = reconcile_res["inserted_items"]
    in_total_idr = sum(float(t["amount_idr"]) for t in inserted_items if t.get("direction") == "IN")
    out_total_idr = sum(float(t["amount_idr"]) for t in inserted_items if t.get("direction") != "IN")

    return {
        "extracted_count": len(tx_records),
        "inserted": reconcile_res["inserted"],
        "skipped_duplicates": reconcile_res["skipped_duplicates"],
        "fx_updated": reconcile_res["fx_updated"],
        "new_income_idr": in_total_idr,
        "new_spending_idr": out_total_idr,
        "inserted_items": [
            {
                "tx_date": t["tx_date"],
                "direction": t["direction"],
                "merchant_or_party": t["merchant_or_party"],
                "category": t["category"],
                "currency": t["currency"],
                "amount_original": t["amount_original"],
                "amount_idr": t["amount_idr"],
            }
            for t in inserted_items
        ],
    }
