import json
from datetime import datetime
from typing import Any, Dict, List

from google import genai
from google.genai import types

from config import (
    DEFAULT_FX_RATES_TO_IDR,
    GEMINI_API_KEY,
    GEMINI_MODEL,
    MYBCA_CATEGORIES,
)
from database import (
    IS_POSTGRES,
    init_db,
    run_read_only_sql,
    set_merchant_category_rule,
    upsert_transaction,
)


def execute_sql_query(sql_query: str) -> str:
    """
    Executes a read-only SQL SELECT query against the user's `transactions` table
    and returns the results as a JSON string.

    Table schema: `transactions`
    - id (INTEGER)
    - tx_date (TEXT, format 'YYYY-MM-DD HH:MM:SS')
    - tx_month (TEXT, format 'YYYY-MM', e.g. '2026-11', '2026-10')
    - direction (TEXT: 'OUT' for spending/outgoing, 'IN' for income/earning)
    - source_channel (TEXT: 'myBCA Email', 'PasporBCA Email', 'PasporBCA ATM', 'e-Statement PDF', 'Manual Telegram')
    - tx_type (TEXT: 'QRIS Payment', 'Transfer to BCA Account', 'Transfer to BCA Virtual Account', 'Flazz Top Up', 'Overseas Debit Card', 'Overseas ATM Withdrawal', 'Manual Entry')
    - merchant_or_party (TEXT: merchant name, beneficiary name, or sender name)
    - beneficiary_account (TEXT: account number or VA number if applicable)
    - location (TEXT: city/region)
    - country (TEXT: 'INDONESIA', 'JAPAN', etc.)
    - category (TEXT: 'Food & Beverage', 'Groceries', 'Account Transfer', 'Transportation & Travel', 'Shopping', 'Business Expense', 'Cash Withdrawal', 'Bills & Utilities', 'Admin Fee', 'Donation', 'Gift', 'Miscellaneous', 'Income / Earning')
    - currency (TEXT: 'IDR', 'JPY', etc.)
    - amount_original (REAL: amount in original currency, e.g. 1450 JPY or 45000 IDR)
    - fx_rate_to_idr (REAL: exchange rate to IDR)
    - amount_idr (REAL: total amount in IDR — always sum this column for unified cashflow totals!)
    - remarks (TEXT: transfer notes or admin fee details)
    """
    try:
        rows = run_read_only_sql(sql_query)
        return json.dumps({"status": "ok", "row_count": len(rows), "rows": rows[:200]}, default=str)
    except Exception as e:
        return json.dumps({"status": "error", "error": str(e)})


def log_manual_transaction(
    direction: str,
    amount: float,
    merchant_or_party: str,
    category: str,
    currency: str = "IDR",
    remarks: str = "",
    tx_date: str = "",
) -> str:
    """
    Logs a manual income/earning or cash expense into the database when the user
    tells the bot things like '+500k from friend', 'Received 10jt salary', or 'Spent 25k cash'.
    - direction: 'IN' (for income/receiving money) or 'OUT' (for spending)
    - amount: numeric amount (e.g. 500000 for 500k)
    - merchant_or_party: who sent the money or where it was spent
    - category: one of the myBCA categories ('Income / Earning' for incoming money)
    - currency: default 'IDR' (or 'JPY', 'USD', etc.)
    - remarks: optional note
    - tx_date: optional 'YYYY-MM-DD HH:MM:SS' (leave empty for current time)
    """
    try:
        init_db()
        now_str = tx_date.strip() if tx_date.strip() else datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        if len(now_str) == 10:
            now_str += " 12:00:00"
        curr = currency.upper().strip() or "IDR"
        fx = DEFAULT_FX_RATES_TO_IDR.get(curr, 1.0)
        amt_idr = round(float(amount) * fx, 2)
        dir_clean = "IN" if direction.upper().startswith("I") else "OUT"
        cat_clean = "Income / Earning" if dir_clean == "IN" and category not in MYBCA_CATEGORIES else category

        tx = {
            "tx_date": now_str,
            "direction": dir_clean,
            "source_channel": "Manual Telegram",
            "tx_type": "Manual Income" if dir_clean == "IN" else "Manual Expense",
            "merchant_or_party": merchant_or_party,
            "country": "JAPAN" if curr == "JPY" else "INDONESIA",
            "category": cat_clean,
            "currency": curr,
            "amount_original": float(amount),
            "fx_rate_to_idr": fx,
            "amount_idr": amt_idr,
            "remarks": remarks,
        }
        inserted = upsert_transaction(tx)
        return json.dumps({
            "status": "ok",
            "inserted": inserted,
            "transaction": tx,
        })
    except Exception as e:
        return json.dumps({"status": "error", "error": str(e)})


def recategorize_merchant(merchant_pattern: str, new_category: str) -> str:
    """
    Updates the category for all past and future transactions matching `merchant_pattern`.
    Use this when the user says e.g. 'Categorize SOME_MERCHANT under Business Expense'.
    Allowed categories: Food & Beverage, Groceries, Account Transfer, Transportation & Travel,
    Shopping, Business Expense, Cash Withdrawal, Bills & Utilities, Admin Fee, Donation, Gift, Miscellaneous, Income / Earning.
    """
    try:
        updated_count = set_merchant_category_rule(merchant_pattern, new_category)
        return json.dumps({
            "status": "ok",
            "merchant_pattern": merchant_pattern,
            "new_category": new_category,
            "updated_transactions_count": updated_count,
        })
    except Exception as e:
        return json.dumps({"status": "error", "error": str(e)})


def ask_financial_ai(user_question: str, chat_history: List[Dict[str, str]] | None = None) -> str:
    """
    Runs the Gemini Financial Advisor agent with SQL Tool Calling, manual logging,
    and recategorization tools.
    """
    if not GEMINI_API_KEY:
        return (
            "⚠️ `GEMINI_API_KEY` is not set yet. Get a free API key from "
            "https://aistudio.google.com/apikey and add it to your `.env` file!"
        )

    init_db()
    sql_dialect = "PostgreSQL" if IS_POSTGRES else "SQLite"
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    system_instruction = f"""
You are a personal myBCA & PasporBCA Financial AI Assistant.
Current local date/time: {now_str}
Database SQL Dialect: {sql_dialect}

You have direct access to the user's real BCA & PasporBCA transactions table via `execute_sql_query`,
can log manual income/expenses via `log_manual_transaction`, and can update merchant categories via `recategorize_merchant`.

Core Guidelines:
1. NEVER guess or hallucinate numbers. Always call `execute_sql_query` first to inspect the actual data!
2. For complex questions like "How can I reduce/save my spendings?" or "How can I reduce food/beverage spendings?":
   - Run 1 to 3 targeted SQL queries (e.g., breakdown by category, top merchants by frequency and total IDR, weekday vs weekend or time-of-day habits, recurring small purchases like coffee/snacks/convenience stores, or Japan vs Indonesia comparisons).
   - Cite the exact merchants (e.g., `JCO DONUTS`, `DCREPES`, `SEVEN-ELEVEN`, `BANKARASINJUKUSANCYO`, `GOPAY TOPUP`), exact transaction counts, and exact IDR/JPY amounts from your query results.
   - Give concrete, actionable monthly savings targets (e.g., "Cutting X from 4x/month to 2x/month saves IDR Y").
3. Remember that `amount_idr` holds the unified IDR amount for both local (`IDR`) and overseas (`JPY`) transactions, while `amount_original` and `currency` hold the original currency value. Mention both when discussing overseas/Japan transactions!
4. Keep formatting clean and easy to read on Telegram (use bullet points and bold numbers).
"""

    client = genai.Client(api_key=GEMINI_API_KEY)
    contents = []
    if chat_history:
        for msg in chat_history[-6:]:
            role = "user" if msg["role"] == "user" else "model"
            contents.append(types.Content(role=role, parts=[types.Part.from_text(text=msg["text"])]))
    contents.append(types.Content(role="user", parts=[types.Part.from_text(text=user_question)]))

    response = client.models.generate_content(
        model=GEMINI_MODEL,
        contents=contents,
        config=types.GenerateContentConfig(
            system_instruction=system_instruction,
            tools=[execute_sql_query, log_manual_transaction, recategorize_merchant],
            temperature=0.2,
        ),
    )
    return response.text or "I processed your request, but no text response was generated."
