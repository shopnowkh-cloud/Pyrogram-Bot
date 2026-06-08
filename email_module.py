"""Email Temporary — 100% faithful port of Email11 for Pyrogram + PostgreSQL.
github.com/limsovannrady/Email11
"""
import os
import asyncio
import logging
from typing import Optional

import psycopg2
from psycopg2.extras import RealDictCursor

import dropmail as dm

from pyrogram import Client
from pyrogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from pyrogram.enums import ParseMode

logger = logging.getLogger(__name__)

DATABASE_URL     = os.environ.get("DATABASE_URL", "")
POLL_INTERVAL    = 3    # seconds — same as Email11
RESTORE_INTERVAL = 600  # seconds — same as Email11

_app: Optional[Client] = None


# ── DB helpers ─────────────────────────────────────────────────────────────────
def _conn():
    if not DATABASE_URL:
        raise RuntimeError("DATABASE_URL is not set — add it to Render environment variables")
    return psycopg2.connect(DATABASE_URL)


def init_db():
    with _conn() as c:
        with c.cursor() as cur:
            cur.execute("""
            CREATE TABLE IF NOT EXISTS email_sessions (
                telegram_user_id    BIGINT PRIMARY KEY,
                telegram_username   TEXT,
                telegram_first_name TEXT,
                dropmail_session_id TEXT,
                email_address       TEXT,
                address_id          TEXT,
                restore_key         TEXT,
                is_active           BOOLEAN DEFAULT TRUE,
                updated_at          TIMESTAMPTZ DEFAULT NOW()
            )""")
            cur.execute("""
            CREATE TABLE IF NOT EXISTS email_history (
                id                  SERIAL PRIMARY KEY,
                telegram_user_id    BIGINT NOT NULL,
                email_address       TEXT NOT NULL,
                dropmail_session_id TEXT,
                address_id          TEXT,
                restore_key         TEXT,
                last_mail_id        TEXT,
                created_at          TIMESTAMPTZ DEFAULT NOW()
            )""")
        c.commit()
    logger.info("Email DB tables ready")


# ── Storage (mirrors Email11 storage.py, backed by PostgreSQL) ─────────────────
def upsert_session(telegram_user_id, telegram_username, telegram_first_name,
                   dropmail_session_id, email_address,
                   address_id=None, restore_key=None):
    with _conn() as c:
        with c.cursor() as cur:
            cur.execute("""
            INSERT INTO email_sessions
              (telegram_user_id, telegram_username, telegram_first_name,
               dropmail_session_id, email_address, address_id, restore_key,
               is_active, updated_at)
            VALUES (%s,%s,%s,%s,%s,%s,%s,TRUE,NOW())
            ON CONFLICT (telegram_user_id) DO UPDATE SET
              telegram_username      = EXCLUDED.telegram_username,
              telegram_first_name    = EXCLUDED.telegram_first_name,
              dropmail_session_id    = EXCLUDED.dropmail_session_id,
              email_address          = EXCLUDED.email_address,
              address_id             = EXCLUDED.address_id,
              restore_key            = EXCLUDED.restore_key,
              is_active              = TRUE,
              updated_at             = NOW()
            """, (telegram_user_id, telegram_username, telegram_first_name,
                  dropmail_session_id, email_address, address_id, restore_key))
        c.commit()


def get_session(telegram_user_id) -> Optional[dict]:
    with _conn() as c:
        with c.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                "SELECT * FROM email_sessions WHERE telegram_user_id=%s",
                (telegram_user_id,))
            row = cur.fetchone()
            return dict(row) if row else None


def update_session_after_restore(telegram_user_id, new_session_id,
                                  new_address_id, new_restore_key):
    with _conn() as c:
        with c.cursor() as cur:
            cur.execute("""
            UPDATE email_sessions
               SET dropmail_session_id=%s, address_id=%s, restore_key=%s,
                   is_active=TRUE, updated_at=NOW()
             WHERE telegram_user_id=%s
            """, (new_session_id, new_address_id, new_restore_key, telegram_user_id))
        c.commit()


def deactivate_session(telegram_user_id):
    with _conn() as c:
        with c.cursor() as cur:
            cur.execute("""
            UPDATE email_sessions
               SET is_active=FALSE, dropmail_session_id=NULL,
                   email_address=NULL, address_id=NULL, restore_key=NULL,
                   updated_at=NOW()
             WHERE telegram_user_id=%s
            """, (telegram_user_id,))
        c.commit()


def add_email_to_history(telegram_user_id, email_address,
                         dropmail_session_id=None, address_id=None,
                         restore_key=None):
    with _conn() as c:
        with c.cursor() as cur:
            cur.execute("""
            INSERT INTO email_history
              (telegram_user_id, email_address, dropmail_session_id,
               address_id, restore_key)
            VALUES (%s,%s,%s,%s,%s)
            """, (telegram_user_id, email_address, dropmail_session_id,
                  address_id, restore_key))
        c.commit()


def get_email_history(telegram_user_id) -> list:
    with _conn() as c:
        with c.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("""
            SELECT email_address FROM email_history
             WHERE telegram_user_id=%s ORDER BY created_at DESC
            """, (telegram_user_id,))
            return [r["email_address"] for r in cur.fetchall()]


def get_user_history_entries(telegram_user_id) -> list:
    with _conn() as c:
        with c.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("""
            SELECT * FROM email_history
             WHERE telegram_user_id=%s ORDER BY created_at DESC
            """, (telegram_user_id,))
            return [dict(r) for r in cur.fetchall()]


def get_all_history_entries() -> list:
    with _conn() as c:
        with c.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                "SELECT * FROM email_history WHERE restore_key IS NOT NULL")
            return [dict(r) for r in cur.fetchall()]


def get_history_entry_by_email(telegram_user_id, email_address) -> Optional[dict]:
    with _conn() as c:
        with c.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("""
            SELECT * FROM email_history
             WHERE telegram_user_id=%s AND email_address=%s
             ORDER BY created_at DESC LIMIT 1
            """, (telegram_user_id, email_address))
            row = cur.fetchone()
            return dict(row) if row else None


def update_history_session(history_id, new_session_id, new_address_id, new_restore_key):
    with _conn() as c:
        with c.cursor() as cur:
            cur.execute("""
            UPDATE email_history
               SET dropmail_session_id=%s, address_id=%s,
                   restore_key=%s, last_mail_id=NULL
             WHERE id=%s
            """, (new_session_id, new_address_id, new_restore_key, history_id))
        c.commit()


def update_history_last_mail_id(history_id, mail_id):
    with _conn() as c:
        with c.cursor() as cur:
            cur.execute(
                "UPDATE email_history SET last_mail_id=%s WHERE id=%s",
                (mail_id, history_id))
        c.commit()


def remove_email_from_history(history_id):
    with _conn() as c:
        with c.cursor() as cur:
            cur.execute("DELETE FROM email_history WHERE id=%s", (history_id,))
        c.commit()


# ── Keyboards (mirrors Email11 handlers.py) ────────────────────────────────────
def _back_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("Back", callback_data="email",
                             icon_custom_emoji_id="5877629862306385808")
    ]])


def email_active_kb(addr: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(f"📋 {addr}", copy_text=addr)],
        [InlineKeyboardButton("📥 ពិនិត្យប្រអប់", callback_data="em_inbox"),
         InlineKeyboardButton("🔄 អ៊ីម៉ែលថ្មី",   callback_data="em_new")],
        [InlineKeyboardButton("📓 List",            callback_data="em_list"),
         InlineKeyboardButton("🗑 លុបអ៊ីម៉ែល",    callback_data="em_del")],
        [InlineKeyboardButton("Back", callback_data="home",
                              icon_custom_emoji_id="5877629862306385808")],
    ])


def email_empty_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("✉️ Email ថ្មី", callback_data="em_new")],
        [InlineKeyboardButton("📓 List",       callback_data="em_list")],
        [InlineKeyboardButton("Back", callback_data="home",
                              icon_custom_emoji_id="5877629862306385808")],
    ])


def inbox_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🔄 ផ្ទុកឡើងវិញ", callback_data="em_inbox"),
         InlineKeyboardButton("🔄 អ៊ីម៉ែលថ្មី",  callback_data="em_new")],
        [InlineKeyboardButton("📓 List",           callback_data="em_list"),
         InlineKeyboardButton("🗑 លុបអ៊ីម៉ែល",   callback_data="em_del")],
        [InlineKeyboardButton("Back", callback_data="home",
                              icon_custom_emoji_id="5877629862306385808")],
    ])


# ── Handler: open email menu ───────────────────────────────────────────────────
async def handle_email_open(edit_fn, user_id: int):
    loop = asyncio.get_running_loop()
    session = await loop.run_in_executor(None, get_session, user_id)
    if session and session.get("is_active") and session.get("email_address"):
        addr = session["email_address"]
        await edit_fn(
            "📧 <b>Email បណ្ដោះអាសន្ន</b>\n\n"
            "👆 ចុចលើអ៊ីម៉ែលខាងលើដើម្បីចម្លង",
            email_active_kb(addr))
    else:
        await edit_fn("📧 <b>Email បណ្ដោះអាសន្ន</b>", email_empty_kb())


# ── Handler: new email (mirrors handle_new_email in Email11) ───────────────────
async def handle_new_email(edit_fn, user, user_id: int):
    await edit_fn("⏳ <b>កំពុងបង្កើត Email...</b>")
    loop = asyncio.get_running_loop()
    try:
        result = await loop.run_in_executor(None, dm.create_session, user_id)
    except Exception as e:
        await edit_fn(f"❌ បង្កើតមិនបានទេ: {e}", email_empty_kb())
        return
    if not result:
        await edit_fn(
            "❌ មិនអាចបង្កើត session បានទេ។ សូមព្យាយាមម្ដងទៀត។",
            email_empty_kb())
        return

    def _persist():
        upsert_session(
            telegram_user_id=user_id,
            telegram_username=getattr(user, "username", None),
            telegram_first_name=getattr(user, "first_name", None),
            dropmail_session_id=result["session_id"],
            email_address=result["email"],
            address_id=result["address_id"],
            restore_key=result["restore_key"],
        )
        add_email_to_history(
            user_id, result["email"],
            dropmail_session_id=result["session_id"],
            address_id=result["address_id"],
            restore_key=result["restore_key"],
        )

    await loop.run_in_executor(None, _persist)
    addr = result["email"]
    await edit_fn(
        f"✅ <b>Email ថ្មីបានបង្កើត!</b>\n\n<code>{addr}</code>",
        email_active_kb(addr))


# ── Handler: check inbox (mirrors _show_inbox in Email11) ─────────────────────
async def handle_inbox(edit_fn, user_id: int):
    loop = asyncio.get_running_loop()
    session = await loop.run_in_executor(None, get_session, user_id)

    if not session or not session.get("is_active") or not session.get("dropmail_session_id"):
        await edit_fn(
            "❌ អ្នកមិនមាន session ដែលសកម្មទេ។\n\n"
            "ចុច <b>✉️ Email ថ្មី</b> ដើម្បីបង្កើត។",
            email_empty_kb())
        return

    try:
        mails = await loop.run_in_executor(
            None, dm.get_new_mails,
            session["dropmail_session_id"], user_id, None)
    except Exception as e:
        await edit_fn(f"❌ កំហុសក្នុងការពិនិត្យ: {e}", inbox_kb())
        return

    if mails is None:
        await edit_fn(
            f"⚠️ Session ផុតកំណត់។ កំពុងស្តារឡើងវិញ...\n"
            f"📧 <code>{session.get('email_address', '')}</code>",
            inbox_kb())
        return

    addr = session.get("email_address", "")
    if not mails:
        text = (
            f"📭 <b>ប្រអប់ទទេ</b>\n\n"
            f"📧 <code>{addr}</code>\n\n"
            f"មិនទាន់មានអ៊ីម៉ែលចូលទេ។ ខ្ញុំនឹងជូនដំណឹងអ្នកភ្លាមៗ។"
        )
    else:
        text = f"📬 <b>ប្រអប់ — {len(mails)} សំបុត្រ</b>\n📧 <code>{addr}</code>\n\n"
        for i, mail in enumerate(mails[-5:], 1):
            subject   = mail.get("headerSubject") or "(គ្មានប្រធានបទ)"
            from_addr = mail.get("fromAddr") or "unknown"
            body      = (mail.get("text") or "").strip()
            preview   = body[:200] + "…" if len(body) > 200 else body
            text += (
                f"━━━━━━━━━━━━━━━━━━\n"
                f"<b>#{i} {subject}</b>\n"
                f"From: <code>{from_addr}</code>\n"
                f"{preview or '<i>(ទទេ)</i>'}\n\n"
            )
    await edit_fn(text, inbox_kb())


# ── Handler: list (mirrors handle_inbox in Email11) ────────────────────────────
async def handle_list(edit_fn, user_id: int):
    loop = asyncio.get_running_loop()
    history = await loop.run_in_executor(None, get_email_history, user_id)
    if not history:
        await edit_fn(
            "📭 គ្មាន email ណាទេ។\n\nចុច ✉️ Email ថ្មី ដើម្បីបង្កើត។",
            email_empty_kb())
        return
    lines = "\n".join(f"{i+1}- <code>{e}</code>" for i, e in enumerate(history))
    text  = f"📧 <b>Email {len(history)}</b>\n\n{lines}"
    await edit_fn(text, _back_kb())


# ── Handler: delete picker (mirrors handle_delete in Email11) ──────────────────
async def handle_delete_picker(edit_fn, user_id: int):
    loop = asyncio.get_running_loop()
    entries = await loop.run_in_executor(None, get_user_history_entries, user_id)
    if not entries:
        await edit_fn("📭 គ្មាន email ណាទេ។", email_empty_kb())
        return
    buttons = [
        [InlineKeyboardButton(
            f"🗑 {e['email_address']}",
            callback_data=f"em_del_one:{e['email_address']}")]
        for e in entries
    ]
    buttons.append([InlineKeyboardButton(
        "Back", callback_data="email",
        icon_custom_emoji_id="5877629862306385808")])
    await edit_fn("ជ្រើសរើស Email ដែលអ្នកចង់លុប៖", InlineKeyboardMarkup(buttons))


# ── Handler: delete current active session (mirrors delete_email cb in Email11) ─
async def handle_delete_current(edit_fn, user_id: int):
    loop = asyncio.get_running_loop()
    session = await loop.run_in_executor(None, get_session, user_id)
    address_id = session.get("address_id") if session else None
    if address_id:
        await loop.run_in_executor(None, dm.delete_address, address_id, user_id)
    await loop.run_in_executor(None, deactivate_session, user_id)
    await edit_fn(
        "🗑 <b>អ៊ីម៉ែលត្រូវបានលុបចោលហើយ។</b>\n\n"
        "ចុច <b>✉️ Email ថ្មី</b> ដើម្បីបង្កើតថ្មី។",
        email_empty_kb())


# ── Handler: delete one from history (mirrors del_email: cb in Email11) ────────
async def handle_delete_one(edit_fn, user_id: int, email_to_delete: str):
    loop = asyncio.get_running_loop()
    entry = await loop.run_in_executor(
        None, get_history_entry_by_email, user_id, email_to_delete)
    if not entry:
        await edit_fn(
            f"❌ រកមិនឃើញ <code>{email_to_delete}</code>",
            email_empty_kb())
        return
    if entry.get("address_id"):
        await loop.run_in_executor(
            None, dm.delete_address, entry["address_id"], user_id)
    await loop.run_in_executor(None, remove_email_from_history, entry["id"])
    session = await loop.run_in_executor(None, get_session, user_id)
    if session and session.get("email_address") == email_to_delete:
        await loop.run_in_executor(None, deactivate_session, user_id)
    await edit_fn(
        f"🗑 លុប <code>{email_to_delete}</code> បានសម្រេច។",
        email_empty_kb())


# ── Main dispatch (call from bot.py cb_handler) ────────────────────────────────
async def handle_email_callback(client, query, edit_fn) -> bool:
    """
    Returns True if the callback was handled by this module.
    Callbacks handled:
      email | em_new | em_inbox | em_list | em_del | em_del_one:<addr>
    """
    d    = query.data or ""
    uid  = query.from_user.id
    user = query.from_user

    if d == "email":
        await handle_email_open(edit_fn, uid)
        return True
    if d == "em_new":
        await handle_new_email(edit_fn, user, uid)
        return True
    if d == "em_inbox":
        await handle_inbox(edit_fn, uid)
        return True
    if d == "em_list":
        await handle_list(edit_fn, uid)
        return True
    if d == "em_del":
        await handle_delete_picker(edit_fn, uid)
        return True
    if d == "em_del_cur":
        await handle_delete_current(edit_fn, uid)
        return True
    if d.startswith("em_del_one:"):
        addr = d[len("em_del_one:"):]
        await handle_delete_one(edit_fn, uid, addr)
        return True
    return False


# ── Poll one history entry (mirrors _poll_one in Email11 handlers.py) ──────────
async def _poll_one(entry: dict):
    history_id    = entry["id"]
    user_id       = entry["telegram_user_id"]
    session_id    = entry["dropmail_session_id"]
    email_address = entry["email_address"]
    restore_key   = entry["restore_key"]
    last_mail_id  = entry.get("last_mail_id")

    if not session_id:
        return

    loop = asyncio.get_running_loop()
    try:
        mails = await loop.run_in_executor(
            None, dm.get_new_mails, session_id, user_id, last_mail_id)
    except Exception as e:
        logger.warning(f"Poll error [{email_address}]: {e}")
        return

    if mails is None:
        # Session expired — auto-restore
        logger.info(f"Restoring [{email_address}] for user {user_id}...")
        try:
            restored = await loop.run_in_executor(
                None, dm.restore_session, email_address, restore_key, user_id)
        except Exception as e:
            logger.warning(f"Restore failed [{email_address}]: {e}")
            return

        if restored and not restored.get("already_in_use"):
            def _persist_restore():
                update_history_session(
                    history_id,
                    new_session_id=restored["session_id"],
                    new_address_id=restored.get("address_id"),
                    new_restore_key=restored.get("restore_key"),
                )
                cur_sess = get_session(user_id)
                if cur_sess and cur_sess.get("email_address") == email_address:
                    update_session_after_restore(
                        telegram_user_id=user_id,
                        new_session_id=restored["session_id"],
                        new_address_id=restored.get("address_id"),
                        new_restore_key=restored.get("restore_key"),
                    )
            await loop.run_in_executor(None, _persist_restore)
            logger.info(f"Restored [{email_address}] → session {restored['session_id']}")
        return

    if not mails:
        return

    newest_id = None
    for mail in mails:
        mail_id   = mail.get("id")
        if last_mail_id and mail_id == last_mail_id:
            continue
        subject   = mail.get("headerSubject") or "(គ្មានប្រធានបទ)"
        from_addr = mail.get("fromAddr") or "unknown"
        to_addr   = mail.get("toAddr") or email_address
        body      = (mail.get("text") or "").strip()
        preview   = body[:800] + "\n…" if len(body) > 800 else body
        text = (
            f"📬 <b>អ៊ីម៉ែលថ្មីចូលមកដល់!</b>\n\n"
            f"📧 ទៅ: <code>{to_addr}</code>\n"
            f"👤 ពី: <code>{from_addr}</code>\n"
            f"📝 ប្រធានបទ: <b>{subject}</b>\n\n"
            f"{'─' * 28}\n"
            f"{preview if preview else '<i>(ទទេ)</i>'}"
        )
        if _app:
            try:
                await _app.send_message(
                    chat_id=user_id, text=text, parse_mode=ParseMode.HTML)
            except Exception as e:
                logger.warning(f"Failed to notify user {user_id}: {e}")
        newest_id = mail_id

    if newest_id:
        await loop.run_in_executor(
            None, update_history_last_mail_id, history_id, newest_id)


# ── Restore one entry (mirrors _restore_one in Email11 handlers.py) ────────────
async def _restore_one(entry: dict):
    history_id    = entry["id"]
    user_id       = entry["telegram_user_id"]
    email_address = entry["email_address"]
    restore_key   = entry.get("restore_key")
    if not restore_key:
        return
    loop = asyncio.get_running_loop()
    try:
        restored = await loop.run_in_executor(
            None, dm.restore_session, email_address, restore_key, user_id)
    except Exception as e:
        logger.warning(f"Proactive restore failed [{email_address}]: {e}")
        return
    if restored and not restored.get("already_in_use"):
        def _persist():
            update_history_session(
                history_id,
                new_session_id=restored["session_id"],
                new_address_id=restored.get("address_id"),
                new_restore_key=restored.get("restore_key"),
            )
            cur_sess = get_session(user_id)
            if cur_sess and cur_sess.get("email_address") == email_address:
                update_session_after_restore(
                    telegram_user_id=user_id,
                    new_session_id=restored["session_id"],
                    new_address_id=restored.get("address_id"),
                    new_restore_key=restored.get("restore_key"),
                )
        await loop.run_in_executor(None, _persist)
        logger.info(f"Proactively restored [{email_address}] → session {restored['session_id']}")


# ── Background loops (mirrors job_queue in Email11 main.py) ───────────────────
async def _poll_all_loop():
    """Polls all history entries every POLL_INTERVAL seconds."""
    while True:
        try:
            loop    = asyncio.get_running_loop()
            entries = await loop.run_in_executor(None, get_all_history_entries)
            if entries:
                await asyncio.gather(
                    *[_poll_one(e) for e in entries],
                    return_exceptions=True)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.warning(f"_poll_all_loop: {e}")
        await asyncio.sleep(POLL_INTERVAL)


async def _restore_all_loop():
    """Proactively restores all sessions every RESTORE_INTERVAL seconds."""
    await asyncio.sleep(30)  # initial delay, same as Email11
    while True:
        try:
            loop    = asyncio.get_running_loop()
            entries = await loop.run_in_executor(None, get_all_history_entries)
            if entries:
                await asyncio.gather(
                    *[_restore_one(e) for e in entries],
                    return_exceptions=True)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.warning(f"_restore_all_loop: {e}")
        await asyncio.sleep(RESTORE_INTERVAL)


# ── Init (call after app.start()) ─────────────────────────────────────────────
def init_email_module(app_instance: Client):
    global _app
    _app = app_instance
    loop = asyncio.get_event_loop()
    init_db()
    loop.create_task(_poll_all_loop())
    loop.create_task(_restore_all_loop())
    logger.info("Email module initialized")
