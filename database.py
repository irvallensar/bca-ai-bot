import hashlib
import re
import sqlite3
from contextlib import contextmanager
from typing import Any, Dict, List, Optional, Set, Tuple
from urllib.parse import quote, unquote

from config import DATABASE_URL

IS_POSTGRES = DATABASE_URL.startswith("postgres://") or DATABASE_URL.startswith("postgresql://")

if IS_POSTGRES:
    import psycopg
    from psycopg.rows import dict_row

_RESOLVED_POSTGRES_URL: Optional[str] = None
_INITIALIZED_DB_URL: Optional[str] = None
_POSTGRES_URI_RE = re.compile(
    r"^(postgres(?:ql)?)://(.+)@((?:[A-Za-z0-9.-]+|\[[0-9A-Fa-f:]+\])(?::\d+)?)([/?#].*)?$"
)


def _encode_pg_password(pw: str) -> str:
    """Percent-encodes reserved URI characters in a Postgres password without double-encoding %XX."""
    if not re.search(r"[^A-Za-z0-9._~-]", pw):
        return pw
    if re.search(r"%[0-9A-Fa-f]{2}", pw):
        return quote(unquote(pw), safe="")
    return quote(pw, safe="")


def _postgres_dsn_candidates(raw_url: str) -> List[str]:
    """
    Normalizes a PostgreSQL URI so passwords with unencoded special characters
    (e.g. `@`, `!`, `#`) or accidental template brackets (`[YOUR-PASSWORD]`) connect cleanly.
    """
    url = raw_url.strip()
    m = _POSTGRES_URI_RE.match(url)
    if not m:
        return [url]
    scheme, user_pass, host_port, suffix = m.groups()
    suffix = suffix or ""
    if ":" not in user_pass:
        return [url]
    user, _, raw_pw = user_pass.partition(":")

    candidates: List[str] = []
    if len(raw_pw) >= 2 and raw_pw.startswith("[") and raw_pw.endswith("]"):
        inner_pw = _encode_pg_password(raw_pw[1:-1])
        candidates.append(f"{scheme}://{user}:{inner_pw}@{host_port}{suffix}")
    encoded_pw = _encode_pg_password(raw_pw)
    literal_uri = f"{scheme}://{user}:{encoded_pw}@{host_port}{suffix}"
    if literal_uri not in candidates:
        candidates.append(literal_uri)
    return candidates


@contextmanager
def get_connection():
    """Yields a DB connection compatible with either SQLite (local) or Supabase PostgreSQL (cloud)."""
    global _RESOLVED_POSTGRES_URL
    if IS_POSTGRES:
        # prepare_threshold=None prevents prepared-statement errors with Supabase PgBouncer (port 6543)
        if _RESOLVED_POSTGRES_URL:
            conn = psycopg.connect(_RESOLVED_POSTGRES_URL, prepare_threshold=None, connect_timeout=15)
        else:
            candidates = _postgres_dsn_candidates(DATABASE_URL)
            conn = None
            last_err: Optional[Exception] = None
            for dsn in candidates:
                try:
                    conn = psycopg.connect(dsn, prepare_threshold=None, connect_timeout=15)
                    _RESOLVED_POSTGRES_URL = dsn
                    break
                except Exception as e:
                    last_err = e
            if conn is None:
                raise last_err  # type: ignore[misc]
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()
    else:
        if DATABASE_URL.startswith(("http://", "https://")):
            raise ValueError(
                "DATABASE_URL is an HTTP(S) URL instead of a PostgreSQL connection string "
                "(postgresql://...). Copy the Pooler URI from Supabase Connect."
            )
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
    global _INITIALIZED_DB_URL
    if _INITIALIZED_DB_URL == DATABASE_URL:
        return

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
        "CREATE INDEX IF NOT EXISTS idx_tx_email_msg_id ON transactions(email_message_id);",
    ]

    with get_connection() as conn:
        cur = conn.cursor()
        cur.execute(create_tx_table)
        cur.execute(create_rules_table)
        for idx_sql in create_indexes:
            cur.execute(idx_sql)

    _INITIALIZED_DB_URL = DATABASE_URL


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


def _extract_col(row: Any, idx: int, key: str) -> Any:
    return row[key] if isinstance(row, dict) else row[idx]


def _load_merchant_rules(cur: Any) -> List[Tuple[str, str]]:
    """Loads custom merchant pattern rules once from an open database cursor."""
    cur.execute("SELECT merchant_pattern, category FROM merchant_rules;")
    rules: List[Tuple[str, str]] = []
    for row in cur.fetchall():
        pattern = _extract_col(row, 0, "merchant_pattern")
        cat = _extract_col(row, 1, "category")
        if pattern and cat:
            rules.append((str(pattern).upper(), str(cat)))
    return rules


def _match_merchant_rule(merchant: str, rules: List[Tuple[str, str]]) -> Optional[str]:
    m_upper = merchant.upper()
    for pattern_upper, cat in rules:
        if pattern_upper in m_upper:
            return cat
    return None


def _build_tx_params(tx: Dict[str, Any], rules: List[Tuple[str, str]]) -> Tuple[Any, ...]:
    dedup_key = tx.get("dedup_key") or compute_dedup_key(
        tx.get("reference_no"),
        tx.get("email_message_id"),
        tx["tx_date"],
        tx["merchant_or_party"],
        float(tx["amount_original"]),
        tx.get("currency", "IDR"),
    )
    tx_month = tx["tx_date"][:7]
    custom_cat = _match_merchant_rule(tx["merchant_or_party"], rules)
    category = custom_cat or tx.get("category", "Miscellaneous")

    return (
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


def get_known_email_message_ids(message_ids: Optional[List[str]] = None) -> Set[str]:
    """
    Returns existing `email_message_id` values already stored in `transactions`.
    If `message_ids` is provided, checks only those IDs in a single DB connection.
    """
    cleaned: Optional[List[str]] = None
    if message_ids is not None:
        cleaned = list(dict.fromkeys(m.strip("<> ") for m in message_ids if m and m.strip("<> ")))
        if not cleaned:
            return set()

    known: Set[str] = set()
    with get_connection() as conn:
        cur = conn.cursor()
        if cleaned is None:
            cur.execute("SELECT email_message_id FROM transactions WHERE email_message_id IS NOT NULL;")
            for row in cur.fetchall():
                val = _extract_col(row, 0, "email_message_id")
                if val:
                    known.add(str(val).strip("<> "))
        elif IS_POSTGRES:
            cur.execute(
                "SELECT email_message_id FROM transactions WHERE email_message_id = ANY(%s);",
                (cleaned,),
            )
            for row in cur.fetchall():
                val = _extract_col(row, 0, "email_message_id")
                if val:
                    known.add(str(val).strip("<> "))
        else:
            chunk_size = 500
            for i in range(0, len(cleaned), chunk_size):
                chunk = cleaned[i : i + chunk_size]
                placeholders = ", ".join(["?"] * len(chunk))
                cur.execute(
                    f"SELECT email_message_id FROM transactions WHERE email_message_id IN ({placeholders});",
                    chunk,
                )
                for row in cur.fetchall():
                    val = _extract_col(row, 0, "email_message_id")
                    if val:
                        known.add(str(val).strip("<> "))
    return known


def upsert_transactions_batch(txs: List[Dict[str, Any]]) -> Dict[str, int]:
    """
    Batch-inserts transactions using a single DB connection and single merchant_rules load.
    Returns `{"inserted": <int>, "skipped_duplicates": <int>}`.
    """
    if not txs:
        return {"inserted": 0, "skipped_duplicates": 0}

    with get_connection() as conn:
        cur = conn.cursor()
        rules = _load_merchant_rules(cur)
        params_list = [_build_tx_params(tx, rules) for tx in txs]

        if IS_POSTGRES:
            row_placeholder = "(" + ", ".join(["%s"] * 20) + ")"
            chunk_size = 500
            inserted = 0
            for i in range(0, len(params_list), chunk_size):
                chunk = params_list[i : i + chunk_size]
                values_sql = ",\n".join([row_placeholder] * len(chunk))
                flat_params = [val for row in chunk for val in row]
                sql = f"""
                INSERT INTO transactions (
                    dedup_key, email_message_id, reference_no, tx_date, tx_month,
                    direction, source_channel, tx_type, merchant_or_party, beneficiary_account,
                    location, country, category, currency, amount_original,
                    fx_rate_to_idr, amount_idr, source_of_fund, remarks, raw_excerpt
                ) VALUES
                {values_sql}
                ON CONFLICT (dedup_key) DO NOTHING
                RETURNING id;
                """
                cur.execute(sql, flat_params)
                inserted += len(cur.fetchall())
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
            before = conn.total_changes
            cur.executemany(sql, params_list)
            inserted = conn.total_changes - before

    return {
        "inserted": inserted,
        "skipped_duplicates": len(params_list) - inserted,
    }


def upsert_transaction(tx: Dict[str, Any]) -> bool:
    """
    Inserts a transaction if not already present.
    Returns True if a new row was inserted, False if it was skipped/updated as duplicate.
    """
    return upsert_transactions_batch([tx])["inserted"] > 0


def get_custom_category_for_merchant(merchant: str) -> Optional[str]:
    """Checks if the user has defined a custom category rule for a merchant substring."""
    with get_connection() as conn:
        cur = conn.cursor()
        rules = _load_merchant_rules(cur)
        return _match_merchant_rule(merchant, rules)


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
