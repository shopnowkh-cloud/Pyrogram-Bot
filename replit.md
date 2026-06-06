# RADY BOT

## Overview

A multi-purpose Telegram Bot built with Python and Pyrogram (MTProto).

## Features
- Text styling (Bold, Italic, Script, Bubble, Upside-down, etc.)
- PDF utilities (images → PDF, PDF → PNG/JPG)
- QR code generation and scanning
- Gold/Silver/Platinum spot price tracking
- Background removal via remove.bg API

## Bot

- **Entry point**: `bot.py`
- **Library**: Pyrofork 2.3.69 (MTProto) + TgCrypto
- **Runtime**: Python 3.11
- **Workflow**: "Telegram Bot" — runs `python bot.py`

## Secrets Required
- `TELEGRAM_API_ID` — Telegram API ID (from my.telegram.org)
- `TELEGRAM_API_HASH` — Telegram API hash (from my.telegram.org)
- `TELEGRAM_BOT_TOKEN` — Bot token (from @BotFather)
- `REMOVE_BG_API_KEY` — remove.bg API key (from remove.bg/api)

## User Preferences
