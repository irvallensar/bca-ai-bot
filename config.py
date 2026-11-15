import os
from dotenv import load_dotenv

load_dotenv()

# Database configuration:
# Defaults to a local SQLite database for instant local testing.
# For Render / Koyeb cloud deployment, set DATABASE_URL to your Supabase PostgreSQL URI:
# e.g. postgresql://postgres.[project-ref]:[password]@aws-0-ap-southeast-1.pooler.supabase.com:6543/postgres
DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./mybca_transactions.db")

# Google Gemini API Key (Free tier from https://aistudio.google.com/apikey)
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")

# Telegram Bot Configuration (Get from @BotFather on Telegram)
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
# Comma-separated Telegram numeric user IDs allowed to query your private bank data (leave empty to allow any during initial test, then lock down!)
ALLOWED_TELEGRAM_USER_IDS = [
    int(uid.strip())
    for uid in os.getenv("ALLOWED_TELEGRAM_USER_IDS", "").split(",")
    if uid.strip().isdigit()
]

# Gmail IMAP Configuration for Automated Email Polling
GMAIL_ADDRESS = os.getenv("GMAIL_ADDRESS", "")
GMAIL_APP_PASSWORD = os.getenv("GMAIL_APP_PASSWORD", "")
EMAIL_SYNC_INTERVAL_SECONDS = int(os.getenv("EMAIL_SYNC_INTERVAL_SECONDS", "300"))  # Every 5 mins

# Default fallback exchange rates to IDR (updated dynamically or via e-Statement reconciliation)
DEFAULT_FX_RATES_TO_IDR = {
    "IDR": 1.0,
    "JPY": float(os.getenv("JPY_TO_IDR_RATE", "105.5")),
    "USD": float(os.getenv("USD_TO_IDR_RATE", "15850.0")),
    "SGD": float(os.getenv("SGD_TO_IDR_RATE", "11800.0")),
    "EUR": float(os.getenv("EUR_TO_IDR_RATE", "17000.0")),
    "MYR": float(os.getenv("MYR_TO_IDR_RATE", "3550.0")),
}

# Official myBCA Financial Diary Categories + Cashflow extensions
MYBCA_CATEGORIES = [
    "Food & Beverage",
    "Groceries",
    "Account Transfer",
    "Transportation & Travel",
    "Shopping",
    "Business Expense",
    "Cash Withdrawal",
    "Bills & Utilities",
    "Admin Fee",
    "Donation",
    "Gift",
    "Miscellaneous",
    "Income / Earning",
]
