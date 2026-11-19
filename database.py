import hashlib
import re
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Set, Tuple
from urllib.parse import quote, unquote

from config import DATABASE_URL

IS_POSTGRES = DATABASE_URL.startswith("postgres://") or DATABASE_URL.startswith("postgresql://")

if IS_POSTGRES:
    import psycopg
    from psycopg.rows import dict_row

_RESOLVED_POSTGRES_URL: Optional[str] = None
_INITIALIZED_DB_URL: Optional[str] = None
_DB_MUTATION_VERSION: int = 0
_POSTGRES_URI_RE = re.compile(
    r"^(postgres(?:ql)?)://(.+)@((?:[A-Za-z0-9.-]+|\[[0-9A-Fa-f:]+\])(?::\d+)?)([/?#].*)?$"
)


def get_db_mutation_version() -> int:
    return _DB_MUTATION_VERSION


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

    create_state_table = f"""
    CREATE TABLE IF NOT EXISTS bot_state (
        key TEXT PRIMARY KEY,
        value TEXT NOT NULL,
        updated_at TIMESTAMP DEFAULT {ts_default}
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
        cur.execute(create_state_table)
        for idx_sql in create_indexes:
            cur.execute(idx_sql)

    _INITIALIZED_DB_URL = DATABASE_URL


def get_bot_state(key: str) -> Optional[str]:
    """Reads a persisted key-value state entry (e.g. last monthly reminder sent)."""
    init_db()
    with get_connection() as conn:
        cur = conn.cursor()
        ph = "%s" if IS_POSTGRES else "?"
        cur.execute(f"SELECT value FROM bot_state WHERE key = {ph};", (key,))
        row = cur.fetchone()
        if not row:
            return None
        val = _extract_col(row, 0, "value")
        return str(val) if val is not None else None


def set_bot_state(key: str, value: str) -> None:
    """Upserts a persisted key-value state entry across restarts/deploys."""
    init_db()
    with get_connection() as conn:
        cur = conn.cursor()
        if IS_POSTGRES:
            cur.execute(
                """
                INSERT INTO bot_state (key, value, updated_at)
                VALUES (%s, %s, NOW())
                ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = NOW();
                """,
                (key, value),
            )
        else:
            cur.execute(
                """
                INSERT INTO bot_state (key, value, updated_at)
                VALUES (?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = CURRENT_TIMESTAMP;
                """,
                (key, value),
            )


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

    if inserted > 0:
        global _DB_MUTATION_VERSION
        _DB_MUTATION_VERSION += 1

    return {
        "inserted": inserted,
        "skipped_duplicates": len(params_list) - inserted,
    }


_GENERIC_BANK_TOKENS = {
    "TRSF", "EBANKING", "BIFAST", "SWITCHING", "QRIS", "KARTU", "DEBIT",
    "PAYMENT", "TRANSFER", "INDONESIA", "JAPAN", "OVERSEAS", "STATEMENT",
    "ENTRY", "DARI", "KEPADA", "BANK", "BCA", "MYBCA",
}


def _merchant_tokens(text: str) -> Set[str]:
    words = re.findall(r"[A-Z0-9]{4,}", (text or "").upper())
    return {w for w in words if w not in _GENERIC_BANK_TOKENS}


def _parse_day(tx_date: str) -> Optional[datetime]:
    try:
        return datetime.strptime((tx_date or "")[:10], "%Y-%m-%d")
    except ValueError:
        return None


def _fetch_existing_candidates_in_window(
    cur: Any,
    start_ts: str,
    end_ts: str,
) -> List[Dict[str, Any]]:
    sql = """
    SELECT id, dedup_key, tx_date, direction, source_channel,
           merchant_or_party, currency, amount_original, amount_idr, remarks
    FROM transactions
    WHERE tx_date >= {ph} AND tx_date <= {ph}
    ORDER BY tx_date ASC, id ASC;
    """.format(ph="%s" if IS_POSTGRES else "?")
    cur.execute(sql, (start_ts, end_ts))
    pool: List[Dict[str, Any]] = []
    for row in cur.fetchall():
        tx_date = str(_extract_col(row, 2, "tx_date") or "")
        merchant = str(_extract_col(row, 5, "merchant_or_party") or "")
        remarks = str(_extract_col(row, 9, "remarks") or "")
        pool.append({
            "id": _extract_col(row, 0, "id"),
            "dedup_key": str(_extract_col(row, 1, "dedup_key") or ""),
            "tx_date": tx_date,
            "tx_day": tx_date[:10],
            "day_dt": _parse_day(tx_date),
            "direction": str(_extract_col(row, 3, "direction") or "OUT").upper(),
            "source_channel": str(_extract_col(row, 4, "source_channel") or ""),
            "merchant_or_party": merchant,
            "currency": str(_extract_col(row, 6, "currency") or "IDR").upper(),
            "amount_original": float(_extract_col(row, 7, "amount_original") or 0.0),
            "amount_idr": float(_extract_col(row, 8, "amount_idr") or 0.0),
            "tokens": _merchant_tokens(f"{merchant} {remarks}"),
            "consumed": False,
        })
    return pool


def _find_matching_existing_tx(
    tx: Dict[str, Any],
    pool: List[Dict[str, Any]],
) -> Optional[Dict[str, Any]]:
    tx_day = str(tx["tx_date"])[:10]
    tx_dt = _parse_day(tx_day)
    tx_dir = str(tx.get("direction", "OUT")).upper()
    tx_curr = str(tx.get("currency", "IDR")).upper()
    tx_amt_orig = float(tx.get("amount_original") or 0.0)
    tx_amt_idr = float(tx.get("amount_idr") or 0.0)
    tx_tokens = _merchant_tokens(f"{tx.get('merchant_or_party', '')} {tx.get('remarks', '')}")

    candidates = [r for r in pool if not r["consumed"] and r["direction"] == tx_dir]

    # Pass 1: Same day + exact IDR amount (within Rp 1) + overlapping merchant token
    for r in candidates:
        if r["tx_day"] == tx_day and abs(r["amount_idr"] - tx_amt_idr) <= 1.0:
            if tx_tokens and (tx_tokens & r["tokens"]):
                return r

    # Pass 2: Same day + exact IDR amount (within Rp 1)
    for r in candidates:
        if r["tx_day"] == tx_day and abs(r["amount_idr"] - tx_amt_idr) <= 1.0:
            return r

    # Pass 3: Foreign-currency / card settlement match within ±3 days
    if tx_dt is not None:
        for r in candidates:
            if r["day_dt"] is None or abs((tx_dt - r["day_dt"]).days) > 3:
                continue
            if tx_curr != "IDR" and r["currency"] == tx_curr and abs(r["amount_original"] - tx_amt_orig) <= 0.01:
                return r
            if r["currency"] != "IDR" and tx_amt_idr > 0:
                rel_diff = abs(r["amount_idr"] - tx_amt_idr) / max(r["amount_idr"], 1.0)
                if rel_diff <= 0.06 and (r["tx_day"] == tx_day or bool(tx_tokens & r["tokens"])):
                    return r

    return None


def reconcile_and_upsert_statement_transactions(txs: List[Dict[str, Any]]) -> Dict[str, Any]:
    """
    Reconciles transactions extracted from a myBCA e-Statement PDF or screenshot against
    existing DB records (1-to-1 by date, direction, and amount/currency) so already-synced
    email transactions are never duplicated, exact BCA IDR settlement amounts update foreign
    card rows when applicable, and all new IN/OUT rows are inserted cleanly.
    """
    if not txs:
        return {"inserted": 0, "skipped_duplicates": 0, "fx_updated": 0, "inserted_items": []}

    init_db()
    valid_days = [d for d in (_parse_day(str(t.get("tx_date", ""))) for t in txs) if d is not None]
    if valid_days:
        start_ts = (min(valid_days) - timedelta(days=3)).strftime("%Y-%m-%d") + " 00:00:00"
        end_ts = (max(valid_days) + timedelta(days=3)).strftime("%Y-%m-%d") + " 23:59:59"
    else:
        start_ts = "1970-01-01 00:00:00"
        end_ts = "9999-12-31 23:59:59"

    with get_connection() as conn:
        cur = conn.cursor()
        pool = _fetch_existing_candidates_in_window(cur, start_ts, end_ts)
        fx_updates: List[Tuple[float, float, Any]] = []
        to_insert: List[Dict[str, Any]] = []
        seen_batch_keys: Dict[str, int] = {}
        skipped = 0

        for tx in txs:
            matched = _find_matching_existing_tx(tx, pool)
            if matched is not None:
                matched["consumed"] = True
                skipped += 1
                tx_amt_idr = float(tx.get("amount_idr") or 0.0)
                if (
                    matched["currency"] != "IDR"
                    and matched["amount_original"] > 0
                    and tx_amt_idr > 0
                    and abs(matched["amount_idr"] - tx_amt_idr) > 1.0
                    and tx.get("source_channel") == "e-Statement PDF"
                ):
                    new_fx = round(tx_amt_idr / matched["amount_original"], 4)
                    fx_updates.append((tx_amt_idr, new_fx, matched["id"]))
                continue

            tx_day = str(tx["tx_date"])[:10]
            tx_dir = str(tx.get("direction", "OUT")).upper()
            tx_curr = str(tx.get("currency", "IDR")).upper()
            tx_amt_orig = float(tx.get("amount_original") or 0.0)
            norm_merchant = re.sub(r"[^A-Z0-9]", "", str(tx.get("merchant_or_party", "")).upper())[:20]
            raw_sig = f"{tx_day}|{tx_dir}|{norm_merchant}|{tx_curr}|{tx_amt_orig:.2f}"
            base_key = "hash:" + hashlib.sha256(raw_sig.encode("utf-8")).hexdigest()[:24]
            occ = seen_batch_keys.get(base_key, 0) + 1
            seen_batch_keys[base_key] = occ
            tx_copy = dict(tx)
            tx_copy["dedup_key"] = base_key if occ == 1 else f"{base_key}#{occ}"
            to_insert.append(tx_copy)

        if fx_updates:
            upd_sql = (
                "UPDATE transactions SET amount_idr = %s, fx_rate_to_idr = %s WHERE id = %s;"
                if IS_POSTGRES
                else "UPDATE transactions SET amount_idr = ?, fx_rate_to_idr = ? WHERE id = ?;"
            )
            for params in fx_updates:
                cur.execute(upd_sql, params)
            global _DB_MUTATION_VERSION
            _DB_MUTATION_VERSION += 1

    inserted_items: List[Dict[str, Any]] = []
    inserted_count = 0
    if to_insert:
        batch_res = upsert_transactions_batch(to_insert)
        inserted_count = batch_res["inserted"]
        skipped += batch_res["skipped_duplicates"]
        if inserted_count > 0:
            inserted_items = to_insert[:inserted_count]

    return {
        "inserted": inserted_count,
        "skipped_duplicates": skipped,
        "fx_updated": len(fx_updates),
        "inserted_items": inserted_items,
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
        global _DB_MUTATION_VERSION
        _DB_MUTATION_VERSION += 1
        return cur.rowcount


def _query_dicts_on_cursor(cur: Any, sql: str) -> List[Dict[str, Any]]:
    cur.execute(sql)
    rows = cur.fetchall()
    if not rows:
        return []
    if isinstance(rows[0], dict):
        return list(rows)
    cols = [col[0] for col in cur.description] if cur.description else []
    return [dict(zip(cols, row)) for row in rows]


def fetch_financial_snapshot() -> Dict[str, Any]:
    """
    Fetches the AI advisor's summary snapshot in a single database connection
    (avoiding 5 sequential TCP/TLS handshakes to Supabase).
    """
    with get_connection() as conn:
        if IS_POSTGRES:
            with conn.cursor(row_factory=dict_row) as cur:
                return _build_snapshot_from_cursor(cur)
        cur = conn.cursor()
        return _build_snapshot_from_cursor(cur)


def _build_snapshot_from_cursor(cur: Any) -> Dict[str, Any]:
    cnt_rows = _query_dicts_on_cursor(cur, "SELECT COUNT(*) AS cnt FROM transactions;")
    total_rows = cnt_rows[0]["cnt"] if cnt_rows else 0
    monthly_totals = _query_dicts_on_cursor(
        cur,
        """
        SELECT tx_month, direction, country, currency,
               COUNT(*) AS tx_count,
               SUM(amount_original) AS total_orig,
               ROUND(SUM(amount_idr)) AS total_idr
        FROM transactions
        GROUP BY tx_month, direction, country, currency
        ORDER BY tx_month DESC, total_idr DESC
        LIMIT 28;
        """,
    )
    category_totals = _query_dicts_on_cursor(
        cur,
        """
        SELECT tx_month, direction, category,
               COUNT(*) AS tx_count,
               ROUND(SUM(amount_idr)) AS total_idr
        FROM transactions
        GROUP BY tx_month, direction, category
        ORDER BY tx_month DESC, total_idr DESC
        LIMIT 40;
        """,
    )
    merchant_breakdown = _query_dicts_on_cursor(
        cur,
        """
        SELECT tx_month, direction, country, category, merchant_or_party, currency,
               COUNT(*) AS tx_count,
               SUM(amount_original) AS total_orig,
               ROUND(SUM(amount_idr)) AS total_idr
        FROM transactions
        GROUP BY tx_month, direction, country, category, merchant_or_party, currency
        ORDER BY tx_month DESC, total_idr DESC
        LIMIT 40;
        """,
    )
    recent_tx = _query_dicts_on_cursor(
        cur,
        """
        SELECT tx_date, direction, country, category, merchant_or_party, currency,
               amount_original, amount_idr, remarks
        FROM transactions
        ORDER BY tx_date DESC
        LIMIT 25;
        """,
    )
    return {
        "total_rows": total_rows,
        "monthly_totals": monthly_totals,
        "category_totals": category_totals,
        "merchant_breakdown": merchant_breakdown,
        "recent_tx": recent_tx,
    }


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
