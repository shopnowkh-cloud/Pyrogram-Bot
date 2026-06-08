# RADY BOT

## Overview

A multi-purpose Telegram Bot built with Python and Kurigram (Pyrogram MTProto fork).

## Features
- Text styling (Bold, Italic, Script, Bubble, Upside-down, etc.)
- PDF utilities (images → PDF, PDF → PNG/JPG)
- QR code generation and scanning
- Gold/Silver/Platinum spot price tracking
- Background removal via remove.bg API
- Temporary email (DropMail.me)
- Order/store system with KHPay + Telegram Stars payments

## Bot

- **Entry point**: `bot.py`
- **Library**: `kurigram==2.2.23` (Pyrogram MTProto) + TgCrypto
- **Runtime**: Python 3.11
- **Workflow**: "Telegram Bot" — runs `python bot.py`

## Secrets Required
- `TELEGRAM_API_ID` — Telegram API ID (from my.telegram.org)
- `TELEGRAM_API_HASH` — Telegram API hash (from my.telegram.org)
- `TELEGRAM_BOT_TOKEN` — Bot token (from @BotFather)
- `REMOVE_BG_API_KEY` — remove.bg API key (from remove.bg/api)
- `DROPMAIL_API_TOKEN` — DropMail.me API token
- `KHPAY_API_KEY` — KHPay payment API key
- `ADMIN_ID` — Telegram user ID of the bot admin
- `TELEGRAM_CHANNEL_ID` — Channel ID for order notifications
- `DATABASE_URL` — PostgreSQL connection string
- `BOT_SESSION_STRING` — Pyrogram session string (exported on first run, paste into Render env vars)

## Render Deployment

### How to deploy on Render
1. **First run** the bot here on Replit — it will print `BOT_SESSION_STRING` in the logs.
2. Copy that string and add it as an env var on Render (`BOT_SESSION_STRING`).
3. Set all env vars listed above in Render's "Environment" tab.
4. Set **Start Command**: `python bot.py`
5. Set **Runtime**: Python 3
6. Set **Build Command**: `pip install -r requirements.txt`

### Render-ready rules (apply whenever editing code)
- All config **must** come from `os.environ` — never hardcode values
- Session persistence uses `BOT_SESSION_STRING` env var → in-memory session (no `.session` file needed on Render)
- Database uses `DATABASE_URL` env var (PostgreSQL — use Render PostgreSQL add-on or external)
- No local file writes that are critical — Render's filesystem is ephemeral (non-persistent disk)
- `rmbg_stats.json` and `email_sessions.json` are non-critical local caches — acceptable
- Background tasks (`_em_poll_loop`, `_em_restore_loop`) use `asyncio` — compatible with Render Worker
- Deploy as a **Background Worker** on Render (not a Web Service — no HTTP port needed)

## User Preferences
- Code changes must always be kept ready for deployment on Render
- Use `kurigram==2.2.23` as the sole Pyrogram MTProto provider — never install standalone `pyrogram`
