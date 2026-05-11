"""CSV → PDF Telegram Bot.

Single-panel inline editor. Supports both `/` and `.` command prefixes.
Admins see the full feature set; standard users get a minimal command list.
"""

import asyncio
import csv
import html
import io
import logging
import os
import re
import textwrap
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from aiohttp import web
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
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
# Configuration
# ---------------------------------------------------------------------------

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger("csvpdfbot")

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
OWNER_ID_RAW = os.getenv("OWNER_ID", "").strip()
PORT = int(os.getenv("PORT", "10000"))

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
    "logo_enabled": True,
    "answer_enabled": True,
    "explanation_enabled": True,
    "columns": 2,
    "page_size": "A4",
    "theme": "green",
}

# Per-user state
USER_SETTINGS: Dict[int, Dict[str, Any]] = {}
USER_CSV: Dict[int, bytes] = {}
USER_CSV_NAME: Dict[int, str] = {}
WAITING_FOR: Dict[int, str] = {}
PANEL_MSG: Dict[int, Tuple[int, int]] = {}  # user_id -> (chat_id, message_id)

THEMES = {
    "green": {"primary": "#0f766e", "accent": "#16a34a", "light": "#ecfdf5", "border": "#99f6e4"},
    "blue": {"primary": "#1d4ed8", "accent": "#0284c7", "light": "#eff6ff", "border": "#bfdbfe"},
    "purple": {"primary": "#7e22ce", "accent": "#9333ea", "light": "#faf5ff", "border": "#e9d5ff"},
    "red": {"primary": "#b91c1c", "accent": "#dc2626", "light": "#fef2f2", "border": "#fecaca"},
    "black": {"primary": "#111827", "accent": "#374151", "light": "#f9fafb", "border": "#d1d5db"},
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
}

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def get_settings(user_id: int) -> Dict[str, Any]:
    if user_id not in USER_SETTINGS:
        USER_SETTINGS[user_id] = DEFAULT_SETTINGS.copy()
    return USER_SETTINGS[user_id]


def is_admin(user_id: Optional[int]) -> bool:
    return bool(user_id and OWNER_ID and user_id == OWNER_ID)


def main_keyboard(settings: Dict[str, Any]) -> InlineKeyboardMarkup:
    def flag(key: str) -> str:
        return "ON" if settings.get(key) else "OFF"

    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("Title", callback_data="set:title"),
            InlineKeyboardButton("Subtitle", callback_data="set:subtitle"),
        ],
        [
            InlineKeyboardButton("Set", callback_data="set:set_name"),
            InlineKeyboardButton("Marks", callback_data="set:marks"),
            InlineKeyboardButton("Time", callback_data="set:time"),
        ],
        [
            InlineKeyboardButton("Footer Text", callback_data="set:footer_text"),
            InlineKeyboardButton("Footer Link", callback_data="set:footer_link"),
        ],
        [
            InlineKeyboardButton(f"Watermark · {flag('watermark_enabled')}", callback_data="toggle:watermark_enabled"),
            InlineKeyboardButton("Edit Text", callback_data="set:watermark_text"),
        ],
        [
            InlineKeyboardButton(f"Logo · {flag('logo_enabled')}", callback_data="toggle:logo_enabled"),
            InlineKeyboardButton(f"Answer · {flag('answer_enabled')}", callback_data="toggle:answer_enabled"),
            InlineKeyboardButton(f"Explain · {flag('explanation_enabled')}", callback_data="toggle:explanation_enabled"),
        ],
        [
            InlineKeyboardButton(f"Columns: {settings['columns']}", callback_data="cycle:columns"),
            InlineKeyboardButton(f"Page: {settings['page_size']}", callback_data="cycle:page_size"),
            InlineKeyboardButton(f"Theme: {settings['theme'].title()}", callback_data="cycle:theme"),
        ],
        [
            InlineKeyboardButton("Reset", callback_data="reset"),
            InlineKeyboardButton("Generate PDF", callback_data="generate"),
        ],
    ])


def cancel_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton("Cancel", callback_data="cancel")]])


def panel_text(user_id: int, settings: Dict[str, Any], note: Optional[str] = None) -> str:
    csv_status = "Loaded" if user_id in USER_CSV else "Not uploaded"
    csv_name = USER_CSV_NAME.get(user_id, "—")
    role = "Administrator" if is_admin(user_id) else "Standard User"
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
      • Watermark: <code>{html.escape(str(settings['watermark_text']))}</code>

    <b>Source</b>
      • CSV: <code>{html.escape(csv_name)}</code> ({csv_status})
    """).strip()
    if note:
        body += f"\n\n<b>›</b> <i>{html.escape(note)}</i>"
    return body


def admin_help() -> str:
    return textwrap.dedent("""
    <b>Administrator Commands</b>

    <code>/start</code> — Open the composer panel
    <code>/panel</code> — Re-open or refresh the panel
    <code>/generate</code> — Build the PDF from the current CSV
    <code>/reset</code> — Restore default settings
    <code>/status</code> — Show current configuration
    <code>/clear</code> — Remove the loaded CSV file
    <code>/help</code> — Show this message

    <i>All commands also work with the </i><code>.</code><i> prefix (e.g. </i><code>.start</code><i>).</i>
    """).strip()


def user_help() -> str:
    return textwrap.dedent("""
    <b>Available Commands</b>

    <code>/start</code> — Get started
    <code>/help</code> — Show this message

    <i>This service is restricted to authorised users.
    Please contact the administrator for access.</i>
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
    settings = get_settings(user_id)

    if waiting_field:
        label = FIELD_LABELS.get(waiting_field, waiting_field)
        text = (
            panel_text(user_id, settings, note)
            + f"\n\n<b>Awaiting input</b>\nReply with the new <b>{html.escape(label)}</b>."
        )
        markup = cancel_keyboard()
    else:
        text = panel_text(user_id, settings, note)
        markup = main_keyboard(settings)

    chat_id = update.effective_chat.id if update.effective_chat else user_id
    panel = PANEL_MSG.get(user_id)

    if panel and panel[0] == chat_id:
        try:
            await context.bot.edit_message_text(
                chat_id=panel[0],
                message_id=panel[1],
                text=text,
                reply_markup=markup,
                parse_mode=ParseMode.HTML,
                disable_web_page_preview=True,
            )
            return
        except BadRequest as exc:
            if "not modified" in str(exc).lower():
                return
            logger.info("Panel edit failed, sending new: %s", exc)

    sent = await context.bot.send_message(
        chat_id=chat_id,
        text=text,
        reply_markup=markup,
        parse_mode=ParseMode.HTML,
        disable_web_page_preview=True,
    )
    PANEL_MSG[user_id] = (sent.chat_id, sent.message_id)


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
    user = update.effective_user
    if not user:
        return True

    admin = is_admin(user.id)

    if cmd == "start":
        await cmd_start(update, context)
    elif cmd == "help":
        await msg.reply_text(
            admin_help() if admin else user_help(),
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=True,
        )
    elif cmd in {"panel", "menu"} and admin:
        PANEL_MSG.pop(user.id, None)
        await send_or_update_panel(update, context)
    elif cmd == "generate" and admin:
        await generate_for_user(update, context)
    elif cmd == "reset" and admin:
        USER_SETTINGS[user.id] = DEFAULT_SETTINGS.copy()
        await send_or_update_panel(update, context, note="Settings restored to defaults.")
    elif cmd == "status" and admin:
        await send_or_update_panel(update, context)
    elif cmd == "clear" and admin:
        USER_CSV.pop(user.id, None)
        USER_CSV_NAME.pop(user.id, None)
        await send_or_update_panel(update, context, note="CSV cleared.")
    else:
        await msg.reply_text(
            "Unknown command." if admin else "This command is not available for your account.",
        )
    return True


# ---------------------------------------------------------------------------
# Handlers
# ---------------------------------------------------------------------------


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    msg = update.effective_message
    if not user or not msg:
        return
    if not is_admin(user.id):
        await msg.reply_text(
            "Welcome.\n\nThis is a private CSV → PDF utility. "
            "Access is limited to authorised administrators.\n\n"
            "Type <code>/help</code> to view available commands.",
            parse_mode=ParseMode.HTML,
        )
        return
    PANEL_MSG.pop(user.id, None)
    WAITING_FOR.pop(user.id, None)
    await send_or_update_panel(update, context, note="Upload a CSV file to begin.")


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.effective_message
    user = update.effective_user
    if not msg or not user:
        return

    # Command routing first (covers / and .)
    if msg.text and msg.text[:1] in "/.":
        if await dispatch_command(update, context):
            return

    if not is_admin(user.id):
        return

    field = WAITING_FOR.pop(user.id, "")
    if not field:
        return

    settings = get_settings(user.id)
    value = (msg.text or "").strip()
    settings[field] = value

    # Delete the user's reply to keep the chat tidy around the single panel.
    try:
        await msg.delete()
    except Exception:
        pass

    await send_or_update_panel(
        update, context,
        note=f"{FIELD_LABELS.get(field, field)} updated.",
    )


async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query:
        return
    await query.answer()
    user = update.effective_user
    if not user or not is_admin(user.id):
        await query.answer("Restricted.", show_alert=True)
        return

    settings = get_settings(user.id)
    data = query.data or ""
    note: Optional[str] = None
    waiting_field: Optional[str] = None

    if data.startswith("set:"):
        field = data.split(":", 1)[1]
        WAITING_FOR[user.id] = field
        waiting_field = field

    elif data.startswith("toggle:"):
        field = data.split(":", 1)[1]
        settings[field] = not bool(settings[field])
        note = f"{field.replace('_', ' ').title()} toggled."

    elif data == "cycle:columns":
        settings["columns"] = 1 if int(settings.get("columns", 2)) == 2 else 2

    elif data == "cycle:page_size":
        settings["page_size"] = "Letter" if settings.get("page_size") == "A4" else "A4"

    elif data == "cycle:theme":
        keys = list(THEMES.keys())
        settings["theme"] = keys[(keys.index(settings.get("theme", "green")) + 1) % len(keys)]

    elif data == "reset":
        USER_SETTINGS[user.id] = DEFAULT_SETTINGS.copy()
        note = "Settings restored to defaults."

    elif data == "cancel":
        WAITING_FOR.pop(user.id, None)
        note = "Cancelled."

    elif data == "generate":
        await generate_for_user(update, context)
        return

    await send_or_update_panel(update, context, note=note, waiting_field=waiting_field)


async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.effective_message
    user = update.effective_user
    if not msg or not user:
        return
    if not is_admin(user.id):
        await msg.reply_text("Restricted.")
        return

    doc = msg.document
    if not doc:
        return

    file_name = (doc.file_name or "").lower()
    mime = (doc.mime_type or "").lower()
    if not (file_name.endswith(".csv") or "csv" in mime or mime in {"text/plain", "application/vnd.ms-excel"}):
        await msg.reply_text("Only .csv files are accepted.")
        return

    await msg.chat.send_action(ChatAction.TYPING)
    tg_file = await context.bot.get_file(doc.file_id)
    data = bytes(await tg_file.download_as_bytearray())
    USER_CSV[user.id] = data
    USER_CSV_NAME[user.id] = doc.file_name or "uploaded.csv"

    try:
        await msg.delete()
    except Exception:
        pass

    await send_or_update_panel(update, context, note="CSV received and ready.")


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
    marks = row.get("marks") or ("" if settings.get("marks") == "auto" else str(settings.get("marks", "")))
    options = [row.get("option_a", ""), row.get("option_b", ""), row.get("option_c", ""), row.get("option_d", "")]
    labels = ["A", "B", "C", "D"]

    opt_cells = "".join(
        f"<td class='option'><span class='opt-label'>{labels[i]}.</span>"
        f"<span class='opt-text'>{render_inline_math(opt)}</span></td>"
        for i, opt in enumerate(options) if opt
    )
    # Two options per row for tidy alignment.
    opt_rows = ""
    pairs = [opt for opt in options if opt]
    if pairs:
        rows_table = ""
        for i in range(0, len([o for o in options if o]), 2):
            cells = ""
            for j in range(2):
                idx = i + j
                if idx < len(options) and options[idx]:
                    cells += (
                        f"<td class='option'><span class='opt-label'>{labels[idx]}.</span>"
                        f"<span class='opt-text'>{render_inline_math(options[idx])}</span></td>"
                    )
                else:
                    cells += "<td class='option'></td>"
            rows_table += f"<tr>{cells}</tr>"
        opt_rows = f"<table class='options'>{rows_table}</table>"

    answer = row.get("answer", "")
    explanation = row.get("explanation", "")

    extras = ""
    if settings.get("answer_enabled") and answer:
        extras += f"<div class='answer'><b>Answer:</b> {render_inline_math(answer)}</div>"
    if settings.get("explanation_enabled") and explanation:
        extras += f"<div class='explanation'><b>Explanation:</b> {render_inline_math(explanation)}</div>"

    mark_html = f"<span class='marks'>{html.escape(marks)}</span>" if marks else ""
    return f"""
    <article class='question'>
      <table class='q-head'><tr>
        <td class='q-no'>{index}</td>
        <td class='q-text'>{render_inline_math(question)}</td>
        <td class='q-marks'>{mark_html}</td>
      </tr></table>
      {opt_rows}
      {extras}
    </article>
    """


def build_html(rows: List[Dict[str, str]], settings: Dict[str, Any]) -> str:
    theme = THEMES.get(settings.get("theme"), THEMES["green"])
    columns = 2 if int(settings.get("columns", 2)) == 2 else 1
    size = "Letter" if settings.get("page_size") == "Letter" else "A4"
    watermark = html.escape(settings.get("watermark_text", "")) if settings.get("watermark_enabled") else ""
    logo = "<td class='logo'>PDF</td>" if settings.get("logo_enabled") else ""
    questions = "\n".join(question_html(row, i + 1, settings) for i, row in enumerate(rows))

    footer_text = html.escape(settings.get("footer_text", ""))
    footer_link = html.escape(settings.get("footer_link", ""))

    return f"""<!doctype html>
<html lang='en'>
<head>
<meta charset='utf-8'>
<title>{html.escape(settings.get('title', 'PDF'))}</title>
<style>
  @font-face {{ font-family: 'Noto Sans Bengali'; src: url('fonts/NotoSansBengali-Regular.ttf') format('truetype'); font-weight: 400; font-style: normal; }}
  @page {{ size: {size}; margin: 14mm 12mm 16mm; @bottom-center {{ content: element(footer); }} }}
  * {{ box-sizing: border-box; }}
  body {{ font-family: 'Noto Sans Bengali', 'DejaVu Sans', 'Helvetica', sans-serif; color: #111827; line-height: 1.5; font-size: 10.5pt; margin: 0; }}
  table {{ border-collapse: collapse; width: 100%; }}

  .header {{ width: 100%; border: 1.5px solid {theme['primary']}; border-radius: 8px; background: {theme['light']}; margin-bottom: 12px; }}
  .header td {{ padding: 10px 12px; vertical-align: middle; }}
  .header .logo {{ width: 52px; }}
  .header .logo div {{ width: 44px; height: 44px; border-radius: 50%; background: {theme['primary']}; color: #fff; text-align: center; line-height: 44px; font-weight: 800; font-size: 13pt; }}
  .header h1 {{ margin: 0; color: {theme['primary']}; font-size: 18pt; font-weight: 800; }}
  .header .subtitle {{ margin-top: 2px; color: #374151; font-size: 10.5pt; }}
  .header .meta {{ text-align: right; font-weight: 700; white-space: nowrap; width: 1%; }}

  .paper {{ column-count: {columns}; column-gap: 10mm; column-rule: 0.5px solid {theme['border']}; }}
  .question {{ break-inside: avoid; page-break-inside: avoid; border-bottom: 1px solid #e5e7eb; padding-bottom: 7px; margin-bottom: 9px; }}

  table.q-head {{ table-layout: fixed; margin-bottom: 4px; }}
  td.q-no {{ width: 24px; vertical-align: top; }}
  td.q-no::before {{ content: attr(data-n); }}
  td.q-no {{ background: {theme['primary']}; color: #fff; border-radius: 999px; text-align: center; font-weight: 800; font-size: 9pt; height: 22px; line-height: 22px; width: 22px; }}
  td.q-text {{ padding-left: 8px; font-weight: 600; vertical-align: top; word-wrap: break-word; }}
  td.q-marks {{ width: 1%; text-align: right; vertical-align: top; padding-left: 6px; }}
  .marks {{ border: 1px solid {theme['accent']}; color: {theme['primary']}; padding: 1px 6px; border-radius: 999px; font-size: 8pt; white-space: nowrap; }}

  table.options {{ table-layout: fixed; margin: 4px 0 0 30px; width: calc(100% - 30px); }}
  table.options td.option {{ width: 50%; padding: 2px 6px 2px 0; vertical-align: top; word-wrap: break-word; }}
  .opt-label {{ color: {theme['primary']}; font-weight: 800; margin-right: 4px; }}

  .answer, .explanation {{ margin: 5px 0 0 30px; padding: 5px 8px; border-left: 3px solid {theme['accent']}; background: #f8fafc; font-size: 9.5pt; break-inside: avoid; }}

  .frac {{ display: inline-block; vertical-align: middle; text-align: center; line-height: 1; font-size: 0.9em; }}
  .frac span {{ display: block; }}
  .frac span:first-child {{ border-bottom: 1px solid currentColor; padding: 0 2px 1px; }}
  .frac span:last-child {{ padding-top: 1px; }}
  sup, sub {{ font-size: 70%; line-height: 0; }}

  .watermark {{ position: fixed; top: 42%; left: 0; right: 0; text-align: center; font-size: 64pt; color: rgba(15, 23, 42, .06); font-weight: 900; z-index: -1; letter-spacing: 4px; }}
  .footer {{ position: running(footer); font-size: 8.5pt; color: #4b5563; text-align: center; border-top: 0.5px solid #d1d5db; padding-top: 4px; }}
  .footer a {{ color: {theme['primary']}; text-decoration: none; }}
</style>
</head>
<body>
  {('<div class="watermark">' + watermark + '</div>') if watermark else ''}
  <div class='footer'>{footer_text}{(' — <a href="' + footer_link + '">' + footer_link + '</a>') if footer_link else ''}</div>

  <table class='header'><tr>
    {('<td class="logo"><div>PDF</div></td>') if settings.get('logo_enabled') else ''}
    <td>
      <h1>{html.escape(settings.get('title', ''))}</h1>
      <div class='subtitle'>{html.escape(settings.get('subtitle', ''))}</div>
    </td>
    <td class='meta'>Set: {html.escape(str(settings.get('set_name', '')))}<br>Time: {html.escape(str(settings.get('time', '')))}</td>
  </tr></table>

  <main class='paper'>{questions}</main>
</body>
</html>"""


def generate_pdf_bytes(csv_data: bytes, settings: Dict[str, Any]) -> bytes:
    rows = parse_csv(csv_data)
    html_string = build_html(rows, settings)
    base_url = str(Path(__file__).resolve().parent)
    return HTML(string=html_string, base_url=base_url).write_pdf()


async def generate_for_user(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    msg = update.effective_message
    if not user or not msg:
        return
    csv_data = USER_CSV.get(user.id)
    if not csv_data:
        await send_or_update_panel(update, context, note="No CSV uploaded yet.")
        return

    settings = get_settings(user.id).copy()
    await send_or_update_panel(update, context, note="Generating PDF…")
    await msg.chat.send_action(ChatAction.UPLOAD_DOCUMENT)

    try:
        pdf_bytes = await asyncio.to_thread(generate_pdf_bytes, csv_data, settings)
        bio = io.BytesIO(pdf_bytes)
        bio.name = "document.pdf"
        await context.bot.send_document(
            chat_id=msg.chat.id,
            document=bio,
            filename="document.pdf",
            caption="Document generated successfully.",
        )
        await send_or_update_panel(update, context, note="PDF delivered.")
    except Exception as exc:
        logger.exception("PDF generation failed")
        await send_or_update_panel(update, context, note=f"Generation failed: {exc}")


# ---------------------------------------------------------------------------
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


async def post_init(_: Application) -> None:
    await start_health_server()
    logger.info("Bot started successfully")


def main() -> None:
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN environment variable is missing")
    if not OWNER_ID:
        raise RuntimeError("OWNER_ID environment variable is missing or invalid")

    app = Application.builder().token(BOT_TOKEN).post_init(post_init).build()
    # Single text handler routes both / and . commands plus settings input.
    app.add_handler(MessageHandler(filters.TEXT, handle_text))
    app.add_handler(CallbackQueryHandler(handle_callback))
    app.add_handler(MessageHandler(filters.Document.ALL, handle_document))

    app.run_polling(drop_pending_updates=True, allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
