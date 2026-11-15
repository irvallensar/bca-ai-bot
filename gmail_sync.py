import email
import imaplib
from email.header import decode_header
from email.utils import parsedate_to_datetime
from typing import Any, Dict

from config import GMAIL_ADDRESS, GMAIL_APP_PASSWORD
from database import init_db, upsert_transaction
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


def sync_from_gmail_imap(max_messages_per_sender: int = 100) -> Dict[str, int]:
    """
    Connects to Gmail IMAP using GMAIL_ADDRESS + GMAIL_APP_PASSWORD,
    fetches emails from `bca@bca.co.id` and `PasporBCA@klikbca.com`,
    parses them, and inserts new transactions into the database.
    """
    if not GMAIL_ADDRESS or not GMAIL_APP_PASSWORD:
        raise ValueError("GMAIL_ADDRESS and GMAIL_APP_PASSWORD must be configured in .env for IMAP sync.")

    init_db()
    stats = {"scanned": 0, "inserted": 0, "skipped_duplicates": 0}

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

        for q in queries:
            status, data = mail.search(None, q)
            if status != "OK" or not data or not data[0]:
                continue
            msg_nums = data[0].split()
            # Take the most recent N messages
            recent_nums = msg_nums[-max_messages_per_sender:]
            for num in reversed(recent_nums):
                res, msg_data = mail.fetch(num, "(RFC822)")
                if res != "OK" or not msg_data or not msg_data[0]:
                    continue
                raw_bytes = msg_data[0][1]
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
                stats["scanned"] += 1
                parsed = parse_bca_email(
                    body,
                    subject=subject,
                    sender=sender,
                    email_message_id=msg_id,
                    fallback_date_iso=fallback_iso,
                )
                if parsed:
                    if upsert_transaction(parsed):
                        stats["inserted"] += 1
                    else:
                        stats["skipped_duplicates"] += 1
    finally:
        try:
            mail.logout()
        except Exception:
            pass

    return stats
