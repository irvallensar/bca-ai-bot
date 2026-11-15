import json
from typing import Any, Dict, List

from google import genai
from google.genai import types

from config import GEMINI_API_KEY, GEMINI_FALLBACK_MODELS, GEMINI_MODEL, MYBCA_CATEGORIES
from database import init_db, upsert_transaction

STATEMENT_EXTRACTION_PROMPT = f"""
You are a precision financial parser for Bank Central Asia (BCA / myBCA) e-Statement PDFs and transaction screenshots.
Extract ALL transactions from the attached document/image into a JSON array of objects.

Important rules:
1. In BCA e-Statements (Mutasi Rekening):
   - Rows marked 'CR' (Credit) or incoming transfers (e.g. BI-FAST CR, TRSF E-BANKING CR, BUNGA) have `direction: "IN"` and `category: "Income / Earning"`.
   - Rows marked 'DB' (Debit) or outgoing payments (e.g. QRIS, TRSF E-BANKING DB, KARTU DEBIT, BIAYA ADM, PAJAK BUNGA, TARIK TUNAI) have `direction: "OUT"`.
2. For overseas debit/ATM transactions on e-Statements, BCA lists the exact IDR amount debited from the account and often includes the original currency (e.g. JPY) in the description. Extract `amount_idr` as the exact IDR amount debited, and if JPY/foreign currency is shown, fill `currency` and `amount_original` too (otherwise set `currency: "IDR"` and `amount_original` equal to `amount_idr`).
3. Choose `category` strictly from this list:
   {json.dumps(MYBCA_CATEGORIES)}
4. Format `tx_date` as "YYYY-MM-DD HH:MM:SS" (if only the date is shown on the statement, use "YYYY-MM-DD 12:00:00"; infer the year from the statement header).

Return ONLY a valid JSON array where each element has:
{{
  "tx_date": "YYYY-MM-DD HH:MM:SS",
  "direction": "IN" or "OUT",
  "tx_type": "e.g. Incoming Transfer, QRIS Payment, Overseas Debit, Admin Fee, etc.",
  "merchant_or_party": "Clean merchant or counterparty name",
  "country": "INDONESIA or JAPAN or OVERSEAS",
  "category": "One of the allowed myBCA categories",
  "currency": "IDR or JPY or USD etc.",
  "amount_original": 45000.0,
  "amount_idr": 45000.0,
  "remarks": "Any extra details or description from the statement row"
}}
"""


def parse_and_ingest_statement_file(file_bytes: bytes, mime_type: str = "application/pdf") -> Dict[str, Any]:
    """
    Sends a myBCA e-Statement PDF or receipt screenshot to Gemini 3.6 Flash,
    extracts all transactions (both IN and OUT), and upserts them into the database
    with automatic deduplication.
    """
    if not GEMINI_API_KEY:
        raise ValueError("GEMINI_API_KEY is not set in environment/.env.")

    init_db()
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
                    types.Part.from_bytes(data=file_bytes, mime_type=mime_type),
                    STATEMENT_EXTRACTION_PROMPT,
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

    items: List[Dict[str, Any]] = json.loads(response.text)
    inserted = 0
    skipped = 0
    in_total_idr = 0.0
    out_total_idr = 0.0

    for item in items:
        amt_idr = float(item.get("amount_idr") or 0.0)
        amt_orig = float(item.get("amount_original") or amt_idr)
        if amt_idr <= 0:
            continue
        fx = round(amt_idr / amt_orig, 4) if amt_orig > 0 else 1.0
        direction = item.get("direction", "OUT").upper()
        tx_record = {
            "reference_no": None,
            "email_message_id": None,
            "tx_date": item["tx_date"],
            "direction": direction,
            "source_channel": "e-Statement PDF" if "pdf" in mime_type else "Screenshot Upload",
            "tx_type": item.get("tx_type", "Statement Entry"),
            "merchant_or_party": item.get("merchant_or_party", "Unknown"),
            "location": item.get("country", "INDONESIA"),
            "country": item.get("country", "INDONESIA").upper(),
            "category": item.get("category", "Income / Earning" if direction == "IN" else "Miscellaneous"),
            "currency": item.get("currency", "IDR").upper(),
            "amount_original": amt_orig,
            "fx_rate_to_idr": fx,
            "amount_idr": amt_idr,
            "remarks": item.get("remarks", ""),
            "raw_excerpt": json.dumps(item),
        }
        if upsert_transaction(tx_record):
            inserted += 1
            if direction == "IN":
                in_total_idr += amt_idr
            else:
                out_total_idr += amt_idr
        else:
            skipped += 1

    return {
        "extracted_count": len(items),
        "inserted": inserted,
        "skipped_duplicates": skipped,
        "new_income_idr": in_total_idr,
        "new_spending_idr": out_total_idr,
    }
