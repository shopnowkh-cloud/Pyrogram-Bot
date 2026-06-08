---
name: Order module integration
description: Ordering system ported from TelegramOrderBot to Pyrogram async; key quirks and decisions.
---

## Rules

**Pyrogram channel filter:** Use `filters.channel` (not `filters.channel_post` — that attribute does not exist in kurigram/Pyrogram).

**Why:** `filters.channel_post` is a Bot API concept; Pyrogram uses `filters.channel` for messages from channels.

**How to apply:** Any handler that needs to catch channel posts must use `@app.on_message(filters.channel)`.

---

**Database:** Replit PostgreSQL via `DATABASE_URL` env var + psycopg2-binary. Tables prefixed `order_`.

**init_order_module(app):** Must be called AFTER `app.start()` in bot.py startup — it runs sync DB init on the current thread.

**ADMIN_ID:** Read from `os.environ.get("ADMIN_ID", "0")` — must be set in Replit secrets for admin features to work.

**order_sessions vs _sessions:** order_module uses its own `order_sessions` dict (keyed by int user_id), completely separate from bot.py's `_sessions` (UserSession dataclass). No conflict.

**Callback dispatch order:** order_module.handle_order_callback is called BEFORE bot.py's `await query.answer()`. If it returns True, bot.py returns early. This avoids double-answering for order callbacks.
