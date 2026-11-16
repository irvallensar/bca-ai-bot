import html
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional

from config import DEFAULT_FX_RATES_TO_IDR

WIB_TZ = timezone(timedelta(hours=7))

_TZ_OFFSETS_HOURS = {
    "UTC": 0.0,
    "GMT": 0.0,
    "WIB": 7.0,
    "WITA": 8.0,
    "WIT": 9.0,
    "SGT": 8.0,
    "JST": 9.0,
    "PDT": -7.0,
    "PST": -8.0,
    "EDT": -4.0,
    "EST": -5.0,
    "CDT": -5.0,
    "CST": -6.0,
}

_CURRENCY_SYMBOLS = (
    ("US$", "USD"),
    ("S$", "SGD"),
    ("RP.", "IDR"),
    ("RP", "IDR"),
    ("RM", "MYR"),
    ("￥", "JPY"),
    ("¥", "JPY"),
    ("$", "USD"),
    ("€", "EUR"),
)

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
        "TIKTOK SHOP", "AMAZON", "MERCARI", "IKEA", "NITORI", "GRAMEDIA", "DIGITAL BOOK",
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
        "NETFLIX", "SPOTIFY", "APPLE.COM/BILL", "GOOGLE*", "GOOGLE ONE", "GOOGLE AI",
        "GOOGLE DIGITAL", "GOOGLE CLOUD", "GOOGLE COLAB", "COLAB COMPUTE",
        "AUTO-RENEWING SUBSCRIPTION", "YOUTUBE", "OPENAI",
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
            token = kw.strip()
            matched = (
                bool(re.search(rf"\b{re.escape(token)}\b", combined))
                if len(token) <= 3
                else kw in combined
            )
            if matched:
                return category

    if "TRANSFER TO BCA ACCOUNT" in tx_type.upper() or "TRANSFER" in tx_type.upper():
        if "VIRTUAL ACCOUNT" not in tx_type.upper():
            return "Account Transfer"

    return "Miscellaneous"


def html_to_lines(raw_body: str) -> list[str]:
    """Strips style/script blocks and HTML tags, returning normalized non-empty lines."""
    clean = re.sub(r"<(style|script)\b[^>]*>.*?</\1>", "\n", raw_body, flags=re.IGNORECASE | re.DOTALL)
    clean = re.sub(r"<[^>]+>", "\n", clean)
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


def _split_currency_and_number(raw_val: str) -> tuple[str, str]:
    s = re.sub(r"\s*/\s*(month|year|week|day|mo|yr|bulan|tahun)\b.*$", "", raw_val.strip(), flags=re.IGNORECASE).strip()
    iso_match = re.match(r"^([A-Z]{3})(?=\s|\d)(.+)$", s)
    if iso_match:
        return iso_match.group(1).upper(), re.sub(r"[^0-9.,]", "", iso_match.group(2))

    s_upper = s.upper()
    for sym, code in _CURRENCY_SYMBOLS:
        if s_upper.startswith(sym.upper()):
            rest = s[len(sym):]
            return code, re.sub(r"[^0-9.,]", "", rest)

    return "IDR", re.sub(r"[^0-9.,]", "", s)


def parse_amount_and_currency(raw_val: str) -> tuple[str, float]:
    """
    Parses amount strings across BCA, PasporBCA, and Google Play/Payments formats:
    - 'IDR 45,000.00' -> ('IDR', 45000.0)
    - 'Rp 72.221,00'   -> ('IDR', 72221.0)
    - 'JPY 1.962'     -> ('JPY', 1962.0)  [PasporBCA uses dot as thousand separator!]
    - '￥720/month'   -> ('JPY', 720.0)
    """
    if not raw_val:
        return ("IDR", 0.0)
    curr, num_str = _split_currency_and_number(raw_val)
    if not num_str:
        return (curr, 0.0)

    if "," in num_str and "." in num_str:
        if num_str.rfind(",") > num_str.rfind("."):
            num_str = num_str.replace(".", "").replace(",", ".")
        else:
            num_str = num_str.replace(",", "")
    elif "." in num_str:
        parts = num_str.split(".")
        if len(parts[-1]) == 3:
            num_str = num_str.replace(".", "")
    elif "," in num_str:
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


def _normalize_google_tx_date(raw_date: str, fallback_iso: Optional[str] = None) -> str:
    """
    Normalizes Google Play/Payments timestamps (e.g. 'Oct 20, 2026 10:07:43 PM GMT+9'
    or 'March 9, 2026 at 6:53:53 AM PDT') into WIB ('YYYY-MM-DD HH:MM:SS').
    """
    s = re.sub(r"\s+", " ", (raw_date or "")).strip()
    s = re.sub(r"\s+at\s+", " ", s, flags=re.IGNORECASE)
    tz_offset_hours: Optional[float] = None

    offset_match = re.search(r"\b(?:GMT|UTC)\s*([+-])\s*(\d{1,2})(?::?(\d{2}))?\s*$", s, flags=re.IGNORECASE)
    if offset_match:
        sign = 1.0 if offset_match.group(1) == "+" else -1.0
        hours = int(offset_match.group(2))
        mins = int(offset_match.group(3) or 0)
        tz_offset_hours = sign * (hours + mins / 60.0)
        s = s[: offset_match.start()].strip()
    else:
        abbr_match = re.search(r"\b([A-Z]{2,4})\s*$", s)
        if abbr_match and abbr_match.group(1) in _TZ_OFFSETS_HOURS:
            tz_offset_hours = _TZ_OFFSETS_HOURS[abbr_match.group(1)]
            s = s[: abbr_match.start()].strip()

    for fmt in (
        "%b %d, %Y %I:%M:%S %p",
        "%B %d, %Y %I:%M:%S %p",
        "%b %d, %Y %I:%M %p",
        "%B %d, %Y %I:%M %p",
        "%b %d, %Y %H:%M:%S",
        "%B %d, %Y %H:%M:%S",
        "%b %d, %Y",
        "%B %d, %Y",
    ):
        try:
            dt = datetime.strptime(s, fmt)
            if tz_offset_hours is not None:
                dt = dt.replace(tzinfo=timezone(timedelta(hours=tz_offset_hours))).astimezone(WIB_TZ)
            return dt.strftime("%Y-%m-%d %H:%M:%S")
        except ValueError:
            pass

    return normalize_tx_date(raw_date, fallback_iso)


def _extract_labeled_line_value(
    lines: list[str],
    label: str,
    require_digit: bool = False,
    last_match: bool = False,
) -> str:
    pattern = re.compile(rf"^{re.escape(label)}\b\s*:?\s*(.*)$", re.IGNORECASE)
    matches: list[str] = []
    n = len(lines)
    for i, line in enumerate(lines):
        m = pattern.match(line)
        if not m:
            continue
        val = m.group(1).strip()
        if not val and i + 1 < n:
            nxt = lines[i + 1].strip()
            if nxt == ":" and i + 2 < n:
                val = lines[i + 2].strip()
            elif nxt.startswith(":"):
                val = nxt[1:].strip() or (lines[i + 2].strip() if i + 2 < n else "")
            else:
                val = nxt
        if val and (not require_digit or re.search(r"\d", val)):
            matches.append(val)
    if not matches:
        return ""
    return matches[-1] if last_match else matches[0]


def _extract_google_item_name(lines: list[str], source_label: str, service_hint: str = "") -> str:
    item = ""
    for i, line in enumerate(lines):
        if re.match(r"^Item(?:\s+(?:Quantity\s+)?Price)?$", line, flags=re.IGNORECASE):
            for candidate in lines[i + 1 : i + 6]:
                low = candidate.lower()
                if low in ("quantity", "price"):
                    continue
                if re.match(r"^(total|tax|consumption tax|subtotal|payment method)\b", low):
                    break
                if re.match(
                    r"^(?:[\d.,]+|(?:Rp\.?|IDR|JPY|USD|US\$|SGD|S\$|EUR|MYR|RM|[￥¥$€])\s*[\d.,]+(?:/[A-Za-z]+)?)$",
                    low,
                    flags=re.IGNORECASE,
                ):
                    continue
                item = candidate
                break
            if item:
                break

    if item:
        item = re.sub(
            r"\s+(?:Rp\.?|IDR|JPY|USD|US\$|SGD|S\$|EUR|MYR|RM|[￥¥$€])\s*[\d.,]+(?:/[A-Za-z]+)?\s*$",
            "",
            item,
            flags=re.IGNORECASE,
        ).strip()
        item = re.sub(r"\s*\(by [^)]+\)\s*$", "", item, flags=re.IGNORECASE).strip()

    if not item:
        item = service_hint or source_label
    if "google" not in item.lower():
        prefix = f"Google {service_hint}" if service_hint and service_hint.lower() not in item.lower() else source_label
        item = f"{prefix} - {item}"
    return item


def _is_google_receipt_candidate(sender: str, subject: str) -> bool:
    sender_low = sender.lower()
    subject_low = subject.lower()
    return (
        "@google.com" in sender_low
        or "google play" in sender_low
        or "google payments" in sender_low
        or "google play order receipt" in subject_low
    )


def parse_google_receipt_email(
    lines: list[str],
    subject: str = "",
    sender: str = "",
    email_message_id: Optional[str] = None,
    fallback_date_iso: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """
    Parses positive-charge Google Play (`googleplay-noreply@google.com` / `noreply@google.com`)
    and Google Payments (`payments-noreply@google.com`) order receipt emails.
    Returns None for $0 trials, ToS updates, verification codes, refunds, or non-receipts.
    """
    joined_low = " ".join(lines).lower()
    subject_low = subject.lower()
    if any(tok in subject_low for tok in ("refund", "canceled", "cancelled")) or "been refunded" in joined_low:
        return None

    order_no = _extract_labeled_line_value(lines, "Order number")
    if not order_no:
        return None

    raw_total = _extract_labeled_line_value(lines, "Total", require_digit=True, last_match=True)
    curr, amt = parse_amount_and_currency(raw_total)
    if amt <= 0:
        return None

    raw_date = _extract_labeled_line_value(lines, "Order date", require_digit=True)
    service_hint = ""
    if not raw_date:
        date_pat = re.compile(
            r"^(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\s+\d{1,2},\s+\d{4}(?:\s+at)?\s+\d{1,2}:\d{2}",
            re.IGNORECASE,
        )
        for idx, line in enumerate(lines):
            if date_pat.match(line):
                raw_date = line
                if idx > 0 and len(lines[idx - 1]) < 40 and not lines[idx - 1].startswith("–"):
                    service_hint = lines[idx - 1].strip()
                break

    tx_date = _normalize_google_tx_date(raw_date, fallback_date_iso)

    sender_low = sender.lower()
    is_play = "google play" in sender_low or "google play" in subject_low or "google play" in joined_low
    source_channel = "Google Play Email" if is_play else "Google Payments Email"
    source_label = "Google Play" if is_play else "Google"

    merchant = _extract_google_item_name(lines, source_label=source_label, service_hint=service_hint)

    raw_pm = _extract_labeled_line_value(lines, "Payment method")
    payment_method = re.sub(r"\s*(?:[•●]\s*)+", "-", raw_pm).strip() if raw_pm else None

    if "auto-renewing subscription" in joined_low or re.search(r"/\s*(?:month|year|week)\b", raw_total, flags=re.IGNORECASE):
        tx_type = "Auto-renewing Subscription"
    elif "digital book" in joined_low:
        tx_type = "Digital Book Purchase"
    else:
        tx_type = "Google Play Purchase" if is_play else "Google Payments Purchase"

    fx = DEFAULT_FX_RATES_TO_IDR.get(curr, 1.0)
    country = "JAPAN" if curr == "JPY" else "INDONESIA"
    remarks = f"Google Order {order_no}" + (f" ({payment_method})" if payment_method else "")
    category = guess_category(merchant, tx_type, remarks)

    return {
        "email_message_id": email_message_id,
        "reference_no": order_no,
        "tx_date": tx_date,
        "direction": "OUT",
        "source_channel": source_channel,
        "tx_type": tx_type,
        "merchant_or_party": merchant,
        "beneficiary_account": None,
        "location": country,
        "country": country,
        "category": category,
        "currency": curr,
        "amount_original": amt,
        "fx_rate_to_idr": fx,
        "amount_idr": round(amt * fx, 2),
        "source_of_fund": payment_method,
        "remarks": remarks,
        "raw_excerpt": f"Order: {order_no} | Item: {merchant} | Total: {raw_total} | PM: {payment_method or '-'}",
    }


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
    Parses transaction emails from BCA (`bca@bca.co.id`), PasporBCA (`PasporBCA@klikbca.com`),
    and Google Play / Google Payments receipts into a normalized dictionary for `upsert_transaction()`.
    Returns None if the email is promotional/non-transactional.
    """
    lines = html_to_lines(raw_html_or_text)
    if _is_google_receipt_candidate(sender, subject):
        return parse_google_receipt_email(
            lines,
            subject=subject,
            sender=sender,
            email_message_id=email_message_id,
            fallback_date_iso=fallback_date_iso,
        )

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
