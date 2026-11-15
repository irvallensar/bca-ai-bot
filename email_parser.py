import html
import re
from datetime import datetime
from typing import Any, Dict, Optional

from config import DEFAULT_FX_RATES_TO_IDR

# Keyword-based auto-categorizer aligned with myBCA Financial Diary categories
CATEGORY_KEYWORDS = {
    "Food & Beverage": [
        "JCO", "DONUT", "COFFEE", "STARBUCKS", "KOPKEN", "KOPI", "CAFE",
        "HAOZ", "DCREPES", "BANKARA", "RAMEN", "SUSHI", "GYUKAKU", "YOSHINOYA",
        "SUKIYA", "MATSUYA", "MCDONALD", "KFC", "BURGER", "PIZZA", "BAKMI",
        "RESTO", "RESTAURANT", "WARUNG", "PADANG", "SATE", "BENTO", "ICHIRAN",
        "COCO ICHIBANYA", "TORIKIZOKU", "SAIZERIYA", "GUSTO", "DOUTOR", "TULLY",
        "KOMEDA", "MATCHA", "BAKERY", "ROTI", "HOKBEN", "CHATIME", "MIXUE",
        "FORE", "TOMORO", "JANJI JIWA", "LAWSON STATION", "FOOD", "BEVERAGE",
    ],
    "Transportation & Travel": [
        "FLAZZ", "GOPAY TOPUP", "DOMPET ANAK BANGSA", "GRAB", "GOJEK", "BLUEBIRD",
        "TAXI", "KAI", "KCIC", "WHOOSH", "MRT", "TRANSJAKARTA", "JR EAST",
        "SUICA", "PASMO", "ICOCA", "TOKYO METRO", "SHINKANSEN", "ANA", "JAL",
        "GARUDA", "AIRASIA", "TRAVELOKA", "TIKET.COM", "AGODA", "BOOKING.COM",
        "PARKIR", "SECURE PARKING", "PERTAMINA", "SHELL",
    ],
    "Shopping": [
        "UNIQLO", "GU ", "DON QUIJOTE", "DONQUIJOTE", "DONKI", "DAISO", "MUJI", "ZARA",
        "H&M", "ABC-MART", "YODOBASHI", "BIC CAMERA", "MATSUMOTO KIYOSHI",
        "WELCIA", "SUNDRUG", "TOKOPEDIA", "SHOPEE", "LAZADA", "BLIBLI",
        "TIKTOK SHOP", "AMAZON", "MERCARI", "IKEA", "NITORI", "GRAMEDIA",
    ],
    "Groceries": [
        "MIDI REGULER", "ALFAMIDI", "ALFAMART", "INDOMARET", "SEVEN-ELEVEN",
        "7-ELEVEN", "FAMILYMART", "FAMILY MART", "LAWSON", "MINISTOP",
        "SUPERINDO", "HYPERMART", "TRANSMART", "FARMERS MARKET", "RANCH MARKET",
        "AEON", "LIFE CORP", "GYOMU", "SEIYU", "OK STORE", "SUPERMARKET",
        "GROCER",
    ],
    "Bills & Utilities": [
        "TELKOMSEL", "MY TELKOMSEL", "FINNET", "XL", "INDOSAT", "SMARTFREN",
        "BIZNET", "INDIHOME", "FIRST MEDIA", "PLN", "LISTRIK", "PDAM", "BPJS",
        "NETFLIX", "SPOTIFY", "APPLE.COM/BILL", "GOOGLE*", "YOUTUBE", "OPENAI",
        "SOFTBANK", "DOCOMO", "AU KDDI", "TOKYO ELECTRIC", "TOKYO GAS",
    ],
    "Cash Withdrawal": [
        "OVERSEAS ATM", "SEVEN BANK", "JAPAN POST BANK", "ATM", "TARIK TUNAI",
    ],
    "Admin Fee": [
        "BIAYA ADM", "ADMIN FEE", "MONTHLY FEE", "KARTU PASPOR",
    ],
}


def guess_category(merchant: str, tx_type: str, remarks: str = "") -> str:
    """Categorizes a transaction into myBCA Financial Diary categories."""
    combined = f"{merchant} {tx_type} {remarks}".upper()

    if "OVERSEAS ATM" in tx_type.upper() or "ATM" in tx_type.upper():
        return "Cash Withdrawal"

    for category, keywords in CATEGORY_KEYWORDS.items():
        for kw in keywords:
            if kw in combined:
                return category

    if "TRANSFER TO BCA ACCOUNT" in tx_type.upper() or "TRANSFER" in tx_type.upper():
        if "VIRTUAL ACCOUNT" not in tx_type.upper():
            return "Account Transfer"

    return "Miscellaneous"


def html_to_lines(raw_body: str) -> list[str]:
    """Strips HTML tags, unescapes entities, and returns non-empty normalized lines."""
    clean = re.sub(r"<[^>]+>", "\n", raw_body)
    clean = html.unescape(clean)
    lines = []
    for line in clean.splitlines():
        norm = re.sub(r"\s+", " ", line).strip()
        if norm:
            lines.append(norm)
    return lines


def extract_kv_pairs(lines: list[str]) -> Dict[str, str]:
    """
    Extracts key-value pairs from BCA/PasporBCA table text where lines appear either as:
    - Key / : / Value (3 lines)
    - Key / : Value (2 lines)
    - Key : Value (1 line)
    """
    kv: Dict[str, str] = {}
    i = 0
    n = len(lines)
    while i < n:
        line = lines[i]
        # Case 1: "Key : Value" on same line (not starting with ':')
        if ":" in line and not line.startswith(":") and not re.match(r"^\d{2}:\d{2}", line):
            k, _, v = line.partition(":")
            k_clean = k.strip()
            v_clean = v.strip()
            if k_clean and len(k_clean) < 40 and not k_clean.lower().startswith(("here are", "http", "note", "jika")):
                if v_clean:
                    kv[k_clean] = v_clean
                    i += 1
                    continue
        # Case 2: Next line is ":" or ": Value"
        if i + 1 < n and lines[i + 1].startswith(":"):
            key = line.strip()
            rem = lines[i + 1][1:].strip()
            if rem:
                kv[key] = rem
                i += 2
                continue
            elif i + 2 < n and not lines[i + 2].startswith(":"):
                # Check if lines[i+2] is actually another key (happens when value is blank like Description : [empty] Reference No. : ...)
                if i + 3 < n and lines[i + 3].startswith(":"):
                    kv[key] = ""
                    i += 2
                    continue
                kv[key] = lines[i + 2].strip()
                i += 3
                continue
        i += 1
    return kv


def parse_amount_and_currency(raw_val: str) -> tuple[str, float]:
    """
    Parses strings like:
    - 'IDR 45,000.00' -> ('IDR', 45000.0)
    - 'JPY 1.962'     -> ('JPY', 1962.0)  [PasporBCA uses dot as thousand separator!]
    - 'JPY 20.000'    -> ('JPY', 20000.0)
    """
    if not raw_val:
        return ("IDR", 0.0)
    s = raw_val.strip()
    m = re.match(r"^([A-Z]{3})\s*(.+)$", s)
    if m:
        curr = m.group(1).upper()
        num_str = m.group(2).strip()
    else:
        curr = "IDR"
        num_str = re.sub(r"[^0-9.,]", "", s)

    # Handle Indonesian dot thousands format in PasporBCA (e.g. '1.962' or '20.000' without comma)
    if "," in num_str and "." in num_str:
        # e.g. '45,000.00' -> remove commas
        num_str = num_str.replace(",", "")
    elif "." in num_str and "," not in num_str:
        # Check if dot is thousand separator (e.g. '1.450', '20.000')
        parts = num_str.split(".")
        if len(parts[-1]) == 3:
            num_str = num_str.replace(".", "")
    elif "," in num_str and "." not in num_str:
        # e.g. '45.000,00' or '45,000'
        parts = num_str.split(",")
        if len(parts[-1]) == 3:
            num_str = num_str.replace(",", "")
        else:
            num_str = num_str.replace(",", ".")

    try:
        amount = float(num_str)
    except ValueError:
        amount = 0.0
    return curr, amount


def normalize_tx_date(raw_date: str, fallback_iso: Optional[str] = None) -> str:
    """
    Normalizes BCA/PasporBCA date strings into 'YYYY-MM-DD HH:MM:SS':
    - '15 Nov 2026 16:04:50' -> '2026-11-15 16:04:50'
    - '22-10-2026'           -> '2026-10-22 00:00:00'
    - '20-10-2026 11:30'     -> '2026-10-20 11:30:00'
    """
    s = (raw_date or "").strip()
    for fmt in ("%d %b %Y %H:%M:%S", "%d %B %Y %H:%M:%S", "%d-%m-%Y %H:%M", "%d-%m-%Y"):
        try:
            dt = datetime.strptime(s, fmt)
            return dt.strftime("%Y-%m-%d %H:%M:%S")
        except ValueError:
            pass
    if fallback_iso:
        return fallback_iso
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def parse_bca_email(
    raw_html_or_text: str,
    subject: str = "",
    sender: str = "",
    email_message_id: Optional[str] = None,
    fallback_date_iso: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """
    Parses any transaction email from `bca@bca.co.id` or `PasporBCA@klikbca.com`
    into a normalized dictionary ready for `upsert_transaction()`.
    Returns None if the email is promotional/non-transactional.
    """
    lines = html_to_lines(raw_html_or_text)
    kv = extract_kv_pairs(lines)
    if not kv:
        return None

    sender_low = sender.lower()
    subject_low = subject.lower()

    # 1. PasporBCA Overseas ATM Withdrawal ('Debit BCA Overseas ATM Transaction Information')
    if "waktu transaksi" in {k.lower() for k in kv} or "nilai transaksi" in {k.lower() for k in kv}:
        raw_date = kv.get("Waktu Transaksi", "")
        tx_date = normalize_tx_date(raw_date, fallback_date_iso)
        atm_id = kv.get("ID ATM", "Overseas ATM")
        curr, amt = parse_amount_and_currency(kv.get("Nilai Transaksi", ""))
        if amt <= 0:
            return None
        fx = DEFAULT_FX_RATES_TO_IDR.get(curr, 1.0)
        country = "JAPAN" if ("JP" in atm_id.upper() or curr == "JPY") else "OVERSEAS"
        return {
            "email_message_id": email_message_id,
            "reference_no": None,
            "tx_date": tx_date,
            "direction": "OUT",
            "source_channel": "PasporBCA ATM",
            "tx_type": "Overseas ATM Withdrawal",
            "merchant_or_party": atm_id,
            "beneficiary_account": None,
            "location": atm_id,
            "country": country,
            "category": "Cash Withdrawal",
            "currency": curr,
            "amount_original": amt,
            "fx_rate_to_idr": fx,
            "amount_idr": round(amt * fx, 2),
            "source_of_fund": kv.get("No. Kartu"),
            "remarks": "Overseas ATM Withdrawal",
            "raw_excerpt": " | ".join(f"{k}: {v}" for k, v in list(kv.items())[:8]),
        }

    # 2. PasporBCA Debit Card Notification ('Notifikasi Transaksi')
    if "pasporbca" in sender_low or ("tanggal" in {k.lower() for k in kv} and "nominal" in {k.lower() for k in kv}):
        raw_date = kv.get("Tanggal", "")
        # Use fallback email timestamp time if Tanggal only has DD-MM-YYYY
        tx_date = normalize_tx_date(raw_date, fallback_date_iso)
        if fallback_date_iso and tx_date.endswith("00:00:00") and fallback_date_iso[:10] == tx_date[:10]:
            tx_date = fallback_date_iso
        merchant = kv.get("Merchant", "Unknown Merchant")
        country = kv.get("Negara", "OVERSEAS").upper()
        curr, amt = parse_amount_and_currency(kv.get("Nominal", ""))
        if amt <= 0:
            return None
        fx = DEFAULT_FX_RATES_TO_IDR.get(curr, 1.0)
        category = guess_category(merchant, "Overseas Debit")
        return {
            "email_message_id": email_message_id,
            "reference_no": None,
            "tx_date": tx_date,
            "direction": "OUT",
            "source_channel": "PasporBCA Email",
            "tx_type": "Overseas Debit Card",
            "merchant_or_party": merchant,
            "beneficiary_account": None,
            "location": country,
            "country": country,
            "category": category,
            "currency": curr,
            "amount_original": amt,
            "fx_rate_to_idr": fx,
            "amount_idr": round(amt * fx, 2),
            "source_of_fund": kv.get("Nomor Kartu"),
            "remarks": f"PasporBCA Debit in {country}",
            "raw_excerpt": " | ".join(f"{k}: {v}" for k, v in list(kv.items())[:8]),
        }

    # 3. myBCA 'Internet Transaction Journal' (bca@bca.co.id)
    status = kv.get("Status", "Successful")
    if status.lower() not in ("successful", "berhasil", ""):
        return None

    tx_date = normalize_tx_date(kv.get("Transaction Date", ""), fallback_date_iso)
    tx_type = kv.get("Transaction Type") or kv.get("Transfer Type") or "myBCA Transaction"

    # Determine merchant / counterparty
    merchant = (
        kv.get("Payment to")
        or kv.get("Company/Product Name")
        or kv.get("Beneficiary Name")
        or kv.get("Name")
        or ("Flazz Card " + kv.get("Flazz Card Number", "") if "Flazz" in tx_type else None)
        or tx_type
    )

    # Determine amount
    raw_amount = (
        kv.get("Total Payment")
        or kv.get("Transfer Amount")
        or kv.get("Top Up Amount")
        or kv.get("Pay Amount")
        or kv.get("Bill Total")
        or kv.get("Nominal")
        or ""
    )
    curr, amt = parse_amount_and_currency(raw_amount)
    if amt <= 0:
        return None

    fx = DEFAULT_FX_RATES_TO_IDR.get(curr, 1.0)
    location = kv.get("Merchant Location", "INDONESIA")
    country = "INDONESIA"
    if location and "," in location:
        last_part = location.split(",")[-1].strip().upper()
        if last_part in ("JP", "JAPAN"):
            country = "JAPAN"

    remarks = kv.get("Remarks") or kv.get("Description") or ""
    if remarks == "-":
        remarks = ""
    if kv.get("Admin Fee"):
        remarks = (remarks + f" (Includes Admin Fee: {kv['Admin Fee']})").strip()

    ref_no = kv.get("Reference No.", "").strip()
    if ref_no.lower().startswith("please save") or len(ref_no) > 60:
        ref_no = ""

    category = guess_category(merchant, tx_type, remarks)

    return {
        "email_message_id": email_message_id,
        "reference_no": ref_no or None,
        "tx_date": tx_date,
        "direction": "OUT",
        "source_channel": "myBCA Email",
        "tx_type": tx_type,
        "merchant_or_party": merchant,
        "beneficiary_account": kv.get("Beneficiary Account") or kv.get("BCA Virtual Account No.") or kv.get("Flazz Card Number"),
        "location": location,
        "country": country,
        "category": category,
        "currency": curr,
        "amount_original": amt,
        "fx_rate_to_idr": fx,
        "amount_idr": round(amt * fx, 2),
        "source_of_fund": kv.get("Source of Fund"),
        "remarks": remarks,
        "raw_excerpt": " | ".join(f"{k}: {v}" for k, v in list(kv.items())[:10]),
    }
