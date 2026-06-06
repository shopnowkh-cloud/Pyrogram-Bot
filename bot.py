#!/usr/bin/env python3
# -*- coding: utf-8 -*-
import os
import io
import logging
import asyncio
import tempfile

import httpx
from dataclasses import dataclass, field
from typing import Optional

from pyrogram import Client, filters
from pyrogram.enums import ParseMode
from pyrogram.types import (
    Message, CallbackQuery,
    InlineKeyboardMarkup, InlineKeyboardButton,
)

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
    state:        int            = S_MAIN
    mid:          Optional[int]  = None
    cid:          Optional[int]  = None
    pdf_photos:   list           = field(default_factory=list)
    pdf_name:     Optional[str]  = None
    pdf2img_fmt:  Optional[str]  = None


_sessions: dict[int, UserSession] = {}

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

def ikb(text: str, cb: str) -> InlineKeyboardButton:
    return InlineKeyboardButton(text, callback_data=cb)

def ikb_url(text: str, url: str) -> InlineKeyboardButton:
    return InlineKeyboardButton(text, url=url)

# ── Inline keyboards ───────────────────────────────────────────────────────────
IK_DOC = mkb([
    [ikb('🖼️ រូបភាព → PDF', 'photo_pdf')],
    [ikb('🖼️ PDF → PNG', 'pdf_png'), ikb('📷 PDF → JPG', 'pdf_jpg')],
    [ikb('🏠 ម៉ឺនុយមេ', 'home')],
])
IK_QR = mkb([
    [ikb('🔳 បង្កើត QR', 'qr_create'), ikb('🔍 Scan QR', 'qr_scan')],
    [ikb('🏠 ម៉ឺនុយមេ', 'home')],
])
IK_PDF_DONE    = mkb([[ikb('🖼️ PDF ថ្មី', 'photo_pdf'), ikb('🏠 ម៉ឺនុយមេ', 'home')]])
IK_QR_CR_DONE  = mkb([[ikb('🔳 QR ថ្មី', 'qr_create'), ikb('🔍 Scan QR', 'qr_scan')], [ikb('🏠 ម៉ឺនុយមេ', 'home')]])
IK_QR_SC_DONE  = mkb([[ikb('🔍 Scan ថ្មី', 'qr_scan'), ikb('🔳 បង្កើត QR', 'qr_create')], [ikb('🏠 ម៉ឺនុយមេ', 'home')]])


def ik_img_done(fmt: str) -> InlineKeyboardMarkup:
    cb = 'pdf_png' if fmt == 'PNG' else 'pdf_jpg'
    return mkb([[ikb(f'🔄 {fmt} ថ្មី', cb), ikb('🏠 ម៉ឺនុយមេ', 'home')]])

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
    '<tg-emoji emoji-id="5449885771420934013">🌱</tg-emoji> <b>RADY BOT</b>'
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

async def safe_delete(client: Client, cid: int, mid: int):
    try:
        await client.delete_messages(cid, mid)
    except Exception:
        pass

# ── Keyboards ──────────────────────────────────────────────────────────────────
def main_kb() -> InlineKeyboardMarkup:
    return mkb([
        [InlineKeyboardButton('រចនាបទអក្សរ', callback_data='style', icon_custom_emoji_id='5197269100878907942'),
         InlineKeyboardButton('PDF', callback_data='doc', icon_custom_emoji_id='5838982342122674517')],
        [InlineKeyboardButton('បង្កើត QR', callback_data='qr', icon_custom_emoji_id='5440410042773824003'),
         InlineKeyboardButton('ហាងឆេងមាស', callback_data='gold', icon_custom_emoji_id='5429651785352501917')],
        [InlineKeyboardButton('Remove BG', callback_data='rmbg', icon_custom_emoji_id='5395663879483181935')],
    ])

def cancel_kb(data: str) -> InlineKeyboardMarkup:
    return mkb([[ikb('◀️ Back', data)]])

def pdf_kb(n: int, name=None) -> InlineKeyboardMarkup:
    lbl = f'✅ បង្កើត PDF ({n} រូប)' + (f' 📄 "{name}"' if name else '')
    return mkb([
        [ikb(lbl, 'pdf_build'), ikb('✏️ ប្តូរឈ្មោះ', 'pdf_rename')],
        [ikb('◀️ Back', 'doc')],
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
    """Fetch remove.bg account info (credits + free calls)."""
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
    """Returns (result_bytes, credits_charged)."""
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


# ── App ────────────────────────────────────────────────────────────────────────
app = Client(
    'simple_bot',
    api_id=int(os.environ['TELEGRAM_API_ID']),
    api_hash=os.environ['TELEGRAM_API_HASH'],
    bot_token=os.environ['TELEGRAM_BOT_TOKEN'],
)

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

    await query.answer()
    save_msg(sess, cid, query.message.id)

    async def edit(text: str, kb=None):
        try:
            await query.message.edit_text(text, reply_markup=kb, parse_mode=ParseMode.HTML)
        except Exception:
            msg = await client.send_message(cid, text, reply_markup=kb, parse_mode=ParseMode.HTML)
            save_msg(sess, cid, msg.id)

    # ── home ────────────────────────────────────────────────────────────────
    if d == 'home':
        reset_sess(uid); sess = get_sess(uid)
        save_msg(sess, cid, query.message.id)
        await edit_or_send(client, sess, cid, HOME_TEXT, main_kb()); return

    # ── style ───────────────────────────────────────────────────────────────
    if d in ('style', 'style_new'):
        await edit_or_send(client, sess, cid,
            '✍️ <b>រចនាប័ទ្មអក្សរ</b>\n\n'
            'បំប្លែងអក្សរឡាតាំងទៅជាពុម្ពអក្សរពិសេស\n'
            'Bold · Italic · Script · Bubble · Upside-down និងច្រើនទៀត\n\n'
            '✏️ <b>វាយអក្សរខាងក្រោម:</b>', cancel_kb('cancel_main'))
        sess.state = S_STYLE; return

    # ── cancel_main ─────────────────────────────────────────────────────────
    if d == 'cancel_main':
        reset_sess(uid); sess = get_sess(uid)
        save_msg(sess, cid, query.message.id)
        await edit_or_send(client, sess, cid, HOME_TEXT, main_kb()); return

    # ── doc / cancel_doc ────────────────────────────────────────────────────
    if d in ('doc', 'cancel_doc'):
        sess.pdf_photos = []; sess.pdf_name = None
        await edit(
            '🗂️ <b>បំប្លែង PDF</b>\n\n'
            '🖼️  រូបភាព → PDF — ផ្សំរូបភាពច្រើនទៅជា PDF តែមួយ\n'
            '🖼️  PDF → PNG — បំប្លែង PDF ម្តាមទំព័រជារូបភាព PNG\n'
            '📷  PDF → JPG — បំប្លែង PDF ម្តាមទំព័រជារូបភាព JPG\n\n'
            '👇 <b>ចុចជ្រើសរើស:</b>', IK_DOC)
        sess.state = S_DOC; return

    # ── cancel_qr ───────────────────────────────────────────────────────────
    if d == 'cancel_qr':
        await edit(
            '📷 <b>QR Code</b>\n\n'
            '🔳  បង្កើត QR — បង្កើត QR Code HD 2048×2048\n'
            '🔍  Scan QR — Decode Link ឬ Text ចេញពី QR\n\n'
            '👇 <b>ចុចជ្រើសរើស:</b>', IK_QR)
        sess.state = S_QR; return

    # ── photo_pdf ───────────────────────────────────────────────────────────
    if d == 'photo_pdf':
        sess.pdf_photos = []; sess.mid = query.message.id
        await edit_or_send(client, sess, cid,
            '🖼️ <b>រូបភាព → PDF</b>\n\n'
            'Upload រូបភាពម្តាមដុំ Bot នឹងផ្សំទៅជា PDF តែមួយ\n'
            'Format: JPG · PNG · WEBP\n\n'
            '📤 <b>ចាប់ផ្ដើម Upload រូបភាព:</b>', cancel_kb('cancel_doc'))
        sess.state = S_PDF; return

    # ── pdf_png / pdf_jpg ───────────────────────────────────────────────────
    if d in ('pdf_png', 'pdf_jpg'):
        fmt = 'PNG' if d == 'pdf_png' else 'JPG'
        sess.pdf2img_fmt = fmt
        ico = '🖼️' if fmt == 'PNG' else '📷'
        await edit_or_send(client, sess, cid,
            f'{ico} <b>PDF → {fmt}</b>\n\n'
            f'Upload ឯកសារ PDF Bot នឹងបំប្លែងម្តាមទំព័រ\n'
            f'ជារូបភាព <b>{fmt}</b> គុណភាពខ្ពស់ — 150 DPI\n\n'
            f'📎 <b>Upload ឯកសារ PDF:</b>', cancel_kb('cancel_doc'))
        sess.state = S_PDF2IMG; return

    # ── pdf_build ───────────────────────────────────────────────────────────
    if d == 'pdf_build':
        await handle_pdf_build(client, sess, cid, query.message); return

    # ── pdf_rename ──────────────────────────────────────────────────────────
    if d == 'pdf_rename':
        n   = len(sess.pdf_photos)
        cur = f'\n📄 ឈ្មោះបច្ចុប្បន្ន: <b>{sess.pdf_name}</b>' if sess.pdf_name else ''
        await edit_or_send(client, sess, cid,
            f'✏️ <b>ប្តូរឈ្មោះ PDF</b>\n\nPDF នេះមាន {n} រូបភាព{cur}\n'
            f'<i>មិនចាំបាច់វាយ .pdf — Bot នឹងបន្ថែមឱ្យ</i>\n\n'
            f'📝 <b>វាយឈ្មោះខាងក្រោម:</b>', cancel_kb('cancel_rename'))
        sess.state = S_PDF_RENAME; return

    # ── cancel_rename ───────────────────────────────────────────────────────
    if d == 'cancel_rename':
        n = len(sess.pdf_photos)
        await edit_or_send(client, sess, cid, f'🖼️ <b>បានទទួល {n} រូប</b>\nUpload បន្ថែម ឬ ចុច <b>បង្កើត PDF</b>', pdf_kb(n, sess.pdf_name))
        sess.state = S_PDF; return

    # ── qr menu ─────────────────────────────────────────────────────────────
    if d == 'qr':
        await edit(
            '📷 <b>QR Code</b>\n\n'
            '🔳  បង្កើត QR — បង្កើត QR Code HD 2048×2048\n'
            '🔍  Scan QR — Decode Link ឬ Text ចេញពី QR\n\n'
            '👇 <b>ចុចជ្រើសរើស:</b>', IK_QR)
        sess.state = S_QR; return

    # ── qr_create ───────────────────────────────────────────────────────────
    if d == 'qr_create':
        await edit_or_send(client, sess, cid,
            '🔳 <b>បង្កើត QR Code</b>\n\n'
            'បង្កើត QR Code HD ទំហំ <b>2048×2048</b>\n'
            'អាចប្រើជាមួយ Link · Text · ព័ត៌មានគ្រប់ប្រភេទ\n\n'
            '✏️ <b>វាយ Link ឬ Text ខាងក្រោម:</b>', cancel_kb('cancel_qr'))
        sess.state = S_QR_CREATE; return

    # ── qr_scan ─────────────────────────────────────────────────────────────
    if d == 'qr_scan':
        await edit_or_send(client, sess, cid,
            '🔍 <b>Scan QR Code</b>\n\n'
            'Upload រូបភាពដែលមាន QR Code\n'
            'Bot នឹង Decode យក <b>Link</b> ឬ <b>Text</b> ឱ្យអ្នក\n\n'
            '📤 <b>Upload រូបភាព QR:</b>', cancel_kb('cancel_qr'))
        sess.state = S_QR_SCAN; return

    # ── rmbg ────────────────────────────────────────────────────────────────
    if d == 'rmbg':
        await edit_or_send(client, sess, cid,
            '🪄 <b>លុប Background AI</b>\n\n'
            'Upload រូបភាព Bot នឹងលុប Background ជូន\n'
            'Format: JPG · PNG · WEBP\n\n'
            '📤 <b>Upload រូបភាព:</b>', cancel_kb('cancel_main'))
        sess.state = S_RMBG; return

    # ── gold ────────────────────────────────────────────────────────────────
    if d in ('gold', 'gold_live', 'cancel_gold'):
        await edit('⏳ <b>កំពុងទាញតម្លៃ...</b>')
        try:
            spots = await fetch_all_spots()
            IK_LIVE = mkb([[ikb('🔄 ធ្វើបន្ទាប់', 'gold_live')], [ikb('🏠 ម៉ឺនុយមេ', 'home')]])
            txt = (
                '📊 <b>ហាងឆេងឥឡូវនេះ (ពិភពលោក)</b>\n'
                + fmt_price(spots['gold'],   'មាស',    '🥇', spots['gold_chg'],   spots['gold_pct'])   + '\n'
                + fmt_price(spots['silver'], 'ប្រាក់',  '🥈', spots['silver_chg'], spots['silver_pct']) + '\n'
                + fmt_price(spots['plat'],   'ផ្លាទីន', '🔩', spots['plat_chg'],   spots['plat_pct'])
            )
            await edit(txt, IK_LIVE)
        except Exception as e:
            logger.error(f'gold: {e}')
            await edit('❌ <b>មានបញ្ហាទាញតម្លៃ! ព្យាយាមម្ដងទៀត</b>',
                       mkb([[ikb('🔄 ព្យាយាមម្ដងទៀត', 'gold'), ikb('🏠 ម៉ឺនុយមេ', 'home')]]))
        sess.state = S_GOLD; return


    # ── unknown → home ──────────────────────────────────────────────────────
    await edit_or_send(client, sess, cid, HOME_TEXT, main_kb())
    sess.state = S_MAIN

# ── Text message dispatcher ────────────────────────────────────────────────────
@app.on_message(filters.incoming & filters.text & ~filters.command(['start']) & filters.private)
async def text_handler(client: Client, message: Message):
    uid  = message.from_user.id
    sess = get_sess(uid)
    if   sess.state == S_STYLE:      await handle_style(client, message, sess)
    elif sess.state == S_QR_CREATE:  await handle_qr_create(client, message, sess)
    elif sess.state == S_PDF_RENAME: await handle_pdf_rename(client, message, sess)
    else:                            await handle_fallback(client, message, sess)


# ── Photo / document dispatcher ────────────────────────────────────────────────
@app.on_message(filters.incoming & (filters.photo | filters.document))
async def media_handler(client: Client, message: Message):
    uid  = message.from_user.id
    sess = get_sess(uid)
    if   sess.state == S_PDF:     await handle_pdf_photo(client, message, sess)
    elif sess.state == S_PDF2IMG: await handle_pdf2img(client, message, sess)
    elif sess.state == S_QR_SCAN: await handle_qr_scan(client, message, sess)
    elif sess.state == S_RMBG:    await handle_rmbg(client, message, sess)
    else:                         await handle_fallback(client, message, sess)

# ── Style handler ──────────────────────────────────────────────────────────────
async def handle_style(client: Client, message: Message, sess: UserSession):
    t    = message.text.strip()
    cid  = message.chat.id
    loop = asyncio.get_running_loop()

    def compute():
        out = []
        for name, fn in TEXT_STYLES:
            try:
                out.append((name, fn(t)))
            except Exception:
                pass
        return out

    pairs = await loop.run_in_executor(None, compute)
    rows = [[InlineKeyboardButton(styled, copy_text=styled)] for _, styled in pairs]
    rows.append([ikb('✍️ ដំណើរការថ្មី', 'style_new'), ikb('🏠 ម៉ឺនុយមេ', 'home')])

    await safe_delete(client, cid, message.id)

    text = f'✍️ <b>Style:</b> <code>{t}</code>\n━━━━━━━━━'
    kb   = InlineKeyboardMarkup(rows)
    edited = False
    if sess.mid:
        try:
            await client.edit_message_text(cid, sess.mid, text, reply_markup=kb, parse_mode=ParseMode.HTML)
            edited = True
        except Exception:
            pass
    if not edited:
        msg = await client.send_message(cid, text, reply_markup=kb, parse_mode=ParseMode.HTML)
        save_msg(sess, cid, msg.id)
    sess.state = S_STYLE

# ── PDF photo handler ──────────────────────────────────────────────────────────
async def handle_pdf_photo(client: Client, message: Message, sess: UserSession):
    cid = message.chat.id
    p, dc = message.photo, message.document
    if not p and not dc:
        await edit_or_send(client, sess, cid, '⚠️ Upload <b>រូបភាព</b>!', cancel_kb('cancel_doc')); return
    try:
        raw = await download_file(client, p.file_id if p else dc.file_id)
        sess.pdf_photos.append(raw)
        n    = len(sess.pdf_photos)
        txt  = f'🖼️ <b>បានទទួល {n} រូប</b>\nUpload បន្ថែម ឬ ចុច <b>បង្កើត PDF</b>'
        await safe_delete(client, cid, message.id)
        mid = sess.mid
        if n == 1 and mid:
            await safe_delete(client, cid, mid); sess.mid = None
        await edit_or_send(client, sess, cid, txt, pdf_kb(n, sess.pdf_name))
    except Exception as e:
        logger.error(f'pdf_photo: {e}')
        await edit_or_send(client, sess, cid, '❌ <b>មានបញ្ហា! ព្យាយាមម្ដងទៀត</b>', cancel_kb('cancel_doc'))

# ── PDF build ──────────────────────────────────────────────────────────────────
async def handle_pdf_build(client: Client, sess: UserSession, cid: int, orig_msg):
    if not sess.pdf_photos:
        await edit_or_send(client, sess, cid, '⚠️ <b>មិនទាន់មានរូបភាព!</b>', cancel_kb('cancel_doc')); return
    try:
        try:
            await orig_msg.edit_text(f'⏳ <b>កំពុងបំប្លែង {len(sess.pdf_photos)} រូប → PDF...</b>', parse_mode=ParseMode.HTML)
        except Exception:
            pass
        loop      = asyncio.get_running_loop()
        pdf_bytes = await loop.run_in_executor(None, images_to_pdf, sess.pdf_photos)
        raw_name  = (sess.pdf_name or 'KhmerBot').strip().rstrip('.').replace('/', '_') or 'KhmerBot'
        fname     = raw_name if raw_name.endswith('.pdf') else raw_name + '.pdf'
        if sess.mid: await safe_delete(client, cid, sess.mid); sess.mid = None
        await client.send_document(
            cid, io.BytesIO(pdf_bytes), file_name=fname,
            caption=f'✅ <b>PDF បង្កើតជោគជ័យ!</b>\n📄 {fname}  |  🖼️ {len(sess.pdf_photos)} ទំព័រ',
            parse_mode=ParseMode.HTML)
        msg = await client.send_message(cid, HOME_TEXT, reply_markup=main_kb(), parse_mode=ParseMode.HTML)
        save_msg(sess, cid, msg.id)
        sess.pdf_photos = []; sess.pdf_name = None; sess.state = S_MAIN
    except Exception as e:
        logger.error(f'pdf_build: {e}')
        await edit_or_send(client, sess, cid, '❌ <b>មានបញ្ហា! ព្យាយាមម្ដងទៀត</b>', cancel_kb('cancel_doc'))

# ── PDF → Image ────────────────────────────────────────────────────────────────
async def handle_pdf2img(client: Client, message: Message, sess: UserSession):
    cid = message.chat.id
    dc  = message.document
    is_pdf = dc and (getattr(dc, 'mime_type', '') == 'application/pdf' or (getattr(dc, 'file_name', '') or '').lower().endswith('.pdf'))
    if not is_pdf:
        await edit_or_send(client, sess, cid, '⚠️ Upload ឯកសារ <b>PDF</b>!', cancel_kb('cancel_doc')); return
    fmt = sess.pdf2img_fmt or 'PNG'
    try:
        await edit_or_send(client, sess, cid, f'⏳ <b>កំពុងបំប្លែង PDF → {fmt}...</b>')
        loop   = asyncio.get_running_loop()
        raw    = await download_file(client, dc.file_id)
        images = await loop.run_in_executor(None, pdf_to_images, raw, fmt)
        total  = len(images)
        if sess.mid: await safe_delete(client, cid, sess.mid); sess.mid = None
        await safe_delete(client, cid, message.id)
        ext = 'png' if fmt == 'PNG' else 'jpg'
        for i, img_bytes in enumerate(images):
            is_last = i == total - 1
            cap = (f'✅ <b>{"រួចរាល់! 1 ទំព័រ" if total==1 else f"រួចរាល់! {total} ទំព័រ → {fmt}" if is_last else f"ទំព័រ {i+1}/{total}"}</b>')
            await client.send_document(cid, io.BytesIO(img_bytes), file_name=f'page_{i+1:02d}.{ext}',
                                       caption=cap, parse_mode=ParseMode.HTML)
        m = await client.send_message(cid, '👇 <b>ជ្រើសរើស:</b>', reply_markup=ik_img_done(fmt), parse_mode=ParseMode.HTML)
        save_msg(sess, cid, m.id); sess.state = S_MAIN
    except Exception as e:
        logger.error(f'pdf2img: {e}')
        await edit_or_send(client, sess, cid, '❌ <b>មានបញ្ហា! ព្យាយាមម្ដងទៀត</b>', cancel_kb('cancel_doc'))

# ── QR create ──────────────────────────────────────────────────────────────────
async def handle_qr_create(client: Client, message: Message, sess: UserSession):
    t      = message.text.strip()
    cid    = message.chat.id
    chunks = [t[i:i+4296] for i in range(0, len(t), 4296)]
    total  = len(chunks)
    loop   = asyncio.get_running_loop()
    try:
        loading = await client.send_message(cid, f'⏳ <b>កំពុងបង្កើត{"" if total==1 else f" {total}"} QR Code{"s" if total>1 else ""}...</b>', parse_mode=ParseMode.HTML)
        for idx, chunk in enumerate(chunks):
            img_bytes = await loop.run_in_executor(None, create_qr, chunk)
            fname     = f'QRCode_HD{"" if total==1 else f"_p{idx+1}"}.png'
            await client.send_document(cid, io.BytesIO(img_bytes), file_name=fname)
        await safe_delete(client, cid, loading.id)
        if sess.mid: await safe_delete(client, cid, sess.mid); sess.mid = None
        await safe_delete(client, cid, message.id)
        m = await client.send_message(cid, '👇 <b>ជ្រើសរើស:</b>', reply_markup=IK_QR_CR_DONE, parse_mode=ParseMode.HTML)
        save_msg(sess, cid, m.id)
    except Exception as e:
        logger.error(f'qr_create: {e}')
        await edit_or_send(client, sess, cid, '❌ <b>មានបញ្ហា! ព្យាយាមម្ដងទៀត</b>', cancel_kb('cancel_qr'))
    sess.state = S_MAIN

# ── QR scan ────────────────────────────────────────────────────────────────────
async def handle_qr_scan(client: Client, message: Message, sess: UserSession):
    cid  = message.chat.id
    p, dc = message.photo, message.document
    if not p and not dc:
        await edit_or_send(client, sess, cid, '⚠️ Upload <b>រូបភាព QR</b>!', cancel_kb('cancel_qr'))
        sess.state = S_QR_SCAN; return
    loop = asyncio.get_running_loop()
    try:
        raw     = await download_file(client, p.file_id if p else dc.file_id)
        results = await loop.run_in_executor(None, scan_qr, raw)
        if not results:
            await edit_or_send(client, sess, cid, '❌ <b>រកមិនឃើញ QR Code!</b>\nសូម Upload រូបភាពច្បាស់ជាង', cancel_kb('cancel_qr'))
            sess.state = S_QR_SCAN; return
        lines = '\n\n'.join(f'📌 <b>លទ្ធផលទី {i+1}:</b>\n<code>{r}</code>' for i,r in enumerate(results))
        if sess.mid: await safe_delete(client, cid, sess.mid); sess.mid = None
        await safe_delete(client, cid, message.id)
        await client.send_message(cid, f'✅ <b>Scan QR ជោគជ័យ!</b> ({len(results)} QR)\n━━━━━━━━━\n{lines}', parse_mode=ParseMode.HTML)
        m = await client.send_message(cid, '👇 <b>ជ្រើសរើស:</b>', reply_markup=IK_QR_SC_DONE, parse_mode=ParseMode.HTML)
        save_msg(sess, cid, m.id)
    except Exception as e:
        logger.error(f'qr_scan: {e}')
        await edit_or_send(client, sess, cid, '❌ <b>មានបញ្ហា! ព្យាយាមម្ដងទៀត</b>', cancel_kb('cancel_qr'))
    sess.state = S_MAIN

# ── PDF rename ─────────────────────────────────────────────────────────────────
async def handle_pdf_rename(client: Client, message: Message, sess: UserSession):
    sess.pdf_name = message.text.strip()
    n   = len(sess.pdf_photos)
    cid = message.chat.id
    await safe_delete(client, cid, message.id)
    txt = f'🖼️ <b>បានទទួល {n} រូប</b>\nUpload បន្ថែម ឬ ចុច <b>បង្កើត PDF</b>'
    await edit_or_send(client, sess, cid, txt, pdf_kb(n, sess.pdf_name))
    sess.state = S_PDF


# ── Remove Background handler ──────────────────────────────────────────────────
async def handle_rmbg(client: Client, message: Message, sess: UserSession):
    cid  = message.chat.id
    uid  = message.from_user.id
    user = message.from_user
    p, dc = message.photo, message.document
    if not p and not dc:
        await edit_or_send(client, sess, cid, '⚠️ Upload <b>រូបភាព</b>!', cancel_kb('cancel_main'))
        sess.state = S_RMBG; return
    try:
        await edit_or_send(client, sess, cid, '⏳ <b>AI កំពុងលុប Background...</b>')
        raw             = await download_file(client, p.file_id if p else dc.file_id)
        result, charged = await remove_bg(raw)
        acct            = await rmbg_account()
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, record_rmbg_use, charged)
        stats = await loop.run_in_executor(None, _load_stats)
        await safe_delete(client, cid, message.id)
        IK_RMBG_DONE = mkb([
            [ikb('🪄 លុបថ្មី', 'rmbg'), ikb('🏠 ម៉ឺនុយមេ', 'home')]
        ])
        await client.send_document(
            cid, io.BytesIO(result), file_name='removed_bg.png',
            caption='✅ <b>លុប Background ជោគជ័យ!</b>',
            parse_mode=ParseMode.HTML)
        m = await client.send_message(cid, '👇 <b>ជ្រើសរើស:</b>', reply_markup=IK_RMBG_DONE, parse_mode=ParseMode.HTML)
        save_msg(sess, cid, m.id); sess.state = S_MAIN

        # ── Notify admin ────────────────────────────────────────────────────
        try:
            admin_id = int(os.environ.get('ADMIN_ID', 0))
            if admin_id:
                name  = (user.first_name or '') + (' ' + user.last_name if user.last_name else '')
                uname = f'@{user.username}' if user.username else 'គ្មាន'
                free  = acct.get('free_calls', '?')
                total = acct.get('total_credits', '?')
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

# ── Fallback ───────────────────────────────────────────────────────────────────
async def handle_fallback(client: Client, message: Message, sess: UserSession):
    uid = message.from_user.id
    reset_sess(uid); sess = get_sess(uid)
    cid = message.chat.id
    msg = await client.send_message(cid, HOME_TEXT, reply_markup=main_kb(), parse_mode=ParseMode.HTML)
    save_msg(sess, cid, msg.id)


# ── Run ────────────────────────────────────────────────────────────────────────
logger.info('🤖 Bot កំពុង Start...')
app.run()
