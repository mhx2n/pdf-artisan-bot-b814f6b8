import asyncio
import csv
import html
import io
import logging
import os
import re
import textwrap
from pathlib import Path
from typing import Any, Dict, List

from aiohttp import web
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ChatAction
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)
from weasyprint import HTML

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
    "title": "পরীক্ষার প্রশ্নপত্র",
    "subtitle": "CSV থেকে তৈরি PDF",
    "set_name": "A",
    "marks": "auto",
    "time": "15 min",
    "footer_text": "আমাদের টেলিগ্রাম চ্যানেল",
    "footer_link": "https://t.me/",
    "watermark_enabled": True,
    "watermark_text": "তরতরিবতর",
    "logo_enabled": True,
    "answer_enabled": True,
    "explanation_enabled": True,
    "columns": 2,
    "page_size": "A4",
    "theme": "green",
}

USER_SETTINGS: Dict[int, Dict[str, Any]] = {}
USER_CSV: Dict[int, bytes] = {}
WAITING_FOR: Dict[int, str] = {}

THEMES = {
    "green": {"primary": "#0f766e", "accent": "#16a34a", "light": "#ecfdf5", "border": "#99f6e4"},
    "blue": {"primary": "#1d4ed8", "accent": "#0284c7", "light": "#eff6ff", "border": "#bfdbfe"},
    "purple": {"primary": "#7e22ce", "accent": "#9333ea", "light": "#faf5ff", "border": "#e9d5ff"},
    "red": {"primary": "#b91c1c", "accent": "#dc2626", "light": "#fef2f2", "border": "#fecaca"},
    "black": {"primary": "#111827", "accent": "#374151", "light": "#f9fafb", "border": "#d1d5db"},
}

COLUMN_ALIASES = {
    "question": ["question", "ques", "q", "প্রশ্ন", "প্রশ্নপত্র", "Question", "QUESTION"],
    "option_a": ["option_a", "a", "A", "option a", "Option A", "ক", "option1", "Option1"],
    "option_b": ["option_b", "b", "B", "option b", "Option B", "খ", "option2", "Option2"],
    "option_c": ["option_c", "c", "C", "option c", "Option C", "গ", "option3", "Option3"],
    "option_d": ["option_d", "d", "D", "option d", "Option D", "ঘ", "option4", "Option4"],
    "answer": ["answer", "ans", "Answer", "ANSWER", "উত্তর", "সঠিক উত্তর"],
    "explanation": ["explanation", "explain", "Explanation", "ব্যাখ্যা", "সমাধান"],
    "marks": ["marks", "mark", "Marks", "মান", "নম্বর"],
}

LATEX_REPLACEMENTS = [
    (r"\\frac\{([^{}]+)\}\{([^{}]+)\}", r"<span class='frac'><span>\1</span><span>\2</span></span>"),
    (r"\\sqrt\{([^{}]+)\}", r"√(<span>\1</span>)"),
    (r"\\times", "×"),
    (r"\\div", "÷"),
    (r"\\pm", "±"),
    (r"\\leq", "≤"),
    (r"\\geq", "≥"),
    (r"\\neq", "≠"),
    (r"\\alpha", "α"),
    (r"\\beta", "β"),
    (r"\\gamma", "γ"),
    (r"\\theta", "θ"),
    (r"\\pi", "π"),
    (r"\\Delta", "Δ"),
]


def get_settings(user_id: int) -> Dict[str, Any]:
    if user_id not in USER_SETTINGS:
        USER_SETTINGS[user_id] = DEFAULT_SETTINGS.copy()
    return USER_SETTINGS[user_id]


def is_owner(update: Update) -> bool:
    user = update.effective_user
    return bool(user and OWNER_ID and user.id == OWNER_ID)


async def reject_non_owner(update: Update) -> bool:
    if is_owner(update):
        return False
    if update.effective_message:
        await update.effective_message.reply_text("⛔ এই বট শুধুমাত্র OWNER_ID দেওয়া ইউজার ব্যবহার করতে পারবে।")
    return True


def main_keyboard(settings: Dict[str, Any]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📝 Title", callback_data="set:title"), InlineKeyboardButton("📌 Subtitle", callback_data="set:subtitle")],
        [InlineKeyboardButton("🔤 Set", callback_data="set:set_name"), InlineKeyboardButton("💯 Total Mark", callback_data="set:marks"), InlineKeyboardButton("⏱ Time", callback_data="set:time")],
        [InlineKeyboardButton("🔗 Footer Text", callback_data="set:footer_text"), InlineKeyboardButton("🌐 Footer Link", callback_data="set:footer_link")],
        [InlineKeyboardButton(("✅" if settings["watermark_enabled"] else "❌") + " Watermark", callback_data="toggle:watermark_enabled"), InlineKeyboardButton("✏️ WM Text", callback_data="set:watermark_text")],
        [InlineKeyboardButton(("✅" if settings["logo_enabled"] else "❌") + " Logo", callback_data="toggle:logo_enabled"), InlineKeyboardButton(("✅" if settings["answer_enabled"] else "❌") + " Answer", callback_data="toggle:answer_enabled"), InlineKeyboardButton(("✅" if settings["explanation_enabled"] else "❌") + " Explain", callback_data="toggle:explanation_enabled")],
        [InlineKeyboardButton(f"📐 Columns: {settings['columns']}", callback_data="cycle:columns"), InlineKeyboardButton(f"📄 Page: {settings['page_size']}", callback_data="cycle:page_size")],
        [InlineKeyboardButton(f"🎨 Theme: {settings['theme']}", callback_data="cycle:theme")],
        [InlineKeyboardButton("👀 Preview Settings", callback_data="preview"), InlineKeyboardButton("♻️ Reset", callback_data="reset")],
        [InlineKeyboardButton("✅ Generate PDF", callback_data="generate")],
    ])


def settings_text(settings: Dict[str, Any]) -> str:
    return textwrap.dedent(f"""
    ⚙️ Current Settings
    • Title: {settings['title']}
    • Subtitle: {settings['subtitle']}
    • Set: {settings['set_name']}  |  Marks: {settings['marks']}  |  Time: {settings['time']}
    • Footer: {settings['footer_text']}
    • Link: {settings['footer_link']}
    • Watermark: {settings['watermark_text']} ({'on' if settings['watermark_enabled'] else 'off'})
    • Logo: {'on' if settings['logo_enabled'] else 'off'}  |  Answer: {'on' if settings['answer_enabled'] else 'off'}  |  Explanation: {'on' if settings['explanation_enabled'] else 'off'}
    • Columns: {settings['columns']}  |  Page: {settings['page_size']}  |  Theme: {settings['theme']}
    """).strip()


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if await reject_non_owner(update):
        return
    settings = get_settings(update.effective_user.id)
    await update.message.reply_text(
        "✅ CSV ফাইল পাঠান, তারপর নিচের বাটন দিয়ে PDF কাস্টমাইজ করুন।\n\n" + settings_text(settings),
        reply_markup=main_keyboard(settings),
    )


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if await reject_non_owner(update):
        return
    await update.message.reply_text(
        "CSV কলাম সাপোর্ট: question, option_a, option_b, option_c, option_d, answer, explanation, marks\n"
        "বাংলা কলাম নামও চলবে: প্রশ্ন, ক, খ, গ, ঘ, উত্তর, ব্যাখ্যা, নম্বর\n"
        "LaTeX basic support: \\frac{a}{b}, \\sqrt{x}, ^2, _2, \\alpha, \\beta, \\pi ইত্যাদি।"
    )


async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    if await reject_non_owner(update):
        return

    user_id = update.effective_user.id
    settings = get_settings(user_id)
    data = query.data or ""

    if data.startswith("set:"):
        field = data.split(":", 1)[1]
        WAITING_FOR[user_id] = field
        await query.message.reply_text(f"✍️ নতুন {field} পাঠান:")
        return

    if data.startswith("toggle:"):
        field = data.split(":", 1)[1]
        settings[field] = not bool(settings[field])

    elif data == "cycle:columns":
        settings["columns"] = 1 if int(settings.get("columns", 2)) == 2 else 2

    elif data == "cycle:page_size":
        settings["page_size"] = "Letter" if settings.get("page_size") == "A4" else "A4"

    elif data == "cycle:theme":
        keys = list(THEMES.keys())
        settings["theme"] = keys[(keys.index(settings.get("theme", "green")) + 1) % len(keys)]

    elif data == "reset":
        USER_SETTINGS[user_id] = DEFAULT_SETTINGS.copy()
        settings = USER_SETTINGS[user_id]

    elif data == "preview":
        await query.message.reply_text(settings_text(settings), reply_markup=main_keyboard(settings))
        return

    elif data == "generate":
        await generate_for_user(update, context)
        return

    await query.message.edit_text(settings_text(settings), reply_markup=main_keyboard(settings))


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if await reject_non_owner(update):
        return
    user_id = update.effective_user.id
    field = WAITING_FOR.pop(user_id, "")
    if not field:
        await update.message.reply_text("CSV ফাইল পাঠান অথবা /start চাপুন।")
        return
    settings = get_settings(user_id)
    value = (update.message.text or "").strip()
    if field == "columns":
        settings[field] = 1 if value != "2" else 2
    else:
        settings[field] = value
    await update.message.reply_text("✅ সেটিং আপডেট হয়েছে।\n\n" + settings_text(settings), reply_markup=main_keyboard(settings))


async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if await reject_non_owner(update):
        return

    doc = update.message.document
    if not doc:
        return

    file_name = (doc.file_name or "").lower()
    mime = (doc.mime_type or "").lower()
    if not (file_name.endswith(".csv") or "csv" in mime or mime in {"text/plain", "application/vnd.ms-excel"}):
        await update.message.reply_text("❌ শুধু .csv ফাইল পাঠান।")
        return

    await update.message.chat.send_action(ChatAction.TYPING)
    tg_file = await context.bot.get_file(doc.file_id)
    data = bytes(await tg_file.download_as_bytearray())
    USER_CSV[update.effective_user.id] = data
    settings = get_settings(update.effective_user.id)
    await update.message.reply_text(
        "✅ CSV পেয়েছি। এখন সেটিংস কাস্টমাইজ করে Generate PDF চাপুন।\n\n" + settings_text(settings),
        reply_markup=main_keyboard(settings),
    )


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
        raise ValueError("CSV ফাইলে কোনো ডাটা পাওয়া যায়নি। Header row আছে কিনা চেক করুন।")
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
    labels = ["ক", "খ", "গ", "ঘ"]

    opt_html = "".join(
        f"<div class='option'><span>{labels[i]}.</span><p>{render_inline_math(opt)}</p></div>"
        for i, opt in enumerate(options) if opt
    )
    answer = row.get("answer", "")
    explanation = row.get("explanation", "")

    answer_html = ""
    if settings.get("answer_enabled") and answer:
        answer_html += f"<div class='answer'><b>উত্তর:</b> {render_inline_math(answer)}</div>"
    if settings.get("explanation_enabled") and explanation:
        answer_html += f"<div class='explanation'><b>ব্যাখ্যা:</b> {render_inline_math(explanation)}</div>"

    mark_html = f"<span class='marks'>{html.escape(marks)}</span>" if marks else ""
    return f"""
    <article class='question'>
      <div class='q-head'><span class='q-no'>{index}</span><div class='q-text'>{render_inline_math(question)}</div>{mark_html}</div>
      <div class='options'>{opt_html}</div>
      {answer_html}
    </article>
    """


def build_html(rows: List[Dict[str, str]], settings: Dict[str, Any]) -> str:
    theme = THEMES.get(settings.get("theme"), THEMES["green"])
    columns = 2 if int(settings.get("columns", 2)) == 2 else 1
    size = "Letter" if settings.get("page_size") == "Letter" else "A4"
    watermark = html.escape(settings.get("watermark_text", "")) if settings.get("watermark_enabled") else ""
    logo = "<div class='logo'>CSV</div>" if settings.get("logo_enabled") else ""
    questions = "\n".join(question_html(row, i + 1, settings) for i, row in enumerate(rows))

    return f"""<!doctype html>
<html lang='bn'>
<head>
<meta charset='utf-8'>
<title>{html.escape(settings.get('title', 'PDF'))}</title>
<style>
  @font-face {{ font-family: 'Noto Sans Bengali'; src: url('fonts/NotoSansBengali-Regular.ttf') format('truetype'); font-weight: 400; font-style: normal; }}
  @page {{ size: {size}; margin: 14mm 12mm 16mm; @bottom-center {{ content: element(footer); }} }}
  * {{ box-sizing: border-box; }}
  body {{ font-family: 'Noto Sans Bengali', 'Noto Sans', 'DejaVu Sans', sans-serif; color: #111827; line-height: 1.45; font-size: 10.5pt; }}
  .header {{ border: 1.5px solid {theme['primary']}; border-radius: 10px; padding: 12px 14px; margin-bottom: 12px; background: {theme['light']}; display: flex; align-items: center; gap: 12px; }}
  .logo {{ width: 44px; height: 44px; border-radius: 50%; background: {theme['primary']}; color: white; display: flex; align-items: center; justify-content: center; font-weight: 800; font-size: 13pt; flex: none; }}
  h1 {{ margin: 0; color: {theme['primary']}; font-size: 19pt; font-weight: 800; }}
  .subtitle {{ margin-top: 2px; color: #374151; font-size: 10.5pt; }}
  .meta {{ margin-left: auto; text-align: right; color: #111827; font-weight: 700; white-space: nowrap; }}
  .paper {{ column-count: {columns}; column-gap: 12mm; column-rule: 0.5px solid {theme['border']}; }}
  .question {{ break-inside: avoid; page-break-inside: avoid; border-bottom: 1px solid #e5e7eb; padding: 0 0 8px; margin: 0 0 9px; }}
  .q-head {{ display: flex; gap: 7px; align-items: flex-start; }}
  .q-no {{ background: {theme['primary']}; color: white; border-radius: 999px; min-width: 21px; height: 21px; display: inline-flex; align-items: center; justify-content: center; font-weight: 800; font-size: 9pt; flex: none; }}
  .q-text {{ flex: 1; font-weight: 600; overflow-wrap: anywhere; }}
  .marks {{ border: 1px solid {theme['accent']}; color: {theme['primary']}; padding: 1px 5px; border-radius: 999px; font-size: 8pt; white-space: nowrap; }}
  .options {{ display: grid; grid-template-columns: 1fr; gap: 2px; margin: 6px 0 0 28px; }}
  .option {{ display: flex; gap: 5px; align-items: baseline; }}
  .option span {{ color: {theme['primary']}; font-weight: 800; }}
  .option p {{ margin: 0; overflow-wrap: anywhere; }}
  .answer, .explanation {{ margin: 5px 0 0 28px; padding: 5px 7px; border-left: 3px solid {theme['accent']}; background: #f8fafc; font-size: 9.3pt; break-inside: avoid; }}
  .frac {{ display: inline-flex; flex-direction: column; vertical-align: middle; text-align: center; line-height: 1; font-size: 0.9em; }}
  .frac span:first-child {{ border-bottom: 1px solid currentColor; padding: 0 2px 1px; }}
  .frac span:last-child {{ padding-top: 1px; }}
  sup, sub {{ font-size: 70%; line-height: 0; }}
  .watermark {{ position: fixed; top: 42%; left: 16%; right: 16%; text-align: center; font-size: 46pt; color: rgba(15, 23, 42, .055); font-weight: 900; white-space: nowrap; z-index: -1; }}
  .footer {{ position: running(footer); font-size: 8.5pt; color: #4b5563; text-align: center; border-top: 0.5px solid #d1d5db; padding-top: 4px; }}
  .footer a {{ color: {theme['primary']}; text-decoration: none; }}
</style>
</head>
<body>
  {'<div class="watermark">' + watermark + '</div>' if watermark else ''}
  <footer class='footer'>{html.escape(settings.get('footer_text', ''))} — <a href='{html.escape(settings.get('footer_link', ''))}'>{html.escape(settings.get('footer_link', ''))}</a></footer>
  <section class='header'>
    {logo}
    <div><h1>{html.escape(settings.get('title', ''))}</h1><div class='subtitle'>{html.escape(settings.get('subtitle', ''))}</div></div>
    <div class='meta'>Set: {html.escape(str(settings.get('set_name', '')))}<br>Time: {html.escape(str(settings.get('time', '')))}</div>
  </section>
  <main class='paper'>{questions}</main>
</body>
</html>"""


def generate_pdf_bytes(csv_data: bytes, settings: Dict[str, Any]) -> bytes:
    rows = parse_csv(csv_data)
    html_string = build_html(rows, settings)
    base_url = str(Path(__file__).resolve().parent)
    # Avoid CSS transform watermark code; WeasyPrint 62 can crash on some transform paths.
    return HTML(string=html_string, base_url=base_url).write_pdf()


async def generate_for_user(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    message = update.effective_message
    csv_data = USER_CSV.get(user_id)
    if not csv_data:
        await message.reply_text("❌ আগে .csv ফাইল পাঠান।")
        return

    settings = get_settings(user_id).copy()
    await message.reply_text("⏳ PDF তৈরি হচ্ছে...")
    await message.chat.send_action(ChatAction.UPLOAD_DOCUMENT)

    try:
        pdf_bytes = await asyncio.to_thread(generate_pdf_bytes, csv_data, settings)
        bio = io.BytesIO(pdf_bytes)
        bio.name = "generated_report.pdf"
        await message.reply_document(document=bio, filename="generated_report.pdf", caption="✅ PDF তৈরি হয়েছে।")
    except Exception as exc:
        logger.exception("PDF generation failed")
        await message.reply_text(f"❌ PDF তৈরি ব্যর্থ: {exc}")


async def health(request: web.Request) -> web.Response:
    return web.Response(text="OK", status=200)


async def start_health_server() -> web.AppRunner:
    app = web.Application()
    app.router.add_get("/", health)
    app.router.add_get("/health", health)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", PORT)
    await site.start()
    logger.info("Health server is live on port %s", PORT)
    return runner


async def post_init(application: Application) -> None:
    await start_health_server()
    logger.info("Bot started successfully")


def main() -> None:
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN environment variable is missing")
    if not OWNER_ID:
        raise RuntimeError("OWNER_ID environment variable is missing or invalid")

    app = Application.builder().token(BOT_TOKEN).post_init(post_init).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CallbackQueryHandler(handle_callback))
    app.add_handler(MessageHandler(filters.Document.ALL, handle_document))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    app.run_polling(drop_pending_updates=True, allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
