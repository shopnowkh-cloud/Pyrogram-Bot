#!/usr/bin/env python3
# -*- coding: utf-8 -*-
import os
import io
import re
import logging
import asyncio
import tempfile
import json
import httpx
import requests
import threading
import time
import datetime
import hashlib
import html
import inspect as _inspect

from dataclasses import dataclass, field
from typing import Optional
from urllib.parse import quote as url_quote

import psycopg2
import psycopg2.extras
from psycopg2.extras import RealDictCursor

from pyrogram import Client, filters
from pyrogram.enums import ParseMode, ButtonStyle
from pyrogram.types import (
    Message, CallbackQuery,
    InlineKeyboardMarkup, InlineKeyboardButton,
    ReplyKeyboardMarkup, KeyboardButton, ReplyKeyboardRemove,
    LabeledPrice, PreCheckoutQuery,
    LinkPreviewOptions,
)
from pyrogram.methods.utilities.idle import idle as _idle

# ── Logger ─────────────────────────────────────────────────────────────────────
logging.basicConfig(level=logging.INFO, format="%(asctime)s|%(levelname)s|%(message)s")
logger = logging.getLogger(__name__)


# ── Remove-bg stats (persisted to file) ────────────────────────────────────────
STATS_FILE = 'rmbg_stats.json'

def _load_stats() -> dict:
    try:
        with open(STATS_FILE, 'r') as f:
            return json.load(f)
    except Exception:
        return {'total_uses': 0, 'total_charged': 0}

def _save_stats(stats: dict):
    try:
        with open(STATS_FILE, 'w') as f:
            json.dump(stats, f)
    except Exception as e:
        logger.warning(f'stats save: {e}')

def record_rmbg_use(charged_str: str):
    stats = _load_stats()
    stats['total_uses'] += 1
    try:
        stats['total_charged'] += int(charged_str)
    except Exception:
        pass
    _save_stats(stats)


# ── States ─────────────────────────────────────────────────────────────────────
S_MAIN       = 0
S_DOC        = 1
S_STYLE      = 2
S_PDF        = 3
S_PDF2IMG    = 4
S_QR         = 5
S_QR_CREATE  = 6
S_QR_SCAN    = 7
S_PDF_RENAME = 8
S_GOLD       = 9
S_RMBG       = 10

# ── Session ────────────────────────────────────────────────────────────────────
@dataclass
class UserSession:
    state:       int           = S_MAIN
    mid:         Optional[int] = None
    cid:         Optional[int] = None
    pdf_photos:  list          = field(default_factory=list)
    pdf_name:    Optional[str] = None
    pdf2img_fmt: Optional[str] = None


_sessions: dict = {}

def get_sess(uid: int) -> UserSession:
    if uid not in _sessions:
        _sessions[uid] = UserSession()
    return _sessions[uid]

def reset_sess(uid: int) -> UserSession:
    _sessions[uid] = UserSession()
    return _sessions[uid]

def save_msg(sess: UserSession, cid: int, mid: int):
    sess.cid = cid
    sess.mid = mid

# ── Inline keyboard helpers ────────────────────────────────────────────────────
def mkb(rows: list) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(rows)

def ikb(text: str, cb: str, style: ButtonStyle = ButtonStyle.DEFAULT) -> InlineKeyboardButton:
    return InlineKeyboardButton(text, callback_data=cb, style=style)

def ikb_url(text: str, url: str) -> InlineKeyboardButton:
    return InlineKeyboardButton(text, url=url)

# ── Inline keyboards ───────────────────────────────────────────────────────────
IK_DOC = mkb([
    [ikb('🖼️ រូបភាព → PDF', 'photo_pdf')],
    [ikb('🖼️ PDF → PNG', 'pdf_png')],
    [ikb('📷 PDF → JPG', 'pdf_jpg')],
    [ikb('🏠 ម៉ឺនុយមេ', 'home')],
])
IK_PDF_DONE = mkb([
    [ikb('🖼️ PDF ថ្មី', 'photo_pdf')],
    [ikb('🏠 ម៉ឺនុយមេ', 'home')],
])
IK_QR_DONE  = mkb([[ikb('🏠 ម៉ឺនុយមេ', 'home')]])


def ik_img_done(fmt: str) -> InlineKeyboardMarkup:
    cb = 'pdf_png' if fmt == 'PNG' else 'pdf_jpg'
    return mkb([
        [ikb(f'🔄 {fmt} ថ្មី', cb)],
        [ikb('🏠 ម៉ឺនុយមេ', 'home')],
    ])

# ── Text style maps ────────────────────────────────────────────────────────────
def rng(u, lo, hi, base):
    return {chr(i): chr(i + u - base) for i in range(lo, hi)}

def apply_map(t: str, m: dict) -> str:
    return ''.join(m.get(c, c) for c in t)

BM   = {**rng(0x1D400,0x41,0x5B,0x41), **rng(0x1D41A,0x61,0x7B,0x61), **rng(0x1D7CE,0x30,0x3A,0x30)}
IM   = {**rng(0x1D434,0x41,0x5B,0x41), **rng(0x1D44E,0x61,0x7B,0x61)}
BIM  = {**rng(0x1D468,0x41,0x5B,0x41), **rng(0x1D482,0x61,0x7B,0x61)}
SM   = {**rng(0x1D49C,0x41,0x5B,0x41), **rng(0x1D4B6,0x61,0x7B,0x61)}
BSM  = {**rng(0x1D4D0,0x41,0x5B,0x41), **rng(0x1D4EA,0x61,0x7B,0x61)}
DM   = {**rng(0x1D538,0x41,0x5B,0x41), **rng(0x1D552,0x61,0x7B,0x61), **rng(0x1D7D8,0x30,0x3A,0x30)}
FM   = {**rng(0x1D504,0x41,0x5B,0x41), **rng(0x1D51E,0x61,0x7B,0x61), 'C':'\u212D','H':'\u210C','I':'\u2111','R':'\u211C','Z':'\u2128'}
SFM  = {**rng(0x1D5A0,0x41,0x5B,0x41), **rng(0x1D5BA,0x61,0x7B,0x61), **rng(0x1D7E2,0x30,0x3A,0x30)}
MOM  = {**rng(0x1D670,0x41,0x5B,0x41), **rng(0x1D68A,0x61,0x7B,0x61), **rng(0x1D7F6,0x30,0x3A,0x30)}
FW   = {**rng(0xFF21,0x41,0x5B,0x41), **rng(0xFF41,0x61,0x7B,0x61), **rng(0xFF10,0x30,0x3A,0x30), ' ':'\u2003'}
SC   = dict(zip('abcdefghijklmnopqrstuvwxyz','ᴀʙᴄᴅᴇꜰɢʜɪᴊᴋʟᴍɴᴏᴘQʀꜱᴛᴜᴠᴡxʏᴢ'))
BB   = {**rng(0x24B6,0x41,0x5B,0x41), **rng(0x24D0,0x61,0x7B,0x61), '0':'\u24ea','1':'\u2460','2':'\u2461','3':'\u2462','4':'\u2463','5':'\u2464','6':'\u2465','7':'\u2466','8':'\u2467','9':'\u2468'}
UD   = {**dict(zip('abcdefghijklmnopqrstuvwxyz','ɐqɔpǝɟƃɥᴉɾʞlɯuoodqɹsʇnʌʍxʎz')),
        'A':'∀','B':'ᗺ','C':'Ɔ','D':'ᗡ','E':'Ǝ','F':'Ⅎ','G':'פ','H':'H','I':'I','J':'ſ',
        'K':'ʞ','L':'˥','M':'W','N':'N','O':'O','P':'Ԁ','Q':'Q','R':'ɹ','S':'S','T':'┴',
        'U':'∩','V':'Λ','W':'M','X':'X','Y':'⅄','Z':'Z',
        '0':'0','1':'Ɩ','2':'ᄅ','3':'Ɛ','4':'ᔭ','5':'ϛ','6':'9','7':'ㄥ','8':'8','9':'6',' ':' '}
SUPM = {'a':'ᵃ','b':'ᵇ','c':'ᶜ','d':'ᵈ','e':'ᵉ','f':'ᶠ','g':'ᵍ','h':'ʰ','i':'ⁱ','j':'ʲ',
        'k':'ᵏ','l':'ˡ','m':'ᵐ','n':'ⁿ','o':'ᵒ','p':'ᵖ','q':'q','r':'ʳ','s':'ˢ','t':'ᵗ',
        'u':'ᵘ','v':'ᵛ','w':'ʷ','x':'ˣ','y':'ʸ','z':'ᶻ','A':'ᴬ','B':'ᴮ','C':'ᶜ','D':'ᴰ',
        'E':'ᴱ','F':'ᶠ','G':'ᴳ','H':'ᴴ','I':'ᴵ','J':'ᴶ','K':'ᴷ','L':'ᴸ','M':'ᴹ','N':'ᴺ',
        'O':'ᴼ','P':'ᴾ','Q':'Q','R':'ᴿ','S':'ˢ','T':'ᵀ','U':'ᵁ','V':'\u2c7d','W':'ᵂ',
        'X':'ˣ','Y':'ʸ','Z':'ᶻ','0':'⁰','1':'¹','2':'²','3':'³','4':'⁴','5':'⁵','6':'⁶',
        '7':'⁷','8':'⁸','9':'⁹'}
SBM  = {**rng(0x1D5D4,0x41,0x5B,0x41), **rng(0x1D5EE,0x61,0x7B,0x61), **rng(0x1D7EC,0x30,0x3A,0x30)}
SIM  = {**rng(0x1D608,0x41,0x5B,0x41), **rng(0x1D622,0x61,0x7B,0x61)}
SBIM = {**rng(0x1D63C,0x41,0x5B,0x41), **rng(0x1D656,0x61,0x7B,0x61)}
BFM  = {**rng(0x1D56C,0x41,0x5B,0x41), **rng(0x1D586,0x61,0x7B,0x61)}
RI   = {**{chr(0x41+i): chr(0x1F1E6+i) for i in range(26)}, **{chr(0x61+i): chr(0x1F1E6+i) for i in range(26)}}
SQM  = {**{chr(0x41+i): chr(0x1F130+i) for i in range(26)}, **{chr(0x61+i): chr(0x1F130+i) for i in range(26)}}
PAR  = {**{chr(0x61+i): chr(0x249C+i) for i in range(26)}, **{chr(0x41+i): chr(0x249C+i) for i in range(26)}}
SUBM = {'a':'ₐ','e':'ₑ','h':'ₕ','i':'ᵢ','j':'ⱼ','k':'ₖ','l':'ₗ','m':'ₘ','n':'ₙ','o':'ₒ',
        'p':'ₚ','r':'ᵣ','s':'ₛ','t':'ₜ','u':'ᵤ','v':'ᵥ','x':'ₓ',
        '0':'₀','1':'₁','2':'₂','3':'₃','4':'₄','5':'₅','6':'₆','7':'₇','8':'₈','9':'₉'}

TEXT_STYLES = [
    ('𝗕𝗼𝗹𝗱',               lambda t: apply_map(t, BM)),
    ('𝘐𝘵𝘢𝘭𝘪𝘤',             lambda t: apply_map(t, IM)),
    ('𝑩𝒐𝒍𝒅 𝑰𝒕𝒂𝒍𝒊𝒄',       lambda t: apply_map(t, BIM)),
    ('𝒮𝒸𝓇𝒾𝓅𝓉',             lambda t: apply_map(t, SM)),
    ('𝓑𝓸𝓵𝓭 𝓢𝓬𝓻𝓲𝓹𝓽',      lambda t: apply_map(t, BSM)),
    ('𝔻𝕠𝕦𝕓𝕝𝕖',             lambda t: apply_map(t, DM)),
    ('𝔊𝔬𝔱𝔥𝔦𝔠',             lambda t: apply_map(t, FM)),
    ('𝕭𝖔𝖑𝖉 𝕱𝖗𝖆𝖐𝖙𝖚𝖗',     lambda t: apply_map(t, BFM)),
    ('𝖲𝖺𝗇𝗌',                lambda t: apply_map(t, SFM)),
    ('𝗦𝗮𝗻𝘀 𝗕𝗼𝗹𝗱',          lambda t: apply_map(t, SBM)),
    ('𝘚𝘢𝘯𝘴 𝘐𝘵𝘢𝘭𝘪𝘤',        lambda t: apply_map(t, SIM)),
    ('𝙎𝙖𝙣𝙨 𝘽𝙤𝙡𝙙 𝙄𝙩𝙖𝙡𝙞𝙘',  lambda t: apply_map(t, SBIM)),
    ('𝙼𝚘𝚗𝚘',                lambda t: apply_map(t, MOM)),
    ('Ｆｕｌｌｗｉｄｔｈ',    lambda t: apply_map(t, FW)),
    ('ˢᵘᵖᵉʳˢᶜʳⁱᵖᵗ',         lambda t: apply_map(t, SUPM)),
    ('ₛᵤᵦₛcᵣᵢₚₜ',            lambda t: apply_map(t, SUBM)),
    ('Sᴍᴀʟʟ Cᴀᴘꜱ',          lambda t: apply_map(t.lower(), SC)),
    ('Ⓑⓤⓑⓑⓛⓔ',            lambda t: apply_map(t, BB)),
    ('🄰🄱🄲 Squared',         lambda t: apply_map(t, SQM)),
    ('⒜⒝⒞ Paren',            lambda t: apply_map(t.lower(), PAR)),
    ('🇷🇪🇬🇮🇴🇳',              lambda t: apply_map(t, RI)),
    ('uʍop ǝpᴉsdn',          lambda t: ''.join(reversed(apply_map(t, UD)))),
    ('S\u0336t\u0336r\u0336i\u0336k\u0336e\u0336',  lambda t: ''.join(c+'\u0336' for c in t)),
    ('U\u0332n\u0332d\u0332e\u0332r\u0332',          lambda t: ''.join(c+'\u0332' for c in t)),
    ('D\u0333o\u0333u\u0333b\u0333l\u0333e\u0333',   lambda t: ''.join(c+'\u0333' for c in t)),
    ('O\u0305v\u0305e\u0305r\u0305l\u0305i\u0305n\u0305e\u0305', lambda t: ''.join(c+'\u0305' for c in t)),
    ('T\u0303i\u0303l\u0303d\u0303e\u0303',          lambda t: ''.join(c+'\u0303' for c in t)),
    ('S\u0338l\u0338a\u0338s\u0338h\u0338',          lambda t: ''.join(c+'\u0338' for c in t)),
    ('W\u0330a\u0330v\u0330y\u0330',                 lambda t: ''.join(c+'\u0330' for c in t)),
    ('D\u0307o\u0307t\u0307t\u0307e\u0307d\u0307',   lambda t: ''.join(c+'\u0307' for c in t)),
    ('G\u0354l\u0354i\u0354t\u0354c\u0354h\u0354',   lambda t: ''.join(c+['\u0315','\u035c','\u0355'][i%3] for i,c in enumerate(t))),
]

# ── Constants ──────────────────────────────────────────────────────────────────
HOME_TEXT = (
    '<b>សូមស្វាគមន៍មកកាន់ RADY Bot</b> <emoji id="5449885771420934013">🌱</emoji>\n\n'
    'នៅទីនេះ អ្នកអាចប្រើប្រាស់មុខងារជាច្រើន ដែលមានប្រយោជន៍សម្រាប់ជីវិតប្រចាំថ្ងៃ។\n\n'
    '<emoji id="5282843764451195532">🖥</emoji> <b>មុខងារ:</b> រចនាបថអក្សរ បំប្លែង PDF បង្កើត QR Code '
    'ហាងឆេងមាស Remove BG និង Email Temporary...'
)

# ── Gold price ─────────────────────────────────────────────────────────────────
CHI = 3.75
DOM = 37.5
OZ  = 31.1035

async def fetch_all_spots() -> dict:
    empty = {k: None for k in ('gold','silver','plat','gold_chg','silver_chg','plat_chg','gold_pct','silver_pct','plat_pct')}
    try:
        headers = {'User-Agent':'Mozilla/5.0','Content-Type':'application/json','Origin':'https://www.tradingview.com','Referer':'https://www.tradingview.com/'}
        body    = {'symbols':{'tickers':['TVC:GOLD','TVC:SILVER','TVC:PLATINUM'],'query':{'types':[]}},'columns':['close','change_abs','change']}
        async with httpx.AsyncClient(timeout=8.0) as client:
            r    = await client.post('https://scanner.tradingview.com/global/scan', json=body, headers=headers)
            rows = {item['s']: item['d'] for item in r.json().get('data', [])}
        def v(k): d = rows.get(k, [None,None,None]); return d if len(d)==3 else [None,None,None]
        gd, sd, pd = v('TVC:GOLD'), v('TVC:SILVER'), v('TVC:PLATINUM')
        return {'gold':gd[0],'silver':sd[0],'plat':pd[0],'gold_chg':gd[1],'silver_chg':sd[1],'plat_chg':pd[1],'gold_pct':gd[2],'silver_pct':sd[2],'plat_pct':pd[2]}
    except Exception as e:
        logger.warning(f'fetch_all_spots: {e}')
        return empty

def fmt_price(usd, label: str, emoji: str, chg=None, pct=None) -> str:
    if usd is None:
        return f'{emoji} <b>ហាងឆេង{label}</b>\n  ដំឡឹង : N/A\n  ជី        : N/A\n  អោន    : N/A'
    def d(v): return f'${v:,.2f}'
    arrow = ''
    if chg is not None:
        arrow = f' {"🔺" if chg >= 0 else "🔻"}{abs(chg):.2f} ({abs(pct or 0):.2f}%)'
    return (
        f'{emoji} <b>ហាងឆេង{label}</b>{arrow}\n'
        f'  ដំឡឹង : <b>{d(usd * DOM / OZ)}</b>\n'
        f'  ជី        : <b>{d(usd * CHI / OZ)}</b>\n'
        f'  អោន    : <b>{d(usd)}</b>'
    )

# ── Helpers ────────────────────────────────────────────────────────────────────
async def download_file(client: Client, file_id: str) -> bytes:
    result = await client.download_media(file_id, in_memory=True)
    return bytes(result.getbuffer()) if hasattr(result, 'getbuffer') else result.read()

async def edit_or_send(client: Client, sess: UserSession, cid: int, text: str, markup=None):
    if sess.mid:
        try:
            await client.edit_message_text(cid, sess.mid, text, reply_markup=markup, parse_mode=ParseMode.HTML)
            return
        except Exception:
            pass
    msg = await client.send_message(cid, text, reply_markup=markup, parse_mode=ParseMode.HTML)
    save_msg(sess, cid, msg.id)

async def _send(client: Client, sess: UserSession, cid: int, text: str, **kwargs):
    return await client.send_message(cid, text, **kwargs)

async def _send_doc(client: Client, sess: UserSession, cid: int, doc, **kwargs):
    return await client.send_document(cid, doc, **kwargs)

async def safe_delete(client: Client, cid: int, mid: int):
    try:
        await client.delete_messages(cid, mid)
    except Exception:
        pass

# ── Keyboards ──────────────────────────────────────────────────────────────────
def main_kb() -> InlineKeyboardMarkup:
    return mkb([
        [InlineKeyboardButton('រចនាបថអក្សរ', callback_data='style', icon_custom_emoji_id='5197269100878907942')],
        [InlineKeyboardButton('បំប្លែង PDF', callback_data='doc', icon_custom_emoji_id='5838982342122674517'),
         InlineKeyboardButton('បង្កើត QR Code', callback_data='qr', icon_custom_emoji_id='5440410042773824003')],
        [InlineKeyboardButton('Remove BG', callback_data='rmbg', icon_custom_emoji_id='5395663879483181935'),
         InlineKeyboardButton('ហាងឆេងមាស', callback_data='gold', icon_custom_emoji_id='5429651785352501917')],
        [InlineKeyboardButton('Email Temporary', callback_data='email', icon_custom_emoji_id='6271565748754190308')],
        [InlineKeyboardButton('🎙 បង្កើតសំឡេង Ai', url='http://t.me/Text2Voice2026bot')],
        [InlineKeyboardButton('Donate', callback_data='donate', icon_custom_emoji_id='5897474556834091884')],
    ])

def cancel_kb(data: str) -> InlineKeyboardMarkup:
    return mkb([[InlineKeyboardButton('Back', callback_data=data, icon_custom_emoji_id='5877629862306385808')]])

def pdf_kb(n: int, name=None) -> InlineKeyboardMarkup:
    lbl = f'✅ បង្កើត PDF ({n} រូប)' + (f' 📄 "{name}"' if name else '')
    return mkb([
        [ikb(lbl, 'pdf_build')],
        [ikb('✏️ ប្តូរឈ្មោះ', 'pdf_rename')],
        [InlineKeyboardButton('Back', callback_data='doc', icon_custom_emoji_id='5877629862306385808')],
    ])

# ── PDF: images → PDF ──────────────────────────────────────────────────────────
def images_to_pdf(photos: list) -> bytes:
    from PIL import Image
    imgs = [Image.open(io.BytesIO(b)).convert('RGB') for b in photos]
    buf = io.BytesIO()
    imgs[0].save(buf, format='PDF', save_all=True, append_images=imgs[1:])
    return buf.getvalue()

# ── PDF: PDF → images ──────────────────────────────────────────────────────────
def pdf_to_images(pdf_bytes: bytes, fmt: str = 'PNG') -> list:
    import fitz
    doc    = fitz.open(stream=pdf_bytes, filetype='pdf')
    result = []
    for page in doc:
        pix = page.get_pixmap(dpi=150)
        result.append(pix.tobytes('png' if fmt == 'PNG' else 'jpeg'))
    doc.close()
    return result

# ── QR: generate ──────────────────────────────────────────────────────────────
def create_qr(text: str) -> bytes:
    import qrcode
    from PIL import Image
    for ec in [qrcode.constants.ERROR_CORRECT_H, qrcode.constants.ERROR_CORRECT_Q,
               qrcode.constants.ERROR_CORRECT_M, qrcode.constants.ERROR_CORRECT_L]:
        try:
            qr = qrcode.QRCode(error_correction=ec, box_size=10, border=1)
            qr.add_data(text)
            qr.make(fit=True)
            img = qr.make_image(fill_color='black', back_color='white').convert('RGB')
            img = img.resize((2048, 2048), Image.NEAREST)
            buf = io.BytesIO()
            img.save(buf, format='PNG', compress_level=1)
            return buf.getvalue()
        except Exception:
            continue
    raise ValueError('Cannot generate QR')

# ── Remove Background ──────────────────────────────────────────────────────────
async def rmbg_account() -> dict:
    api_key = os.environ['REMOVE_BG_API_KEY']
    async with httpx.AsyncClient(timeout=15.0) as client:
        r = await client.get(
            'https://api.remove.bg/v1.0/account',
            headers={'X-Api-Key': api_key},
        )
    if r.status_code == 200:
        data = r.json().get('data', {})
        attrs = data.get('attributes', {})
        credits = attrs.get('credits', {})
        return {
            'total_credits': credits.get('total', 0),
            'subscription':  credits.get('subscription', 0),
            'payg':          credits.get('payg', 0),
            'free_calls':    attrs.get('api', {}).get('free_calls', 0),
            'sizes':         attrs.get('api', {}).get('sizes', 'auto'),
        }
    return {}

async def remove_bg(image_bytes: bytes) -> tuple:
    api_key = os.environ['REMOVE_BG_API_KEY']
    async with httpx.AsyncClient(timeout=60.0) as client:
        r = await client.post(
            'https://api.remove.bg/v1.0/removebg',
            files={'image_file': ('image.png', image_bytes, 'image/png')},
            data={'size': 'auto'},
            headers={'X-Api-Key': api_key},
        )
    if r.status_code == 200:
        charged = r.headers.get('x-credits-charged',
                  r.headers.get('X-Credits-Charged', '0'))
        return r.content, charged
    raise RuntimeError(f'remove.bg error {r.status_code}: {r.text}')

# ── QR: scan ──────────────────────────────────────────────────────────────────
def scan_qr(image_bytes: bytes) -> list:
    import cv2
    import numpy as np
    arr = np.frombuffer(image_bytes, np.uint8)
    img = cv2.imdecode(arr, cv2.IMREAD_COLOR)
    detector = cv2.QRCodeDetector()
    data, _, _ = detector.detectAndDecode(img)
    return [data] if data else []


# ══════════════════════════════════════════════════════════════════════════════
# SECTION: DROPMAIL API
# ══════════════════════════════════════════════════════════════════════════════

def _dm_get_url() -> str:
    token = os.environ.get("DROPMAIL_API_TOKEN", "")
    return f"https://dropmail.me/api/graphql/{token}"

def _dm_gql(query: str, variables: Optional[dict] = None, uid: Optional[int] = None) -> dict:
    payload = {"query": query}
    if variables:
        payload["variables"] = variables
    try:
        resp = requests.post(_dm_get_url(), json=payload, timeout=8)
        if resp.status_code == 503:
            _dm_mark_503()
            resp.raise_for_status()
        resp.raise_for_status()
    except requests.exceptions.HTTPError as e:
        if "503" in str(e):
            _dm_mark_503()
        raise
    return resp.json()

def dm_create_session(uid: int) -> Optional[dict]:
    data = _dm_gql("""
    mutation {
        introduceSession {
            id
            expiresAt
            addresses { id address restoreKey }
        }
    }
    """, uid=uid)
    session = data.get("data", {}).get("introduceSession")
    if not session:
        return None
    addr = session["addresses"][0] if session.get("addresses") else {}
    return {
        "session_id": session["id"],
        "email":       addr.get("address"),
        "address_id":  addr.get("id"),
        "restore_key": addr.get("restoreKey"),
    }

def dm_check_session(session_id: str, uid: int) -> Optional[dict]:
    data = _dm_gql("""
    query Check($id: ID!) {
        session(id: $id) {
            id
            expiresAt
            addresses { id address restoreKey }
        }
    }
    """, {"id": session_id}, uid=uid)
    session = (data.get("data") or {}).get("session")
    if not session:
        return None
    addr = session["addresses"][0] if session.get("addresses") else {}
    return {
        "session_id":  session["id"],
        "expires_at":  session.get("expiresAt"),
        "email":       addr.get("address"),
        "address_id":  addr.get("id"),
        "restore_key": addr.get("restoreKey"),
    }

def dm_find_user_sessions(uid: int) -> list:
    data = _dm_gql("""
    {
        sessions {
            id
            expiresAt
            addresses { id address restoreKey }
        }
    }
    """, uid=uid)
    sessions = (data.get("data") or {}).get("sessions") or []
    result = []
    for session in sessions:
        for addr in session.get("addresses") or []:
            result.append({
                "session_id":  session["id"],
                "expires_at":  session.get("expiresAt"),
                "email":       addr["address"],
                "address_id":  addr.get("id"),
                "restore_key": addr.get("restoreKey"),
            })
    return result

def dm_find_session_by_address(email_address: str, uid: int) -> Optional[dict]:
    for s in dm_find_user_sessions(uid):
        if s.get("email") == email_address:
            return s
    return None

def dm_restore_session(mail_address: str, restore_key: str, uid: int) -> Optional[dict]:
    data = _dm_gql('mutation { introduceSession(input: { withAddress: false }) { id } }', uid=uid)
    new_id = (data.get("data", {}).get("introduceSession") or {}).get("id")
    if not new_id:
        logger.warning('dm_restore_session: could not create blank session')
        return None
    r = _dm_gql("""
    mutation Restore($mailAddress: String!, $restoreKey: String!, $sessionId: ID!) {
        restoreAddress(input: { mailAddress: $mailAddress, restoreKey: $restoreKey, sessionId: $sessionId }) {
            id address restoreKey
        }
    }
    """, {"mailAddress": mail_address, "restoreKey": restore_key, "sessionId": new_id}, uid=uid)
    errors = r.get("errors") or []
    for e in errors:
        msg  = e.get("message", "")
        code = (e.get("extensions") or {}).get("code", "")
        logger.warning(f'dm_restore_session error: code={code} msg={msg}')
        if msg == "already_in_use" or code == "already_in_use":
            return {"already_in_use": True}
    addr = r.get("data", {}).get("restoreAddress")
    if not addr:
        return None
    return {
        "session_id":  new_id,
        "email":       addr.get("address"),
        "address_id":  addr.get("id"),
        "restore_key": addr.get("restoreKey"),
    }

def dm_delete_address(address_id: str, uid: int) -> bool:
    try:
        data = _dm_gql('mutation Delete($a: ID!) { deleteAddress(input: { addressId: $a }) }',
                        {"a": address_id}, uid=uid)
        return bool(data.get("data", {}).get("deleteAddress"))
    except Exception:
        return False

def dm_get_new_mails(session_id: str, uid: int, after_mail_id: Optional[str] = None):
    if after_mail_id:
        query = """
        query GetMails($id: ID!, $mailId: ID!) {
            session(id: $id) {
                mailsAfterId(mailId: $mailId) { id fromAddr toAddr headerSubject text receivedAt }
            }
        }
        """
        variables = {"id": session_id, "mailId": after_mail_id}
    else:
        query = """
        query GetMails($id: ID!) {
            session(id: $id) {
                mails { id fromAddr toAddr headerSubject text receivedAt }
            }
        }
        """
        variables = {"id": session_id}
    data = _dm_gql(query, variables, uid=uid)
    session_data = data.get("data", {}).get("session")
    if session_data is None:
        return None
    return session_data.get("mailsAfterId") or session_data.get("mails") or []


# ══════════════════════════════════════════════════════════════════════════════
# SECTION: EMAIL MODULE
# ══════════════════════════════════════════════════════════════════════════════

DATABASE_URL      = os.environ.get("DATABASE_URL", "")
EM_POLL_INTERVAL  = 3
EM_RESTORE_INTERVAL = 600

# Circuit breaker: when DropMail returns 503, pause all loops for 60s
_dm_503_backoff_until: float = 0.0

def _dm_mark_503():
    global _dm_503_backoff_until
    _dm_503_backoff_until = time.time() + 60

def _dm_is_backoff() -> bool:
    return time.time() < _dm_503_backoff_until

# ── DB Connection Pool (shared by email + order modules) ──────────────────────
import psycopg2.pool as _pg_pool

_db_pool: Optional[_pg_pool.ThreadedConnectionPool] = None
_db_pool_lock = threading.Lock()

def _get_pool() -> _pg_pool.ThreadedConnectionPool:
    global _db_pool
    if _db_pool is None:
        with _db_pool_lock:
            if _db_pool is None:
                if not DATABASE_URL:
                    raise RuntimeError("DATABASE_URL is not set")
                _db_pool = _pg_pool.ThreadedConnectionPool(2, 10, DATABASE_URL)
    return _db_pool

class _PoolConn:
    """Context manager that borrows a connection from the pool and returns it."""
    def __init__(self, dict_cursor: bool = False):
        self._dict_cursor = dict_cursor
        self._conn = None

    def __enter__(self):
        self._conn = _get_pool().getconn()
        return self._conn

    def __exit__(self, exc_type, exc_val, exc_tb):
        if exc_type is None:
            try:
                self._conn.commit()
            except Exception:
                pass
        else:
            try:
                self._conn.rollback()
            except Exception:
                pass
        _get_pool().putconn(self._conn)
        return False

def _em_conn():
    return _PoolConn()


def em_init_db():
    with _em_conn() as c:
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


def em_upsert_session(telegram_user_id, telegram_username, telegram_first_name,
                      dropmail_session_id, email_address,
                      address_id=None, restore_key=None):
    with _em_conn() as c:
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


def em_get_session(telegram_user_id) -> Optional[dict]:
    with _em_conn() as c:
        with c.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                "SELECT * FROM email_sessions WHERE telegram_user_id=%s",
                (telegram_user_id,))
            row = cur.fetchone()
            return dict(row) if row else None


def em_update_session_after_restore(telegram_user_id, new_session_id,
                                    new_address_id, new_restore_key):
    with _em_conn() as c:
        with c.cursor() as cur:
            cur.execute("""
            UPDATE email_sessions
               SET dropmail_session_id=%s, address_id=%s, restore_key=%s,
                   is_active=TRUE, updated_at=NOW()
             WHERE telegram_user_id=%s
            """, (new_session_id, new_address_id, new_restore_key, telegram_user_id))
        c.commit()


def em_deactivate_session(telegram_user_id):
    with _em_conn() as c:
        with c.cursor() as cur:
            cur.execute("""
            UPDATE email_sessions
               SET is_active=FALSE, dropmail_session_id=NULL,
                   email_address=NULL, address_id=NULL, restore_key=NULL,
                   updated_at=NOW()
             WHERE telegram_user_id=%s
            """, (telegram_user_id,))
        c.commit()


def em_add_email_to_history(telegram_user_id, email_address,
                             dropmail_session_id=None, address_id=None,
                             restore_key=None):
    with _em_conn() as c:
        with c.cursor() as cur:
            cur.execute("""
            INSERT INTO email_history
              (telegram_user_id, email_address, dropmail_session_id,
               address_id, restore_key)
            VALUES (%s,%s,%s,%s,%s)
            """, (telegram_user_id, email_address, dropmail_session_id,
                  address_id, restore_key))
        c.commit()


def em_get_email_history(telegram_user_id) -> list:
    with _em_conn() as c:
        with c.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("""
            SELECT email_address FROM email_history
             WHERE telegram_user_id=%s ORDER BY created_at DESC
            """, (telegram_user_id,))
            return [r["email_address"] for r in cur.fetchall()]


def em_get_user_history_entries(telegram_user_id) -> list:
    with _em_conn() as c:
        with c.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("""
            SELECT * FROM email_history
             WHERE telegram_user_id=%s ORDER BY created_at DESC
            """, (telegram_user_id,))
            return [dict(r) for r in cur.fetchall()]


def em_get_all_history_entries() -> list:
    with _em_conn() as c:
        with c.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                "SELECT * FROM email_history WHERE restore_key IS NOT NULL")
            return [dict(r) for r in cur.fetchall()]


def em_clear_restore_key(history_id: int):
    """Mark an email history entry as permanently unrestorable (already_in_use)."""
    with _em_conn() as c:
        with c.cursor() as cur:
            cur.execute(
                "UPDATE email_history SET restore_key=NULL, dropmail_session_id=NULL WHERE id=%s",
                (history_id,))
        c.commit()


def em_get_history_entry_by_email(telegram_user_id, email_address) -> Optional[dict]:
    with _em_conn() as c:
        with c.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("""
            SELECT * FROM email_history
             WHERE telegram_user_id=%s AND email_address=%s
             ORDER BY created_at DESC LIMIT 1
            """, (telegram_user_id, email_address))
            row = cur.fetchone()
            return dict(row) if row else None


def em_update_history_session(history_id, new_session_id, new_address_id, new_restore_key):
    with _em_conn() as c:
        with c.cursor() as cur:
            cur.execute("""
            UPDATE email_history
               SET dropmail_session_id=%s, address_id=%s,
                   restore_key=%s, last_mail_id=NULL
             WHERE id=%s
            """, (new_session_id, new_address_id, new_restore_key, history_id))
        c.commit()


def em_update_history_last_mail_id(history_id, mail_id):
    with _em_conn() as c:
        with c.cursor() as cur:
            cur.execute(
                "UPDATE email_history SET last_mail_id=%s WHERE id=%s",
                (mail_id, history_id))
        c.commit()


def em_remove_email_from_history(history_id):
    with _em_conn() as c:
        with c.cursor() as cur:
            cur.execute("DELETE FROM email_history WHERE id=%s", (history_id,))
        c.commit()


# ── Email Keyboards ────────────────────────────────────────────────────────────
def _em_back_kb() -> InlineKeyboardMarkup:
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


# ── Email Handlers ─────────────────────────────────────────────────────────────
async def handle_email_open(edit_fn, user_id: int):
    loop = asyncio.get_running_loop()
    session = await loop.run_in_executor(None, em_get_session, user_id)
    if session and session.get("is_active") and session.get("email_address"):
        addr = session["email_address"]
        await edit_fn(
            "📧 <b>Email បណ្ដោះអាសន្ន</b>\n\n"
            "👆 ចុចលើអ៊ីម៉ែលខាងលើដើម្បីចម្លង",
            email_active_kb(addr))
    else:
        await edit_fn("📧 <b>Email បណ្ដោះអាសន្ន</b>", email_empty_kb())


async def handle_new_email(edit_fn, user, user_id: int):
    await edit_fn("⏳ <b>កំពុងបង្កើត Email...</b>")
    loop = asyncio.get_running_loop()
    try:
        result = await loop.run_in_executor(None, dm_create_session, user_id)
    except Exception as e:
        await edit_fn(f"❌ បង្កើតមិនបានទេ: {e}", email_empty_kb())
        return
    if not result:
        await edit_fn(
            "❌ មិនអាចបង្កើត session បានទេ។ សូមព្យាយាមម្ដងទៀត។",
            email_empty_kb())
        return

    def _persist():
        em_upsert_session(
            telegram_user_id=user_id,
            telegram_username=getattr(user, "username", None),
            telegram_first_name=getattr(user, "first_name", None),
            dropmail_session_id=result["session_id"],
            email_address=result["email"],
            address_id=result["address_id"],
            restore_key=result["restore_key"],
        )
        em_add_email_to_history(
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


async def handle_inbox(edit_fn, user_id: int):
    loop = asyncio.get_running_loop()
    session = await loop.run_in_executor(None, em_get_session, user_id)
    if not session or not session.get("is_active") or not session.get("dropmail_session_id"):
        await edit_fn(
            "❌ អ្នកមិនមាន session ដែលសកម្មទេ។\n\n"
            "ចុច <b>✉️ Email ថ្មី</b> ដើម្បីបង្កើត។",
            email_empty_kb())
        return
    try:
        mails = await loop.run_in_executor(
            None, dm_get_new_mails,
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


async def handle_list(edit_fn, user_id: int):
    loop = asyncio.get_running_loop()
    history = await loop.run_in_executor(None, em_get_email_history, user_id)
    if not history:
        await edit_fn(
            "📭 គ្មាន email ណាទេ។\n\nចុច ✉️ Email ថ្មី ដើម្បីបង្កើត។",
            email_empty_kb())
        return
    lines = "\n".join(f"{i+1}- <code>{e}</code>" for i, e in enumerate(history))
    text  = f"📧 <b>Email {len(history)}</b>\n\n{lines}"
    await edit_fn(text, _em_back_kb())


async def handle_delete_picker(edit_fn, user_id: int):
    loop = asyncio.get_running_loop()
    entries = await loop.run_in_executor(None, em_get_user_history_entries, user_id)
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


async def handle_delete_current(edit_fn, user_id: int):
    loop = asyncio.get_running_loop()
    session = await loop.run_in_executor(None, em_get_session, user_id)
    address_id = session.get("address_id") if session else None
    if address_id:
        await loop.run_in_executor(None, dm_delete_address, address_id, user_id)
    await loop.run_in_executor(None, em_deactivate_session, user_id)
    await edit_fn(
        "🗑 <b>អ៊ីម៉ែលត្រូវបានលុបចោលហើយ។</b>\n\n"
        "ចុច <b>✉️ Email ថ្មី</b> ដើម្បីបង្កើតថ្មី។",
        email_empty_kb())


async def handle_delete_one(edit_fn, user_id: int, email_to_delete: str):
    loop = asyncio.get_running_loop()
    entry = await loop.run_in_executor(
        None, em_get_history_entry_by_email, user_id, email_to_delete)
    if not entry:
        await edit_fn(
            f"❌ រកមិនឃើញ <code>{email_to_delete}</code>",
            email_empty_kb())
        return
    if entry.get("address_id"):
        await loop.run_in_executor(
            None, dm_delete_address, entry["address_id"], user_id)
    await loop.run_in_executor(None, em_remove_email_from_history, entry["id"])
    session = await loop.run_in_executor(None, em_get_session, user_id)
    if session and session.get("email_address") == email_to_delete:
        await loop.run_in_executor(None, em_deactivate_session, user_id)
    await edit_fn(
        f"🗑 លុប <code>{email_to_delete}</code> បានសម្រេច។",
        email_empty_kb())


async def handle_email_callback(client, query, edit_fn) -> bool:
    d    = query.data or ""
    uid  = query.from_user.id
    user = query.from_user
    if d == "email":
        await handle_email_open(edit_fn, uid); return True
    if d == "em_new":
        await handle_new_email(edit_fn, user, uid); return True
    if d == "em_inbox":
        await handle_inbox(edit_fn, uid); return True
    if d == "em_list":
        await handle_list(edit_fn, uid); return True
    if d == "em_del":
        await handle_delete_picker(edit_fn, uid); return True
    if d == "em_del_cur":
        await handle_delete_current(edit_fn, uid); return True
    if d.startswith("em_del_one:"):
        addr = d[len("em_del_one:"):]
        await handle_delete_one(edit_fn, uid, addr); return True
    return False


# ── Email Background Tasks ─────────────────────────────────────────────────────
async def _em_poll_one(entry: dict):
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
            None, dm_get_new_mails, session_id, user_id, last_mail_id)
    except Exception as e:
        logger.warning(f"Poll error [{email_address}]: {e}")
        return
    if mails is None:
        logger.info(f"Restoring [{email_address}] for user {user_id}...")
        try:
            restored = await loop.run_in_executor(
                None, dm_restore_session, email_address, restore_key, user_id)
        except Exception as e:
            logger.warning(f"Restore failed [{email_address}]: {e}")
            return
        if restored:
            if restored.get("already_in_use"):
                logger.info(f"Poll restore [{email_address}]: already_in_use → clearing restore_key")
                await loop.run_in_executor(None, em_clear_restore_key, history_id)
            else:
                def _persist_restore():
                    em_update_history_session(
                        history_id,
                        new_session_id=restored["session_id"],
                        new_address_id=restored.get("address_id"),
                        new_restore_key=restored.get("restore_key"),
                    )
                    cur_sess = em_get_session(user_id)
                    if cur_sess and cur_sess.get("email_address") == email_address:
                        em_update_session_after_restore(
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
        try:
            await app.send_message(
                chat_id=user_id, text=text, parse_mode=ParseMode.HTML)
        except Exception as e:
            logger.warning(f"Failed to notify user {user_id}: {e}")
        newest_id = mail_id
    if newest_id:
        await loop.run_in_executor(
            None, em_update_history_last_mail_id, history_id, newest_id)


async def _em_restore_one(entry: dict):
    history_id    = entry["id"]
    user_id       = entry["telegram_user_id"]
    email_address = entry["email_address"]
    restore_key   = entry.get("restore_key")
    if not restore_key:
        return
    loop = asyncio.get_running_loop()
    try:
        restored = await loop.run_in_executor(
            None, dm_restore_session, email_address, restore_key, user_id)
    except Exception as e:
        if "503" not in str(e):
            logger.warning(f"Proactive restore failed [{email_address}]: {e}")
        return
    if not restored:
        return
    if restored.get("already_in_use"):
        # Session permanently taken — stop retrying forever
        logger.info(f"Restore [{email_address}]: already_in_use → clearing restore_key")
        await loop.run_in_executor(None, em_clear_restore_key, history_id)
        return
    def _persist():
        em_update_history_session(
            history_id,
            new_session_id=restored["session_id"],
            new_address_id=restored.get("address_id"),
            new_restore_key=restored.get("restore_key"),
        )
        cur_sess = em_get_session(user_id)
        if cur_sess and cur_sess.get("email_address") == email_address:
            em_update_session_after_restore(
                telegram_user_id=user_id,
                new_session_id=restored["session_id"],
                new_address_id=restored.get("address_id"),
                new_restore_key=restored.get("restore_key"),
            )
    await loop.run_in_executor(None, _persist)
    logger.info(f"Restore loop: restored [{email_address}]")


async def _em_poll_loop():
    while True:
        try:
            if not _dm_is_backoff():
                loop = asyncio.get_running_loop()
                entries = await loop.run_in_executor(None, em_get_all_history_entries)
                tasks = [_em_poll_one(e) for e in entries if e.get("dropmail_session_id")]
                if tasks:
                    await asyncio.gather(*tasks, return_exceptions=True)
        except Exception as e:
            logger.error(f"poll_loop error: {e}")
        await asyncio.sleep(EM_POLL_INTERVAL)


async def _em_restore_loop():
    while True:
        await asyncio.sleep(EM_RESTORE_INTERVAL)
        try:
            if _dm_is_backoff():
                logger.info("restore_loop: DropMail backoff active, skipping")
                continue
            loop = asyncio.get_running_loop()
            entries = await loop.run_in_executor(None, em_get_all_history_entries)
            tasks = [_em_restore_one(e) for e in entries if not e.get("dropmail_session_id")]
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)
        except Exception as e:
            logger.error(f"restore_loop error: {e}")


# ══════════════════════════════════════════════════════════════════════════════
# SECTION: ORDER MODULE
# ══════════════════════════════════════════════════════════════════════════════

KHPAY_API_KEY    = os.environ.get("KHPAY_API_KEY", "")
KHPAY_BASE_URL   = "https://khpay.site/api/v1"
ORD_POLL_INTERVAL = 10
ORD_POLL_COUNT    = 6
ADMIN_ID: int    = int(os.environ.get("ADMIN_ID", "0") or "0")

CHANNEL_ID       = os.environ.get("TELEGRAM_CHANNEL_ID", "").strip()
PAYMENT_NAME     = "RADY"
MAINTENANCE_MODE = False
EXTRA_ADMIN_IDS: set = set()

_data_lock   = threading.RLock()
_polls_lock  = threading.Lock()
_active_polls: set = set()
_admin_order_msg: dict = {}

order_sessions: dict  = {}
accounts_data: dict   = {"accounts": [], "account_types": {}, "prices": {}}
_data_loaded_ok       = False
_notified_users: set  = set()
_notified_lock        = threading.Lock()


def is_admin(uid) -> bool:
    try:
        uid_int = int(uid)
    except (TypeError, ValueError):
        return False
    return uid_int == ADMIN_ID or uid_int in EXTRA_ADMIN_IDS


# ── Order DB helpers ───────────────────────────────────────────────────────────
def _db_execute(query: str, params=None) -> int:
    with _PoolConn() as conn:
        with conn.cursor() as cur:
            cur.execute(query, params or [])
            return getattr(cur, 'rowcount', 0) or 0


def _db_query(query: str, params=None) -> list:
    with _PoolConn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(query, params or [])
            rows = cur.fetchall()
            return [dict(r) for r in rows] if rows else []


async def _aexec(query: str, params=None) -> int:
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, _db_execute, query, params)


async def _aquery(query: str, params=None) -> list:
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, _db_query, query, params)


def _init_db_sync():
    global PAYMENT_NAME, MAINTENANCE_MODE, EXTRA_ADMIN_IDS, CHANNEL_ID
    try:
        with _PoolConn() as conn:
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
                cur.execute("SELECT COUNT(*) FROM order_accounts")
                if (cur.fetchone() or (0,))[0] == 0:
                    cur.execute("INSERT INTO order_accounts (data) VALUES (%s)",
                                [json.dumps({"accounts": [], "account_types": {}, "prices": {}})])
                cur.execute("SELECT COUNT(*) FROM order_sessions_store")
                if (cur.fetchone() or (0,))[0] == 0:
                    cur.execute("INSERT INTO order_sessions_store (data) VALUES (%s)", [json.dumps({})])
        logger.info("Order DB tables ready")
    except Exception as e:
        logger.error(f"DB init failed: {e}")
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


# ── Order Keyboard helpers ─────────────────────────────────────────────────────
def _ikb(text, cb):
    return InlineKeyboardButton(text, callback_data=cb)

def _back_btn(cb='o:home'):
    return InlineKeyboardButton('◀️ ត្រឡប់', callback_data=cb,
                                icon_custom_emoji_id='5877629862306385808')

_KB_ADD_ACC   = '➕ បន្ថែម Account'
_KB_DEL_TYPE  = '🗑 លុបប្រភេទ'
_KB_BUYERS    = '📋 របាយការណ៍ទិញ'
_KB_USERS     = '👥 អ្នកប្រើ'
_KB_PAYMENT   = '💳 Payment Name'
_KB_CHANNEL   = '📢 Channel ID'
_KB_BAKONG    = '🔑 Bakong Token'
_KB_ADMINS    = '👑 Admins'
_KB_MAINT     = '🛠 Maintenance'
_KB_BROADCAST = '📢 Broadcast'
_ADMIN_KB_LABELS = {
    _KB_ADD_ACC, _KB_DEL_TYPE, _KB_BUYERS, _KB_USERS,
    _KB_PAYMENT, _KB_CHANNEL, _KB_BAKONG, _KB_ADMINS,
    _KB_MAINT, _KB_BROADCAST,
}

def _settings_main_ikb():
    return InlineKeyboardMarkup([
        [_ikb('🛒 ទិញ Account', 'order')],
        [_ikb('➕ បន្ថែម Account', 's:add_acc'),  _ikb('🗑 លុបប្រភេទ', 's:del_type')],
        [_ikb('📋 របាយការណ៍ទិញ', 's:buyers'),    _ikb('👥 អ្នកប្រើ', 's:users')],
        [_ikb('💳 Payment Name', 's:pay'),          _ikb('📢 Channel ID', 's:ch')],
        [_ikb('🔑 Bakong Token', 's:bak'),           _ikb('👑 Admins', 's:adm')],
        [_ikb('🛠 Maintenance', 's:mnt'),            _ikb('📢 Broadcast', 's:broadcast')],
    ])

def _settings_main_rkb():
    return ReplyKeyboardMarkup([
        [KeyboardButton(_KB_ADD_ACC),  KeyboardButton(_KB_DEL_TYPE)],
        [KeyboardButton(_KB_BUYERS),   KeyboardButton(_KB_USERS)],
        [KeyboardButton(_KB_PAYMENT),  KeyboardButton(_KB_CHANNEL)],
        [KeyboardButton(_KB_BAKONG),   KeyboardButton(_KB_ADMINS)],
        [KeyboardButton(_KB_MAINT),    KeyboardButton(_KB_BROADCAST)],
    ], resize_keyboard=True)

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


# ── Order send helper ──────────────────────────────────────────────────────────
async def _osend(chat_id, text, kb=None):
    try:
        return await app.send_message(
            chat_id, text,
            parse_mode=ParseMode.HTML,
            reply_markup=kb,
            link_preview_options=LinkPreviewOptions(is_disabled=True))
    except Exception as e:
        logger.error(f"_osend to {chat_id}: {e}")
        return None

async def _del_msg(chat_id, message_id):
    try:
        await app.delete_messages(chat_id, message_id)
    except Exception:
        pass


# ── Admin settings panel ───────────────────────────────────────────────────────
async def _settings_edit(chat_id, user_id, text, kb=None):
    mid = _admin_order_msg.get(user_id)
    if mid:
        try:
            await app.edit_message_text(
                chat_id, mid, text,
                parse_mode=ParseMode.HTML, reply_markup=kb,
                link_preview_options=LinkPreviewOptions(is_disabled=True))
            return
        except Exception:
            pass
    msg = await _osend(chat_id, text, kb)
    if msg:
        _admin_order_msg[user_id] = msg.id

async def send_admin_settings(client, chat_id, user_id):
    mid = _admin_order_msg.pop(user_id, None)
    if mid:
        await _del_msg(chat_id, mid)
    msg = await _osend(chat_id,
        "<b>⚙️ ការកំណត់ Admin</b>\n\nសូមជ្រើសរើសប្រតិបត្តិការ៖",
        _settings_main_rkb())
    if msg:
        _admin_order_msg[user_id] = msg.id

async def _show_pay_panel(chat_id, user_id):
    await _settings_edit(chat_id, user_id,
        f"💳 <b>Payment Name</b>\n\nបច្ចុប្បន្ន: <b>{html.escape(PAYMENT_NAME or '(មិនទាន់)')}</b>",
        _settings_pay_ikb())

async def _show_bak_panel(chat_id, user_id):
    await _settings_edit(chat_id, user_id,
        f"🔑 <b>Bakong Token/Note</b>\n\nបច្ចុប្បន្ន: <b>{html.escape(PAYMENT_NAME or '(មិនទាន់)')}</b>",
        _settings_bak_ikb())

async def _show_ch_panel(chat_id, user_id):
    ch = CHANNEL_ID or '(មិនទាន់)'
    await _settings_edit(chat_id, user_id,
        f"📢 <b>Channel ID</b>\n\nបច្ចុប្បន្ន: <code>{html.escape(ch)}</code>",
        _settings_ch_ikb())

async def _show_adm_panel(chat_id, user_id):
    lines = [f"👑 <b>Admins</b>\n\n🔒 Primary: <code>{ADMIN_ID}</code>"]
    for uid in sorted(EXTRA_ADMIN_IDS):
        lines.append(f"👤 <code>{uid}</code>")
    await _settings_edit(chat_id, user_id, "\n".join(lines), _settings_adm_ikb())

async def _show_mnt_panel(chat_id, user_id):
    status = "🔴 <b>MAINTENANCE</b>" if MAINTENANCE_MODE else "🟢 <b>ដំណើរការធម្មតា</b>"
    await _settings_edit(chat_id, user_id,
        f"🛠 <b>Maintenance Mode</b>\n\nស្ថានភាព: {status}",
        _settings_mnt_ikb())

async def _show_del_type_panel(chat_id, user_id):
    with _data_lock:
        types = list(accounts_data.get('account_types', {}).keys())
    if not types:
        await _settings_edit(chat_id, user_id,
            "📭 <b>គ្មានប្រភេទ Account</b>",
            InlineKeyboardMarkup([[_back_btn('s:main')]]))
        return
    rows = [[_ikb(f"🗑 {t}", f"dts:{_type_cb_id(t)}")] for t in types]
    rows.append([_back_btn('s:main')])
    await _settings_edit(chat_id, user_id,
        "🗑 <b>ជ្រើសរើសប្រភេទដើម្បីលុប:</b>",
        InlineKeyboardMarkup(rows))

async def _prompt_admin_input(chat_id, user_id, key, prompt_text, return_menu='main'):
    with _data_lock:
        order_sessions[user_id] = {
            'state': f'admin_input:{key}',
            'settings_return': return_menu,
        }
    _save_sessions_bg()
    await _settings_edit(chat_id, user_id, prompt_text, _settings_cancel_ikb())

async def _show_users_panel(chat_id):
    loop = asyncio.get_running_loop()
    rows = await loop.run_in_executor(None, _db_query,
        "SELECT * FROM order_known_users ORDER BY last_seen DESC")
    if not rows:
        await _osend(chat_id, "📭 <b>មិនមានអ្នកប្រើប្រាស់ណាមួយ!</b>")
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
        await app.send_document(chat_id, buf, caption=f"👥 អ្នកប្រើប្រាស់ — {total} នាក់",
                                file_name=fname)
    except Exception as e:
        logger.error(f"send users doc: {e}")
        await _osend(chat_id, f"❌ <b>Error:</b> <code>{html.escape(str(e))}</code>")

async def _export_buyers_panel(chat_id):
    loop = asyncio.get_running_loop()
    rows = await loop.run_in_executor(None, _db_query, """
        SELECT ph.user_id, ph.account_type, ph.accounts,
               ku.first_name, ku.last_name, ku.username
        FROM order_purchase_history ph
        LEFT JOIN order_known_users ku ON ku.user_id = ph.user_id
        ORDER BY ph.user_id, ph.purchased_at DESC""")
    if not rows:
        await _osend(chat_id, "មិនមានទិន្នន័យ​ទិញ​ណាមួយ​ទេ​")
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
        await app.send_document(chat_id, buf,
            caption=f"📋 របាយការណ៍ទិញ — {len(grouped)} នាក់, {total_coupons} គូប៉ុង",
            file_name=fname)
    except Exception as e:
        logger.error(f"send buyers doc: {e}")
        await _osend(chat_id, f"❌ <b>Error:</b> <code>{html.escape(str(e))}</code>")

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


# ── Donate UI ─────────────────────────────────────────────────────────────────
DONATE_PRESETS = [1, 2, 5, 10, 20, 50]
DONATE_STARS_PRESETS = [10, 25, 50, 100, 250, 500]
MEDALS = ['🥇', '🥈', '🥉', '4️⃣', '5️⃣', '6️⃣', '7️⃣', '8️⃣', '9️⃣', '🔟']

def _donate_top_ikb():
    return InlineKeyboardMarkup([
        [_ikb('💳 KHPay (Bakong QR)', 'donate_khpay')],
        [_ikb('⭐ Telegram Stars', 'donate_stars')],
        [_ikb('🏆 Top Donation', 'don_top')],
        [InlineKeyboardButton('🏠 ម៉ឺនុយមេ', callback_data='home',
                              icon_custom_emoji_id='5282843764451195532')],
    ])

def _donate_ikb():
    rows = []
    for i in range(0, len(DONATE_PRESETS), 3):
        row = [_ikb(f'${amt}', f'don:{amt}') for amt in DONATE_PRESETS[i:i+3]]
        rows.append(row)
    rows.append([_ikb('✏️ ចំនួនផ្ទាល់ខ្លួន', 'don_custom')])
    rows.append([_ikb('🏆 Top Donation', 'don_top')])
    rows.append([InlineKeyboardButton('⬅️ Back', callback_data='donate',
                                      icon_custom_emoji_id='5877629862306385808')])
    return InlineKeyboardMarkup(rows)

def _donate_stars_ikb():
    rows = []
    for i in range(0, len(DONATE_STARS_PRESETS), 3):
        row = [_ikb(f'⭐ {amt}', f'dons:{amt}') for amt in DONATE_STARS_PRESETS[i:i+3]]
        rows.append(row)
    rows.append([InlineKeyboardButton('⬅️ Back', callback_data='donate',
                                      icon_custom_emoji_id='5877629862306385808')])
    return InlineKeyboardMarkup(rows)

def _donate_cancel_ikb():
    return InlineKeyboardMarkup([[_ikb('🚫 បោះបង់', 'don_cancel')]])

async def send_donate_menu(chat_id, user_id, query=None):
    loop = asyncio.get_running_loop()
    total, times = await loop.run_in_executor(None, _get_user_donation_total_sync, user_id)
    my_line = ''
    if total > 0:
        my_line = f'\n\n💝 <b>អ្នកបានបរិច្ចាគ:</b> <b>${total:.2f}</b> ({times} ដង)'
    text = (
        '💝 <b>Donate ជូន RADY Bot</b>\n\n'
        'ការបរិច្ចាគរបស់អ្នកជួយឱ្យ Bot នេះបន្តដំណើរការ 🙏\n\n'
        '💳 <b>KHPay</b> — Bakong QR (USD)\n'
        '⭐ <b>Telegram Stars</b> — native Stars' + my_line
    )
    if query:
        try:
            await query.message.edit_text(text, parse_mode=ParseMode.HTML,
                                          reply_markup=_donate_top_ikb())
            return
        except Exception:
            pass
    await _osend(chat_id, text, kb=_donate_top_ikb())

async def send_donate_stars_menu(chat_id, user_id, query=None):
    text = (
        '⭐ <b>Donate តាម Telegram Stars</b>\n\n'
        'ជ្រើសរើសចំនួន Stars ដែលចង់ Donate:\n\n'
        '<i>Stars ត្រូវបាន charge ពី Telegram wallet របស់អ្នក</i>'
    )
    if query:
        try:
            await query.message.edit_text(text, parse_mode=ParseMode.HTML,
                                          reply_markup=_donate_stars_ikb())
            return
        except Exception:
            pass
    await _osend(chat_id, text, kb=_donate_stars_ikb())

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
    await _osend(chat_id, thank_text, kb=kb)
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
        await _osend(ADMIN_ID, notif)
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
        for i in range(ORD_POLL_COUNT):
            await asyncio.sleep(ORD_POLL_INTERVAL)
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
            if i < ORD_POLL_COUNT - 1:
                if status_msg_id:
                    await _del_msg(chat_id, status_msg_id)
                m = await _osend(chat_id, '🔍 រង់ចាំការ Donate...')
                if m:
                    status_msg_id = m.id
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
        loading_msg = await app.send_message(
            chat_id,
            f'⏳ <b>កំពុងបង្កើត QR Donate ${amount:.2f}...</b>',
            parse_mode=ParseMode.HTML)
        img_bytes, txn_id, qr_string = await generate_payment_qr(amount)
        try:
            await loading_msg.delete()
        except Exception:
            pass
        if not img_bytes:
            await _osend(chat_id, f'❌ <b>មានបញ្ហាបង្កើត QR</b>\n\nព្យាយាមម្ដងទៀត')
            with _data_lock:
                order_sessions.pop(user_id, None)
            _save_sessions_bg()
            return
        session['txn_id']     = txn_id
        session['qr_sent_at'] = time.time()
        buf = io.BytesIO(img_bytes); buf.name = 'donate_qr.png'
        caption = (
            f'💝 <b>Donate ${amount:.2f} USD</b>\n\n'
            f'<i>Scan QR ខាងក្រោម ហើយបង់ប្រាក់</i>\n'
            f'⏳ <b>ផុតកំណត់:</b> {ORD_POLL_COUNT * ORD_POLL_INTERVAL}s'
        )
        photo_msg = await app.send_photo(chat_id, buf, caption=caption,
                                         parse_mode=ParseMode.HTML,
                                         reply_markup=_donate_cancel_ikb())
        if photo_msg:
            session['qr_message_id'] = photo_msg.id
        _save_sessions_bg()
        asyncio.create_task(_poll_donation(client, user_id, chat_id, txn_id,
                                           amount, session.get('qr_message_id', 0), session))
    except Exception as e:
        logger.error(f"generate_and_send_donate_qr: {e}")
        try:
            await loading_msg.delete()
        except Exception:
            pass
        await _osend(chat_id, '❌ <b>មានបញ្ហាបង្កើត QR</b>\n\nព្យាយាមម្ដងទៀត')
        with _data_lock:
            order_sessions.pop(user_id, None)
        _save_sessions_bg()


# ── Account selection UI ───────────────────────────────────────────────────────
async def send_account_selection(chat_id):
    with _data_lock:
        types = {t: accs for t, accs in accounts_data.get('account_types', {}).items() if len(accs) > 0}
    if not types:
        await _osend(chat_id, "<i>😔 សូមអភ័យទោស! គ្មានទំនិញក្នុងស្តុក</i>")
        return
    rows = []
    for t, accs in types.items():
        count = len(accs)
        price = accounts_data.get('prices', {}).get(t, 0)
        label = f"ទិញ {t} — ស្តុក {count} · ${price}/ខ"
        rows.append([_ikb(label, f"buy:{_type_cb_id(t)}")])
    rows.append([InlineKeyboardButton('🏠 ម៉ឺនុយមេ', callback_data='home',
                                      icon_custom_emoji_id='5282843764451195532')])
    await _osend(chat_id, "<b>🛒 សូមជ្រើសរើសគូប៉ុងដើម្បីទិញ:</b>",
                kb=InlineKeyboardMarkup(rows))


# ── Payment generation ─────────────────────────────────────────────────────────
async def generate_payment_qr(amount):
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


# ── Deliver accounts ───────────────────────────────────────────────────────────
async def _deliver_accounts(client, chat_id, user_id, session, payment_data=None, user_name=''):
    account_type = session.get('account_type', '')
    quantity     = session.get('quantity', 1)
    with _data_lock:
        if account_type not in accounts_data.get('account_types', {}):
            await _osend(chat_id, f"❌ <b>មានបញ្ហា!</b>\n\nគ្មាន Account ប្រភេទ {account_type}")
            return
        available = accounts_data['account_types'][account_type]
        if len(available) < quantity:
            await _osend(chat_id, f"❌ <b>មានបញ្ហា!</b>\n\nសុំទោស! មានត្រឹមតែ {len(available)} Accounts ក្នុងស្តុក")
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
    await _osend(chat_id, msg, kb=kb)
    await send_account_selection(chat_id)
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
        await _osend(target, admin_msg)
    except Exception as e:
        logger.error(f"admin notify: {e}")


# ── Payment polling task ───────────────────────────────────────────────────────
async def _poll_payment(client, user_id, chat_id, txn_id, amount, qr_msg_id):
    with _polls_lock:
        if user_id in _active_polls:
            return
        _active_polls.add(user_id)
    status_msg_id = None
    try:
        for i in range(ORD_POLL_COUNT):
            await asyncio.sleep(ORD_POLL_INTERVAL)
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
                    await _osend(chat_id,
                        f"❌ <b>ការបង់ប្រាក់ {fail}</b>\n\nជ្រើសរើស Account ម្ដងទៀត")
                    with _data_lock:
                        order_sessions.pop(user_id, None)
                    loop = asyncio.get_running_loop()
                    await loop.run_in_executor(None, _delete_pending_payment_sync, user_id)
                    _save_sessions_bg()
                    return
            except Exception as e:
                logger.error(f"poll check error: {e}")
            if i < ORD_POLL_COUNT - 1:
                if status_msg_id:
                    await _del_msg(chat_id, status_msg_id)
                msg2 = await _osend(chat_id, "🔍 រង់ចាំការបង់ប្រាក់...")
                if msg2:
                    status_msg_id = msg2.id
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
                await _osend(chat_id, f"❌ <b>QR Error (Admin Debug):</b>\n<code>{html.escape(err)}</code>")
            else:
                await _osend(chat_id, "❌ <b>មានបញ្ហាក្នុងការបង្កើត QR Code</b>\n\nសូមព្យាយាមម្តងទៀត")
                await _osend(ADMIN_ID, f"⚠️ QR Error (user {user_id}):\n<code>{html.escape(err)}</code>")
            with _data_lock:
                order_sessions.pop(user_id, None)
            _save_sessions_bg()
            return
        session['txn_id']     = txn_id
        session['qr_sent_at'] = time.time()
        buf = io.BytesIO(img_bytes); buf.name = "payment_qr.png"
        photo_msg = await app.send_photo(chat_id, buf, reply_markup=_check_payment_ikb())
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
        await _osend(chat_id, "❌ <b>មានបញ្ហាក្នុងការបង្កើត QR Code</b>\n\nសូមព្យាយាមម្តងទៀត")
        with _data_lock:
            order_sessions.pop(user_id, None)
        _save_sessions_bg()


# ── New user notification ──────────────────────────────────────────────────────
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
        await _osend(ADMIN_ID, msg)
    except Exception as e:
        logger.error(f"notify_new_user send: {e}")
    loop = asyncio.get_running_loop()
    await loop.run_in_executor(None, _upsert_known_user_sync, user_id, first, last, username)


# ── E-GetS Verification Relay ──────────────────────────────────────────────────
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
                sent = await _osend(buyer_id, fmsg, kb=kb)
                if sent:
                    logger.info(f"Sent E-GetS code for {email} to {buyer_id}")
            except Exception as e:
                logger.warning(f"E-GetS relay to {buyer_id}: {e}")
    except Exception as e:
        logger.error(f"handle_channel_post: {e}")


# ── Broadcast ──────────────────────────────────────────────────────────────────
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
                    await app.copy_message(uid, from_chat_id, source_message_id)
                else:
                    await app.forward_messages(uid, from_chat_id, source_message_id)
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
        await _osend(from_chat_id, summary,
                    kb=InlineKeyboardMarkup([[_back_btn('s:main')]]))
    except Exception as e:
        logger.error(f"broadcast crashed: {e}")
        await _osend(from_chat_id, f"❌ Broadcast error: <code>{html.escape(str(e))}</code>",
                    kb=InlineKeyboardMarkup([[_back_btn('s:main')]]))


# ── Handle admin settings input ────────────────────────────────────────────────
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
        mid = _admin_order_msg.pop(user_id, None)
        if mid:
            await _del_msg(chat_id, mid)
        await _osend(chat_id,
            "<b>⚙️ ការកំណត់ Admin</b>\n\nសូមជ្រើសរើសប្រតិបត្តិការ៖",
            kb=_settings_main_rkb())
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


# ── Pending payment reminder ───────────────────────────────────────────────────
async def _remind_pending(chat_id, session):
    qr_mid = session.get('qr_message_id') or session.get('photo_message_id')
    if qr_mid:
        try:
            await app.copy_message(chat_id, chat_id, qr_mid,
                                   reply_markup=_check_payment_ikb())
            return
        except Exception:
            pass
    await _osend(chat_id,
        "⚠️ <b>លោកអ្នកមានការទិញដែលកំពុងរង់ចាំការបង់ប្រាក់</b>\n\n"
        "ចុច 🚫 បោះបង់ ដើម្បីបោះបង់", kb=_check_payment_ikb())


# ── Main callback handler ──────────────────────────────────────────────────────
async def handle_order_callback(client, query: CallbackQuery) -> bool:
    global CHANNEL_ID, MAINTENANCE_MODE, EXTRA_ADMIN_IDS, PAYMENT_NAME
    d    = query.data or ''
    cid  = query.message.chat.id
    uid  = query.from_user.id
    user = query.from_user

    if uid != ADMIN_ID:
        asyncio.create_task(_notify_new_user(uid,
            user.first_name or '', user.last_name or '', user.username or ''))

    if d == 'o:home':
        await query.answer()
        await send_account_selection(cid)
        return True

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
        await _osend(cid, f"<b>🛒 {html.escape(account_type)}</b> — ${price}/ខ\n\n<b>សូមជ្រើសរើសចំនួន:</b>",
                    kb=InlineKeyboardMarkup(qty_rows))
        return True

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
        msg_sent = await _osend(cid, summary, kb=confirm_kb)
        if msg_sent:
            sess['summary_message_id'] = msg_sent.id
        return True

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

    if d == 'cancel_buy':
        await query.answer()
        with _data_lock:
            order_sessions.pop(uid, None)
        _save_sessions_bg()
        await _del_msg(cid, query.message.id)
        await send_account_selection(cid)
        return True

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

    if d == 'don_top':
        await query.answer()
        top_text = await _format_top_donors()
        kb = InlineKeyboardMarkup([
            [_ikb('🔄 ផ្ទុកឡើងវិញ', 'don_top')],
            [_ikb('💝 Donate', 'donate'),
             InlineKeyboardButton('🏠 ម៉ឺនុយមេ', callback_data='home',
                                  icon_custom_emoji_id='5282843764451195532')],
        ])
        try:
            await query.message.edit_text(top_text, parse_mode=ParseMode.HTML, reply_markup=kb)
        except Exception:
            await _osend(cid, top_text, kb=kb)
        return True

    if d == 'donate_khpay':
        await query.answer()
        await send_donate_menu(cid, uid, query=query)
        return True

    if d == 'donate_stars':
        await query.answer()
        await send_donate_stars_menu(cid, uid, query=query)
        return True

    if d.startswith('dons:'):
        try:
            stars = int(d.split(':', 1)[1])
        except (ValueError, IndexError):
            await query.answer()
            return True
        if stars <= 0:
            await query.answer('❌ ចំនួនមិនត្រឹមត្រូវ!', show_alert=True)
            return True
        await query.answer()
        try:
            await client.send_invoice(
                chat_id=cid,
                title='⭐ Donate ជូន RADY Bot',
                description=f'Donate {stars} Telegram Stars ជួយ Bot នេះបន្តដំណើរការ 🙏',
                payload=f'donate_stars_{stars}_{uid}',
                currency='XTR',
                prices=[LabeledPrice(label='Stars', amount=stars)],
            )
        except Exception as e:
            logger.error(f'send_invoice stars: {e}')
            await _osend(cid, '❌ មិនអាចបង្កើត Stars invoice បាន។ សូមព្យាយាមម្ដងទៀត។')
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
        await _osend(cid,
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

    if d.startswith('s:') and is_admin(uid):
        action = d[2:]
        await query.answer()
        _admin_order_msg[uid] = query.message.id

        if action == 'close':
            await _del_msg(cid, query.message.id)
            _admin_order_msg.pop(uid, None)
            await _osend(cid, "✅ Admin Panel បានបិទ", kb=ReplyKeyboardRemove())
            return True
        if action == 'main':
            await _del_msg(cid, query.message.id)
            _admin_order_msg.pop(uid, None)
            await _osend(cid,
                "<b>⚙️ ការកំណត់ Admin</b>\n\nសូមជ្រើសរើសប្រតិបត្តិការ៖",
                kb=_settings_main_rkb())
            return True
        if action == 'cancel_input':
            with _data_lock:
                order_sessions.pop(uid, None)
            _save_sessions_bg()
            await _del_msg(cid, query.message.id)
            _admin_order_msg.pop(uid, None)
            await _osend(cid,
                "<b>⚙️ ការកំណត់ Admin</b>\n\nសូមជ្រើសរើសប្រតិបត្តិការ៖",
                kb=_settings_main_rkb())
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
        return True

    return False


# ── Order message handler ──────────────────────────────────────────────────────
async def handle_order_message(client, message: Message) -> bool:
    uid  = message.from_user.id if message.from_user else None
    if not uid:
        return False
    cid  = message.chat.id
    text = message.text or message.caption or ''
    user = message.from_user

    if uid != ADMIN_ID:
        asyncio.create_task(_notify_new_user(uid,
            user.first_name or '', user.last_name or '', user.username or ''))

    sess = order_sessions.get(uid)
    state = sess.get('state', '') if sess else ''

    if is_admin(uid) and text in _ADMIN_KB_LABELS:
        busy_states = {'waiting_for_accounts', 'waiting_for_account_type',
                       'waiting_for_price', 'waiting_for_del_confirm', 'broadcast_confirm'}
        if not state.startswith('admin_input:') and state not in busy_states:
            if text == _KB_ADD_ACC:
                await _start_add_account_flow(cid, uid)
            elif text == _KB_DEL_TYPE:
                await _show_del_type_panel(cid, uid)
            elif text == _KB_BUYERS:
                await _export_buyers_panel(cid)
            elif text == _KB_USERS:
                await _show_users_panel(cid)
            elif text == _KB_PAYMENT:
                await _show_pay_panel(cid, uid)
            elif text == _KB_CHANNEL:
                await _show_ch_panel(cid, uid)
            elif text == _KB_BAKONG:
                await _show_bak_panel(cid, uid)
            elif text == _KB_ADMINS:
                await _show_adm_panel(cid, uid)
            elif text == _KB_MAINT:
                await _show_mnt_panel(cid, uid)
            elif text == _KB_BROADCAST:
                await _prompt_admin_input(cid, uid, 'broadcast',
                    "📢 <b>Broadcast</b>\n\nផ្ញើ​សារ​ (text/photo/file) ដែលចង់ផ្សាយ:", 'main')
            return True

    if not sess:
        return False

    if state.startswith('admin_input:') and is_admin(uid):
        key = state.split(':', 1)[1]
        await _handle_admin_input(client, cid, uid, key, text, message)
        return True

    if state == 'broadcast_confirm' and is_admin(uid):
        return False

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

    if state == 'payment_pending':
        await _remind_pending(cid, sess)
        return True

    if state == 'donation_pending':
        await _osend(cid,
            '⚠️ <b>មានការ Donate ដែលកំពុងរង់ចាំ</b>\n\nចុច 🚫 បោះបង់ ដើម្បីបោះបង់',
            kb=InlineKeyboardMarkup([[_ikb('🚫 បោះបង់', 'don_cancel')]]))
        return True

    if state == 'don_waiting_amount':
        raw = text.strip()
        try:
            amount = float(raw)
            if amount <= 0:
                raise ValueError
        except ValueError:
            await _osend(cid,
                '❌ <b>ចំនួនមិនត្រឹមត្រូវ!</b>\n\nផ្ញើជាលេខ (ឧ. <code>3.5</code> ឬ <code>10</code>)',
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


# ── Order command handlers ─────────────────────────────────────────────────────
async def handle_settings_command(client, message: Message):
    uid = message.from_user.id
    if not is_admin(uid):
        await message.reply("⛔ <b>អ្នកមិនមានសិទ្ធ!</b>", parse_mode=ParseMode.HTML)
        return
    await send_admin_settings(client, message.chat.id, uid)

async def handle_order_command(client, message: Message):
    uid = message.from_user.id if message.from_user else None
    if not uid:
        return
    cid = message.chat.id
    user = message.from_user
    asyncio.create_task(_notify_new_user(uid,
        user.first_name or '', user.last_name or '', user.username or ''))
    if MAINTENANCE_MODE and not is_admin(uid):
        await _osend(cid, "🛠 <b>Bot កំពុង Maintenance</b>\n\nសូមរង់ចាំ!")
        return
    await send_account_selection(cid)

async def handle_history_command(client, message: Message):
    uid = message.from_user.id if message.from_user else None
    if not uid:
        return
    cid = message.chat.id
    loop = asyncio.get_running_loop()
    rows = await loop.run_in_executor(None, _get_purchase_history_sync, uid, 10)
    if not rows:
        await _osend(cid, "📭 <b>អ្នកមិនទាន់មានប្រវត្តិទិញ</b>")
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
    await _osend(cid, "\n".join(lines))


# ══════════════════════════════════════════════════════════════════════════════
# SECTION: BOT APP & HANDLERS
# ══════════════════════════════════════════════════════════════════════════════

_session_string = os.environ.get('BOT_SESSION_STRING', '').strip() or None
_client_kwargs = dict(
    api_id=int(os.environ['TELEGRAM_API_ID']),
    api_hash=os.environ['TELEGRAM_API_HASH'],
    bot_token=os.environ['TELEGRAM_BOT_TOKEN'],
)
if _session_string:
    _client_kwargs['session_string'] = _session_string
    _client_kwargs['in_memory'] = True

app = Client('simple_bot' if not _session_string else ':memory:', **_client_kwargs)

# ── /start ─────────────────────────────────────────────────────────────────────
@app.on_message(filters.incoming & filters.command('start'))
async def cmd_start(client: Client, message: Message):
    uid  = message.from_user.id
    sess = reset_sess(uid)
    cid  = message.chat.id
    msg = await client.send_message(cid, HOME_TEXT, reply_markup=main_kb(), parse_mode=ParseMode.HTML)
    save_msg(sess, cid, msg.id)
    logger.info(f'[/start] uid={uid}')

# ── Callback handler ───────────────────────────────────────────────────────────
@app.on_callback_query()
async def cb_handler(client: Client, query: CallbackQuery):
    d    = query.data or ''
    cid  = query.message.chat.id
    uid  = query.from_user.id
    sess = get_sess(uid)

    try:
        if await handle_order_callback(client, query):
            return
    except Exception as _oe:
        logger.error(f'order cb error: {_oe}')

    await query.answer()
    save_msg(sess, cid, query.message.id)

    async def edit(text: str, kb=None):
        try:
            await query.message.edit_text(text, reply_markup=kb, parse_mode=ParseMode.HTML)
        except Exception:
            msg = await client.send_message(cid, text, reply_markup=kb, parse_mode=ParseMode.HTML)
            save_msg(sess, cid, msg.id)

    if d == 'home':
        reset_sess(uid); sess = get_sess(uid)
        save_msg(sess, cid, query.message.id)
        await edit_or_send(client, sess, cid, HOME_TEXT, main_kb()); return

    if d == 'order':
        await send_account_selection(cid); return

    if d == 'style':
        sess.state = S_STYLE
        await edit('<b>✍️ ផ្ញើអក្សរដើម្បីប្តូររចនាបថ</b>', cancel_kb('home')); return

    if d == 'doc':
        sess.state = S_DOC
        await edit('<b>📄 ជ្រើសរើសប្រតិបត្តិការ PDF</b>', IK_DOC); return

    if d == 'photo_pdf':
        sess.state = S_PDF; sess.pdf_photos = []; sess.pdf_name = None
        await edit('<b>🖼️ ផ្ញើរូបភាព (1-20 រូប) ដើម្បីបំប្លែងជា PDF</b>',
                   cancel_kb('doc')); return

    if d == 'pdf_rename':
        if not sess.pdf_photos:
            await edit('<b>⚠️ ផ្ញើរូបភាពជាមុន!</b>', cancel_kb('doc')); return
        sess.state = S_PDF_RENAME
        await edit('<b>✏️ ផ្ញើឈ្មោះ PDF ថ្មី</b>', cancel_kb('doc')); return

    if d == 'pdf_build':
        if not sess.pdf_photos:
            await edit('<b>⚠️ មិនមានរូបភាព!</b>', cancel_kb('doc')); return
        await edit('<b>⏳ កំពុងបំប្លែង...</b>')
        try:
            loop = asyncio.get_event_loop()
            photos = sess.pdf_photos[:]
            pdf_bytes = await loop.run_in_executor(None, images_to_pdf, photos)
            buf = io.BytesIO(pdf_bytes)
            fname = (sess.pdf_name or 'document') + '.pdf'
            buf.name = fname
            await client.send_document(cid, buf, file_name=fname)
            sess.pdf_photos = []; sess.state = S_DOC
            await client.send_message(cid, '✅ <b>PDF បានបង្កើតរួច!</b>',
                                       reply_markup=IK_PDF_DONE, parse_mode=ParseMode.HTML)
        except Exception as e:
            logger.error(f'pdf_build: {e}')
            await edit(f'❌ <b>Error:</b> {e}', cancel_kb('doc'))
        return

    if d in ('pdf_png', 'pdf_jpg'):
        fmt = 'PNG' if d == 'pdf_png' else 'JPG'
        sess.state = S_PDF2IMG; sess.pdf2img_fmt = fmt
        await edit(f'<b>📄 ផ្ញើ PDF ដើម្បីបំប្លែងជា {fmt}</b>', cancel_kb('doc')); return

    if d == 'qr':
        sess.state = S_QR
        await edit(
            '<b>📱 QR Code</b>\n\n'
            '🔹 <b>បង្កើត QR</b> — ផ្ញើអក្សរ/Link\n'
            '🔸 <b>Scan QR</b> — ផ្ញើរូបភាព QR',
            mkb([
                [ikb('✏️ បង្កើត QR', 'qr_create')],
                [ikb('📷 Scan QR', 'qr_scan')],
                [InlineKeyboardButton('Back', callback_data='home',
                                     icon_custom_emoji_id='5877629862306385808')],
            ])); return

    if d == 'qr_create':
        sess.state = S_QR_CREATE
        await edit('<b>✏️ ផ្ញើអក្សរ ឬ Link ដើម្បីបង្កើត QR Code</b>',
                   cancel_kb('qr')); return

    if d == 'qr_scan':
        sess.state = S_QR_SCAN
        await edit('<b>📷 ផ្ញើរូបភាព QR Code ដើម្បី Scan</b>',
                   cancel_kb('qr')); return

    if d == 'gold':
        sess.state = S_GOLD
        await edit('<b>⏳ កំពុងទាញយកតម្លៃ...</b>')
        spots = await fetch_all_spots()
        text = '\n\n'.join([
            fmt_price(spots['gold'],   'មាស',     '🥇', spots['gold_chg'],   spots['gold_pct']),
            fmt_price(spots['silver'], 'ប្រាក់',   '🥈', spots['silver_chg'], spots['silver_pct']),
            fmt_price(spots['plat'],   'ផ្លាទីន',  '⬜', spots['plat_chg'],   spots['plat_pct']),
        ])
        await edit(text, mkb([[ikb('🔄 ធ្វើឱ្យទាន់សម័យ', 'gold')],
                               [InlineKeyboardButton('Back', callback_data='home',
                                                     icon_custom_emoji_id='5877629862306385808')]])); return

    if d == 'rmbg':
        sess.state = S_RMBG
        await edit(
            '<b>🖼️ Remove Background</b>\n\nផ្ញើរូបភាព ដើម្បីដកផ្ទៃខាងក្រោយ',
            cancel_kb('home')); return

    if d == 'donate':
        await send_donate_menu(cid, uid, query=query); return

    if await handle_email_callback(client, query, edit):
        return

    if d == 'cancel_main':
        reset_sess(uid); sess = get_sess(uid)
        await edit_or_send(client, sess, cid, HOME_TEXT, main_kb()); return

# ── Message handler ────────────────────────────────────────────────────────────
@app.on_message(filters.incoming & ~filters.command(['start','settings','order','history']) & filters.private)
async def msg_handler(client: Client, message: Message):
    uid  = message.from_user.id
    sess = get_sess(uid)
    cid  = message.chat.id

    try:
        if await handle_order_message(client, message):
            return
    except Exception as _oe:
        logger.error(f'order msg error: {_oe}')

    # ── Style ──────────────────────────────────────────────────────────────────
    if sess.state == S_STYLE:
        text = message.text or ''
        if not text:
            await edit_or_send(client, sess, cid, '<b>✍️ ផ្ញើអក្សរដើម្បីប្តូររចនាបថ</b>', cancel_kb('home'))
            return
        rows = []
        for name, fn in TEXT_STYLES:
            try:
                styled = fn(text)
            except Exception:
                styled = text
            rows.append([InlineKeyboardButton(
                f'{name}: {styled[:30]}', copy_text=styled)])
        rows.append([InlineKeyboardButton('Back', callback_data='home',
                                          icon_custom_emoji_id='5877629862306385808')])
        msg = await client.send_message(
            cid, f'<b>✍️ រចនាបថ</b> — <code>{text}</code>',
            reply_markup=InlineKeyboardMarkup(rows), parse_mode=ParseMode.HTML)
        save_msg(sess, cid, msg.id)
        return

    # ── PDF rename ──────────────────────────────────────────────────────────────
    if sess.state == S_PDF_RENAME:
        name = (message.text or '').strip()
        if name:
            sess.pdf_name = name
        sess.state = S_PDF
        n = len(sess.pdf_photos)
        msg = await client.send_message(
            cid,
            f'<b>✏️ ឈ្មោះ PDF: "{sess.pdf_name or "document"}"</b>\n'
            f'រូបភាព: {n}',
            reply_markup=pdf_kb(n, sess.pdf_name),
            parse_mode=ParseMode.HTML)
        save_msg(sess, cid, msg.id)
        return

    # ── PDF collect photos ──────────────────────────────────────────────────────
    if sess.state == S_PDF:
        photo = message.photo or (message.document if message.document and
                                  message.document.mime_type and
                                  message.document.mime_type.startswith('image/') else None)
        if not photo:
            await edit_or_send(client, sess, cid,
                               '<b>⚠️ ផ្ញើរូបភាពប៉ុណ្ណោះ!</b>', cancel_kb('doc'))
            return
        if len(sess.pdf_photos) >= 20:
            await client.send_message(cid, '<b>⚠️ អតិបរមា 20 រូប!</b>', parse_mode=ParseMode.HTML)
            return
        fid   = photo.file_id
        raw   = await download_file(client, fid)
        sess.pdf_photos.append(raw)
        n     = len(sess.pdf_photos)
        msg   = await client.send_message(
            cid, f'✅ <b>រូបទី {n}</b>', reply_markup=pdf_kb(n, sess.pdf_name),
            parse_mode=ParseMode.HTML)
        save_msg(sess, cid, msg.id)
        return

    # ── PDF → images ────────────────────────────────────────────────────────────
    if sess.state == S_PDF2IMG:
        doc = message.document
        if not doc or not doc.file_name or not doc.file_name.lower().endswith('.pdf'):
            await edit_or_send(client, sess, cid, '<b>⚠️ ផ្ញើ PDF ប៉ុណ្ណោះ!</b>', cancel_kb('doc'))
            return
        fmt = sess.pdf2img_fmt or 'PNG'
        wait_msg = await client.send_message(cid, '<b>⏳ កំពុងបំប្លែង...</b>', parse_mode=ParseMode.HTML)
        try:
            raw    = await download_file(client, doc.file_id)
            loop   = asyncio.get_event_loop()
            images = await loop.run_in_executor(None, pdf_to_images, raw, fmt)
            for i, img in enumerate(images):
                buf = io.BytesIO(img)
                ext = fmt.lower()
                buf.name = f'page_{i+1}.{ext}'
                await client.send_document(cid, buf, file_name=buf.name)
            await client.delete_messages(cid, wait_msg.id)
            msg = await client.send_message(
                cid, f'✅ <b>បំប្លែង {len(images)} ទំព័រ → {fmt} រួចហើយ!</b>',
                reply_markup=ik_img_done(fmt), parse_mode=ParseMode.HTML)
            save_msg(sess, cid, msg.id)
            sess.state = S_DOC
        except Exception as e:
            logger.error(f'pdf2img: {e}')
            await client.delete_messages(cid, wait_msg.id)
            await edit_or_send(client, sess, cid, f'❌ <b>Error:</b> {e}', cancel_kb('doc'))
        return

    # ── QR create ───────────────────────────────────────────────────────────────
    if sess.state == S_QR_CREATE:
        text = message.text or ''
        if not text:
            await edit_or_send(client, sess, cid,
                               '<b>✏️ ផ្ញើអក្សរ ឬ Link ដើម្បីបង្កើត QR Code</b>', cancel_kb('qr'))
            return
        try:
            loop = asyncio.get_event_loop()
            qr_bytes = await loop.run_in_executor(None, create_qr, text)
            buf = io.BytesIO(qr_bytes); buf.name = 'qr.png'
            await client.send_photo(cid, buf, caption=f'<code>{text}</code>',
                                    reply_markup=IK_QR_DONE, parse_mode=ParseMode.HTML)
            sess.state = S_QR
        except Exception as e:
            logger.error(f'qr_create: {e}')
            await edit_or_send(client, sess, cid, f'❌ <b>Error:</b> {e}', cancel_kb('qr'))
        return

    # ── QR scan ─────────────────────────────────────────────────────────────────
    if sess.state == S_QR_SCAN:
        photo = message.photo or (message.document if message.document and
                                  message.document.mime_type and
                                  message.document.mime_type.startswith('image/') else None)
        if not photo:
            await edit_or_send(client, sess, cid,
                               '<b>📷 ផ្ញើរូបភាព QR Code ដើម្បី Scan</b>', cancel_kb('qr'))
            return
        try:
            raw    = await download_file(client, photo.file_id)
            loop   = asyncio.get_event_loop()
            texts  = await loop.run_in_executor(None, scan_qr, raw)
            if texts:
                kb_rows = [[InlineKeyboardButton(f'📋 Copy: {t[:40]}', copy_text=t)] for t in texts]
                kb_rows.append([InlineKeyboardButton('Back', callback_data='home',
                                                     icon_custom_emoji_id='5877629862306385808')])
                msg = await client.send_message(
                    cid,
                    '✅ <b>QR បានស្កែន!</b>\n\n' +
                    '\n'.join(f'<code>{t}</code>' for t in texts),
                    reply_markup=InlineKeyboardMarkup(kb_rows),
                    parse_mode=ParseMode.HTML)
            else:
                msg = await client.send_message(
                    cid, '❌ <b>រកមិនឃើញ QR Code ក្នុងរូបនេះ!</b>',
                    reply_markup=IK_QR_DONE, parse_mode=ParseMode.HTML)
            save_msg(sess, cid, msg.id)
        except Exception as e:
            logger.error(f'qr_scan: {e}')
            await edit_or_send(client, sess, cid, f'❌ <b>Error:</b> {e}', cancel_kb('qr'))
        return

    # ── Remove BG ───────────────────────────────────────────────────────────────
    if sess.state == S_RMBG:
        photo = message.photo or (message.document if message.document and
                                  message.document.mime_type and
                                  message.document.mime_type.startswith('image/') else None)
        if not photo:
            await edit_or_send(client, sess, cid,
                               '<b>🖼️ ផ្ញើរូបភាព ដើម្បីដកផ្ទៃខាងក្រោយ</b>', cancel_kb('home'))
            return
        wait_msg = await client.send_message(
            cid, '⏳ <b>កំពុងដក Background...</b>', parse_mode=ParseMode.HTML)
        try:
            raw = await download_file(client, photo.file_id)
            result_bytes, charged = await remove_bg(raw)
            record_rmbg_use(str(charged))
            buf = io.BytesIO(result_bytes); buf.name = 'removed_bg.png'
            await client.send_document(
                cid, buf,
                caption='✅ <b>Background បានដក!</b>',
                file_name='removed_bg.png',
                reply_markup=cancel_kb('home'),
                parse_mode=ParseMode.HTML)
            await client.delete_messages(cid, wait_msg.id)
            sess.state = S_RMBG

            try:
                acct = await rmbg_account()
                admin_id = int(os.environ.get('ADMIN_ID', 0))
                if admin_id:
                    name  = (message.from_user.first_name or '') + (' ' + message.from_user.last_name if message.from_user.last_name else '')
                    uname = f'@{message.from_user.username}' if message.from_user.username else 'គ្មាន'
                    free  = acct.get('free_calls', '?')
                    await client.send_message(
                        admin_id,
                        f'🪄 <b>{name.strip()}</b> ({uname}) បានប្រើ Remove BG\n'
                        f'🎁 Free Calls នៅសល់ : <b>{free}</b>',
                        parse_mode=ParseMode.HTML)
            except Exception as ae:
                logger.warning(f'admin notify rmbg: {ae}')

        except Exception as e:
            logger.error(f'rmbg: {e}')
            await edit_or_send(client, sess, cid, '❌ <b>មានបញ្ហា! ព្យាយាមម្ដងទៀត</b>', cancel_kb('cancel_main'))
            sess.state = S_RMBG
        return

    # ── Fallback ─────────────────────────────────────────────────────────────────
    reset_sess(uid); sess = get_sess(uid)
    msg = await _send(client, sess, cid, HOME_TEXT, reply_markup=main_kb(), parse_mode=ParseMode.HTML)
    save_msg(sess, cid, msg.id)


# ── /settings command ──────────────────────────────────────────────────────────
@app.on_message(filters.incoming & filters.command('settings') & filters.private)
async def cmd_settings(client: Client, message: Message):
    await handle_settings_command(client, message)

# ── /order command ─────────────────────────────────────────────────────────────
@app.on_message(filters.incoming & filters.command('order') & filters.private)
async def cmd_order(client: Client, message: Message):
    await handle_order_command(client, message)

# ── /history command ───────────────────────────────────────────────────────────
@app.on_message(filters.incoming & filters.command('history') & filters.private)
async def cmd_history(client: Client, message: Message):
    await handle_history_command(client, message)

# ── Channel post handler ────────────────────────────────────────────────────────
@app.on_message(filters.channel)
async def channel_post_handler(client: Client, message: Message):
    try:
        await handle_channel_post(client, message)
    except Exception as e:
        logger.error(f'channel_post_handler: {e}')

# ── Stars: pre-checkout ────────────────────────────────────────────────────────
@app.on_pre_checkout_query()
async def pre_checkout_handler(client: Client, query: PreCheckoutQuery):
    await query.answer(ok=True)

# ── Stars: successful payment ──────────────────────────────────────────────────
@app.on_message(filters.successful_payment & filters.private)
async def payment_success(client: Client, message: Message):
    stars = message.successful_payment.total_amount
    uid = message.from_user.id
    first = message.from_user.first_name or ''
    last  = message.from_user.last_name or ''
    uname = message.from_user.username or ''
    loop = asyncio.get_event_loop()
    try:
        await loop.run_in_executor(
            None, _save_donation_sync,
            uid, first, last, uname, float(stars), f'stars_{stars}'
        )
        total, times = await loop.run_in_executor(None, _get_user_donation_total_sync, uid)
    except Exception as e:
        logger.error(f'payment_success db: {e}')
        total, times = 0, 0
    full = f'{first} {last}'.strip() or 'Anonymous'
    await message.reply(
        f'🎉 <b>អរគុណខ្លាំងណាស់!</b>\n\n'
        f'💝 <b>{html.escape(full)}</b>\n'
        f'បានបរិច្ចាគ <b>{stars} ⭐ Star{"s" if stars > 1 else ""}</b> ជូន RADY Bot! 🙏\n\n'
        f'💵 <b>សរុបរបស់អ្នក:</b> ${total:.2f} ({times} ដង)',
        parse_mode=ParseMode.HTML,
    )
    try:
        if CHANNEL_ID:
            await client.send_message(
                CHANNEL_ID,
                f'⭐ <b>Stars Donation</b>\n'
                f'👤 {html.escape(full)}'
                + (f' (@{html.escape(uname)})' if uname else '') +
                f'\n⭐ <b>{stars} Stars</b>',
                parse_mode=ParseMode.HTML,
            )
    except Exception as e:
        logger.error(f'payment_success channel: {e}')

# ── Run ────────────────────────────────────────────────────────────────────────
_run = app.loop.run_until_complete

if _inspect.iscoroutinefunction(app.start):
    _run(app.start())
else:
    app.start()

if not _session_string:
    exported = app.export_session_string()
    logger.info('=' * 60)
    logger.info('BOT_SESSION_STRING (copy this to Render env vars):')
    logger.info(exported)
    logger.info('=' * 60)

# Init DB and modules
try:
    em_init_db()
except Exception as _e:
    logger.error(f'email DB init: {_e}')

try:
    _init_db_sync()
    _load_data_sync()
    _load_sessions_sync()
except Exception as _e:
    logger.error(f'order DB init: {_e}')

# Start email background tasks
asyncio.get_event_loop().create_task(_em_poll_loop())
asyncio.get_event_loop().create_task(_em_restore_loop())

logger.info('🤖 Bot កំពុង Start...')

_idle_obj = _idle()
if _inspect.iscoroutine(_idle_obj):
    _run(_idle_obj)

if _inspect.iscoroutinefunction(app.stop):
    _run(app.stop())
else:
    app.stop()
