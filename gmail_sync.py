import email
import imaplib
from email.header import decode_header
from email.utils import parsedate_to_datetime
from typing import Any, Dict, List, Optional, Set

from config import GMAIL_ADDRESS, GMAIL_APP_PASSWORD
from database import get_known_email_message_ids, init_db, upsert_transactions_batch
from email_parser import parse_bca_email


def decode_mime_header(val: str) -> str:
    if not val:
        return ""
    parts = decode_header(val)
    out = []
    for text, enc in parts:
        if isinstance(text, bytes):
            out.append(text.decode(enc or "utf-8", errors="replace"))
        else:
            out.append(str(text))
    return "".join(out)


def extract_best_body_from_email_message(msg: email.message.Message) -> str:
    """
    Extracts the HTML or plain-text body from a standard Python `email.message.Message`.
    Crucial: PasporBCA emails have a 1-byte '-' in text/plain and put the real content in text/html!
    So we always prefer text/html (or whichever part has >20 characters).
    """
    html_parts = []
    text_parts = []

    if msg.is_multipart():
        for part in msg.walk():
            ctype = part.get_content_type()
            cdisp = str(part.get("Content-Disposition") or "")
            if "attachment" in cdisp.lower():
                continue
            payload = part.get_payload(decode=True)
            if not payload:
                continue
            charset = part.get_content_charset() or "utf-8"
            decoded = payload.decode(charset, errors="replace")
            if ctype == "text/html":
                html_parts.append(decoded)
            elif ctype == "text/plain":
                text_parts.append(decoded)
    else:
        payload = msg.get_payload(decode=True)
        if payload:
            charset = msg.get_content_charset() or "utf-8"
            decoded = payload.decode(charset, errors="replace")
            if msg.get_content_type() == "text/html":
                html_parts.append(decoded)
            else:
                text_parts.append(decoded)

    if html_parts:
        return "\n".join(html_parts)
    return "\n".join(text_parts)


def _parse_imap_fetch_map(fetch_data: Any) -> Dict[bytes, bytes]:
    """Maps IMAP sequence numbers (bytes) to fetched payload bytes."""
    mapped: Dict[bytes, bytes] = {}
    if not fetch_data:
        return mapped
    for item in fetch_data:
        if (
            isinstance(item, tuple)
            and len(item) >= 2
            and isinstance(item[0], bytes)
            and isinstance(item[1], bytes)
        ):
            seq = item[0].split(None, 1)[0]
            if seq.isdigit():
                mapped[seq] = item[1]
    return mapped


def _fetch_message_ids_batch(
    mail: Any,
    seq_nums: List[bytes],
    chunk_size: int = 100,
) -> Dict[bytes, str]:
    """Fetches Message-ID headers in batch so already-synced emails skip full RFC822 download."""
    seq_to_msgid: Dict[bytes, str] = {}
    for i in range(0, len(seq_nums), chunk_size):
        chunk = seq_nums[i : i + chunk_size]
        msg_set = b",".join(chunk)
        try:
            res, hdr_data = mail.fetch(msg_set, "(BODY.PEEK[HEADER.FIELDS (MESSAGE-ID)])")
        except Exception:
            continue
        if res != "OK":
            continue
        for seq, hdr_bytes in _parse_imap_fetch_map(hdr_data).items():
            hdr_msg = email.message_from_bytes(hdr_bytes)
            msg_id = (hdr_msg.get("Message-ID") or "").strip("<> ")
            if msg_id:
                seq_to_msgid[seq] = msg_id
    return seq_to_msgid


def _fetch_raw_emails_batch(
    mail: Any,
    seq_nums: List[bytes],
    chunk_size: int = 20,
) -> Dict[bytes, bytes]:
    """Fetches full RFC822 messages in small batches with single-message fallback."""
    raw_by_seq: Dict[bytes, bytes] = {}
    for i in range(0, len(seq_nums), chunk_size):
        chunk = seq_nums[i : i + chunk_size]
        msg_set = b",".join(chunk)
        try:
            res, msg_data = mail.fetch(msg_set, "(RFC822)")
            if res == "OK":
                raw_by_seq.update(_parse_imap_fetch_map(msg_data))
        except Exception:
            pass

        missing = [num for num in chunk if num not in raw_by_seq]
        for num in missing:
            try:
                res, msg_data = mail.fetch(num, "(RFC822)")
            except Exception:
                continue
            if res == "OK":
                parsed_map = _parse_imap_fetch_map(msg_data)
                if num in parsed_map:
                    raw_by_seq[num] = parsed_map[num]
                elif msg_data and isinstance(msg_data[0], tuple) and isinstance(msg_data[0][1], bytes):
                    raw_by_seq[num] = msg_data[0][1]
    return raw_by_seq


def _parse_raw_email_message(raw_bytes: bytes) -> Optional[Dict[str, Any]]:
    msg = email.message_from_bytes(raw_bytes)
    subject = decode_mime_header(msg.get("Subject", ""))
    sender = decode_mime_header(msg.get("From", ""))
    msg_id = (msg.get("Message-ID") or "").strip("<> ")
    date_hdr = msg.get("Date", "")
    fallback_iso = None
    if date_hdr:
        try:
            dt = parsedate_to_datetime(date_hdr)
            fallback_iso = dt.strftime("%Y-%m-%d %H:%M:%S")
        except Exception:
            pass

    body = extract_best_body_from_email_message(msg)
    return parse_bca_email(
        body,
        subject=subject,
        sender=sender,
        email_message_id=msg_id,
        fallback_date_iso=fallback_iso,
    )


def sync_from_gmail_imap(max_messages_per_sender: Optional[int] = None) -> Dict[str, int]:
    """
    Connects to Gmail IMAP using GMAIL_ADDRESS + GMAIL_APP_PASSWORD,
    fetches emails from `bca@bca.co.id` and `PasporBCA@klikbca.com`
    (all matching emails when `max_messages_per_sender` is None),
    skips emails whose Message-ID is already stored, parses new ones,
    and batch-inserts new transactions into the database.
    """
    if not GMAIL_ADDRESS or not GMAIL_APP_PASSWORD:
        raise ValueError("GMAIL_ADDRESS and GMAIL_APP_PASSWORD must be configured in .env for IMAP sync.")

    init_db()
    stats = {"scanned": 0, "inserted": 0, "skipped_duplicates": 0}
    parsed_txs: List[Dict[str, Any]] = []

    mail = imaplib.IMAP4_SSL("imap.gmail.com", timeout=30)
    try:
        mail.login(GMAIL_ADDRESS, GMAIL_APP_PASSWORD)
        sel_status, _ = mail.select('"[Gmail]/All Mail"', readonly=True)
        if sel_status != "OK":
            mail.select("inbox", readonly=True)

        queries = [
            '(FROM "bca@bca.co.id")',
            '(FROM "PasporBCA@klikbca.com")',
        ]

        candidate_nums: List[bytes] = []
        seen_seq: Set[bytes] = set()
        for q in queries:
            status, data = mail.search(None, q)
            if status != "OK" or not data or not data[0]:
                continue
            msg_nums = data[0].split()
            target_nums = (
                msg_nums
                if not max_messages_per_sender or max_messages_per_sender <= 0
                else msg_nums[-max_messages_per_sender:]
            )
            for num in reversed(target_nums):
                if num not in seen_seq:
                    seen_seq.add(num)
                    candidate_nums.append(num)

        if not candidate_nums:
            return stats

        seq_to_msgid = _fetch_message_ids_batch(mail, candidate_nums)
        known_ids = get_known_email_message_ids(list(seq_to_msgid.values()))

        nums_to_fetch: List[bytes] = []
        for num in candidate_nums:
            msg_id = seq_to_msgid.get(num, "")
            if msg_id and msg_id in known_ids:
                stats["scanned"] += 1
                stats["skipped_duplicates"] += 1
            else:
                nums_to_fetch.append(num)

        if nums_to_fetch:
            raw_by_seq = _fetch_raw_emails_batch(mail, nums_to_fetch)
            for num in nums_to_fetch:
                raw_bytes = raw_by_seq.get(num)
                if not raw_bytes:
                    continue
                stats["scanned"] += 1
                parsed = _parse_raw_email_message(raw_bytes)
                if parsed:
                    parsed_txs.append(parsed)
    finally:
        try:
            mail.logout()
        except Exception:
            pass

    if parsed_txs:
        batch_stats = upsert_transactions_batch(parsed_txs)
        stats["inserted"] += batch_stats["inserted"]
        stats["skipped_duplicates"] += batch_stats["skipped_duplicates"]

    return stats
