import hashlib
import re
import sqlite3
from contextlib import contextmanager
from typing import Any, Dict, List, Optional

from config import DATABASE_URL

IS_POSTGRES = DATABASE_URL.startswith("postgres://") or DATABASE_URL.startswith("postgresql://")

if IS_POSTGRES:
    import psycopg
    from psycopg.rows import dict_row


@contextmanager
def get_connection():
    """Yields a DB connection compatible with either SQLite (local) or Supabase PostgreSQL (cloud)."""
    if IS_POSTGRES:
        # prepare_threshold=None prevents prepared-statement errors with Supabase PgBouncer (port 6543)
        conn = psycopg.connect(DATABASE_URL, prepare_threshold=None)
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()
    else:
        db_path = DATABASE_URL.replace("sqlite:///", "")
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()


def init_db() -> None:
    """Initializes the transactions and merchant_rules tables in SQLite or Supabase Postgres."""
    id_col = "SERIAL PRIMARY KEY" if IS_POSTGRES else "INTEGER PRIMARY KEY AUTOINCREMENT"
    ts_default = " CURRENT_TIMESTAMP"

    create_tx_table = f"""
    CREATE TABLE IF NOT EXISTS transactions (
        id {id_col},
        dedup_key TEXT UNIQUE NOT NULL,
        email_message_id TEXT,
        reference_no TEXT,
        tx_date TEXT NOT NULL,           -- ISO 8601 format: YYYY-MM-DD HH:MM:SS or YYYY-MM-DD
        tx_month TEXT NOT NULL,          -- YYYY-MM for fast monthly grouping
        direction TEXT NOT NULL,         -- 'OUT' (Spending) or 'IN' (Earning/Income)
        source_channel TEXT NOT NULL,    -- 'myBCA Email', 'PasporBCA Email', 'PasporBCA ATM', 'e-Statement PDF', 'Manual Telegram'
        tx_type TEXT NOT NULL,           -- 'QRIS Payment', 'Transfer to BCA Account', 'Flazz Top Up', 'Overseas Debit', 'Overseas ATM', etc.
        merchant_or_party TEXT NOT NULL, -- Merchant name, Beneficiary name, or Sender name
        beneficiary_account TEXT,
        location TEXT,
        country TEXT DEFAULT 'INDONESIA',
        category TEXT NOT NULL,          -- Matches myBCA Financial Diary categories
        currency TEXT NOT NULL DEFAULT 'IDR',
        amount_original REAL NOT NULL,
        fx_rate_to_idr REAL NOT NULL DEFAULT 1.0,
        amount_idr REAL NOT NULL,
        source_of_fund TEXT,
        remarks TEXT,
        raw_excerpt TEXT,
        created_at TIMESTAMP DEFAULT {ts_default}
    );
    """

    create_rules_table = f"""
    CREATE TABLE IF NOT EXISTS merchant_rules (
        merchant_pattern TEXT PRIMARY KEY,
        category TEXT NOT NULL,
        notes TEXT
    );
    """

    create_indexes = [
        "CREATE INDEX IF NOT EXISTS idx_tx_date ON transactions(tx_date);",
        "CREATE INDEX IF NOT EXISTS idx_tx_month ON transactions(tx_month);",
        "CREATE INDEX IF NOT EXISTS idx_tx_category ON transactions(category);",
        "CREATE INDEX IF NOT EXISTS idx_tx_direction ON transactions(direction);",
        "CREATE INDEX IF NOT EXISTS idx_tx_country ON transactions(country);",
    ]

    with get_connection() as conn:
        cur = conn.cursor()
        cur.execute(create_tx_table)
        cur.execute(create_rules_table)
        for idx_sql in create_indexes:
            cur.execute(idx_sql)


def compute_dedup_key(
    reference_no: Optional[str],
    email_message_id: Optional[str],
    tx_date: str,
    merchant_or_party: str,
    amount_original: float,
    currency: str,
) -> str:
    """
    Computes a stable deduplication key so re-syncing emails or uploading a monthly
    e-Statement PDF never duplicates existing transactions.
    """
    if reference_no and reference_no not in ("-", ""):
        return f"ref:{reference_no.strip()}"
    if email_message_id:
        return f"msg:{email_message_id.strip()}"
    date_part = tx_date[:10]
    norm_merchant = re.sub(r"[^A-Z0-9]", "", merchant_or_party.upper())[:20]
    raw = f"{date_part}|{norm_merchant}|{currency}|{amount_original:.2f}"
    return "hash:" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]


def upsert_transaction(tx: Dict[str, Any]) -> bool:
    """
    Inserts a transaction if not already present.
    Returns True if a new row was inserted, False if it was skipped/updated as duplicate.
    """
    dedup_key = tx.get("dedup_key") or compute_dedup_key(
        tx.get("reference_no"),
        tx.get("email_message_id"),
        tx["tx_date"],
        tx["merchant_or_party"],
        float(tx["amount_original"]),
        tx.get("currency", "IDR"),
    )
    tx_month = tx["tx_date"][:7]

    # Check custom user merchant categorization rules first
    custom_cat = get_custom_category_for_merchant(tx["merchant_or_party"])
    category = custom_cat or tx.get("category", "Miscellaneous")

    params = (
        dedup_key,
        tx.get("email_message_id"),
        tx.get("reference_no"),
        tx["tx_date"],
        tx_month,
        tx.get("direction", "OUT"),
        tx.get("source_channel", "myBCA Email"),
        tx.get("tx_type", "Payment"),
        tx["merchant_or_party"],
        tx.get("beneficiary_account"),
        tx.get("location"),
        tx.get("country", "INDONESIA"),
        category,
        tx.get("currency", "IDR"),
        float(tx["amount_original"]),
        float(tx.get("fx_rate_to_idr", 1.0)),
        float(tx["amount_idr"]),
        tx.get("source_of_fund"),
        tx.get("remarks"),
        tx.get("raw_excerpt"),
    )

    with get_connection() as conn:
        cur = conn.cursor()
        if IS_POSTGRES:
            sql = """
            INSERT INTO transactions (
                dedup_key, email_message_id, reference_no, tx_date, tx_month,
                direction, source_channel, tx_type, merchant_or_party, beneficiary_account,
                location, country, category, currency, amount_original,
                fx_rate_to_idr, amount_idr, source_of_fund, remarks, raw_excerpt
            ) VALUES (
                %s, %s, %s, %s, %s,
                %s, %s, %s, %s, %s,
                %s, %s, %s, %s, %s,
                %s, %s, %s, %s, %s
            )
            ON CONFLICT (dedup_key) DO NOTHING
            RETURNING id;
            """
            cur.execute(sql, params)
            return cur.fetchone() is not None
        else:
            sql = """
            INSERT OR IGNORE INTO transactions (
                dedup_key, email_message_id, reference_no, tx_date, tx_month,
                direction, source_channel, tx_type, merchant_or_party, beneficiary_account,
                location, country, category, currency, amount_original,
                fx_rate_to_idr, amount_idr, source_of_fund, remarks, raw_excerpt
            ) VALUES (
                ?, ?, ?, ?, ?,
                ?, ?, ?, ?, ?,
                ?, ?, ?, ?, ?,
                ?, ?, ?, ?, ?
            );
            """
            cur.execute(sql, params)
            return cur.rowcount > 0


def get_custom_category_for_merchant(merchant: str) -> Optional[str]:
    """Checks if the user has defined a custom category rule for a merchant substring."""
    with get_connection() as conn:
        cur = conn.cursor()
        cur.execute("SELECT merchant_pattern, category FROM merchant_rules;")
        rows = cur.fetchall()
        m_upper = merchant.upper()
        for row in rows:
            pattern = row[0] if not isinstance(row, dict) else row["merchant_pattern"]
            cat = row[1] if not isinstance(row, dict) else row["category"]
            if pattern.upper() in m_upper:
                return cat
    return None


def set_merchant_category_rule(merchant_pattern: str, category: str, notes: str = "") -> int:
    """Saves a permanent categorization rule and updates all existing matching transactions."""
    pattern_clean = merchant_pattern.strip().upper()
    with get_connection() as conn:
        cur = conn.cursor()
        if IS_POSTGRES:
            cur.execute(
                """
                INSERT INTO merchant_rules (merchant_pattern, category, notes)
                VALUES (%s, %s, %s)
                ON CONFLICT (merchant_pattern) DO UPDATE SET category = EXCLUDED.category, notes = EXCLUDED.notes;
                """,
                (pattern_clean, category, notes),
            )
            cur.execute(
                "UPDATE transactions SET category = %s WHERE UPPER(merchant_or_party) LIKE %s;",
                (category, f"%{pattern_clean}%"),
            )
        else:
            cur.execute(
                """
                INSERT OR REPLACE INTO merchant_rules (merchant_pattern, category, notes)
                VALUES (?, ?, ?);
                """,
                (pattern_clean, category, notes),
            )
            cur.execute(
                "UPDATE transactions SET category = ? WHERE UPPER(merchant_or_party) LIKE ?;",
                (category, f"%{pattern_clean}%"),
            )
        return cur.rowcount


def run_read_only_sql(sql_query: str) -> List[Dict[str, Any]]:
    """
    Executes a read-only SELECT query generated by the AI agent and returns a list of dicts.
    Blocks any mutating statements for safety.
    """
    cleaned = sql_query.strip().rstrip(";")
    first_word = cleaned.split()[0].upper() if cleaned.split() else ""
    if first_word not in ("SELECT", "WITH", "PRAGMA", "EXPLAIN"):
        raise ValueError("Only SELECT or WITH read-only queries are allowed.")

    forbidden = re.compile(r"\b(DROP|DELETE|UPDATE|INSERT|ALTER|TRUNCATE|CREATE|REPLACE)\b", re.IGNORECASE)
    if forbidden.search(cleaned):
        raise ValueError("Query contains forbidden write keywords.")

    with get_connection() as conn:
        if IS_POSTGRES:
            with conn.cursor(row_factory=dict_row) as cur:
                cur.execute(cleaned)
                return list(cur.fetchall())
        else:
            cur = conn.cursor()
            cur.execute(cleaned)
            cols = [col[0] for col in cur.description] if cur.description else []
            return [dict(zip(cols, row)) for row in cur.fetchall()]
