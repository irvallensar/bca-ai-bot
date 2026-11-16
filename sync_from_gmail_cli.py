import base64
import json
import subprocess
from email.utils import parsedate_to_datetime
from typing import Any, Dict, List

from database import init_db, run_read_only_sql, upsert_transaction
from email_parser import parse_bca_email


def extract_html_or_text_from_gmail_payload(payload: Dict[str, Any]) -> str:
    """Recursively extracts the HTML or plain-text body from a Gmail API message payload."""
    html_bodies: List[str] = []
    text_bodies: List[str] = []

    def walk(part: Dict[str, Any]):
        mime = part.get("mimeType", "")
        data = part.get("body", {}).get("data")
        if data:
            decoded = base64.urlsafe_b64decode(data + "===").decode("utf-8", errors="replace")
            if mime == "text/html":
                html_bodies.append(decoded)
            elif mime == "text/plain" and len(decoded.strip()) > 5:
                text_bodies.append(decoded)
        for sub in part.get("parts", []):
            walk(sub)

    walk(payload)
    if html_bodies:
        return "\n".join(html_bodies)
    return "\n".join(text_bodies)


def run_gmail_cli(args: List[str]) -> str:
    res = subprocess.run(["gmail"] + args, capture_output=True, text=True, check=True)
    return res.stdout


def sync_via_local_gmail_cli(max_per_sender: int = 50) -> Dict[str, int]:
    """
    Uses the authenticated `gmail` CLI on this Mac to pull your real BCA & PasporBCA
    emails and populate the database immediately (zero App Password required locally!).
    """
    init_db()
    stats = {"scanned": 0, "inserted": 0, "skipped": 0}

    queries = [
        "from:bca@bca.co.id",
        "from:PasporBCA@klikbca.com",
        "from:googleplay-noreply@google.com subject:Receipt",
        'from:payments-noreply@google.com "Order number"',
        "from:noreply@google.com subject:Receipt",
    ]

    for q in queries:
        raw_list = run_gmail_cli(["readonly", "search", q, "--max", str(max_per_sender), "--json"])
        items = json.loads(raw_list or "[]")
        for item in items:
            msg_id = item["id"]
            raw_msg = run_gmail_cli(["readonly", "get", msg_id, "--json"])
            msg_obj = json.loads(raw_msg)
            headers = {h["name"].lower(): h["value"] for h in msg_obj.get("payload", {}).get("headers", [])}
            subject = headers.get("subject", "")
            sender = headers.get("from", "")
            date_hdr = headers.get("date", "")
            fallback_iso = None
            if date_hdr:
                try:
                    dt = parsedate_to_datetime(date_hdr)
                    fallback_iso = dt.strftime("%Y-%m-%d %H:%M:%S")
                except Exception:
                    pass

            body = extract_html_or_text_from_gmail_payload(msg_obj.get("payload", {}))
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
                    stats["skipped"] += 1

    return stats


if __name__ == "__main__":
    print("Syncing your real BCA & PasporBCA emails into mybca_transactions.db...")
    result = sync_via_local_gmail_cli(max_per_sender=40)
    print("Sync result:", json.dumps(result, indent=2))

    print("\n--- Summary by Month & Country ---")
    summary = run_read_only_sql(
        """
        SELECT tx_month, country, currency, COUNT(*) as tx_count,
               SUM(amount_original) as total_original,
               ROUND(SUM(amount_idr)) as total_idr
        FROM transactions
        GROUP BY tx_month, country, currency
        ORDER BY tx_month DESC, total_idr DESC;
        """
    )
    for row in summary:
        print(row)

    print("\n--- November 2026 Breakdown by Category ---")
    nov = run_read_only_sql(
        """
        SELECT category, COUNT(*) as tx_count, SUM(amount_idr) as total_idr
        FROM transactions
        WHERE tx_month = '2026-11'
        GROUP BY category
        ORDER BY total_idr DESC;
        """
    )
    for row in nov:
        print(row)
