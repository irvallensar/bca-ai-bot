# Multimodal Financial Intelligence & Cashflow Agent (`bca-ai-bot`)

A personal multimodal AI financial intelligence pipeline built for **Bank Central Asia (BCA / myBCA)** and **PasporBCA** users. While myBCA tracks static transaction histories, it lacks a conversational AI layer to query multi-currency (`IDR` + `JPY`) cashflow, reconcile silent subscriptions and incoming transfers, or recommend data-grounded spending reductions.

This system continuously ingests bank and subscription emails via Gmail IMAP, extracts and reconciles monthly **myBCA e-Statement PDFs** and live **mobile banking screenshots**, transcribes **Telegram voice notes**, and exposes an authenticated REST API for **iPhone Apple Shortcuts & Back Tap**—all backed by **PostgreSQL (Supabase)**, **FastAPI**, and a multi-model **Google Gemini** failover engine.

---

## Key Capabilities

1. **Multi-Channel Email Ingestion & Normalization (`gmail_sync.py`, `email_parser.py`)**
   - **`bca@bca.co.id` (*Internet Transaction Journal*):** QRIS payments, BCA / BI-FAST transfers (preserving `Remarks`), Virtual Account payments (with `Admin Fee` tracking), and Flazz top-ups.
   - **`PasporBCA@klikbca.com` (*Overseas Debit & ATM*):** Extracts `text/html` transaction tables, handles Indonesian dot-thousands (`JPY 1.962` → `1962 JPY`), and converts multi-currency spend into unified `amount_idr`.
   - **Google Play & Google Payments Receipts (`googleplay-noreply@google.com`, `payments-noreply@google.com`, `noreply@google.com`):** Captures silent recurring card-on-file subscriptions (e.g., Google AI Pro / Google One, Colab) with timezone normalization (`GMT+9` / `PDT` → `WIB`) and automatic skipping of `$0` trials, ToS updates, and refunds.
   - **Fast Header-Prefiltered IMAP Sync:** Uses batched `BODY.PEEK[HEADER.FIELDS (MESSAGE-ID)]` lookups against stored `email_message_id` records to skip already-synced messages before downloading `(RFC822)` bodies, plus single-connection batch PostgreSQL upserts (`ON CONFLICT (dedup_key) DO NOTHING`) protected by an `asyncio.Lock`.

2. **Cross-Source e-Statement PDF & Screenshot Reconciliation (`statement_parser.py`, `database.py`)**
   - Upload monthly **myBCA e-Statement PDFs** or live **Mutasi Rekening / Uang Masuk screenshots** via Telegram or iPhone Shortcuts.
   - Performs **1-to-1 cross-source matching** by `(date, direction, amount)` against existing email transactions so overlapping outgoing (`DB`) rows are never duplicated while incoming (`CR`) transfers and silent bank fees (`BIAYA ADM`) are inserted cleanly.
   - Reconciles `±3`-day foreign-currency (`JPY`) card settlements on monthly e-Statement PDFs by updating estimated fallback FX rates with BCA's exact debited IDR amount.

3. **Multimodal Text-to-SQL Advisor, Voice Notes & iPhone Shortcuts (`ai_agent.py`, `bot.py`)**
   - **Single-Connection Cached SQL Snapshot:** Consolidates 5 summary queries into 1 database connection (`fetch_financial_snapshot`) cached in memory (invalidated automatically on writes), paired with read-only `execute_sql_query`, `log_manual_transaction`, and `recategorize_merchant` tool calling.
   - **Voice & Audio Support:** Transcribes Telegram voice messages (`audio/ogg`, `audio/mp4`, `audio/wav`) and raw audio uploads via Gemini multimodal audio before routing to the SQL advisor.
   - **Native iOS Integration (`POST /api/ask`, `/api/ingest`):** Authenticated JSON, raw audio, and raw image/PDF endpoint (with magic-byte detection) designed for **iPhone Apple Shortcuts & Back Tap**—enabling both voice Q&A and 1-tap `Take Screenshot` incoming-transfer sync outside Telegram.
   - **Multi-Generation Gemini Failover:** Defaults to `gemini-3.5-flash-lite` with automatic cascade across `gemini-3.1-flash-lite`, `gemini-2.5-flash-lite`, `gemini-3.6-flash`, and `gemini-2.5-flash`, version-aware `ThinkingConfig` (`thinking_budget=0` for 2.5 vs. `thinking_level=LOW` for 3.x), `10s` fail-fast chat timeouts, and 15-minute sticky model promotion.

---

## Quick Start (Local Setup)

1. **Clone & install dependencies:**
   ```bash
   git clone https://github.com/irvallensar/bca-ai-bot.git
   cd bca-ai-bot
   python3 -m venv .venv
   source .venv/bin/activate
   pip install -r requirements.txt
   ```

2. **Configure environment variables:**
   ```bash
   cp .env.example .env
   ```
   Fill in your credentials in `.env` (never committed; excluded via `.gitignore`):
   - `GEMINI_API_KEY`: Free API key from [Google AI Studio](https://aistudio.google.com/apikey)
   - `TELEGRAM_BOT_TOKEN`: Bot token from `@BotFather`
   - `ALLOWED_TELEGRAM_USER_IDS`: Your numeric Telegram user ID (restricts access)
   - `GMAIL_ADDRESS` & `GMAIL_APP_PASSWORD`: 16-character [Google App Password](https://myaccount.google.com/apppasswords) for IMAP sync
   - `DATABASE_URL`: Defaults to local SQLite (`sqlite:///./mybca_transactions.db`), or set to a Supabase PostgreSQL pooler URI for cloud hosting

3. **Run the application:**
   - **Interactive CLI Mode:**
     ```bash
     python bot.py --cli
     ```
   - **FastAPI + Telegram Server Mode:**
     ```bash
     python bot.py
     ```

---

## Cloud Deployment (Render + Supabase)

Configured via `render.yaml` for zero-cost 24/7 operation:
1. **Supabase (PostgreSQL):** Create a project and copy the **Session/Transaction Pooler URI** into `DATABASE_URL` (IPv4-compatible for Render, using `psycopg` v3).
2. **Render Web Service:** Deploy via Blueprint (`render.yaml`) and set your secret environment variables (`DATABASE_URL`, `GEMINI_API_KEY`, `TELEGRAM_BOT_TOKEN`, `ALLOWED_TELEGRAM_USER_IDS`, `GMAIL_ADDRESS`, `GMAIL_APP_PASSWORD`). Render sets `RENDER_EXTERNAL_URL` automatically, enabling zero-conflict **Telegram Webhook mode** with `X-Telegram-Bot-Api-Secret-Token` verification.
3. **Optional Keep-Alive:** Monitor `/health` on UptimeRobot every 5–10 minutes to prevent free-tier cold starts.

---

## Privacy & Security
- `.env`, `*.db`, `*.sqlite*`, PDFs, screenshots, and audio recordings are strictly excluded via `.gitignore`.
- All Telegram handlers enforce `ALLOWED_TELEGRAM_USER_IDS`; `/telegram-webhook` verifies an HMAC-derived secret header; `/api/ask` validates Bearer/raw tokens with `hmac.compare_digest`.
- `run_read_only_sql` strictly allows `SELECT`/`WITH` statements and blocks write DDL/DML keywords.
