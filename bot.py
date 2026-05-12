"""CSV → PDF Telegram Bot — Professional Edition.

Features
========
* Single-panel inline composer (admin / generator / owner).
* Dual command prefix support: `/cmd` and `.cmd`.
* Role-based access: Owner, Admins, Generators. Owner can add / remove both.
* CSV → PDF rendering with themes, watermark (text or image), logo, thumbnail.
* PDF rename via reply (reply to a generated PDF with the new file name).
* Inline animated processing message that is replaced by the final file only.
* Per-user concurrency: each user has an independent lock, so several people
  can generate PDFs simultaneously without blocking each other.
* Persistent state (settings, CSV, button labels, roles, assets).
* Owner-only utilities: /logs, /restart, /buttons, /admins, /gens.
"""

from __future__ import annotations

import asyncio
import base64
import csv
import html
import io
import json
import logging
import os
import re
import resource
import sys
import textwrap
import time
import traceback
from collections import defaultdict, deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Deque, Dict, List, Optional, Set, Tuple

from aiohttp import web
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, InputFile, Update
from telegram.constants import ChatAction, ParseMode
from telegram.error import BadRequest
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    ContextTypes,
    MessageHandler,
    filters,
)
from weasyprint import HTML

# ---------------------------------------------------------------------------
# Configuration & paths
# ---------------------------------------------------------------------------

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = Path(os.getenv("DATA_DIR", BASE_DIR / "data"))
DATA_DIR.mkdir(parents=True, exist_ok=True)
STATE_PATH = DATA_DIR / "state.json"
LOG_PATH = DATA_DIR / "bot.log"
# Legacy global paths kept only for backward-compatible migration.
WATERMARK_IMG_PATH = DATA_DIR / "watermark_image.png"
LOGO_IMG_PATH = DATA_DIR / "logo_image.png"
THUMB_IMG_PATH = DATA_DIR / "thumbnail_image.jpg"


def wm_path(uid: int) -> Path:
    return DATA_DIR / f"watermark_{uid}.png"


def logo_path(uid: int) -> Path:
    return DATA_DIR / f"logo_{uid}.png"


def thumb_path(uid: int) -> Path:
    return DATA_DIR / f"thumb_{uid}.jpg"


def front_path(uid: int) -> Path:
    return DATA_DIR / f"front_{uid}.pdf"


def back_path(uid: int) -> Path:
    return DATA_DIR / f"back_{uid}.pdf"

LOG_BUFFER: Deque[str] = deque(maxlen=2000)
ERROR_BUFFER: Deque[Tuple[float, str]] = deque(maxlen=500)


class BufferHandler(logging.Handler):
    def emit(self, record: logging.LogRecord) -> None:
        try:
            line = self.format(record)
            LOG_BUFFER.append(line)
            if record.levelno >= logging.ERROR:
                ERROR_BUFFER.append((time.time(), line))
        except Exception:
            pass


_fmt = logging.Formatter("%(asctime)s | %(levelname)s | %(name)s | %(message)s")
_root = logging.getLogger()
_root.setLevel(logging.INFO)
for _h in (
    logging.StreamHandler(sys.stdout),
    logging.FileHandler(LOG_PATH, encoding="utf-8"),
    BufferHandler(),
):
    _h.setFormatter(_fmt)
    _root.addHandler(_h)
logger = logging.getLogger("csvpdfbot")

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
OWNER_ID_RAW = os.getenv("OWNER_ID", "").strip()
PORT = int(os.getenv("PORT", "10000"))
START_TIME = time.time()

try:
    OWNER_ID = int(OWNER_ID_RAW) if OWNER_ID_RAW else 0
except ValueError:
    OWNER_ID = 0


DEFAULT_SETTINGS: Dict[str, Any] = {
    "title": "Examination Question Paper",
    "subtitle": "Generated from CSV",
    "set_name": "A",
    "marks": "auto",
    "time": "15 min",
    "footer_text": "Official Channel",
    "footer_link": "https://t.me/",
    "watermark_enabled": True,
    "watermark_text": "CONFIDENTIAL",
    "watermark_opacity": 8,
    "watermark_image_enabled": False,
    "logo_enabled": True,
    "answer_enabled": True,
    "explanation_enabled": True,
    "columns": 2,
    "page_size": "A4",
    "theme": "green",
    "bn_font": "Noto Sans Bengali",
    "en_font": "Inter",
    "math_font": "STIX Two Math",
}

# Curated, professional Google Fonts. WeasyPrint will fetch via @import.
BN_FONTS = [
    "Noto Sans Bengali", "Hind Siliguri", "Baloo Da 2", "Tiro Bangla",
    "Atma", "Mina", "Galada", "Anek Bangla", "Noto Serif Bengali",
]
EN_FONTS = [
    "Inter", "Poppins", "Roboto", "Montserrat", "Lato", "Open Sans",
    "Nunito", "Work Sans", "Manrope", "Source Sans 3", "Merriweather",
    "Playfair Display", "Raleway", "Rubik", "DM Sans", "Mulish",
]
MATH_FONTS = [
    "STIX Two Math", "STIX Two Text", "Lora", "Source Serif 4",
    "JetBrains Mono", "Fira Code", "IBM Plex Mono", "Roboto Mono",
    "Cambay", "Spectral",
]

DEFAULT_BUTTON_LABELS: Dict[str, str] = {
    "title": "Title",
    "subtitle": "Subtitle",
    "set_name": "Set",
    "marks": "Marks",
    "time": "Time",
    "footer_text": "Footer Text",
    "footer_link": "Footer Link",
    "watermark_text": "Edit Text",
    "watermark_image": "Watermark Image",
    "logo_image": "Logo Image",
    "thumbnail_image": "Thumbnail",
    "watermark_opacity": "Opacity",
    "logo_enabled": "Logo",
    "watermark_enabled": "Watermark",
    "watermark_image_enabled": "Image WM",
    "answer_enabled": "Answer",
    "explanation_enabled": "Explain",
    "columns": "Columns",
    "page_size": "Page",
    "theme": "Theme",
    "bn_font": "Bangla Font",
    "en_font": "English Font",
    "math_font": "Math Font",
    "reset": "Reset",
    "generate": "Generate PDF",
}

THEMES = {
    "green":  {"primary": "#0f766e", "accent": "#16a34a", "light": "#ecfdf5", "border": "#99f6e4"},
    "blue":   {"primary": "#1d4ed8", "accent": "#0284c7", "light": "#eff6ff", "border": "#bfdbfe"},
    "purple": {"primary": "#7e22ce", "accent": "#9333ea", "light": "#faf5ff", "border": "#e9d5ff"},
    "red":    {"primary": "#b91c1c", "accent": "#dc2626", "light": "#fef2f2", "border": "#fecaca"},
    "black":  {"primary": "#111827", "accent": "#374151", "light": "#f9fafb", "border": "#d1d5db"},
}

COLUMN_ALIASES = {
    "question": ["question", "ques", "q", "প্রশ্ন", "প্রশ্নপত্র"],
    "option_a": ["option_a", "a", "option a", "ক", "option1"],
    "option_b": ["option_b", "b", "option b", "খ", "option2"],
    "option_c": ["option_c", "c", "option c", "গ", "option3"],
    "option_d": ["option_d", "d", "option d", "ঘ", "option4"],
    "answer": ["answer", "ans", "উত্তর", "সঠিক উত্তর"],
    "explanation": ["explanation", "explain", "ব্যাখ্যা", "সমাধান"],
    "marks": ["marks", "mark", "মান", "নম্বর"],
}

LATEX_REPLACEMENTS = [
    (r"\\frac\{([^{}]+)\}\{([^{}]+)\}", r"<span class='frac'><span>\1</span><span>\2</span></span>"),
    (r"\\sqrt\{([^{}]+)\}", r"√(<span>\1</span>)"),
    (r"\\times", "×"), (r"\\div", "÷"), (r"\\pm", "±"),
    (r"\\leq", "≤"), (r"\\geq", "≥"), (r"\\neq", "≠"),
    (r"\\alpha", "α"), (r"\\beta", "β"), (r"\\gamma", "γ"),
    (r"\\theta", "θ"), (r"\\pi", "π"), (r"\\Delta", "Δ"),
]

FIELD_LABELS = {
    "title": "Title",
    "subtitle": "Subtitle",
    "set_name": "Set Name",
    "marks": "Total Marks",
    "time": "Time Limit",
    "footer_text": "Footer Text",
    "footer_link": "Footer Link",
    "watermark_text": "Watermark Text",
    "watermark_opacity": "Watermark Opacity (0–100)",
}

# ---------------------------------------------------------------------------
# Persistent state
# ---------------------------------------------------------------------------

USER_SETTINGS: Dict[int, Dict[str, Any]] = {}
USER_CSV: Dict[int, bytes] = {}
USER_CSV_NAME: Dict[int, str] = {}
WAITING_FOR: Dict[int, str] = {}
PANEL_MSG: Dict[int, Tuple[int, int]] = {}
BUTTON_LABELS: Dict[str, str] = dict(DEFAULT_BUTTON_LABELS)
ACTIVITY: Dict[int, Dict[str, Any]] = {}
GENERATION_COUNT = 0

ADMIN_IDS: Set[int] = set()       # full composer access
GENERATOR_IDS: Set[int] = set()   # generate-only access
USER_LOCKS: Dict[int, asyncio.Lock] = defaultdict(asyncio.Lock)

# --- Quiz collection (forward Telegram quizzes → PDF) -----------------------
USER_QUIZ: Dict[int, List[Dict[str, Any]]] = defaultdict(list)
QUIZ_STATUS_MSG: Dict[int, Tuple[int, int]] = {}   # uid -> (chat_id, msg_id)
QUIZ_MODE_DEFAULT_ON = True   # any forwarded poll is auto-collected

# --- Force subscription ------------------------------------------------------
# Each entry: {"chat": "@channel" or -100xxxx, "title": str, "link": str, "button": str}
FORCE_CHANNELS: List[Dict[str, str]] = []
FORCE_CAPTION: str = (
    "<b>Membership required</b>\n\n"
    "To use this service, please join the channel(s) below and then tap "
    "<b>I have joined</b> to verify your access."
)


def _save_state() -> None:
    try:
        payload = {
            "user_settings": {str(k): v for k, v in USER_SETTINGS.items()},
            "button_labels": BUTTON_LABELS,
            "user_csv_name": {str(k): v for k, v in USER_CSV_NAME.items()},
            "user_csv": {str(k): base64.b64encode(v).decode() for k, v in USER_CSV.items()},
            "admins": list(ADMIN_IDS),
            "generators": list(GENERATOR_IDS),
            "user_quiz": {str(k): v for k, v in USER_QUIZ.items() if v},
            "force_channels": FORCE_CHANNELS,
            "force_caption": FORCE_CAPTION,
        }
        STATE_PATH.write_text(json.dumps(payload), encoding="utf-8")
    except Exception:
        logger.exception("Failed to persist state")


def _load_state() -> None:
    global FORCE_CAPTION
    if not STATE_PATH.exists():
        return
    try:
        payload = json.loads(STATE_PATH.read_text(encoding="utf-8"))
        for k, v in (payload.get("user_settings") or {}).items():
            merged = DEFAULT_SETTINGS.copy()
            merged.update(v or {})
            USER_SETTINGS[int(k)] = merged
        for k, v in (payload.get("button_labels") or {}).items():
            if k in DEFAULT_BUTTON_LABELS:
                BUTTON_LABELS[k] = v
        for k, v in (payload.get("user_csv_name") or {}).items():
            USER_CSV_NAME[int(k)] = v
        for k, v in (payload.get("user_csv") or {}).items():
            try:
                USER_CSV[int(k)] = base64.b64decode(v)
            except Exception:
                pass
        for uid in payload.get("admins") or []:
            try: ADMIN_IDS.add(int(uid))
            except Exception: pass
        for uid in payload.get("generators") or []:
            try: GENERATOR_IDS.add(int(uid))
            except Exception: pass
        for k, v in (payload.get("user_quiz") or {}).items():
            try: USER_QUIZ[int(k)] = list(v) if isinstance(v, list) else []
            except Exception: pass
        fc = payload.get("force_channels")
        if isinstance(fc, list):
            FORCE_CHANNELS.clear()
            for entry in fc:
                if isinstance(entry, dict) and entry.get("chat"):
                    FORCE_CHANNELS.append({
                        "chat": str(entry.get("chat", "")),
                        "title": str(entry.get("title", "Channel")),
                        "link": str(entry.get("link", "")),
                        "button": str(entry.get("button", "Join Channel")),
                    })
        cap = payload.get("force_caption")
        if isinstance(cap, str) and cap.strip():
            FORCE_CAPTION = cap
        logger.info("State restored from %s", STATE_PATH)
    except Exception:
        logger.exception("Failed to load state")


# ---------------------------------------------------------------------------
# Roles
# ---------------------------------------------------------------------------

def is_owner(uid: Optional[int]) -> bool:
    return bool(uid and OWNER_ID and uid == OWNER_ID)


def is_admin(uid: Optional[int]) -> bool:
    """Owner or explicitly-added admin (full composer)."""
    return is_owner(uid) or (uid is not None and uid in ADMIN_IDS)


def is_generator(uid: Optional[int]) -> bool:
    """Has at least generate-only access."""
    return is_admin(uid) or (uid is not None and uid in GENERATOR_IDS)


def role_label(uid: int) -> str:
    if is_owner(uid): return "Owner"
    if uid in ADMIN_IDS: return "Administrator"
    if uid in GENERATOR_IDS: return "Generator"
    return "Guest"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def get_settings(user_id: int) -> Dict[str, Any]:
    if user_id not in USER_SETTINGS:
        USER_SETTINGS[user_id] = DEFAULT_SETTINGS.copy()
    else:
        for k, v in DEFAULT_SETTINGS.items():
            USER_SETTINGS[user_id].setdefault(k, v)
    return USER_SETTINGS[user_id]


def lbl(key: str) -> str:
    return BUTTON_LABELS.get(key, DEFAULT_BUTTON_LABELS.get(key, key))


def track(user, action: str) -> None:
    if not user:
        return
    ACTIVITY[user.id] = {
        "name": (user.full_name or user.username or str(user.id))[:48],
        "username": user.username,
        "last_action": action,
        "last_seen": time.time(),
    }


def main_keyboard(settings: Dict[str, Any], owner_view: bool) -> InlineKeyboardMarkup:
    def flag(key: str) -> str:
        return "ON" if settings.get(key) else "OFF"

    rows = [
        [
            InlineKeyboardButton(lbl("title"), callback_data="set:title"),
            InlineKeyboardButton(lbl("subtitle"), callback_data="set:subtitle"),
        ],
        [
            InlineKeyboardButton(lbl("set_name"), callback_data="set:set_name"),
            InlineKeyboardButton(lbl("marks"), callback_data="set:marks"),
            InlineKeyboardButton(lbl("time"), callback_data="set:time"),
        ],
        [
            InlineKeyboardButton(lbl("footer_text"), callback_data="set:footer_text"),
            InlineKeyboardButton(lbl("footer_link"), callback_data="set:footer_link"),
        ],
        [
            InlineKeyboardButton(f"{lbl('watermark_enabled')} · {flag('watermark_enabled')}", callback_data="toggle:watermark_enabled"),
            InlineKeyboardButton(lbl("watermark_text"), callback_data="set:watermark_text"),
            InlineKeyboardButton(f"{lbl('watermark_opacity')}: {settings.get('watermark_opacity', 8)}%", callback_data="set:watermark_opacity"),
        ],
        [
            InlineKeyboardButton(f"{lbl('watermark_image_enabled')} · {flag('watermark_image_enabled')}", callback_data="toggle:watermark_image_enabled"),
            InlineKeyboardButton(lbl("watermark_image"), callback_data="upload:watermark_image"),
        ],
        [
            InlineKeyboardButton(f"{lbl('logo_enabled')} · {flag('logo_enabled')}", callback_data="toggle:logo_enabled"),
            InlineKeyboardButton(lbl("logo_image"), callback_data="upload:logo_image"),
            InlineKeyboardButton(lbl("thumbnail_image"), callback_data="upload:thumbnail_image"),
        ],
        [
            InlineKeyboardButton(f"{lbl('answer_enabled')} · {flag('answer_enabled')}", callback_data="toggle:answer_enabled"),
            InlineKeyboardButton(f"{lbl('explanation_enabled')} · {flag('explanation_enabled')}", callback_data="toggle:explanation_enabled"),
        ],
        [
            InlineKeyboardButton(f"{lbl('columns')}: {settings['columns']}", callback_data="cycle:columns"),
            InlineKeyboardButton(f"{lbl('page_size')}: {settings['page_size']}", callback_data="cycle:page_size"),
            InlineKeyboardButton(f"{lbl('theme')}: {settings['theme'].title()}", callback_data="cycle:theme"),
        ],
        [
            InlineKeyboardButton(f"BN: {settings.get('bn_font', 'Noto Sans Bengali')}", callback_data="cycle:bn_font"),
            InlineKeyboardButton(f"EN: {settings.get('en_font', 'Inter')}", callback_data="cycle:en_font"),
        ],
        [
            InlineKeyboardButton(f"Math: {settings.get('math_font', 'STIX Two Math')}", callback_data="cycle:math_font"),
        ],
        [
            InlineKeyboardButton("Front Cover", callback_data="upload:front_page"),
            InlineKeyboardButton("Back Cover", callback_data="upload:back_page"),
            InlineKeyboardButton("Quiz Mode", callback_data="quiz:start"),
        ],
        [
            InlineKeyboardButton(lbl("reset"), callback_data="reset"),
            InlineKeyboardButton(lbl("generate"), callback_data="generate"),
        ],
    ]
    return InlineKeyboardMarkup(rows)


def generator_keyboard(settings: Dict[str, Any]) -> InlineKeyboardMarkup:
    """Compact panel for generate-only users."""
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(lbl("title"), callback_data="set:title"),
         InlineKeyboardButton(lbl("subtitle"), callback_data="set:subtitle")],
        [InlineKeyboardButton(lbl("set_name"), callback_data="set:set_name"),
         InlineKeyboardButton(lbl("marks"), callback_data="set:marks"),
         InlineKeyboardButton(lbl("time"), callback_data="set:time")],
        [InlineKeyboardButton(f"{lbl('columns')}: {settings['columns']}", callback_data="cycle:columns"),
         InlineKeyboardButton(f"{lbl('theme')}: {settings['theme'].title()}", callback_data="cycle:theme")],
        [InlineKeyboardButton(lbl("reset"), callback_data="reset"),
         InlineKeyboardButton(lbl("generate"), callback_data="generate")],
    ])


def cancel_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton("Cancel", callback_data="cancel")]])


def buttons_editor_keyboard() -> InlineKeyboardMarkup:
    rows: List[List[InlineKeyboardButton]] = []
    keys = list(DEFAULT_BUTTON_LABELS.keys())
    for i in range(0, len(keys), 2):
        chunk = keys[i:i + 2]
        rows.append([InlineKeyboardButton(f"{lbl(k)}", callback_data=f"btnedit:{k}") for k in chunk])
    rows.append([InlineKeyboardButton("Reset Labels", callback_data="btnreset"),
                 InlineKeyboardButton("Close", callback_data="btnclose")])
    return InlineKeyboardMarkup(rows)


def panel_text(user_id: int, settings: Dict[str, Any], note: Optional[str] = None) -> str:
    csv_status = "Loaded" if user_id in USER_CSV else "Not uploaded"
    csv_name = USER_CSV_NAME.get(user_id, "—")
    role = role_label(user_id)
    wm_mode = "Image" if settings.get("watermark_image_enabled") and wm_path(user_id).exists() else "Text"
    logo_mode = "Image" if logo_path(user_id).exists() else "Default"
    thumb_mode = "Set" if thumb_path(user_id).exists() else "None"
    front_mode = "Set" if front_path(user_id).exists() else "None"
    back_mode = "Set" if back_path(user_id).exists() else "None"
    quiz_count = len(USER_QUIZ.get(user_id, []))
    body = textwrap.dedent(f"""
    <b>PDF Composer</b>
    <i>Role: {role}</i>

    <b>Document</b>
      • Title: <code>{html.escape(str(settings['title']))}</code>
      • Subtitle: <code>{html.escape(str(settings['subtitle']))}</code>
      • Set / Marks / Time: <code>{html.escape(str(settings['set_name']))}</code> · <code>{html.escape(str(settings['marks']))}</code> · <code>{html.escape(str(settings['time']))}</code>

    <b>Footer</b>
      • Text: <code>{html.escape(str(settings['footer_text']))}</code>
      • Link: <code>{html.escape(str(settings['footer_link']))}</code>

    <b>Layout</b>
      • Columns: <code>{settings['columns']}</code> · Page: <code>{settings['page_size']}</code> · Theme: <code>{settings['theme'].title()}</code>
      • Watermark ({wm_mode}): <code>{html.escape(str(settings['watermark_text']))}</code> · Opacity: <code>{settings.get('watermark_opacity', 8)}%</code>
      • Logo: <code>{logo_mode}</code> · Thumbnail: <code>{thumb_mode}</code>
      • Fonts — BN: <code>{html.escape(str(settings.get('bn_font', '—')))}</code> · EN: <code>{html.escape(str(settings.get('en_font', '—')))}</code> · Math: <code>{html.escape(str(settings.get('math_font', '—')))}</code>

    <b>Source</b>
      • CSV: <code>{html.escape(csv_name)}</code> ({csv_status})
    """).strip()
    if note:
        body += f"\n\n<b>›</b> <i>{html.escape(note)}</i>"
    return body


def help_text(user_id: int) -> str:
    if is_owner(user_id):
        return textwrap.dedent("""
        <b>Owner Console</b>

        <b>Composer</b>
        <code>/start</code> — Open composer
        <code>/panel</code> — Refresh panel
        <code>/generate</code> — Build the PDF
        <code>/reset</code> — Restore defaults
        <code>/status</code> — Current configuration
        <code>/clear</code> — Remove the loaded CSV

        <b>Access management</b>
        <code>/users</code> — List all administrators &amp; generators
        <code>/admins</code> — List administrators
        <code>/addadmin &lt;id&gt;</code> (alias <code>/promote</code>) — Promote a user to administrator
        <code>/removeadmin &lt;id&gt;</code> (alias <code>/demote</code>) — Revoke administrator access
        <code>/gens</code> — List generator users
        <code>/addgen &lt;id&gt;</code> (alias <code>/allow</code>) — Grant generate-only access
        <code>/removegen &lt;id&gt;</code> (alias <code>/deny</code>) — Revoke generate-only access

        <b>Customisation &amp; ops</b>
        <code>/buttons</code> — Customise button labels
        <code>/logs</code> — Activity, memory &amp; recent errors
        <code>/restart</code> — Restart the bot (state preserved)
        <code>/help</code> — Show this message

        <b>Quiz → PDF</b>
        <code>/quiz</code> — Open the quiz collector card
        <code>/quizclear</code> — Clear collected quiz polls
        <code>/genquiz</code> — Generate PDF from collected polls
        <i>(Forwarding any quiz poll auto-collects it.)</i>

        <b>Front / Back covers</b>
        <code>/frontpage</code>, <code>/backpage</code> — Upload PDF or image cover
        <code>/removefront</code>, <code>/removeback</code> — Remove cover

        <b>Required channels (force-subscribe)</b>
        <code>/channels</code> — Show / manage required channels
        <code>/addchannel @ch | Title | https://t.me/ch | Button</code>
        <code>/removechannel &lt;index&gt;</code>
        <code>/setjoinmsg &lt;text&gt;</code> — Customise the gate caption

        <b>PDF rename</b> — Reply to any generated PDF with the desired file name.

        <i>All commands also accept the </i><code>.</code><i> prefix.</i>
        """).strip()

    if is_admin(user_id):
        return textwrap.dedent("""
        <b>Administrator Commands</b>

        <code>/start</code> — Open composer
        <code>/panel</code> — Refresh panel
        <code>/generate</code> — Build the PDF
        <code>/reset</code> — Restore defaults
        <code>/status</code> — Current configuration
        <code>/clear</code> — Remove the loaded CSV
        <code>/help</code> — Show this message

        <b>PDF rename</b> — Reply to any generated PDF with the desired file name.

        <i>All commands also accept the </i><code>.</code><i> prefix.</i>
        """).strip()

    if is_generator(user_id):
        return textwrap.dedent("""
        <b>Available Commands</b>

        <code>/start</code> — Open the generator panel
        <code>/generate</code> — Build the PDF from your CSV
        <code>/reset</code> — Restore defaults
        <code>/clear</code> — Remove the loaded CSV
        <code>/help</code> — Show this message

        <b>PDF rename</b> — Reply to any generated PDF with the desired file name.

        <i>All commands also accept the </i><code>.</code><i> prefix.</i>
        """).strip()

    return textwrap.dedent("""
    <b>Restricted Service</b>

    This is a private utility. Please contact the administrator to request
    access. Provide your numeric Telegram ID when requesting permission.
    """).strip()


# ---------------------------------------------------------------------------
# Panel rendering
# ---------------------------------------------------------------------------

async def send_or_update_panel(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    note: Optional[str] = None,
    waiting_field: Optional[str] = None,
) -> None:
    user = update.effective_user
    if not user:
        return
    user_id = user.id
    if not is_generator(user_id):
        return
    settings = get_settings(user_id)

    if waiting_field in {"watermark_image", "logo_image", "thumbnail_image", "front_page", "back_page"}:
        kind_map = {
            "watermark_image": ("watermark image", "PNG or JPG"),
            "logo_image": ("logo image", "PNG or JPG"),
            "thumbnail_image": ("thumbnail image", "PNG or JPG"),
            "front_page": ("front cover", "PDF or image"),
            "back_page": ("back cover", "PDF or image"),
        }
        kind, fmt = kind_map[waiting_field]
        text = panel_text(user_id, settings, note) + (
            f"\n\n<b>Awaiting upload</b>\nSend a {fmt} file to use as the {kind}."
        )
        markup = cancel_keyboard()
    elif waiting_field and waiting_field.startswith("btnlabel:"):
        key = waiting_field.split(":", 1)[1]
        text = panel_text(user_id, settings, note) + (
            f"\n\n<b>Awaiting input</b>\nReply with the new label (emoji allowed) for <b>{html.escape(key)}</b>."
        )
        markup = cancel_keyboard()
    elif waiting_field:
        label = FIELD_LABELS.get(waiting_field, waiting_field)
        text = panel_text(user_id, settings, note) + (
            f"\n\n<b>Awaiting input</b>\nReply with the new <b>{html.escape(label)}</b>."
        )
        markup = cancel_keyboard()
    else:
        text = panel_text(user_id, settings, note)
        markup = main_keyboard(settings, is_admin(user_id)) if is_admin(user_id) else generator_keyboard(settings)

    chat_id = update.effective_chat.id if update.effective_chat else user_id
    panel = PANEL_MSG.get(user_id)

    if panel and panel[0] == chat_id:
        try:
            await context.bot.edit_message_text(
                chat_id=panel[0], message_id=panel[1],
                text=text, reply_markup=markup,
                parse_mode=ParseMode.HTML, disable_web_page_preview=True,
            )
            return
        except BadRequest as exc:
            if "not modified" in str(exc).lower():
                return
            logger.info("Panel edit failed, sending new: %s", exc)

    sent = await context.bot.send_message(
        chat_id=chat_id, text=text, reply_markup=markup,
        parse_mode=ParseMode.HTML, disable_web_page_preview=True,
    )
    PANEL_MSG[user_id] = (sent.chat_id, sent.message_id)


# ---------------------------------------------------------------------------
# Owner-only utilities
# ---------------------------------------------------------------------------

def _format_uptime(seconds: float) -> str:
    s = int(seconds)
    d, s = divmod(s, 86400)
    h, s = divmod(s, 3600)
    m, s = divmod(s, 60)
    parts = []
    if d: parts.append(f"{d}d")
    if h: parts.append(f"{h}h")
    if m: parts.append(f"{m}m")
    parts.append(f"{s}s")
    return " ".join(parts)


def _memory_mb() -> float:
    try:
        usage = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        if sys.platform == "darwin":
            return usage / (1024 * 1024)
        return usage / 1024
    except Exception:
        return 0.0


def build_logs_text() -> str:
    now = time.time()
    active_window = 600
    active = [a for a in ACTIVITY.values() if now - a["last_seen"] < active_window]
    recent_errors = [(t, m) for (t, m) in ERROR_BUFFER if now - t < 3600]

    lines: List[str] = []
    lines.append("<b>Bot Status</b>")
    lines.append(f"  • Uptime: <code>{_format_uptime(now - START_TIME)}</code>")
    lines.append(f"  • Memory: <code>{_memory_mb():.1f} MB</code>")
    lines.append(f"  • PID: <code>{os.getpid()}</code>")
    lines.append(f"  • Generated PDFs: <code>{GENERATION_COUNT}</code>")
    lines.append(f"  • Admins: <code>{len(ADMIN_IDS)}</code> · Generators: <code>{len(GENERATOR_IDS)}</code>")
    lines.append("")
    lines.append(f"<b>Active Users (last 10 min): {len(active)}</b>")
    if active:
        for a in sorted(active, key=lambda x: -x["last_seen"])[:15]:
            ago = int(now - a["last_seen"])
            lines.append(f"  • {html.escape(a['name'])} — <i>{html.escape(a['last_action'])}</i> ({ago}s ago)")
    else:
        lines.append("  • <i>None</i>")
    lines.append("")
    lines.append(f"<b>Errors (last hour): {len(recent_errors)}</b>")
    if recent_errors:
        for t, m in recent_errors[-6:]:
            stamp = datetime.fromtimestamp(t, tz=timezone.utc).strftime("%H:%M:%S")
            snippet = html.escape(m[-220:])
            lines.append(f"  <code>[{stamp}]</code> {snippet}")
    else:
        lines.append("  • <i>No errors in the last hour</i>")
    return "\n".join(lines)


async def cmd_logs(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.effective_message
    if not msg:
        return
    text = build_logs_text()
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("Refresh", callback_data="logs:refresh"),
         InlineKeyboardButton("Download Log File", callback_data="logs:download")],
    ])
    await msg.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=kb, disable_web_page_preview=True)


async def cmd_restart(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.effective_message
    if not msg:
        return
    _save_state()
    await msg.reply_text("⟳ Restart initiated. State preserved.", parse_mode=ParseMode.HTML)
    try:
        (DATA_DIR / "restart_target.json").write_text(
            json.dumps({"chat_id": msg.chat_id}), encoding="utf-8"
        )
    except Exception:
        pass
    logger.warning("Restart requested by owner")
    await asyncio.sleep(0.5)
    os.execv(sys.executable, [sys.executable, *sys.argv])


# ---------------------------------------------------------------------------
# Role management commands
# ---------------------------------------------------------------------------

def _parse_target_id(msg, args: str) -> Optional[int]:
    """Accept a numeric ID in args, or the user_id of a replied-to message."""
    if args:
        m = re.search(r"-?\d+", args)
        if m:
            try: return int(m.group(0))
            except Exception: return None
    if msg.reply_to_message and msg.reply_to_message.from_user:
        return msg.reply_to_message.from_user.id
    return None


def _format_id_list(ids: Set[int], title: str) -> str:
    if not ids:
        return f"<b>{title}</b>\n  • <i>None</i>"
    out = [f"<b>{title}</b>"]
    for uid in sorted(ids):
        info = ACTIVITY.get(uid)
        name = html.escape(info["name"]) if info else f"User {uid}"
        out.append(f"  • <code>{uid}</code> — {name}")
    return "\n".join(out)


async def handle_role_command(cmd: str, args: str, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.effective_message
    if not msg:
        return

    if cmd == "admins":
        await msg.reply_text(_format_id_list(ADMIN_IDS, "Administrators"), parse_mode=ParseMode.HTML)
        return
    if cmd == "gens":
        await msg.reply_text(_format_id_list(GENERATOR_IDS, "Generator Users"), parse_mode=ParseMode.HTML)
        return

    target = _parse_target_id(msg, args)
    if not target:
        await msg.reply_text(
            "Provide a numeric user ID or reply to that user's message.\n"
            "Example: <code>/addadmin 123456789</code>",
            parse_mode=ParseMode.HTML,
        )
        return

    if target == OWNER_ID:
        await msg.reply_text("The owner already holds full privileges.")
        return

    if cmd == "addadmin":
        ADMIN_IDS.add(target); GENERATOR_IDS.discard(target); _save_state()
        await msg.reply_text(f"✓ Added <code>{target}</code> as administrator.", parse_mode=ParseMode.HTML)
    elif cmd == "removeadmin":
        if target in ADMIN_IDS:
            ADMIN_IDS.discard(target); _save_state()
            await msg.reply_text(f"✓ Removed administrator <code>{target}</code>.", parse_mode=ParseMode.HTML)
        else:
            await msg.reply_text("That user is not an administrator.")
    elif cmd == "addgen":
        if target in ADMIN_IDS:
            await msg.reply_text("That user is already an administrator.")
            return
        GENERATOR_IDS.add(target); _save_state()
        await msg.reply_text(f"✓ Granted generate-only access to <code>{target}</code>.", parse_mode=ParseMode.HTML)
    elif cmd == "removegen":
        if target in GENERATOR_IDS:
            GENERATOR_IDS.discard(target); _save_state()
            await msg.reply_text(f"✓ Revoked access for <code>{target}</code>.", parse_mode=ParseMode.HTML)
        else:
            await msg.reply_text("That user does not have generator access.")


# ---------------------------------------------------------------------------
# Command dispatcher (supports / and .)
# ---------------------------------------------------------------------------

COMMAND_RE = re.compile(r"^[\/\.]([a-zA-Z_]+)(?:@\S+)?(?:\s+(.*))?$", re.DOTALL)


async def dispatch_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    msg = update.effective_message
    if not msg or not msg.text:
        return False
    match = COMMAND_RE.match(msg.text.strip())
    if not match:
        return False
    cmd = match.group(1).lower()
    args = match.group(2) or ""
    user = update.effective_user
    if not user:
        return True

    track(user, f"/{cmd}")

    # Public commands
    if cmd == "help":
        await msg.reply_text(help_text(user.id), parse_mode=ParseMode.HTML, disable_web_page_preview=True)
        return True
    if cmd == "start":
        await cmd_start(update, context)
        return True

    # Owner-only
    owner_cmds = {
        "buttons", "logs", "restart",
        "admins", "gens", "users",
        "addadmin", "removeadmin",
        "addgen", "removegen",
        "allow", "deny", "promote", "demote",
        "channels", "addchannel", "removechannel", "setjoinmsg",
    }
    alias_map = {
        "users": "users",
        "allow": "addgen",
        "deny": "removegen",
        "promote": "addadmin",
        "demote": "removeadmin",
    }
    if cmd in owner_cmds:
        if not is_owner(user.id):
            await msg.reply_text("This command is restricted to the owner.")
            return True
        if cmd == "buttons":
            await msg.reply_text(
                "<b>Button Editor</b>\nSelect a button to rename (emoji supported).",
                parse_mode=ParseMode.HTML, reply_markup=buttons_editor_keyboard(),
            )
        elif cmd == "logs":
            await cmd_logs(update, context)
        elif cmd == "restart":
            await cmd_restart(update, context)
        elif cmd == "users":
            text = (
                _format_id_list(ADMIN_IDS, "Administrators")
                + "\n\n"
                + _format_id_list(GENERATOR_IDS, "Generator Users")
            )
            await msg.reply_text(text, parse_mode=ParseMode.HTML)
        elif cmd == "channels":
            await cmd_channels(update, context)
        elif cmd == "addchannel":
            await cmd_addchannel(update, context, args)
        elif cmd == "removechannel":
            await cmd_removechannel(update, context, args)
        elif cmd == "setjoinmsg":
            await cmd_setjoinmsg(update, context, args)
        else:
            real = alias_map.get(cmd, cmd)
            await handle_role_command(real, args, update, context)
        return True

    # Admin-only commands (front / back page management)
    admin_cmds = {"frontpage", "backpage", "removefront", "removeback"}
    if cmd in admin_cmds:
        if not is_admin(user.id):
            await msg.reply_text("This command is restricted to administrators.")
            return True
        if not await enforce_subscription(update, context):
            return True
        if cmd == "frontpage":
            WAITING_FOR[user.id] = "front_page"
            await msg.reply_text(
                "Send the <b>front cover</b> as a PDF or image. It will be inserted "
                "as the first page(s) of every generated PDF.",
                parse_mode=ParseMode.HTML,
            )
        elif cmd == "backpage":
            WAITING_FOR[user.id] = "back_page"
            await msg.reply_text(
                "Send the <b>back cover</b> as a PDF or image. It will be appended "
                "as the final page(s) of every generated PDF.",
                parse_mode=ParseMode.HTML,
            )
        elif cmd == "removefront":
            try: front_path(user.id).unlink()
            except FileNotFoundError: pass
            await msg.reply_text("✓ Front cover removed.")
        elif cmd == "removeback":
            try: back_path(user.id).unlink()
            except FileNotFoundError: pass
            await msg.reply_text("✓ Back cover removed.")
        return True

    # Generator-or-better commands
    gen_cmds = {
        "panel", "menu", "generate", "reset", "status", "clear",
        "quiz", "quizclear", "genquiz",
    }
    if cmd in gen_cmds:
        if not is_generator(user.id):
            await msg.reply_text("This command is not available for your account.")
            return True
        if not await enforce_subscription(update, context):
            return True
        if cmd in {"panel", "menu"}:
            PANEL_MSG.pop(user.id, None)
            await send_or_update_panel(update, context)
        elif cmd == "generate" or cmd == "genquiz":
            await generate_for_user(update, context)
        elif cmd == "reset":
            USER_SETTINGS[user.id] = DEFAULT_SETTINGS.copy(); _save_state()
            await send_or_update_panel(update, context, note="Settings restored to defaults.")
        elif cmd == "status":
            await send_or_update_panel(update, context)
        elif cmd == "clear":
            USER_CSV.pop(user.id, None); USER_CSV_NAME.pop(user.id, None); _save_state()
            await send_or_update_panel(update, context, note="CSV cleared.")
        elif cmd == "quiz":
            await cmd_quizstart(update, context)
        elif cmd == "quizclear":
            await cmd_quizclear(update, context)
        return True

    await msg.reply_text("Unknown command.")
    return True


# ---------------------------------------------------------------------------
# Handlers
# ---------------------------------------------------------------------------

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    msg = update.effective_message
    if not user or not msg:
        return
    if not is_generator(user.id):
        await msg.reply_text(
            "Welcome.\n\nThis is a private CSV → PDF utility. "
            "Access is limited to authorised users.\n\n"
            f"Your Telegram ID: <code>{user.id}</code>\n"
            "Please share this ID with the administrator to request access.",
            parse_mode=ParseMode.HTML,
        )
        return
    if not await enforce_subscription(update, context):
        return
    PANEL_MSG.pop(user.id, None)
    WAITING_FOR.pop(user.id, None)
    await send_or_update_panel(update, context, note="Upload a CSV file or forward quiz polls to begin.")


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.effective_message
    user = update.effective_user
    if not msg or not user:
        return

    # PDF rename via reply
    if (
        msg.reply_to_message
        and msg.reply_to_message.document
        and (msg.reply_to_message.document.mime_type or "").lower() == "application/pdf"
        and is_generator(user.id)
        and msg.text and not msg.text.startswith(("/", "."))
    ):
        await rename_pdf_via_reply(update, context)
        return

    if msg.text and msg.text[:1] in "/.":
        if await dispatch_command(update, context):
            return

    if not is_generator(user.id):
        return

    field = WAITING_FOR.pop(user.id, "")
    if not field:
        return

    value = (msg.text or "").strip()
    note = ""

    if field.startswith("btnlabel:"):
        if not is_owner(user.id):
            return
        key = field.split(":", 1)[1]
        if key in DEFAULT_BUTTON_LABELS:
            BUTTON_LABELS[key] = value or DEFAULT_BUTTON_LABELS[key]
            note = f"Button '{key}' renamed."
    else:
        settings = get_settings(user.id)
        if field == "watermark_opacity":
            try:
                n = max(0, min(100, int(re.sub(r"\D", "", value) or "0")))
                settings[field] = n
                note = f"Watermark opacity set to {n}%."
            except Exception:
                note = "Invalid number."
        else:
            settings[field] = value
            note = f"{FIELD_LABELS.get(field, field)} updated."

    _save_state()
    try: await msg.delete()
    except Exception: pass

    track(user, f"set {field}")
    await send_or_update_panel(update, context, note=note)


async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query:
        return
    await query.answer()
    user = update.effective_user
    if not user or not is_generator(user.id):
        try: await query.answer("Restricted.", show_alert=True)
        except Exception: pass
        return

    track(user, f"btn:{query.data}")
    data = query.data or ""

    # Force-subscribe verification button — always allowed.
    if data == "fsub:check":
        pending = await missing_subscriptions(context, user.id)
        if not pending:
            try:
                await query.edit_message_text(
                    "<b>Membership verified.</b> You can continue using the bot.",
                    parse_mode=ParseMode.HTML,
                )
            except BadRequest: pass
            return
        try:
            await query.answer("You have not joined all required channels yet.", show_alert=True)
            await query.edit_message_reply_markup(reply_markup=_join_keyboard(pending))
        except BadRequest: pass
        return

    # Quiz status card buttons
    if data == "quiz:gen":
        await generate_for_user(update, context)
        return
    if data == "quiz:clear":
        USER_QUIZ.pop(user.id, None); _save_state()
        await _refresh_quiz_status(context, user.id, query.message.chat_id)
        return
    if data == "quiz:start":
        await _refresh_quiz_status(context, user.id, query.message.chat_id)
        return
    if not await enforce_subscription(update, context):
        return

    settings = get_settings(user.id)
    note: Optional[str] = None
    waiting_field: Optional[str] = None

    # Owner-only: logs / button editor
    if data in {"logs:refresh", "logs:download"} or data.startswith("btnedit:") or data in {"btnreset", "btnclose"}:
        if not is_owner(user.id):
            return
        if data == "logs:refresh":
            try:
                await query.edit_message_text(
                    build_logs_text(), parse_mode=ParseMode.HTML,
                    reply_markup=InlineKeyboardMarkup([[
                        InlineKeyboardButton("Refresh", callback_data="logs:refresh"),
                        InlineKeyboardButton("Download Log File", callback_data="logs:download"),
                    ]]), disable_web_page_preview=True,
                )
            except BadRequest:
                pass
            return
        if data == "logs:download":
            if LOG_PATH.exists():
                await context.bot.send_document(
                    chat_id=query.message.chat_id, document=LOG_PATH.open("rb"),
                    filename="bot.log", caption="Full log file.",
                )
            else:
                await query.answer("No log file yet.", show_alert=True)
            return
        if data.startswith("btnedit:"):
            key = data.split(":", 1)[1]
            WAITING_FOR[user.id] = f"btnlabel:{key}"
            waiting_field = f"btnlabel:{key}"
        elif data == "btnreset":
            BUTTON_LABELS.clear(); BUTTON_LABELS.update(DEFAULT_BUTTON_LABELS); _save_state()
            try: await query.edit_message_reply_markup(reply_markup=buttons_editor_keyboard())
            except BadRequest: pass
            return
        elif data == "btnclose":
            try: await query.message.delete()
            except Exception: pass
            return

    # Admin-only image uploads & toggles
    admin_only_actions = {
        "upload:watermark_image", "upload:logo_image", "upload:thumbnail_image",
        "upload:front_page", "upload:back_page",
        "toggle:watermark_enabled", "toggle:watermark_image_enabled", "toggle:logo_enabled",
        "toggle:answer_enabled", "toggle:explanation_enabled",
        "set:watermark_text", "set:watermark_opacity",
        "set:footer_text", "set:footer_link",
        "cycle:page_size",
    }
    if data in admin_only_actions and not is_admin(user.id):
        try: await query.answer("Administrator only.", show_alert=True)
        except Exception: pass
        return

    if data.startswith("set:"):
        field = data.split(":", 1)[1]
        WAITING_FOR[user.id] = field
        waiting_field = field
    elif data.startswith("upload:"):
        kind = data.split(":", 1)[1]
        WAITING_FOR[user.id] = kind
        waiting_field = kind
    elif data.startswith("toggle:"):
        field = data.split(":", 1)[1]
        settings[field] = not bool(settings.get(field))
        note = f"{field.replace('_', ' ').title()} toggled."
    elif data == "cycle:columns":
        settings["columns"] = 1 if int(settings.get("columns", 2)) == 2 else 2
    elif data == "cycle:page_size":
        settings["page_size"] = "Letter" if settings.get("page_size") == "A4" else "A4"
    elif data == "cycle:theme":
        keys = list(THEMES.keys())
        settings["theme"] = keys[(keys.index(settings.get("theme", "green")) + 1) % len(keys)]
    elif data in ("cycle:bn_font", "cycle:en_font", "cycle:math_font"):
        if not is_admin(user.id):
            try: await query.answer("Administrator only.", show_alert=True)
            except Exception: pass
            return
        field = data.split(":", 1)[1]
        pool = {"bn_font": BN_FONTS, "en_font": EN_FONTS, "math_font": MATH_FONTS}[field]
        cur = settings.get(field, pool[0])
        idx = pool.index(cur) if cur in pool else -1
        settings[field] = pool[(idx + 1) % len(pool)]
        note = f"{field.replace('_', ' ').title()} → {settings[field]}"
    elif data == "reset":
        USER_SETTINGS[user.id] = DEFAULT_SETTINGS.copy()
        note = "Settings restored to defaults."
    elif data == "cancel":
        WAITING_FOR.pop(user.id, None)
        note = "Cancelled."
    elif data == "generate":
        await generate_for_user(update, context)
        return

    _save_state()
    await send_or_update_panel(update, context, note=note, waiting_field=waiting_field)


async def _save_image_upload(context, doc_or_photo, target: Path) -> None:
    file_id = doc_or_photo.file_id
    tg_file = await context.bot.get_file(file_id)
    await tg_file.download_to_drive(custom_path=str(target))


async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.effective_message
    user = update.effective_user
    if not msg or not user:
        return
    if not is_generator(user.id):
        await msg.reply_text("Restricted.")
        return
    track(user, "upload document")

    doc = msg.document
    if not doc:
        return

    file_name = (doc.file_name or "").lower()
    mime = (doc.mime_type or "").lower()
    is_image = mime.startswith("image/") or file_name.endswith((".png", ".jpg", ".jpeg", ".webp"))

    waiting = WAITING_FOR.get(user.id, "")
    image_targets = {
        "watermark_image": (wm_path(user.id), "watermark_image_enabled", "Watermark image saved and enabled."),
        "logo_image":      (logo_path(user.id), "logo_enabled", "Logo image saved and enabled."),
        "thumbnail_image": (thumb_path(user.id), None, "Thumbnail image saved."),
    }

    if waiting in image_targets and is_image:
        if not is_admin(user.id):
            await msg.reply_text("Administrator only.")
            return
        target, enable_key, note = image_targets[waiting]
        WAITING_FOR.pop(user.id, None)
        await _save_image_upload(context, doc, target)
        if enable_key:
            get_settings(user.id)[enable_key] = True
        _save_state()
        try: await msg.delete()
        except Exception: pass
        await send_or_update_panel(update, context, note=note)
        return

    # Front / back cover upload (admin only) — accepts PDF or image documents
    if waiting in {"front_page", "back_page"}:
        if not is_admin(user.id):
            await msg.reply_text("Administrator only.")
            return
        target = front_path(user.id) if waiting == "front_page" else back_path(user.id)
        kind = "front" if waiting == "front_page" else "back"
        WAITING_FOR.pop(user.id, None)
        try:
            note = await _save_front_back(context, doc, target, kind)
        except Exception as exc:
            await msg.reply_text(f"⚠ Could not save {kind} page: {html.escape(str(exc))}",
                                 parse_mode=ParseMode.HTML)
            return
        try: await msg.delete()
        except Exception: pass
        await send_or_update_panel(update, context, note=note)
        return

    if not (file_name.endswith(".csv") or "csv" in mime or mime in {"text/plain", "application/vnd.ms-excel"}):
        await msg.reply_text("Only .csv files are accepted (or use /frontpage / /backpage for cover uploads).")
        return

    await msg.chat.send_action(ChatAction.TYPING)
    tg_file = await context.bot.get_file(doc.file_id)
    data = bytes(await tg_file.download_as_bytearray())
    USER_CSV[user.id] = data
    USER_CSV_NAME[user.id] = doc.file_name or "uploaded.csv"
    # New CSV resets any pending quiz pool
    USER_QUIZ.pop(user.id, None)
    stat = QUIZ_STATUS_MSG.pop(user.id, None)
    if stat:
        try: await context.bot.delete_message(chat_id=stat[0], message_id=stat[1])
        except Exception: pass
    _save_state()

    try: await msg.delete()
    except Exception: pass

    await send_or_update_panel(update, context, note="CSV received and ready.")


async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.effective_message
    user = update.effective_user
    if not msg or not user or not is_generator(user.id):
        return
    waiting = WAITING_FOR.get(user.id, "")
    image_targets = {
        "watermark_image": (wm_path(user.id), "watermark_image_enabled", "Watermark image saved and enabled."),
        "logo_image":      (logo_path(user.id), "logo_enabled", "Logo image saved and enabled."),
        "thumbnail_image": (thumb_path(user.id), None, "Thumbnail image saved."),
    }
    photo = msg.photo[-1] if msg.photo else None
    if not photo:
        return

    # Front/back cover via compressed photo
    if waiting in {"front_page", "back_page"}:
        if not is_admin(user.id):
            await msg.reply_text("Administrator only.")
            return
        target = front_path(user.id) if waiting == "front_page" else back_path(user.id)
        kind = "front" if waiting == "front_page" else "back"
        WAITING_FOR.pop(user.id, None)
        try:
            tg_file = await context.bot.get_file(photo.file_id)
            raw = bytes(await tg_file.download_as_bytearray())
            tmp = target.with_suffix(target.suffix + ".src")
            tmp.write_bytes(raw)
            try:
                pdf_bytes = await asyncio.to_thread(_image_to_pdf_bytes, tmp)
                target.write_bytes(pdf_bytes)
            finally:
                try: tmp.unlink()
                except Exception: pass
        except Exception as exc:
            await msg.reply_text(f"⚠ Could not save {kind} page: {html.escape(str(exc))}",
                                 parse_mode=ParseMode.HTML)
            return
        try: await msg.delete()
        except Exception: pass
        await send_or_update_panel(update, context, note=f"{kind.title()} page (image) saved.")
        return

    if waiting not in image_targets:
        return
    if not is_admin(user.id):
        await msg.reply_text("Administrator only.")
        return
    target, enable_key, note = image_targets[waiting]
    WAITING_FOR.pop(user.id, None)
    await _save_image_upload(context, photo, target)
    if enable_key:
        get_settings(user.id)[enable_key] = True
    _save_state()
    try: await msg.delete()
    except Exception: pass
    await send_or_update_panel(update, context, note=note)


# ---------------------------------------------------------------------------
# PDF rename via reply
# ---------------------------------------------------------------------------

INVALID_FN = re.compile(r'[\\/:*?"<>|\r\n\t]+')


async def rename_pdf_via_reply(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.effective_message
    user = update.effective_user
    if not msg or not user:
        return
    target_msg = msg.reply_to_message
    doc = target_msg.document
    if not doc:
        return
    new_name = INVALID_FN.sub(" ", (msg.text or "").strip()).strip()
    if not new_name:
        await msg.reply_text("Please provide a valid file name.")
        return
    if not new_name.lower().endswith(".pdf"):
        new_name += ".pdf"

    track(user, "rename pdf")
    progress = await context.bot.send_message(chat_id=msg.chat_id, text="⟳ Renaming document…")
    try:
        tg_file = await context.bot.get_file(doc.file_id)
        data = bytes(await tg_file.download_as_bytearray())
        bio = io.BytesIO(data)
        bio.name = new_name

        thumb = None
        tpath = thumb_path(user.id)
        if tpath.exists():
            thumb = InputFile(tpath.open("rb"), filename="thumb.jpg")

        await context.bot.send_document(
            chat_id=msg.chat_id,
            document=InputFile(bio, filename=new_name),
            filename=new_name,
            thumbnail=thumb,
        )
        try: await context.bot.delete_message(chat_id=msg.chat_id, message_id=progress.message_id)
        except Exception: pass
        try: await msg.delete()
        except Exception: pass
        try: await target_msg.delete()
        except Exception: pass
    except Exception as exc:
        logger.exception("Rename failed")
        try:
            await context.bot.edit_message_text(
                chat_id=msg.chat_id, message_id=progress.message_id,
                text=f"⚠ Rename failed: {html.escape(str(exc))}",
                parse_mode=ParseMode.HTML,
            )
        except Exception:
            pass


# ---------------------------------------------------------------------------
# CSV → HTML → PDF
# ---------------------------------------------------------------------------

def decode_csv(data: bytes) -> str:
    for enc in ("utf-8-sig", "utf-8", "cp1252", "latin-1"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")


def normalize_key(key: str) -> str:
    compact = " ".join((key or "").strip().split())
    lower = compact.lower()
    for canonical, aliases in COLUMN_ALIASES.items():
        if compact in aliases or lower in [a.lower() for a in aliases]:
            return canonical
    return lower.replace(" ", "_")


def parse_csv(data: bytes) -> List[Dict[str, str]]:
    text = decode_csv(data)
    sample = text[:4096]
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=",;\t|")
    except csv.Error:
        dialect = csv.excel
    reader = csv.DictReader(io.StringIO(text), dialect=dialect)
    rows: List[Dict[str, str]] = []
    for raw in reader:
        row: Dict[str, str] = {}
        for key, value in (raw or {}).items():
            row[normalize_key(key or "")] = "" if value is None else str(value).strip()
        if any(row.values()):
            rows.append(row)
    if not rows:
        raise ValueError("No data rows found in the CSV. Please verify the header row.")
    return rows


def render_inline_math(text: str) -> str:
    safe = html.escape(str(text or ""))
    safe = re.sub(r"\$([^$]+)\$", r"\1", safe)
    for pattern, repl in LATEX_REPLACEMENTS:
        safe = re.sub(pattern, repl, safe)
    safe = re.sub(r"([A-Za-z0-9\)\]])\^\{?([A-Za-z0-9+\-]+)\}?", r"\1<sup>\2</sup>", safe)
    safe = re.sub(r"([A-Za-z0-9\)\]])_\{?([A-Za-z0-9+\-]+)\}?", r"\1<sub>\2</sub>", safe)
    safe = safe.replace("\n", "<br>")
    return safe


def question_html(row: Dict[str, str], index: int, settings: Dict[str, Any]) -> str:
    question = row.get("question") or row.get("প্রশ্ন") or next(iter(row.values()), "")
    options = [row.get("option_a", ""), row.get("option_b", ""), row.get("option_c", ""), row.get("option_d", "")]
    labels = ["A", "B", "C", "D"]

    opt_rows = ""
    if any(options):
        rows_html = ""
        present = [(labels[i], opt) for i, opt in enumerate(options) if opt]
        for i in range(0, len(present), 2):
            cells = ""
            for j in range(2):
                if i + j < len(present):
                    lab, opt = present[i + j]
                    cells += (
                        f"<td class='option'><span class='opt-label'>{lab}.</span>"
                        f"<span class='opt-text'>{render_inline_math(opt)}</span></td>"
                    )
                else:
                    cells += "<td class='option'></td>"
            rows_html += f"<tr>{cells}</tr>"
        opt_rows = f"<table class='options'>{rows_html}</table>"

    answer = row.get("answer", "")
    explanation = row.get("explanation", "")
    extras = ""
    if settings.get("answer_enabled") and answer:
        ans_str = str(answer).strip()
        num_to_letter = {"1": "A", "2": "B", "3": "C", "4": "D"}
        if ans_str in num_to_letter:
            ans_display = num_to_letter[ans_str]
        elif ans_str.upper() in {"A", "B", "C", "D"}:
            ans_display = ans_str.upper()
        else:
            ans_display = ans_str
        extras += f"<div class='answer'><b>Answer:</b> {render_inline_math(ans_display)}</div>"
    if settings.get("explanation_enabled") and explanation:
        extras += f"<div class='explanation'><b>Explanation:</b> {render_inline_math(explanation)}</div>"

    source = (row.get("source") or "").strip()
    source_html = (
        f"<div class='q-source'>{html.escape(source)}</div>" if source else ""
    )

    # Pill-shaped number badge: scales naturally for 1, 2, or 3 digits.
    return f"""
    <article class='question'>
      <table class='q-head'><tr>
        <td class='q-no'><span class='q-circle'>{index}</span></td>
        <td class='q-text'>{source_html}{render_inline_math(question)}</td>
      </tr></table>
      {opt_rows}
      {extras}
    </article>
    """


def build_html(rows: List[Dict[str, str]], settings: Dict[str, Any], user_id: int) -> str:
    theme = THEMES.get(settings.get("theme"), THEMES["green"])
    columns = 2 if int(settings.get("columns", 2)) == 2 else 1
    size = "Letter" if settings.get("page_size") == "Letter" else "A4"
    opacity = max(0, min(100, int(settings.get("watermark_opacity", 8)))) / 100.0
    bn_font = settings.get("bn_font", "Noto Sans Bengali")
    en_font = settings.get("en_font", "Inter")
    math_font = settings.get("math_font", "STIX Two Math")
    bn_font_q = bn_font.replace(" ", "+")
    en_font_q = en_font.replace(" ", "+")
    math_font_q = math_font.replace(" ", "+")
    u_wm = wm_path(user_id)
    u_logo = logo_path(user_id)
    use_image_wm = bool(settings.get("watermark_enabled")) and bool(settings.get("watermark_image_enabled")) and u_wm.exists()
    use_text_wm = bool(settings.get("watermark_enabled")) and not use_image_wm
    watermark_text = html.escape(settings.get("watermark_text", "")) if use_text_wm else ""
    questions = "\n".join(question_html(row, i + 1, settings) for i, row in enumerate(rows))

    footer_text = html.escape(settings.get("footer_text", ""))
    footer_link = html.escape(settings.get("footer_link", ""))

    wm_html = ""
    if use_image_wm:
        wm_html = (
            f"<div class='watermark-img'>"
            f"<img src='{u_wm.name}' alt='' style='opacity:{opacity};'/>"
            f"</div>"
        )
    elif use_text_wm and watermark_text:
        wm_html = f"<div class='watermark' style='opacity:{opacity};'>{watermark_text}</div>"

    # Header logo: image if uploaded, otherwise the "PDF" badge.
    if settings.get("logo_enabled"):
        if u_logo.exists():
            logo_html = (
                "<td class='logo'>"
                f"<div class='logo-circle'><img src='{u_logo.name}' alt='logo'/></div>"
                "</td>"
            )
        else:
            logo_html = "<td class='logo'><div class='logo-circle logo-text'>PDF</div></td>"
    else:
        logo_html = ""

    return f"""<!doctype html>
<html lang='en'>
<head>
<meta charset='utf-8'>
<title>{html.escape(settings.get('title', 'PDF'))}</title>
<style>
  @font-face {{ font-family: 'Noto Sans Bengali'; src: url('fonts/NotoSansBengali-Regular.ttf') format('truetype'); font-weight: 400; font-style: normal; }}
  @import url('https://fonts.googleapis.com/css2?family={en_font_q}:wght@400;600;700;800&family={bn_font_q}:wght@400;600;700&family={math_font_q}:wght@400;700&display=swap');
  @page {{
      size: {size};
      margin: 14mm 12mm 18mm;
      @bottom-center {{ content: element(footer); }}
      @bottom-right {{
          content: "Page " counter(page) " of " counter(pages);
          font-family: '{en_font}', 'DejaVu Sans', sans-serif;
          font-size: 8.5pt; color: #6b7280;
          padding-bottom: 4mm;
      }}
  }}
  * {{ box-sizing: border-box; }}
  body {{ font-family: '{bn_font}', '{en_font}', 'Noto Sans Bengali', 'DejaVu Sans', sans-serif; color: #111827; line-height: 1.55; font-size: 10.5pt; margin: 0; }}
  .math, sup, sub, .frac {{ font-family: '{math_font}', '{en_font}', 'DejaVu Sans', serif; }}
  table {{ border-collapse: collapse; width: 100%; }}

  .header {{ width: 100%; border: 1.5px solid {theme['primary']}; border-radius: 8px; background: {theme['light']}; margin-bottom: 12px; }}
  .header td {{ padding: 10px 12px; vertical-align: middle; }}
  .header .logo {{ width: 64px; padding-right: 0; text-align: center; }}
  .header .logo-circle {{
      width: 52px; height: 52px; border-radius: 50%;
      background: {theme['primary']}; color: #fff;
      margin: 0 auto;
      display: flex; align-items: center; justify-content: center;
      overflow: hidden;
      font-weight: 800; font-size: 13pt;
      font-family: 'DejaVu Sans', sans-serif;
  }}
  .header .logo-circle img {{ width: 100%; height: 100%; object-fit: cover; display: block; }}
  .header .logo-text {{ line-height: 52px; }}
  .header h1 {{ margin: 0; color: {theme['primary']}; font-size: 17pt; font-weight: 800; }}
  .header .subtitle {{ margin-top: 2px; color: #374151; font-size: 10.5pt; }}
  .header .meta {{ text-align: right; font-weight: 700; white-space: nowrap; width: 1%; font-size: 10pt; line-height: 1.5; }}

  .paper {{ column-count: {columns}; column-gap: 10mm; column-rule: 0.5px solid {theme['border']}; column-fill: auto; }}
  .question {{ break-inside: avoid; page-break-inside: avoid; border-bottom: 1px solid #e5e7eb; padding-bottom: 7px; margin-bottom: 9px; }}

  table.q-head {{ table-layout: fixed; margin-bottom: 4px; }}
  td.q-no {{ width: 40px; vertical-align: top; padding: 0 8px 0 0; }}
  .q-circle {{
      display: inline-block;
      min-width: 26px; height: 22px;
      line-height: 22px; padding: 0 7px;
      border-radius: 11px;
      background: {theme['primary']}; color: #fff;
      text-align: center; font-weight: 700; font-size: 9.5pt;
      font-family: 'DejaVu Sans', sans-serif;
      box-sizing: border-box;
  }}
  td.q-text {{ padding-left: 12px; font-weight: 600; vertical-align: top; word-wrap: break-word; }}
  .q-source {{ font-size: 8.5pt; color: {theme['primary']}; font-style: italic; font-weight: 600; margin-bottom: 2px; letter-spacing: 0.2px; }}

  table.options {{ table-layout: fixed; margin: 4px 0 0 48px; width: calc(100% - 48px); }}
  table.options td.option {{ width: 50%; padding: 2px 6px 2px 0; vertical-align: top; word-wrap: break-word; }}
  .opt-label {{ color: {theme['primary']}; font-weight: 800; margin-right: 4px; }}

  .answer, .explanation {{ margin: 5px 0 0 48px; padding: 5px 8px; border-left: 3px solid {theme['accent']}; background: #f8fafc; font-size: 9.5pt; break-inside: avoid; }}

  .frac {{ display: inline-block; vertical-align: middle; text-align: center; line-height: 1; font-size: 0.9em; }}
  .frac span {{ display: block; }}
  .frac span:first-child {{ border-bottom: 1px solid currentColor; padding: 0 2px 1px; }}
  .frac span:last-child {{ padding-top: 1px; }}
  sup, sub {{ font-size: 70%; line-height: 0; }}

  .watermark {{ position: fixed; top: 42%; left: 0; right: 0; text-align: center; font-size: 64pt; color: #0f172a; font-weight: 900; z-index: -1; letter-spacing: 4px; }}
  .watermark-img {{ position: fixed; top: 0; left: 0; right: 0; bottom: 0; z-index: -1; }}
  .watermark-img img {{ position: absolute; top: 0; left: 0; right: 0; bottom: 0; margin: auto; width: 110mm; height: auto; max-width: 70%; max-height: 60%; }}
  .footer {{ position: running(footer); font-size: 8.5pt; color: #4b5563; text-align: center; border-top: 0.5px solid #d1d5db; padding-top: 4px; }}
  .footer a {{ color: {theme['primary']}; text-decoration: none; }}
</style>
</head>
<body>
  {wm_html}
  <div class='footer'>{footer_text}{(' — <a href="' + footer_link + '">' + footer_link + '</a>') if footer_link else ''}</div>

  <table class='header'><tr>
    {logo_html}
    <td>
      <h1>{html.escape(settings.get('title', ''))}</h1>
      <div class='subtitle'>{html.escape(settings.get('subtitle', ''))}</div>
    </td>
    <td class='meta'>Set: {html.escape(str(settings.get('set_name', '')))}<br>Marks: {html.escape(str(settings.get('marks', '')))}<br>Time: {html.escape(str(settings.get('time', '')))}</td>
  </tr></table>

  <main class='paper'>{questions}</main>
</body>
</html>"""


def generate_pdf_bytes(
    csv_data: Optional[bytes],
    settings: Dict[str, Any],
    user_id: int,
    rows_override: Optional[List[Dict[str, str]]] = None,
) -> bytes:
    if rows_override is not None:
        rows = rows_override
    else:
        rows = parse_csv(csv_data or b"")
    html_string = build_html(rows, settings, user_id)
    base_url = str(DATA_DIR)
    html_string = html_string.replace(
        "url('fonts/NotoSansBengali-Regular.ttf')",
        f"url('file://{BASE_DIR}/fonts/NotoSansBengali-Regular.ttf')",
    )
    body_pdf = HTML(string=html_string, base_url=base_url).write_pdf()
    return _merge_with_front_back(user_id, body_pdf)


async def generate_for_user(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    global GENERATION_COUNT
    user = update.effective_user
    msg = update.effective_message
    if not user or not msg:
        return
    if not is_generator(user.id):
        return
    if not await enforce_subscription(update, context):
        return

    csv_data = USER_CSV.get(user.id)
    quiz_rows = _quizzes_to_rows(user.id)
    if not csv_data and not quiz_rows:
        await send_or_update_panel(
            update, context,
            note="Upload a CSV or forward quiz polls to begin.",
        )
        return

    settings = get_settings(user.id).copy()
    chat_id = msg.chat.id
    track(user, "generating PDF")

    lock = USER_LOCKS[user.id]
    if lock.locked():
        await context.bot.send_message(chat_id=chat_id, text="A previous job is still running. Please wait…")
        return

    async with lock:
        progress = await context.bot.send_message(chat_id=chat_id, text="⟳ Processing… preparing your document.")
        stop_flag = {"v": False}

        async def animate():
            dots = ["⟳ Processing", "⟳ Processing.", "⟳ Processing..", "⟳ Processing..."]
            i = 0
            while not stop_flag["v"]:
                try:
                    await context.bot.edit_message_text(
                        chat_id=chat_id, message_id=progress.message_id,
                        text=f"{dots[i % 4]}\n<i>Rendering PDF, please wait…</i>",
                        parse_mode=ParseMode.HTML,
                    )
                except Exception:
                    pass
                await asyncio.sleep(1.2)
                i += 1

        anim_task = asyncio.create_task(animate())

        try:
            await msg.chat.send_action(ChatAction.UPLOAD_DOCUMENT)
            pdf_bytes = await asyncio.to_thread(
                generate_pdf_bytes, csv_data, settings, user.id,
                quiz_rows if quiz_rows and not csv_data else None,
            )
            stop_flag["v"] = True
            anim_task.cancel()
            try: await context.bot.delete_message(chat_id=chat_id, message_id=progress.message_id)
            except Exception: pass

            if csv_data:
                base = USER_CSV_NAME.get(user.id, "document.csv")
                filename = re.sub(r"\.csv$", "", base, flags=re.IGNORECASE) + ".pdf"
            else:
                filename = "quiz_collection.pdf"

            bio = io.BytesIO(pdf_bytes)
            bio.name = filename
            thumb = None
            tpath = thumb_path(user.id)
            if tpath.exists():
                thumb = InputFile(tpath.open("rb"), filename="thumb.jpg")

            await context.bot.send_document(
                chat_id=chat_id,
                document=InputFile(bio, filename=filename),
                filename=filename,
                thumbnail=thumb,
            )
            GENERATION_COUNT += 1
            # Quiz pool is preserved across generations; only /quizclear or
            # uploading a new CSV resets it. The user can keep generating
            # PDFs from the same collected quizzes as many times as needed.
        except Exception as exc:
            stop_flag["v"] = True
            anim_task.cancel()
            logger.exception("PDF generation failed")
            try:
                await context.bot.edit_message_text(
                    chat_id=chat_id, message_id=progress.message_id,
                    text=f"⚠ Generation failed: {html.escape(str(exc))}",
                    parse_mode=ParseMode.HTML,
                )
            except Exception:
                pass


# ---------------------------------------------------------------------------
# Force-subscribe gate
# ---------------------------------------------------------------------------

async def _is_member(context: ContextTypes.DEFAULT_TYPE, chat_ref: str, uid: int) -> bool:
    """Return True if uid is a member of chat_ref. Tolerates errors."""
    if not chat_ref:
        return True
    ref: Any = chat_ref
    if isinstance(chat_ref, str) and chat_ref.lstrip("-").isdigit():
        try: ref = int(chat_ref)
        except Exception: ref = chat_ref
    try:
        member = await context.bot.get_chat_member(chat_id=ref, user_id=uid)
        status = getattr(member, "status", "")
        return status in {"creator", "administrator", "member", "owner"}
    except Exception as exc:
        logger.info("get_chat_member failed for %s: %s", chat_ref, exc)
        # If the bot is not in the channel, we cannot verify — fail open
        # so we never permanently lock users out due to misconfiguration.
        return True


async def missing_subscriptions(context: ContextTypes.DEFAULT_TYPE, uid: int) -> List[Dict[str, str]]:
    if not FORCE_CHANNELS or is_owner(uid):
        return []
    pending: List[Dict[str, str]] = []
    for entry in FORCE_CHANNELS:
        if not await _is_member(context, entry.get("chat", ""), uid):
            pending.append(entry)
    return pending


def _join_keyboard(pending: List[Dict[str, str]]) -> InlineKeyboardMarkup:
    rows: List[List[InlineKeyboardButton]] = []
    for entry in pending:
        link = entry.get("link") or ""
        label = entry.get("button") or f"Join {entry.get('title', 'Channel')}"
        if link:
            rows.append([InlineKeyboardButton(label, url=link)])
    rows.append([InlineKeyboardButton("✓ I have joined", callback_data="fsub:check")])
    return InlineKeyboardMarkup(rows)


async def enforce_subscription(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """Returns True if user is allowed to proceed. Otherwise sends gate."""
    user = update.effective_user
    chat = update.effective_chat
    if not user or not chat or is_owner(user.id):
        return True
    pending = await missing_subscriptions(context, user.id)
    if not pending:
        return True
    text = FORCE_CAPTION
    markup = _join_keyboard(pending)
    try:
        if update.callback_query:
            await update.callback_query.answer("Membership required.", show_alert=False)
            await context.bot.send_message(
                chat_id=chat.id, text=text, reply_markup=markup,
                parse_mode=ParseMode.HTML, disable_web_page_preview=True,
            )
        else:
            await context.bot.send_message(
                chat_id=chat.id, text=text, reply_markup=markup,
                parse_mode=ParseMode.HTML, disable_web_page_preview=True,
            )
    except Exception:
        logger.exception("Failed to send subscription gate")
    return False


# ---------------------------------------------------------------------------
# Force-subscribe channel management (owner)
# ---------------------------------------------------------------------------

CHANNELS_WAITING: Dict[int, Dict[str, str]] = {}  # owner add-flow scratch


def _channels_overview() -> str:
    if not FORCE_CHANNELS:
        return ("<b>Required Channels</b>\n  • <i>None configured.</i>\n\n"
                "Use <code>/addchannel</code> to add one.")
    lines = ["<b>Required Channels</b>"]
    for i, c in enumerate(FORCE_CHANNELS, 1):
        lines.append(
            f"  <b>{i}.</b> <code>{html.escape(c.get('title', '—'))}</code>\n"
            f"     · Chat: <code>{html.escape(c.get('chat', ''))}</code>\n"
            f"     · Link: {html.escape(c.get('link', '—'))}\n"
            f"     · Button: <code>{html.escape(c.get('button', '—'))}</code>"
        )
    lines.append("")
    lines.append("<b>Gate caption</b>")
    lines.append(f"<i>{html.escape(FORCE_CAPTION)[:400]}</i>")
    return "\n".join(lines)


async def cmd_channels(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.effective_message
    if not msg:
        return
    await msg.reply_text(
        _channels_overview()
        + "\n\nCommands:\n"
        "<code>/addchannel &lt;@channel_or_id&gt; | &lt;Title&gt; | &lt;https://t.me/...&gt; | &lt;Button label&gt;</code>\n"
        "<code>/removechannel &lt;index&gt;</code>\n"
        "<code>/setjoinmsg &lt;text&gt;</code> — update the gate caption (HTML allowed)",
        parse_mode=ParseMode.HTML, disable_web_page_preview=True,
    )


async def cmd_addchannel(update: Update, context: ContextTypes.DEFAULT_TYPE, args: str) -> None:
    msg = update.effective_message
    if not msg:
        return
    parts = [p.strip() for p in args.split("|")]
    if len(parts) < 3 or not parts[0]:
        await msg.reply_text(
            "Usage:\n<code>/addchannel @channel | Title | https://t.me/... | Button label</code>",
            parse_mode=ParseMode.HTML,
        )
        return
    chat_ref = parts[0]
    title = parts[1] or "Channel"
    link = parts[2] or ""
    button = parts[3] if len(parts) >= 4 and parts[3] else f"Join {title}"
    FORCE_CHANNELS.append({"chat": chat_ref, "title": title, "link": link, "button": button})
    _save_state()
    await msg.reply_text(
        f"✓ Added channel <code>{html.escape(chat_ref)}</code>.\n\n" + _channels_overview(),
        parse_mode=ParseMode.HTML, disable_web_page_preview=True,
    )


async def cmd_removechannel(update: Update, context: ContextTypes.DEFAULT_TYPE, args: str) -> None:
    msg = update.effective_message
    if not msg:
        return
    try:
        idx = int(re.sub(r"\D", "", args)) - 1
    except Exception:
        idx = -1
    if idx < 0 or idx >= len(FORCE_CHANNELS):
        await msg.reply_text("Provide a valid channel index from <code>/channels</code>.",
                             parse_mode=ParseMode.HTML)
        return
    removed = FORCE_CHANNELS.pop(idx)
    _save_state()
    await msg.reply_text(
        f"✓ Removed <code>{html.escape(removed.get('title', ''))}</code>.",
        parse_mode=ParseMode.HTML,
    )


async def cmd_setjoinmsg(update: Update, context: ContextTypes.DEFAULT_TYPE, args: str) -> None:
    global FORCE_CAPTION
    msg = update.effective_message
    if not msg:
        return
    text = args.strip()
    if not text:
        await msg.reply_text("Provide caption text after the command.")
        return
    FORCE_CAPTION = text
    _save_state()
    await msg.reply_text("✓ Gate caption updated.", parse_mode=ParseMode.HTML)


# ---------------------------------------------------------------------------
# Quiz collection (forwarded Telegram polls → PDF)
# ---------------------------------------------------------------------------

def _clean_quiz_question(text: str, source_title: Optional[str]) -> Tuple[str, Optional[str]]:
    """Split a possibly multi-line poll question into (question, source_label).

    Heuristic: if the question's first line is short and the next lines
    contain real text, treat the first line as the source/channel label.
    """
    raw = (text or "").strip()
    if not raw:
        return "", source_title
    lines = [ln.strip() for ln in raw.splitlines() if ln.strip()]
    if len(lines) >= 2 and len(lines[0]) <= 60:
        # Looks like "Channel name\nActual question..."
        return "\n".join(lines[1:]).strip(), lines[0]
    return raw, source_title


async def _refresh_quiz_status(context: ContextTypes.DEFAULT_TYPE, uid: int, chat_id: int) -> None:
    bucket = USER_QUIZ.get(uid) or []
    n = len(bucket)
    if n == 0:
        text = (
            "<b>Quiz Collector</b>\n\n"
            "Forward Telegram quiz polls to me — each one is captured automatically.\n"
            "When you are ready, run <code>/genquiz</code> to build the PDF."
        )
    else:
        preview_lines = []
        for i, q in enumerate(bucket[-5:], start=max(1, n - 4)):
            qt = (q.get("question") or "").splitlines()[0][:64]
            preview_lines.append(f"  <b>{i}.</b> {html.escape(qt)}")
        preview = "\n".join(preview_lines)
        text = (
            f"<b>Quiz Collector</b> — <code>{n}</code> question(s) captured.\n\n"
            f"{preview}\n\n"
            "Forward more polls to add, or run <code>/genquiz</code> to generate the PDF.\n"
            "Use <code>/quizclear</code> to start over."
        )
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("Generate PDF", callback_data="quiz:gen"),
         InlineKeyboardButton("Clear", callback_data="quiz:clear")],
    ])

    existing = QUIZ_STATUS_MSG.get(uid)
    if existing and existing[0] == chat_id:
        try:
            await context.bot.edit_message_text(
                chat_id=existing[0], message_id=existing[1],
                text=text, reply_markup=kb,
                parse_mode=ParseMode.HTML, disable_web_page_preview=True,
            )
            return
        except BadRequest as exc:
            if "not modified" in str(exc).lower():
                return
    sent = await context.bot.send_message(
        chat_id=chat_id, text=text, reply_markup=kb,
        parse_mode=ParseMode.HTML, disable_web_page_preview=True,
    )
    QUIZ_STATUS_MSG[uid] = (sent.chat_id, sent.message_id)


async def handle_poll_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.effective_message
    user = update.effective_user
    if not msg or not user or not msg.poll:
        return
    if not is_generator(user.id):
        return
    if not await enforce_subscription(update, context):
        return

    poll = msg.poll
    # Source title (channel name) from forward metadata
    source_title: Optional[str] = None
    try:
        fwd_chat = getattr(msg, "forward_from_chat", None)
        if fwd_chat:
            source_title = getattr(fwd_chat, "title", None) or getattr(fwd_chat, "username", None)
        if not source_title:
            origin = getattr(msg, "forward_origin", None)
            if origin:
                src = getattr(origin, "chat", None) or getattr(origin, "sender_chat", None)
                if src:
                    source_title = getattr(src, "title", None) or getattr(src, "username", None)
                if not source_title:
                    source_title = getattr(origin, "sender_user_name", None)
    except Exception:
        source_title = None

    question_text, source = _clean_quiz_question(poll.question or "", source_title)
    options = [opt.text for opt in (poll.options or [])][:4]
    while len(options) < 4:
        options.append("")

    # Quiz polls expose correct_option_id and explanation
    correct_id = getattr(poll, "correct_option_id", None)
    if correct_id is None:
        # Try poll.correct_option_id from update.poll? not available here
        try:
            correct_id = poll.correct_option_id
        except Exception:
            correct_id = None
    answer_letter = ""
    if isinstance(correct_id, int) and 0 <= correct_id < 4:
        answer_letter = "ABCD"[correct_id]

    explanation = getattr(poll, "explanation", "") or ""

    record = {
        "question": question_text,
        "option_a": options[0],
        "option_b": options[1],
        "option_c": options[2],
        "option_d": options[3],
        "answer": answer_letter,
        "explanation": explanation,
        "source": source or "",
    }
    USER_QUIZ[user.id].append(record)
    _save_state()
    track(user, "quiz captured")

    try: await msg.delete()
    except Exception: pass

    await _refresh_quiz_status(context, user.id, msg.chat_id)


async def cmd_quizclear(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    msg = update.effective_message
    if not user or not msg:
        return
    USER_QUIZ.pop(user.id, None)
    _save_state()
    await _refresh_quiz_status(context, user.id, msg.chat_id)


async def cmd_quizstart(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    msg = update.effective_message
    if not user or not msg:
        return
    QUIZ_STATUS_MSG.pop(user.id, None)
    await _refresh_quiz_status(context, user.id, msg.chat_id)


def _quizzes_to_rows(uid: int) -> List[Dict[str, str]]:
    out: List[Dict[str, str]] = []
    for q in USER_QUIZ.get(uid, []):
        text = q.get("question") or ""
        src = (q.get("source") or "").strip()
        if src:
            text = f"<i>[{html.escape(src)}]</i><br>{html.escape(text)}"
        else:
            text = html.escape(text)
        # build_html will run render_inline_math which html-escapes again,
        # so feed plain text through the normal pipeline instead.
        row = {
            "question": q.get("question") or "",
            "option_a": q.get("option_a") or "",
            "option_b": q.get("option_b") or "",
            "option_c": q.get("option_c") or "",
            "option_d": q.get("option_d") or "",
            "answer": q.get("answer") or "",
            "explanation": q.get("explanation") or "",
            "source": src,
        }
        out.append(row)
    return out


# ---------------------------------------------------------------------------
# Front / back page upload
# ---------------------------------------------------------------------------

def _image_to_pdf_bytes(img_path: Path) -> bytes:
    """Render an image as a single full-page PDF using WeasyPrint."""
    src = f"file://{img_path.resolve()}"
    html_doc = f"""<!doctype html><html><head><style>
        @page {{ size: A4; margin: 0; }}
        html, body {{ margin: 0; padding: 0; height: 100%; }}
        .wrap {{ width: 100%; height: 100vh; display: flex;
                 align-items: center; justify-content: center; background: #fff; }}
        img {{ max-width: 100%; max-height: 100%; object-fit: contain; }}
    </style></head><body><div class='wrap'>
        <img src='{src}' alt=''/></div></body></html>"""
    return HTML(string=html_doc, base_url=str(img_path.parent)).write_pdf()


async def _save_front_back(context, doc, target: Path, kind: str) -> str:
    """Save uploaded PDF or image as a single-page (or multi-page) PDF."""
    file_name = (doc.file_name or "").lower()
    mime = (doc.mime_type or "").lower()
    tg_file = await context.bot.get_file(doc.file_id)
    raw = bytes(await tg_file.download_as_bytearray())
    if file_name.endswith(".pdf") or "pdf" in mime:
        target.write_bytes(raw)
        return f"{kind.title()} page (PDF) saved."
    if mime.startswith("image/") or file_name.endswith((".png", ".jpg", ".jpeg", ".webp")):
        tmp = target.with_suffix(target.suffix + ".src")
        tmp.write_bytes(raw)
        try:
            pdf_bytes = await asyncio.to_thread(_image_to_pdf_bytes, tmp)
            target.write_bytes(pdf_bytes)
        finally:
            try: tmp.unlink()
            except Exception: pass
        return f"{kind.title()} page (image) saved."
    raise ValueError("Only PDF or image files are accepted for front/back pages.")


def _merge_with_front_back(uid: int, body_pdf: bytes) -> bytes:
    """Prepend front_path and append back_path if present."""
    fp = front_path(uid)
    bp = back_path(uid)
    if not fp.exists() and not bp.exists():
        return body_pdf
    try:
        from pypdf import PdfReader, PdfWriter
    except Exception:
        logger.exception("pypdf not available; skipping front/back merge")
        return body_pdf
    writer = PdfWriter()
    if fp.exists():
        for page in PdfReader(str(fp)).pages:
            writer.add_page(page)
    for page in PdfReader(io.BytesIO(body_pdf)).pages:
        writer.add_page(page)
    if bp.exists():
        for page in PdfReader(str(bp)).pages:
            writer.add_page(page)
    out = io.BytesIO()
    writer.write(out)
    return out.getvalue()



# Health server (Render web service)
# ---------------------------------------------------------------------------

async def health(_: web.Request) -> web.Response:
    return web.Response(text="OK", status=200)


async def start_health_server() -> web.AppRunner:
    app = web.Application()
    app.router.add_get("/", health)
    app.router.add_get("/health", health)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", PORT)
    await site.start()
    logger.info("Health server listening on port %s", PORT)
    return runner


async def post_init(app: Application) -> None:
    await start_health_server()
    logger.info("Bot started successfully")
    marker = DATA_DIR / "restart_target.json"
    if marker.exists():
        try:
            data = json.loads(marker.read_text(encoding="utf-8"))
            chat_id = data.get("chat_id")
            if chat_id:
                await app.bot.send_message(chat_id=chat_id, text="✓ Bot restarted successfully. State preserved.")
        except Exception:
            logger.exception("Restart notification failed")
        finally:
            try: marker.unlink()
            except Exception: pass


def main() -> None:
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN environment variable is missing")
    if not OWNER_ID:
        raise RuntimeError("OWNER_ID environment variable is missing or invalid")

    _load_state()

    # Concurrency: process several updates in parallel so multiple users
    # never block each other. PTB v21 default is sequential per-update,
    # which is fine because each handler awaits I/O — but PDF rendering is
    # CPU-bound and runs in a thread, releasing the event loop.
    # Generous HTTP timeouts so large PDF re-uploads (rename) don't time out.
    from telegram.request import HTTPXRequest
    req = HTTPXRequest(
        connection_pool_size=16,
        connect_timeout=30.0,
        read_timeout=180.0,
        write_timeout=180.0,
        pool_timeout=60.0,
    )
    app = (
        Application.builder()
        .token(BOT_TOKEN)
        .request(req)
        .get_updates_request(HTTPXRequest(connect_timeout=30.0, read_timeout=60.0))
        .post_init(post_init)
        .concurrent_updates(True)
        .build()
    )
    app.add_handler(MessageHandler(filters.TEXT, handle_text))
    app.add_handler(CallbackQueryHandler(handle_callback))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.add_handler(MessageHandler(filters.Document.ALL, handle_document))
    app.add_handler(MessageHandler(filters.POLL, handle_poll_message))

    app.run_polling(drop_pending_updates=True, allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    try:
        main()
    except Exception:
        logger.error("Fatal:\n%s", traceback.format_exc())
        raise
