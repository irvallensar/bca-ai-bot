# 🏦 bca-ai-bot — Personal myBCA & PasporBCA Financial AI

An open-source personal AI financial assistant for **Bank Central Asia (BCA)** users. It automatically parses your **myBCA (`bca@bca.co.id`)** and **PasporBCA (`PasporBCA@klikbca.com`)** transaction notification emails, reconciles **myBCA e-Statement PDFs** and manual income logs, and answers complex spending & saving questions via **Telegram** (or terminal CLI) using **Google Gemini 3.6 Flash** + **SQL Tool Calling**.

---

## ✨ Features

1. **Multi-Format BCA & PasporBCA Email Parser (`email_parser.py`)**
   - **`bca@bca.co.id` (*Internet Transaction Journal*)**:
     - QRIS Payments (e.g., cafes, restaurants, minimarkets)
     - Transfers to BCA Accounts & BI-FAST (preserves your `Remarks` field so the AI knows *why* you transferred money)
     - Transfers to BCA Virtual Accounts (GoPay top-ups, Telkomsel/PLN bills, including `Admin Fee` tracking)
     - Flazz Card Top Ups
   - **`PasporBCA@klikbca.com` (*International Debit & Overseas ATM*)**:
     - Overseas Debit Card (`Notifikasi Transaksi`, e.g., merchants in Japan/Singapore/etc., properly handling Indonesian dot thousands separators like `JPY 1.962` → `1962 JPY`)
     - Overseas ATM Withdrawals (`Debit BCA Overseas ATM Transaction Information`, e.g., `SEVEN BANK - JP`, `JPY 20.000` → `20000 JPY`)
   - **HTML Body Extraction Fix**: `PasporBCA` emails put a dummy `-` in `text/plain` and store the actual transaction table in `text/html`. The parser automatically extracts `text/html`.

2. **Hybrid Income & e-Statement PDF Sync (`statement_parser.py` & `ai_agent.py`)**
   - **Manual Chat Logging**: Since BCA does not send emails for incoming transfers, simply message the bot `"+5,000,000 salary"` or `"+150k from friend for dinner"`—the AI automatically logs it as `IN` (`Income / Earning`).
   - **Monthly myBCA e-Statement PDF / Screenshot Upload**: Drop your monthly myBCA PDF e-Statement or screenshot directly into the Telegram chat. Gemini 3.6 Flash extracts all incoming (`CR`) and outgoing (`DB`) rows and deduplicates them against existing email records.

3. **Smart SQL Financial Advisor (`ai_agent.py`)**
   - Uses **Function Calling (`execute_sql_query`)** to run real SQL aggregations over your database before answering questions like:
     - *"How can I reduce my food & beverage spendings?"*
     - *"Compare my spending in Japan vs Indonesia"*
     - *"What are my top 10 merchants this month?"*
   - Supports on-the-fly recategorization: tell the bot *"Categorize [Merchant/Name] as Business Expense"* and it updates both past transactions and future rules.

---

## 🚀 Quick Start (Local Setup)

1. **Clone & install dependencies:**
   ```bash
   git clone https://github.com/irvallensar/bca-ai-bot.git
   cd bca-ai-bot
   python3 -m venv .venv
   source .venv/bin/activate
   pip install -r requirements.txt
   ```

2. **Configure `.env`:**
   ```bash
   cp .env.example .env
   ```
   - Get a free **Gemini API Key** from [Google AI Studio](https://aistudio.google.com/apikey) and set `GEMINI_API_KEY`.
   - Create a free **Telegram Bot** via `@BotFather` on Telegram and set `TELEGRAM_BOT_TOKEN`.
   - *(For automated Gmail IMAP sync)* Create a 16-character [Google App Password](https://myaccount.google.com/apppasswords) and set `GMAIL_ADDRESS` and `GMAIL_APP_PASSWORD`.

3. **Run the Bot:**
   - **Interactive Terminal CLI Mode (No Telegram needed):**
     ```bash
     python bot.py --cli
     ```
   - **Telegram Bot + FastAPI Server Mode:**
     ```bash
     python bot.py
     ```

---

## ☁️ Free 24/7 Cloud Deployment (Render.com + Supabase + UptimeRobot)

You can host your bot 24/7 for **$0/month (no credit card required)**:

1. **Supabase (Free Cloud PostgreSQL)**:
   - Create a free project at [supabase.com](https://supabase.com).
   - Go to **Project Settings → Database → Connection string → URI (Transaction Pooler, port 6543)** and copy your `postgresql://...` URI into `DATABASE_URL`.
2. **Render.com (Free Web Service)**:
   - Fork/clone this repo into your GitHub account.
   - On [render.com](https://render.com), click **New → Web Service** (or **Blueprint** to auto-load `render.yaml`), choose the **Free** plan, and set your environment variables (`DATABASE_URL`, `GEMINI_API_KEY`, `TELEGRAM_BOT_TOKEN`, `ALLOWED_TELEGRAM_USER_IDS`, `GMAIL_ADDRESS`, `GMAIL_APP_PASSWORD`).
3. **Keep Alive 24/7 (Optional)**:
   - Add your Render health check URL (`https://your-app.onrender.com/health`) to [UptimeRobot](https://uptimerobot.com) (free) with a 10-minute ping interval.

---

## 🔒 Privacy & Security Note
- Never commit your `.env` file or `*.db` SQLite database files (already excluded via `.gitignore`).
- Always set `ALLOWED_TELEGRAM_USER_IDS` in `.env` to your numeric Telegram User ID (displayed when you run `/start`) so only you can query your financial data.
