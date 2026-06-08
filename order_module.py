#!/usr/bin/env python3
# order_module.py — Account Ordering System (ported from TelegramOrderBot to Pyrogram)
import asyncio
import html
import io
import json
import logging
import os
import re
import threading
import time
import datetime
import hashlib
from typing import Optional
from urllib.parse import quote as url_quote

import httpx
import psycopg2
import psycopg2.extras

from pyrogram.types import (
    InlineKeyboardMarkup, InlineKeyboardButton,
    Message, CallbackQuery
)
from pyrogram.enums import ParseMode

logger = logging.getLogger(__name__)

# ─── Config ───────────────────────────────────────────────────────────────────
KHPAY_API_KEY   = os.environ.get("KHPAY_API_KEY", "")
KHPAY_BASE_URL  = "https://khpay.site/api/v1"
POLL_INTERVAL   = 10
POLL_COUNT      = 6
DATABASE_URL    = os.environ.get("DATABASE_URL", "")
ADMIN_ID: int   = int(os.environ.get("ADMIN_ID", "0"))

# Runtime globals (loaded from DB after init)
CHANNEL_ID       = os.environ.get("TELEGRAM_CHANNEL_ID", "").strip()
PAYMENT_NAME     = "RADY"
MAINTENANCE_MODE = False
EXTRA_ADMIN_IDS: set = set()

_data_lock   = threading.RLock()
_polls_lock  = threading.Lock()
_active_polls: set = set()
_admin_order_msg: dict = {}      # {user_id: message_id} — settings panel msg

order_sessions: dict  = {}       # {user_id: {state, ...}}
accounts_data: dict   = {"accounts": [], "account_types": {}, "prices": {}}
_data_loaded_ok       = False
_notified_users: set  = set()
_notified_lock        = threading.Lock()

_app = None   # set by init_order_module()


def is_admin(uid) -> bool:
    try:
        uid_int = int(uid)
    except (TypeError, ValueError):
        return False
    return uid_int == ADMIN_ID or uid_int in EXTRA_ADMIN_IDS


# ─── DB helpers ───────────────────────────────────────────────────────────────
def _db_conn():
    return psycopg2.connect(DATABASE_URL, cursor_factory=psycopg2.extras.RealDictCursor)


def _db_execute(query: str, params=None) -> int:
    conn = _db_conn()
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute(query, params or [])
                return getattr(cur, 'rowcount', 0) or 0
    finally:
        conn.close()


def _db_query(query: str, params=None) -> list:
    conn = _db_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(query, params or [])
            rows = cur.fetchall()
            return [dict(r) for r in rows] if rows else []
    finally:
        conn.close()


async def _aexec(query: str, params=None) -> int:
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, _db_execute, query, params)


async def _aquery(query: str, params=None) -> list:
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, _db_query, query, params)


# ─── DB init ──────────────────────────────────────────────────────────────────
def _init_db_sync():
    global PAYMENT_NAME, MAINTENANCE_MODE, EXTRA_ADMIN_IDS, CHANNEL_ID
    conn = _db_conn()
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute("""CREATE TABLE IF NOT EXISTS order_accounts (
                    id SERIAL PRIMARY KEY,
                    data JSONB NOT NULL DEFAULT '{}'
                )""")
                cur.execute("""CREATE TABLE IF NOT EXISTS order_sessions_store (
                    id SERIAL PRIMARY KEY,
                    data JSONB NOT NULL DEFAULT '{}'
                )""")
                cur.execute("""CREATE TABLE IF NOT EXISTS order_donations (
                    id SERIAL PRIMARY KEY,
                    user_id BIGINT NOT NULL,
                    first_name TEXT DEFAULT '',
                    last_name TEXT DEFAULT '',
                    username TEXT DEFAULT '',
                    amount NUMERIC NOT NULL,
                    txn_id TEXT DEFAULT '',
                    donated_at TIMESTAMPTZ DEFAULT NOW()
                )""")
                cur.execute("""CREATE TABLE IF NOT EXISTS order_pending_payments (
                    user_id BIGINT PRIMARY KEY,
                    chat_id BIGINT NOT NULL,
                    account_type TEXT,
                    quantity INT,
                    total_price NUMERIC,
                    txn_id TEXT,
                    qr_message_id BIGINT,
                    session_data JSONB DEFAULT '{}',
                    created_at TIMESTAMPTZ DEFAULT NOW()
                )""")
                cur.execute("""CREATE TABLE IF NOT EXISTS order_purchase_history (
                    id SERIAL PRIMARY KEY,
                    user_id BIGINT NOT NULL,
                    account_type TEXT,
                    quantity INT,
                    total_price NUMERIC,
                    accounts JSONB DEFAULT '[]',
                    purchased_at TIMESTAMPTZ DEFAULT NOW()
                )""")
                cur.execute("""CREATE TABLE IF NOT EXISTS order_known_users (
                    user_id BIGINT PRIMARY KEY,
                    first_name TEXT DEFAULT '',
                    last_name TEXT DEFAULT '',
                    username TEXT DEFAULT '',
                    first_seen TIMESTAMPTZ DEFAULT NOW(),
                    last_seen TIMESTAMPTZ DEFAULT NOW(),
                    admin_notified INT DEFAULT 0
                )""")
                cur.execute("""CREATE TABLE IF NOT EXISTS order_email_buyer_map (
                    email TEXT PRIMARY KEY,
                    user_id BIGINT NOT NULL,
                    account_type TEXT,
                    purchased_at TIMESTAMPTZ DEFAULT NOW()
                )""")
                cur.execute("""CREATE TABLE IF NOT EXISTS order_settings (
                    key TEXT PRIMARY KEY,
                    value TEXT
                )""")
                cur.execute("""CREATE TABLE IF NOT EXISTS order_sent_verifications (
                    email TEXT NOT NULL,
                    code TEXT NOT NULL,
                    first_sent_at TIMESTAMPTZ DEFAULT NOW(),
                    PRIMARY KEY (email, code)
                )""")
                # Seed empty rows
                cur.execute("SELECT COUNT(*) AS cnt FROM order_accounts")
                if (cur.fetchone() or {}).get('cnt', 0) == 0:
                    cur.execute("INSERT INTO order_accounts (data) VALUES (%s)",
                                [json.dumps({"accounts": [], "account_types": {}, "prices": {}})])
                cur.execute("SELECT COUNT(*) AS cnt FROM order_sessions_store")
                if (cur.fetchone() or {}).get('cnt', 0) == 0:
                    cur.execute("INSERT INTO order_sessions_store (data) VALUES (%s)", [json.dumps({})])
        logger.info("Order DB tables ready")
    except Exception as e:
        logger.error(f"DB init failed: {e}")
    finally:
        conn.close()
    # Restore settings
    try:
        rows = _db_query("SELECT key, value FROM order_settings")
        settings = {r['key']: r['value'] for r in rows}
        if settings.get('PAYMENT_NAME'):
            PAYMENT_NAME = settings['PAYMENT_NAME']
        if settings.get('MAINTENANCE_MODE'):
            MAINTENANCE_MODE = settings['MAINTENANCE_MODE'].lower() == 'true'
        if settings.get('EXTRA_ADMIN_IDS'):
            EXTRA_ADMIN_IDS = set(int(x) for x in json.loads(settings['EXTRA_ADMIN_IDS']) if x)
        if settings.get('TELEGRAM_CHANNEL_ID'):
            CHANNEL_ID = settings['TELEGRAM_CHANNEL_ID'].strip()
        logger.info("Order settings restored from DB")
    except Exception as e:
        logger.error(f"Settings restore failed: {e}")


def _get_setting(key, default=None):
    try:
        rows = _db_query("SELECT value FROM order_settings WHERE key = %s", [key])
        if rows:
            return rows[0]['value']
    except Exception as e:
        logger.error(f"get_setting {key}: {e}")
    return default


def _set_setting(key, value):
    try:
        _db_execute("""INSERT INTO order_settings (key, value) VALUES (%s, %s)
            ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value""",
            [key, str(value)])
    except Exception as e:
        logger.error(f"set_setting {key}: {e}")


def _load_data_sync():
    global accounts_data, _data_loaded_ok
    try:
        rows = _db_query("SELECT data FROM order_accounts LIMIT 1")
        if rows:
            data = rows[0]['data']
            if isinstance(data, str):
                data = json.loads(data)
            with _data_lock:
                accounts_data.update(data)
            _data_loaded_ok = True
            logger.info("Accounts data loaded from DB")
            return
    except Exception as e:
        logger.error(f"load_data failed: {e}")
    _data_loaded_ok = False


def _save_data_sync():
    try:
        with _data_lock:
            payload = json.dumps(accounts_data, ensure_ascii=False)
        _db_execute("UPDATE order_accounts SET data = %s", [payload])
    except Exception as e:
        logger.error(f"save_data failed: {e}")


def _load_sessions_sync():
    global order_sessions
    try:
        rows = _db_query("SELECT data FROM order_sessions_store LIMIT 1")
        if rows:
            data = rows[0]['data']
            if isinstance(data, str):
                data = json.loads(data)
            order_sessions = {int(k): v for k, v in data.items()}
            logger.info(f"Order sessions loaded: {len(order_sessions)}")
    except Exception as e:
        logger.error(f"load_sessions failed: {e}")


def _save_sessions_sync():
    try:
        with _data_lock:
            payload = json.dumps({str(k): v for k, v in order_sessions.items()}, ensure_ascii=False)
        _db_execute("UPDATE order_sessions_store SET data = %s", [payload])
    except Exception as e:
        logger.error(f"save_sessions failed: {e}")


def _save_sessions_bg():
    threading.Thread(target=_save_sessions_sync, daemon=True).start()


def _save_data_bg():
    threading.Thread(target=_save_data_sync, daemon=True).start()


# ─── Pending payments DB ──────────────────────────────────────────────────────
def _save_pending_payment_sync(user_id, chat_id, session):
    try:
        _db_execute("""INSERT INTO order_pending_payments
            (user_id, chat_id, account_type, quantity, total_price, txn_id, qr_message_id, session_data)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
            ON CONFLICT (user_id) DO UPDATE SET
                chat_id=EXCLUDED.chat_id, account_type=EXCLUDED.account_type,
                quantity=EXCLUDED.quantity, total_price=EXCLUDED.total_price,
                txn_id=EXCLUDED.txn_id, qr_message_id=EXCLUDED.qr_message_id,
                session_data=EXCLUDED.session_data, created_at=NOW()""",
            [str(user_id), str(chat_id),
             session.get('account_type'), str(session.get('quantity', 1)),
             str(session.get('total_price', 0)), session.get('txn_id', ''),
             str(session.get('qr_message_id', 0)),
             json.dumps(session, ensure_ascii=False)])
    except Exception as e:
        logger.error(f"save_pending_payment: {e}")


def _get_pending_payment_sync(user_id):
    try:
        rows = _db_query("SELECT * FROM order_pending_payments WHERE user_id = %s", [str(user_id)])
        if rows:
            r = rows[0]
            sess = r.get('session_data') or {}
            if isinstance(sess, str):
                try:
                    sess = json.loads(sess)
                except Exception:
                    sess = {}
            sess['state'] = 'payment_pending'
            sess['account_type'] = r.get('account_type') or sess.get('account_type')
            sess['quantity']     = int(r.get('quantity') or sess.get('quantity') or 1)
            sess['total_price']  = float(r.get('total_price') or sess.get('total_price') or 0)
            sess['txn_id']       = r.get('txn_id') or sess.get('txn_id')
            sess['qr_message_id']= int(r.get('qr_message_id') or 0)
            sess['chat_id']      = int(r.get('chat_id') or 0)
            return sess
    except Exception as e:
        logger.error(f"get_pending_payment: {e}")
    return None


def _delete_pending_payment_sync(user_id):
    try:
        _db_execute("DELETE FROM order_pending_payments WHERE user_id = %s", [str(user_id)])
    except Exception as e:
        logger.error(f"delete_pending_payment: {e}")


# ─── Donation DB helpers ──────────────────────────────────────────────────────
def _save_donation_sync(user_id, first_name, last_name, username, amount, txn_id=''):
    try:
        _db_execute("""INSERT INTO order_donations
            (user_id, first_name, last_name, username, amount, txn_id)
            VALUES (%s,%s,%s,%s,%s,%s)""",
            [str(user_id), first_name or '', last_name or '', username or '',
             str(amount), txn_id or ''])
    except Exception as e:
        logger.error(f"save_donation: {e}")


def _get_top_donors_sync(limit=10):
    try:
        return _db_query("""
            SELECT user_id, first_name, last_name, username,
                   SUM(amount) AS total, COUNT(*) AS times
            FROM order_donations
            GROUP BY user_id, first_name, last_name, username
            ORDER BY total DESC LIMIT %s""", [str(limit)])
    except Exception as e:
        logger.error(f"get_top_donors: {e}")
    return []


def _get_user_donation_total_sync(user_id):
    try:
        rows = _db_query("""SELECT COALESCE(SUM(amount),0) AS total, COUNT(*) AS times
            FROM order_donations WHERE user_id=%s""", [str(user_id)])
        if rows:
            return float(rows[0].get('total') or 0), int(rows[0].get('times') or 0)
    except Exception as e:
        logger.error(f"get_user_donation_total: {e}")
    return 0.0, 0


# ─── Purchase history DB ──────────────────────────────────────────────────────
def _save_purchase_history_sync(user_id, account_type, quantity, total_price, accs=None):
    try:
        accs_json = json.dumps(accs or [], ensure_ascii=False)
        _db_execute("""INSERT INTO order_purchase_history
            (user_id, account_type, quantity, total_price, accounts)
            VALUES (%s,%s,%s,%s,%s)""",
            [str(user_id), account_type, str(quantity), str(total_price), accs_json])
        for acc in (accs or []):
            if isinstance(acc, dict) and acc.get('email'):
                try:
                    _db_execute("""INSERT INTO order_email_buyer_map (email, user_id, account_type)
                        VALUES (%s,%s,%s)
                        ON CONFLICT (email) DO UPDATE
                            SET user_id=EXCLUDED.user_id, account_type=EXCLUDED.account_type,
                                purchased_at=NOW()""",
                        [str(acc['email']).strip().lower(), str(user_id), account_type])
                except Exception:
                    pass
    except Exception as e:
        logger.error(f"save_purchase_history: {e}")


def _get_purchase_history_sync(user_id, limit=10):
    try:
        return _db_query("""SELECT account_type, quantity, total_price, accounts, purchased_at
            FROM order_purchase_history WHERE user_id = %s
            ORDER BY purchased_at DESC LIMIT %s""",
            [str(user_id), str(limit)])
    except Exception as e:
        logger.error(f"get_purchase_history: {e}")
    return []


# ─── Known users DB ───────────────────────────────────────────────────────────
def _upsert_known_user_sync(user_id, first_name, last_name, username):
    try:
        _db_execute("""INSERT INTO order_known_users
            (user_id, first_name, last_name, username, first_seen, last_seen, admin_notified)
            VALUES (%s,%s,%s,%s,NOW(),NOW(),1)
            ON CONFLICT (user_id) DO UPDATE SET
                first_name=EXCLUDED.first_name, last_name=EXCLUDED.last_name,
                username=EXCLUDED.username, last_seen=NOW(), admin_notified=1""",
            [str(user_id), first_name or '', last_name or '', username or ''])
    except Exception as e:
        logger.error(f"upsert_known_user: {e}")


def _is_admin_notified(uid):
    with _notified_lock:
        if uid in _notified_users:
            return True
    try:
        rows = _db_query(
            "SELECT admin_notified FROM order_known_users WHERE user_id = %s", [str(uid)])
        if rows and rows[0].get('admin_notified'):
            with _notified_lock:
                _notified_users.add(uid)
            return True
    except Exception:
        pass
    return False


# ─── E-GetS buyer lookup ──────────────────────────────────────────────────────
def _find_buyers_by_email_sync(email: str) -> list:
    email = email.strip().lower()
    if not email:
        return []
    buyers = []
    seen   = set()
    try:
        rows = _db_query(
            "SELECT user_id FROM order_email_buyer_map WHERE email = %s LIMIT 1", [email])
        for r in rows:
            uid = int(r['user_id'])
            if uid not in seen:
                seen.add(uid)
                buyers.append(uid)
    except Exception as e:
        logger.warning(f"email_buyer_map lookup: {e}")
    if buyers:
        return buyers
    try:
        rows = _db_query("""SELECT user_id FROM order_purchase_history
            WHERE accounts::text ILIKE %s ORDER BY purchased_at DESC""",
            [f'%{email}%'])
        for r in rows:
            uid = int(r['user_id'])
            if uid not in seen:
                seen.add(uid)
                buyers.append(uid)
    except Exception as e:
        logger.error(f"buyer ILIKE scan: {e}")
    if buyers:
        try:
            _db_execute("""INSERT INTO order_email_buyer_map (email, user_id, purchased_at)
                VALUES (%s,%s,NOW()) ON CONFLICT (email) DO UPDATE SET user_id=EXCLUDED.user_id""",
                [email, str(buyers[0])])
        except Exception:
            pass
    return buyers


# ─── Keyboard helpers ─────────────────────────────────────────────────────────
def _ikb(text, cb):
    return InlineKeyboardButton(text, callback_data=cb)


def _back_btn(cb='o:home'):
    return InlineKeyboardButton('◀️ ត្រឡប់', callback_data=cb,
                                icon_custom_emoji_id='5877629862306385808')


def _settings_main_ikb():
    return InlineKeyboardMarkup([
        [_ikb('➕ បន្ថែម Account', 's:add_acc'),  _ikb('🗑 លុបប្រភេទ', 's:del_type')],
        [_ikb('📋 របាយការណ៍ទិញ', 's:buyers'),    _ikb('👥 អ្នកប្រើ', 's:users')],
        [_ikb('💳 Payment Name', 's:pay'),          _ikb('📢 Channel ID', 's:ch')],
        [_ikb('🔑 Bakong Token', 's:bak'),           _ikb('👑 Admins', 's:adm')],
        [_ikb('🛠 Maintenance', 's:mnt'),            _ikb('📢 Broadcast', 's:broadcast')],
        [_ikb('✖️ បិទ', 's:close')],
    ])


def _settings_cancel_ikb():
    return InlineKeyboardMarkup([[_ikb('🚫 បោះបង់', 's:cancel_input')]])


def _settings_pay_ikb():
    return InlineKeyboardMarkup([
        [_ikb('✏️ ប្តូរ Payment Name', 's:pay_edit')],
        [_back_btn('s:main')],
    ])


def _settings_bak_ikb():
    return InlineKeyboardMarkup([
        [_ikb('✏️ ប្តូរ Bakong Token', 's:bak_edit')],
        [_back_btn('s:main')],
    ])


def _settings_ch_ikb():
    return InlineKeyboardMarkup([
        [_ikb('✏️ ប្តូរ Channel ID', 's:ch_edit'), _ikb('🗑 លុប', 's:ch_clear')],
        [_back_btn('s:main')],
    ])


def _settings_adm_ikb():
    rows = []
    for uid in sorted(EXTRA_ADMIN_IDS):
        rows.append([_ikb(f'👤 {uid}', 's:adm_noop'), _ikb('🗑 លុប', f's:adm_del:{uid}')])
    rows.append([_ikb('➕ បន្ថែម Admin', 's:adm_add')])
    rows.append([_back_btn('s:main')])
    return InlineKeyboardMarkup(rows)


def _settings_mnt_ikb():
    btn = ('🟢 បើក Bot', 's:mnt_off') if MAINTENANCE_MODE else ('🔴 បិទ Bot', 's:mnt_on')
    return InlineKeyboardMarkup([[_ikb(btn[0], btn[1])], [_back_btn('s:main')]])


def _check_payment_ikb():
    return InlineKeyboardMarkup([[_ikb('🚫 បោះបង់', 'cancel_purchase')]])


def _broadcast_confirm_ikb():
    return InlineKeyboardMarkup([[
        _ikb('✅ បញ្ជាក់ផ្សាយ', 's:broadcast_confirm'),
        _ikb('🚫 បោះបង់', 's:broadcast_cancel'),
    ]])


def _type_cb_id(account_type: str) -> str:
    return hashlib.sha1(account_type.encode()).hexdigest()[:12]


def _type_from_cb_id(cb_id: str) -> Optional[str]:
    with _data_lock:
        for t in accounts_data.get('account_types', {}):
            if _type_cb_id(t) == cb_id:
                return t
    return None


def _short_label(text, limit=36) -> str:
    clean = " ".join(str(text).split())
    return clean if len(clean) <= limit else clean[:limit - 1] + "…"


# ─── Pyrogram send helpers ────────────────────────────────────────────────────
async def _send(chat_id, text, *, kb=None, **kw):
    return await _app.send_message(chat_id, text, parse_mode=ParseMode.HTML,
                                   reply_markup=kb, **kw)


async def _edit_msg(chat_id, mid, text, *, kb=None):
    try:
        await _app.edit_message_text(chat_id, mid, text,
                                     parse_mode=ParseMode.HTML, reply_markup=kb)
        return True
    except Exception:
        return False


async def _del_msg(chat_id, mid):
    if not mid:
        return
    try:
        await _app.delete_messages(chat_id, mid)
    except Exception:
        pass


# ─── Admin settings panel helpers ─────────────────────────────────────────────
async def _settings_edit(chat_id, user_id, text, kb):
    mid = _admin_order_msg.get(user_id)
    if mid:
        ok = await _edit_msg(chat_id, mid, text, kb=kb)
        if ok:
            return
    msg = await _send(chat_id, text, kb=kb)
    if msg:
        _admin_order_msg[user_id] = msg.id


async def _settings_main_text():
    return "<b>⚙️ ការកំណត់ Admin</b>\n\nសូមជ្រើសរើសប្រតិបត្តិការ៖"


async def send_admin_settings(client, chat_id, user_id):
    text = "<b>⚙️ ការកំណត់ Admin</b>\n\nសូមជ្រើសរើសប្រតិបត្តិការ៖"
    msg = await _send(chat_id, text, kb=_settings_main_ikb())
    if msg:
        _admin_order_msg[user_id] = msg.id


async def _prompt_admin_input(chat_id, user_id, key, prompt, return_menu='main'):
    with _data_lock:
        order_sessions[user_id] = {'state': f'admin_input:{key}', 'settings_return': return_menu}
    _save_sessions_bg()
    await _settings_edit(chat_id, user_id,
        prompt + "\n\n<i>ចុច 🚫 បោះបង់ ដើម្បីបោះបង់</i>",
        _settings_cancel_ikb())


async def _show_pay_panel(chat_id, user_id):
    text = f"💳 <b>Payment Name បច្ចុប្បន្ន:</b>\n<code>{html.escape(PAYMENT_NAME or '(មិនទាន់កំណត់)')}</code>"
    await _settings_edit(chat_id, user_id, text, _settings_pay_ikb())


async def _show_bak_panel(chat_id, user_id):
    text = "🔑 <b>Bakong Token:</b>\n<i>(ប្រើ KHPAY API ដោយស្វ័យប្រវត្តិ)</i>"
    await _settings_edit(chat_id, user_id, text, _settings_bak_ikb())


async def _show_ch_panel(chat_id, user_id):
    cur = CHANNEL_ID if CHANNEL_ID else "(មិនទាន់កំណត់)"
    text = f"📢 <b>Channel ID បច្ចុប្បន្ន:</b>\n<code>{html.escape(str(cur))}</code>"
    await _settings_edit(chat_id, user_id, text, _settings_ch_ikb())


async def _show_adm_panel(chat_id, user_id):
    extras = sorted(EXTRA_ADMIN_IDS)
    extras_str = "\n".join(f"• <code>{x}</code>" for x in extras) if extras else "(គ្មាន)"
    text = (f"👑 <b>Admin បឋម:</b> <code>{ADMIN_ID}</code>\n\n"
            f"➕ <b>Admin បន្ថែម:</b>\n{extras_str}")
    await _settings_edit(chat_id, user_id, text, _settings_adm_ikb())


async def _show_mnt_panel(chat_id, user_id):
    status = "🔴 បិទ (Maintenance ON)" if MAINTENANCE_MODE else "🟢 បើក (ធម្មតា)"
    text = f"🛠 <b>ស្ថានភាព Bot:</b> {status}"
    await _settings_edit(chat_id, user_id, text, _settings_mnt_ikb())


async def _show_del_type_panel(chat_id, user_id):
    with _data_lock:
        types = [t for t, accs in accounts_data.get('account_types', {}).items() if len(accs) > 0]
    if not types:
        await _settings_edit(chat_id, user_id, "⚠️ <b>មិនមានប្រភេទ Account ណាមួយ!</b>",
            InlineKeyboardMarkup([[_back_btn('s:main')]]))
        return
    rows = []
    for i in range(0, len(types), 2):
        row = []
        for t in types[i:i+2]:
            count = len(accounts_data['account_types'].get(t, []))
            price = accounts_data.get('prices', {}).get(t, 0)
            label = f"{_short_label(t)} ({count}·${price})"
            row.append(_ikb(label, f"dts:{_type_cb_id(t)}"))
        rows.append(row)
    rows.append([_back_btn('s:main')])
    await _settings_edit(chat_id, user_id, "🗑 <b>ជ្រើសរើសប្រភេទ Account ដែលចង់លុប:</b>",
        InlineKeyboardMarkup(rows))


async def _show_users_panel(chat_id):
    loop = asyncio.get_running_loop()
    rows = await loop.run_in_executor(None, _db_query,
        "SELECT user_id, first_name, last_name, username, first_seen FROM order_known_users ORDER BY first_seen DESC")
    if not rows:
        await _send(chat_id, "📭 <b>មិនមានអ្នកប្រើប្រាស់ណាមួយ!</b>")
        return
    total = len(rows)
    lines = [f"👥 អ្នកប្រើប្រាស់សរុប: {total}", ""]
    for i, r in enumerate(rows, 1):
        first = r.get('first_name') or ''; last = r.get('last_name') or ''
        full  = f"{first} {last}".strip() or 'N/A'
        uname = r.get('username') or ''; uid = r.get('user_id')
        lines += [f"{i}. {full}", f"   🔖 @{uname}" if uname else "   🔖 —", f"   🪪 {uid}", ""]
    txt = "\n".join(lines).encode('utf-8')
    fname = f"users_{datetime.datetime.utcnow().strftime('%Y%m%d_%H%M%S')}.txt"
    buf = io.BytesIO(txt); buf.name = fname
    try:
        await _app.send_document(chat_id, buf, caption=f"👥 អ្នកប្រើប្រាស់ — {total} នាក់",
                                  file_name=fname)
    except Exception as e:
        logger.error(f"send users doc: {e}")
        await _send(chat_id, f"❌ <b>Error:</b> <code>{html.escape(str(e))}</code>")


async def _export_buyers_panel(chat_id):
    loop = asyncio.get_running_loop()
    rows = await loop.run_in_executor(None, _db_query, """
        SELECT ph.user_id, ph.account_type, ph.accounts,
               ku.first_name, ku.last_name, ku.username
        FROM order_purchase_history ph
        LEFT JOIN order_known_users ku ON ku.user_id = ph.user_id
        ORDER BY ph.user_id, ph.purchased_at DESC""")
    if not rows:
        await _send(chat_id, "មិនមានទិន្នន័យ​ទិញ​ណាមួយ​ទេ​")
        return
    grouped = {}
    for r in rows:
        uid = str(r.get('user_id'))
        grouped.setdefault(uid, {'first_name': r.get('first_name') or '',
                                  'last_name':  r.get('last_name') or '',
                                  'username':   r.get('username') or '',
                                  'emails': []})
        accs = r.get('accounts') or []
        if isinstance(accs, str):
            try: accs = json.loads(accs)
            except Exception: accs = []
        for a in accs:
            if isinstance(a, dict) and a.get('email'):
                em = str(a['email'])
                if em.lower() not in [x.lower() for x in grouped[uid]['emails']]:
                    grouped[uid]['emails'].append(em)
    lines = []
    total_coupons = 0
    for uid, info in grouped.items():
        full = (info['first_name'] + ' ' + info['last_name']).strip() or '(no name)'
        lines += [f"ឈ្មោះ : {full}", f"ID    : {uid}", ""]
        if info['emails']:
            lines += info['emails']
            total_coupons += len(info['emails'])
        else:
            lines.append("—")
        lines.append("")
    txt = "\n".join(lines).encode('utf-8')
    fname = f"buyers_{datetime.datetime.utcnow().strftime('%Y%m%d_%H%M%S')}.txt"
    buf = io.BytesIO(txt); buf.name = fname
    try:
        await _app.send_document(chat_id, buf,
            caption=f"📋 របាយការណ៍ទិញ — {len(grouped)} នាក់, {total_coupons} គូប៉ុង",
            file_name=fname)
    except Exception as e:
        logger.error(f"send buyers doc: {e}")
        await _send(chat_id, f"❌ <b>Error:</b> <code>{html.escape(str(e))}</code>")


async def _start_add_account_flow(chat_id, user_id):
    with _data_lock:
        order_sessions[user_id] = {'state': 'waiting_for_accounts'}
    _save_sessions_bg()
    await _settings_edit(chat_id, user_id,
        "➕ <b>បន្ថែម Account</b>\n\n"
        "📧 ផ្ញើ Email ម្តងមួយបន្ទាត់:\n\n"
        "<code>example@gmail.com\ntest123@yahoo.com</code>\n\n"
        "<i>ចុច 🚫 បោះបង់ ដើម្បីបោះបង់</i>",
        _settings_cancel_ikb())


# ─── Donate UI ────────────────────────────────────────────────────────────────
DONATE_PRESETS = [1, 2, 5, 10, 20, 50]
MEDALS = ['🥇', '🥈', '🥉', '4️⃣', '5️⃣', '6️⃣', '7️⃣', '8️⃣', '9️⃣', '🔟']


def _donate_ikb():
    rows = []
    for i in range(0, len(DONATE_PRESETS), 3):
        row = [_ikb(f'${amt}', f'don:{amt}') for amt in DONATE_PRESETS[i:i+3]]
        rows.append(row)
    rows.append([_ikb('✏️ ចំនួនផ្ទាល់ខ្លួន', 'don_custom')])
    rows.append([_ikb('🏆 Top Donation', 'don_top')])
    rows.append([InlineKeyboardButton('🏠 ម៉ឺនុយមេ', callback_data='home',
                                      icon_custom_emoji_id='5282843764451195532')])
    return InlineKeyboardMarkup(rows)


def _donate_cancel_ikb():
    return InlineKeyboardMarkup([
        [_ikb('🚫 បោះបង់', 'don_cancel')],
    ])


async def send_donate_menu(chat_id, user_id):
    loop = asyncio.get_running_loop()
    total, times = await loop.run_in_executor(None, _get_user_donation_total_sync, user_id)
    my_line = ''
    if total > 0:
        my_line = f'\n\n💝 <b>អ្នកបានបរិច្ចាគ:</b> <b>${total:.2f}</b> ({times} ដង)'
    text = (
        '💝 <b>Donate តាម KHPay (Bakong QR)</b>\n\n'
        'ការបរិច្ចាគរបស់អ្នកជួយឱ្យ Bot នេះបន្តដំណើរការ 🙏\n\n'
        '💵 <b>ជ្រើសរើសចំនួន (USD):</b>' + my_line
    )
    await _send(chat_id, text, kb=_donate_ikb())


async def _format_top_donors() -> str:
    loop = asyncio.get_running_loop()
    rows = await loop.run_in_executor(None, _get_top_donors_sync, 10)
    if not rows:
        return '🏆 <b>Top Donation</b>\n\n<i>មិនទាន់មានអ្នកបរិច្ចាគ</i>'
    lines = ['🏆 <b>Top Donation</b>\n']
    for i, r in enumerate(rows):
        medal = MEDALS[i] if i < len(MEDALS) else f'{i+1}.'
        first = r.get('first_name') or ''; last = r.get('last_name') or ''
        full  = html.escape((f'{first} {last}').strip() or 'Anonymous')
        uname = r.get('username') or ''
        uname_str = f' (@{html.escape(uname)})' if uname else ''
        total = float(r.get('total') or 0)
        times = int(r.get('times') or 0)
        lines.append(f'{medal} <b>{full}</b>{uname_str}\n   💵 ${total:.2f}  ·  {times} ដង\n')
    return '\n'.join(lines)


async def _confirm_donation(client, chat_id, user_id, amount, session, payment_data=None):
    """Called when donation payment confirmed."""
    user_info = session.get('user_info', {})
    first = user_info.get('first_name', '')
    last  = user_info.get('last_name', '')
    uname = user_info.get('username', '')
    txn   = session.get('txn_id', '')
    loop  = asyncio.get_running_loop()
    await loop.run_in_executor(None, _save_donation_sync,
                               user_id, first, last, uname, amount, txn)
    total, times = await loop.run_in_executor(None, _get_user_donation_total_sync, user_id)
    with _data_lock:
        order_sessions.pop(user_id, None)
    _save_sessions_bg()
    full = f'{first} {last}'.strip() or 'Anonymous'
    thank_text = (
        f'🎉 <b>អរគុណខ្លាំងណាស់!</b>\n\n'
        f'💝 <b>{html.escape(full)}</b>\n'
        f'បានបរិច្ចាគ <b>${amount:.2f}</b> ជូន RADY Bot! 🙏\n\n'
        f'💵 <b>សរុបរបស់អ្នក:</b> ${total:.2f} ({times} ដង)'
    )
    kb = InlineKeyboardMarkup([
        [_ikb('🏆 Top Donation', 'don_top')],
        [InlineKeyboardButton('🏠 ម៉ឺនុយមេ', callback_data='home',
                              icon_custom_emoji_id='5282843764451195532')],
    ])
    await _send(chat_id, thank_text, kb=kb)
    # Notify admin
    try:
        uname_str = f'@{uname}' if uname else '—'
        notif = (
            f'💝 <b>Donation បានទទួល!</b>\n'
            f'━━━━━━━━━━━━━━━━━━━\n'
            f'👤 {html.escape(full)} ({uname_str})\n'
            f'🪪 ID: <code>{user_id}</code>\n'
            f'💵 ចំនួន: <b>${amount:.2f}</b>\n'
            f'💎 សរុប: ${total:.2f} ({times} ដង)'
        )
        await _send(ADMIN_ID, notif)
    except Exception as e:
        logger.error(f"donate admin notif: {e}")


async def _poll_donation(client, user_id, chat_id, txn_id, amount, qr_msg_id, session):
    with _polls_lock:
        key = f'don_{user_id}'
        if key in _active_polls:
            return
        _active_polls.add(key)
    status_msg_id = None
    try:
        for i in range(POLL_COUNT):
            await asyncio.sleep(POLL_INTERVAL)
            with _data_lock:
                sess = order_sessions.get(user_id)
            if not sess or sess.get('state') != 'donation_pending':
                return
            try:
                is_paid, pd = await check_payment_status(txn_id)
                if is_paid:
                    with _polls_lock:
                        _active_polls.discard(key)
                    if status_msg_id:
                        await _del_msg(chat_id, status_msg_id)
                    await _del_msg(chat_id, qr_msg_id)
                    await _confirm_donation(client, chat_id, user_id, amount, session, pd)
                    return
            except Exception as e:
                logger.error(f"poll_donation check: {e}")
            if i < POLL_COUNT - 1:
                if status_msg_id:
                    await _del_msg(chat_id, status_msg_id)
                m = await _send(chat_id, '🔍 រង់ចាំការ Donate...')
                if m:
                    status_msg_id = m.id
        # Timed out
        if status_msg_id:
            await _del_msg(chat_id, status_msg_id)
        await _del_msg(chat_id, qr_msg_id)
        with _data_lock:
            sess = order_sessions.get(user_id)
        if sess and sess.get('state') == 'donation_pending':
            with _data_lock:
                order_sessions.pop(user_id, None)
            _save_sessions_bg()
            await send_donate_menu(chat_id, user_id)
    finally:
        with _polls_lock:
            _active_polls.discard(f'don_{user_id}')


async def _generate_and_send_donate_qr(client, chat_id, user_id, amount, session):
    try:
        img_bytes, txn_id, qr_string = await generate_payment_qr(amount)
        if not img_bytes:
            err = txn_id or 'Unknown'
            await _send(chat_id, f'❌ <b>មានបញ្ហាបង្កើត QR</b>\n\nព្យាយាមម្ដងទៀត')
            with _data_lock:
                order_sessions.pop(user_id, None)
            _save_sessions_bg()
            return
        session['txn_id']    = txn_id
        session['qr_sent_at'] = time.time()
        buf = io.BytesIO(img_bytes); buf.name = 'donate_qr.png'
        caption = (
            f'💝 <b>Donate ${amount:.2f} USD</b>\n\n'
            f'<i>Scan QR ខាងក្រោម ហើយបង់ប្រាក់</i>\n'
            f'⏳ <b>ផុតកំណត់:</b> {POLL_COUNT * POLL_INTERVAL}s'
        )
        photo_msg = await _app.send_photo(chat_id, buf, caption=caption,
                                          parse_mode=ParseMode.HTML,
                                          reply_markup=_donate_cancel_ikb())
        if photo_msg:
            session['qr_message_id'] = photo_msg.id
        _save_sessions_bg()
        asyncio.create_task(_poll_donation(client, user_id, chat_id, txn_id,
                                           amount, session.get('qr_message_id', 0), session))
    except Exception as e:
        logger.error(f"generate_and_send_donate_qr: {e}")
        await _send(chat_id, '❌ <b>មានបញ្ហាបង្កើត QR</b>\n\nព្យាយាមម្ដងទៀត')
        with _data_lock:
            order_sessions.pop(user_id, None)
        _save_sessions_bg()


# ─── Account selection UI ─────────────────────────────────────────────────────
async def send_account_selection(chat_id):
    with _data_lock:
        types = {t: accs for t, accs in accounts_data.get('account_types', {}).items() if len(accs) > 0}
    if not types:
        await _send(chat_id, "<i>😔 សូមអភ័យទោស! គ្មានទំនិញក្នុងស្តុក</i>")
        return
    rows = []
    for t, accs in types.items():
        count = len(accs)
        price = accounts_data.get('prices', {}).get(t, 0)
        label = f"ទិញ {t} — ស្តុក {count} · ${price}/ខ"
        rows.append([_ikb(label, f"buy:{_type_cb_id(t)}")])
    rows.append([InlineKeyboardButton('🏠 ម៉ឺនុយមេ', callback_data='home',
                                      icon_custom_emoji_id='5282843764451195532')])
    await _send(chat_id, "<b>🛒 សូមជ្រើសរើសគូប៉ុងដើម្បីទិញ:</b>",
                kb=InlineKeyboardMarkup(rows))


# ─── Payment generation ───────────────────────────────────────────────────────
async def generate_payment_qr(amount):
    """Returns (img_bytes, txn_id, qr_string) or (None, error_msg, None)."""
    if not KHPAY_API_KEY:
        return None, "KHPAY_API_KEY មិនទាន់កំណត់", None
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(
                f"{KHPAY_BASE_URL}/bakong/generate",
                json={"amount": amount, "currency": "USD",
                      "note": PAYMENT_NAME or "RADY",
                      "type": "individual", "static": False},
                headers={"Authorization": f"Bearer {KHPAY_API_KEY}",
                         "Content-Type": "application/json"})
            data = resp.json()
        if not data.get("success"):
            msg = data.get("error") or data.get("message") or "KHPAY generate failed"
            logger.error(f"KHPAY generate: {msg}")
            return None, msg, None
        payload = data.get("data", {})
        txn_id  = payload.get("transaction_id", "")
        qr      = payload.get("qr", "")
        logger.info(f"KHPAY QR generated, txn_id={txn_id}")
        img_bytes = None
        try:
            import qrcode as _qrc
            qr_img = _qrc.make(qr)
            buf = io.BytesIO(); qr_img.save(buf, format='PNG')
            img_bytes = buf.getvalue()
        except Exception as e1:
            logger.warning(f"qrcode lib failed: {e1}")
        if not img_bytes:
            try:
                async with httpx.AsyncClient(timeout=10) as hc:
                    r2 = await hc.get(
                        f"https://api.qrserver.com/v1/create-qr-code/?size=500x500&data={url_quote(qr)}")
                    r2.raise_for_status()
                    img_bytes = r2.content
            except Exception as e2:
                msg = f"QR image generation failed: {e2}"
                logger.error(msg)
                return None, msg, None
        return img_bytes, txn_id, qr
    except Exception as e:
        msg = f"Unexpected: {type(e).__name__}: {e}"
        logger.error(f"generate_payment_qr: {msg}")
        return None, msg, None


async def check_payment_status(txn_id):
    """Returns (is_paid: bool, payment_data: dict|None)."""
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(
                f"{KHPAY_BASE_URL}/bakong/check",
                json={"transaction_id": txn_id},
                headers={"Authorization": f"Bearer {KHPAY_API_KEY}",
                         "Content-Type": "application/json"})
            s = resp.json()
        paid_vals = {"paid", "success", "completed", "approved"}
        if s.get("paid") is True: return True, s.get("data") or s
        if str(s.get("status", "")).lower() in paid_vals: return True, s.get("data") or s
        d = s.get("data", {})
        if isinstance(d, dict):
            if d.get("paid") is True: return True, d
            if str(d.get("status", "")).lower() in paid_vals: return True, d
        return False, None
    except Exception as e:
        logger.error(f"check_payment_status: {e}")
    return False, None


def _khpay_is_failed(s: dict):
    fail_vals = {"expired", "failed", "cancelled"}
    if str(s.get("status", "")).lower() in fail_vals:
        return str(s.get("status")).lower()
    d = s.get("data", {})
    if isinstance(d, dict) and str(d.get("status", "")).lower() in fail_vals:
        return str(d.get("status")).lower()
    return None


# ─── Deliver accounts ─────────────────────────────────────────────────────────
async def _deliver_accounts(client, chat_id, user_id, session, payment_data=None, user_name=''):
    account_type = session.get('account_type', '')
    quantity     = session.get('quantity', 1)

    with _data_lock:
        if account_type not in accounts_data.get('account_types', {}):
            await _send(chat_id, f"❌ <b>មានបញ្ហា!</b>\n\nគ្មាន Account ប្រភេទ {account_type}")
            return
        available = accounts_data['account_types'][account_type]
        if len(available) < quantity:
            await _send(chat_id, f"❌ <b>មានបញ្ហា!</b>\n\nសុំទោស! មានត្រឹមតែ {len(available)} Accounts ក្នុងស្តុក")
            return
        delivered = available[:quantity]
        accounts_data['account_types'][account_type] = available[quantity:]
        with _data_lock:
            if user_id in order_sessions:
                del order_sessions[user_id]

    _save_data_bg()
    _save_sessions_bg()

    loop = asyncio.get_running_loop()
    await loop.run_in_executor(None, _save_purchase_history_sync,
                               user_id, account_type, quantity,
                               session.get('total_price', 0), delivered)

    msg  = "🎉 <b>ការទិញបានបញ្ជាក់ដោយជោគជ័យ</b>\n\n"
    msg += "<b>គូប៉ុងរបស់អ្នក:</b>\n\n"
    for acc in delivered:
        if 'email' in acc:
            msg += f"<code>{html.escape(acc['email'])}</code>\n"
        else:
            msg += f"<code>{html.escape(str(acc.get('phone','')))} | {html.escape(str(acc.get('password','')))}</code>\n"
    msg += "\n<i>🙏 សូមអរគុណសម្រាប់ការទិញ!</i>"

    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton('🏠 ម៉ឺនុយមេ', callback_data='home',
                             icon_custom_emoji_id='5282843764451195532')
    ]])
    await _send(chat_id, msg, kb=kb)
    await send_account_selection(chat_id)

    # Notify admin/channel
    try:
        now_kh = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=7)))
        pd = payment_data or {}
        from_acc = pd.get('fromAccountId') or pd.get('hash') or 'N/A'
        memo = pd.get('memo') or 'គ្មាន'
        ref  = pd.get('externalRef') or pd.get('transactionId') or pd.get('md5') or 'N/A'
        admin_msg = (
            "🎉 ទទួលបានការបង់ប្រាក់ជោគជ័យ\n"
            "━━━━━━━━━━━━━━━━━━━\n"
            f"🆔 អ្នកទិញ: {html.escape(user_name or str(user_id))}\n"
            f"💵 ទឹកប្រាក់: ${session.get('total_price', 0)} USD\n"
            f"📦 ប្រភេទ: {html.escape(account_type)} × {quantity}\n"
            f"👤 ពីធនាគារ: {from_acc}\n"
            f"📝 ចំណាំ: {memo}\n"
            f"🧾 លេខយោង: {ref}\n"
            f"⏰ ម៉ោង: {now_kh.strftime('%d/%m/%Y %H:%M')}"
        )
        target = int(CHANNEL_ID) if CHANNEL_ID else ADMIN_ID
        await _send(target, admin_msg)
    except Exception as e:
        logger.error(f"admin notify: {e}")


# ─── Payment polling task ─────────────────────────────────────────────────────
async def _poll_payment(client, user_id, chat_id, txn_id, amount, qr_msg_id):
    with _polls_lock:
        if user_id in _active_polls:
            return
        _active_polls.add(user_id)
    status_msg_id = None
    try:
        for i in range(POLL_COUNT):
            await asyncio.sleep(POLL_INTERVAL)
            with _data_lock:
                sess = order_sessions.get(user_id)
            if not sess or sess.get('state') != 'payment_pending':
                return
            try:
                is_paid, pd = await check_payment_status(txn_id)
                if is_paid:
                    with _polls_lock:
                        _active_polls.discard(user_id)
                    if status_msg_id:
                        await _del_msg(chat_id, status_msg_id)
                    await _del_msg(chat_id, qr_msg_id)
                    with _data_lock:
                        cur_sess = order_sessions.get(user_id)
                    if cur_sess and cur_sess.get('state') == 'payment_pending':
                        await _deliver_accounts(client, chat_id, user_id, cur_sess, pd)
                        loop = asyncio.get_running_loop()
                        await loop.run_in_executor(None, _delete_pending_payment_sync, user_id)
                    return
                fail = _khpay_is_failed({'status': ''})
                async with httpx.AsyncClient(timeout=30) as hc:
                    r2 = await hc.post(f"{KHPAY_BASE_URL}/bakong/check",
                        json={"transaction_id": txn_id},
                        headers={"Authorization": f"Bearer {KHPAY_API_KEY}",
                                 "Content-Type": "application/json"})
                    s2 = r2.json()
                fail = _khpay_is_failed(s2)
                if fail:
                    with _polls_lock:
                        _active_polls.discard(user_id)
                    if status_msg_id:
                        await _del_msg(chat_id, status_msg_id)
                    await _send(chat_id,
                        f"❌ <b>ការបង់ប្រាក់ {fail}</b>\n\nជ្រើសរើស Account ម្ដងទៀត")
                    with _data_lock:
                        order_sessions.pop(user_id, None)
                    loop = asyncio.get_running_loop()
                    await loop.run_in_executor(None, _delete_pending_payment_sync, user_id)
                    _save_sessions_bg()
                    return
            except Exception as e:
                logger.error(f"poll check error: {e}")
            if i < POLL_COUNT - 1:
                if status_msg_id:
                    await _del_msg(chat_id, status_msg_id)
                msg2 = await _send(chat_id, "🔍 រង់ចាំការបង់ប្រាក់...")
                if msg2:
                    status_msg_id = msg2.id
        # Timed out
        if status_msg_id:
            await _del_msg(chat_id, status_msg_id)
        await _del_msg(chat_id, qr_msg_id)
        with _data_lock:
            sess = order_sessions.get(user_id)
        if sess and sess.get('state') == 'payment_pending':
            with _data_lock:
                order_sessions.pop(user_id, None)
            loop = asyncio.get_running_loop()
            await loop.run_in_executor(None, _delete_pending_payment_sync, user_id)
            _save_sessions_bg()
            await send_account_selection(chat_id)
    finally:
        with _polls_lock:
            _active_polls.discard(user_id)


async def _generate_and_send_qr(client, chat_id, user_id, session):
    try:
        img_bytes, txn_id, qr_string = await generate_payment_qr(session['total_price'])
        if not img_bytes:
            err = txn_id or "មិនដឹងមូលហេតុ"
            if user_id == ADMIN_ID:
                await _send(chat_id, f"❌ <b>QR Error (Admin Debug):</b>\n<code>{html.escape(err)}</code>")
            else:
                await _send(chat_id, "❌ <b>មានបញ្ហាក្នុងការបង្កើត QR Code</b>\n\nសូមព្យាយាមម្តងទៀត")
                await _send(ADMIN_ID, f"⚠️ QR Error (user {user_id}):\n<code>{html.escape(err)}</code>")
            with _data_lock:
                order_sessions.pop(user_id, None)
            _save_sessions_bg()
            return
        session['txn_id']      = txn_id
        session['qr_sent_at']  = time.time()
        buf = io.BytesIO(img_bytes); buf.name = "payment_qr.png"
        photo_msg = await _app.send_photo(chat_id, buf, reply_markup=_check_payment_ikb())
        if photo_msg:
            session['qr_message_id'] = photo_msg.id
        _save_sessions_bg()
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, _save_pending_payment_sync, user_id, chat_id, session)
        qr_mid = session.get('qr_message_id', 0)
        asyncio.create_task(_poll_payment(client, user_id, chat_id, txn_id,
                                          session['total_price'], qr_mid))
    except Exception as e:
        logger.error(f"generate_and_send_qr: {e}")
        await _send(chat_id, "❌ <b>មានបញ្ហាក្នុងការបង្កើត QR Code</b>\n\nសូមព្យាយាមម្តងទៀត")
        with _data_lock:
            order_sessions.pop(user_id, None)
        _save_sessions_bg()


# ─── New user notification ────────────────────────────────────────────────────
async def _notify_new_user(user_id, first, last, username):
    if _is_admin_notified(user_id):
        return
    with _notified_lock:
        if user_id in _notified_users:
            return
        _notified_users.add(user_id)
    full = f"{first} {last}".strip() or 'N/A'
    uname_str = f"@{username}" if username else '—'
    msg = (f"🆕 <b>អ្នកប្រើប្រាស់ថ្មី!</b>\n\n"
           f"👤 ឈ្មោះ: {html.escape(full)}\n"
           f"🔖 Username: {html.escape(uname_str)}\n"
           f"🪪 ID: <code>{user_id}</code>")
    try:
        await _send(ADMIN_ID, msg)
    except Exception as e:
        logger.error(f"notify_new_user send: {e}")
    loop = asyncio.get_running_loop()
    await loop.run_in_executor(None, _upsert_known_user_sync, user_id, first, last, username)


# ─── E-GetS Verification Relay ────────────────────────────────────────────────
def _parse_egets_message(text):
    email_m = re.search(r'[\w.+%-]+@[\w.-]+\.[A-Za-z]{2,}', text or '')
    code_m  = re.search(r'(?<!\d)\d{4,8}(?!\d)', text or '')
    if not email_m or not code_m:
        return None, None
    return email_m.group(0).strip().lower(), code_m.group(0)


async def handle_channel_post(client, message: Message):
    global CHANNEL_ID
    if not CHANNEL_ID:
        return
    try:
        chat_id = message.chat.id
        if str(chat_id) != str(CHANNEL_ID):
            return
        text = message.text or message.caption or ''
        email, code = _parse_egets_message(text)
        if not email or not code:
            return
        kb = InlineKeyboardMarkup([[InlineKeyboardButton(f'📋 Copy: {code}', copy_text=code)]])
        fmsg = (f"📩 <b>លេខកូដផ្ទៀងផ្ទាត់ E-GetS</b>\n\n"
                f"{html.escape(email)}\n\n<code>{html.escape(code)}</code>")
        loop = asyncio.get_running_loop()
        buyer_ids = await loop.run_in_executor(None, _find_buyers_by_email_sync, email)
        if not buyer_ids:
            logger.info(f"No buyer found for email {email}")
            return
        for buyer_id in buyer_ids:
            try:
                sent = await _send(buyer_id, fmsg, kb=kb)
                if sent:
                    logger.info(f"Sent E-GetS code for {email} to {buyer_id}")
            except Exception as e:
                logger.warning(f"E-GetS relay to {buyer_id}: {e}")
    except Exception as e:
        logger.error(f"handle_channel_post: {e}")


# ─── Broadcast ────────────────────────────────────────────────────────────────
async def _run_broadcast(from_chat_id, source_message_id, use_copy=True):
    loop = asyncio.get_running_loop()
    try:
        rows = await loop.run_in_executor(None, _db_query,
                                          "SELECT user_id FROM order_known_users")
        total = len(rows)
        sent = failed = blocked = 0
        for r in rows:
            uid = r.get('user_id')
            if not uid:
                continue
            try:
                if use_copy:
                    await _app.copy_message(uid, from_chat_id, source_message_id)
                else:
                    await _app.forward_messages(uid, from_chat_id, source_message_id)
                sent += 1
            except Exception as e:
                err = str(e).lower()
                if any(w in err for w in ('blocked', 'deactivated', 'forbidden', 'not found')):
                    blocked += 1
                else:
                    failed += 1
            await asyncio.sleep(0.05)
        summary = (f"📢 <b>ផ្សាយ​បានចប់</b>\n"
                   f"━━━━━━━━━━━━━━━━━\n"
                   f"👥 សរុប: {total}\n✅ ជោគជ័យ: {sent}\n"
                   f"⛔ Block/លុប: {blocked}\n❌ Error: {failed}")
        await _send(from_chat_id, summary,
                    kb=InlineKeyboardMarkup([[_back_btn('s:main')]]))
    except Exception as e:
        logger.error(f"broadcast crashed: {e}")
        await _send(from_chat_id, f"❌ Broadcast error: <code>{html.escape(str(e))}</code>",
                    kb=InlineKeyboardMarkup([[_back_btn('s:main')]]))


# ─── Handle admin settings input (text messages) ──────────────────────────────
async def _handle_admin_input(client, chat_id, user_id, key, text, message: Message):
    global PAYMENT_NAME, CHANNEL_ID, EXTRA_ADMIN_IDS, MAINTENANCE_MODE
    raw  = (text or '').strip()
    sess = order_sessions.get(user_id, {})
    return_menu = sess.get('settings_return', 'main')

    def _finish():
        with _data_lock:
            order_sessions.pop(user_id, None)
        _save_sessions_bg()

    cancel_words = {'បោះបង់', '🚫 បោះបង់'}
    if raw in cancel_words:
        _finish()
        await _settings_edit(chat_id, user_id,
            "<b>⚙️ ការកំណត់ Admin</b>\n\nសូមជ្រើសរើសប្រតិបត្តិការ:",
            _settings_main_ikb())
        return True

    if key == 'payment':
        if not raw:
            await _settings_edit(chat_id, user_id,
                "💳 <b>Payment Name</b>\n\nផ្ញើ Payment Name ថ្មី:", _settings_cancel_ikb())
            return True
        PAYMENT_NAME = raw
        _set_setting('PAYMENT_NAME', raw)
        _finish()
        await _show_pay_panel(chat_id, user_id)
        return True

    if key == 'bakong':
        if not raw:
            await _settings_edit(chat_id, user_id,
                "🔑 <b>Bakong Note</b>\n\nផ្ញើ Payment Note ថ្មី:", _settings_cancel_ikb())
            return True
        PAYMENT_NAME = raw
        _set_setting('PAYMENT_NAME', raw)
        _finish()
        await _show_bak_panel(chat_id, user_id)
        return True

    if key == 'channel':
        if not raw:
            await _settings_edit(chat_id, user_id,
                "📢 <b>Channel ID</b>\n\nផ្ញើ Channel ID (ឧ. <code>-1001234567890</code>)\nឬ <code>off</code> ដើម្បីលុប:",
                _settings_cancel_ikb())
            return True
        if raw.lower() in ('off', 'none', 'clear'):
            CHANNEL_ID = ''
            _set_setting('TELEGRAM_CHANNEL_ID', '')
        else:
            CHANNEL_ID = raw
            _set_setting('TELEGRAM_CHANNEL_ID', raw)
        _finish()
        await _show_ch_panel(chat_id, user_id)
        return True

    if key == 'admin_add':
        try:
            target = int(raw)
        except ValueError:
            await _settings_edit(chat_id, user_id,
                "❌ <b>user_id ត្រូវតែជាលេខ</b>\n\nផ្ញើ Telegram User ID:", _settings_cancel_ikb())
            return True
        if target == ADMIN_ID:
            await _settings_edit(chat_id, user_id,
                "ℹ️ Admin បឋមមិនអាចកែប្រែ\n\n<i>ចុច 🚫 បោះបង់</i>", _settings_cancel_ikb())
            return True
        EXTRA_ADMIN_IDS.add(target)
        _set_setting('EXTRA_ADMIN_IDS', json.dumps(sorted(EXTRA_ADMIN_IDS)))
        _finish()
        await _show_adm_panel(chat_id, user_id)
        return True

    if key == 'admin_remove':
        try:
            target = int(raw)
        except ValueError:
            await _settings_edit(chat_id, user_id,
                "❌ <b>user_id ត្រូវតែជាលេខ</b>\n\nផ្ញើ Telegram User ID:", _settings_cancel_ikb())
            return True
        EXTRA_ADMIN_IDS.discard(target)
        _set_setting('EXTRA_ADMIN_IDS', json.dumps(sorted(EXTRA_ADMIN_IDS)))
        _finish()
        await _show_adm_panel(chat_id, user_id)
        return True

    if key == 'broadcast':
        if not message:
            await _settings_edit(chat_id, user_id,
                "📢 <b>Broadcast</b>\n\nផ្ញើ​សារដែលចង់ផ្សាយ:", _settings_cancel_ikb())
            return True
        is_text = bool(raw)
        with _data_lock:
            order_sessions[user_id] = {
                'state': 'broadcast_confirm',
                'broadcast_message_id': message.id,
                'broadcast_chat_id': chat_id,
                'broadcast_use_copy': is_text,
            }
        _save_sessions_bg()
        await _settings_edit(chat_id, user_id,
            "❓ <b>ផ្សាយ​សារ​ខាងលើ​ទៅ​អ្នក​ប្រើ​ប្រាស់​ទាំងអស់?</b>",
            _broadcast_confirm_ikb())
        return True

    return False


# ─── Main callback handler (returns True if handled) ─────────────────────────
async def handle_order_callback(client, query: CallbackQuery) -> bool:
    global CHANNEL_ID, MAINTENANCE_MODE, EXTRA_ADMIN_IDS, PAYMENT_NAME
    d    = query.data or ''
    cid  = query.message.chat.id
    uid  = query.from_user.id
    user = query.from_user

    # Track user
    if uid != ADMIN_ID:
        asyncio.create_task(_notify_new_user(uid,
            user.first_name or '', user.last_name or '', user.username or ''))

    # ── Order home button ────────────────────────────────────────────────────
    if d == 'o:home':
        await query.answer()
        await send_account_selection(cid)
        return True

    # ── Buy flow ─────────────────────────────────────────────────────────────
    if d.startswith('buy:'):
        if MAINTENANCE_MODE and not is_admin(uid):
            await query.answer("🛠 Bot កំពុង Maintenance — សូមរង់ចាំ!", show_alert=True)
            return True
        existing = order_sessions.get(uid)
        if existing and existing.get('state') == 'payment_pending':
            await query.answer()
            await _del_msg(cid, query.message.id)
            await _remind_pending(cid, existing)
            return True
        cb_id = d[4:]
        account_type = _type_from_cb_id(cb_id)
        if not account_type:
            await query.answer("ប្រភេទនេះមិនមានទៀតហើយ!", show_alert=True)
            return True
        with _data_lock:
            avail = accounts_data.get('account_types', {}).get(account_type, [])
            count = len(avail)
            price = accounts_data.get('prices', {}).get(account_type, 0)
        if count <= 0:
            await query.answer(f"សុំទោស! {account_type} អស់ស្តុក!", show_alert=True)
            return True
        await query.answer()
        with _data_lock:
            order_sessions[uid] = {
                'state': 'waiting_for_quantity',
                'account_type': account_type,
                'price': price,
                'available_count': count,
            }
        _save_sessions_bg()
        qty_rows = [[_ikb(str(n), f'qty:{n}') for n in range(i, min(i+4, count+1))]
                    for i in range(1, count+1, 4)]
        qty_rows.append([_ikb('🚫 បោះបង់', 'cancel_buy')])
        await _del_msg(cid, query.message.id)
        await _send(cid, f"<b>🛒 {html.escape(account_type)}</b> — ${price}/ខ\n\n<b>សូមជ្រើសរើសចំនួន:</b>",
                    kb=InlineKeyboardMarkup(qty_rows))
        return True

    # ── Quantity selected ─────────────────────────────────────────────────────
    if d.startswith('qty:'):
        sess = order_sessions.get(uid)
        if not sess or sess.get('state') != 'waiting_for_quantity':
            await query.answer()
            await _del_msg(cid, query.message.id)
            await send_account_selection(cid)
            return True
        try:
            quantity = int(d.split(':', 1)[1])
        except (ValueError, IndexError):
            await query.answer()
            await _del_msg(cid, query.message.id)
            await send_account_selection(cid)
            return True
        if quantity > sess.get('available_count', 0):
            await query.answer()
            await _del_msg(cid, query.message.id)
            await send_account_selection(cid)
            return True
        total_price = quantity * sess.get('price', 0)
        account_type = sess.get('account_type', '')
        with _data_lock:
            sess['quantity']    = quantity
            sess['total_price'] = total_price
            sess['state']       = 'waiting_for_confirmation'
        _save_sessions_bg()
        await query.answer()
        await _del_msg(cid, query.message.id)
        confirm_kb = InlineKeyboardMarkup([
            [_ikb('✅ យល់ព្រម', 'confirm_buy')],
            [_ikb('🚫 បោះបង់', 'cancel_buy')],
        ])
        summary = (f"📋 <b>បញ្ជាក់ការទិញ</b>\n\n"
                   f"📦 ប្រភេទ: <b>{html.escape(account_type)}</b>\n"
                   f"🔢 ចំនួន: <b>{quantity}</b>\n"
                   f"💵 តម្លៃ: <b>${total_price:.2f}</b>\n\n"
                   f"<i>ចុច ✅ យល់ព្រម ដើម្បីបង្កើត QR Payment</i>")
        msg_sent = await _send(cid, summary, kb=confirm_kb)
        if msg_sent:
            sess['summary_message_id'] = msg_sent.id
        return True

    # ── Confirm buy ───────────────────────────────────────────────────────────
    if d == 'confirm_buy':
        sess = order_sessions.get(uid)
        if not sess or sess.get('state') != 'waiting_for_confirmation':
            await query.answer("មិនមានការទិញដែលកំពុងរង់ចាំ", show_alert=True)
            return True
        await query.answer("កំពុងបង្កើត QR...")
        with _data_lock:
            sess['state'] = 'payment_pending'
        await _del_msg(cid, query.message.id)
        await _generate_and_send_qr(client, cid, uid, sess)
        return True

    # ── Cancel buy ────────────────────────────────────────────────────────────
    if d == 'cancel_buy':
        await query.answer()
        with _data_lock:
            order_sessions.pop(uid, None)
        _save_sessions_bg()
        await _del_msg(cid, query.message.id)
        await send_account_selection(cid)
        return True

    # ── Cancel purchase (pending payment) ─────────────────────────────────────
    if d == 'cancel_purchase':
        sess = order_sessions.get(uid)
        if not sess:
            loop = asyncio.get_running_loop()
            sess = await loop.run_in_executor(None, _get_pending_payment_sync, uid)
        txn_id = (sess.get('txn_id') or sess.get('md5_hash')) if sess else None
        if txn_id:
            is_paid, pd = await check_payment_status(txn_id)
            if is_paid:
                await query.answer("✅ ការបង់ប្រាក់បានបញ្ជាក់!")
                user_name = f"{user.first_name or ''} {user.last_name or ''}".strip()
                if sess:
                    await _deliver_accounts(client, cid, uid, sess, pd, user_name)
                loop = asyncio.get_running_loop()
                await loop.run_in_executor(None, _delete_pending_payment_sync, uid)
                _save_sessions_bg()
                return True
        await query.answer()
        btn_mid = query.message.id
        await _del_msg(cid, btn_mid)
        if sess:
            for k in ('photo_message_id', 'qr_message_id'):
                mid = sess.get(k)
                if mid and mid != btn_mid:
                    await _del_msg(cid, mid)
        with _data_lock:
            order_sessions.pop(uid, None)
        _save_sessions_bg()
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, _delete_pending_payment_sync, uid)
        await send_account_selection(cid)
        return True

    # ── Check payment (manual) ────────────────────────────────────────────────
    if d == 'check_payment':
        sess = order_sessions.get(uid)
        if not sess:
            loop = asyncio.get_running_loop()
            sess = await loop.run_in_executor(None, _get_pending_payment_sync, uid)
        if not sess:
            await query.answer()
            await _del_msg(cid, query.message.id)
            await send_account_selection(cid)
            return True
        txn_id = sess.get('txn_id') or sess.get('md5_hash')
        if not txn_id:
            await query.answer()
            await _del_msg(cid, query.message.id)
            await send_account_selection(cid)
            return True
        is_paid, pd = await check_payment_status(txn_id)
        if is_paid:
            await query.answer("✅ ការបង់ប្រាក់បានបញ្ជាក់!")
            user_name = f"{user.first_name or ''} {user.last_name or ''}".strip()
            await _deliver_accounts(client, cid, uid, sess, pd, user_name)
            loop = asyncio.get_running_loop()
            await loop.run_in_executor(None, _delete_pending_payment_sync, uid)
            _save_sessions_bg()
        else:
            await query.answer("⏳ មិនទាន់បានទទួលការបង់ប្រាក់! សូមព្យាយាមម្ដងទៀត", show_alert=True)
        return True

    # ── Donate flow ───────────────────────────────────────────────────────────
    if d == 'don_top':
        await query.answer()
        top_text = await _format_top_donors()
        kb = InlineKeyboardMarkup([
            [_ikb('🔄 ផ្ទុកឡើងវិញ', 'don_top')],
            [_ikb('💝 Donate', 'donate_khpay'),
             InlineKeyboardButton('🏠 ម៉ឺនុយមេ', callback_data='home',
                                  icon_custom_emoji_id='5282843764451195532')],
        ])
        try:
            await query.message.edit_text(top_text, parse_mode=ParseMode.HTML, reply_markup=kb)
        except Exception:
            await _send(cid, top_text, kb=kb)
        return True

    if d == 'donate_khpay':
        await query.answer()
        await send_donate_menu(cid, uid)
        return True

    if d.startswith('don:'):
        existing = order_sessions.get(uid)
        if existing and existing.get('state') in ('donation_pending', 'payment_pending'):
            await query.answer('⚠️ មានការ Donate/ទិញដែលកំពុងរង់ចាំ!', show_alert=True)
            return True
        try:
            amount = float(d.split(':', 1)[1])
        except (ValueError, IndexError):
            await query.answer()
            return True
        if amount <= 0:
            await query.answer('❌ ចំនួនមិនត្រឹមត្រូវ!', show_alert=True)
            return True
        await query.answer()
        with _data_lock:
            order_sessions[uid] = {
                'state': 'donation_pending',
                'amount': amount,
                'user_info': {
                    'first_name': user.first_name or '',
                    'last_name':  user.last_name or '',
                    'username':   user.username or '',
                },
            }
        _save_sessions_bg()
        await _generate_and_send_donate_qr(client, cid, uid, amount,
                                            order_sessions[uid])
        return True

    if d == 'don_custom':
        existing = order_sessions.get(uid)
        if existing and existing.get('state') in ('donation_pending', 'payment_pending'):
            await query.answer('⚠️ មានការ Donate/ទិញដែលកំពុងរង់ចាំ!', show_alert=True)
            return True
        await query.answer()
        with _data_lock:
            order_sessions[uid] = {
                'state': 'don_waiting_amount',
                'user_info': {
                    'first_name': user.first_name or '',
                    'last_name':  user.last_name or '',
                    'username':   user.username or '',
                },
            }
        _save_sessions_bg()
        await _send(cid,
            '✏️ <b>Donate ចំនួនផ្ទាល់ខ្លួន</b>\n\n'
            'ផ្ញើចំនួន <b>USD</b> ដែលចង់ Donate:\n\n'
            '<code>3.5</code> · <code>7</code> · <code>15</code> · <code>25</code>\n\n'
            '<i>ចុច 🚫 បោះបង់ ដើម្បីបោះបង់</i>',
            kb=InlineKeyboardMarkup([[_ikb('🚫 បោះបង់', 'don_cancel')]]))
        return True

    if d == 'don_cancel':
        await query.answer()
        with _data_lock:
            sess = order_sessions.pop(uid, None)
        _save_sessions_bg()
        if sess and sess.get('qr_message_id'):
            await _del_msg(cid, sess['qr_message_id'])
        await _del_msg(cid, query.message.id)
        await send_donate_menu(cid, uid)
        return True

    # ── Delete type confirm ───────────────────────────────────────────────────
    if d.startswith('dts:') and is_admin(uid):
        type_name = _type_from_cb_id(d[4:]) or d[4:]
        if type_name not in accounts_data.get('account_types', {}):
            await query.answer("ប្រភេទនេះមិនមានទៀត!", show_alert=True)
            return True
        await query.answer()
        _admin_order_msg[uid] = query.message.id
        count = len(accounts_data['account_types'].get(type_name, []))
        price = accounts_data.get('prices', {}).get(type_name, 0)
        confirm_cb = f"dtc:{_type_cb_id(type_name)}"
        await _settings_edit(cid, uid,
            f"⚠️ <b>តើពិតចង់លុបប្រភេទ Account នេះ?</b>\n\n"
            f"<blockquote>🔹 ប្រភេទ: {html.escape(type_name)}\n"
            f"🔹 ចំនួន: {count}\n🔹 តម្លៃ: ${price}</blockquote>\n\n"
            f"⚠️ Account ទាំងអស់នឹងត្រូវបានលុបជាអចិន្ត្រៃយ៍!",
            InlineKeyboardMarkup([
                [_ikb('✅ បញ្ជាក់លុប', confirm_cb), _ikb('🚫 បោះបង់', 'dtcancel')]
            ]))
        return True

    if d.startswith('dtc:') and is_admin(uid):
        type_name = _type_from_cb_id(d[4:]) or d[4:]
        if type_name not in accounts_data.get('account_types', {}):
            await query.answer("ប្រភេទនេះមិនមានទៀត!", show_alert=True)
            return True
        await query.answer()
        _admin_order_msg[uid] = query.message.id
        with _data_lock:
            count = len(accounts_data['account_types'].pop(type_name, []))
            accounts_data.get('prices', {}).pop(type_name, None)
        _save_data_bg()
        await _settings_edit(cid, uid,
            f"✅ <b>បានលុបប្រភេទ <code>{html.escape(type_name)}</code> — {count} records</b>",
            InlineKeyboardMarkup([[_back_btn('s:del_type')]]))
        return True

    if d == 'dtcancel' and is_admin(uid):
        await query.answer()
        _admin_order_msg[uid] = query.message.id
        await _show_del_type_panel(cid, uid)
        return True

    # ── Settings panel (s: callbacks) ─────────────────────────────────────────
    if d.startswith('s:') and is_admin(uid):
        action = d[2:]
        await query.answer()
        _admin_order_msg[uid] = query.message.id

        if action == 'close':
            await _del_msg(cid, query.message.id)
            return True
        if action == 'main':
            await _settings_edit(cid, uid,
                "<b>⚙️ ការកំណត់ Admin</b>\n\nសូមជ្រើសរើស:",
                _settings_main_ikb())
            return True
        if action == 'cancel_input':
            with _data_lock:
                order_sessions.pop(uid, None)
            _save_sessions_bg()
            await _settings_edit(cid, uid,
                "<b>⚙️ ការកំណត់ Admin</b>\n\nសូមជ្រើសរើស:",
                _settings_main_ikb())
            return True
        if action == 'pay':
            await _show_pay_panel(cid, uid); return True
        if action == 'pay_edit':
            await _prompt_admin_input(cid, uid, 'payment',
                f"💳 Payment Name បច្ចុប្បន្ន: <b>{html.escape(PAYMENT_NAME or '(មិនទាន់)')}</b>\n\nផ្ញើ Payment Name ថ្មី:", 'pay')
            return True
        if action == 'bak':
            await _show_bak_panel(cid, uid); return True
        if action == 'bak_edit':
            await _prompt_admin_input(cid, uid, 'bakong',
                "🔑 ផ្ញើ Payment Note ថ្មី (ដូច RADY, MyShop, …):", 'bak')
            return True
        if action == 'ch':
            await _show_ch_panel(cid, uid); return True
        if action == 'ch_edit':
            await _prompt_admin_input(cid, uid, 'channel',
                "📢 ផ្ញើ Channel ID ថ្មី (ឧ. <code>-1001234567890</code>)\nឬ <code>off</code> ដើម្បីលុប:", 'ch')
            return True
        if action == 'ch_clear':
            CHANNEL_ID = ''
            _set_setting('TELEGRAM_CHANNEL_ID', '')
            await _show_ch_panel(cid, uid); return True
        if action == 'adm':
            await _show_adm_panel(cid, uid); return True
        if action == 'adm_add':
            await _prompt_admin_input(cid, uid, 'admin_add',
                "➕ ផ្ញើ <b>Telegram User ID</b> ដើម្បីបន្ថែមជា Admin:", 'adm')
            return True
        if action == 'adm_rm':
            await _prompt_admin_input(cid, uid, 'admin_remove',
                "➖ ផ្ញើ <b>Telegram User ID</b> ដើម្បីដក Admin:", 'adm')
            return True
        if action == 'adm_noop':
            return True
        if action.startswith('adm_del:'):
            try:
                del_id = int(action.split(':', 1)[1])
            except (IndexError, ValueError):
                return True
            EXTRA_ADMIN_IDS.discard(del_id)
            _set_setting('EXTRA_ADMIN_IDS', json.dumps(sorted(EXTRA_ADMIN_IDS)))
            await _show_adm_panel(cid, uid); return True
        if action == 'mnt':
            await _show_mnt_panel(cid, uid); return True
        if action == 'mnt_on':
            MAINTENANCE_MODE = True
            _set_setting('MAINTENANCE_MODE', 'true')
            await _show_mnt_panel(cid, uid); return True
        if action == 'mnt_off':
            MAINTENANCE_MODE = False
            _set_setting('MAINTENANCE_MODE', 'false')
            await _show_mnt_panel(cid, uid); return True
        if action == 'add_acc':
            await _start_add_account_flow(cid, uid); return True
        if action == 'del_type':
            await _show_del_type_panel(cid, uid); return True
        if action == 'buyers':
            await _export_buyers_panel(cid); return True
        if action == 'users':
            await _show_users_panel(cid); return True
        if action == 'broadcast':
            await _prompt_admin_input(cid, uid, 'broadcast',
                "📢 <b>Broadcast</b>\n\nផ្ញើ​សារ​ (text/photo/file) ដែលចង់ផ្សាយ:", 'main')
            return True
        if action == 'broadcast_confirm':
            sess = order_sessions.get(uid, {})
            if sess.get('state') != 'broadcast_confirm':
                return True
            src_mid  = sess.get('broadcast_message_id')
            src_cid  = sess.get('broadcast_chat_id', cid)
            use_copy = sess.get('broadcast_use_copy', True)
            with _data_lock:
                order_sessions.pop(uid, None)
            _save_sessions_bg()
            await _settings_edit(cid, uid, "📢 <b>ចាប់ផ្ដើមផ្សាយ...</b>",
                                  InlineKeyboardMarkup([]))
            asyncio.create_task(_run_broadcast(src_cid, src_mid, use_copy))
            return True
        if action == 'broadcast_cancel':
            with _data_lock:
                order_sessions.pop(uid, None)
            _save_sessions_bg()
            await _settings_edit(cid, uid, "🚫 <b>បានបោះបង់ការផ្សាយ</b>",
                InlineKeyboardMarkup([[_back_btn('s:main')]]))
            return True
        return True  # unknown s: action — still handled

    return False  # not handled by order module


# ─── Pending payment reminder ─────────────────────────────────────────────────
async def _remind_pending(chat_id, session):
    qr_mid = session.get('qr_message_id') or session.get('photo_message_id')
    if qr_mid:
        try:
            await _app.copy_message(chat_id, chat_id, qr_mid,
                                     reply_markup=_check_payment_ikb())
            return
        except Exception:
            pass
    await _send(chat_id,
        "⚠️ <b>លោកអ្នកមានការទិញដែលកំពុងរង់ចាំការបង់ប្រាក់</b>\n\n"
        "ចុច 🚫 បោះបង់ ដើម្បីបោះបង់", kb=_check_payment_ikb())


# ─── Main message handler (returns True if handled) ───────────────────────────
async def handle_order_message(client, message: Message) -> bool:
    uid  = message.from_user.id if message.from_user else None
    if not uid:
        return False
    cid  = message.chat.id
    text = message.text or message.caption or ''
    user = message.from_user

    # Track user
    if uid != ADMIN_ID:
        asyncio.create_task(_notify_new_user(uid,
            user.first_name or '', user.last_name or '', user.username or ''))

    sess = order_sessions.get(uid)
    if not sess:
        return False

    state = sess.get('state', '')

    # ── Admin input states ────────────────────────────────────────────────────
    if state.startswith('admin_input:') and is_admin(uid):
        key = state.split(':', 1)[1]
        await _handle_admin_input(client, cid, uid, key, text, message)
        return True

    # ── Broadcast confirm (waiting for message to broadcast) ──────────────────
    if state == 'broadcast_confirm' and is_admin(uid):
        return False  # Let bot.py handle normally, this state is already set

    # ── Waiting for accounts (admin adding emails) ────────────────────────────
    if state == 'waiting_for_accounts' and is_admin(uid):
        if not text:
            return False
        email_pattern = re.compile(r'^[^@\s]+@[^@\s]+\.[^@\s]+$')
        accs = []
        seen = set()
        for line in text.strip().split('\n'):
            em = line.strip()
            if em and email_pattern.match(em) and em.lower() not in seen:
                seen.add(em.lower())
                accs.append({'email': em})
        if not accs:
            await _settings_edit(cid, uid,
                "❌ <b>មិនរកឃើញ Email ត្រឹមត្រូវ!</b>\n\n"
                "📧 ផ្ញើ Email ម្តងមួយបន្ទាត់:\n\n"
                "<code>example@gmail.com\ntest@yahoo.com</code>\n\n"
                "<i>ចុច 🚫 បោះបង់ ដើម្បីបោះបង់</i>",
                _settings_cancel_ikb())
            return True
        with _data_lock:
            order_sessions[uid] = {'state': 'waiting_for_account_type', 'pending_accounts': accs}
        _save_sessions_bg()
        await _settings_edit(cid, uid,
            f"✅ <b>បានទទួល {len(accs)} Accounts</b>\n\n"
            "📂 ផ្ញើប្រភេទ Account (ឧ. Facebook, Netflix, TikTok):\n\n"
            "<i>ចុច 🚫 បោះបង់ ដើម្បីបោះបង់</i>",
            _settings_cancel_ikb())
        return True

    if state == 'waiting_for_account_type' and is_admin(uid):
        if not text:
            return True
        account_type = text.strip()
        pending = sess.get('pending_accounts', [])
        existing_price = accounts_data.get('prices', {}).get(account_type, 0)
        with _data_lock:
            order_sessions[uid] = {
                'state': 'waiting_for_price',
                'pending_accounts': pending,
                'account_type': account_type,
            }
        _save_sessions_bg()
        hint = f"\n💵 <b>តម្លៃបច្ចុប្បន្ន: ${existing_price}</b>" if existing_price else ""
        await _settings_edit(cid, uid,
            f"📂 <b>ប្រភេទ: {html.escape(account_type)}</b>{hint}\n\n"
            "💲 ផ្ញើតម្លៃ (USD) ក្នុងមួយ Account:\n\n"
            "<i>ចុច 🚫 បោះបង់ ដើម្បីបោះបង់</i>",
            _settings_cancel_ikb())
        return True

    if state == 'waiting_for_price' and is_admin(uid):
        try:
            price        = float(text.strip())
            account_type = sess['account_type']
            new_accs     = sess.get('pending_accounts', [])
            with _data_lock:
                existing_emails = {
                    a.get('email', '').lower()
                    for accs_list in accounts_data.get('account_types', {}).values()
                    for a in accs_list if a.get('email')
                }
            dup = [a['email'] for a in new_accs if a['email'].lower() in existing_emails]
            uniq = [a for a in new_accs if a['email'].lower() not in existing_emails]
            if dup and not uniq:
                await _settings_edit(cid, uid,
                    f"❌ <b>Email ទាំងអស់មានស្រាប់ក្នុងប្រព័ន្ធ!</b>\n\n"
                    f"<code>{html.escape(chr(10).join(dup))}</code>\n\n"
                    "<i>ចុច 🚫 បោះបង់</i>", _settings_cancel_ikb())
                return True
            if dup:
                await _settings_edit(cid, uid,
                    f"⚠️ <b>Email ខាងក្រោមមានស្រាប់ ហើយត្រូវបានរំលង:</b>\n"
                    f"<code>{html.escape(chr(10).join(dup))}</code>",
                    _settings_cancel_ikb())
            new_accs = uniq
            count = len(new_accs)
            with _data_lock:
                accounts_data['accounts'].extend(new_accs)
                if account_type in accounts_data['account_types']:
                    accounts_data['account_types'][account_type].extend(new_accs)
                else:
                    accounts_data['account_types'][account_type] = new_accs
                accounts_data['prices'][account_type] = price
                order_sessions.pop(uid, None)
            _save_data_bg(); _save_sessions_bg()
            await _settings_edit(cid, uid,
                f"✅ <b>បានបន្ថែម Account ជោគជ័យ</b>\n\n"
                f"<blockquote>🔹 ចំនួន: {count}\n"
                f"🔹 ប្រភេទ: {html.escape(account_type)}\n"
                f"🔹 តម្លៃ: ${price}</blockquote>",
                InlineKeyboardMarkup([[_back_btn('s:main')]]))
        except ValueError:
            await _settings_edit(cid, uid,
                "❌ <b>តម្លៃមិនត្រឹមត្រូវ</b>\n\nផ្ញើតម្លៃជាលេខ (ឧ. <code>5.99</code>)\n\n"
                "<i>ចុច 🚫 បោះបង់</i>", _settings_cancel_ikb())
        return True

    # ── Payment pending — remind ───────────────────────────────────────────────
    if state == 'payment_pending':
        await _remind_pending(cid, sess)
        return True

    # ── Donation pending — remind ──────────────────────────────────────────────
    if state == 'donation_pending':
        await _send(cid,
            '⚠️ <b>មានការ Donate ដែលកំពុងរង់ចាំ</b>\n\nចុច 🚫 បោះបង់ ដើម្បីបោះបង់',
            kb=InlineKeyboardMarkup([[_ikb('🚫 បោះបង់', 'don_cancel')]]))
        return True

    # ── Custom donation amount input ───────────────────────────────────────────
    if state == 'don_waiting_amount':
        raw = text.strip()
        try:
            amount = float(raw)
            if amount <= 0:
                raise ValueError
        except ValueError:
            await _send(cid,
                '❌ <b>ចំនួនមិនត្រឹមត្រូវ!</b>\n\nផ្ញើជាលេខ (ឧ. <code>3.5</code> ឬ <code>10</code>)',
                kb=InlineKeyboardMarkup([[_ikb('🚫 បោះបង់', 'don_cancel')]]))
            return True
        if amount < 0.5:
            await _send(cid,
                '❌ <b>ចំនួនតិចពេក!</b> យ៉ាងហោចណាស់ <b>$0.50</b>\n\nផ្ញើចំនួនថ្មី:',
                kb=InlineKeyboardMarkup([[_ikb('🚫 បោះបង់', 'don_cancel')]]))
            return True
        user_info = sess.get('user_info', {})
        with _data_lock:
            order_sessions[uid] = {
                'state': 'donation_pending',
                'amount': amount,
                'user_info': user_info,
            }
        _save_sessions_bg()
        await _generate_and_send_donate_qr(client, cid, uid, amount, order_sessions[uid])
        return True

    return False


# ─── /settings command handler ───────────────────────────────────────────────
async def handle_settings_command(client, message: Message):
    uid = message.from_user.id
    if not is_admin(uid):
        await message.reply("⛔ <b>អ្នកមិនមានសិទ្ធ!</b>", parse_mode=ParseMode.HTML)
        return
    await send_admin_settings(client, message.chat.id, uid)


# ─── /order command / order button handler ───────────────────────────────────
async def handle_order_command(client, message: Message):
    uid = message.from_user.id if message.from_user else None
    if not uid:
        return
    cid = message.chat.id
    user = message.from_user
    asyncio.create_task(_notify_new_user(uid,
        user.first_name or '', user.last_name or '', user.username or ''))
    if MAINTENANCE_MODE and not is_admin(uid):
        await _send(cid, "🛠 <b>Bot កំពុង Maintenance</b>\n\nសូមរង់ចាំ!")
        return
    await send_account_selection(cid)


# ─── /history command handler ─────────────────────────────────────────────────
async def handle_history_command(client, message: Message):
    uid = message.from_user.id if message.from_user else None
    if not uid:
        return
    cid = message.chat.id
    loop = asyncio.get_running_loop()
    rows = await loop.run_in_executor(None, _get_purchase_history_sync, uid, 10)
    if not rows:
        await _send(cid, "📭 <b>អ្នកមិនទាន់មានប្រវត្តិទិញ</b>")
        return
    lines = ["📋 <b>ប្រវត្តិទិញរបស់អ្នក</b>\n"]
    for i, r in enumerate(rows, 1):
        accs = r.get('accounts') or []
        if isinstance(accs, str):
            try: accs = json.loads(accs)
            except Exception: accs = []
        emails = [a.get('email', '') for a in accs if isinstance(a, dict) and a.get('email')]
        ts = str(r.get('purchased_at') or '')[:16]
        lines.append(f"{i}. <b>{html.escape(r.get('account_type',''))} × {r.get('quantity',0)}</b> — ${r.get('total_price',0)}")
        lines.append(f"   ⏰ {ts}")
        for em in emails:
            lines.append(f"   <code>{html.escape(em)}</code>")
        lines.append("")
    await _send(cid, "\n".join(lines))


# ─── Init ─────────────────────────────────────────────────────────────────────
def init_order_module(app_instance):
    global _app
    _app = app_instance
    try:
        _init_db_sync()
        _load_data_sync()
        _load_sessions_sync()
        logger.info("Order module initialized")
    except Exception as e:
        logger.error(f"Order module init error: {e}")
