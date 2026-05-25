# =========================================================
# IMPORTS
# =========================================================

import os
import io
import asyncio
import datetime
import traceback
import logging

from threading import Thread

import requests

from flask import Flask, request

from google import genai
from google.genai import types

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    ReplyKeyboardMarkup,
    KeyboardButton,
    User,
)

from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

# =========================================================
# LOGGING
# =========================================================

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)

logger = logging.getLogger(__name__)

# =========================================================
# ENV VARIABLES
# =========================================================

TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")

BOT_USERNAME = os.getenv("TELEGRAM_BOT_USERNAME")

SUPABASE_URL = os.getenv("SUPABASE_URL")

SUPABASE_KEY = os.getenv("SUPABASE_SERVICE_KEY") or os.getenv("SUPABASE_KEY")

GOOGLE_API_KEY = os.getenv("GEMINI_API_KEY")

ADMIN_ID = os.getenv("ADMIN_ID")

RENDER_EXTERNAL_URL = os.getenv("RENDER_EXTERNAL_URL")

PRIMARY_CHANNEL = "@Axia_Tech"

# =========================================================
# VALIDATION
# =========================================================

if not TOKEN:
    raise ValueError("TELEGRAM_BOT_TOKEN missing")

if not GOOGLE_API_KEY:
    raise ValueError("GEMINI_API_KEY missing")

if not SUPABASE_URL:
    raise ValueError("SUPABASE_URL missing")

if not SUPABASE_KEY:
    raise ValueError("SUPABASE_SERVICE_KEY missing")

# =========================================================
# APPS
# =========================================================

app = Flask(__name__)

ai_client = genai.Client(
    api_key=GOOGLE_API_KEY
)

ptb_app = Application.builder().token(TOKEN).build()

# =========================================================
# ASYNC LOOP
# =========================================================

bot_loop = asyncio.new_event_loop()


def start_background_loop(loop):

    asyncio.set_event_loop(loop)

    loop.run_forever()


Thread(
    target=start_background_loop,
    args=(bot_loop,),
    daemon=True
).start()

# =========================================================
# DATABASE
# =========================================================

def db_request(
    method,
    table,
    params=None,
    json_data=None
):

    url = f"{SUPABASE_URL}/rest/v1/{table}"

    headers = {
        "apikey": SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}",
        "Content-Type": "application/json",
        "Prefer": "return=representation"
    }

    try:

        if method == "GET":

            res = requests.get(
                url,
                headers=headers,
                params=params,
                timeout=30
            )

        elif method == "POST":

            res = requests.post(
                url,
                headers=headers,
                json=json_data,
                timeout=30
            )

        elif method == "PATCH":

            res = requests.patch(
                url,
                headers=headers,
                params=params,
                json=json_data,
                timeout=30
            )

        else:

            return []

        if res.status_code in [200, 201]:

            try:
                return res.json()
            except:
                return []

        logger.error(f"SUPABASE ERROR: {res.text}")

        return []

    except Exception as e:

        logger.error(f"DB ERROR: {e}")

        traceback.print_exc()

        return []

# =========================================================
# USERS
# =========================================================

async def get_or_create_user(
    tg_user: User,
    context: ContextTypes.DEFAULT_TYPE,
    referrer_id=None
):

    user_id = str(tg_user.id)

    data = db_request(
        "GET",
        "users",
        params={
            "user_id": f"eq.{user_id}"
        }
    )

    if data:

        return data[0]

    new_user = {
        "user_id": user_id,
        "username": tg_user.username or "",
        "points": 0,
        "last_daily_gift": "1970-01-01"
    }

    # لا تضيف referred_by إلا إذا كان العمود موجود
    if referrer_id:
        new_user["referred_by"] = referrer_id

    create_result = db_request(
        "POST",
        "users",
        json_data=new_user
    )

    if not create_result:

        logger.error("FAILED TO CREATE USER")

        return {
            "user_id": user_id,
            "points": 0
        }

    limits_data = db_request(
        "GET",
        "daily_limits",
        params={
            "user_id": f"eq.{user_id}"
        }
    )

    if not limits_data:

        limits = {
            "user_id": user_id,
            "last_reset": str(datetime.date.today()),
            "pdf_count": 0,
            "translate_count": 0,
            "voice_count": 0
        }

        db_request(
            "POST",
            "daily_limits",
            json_data=limits
        )

    data = db_request(
        "GET",
        "users",
        params={
            "user_id": f"eq.{user_id}"
        }
    )

    if data:
        return data[0]

    return {
        "user_id": user_id,
        "points": 0
    }

# =========================================================
# LIMITS
# =========================================================

def check_and_reset_limits(user_id):

    today = str(datetime.date.today())

    data = db_request(
        "GET",
        "daily_limits",
        params={
            "user_id": f"eq.{user_id}"
        }
    )

    if not data:

        limits = {
            "user_id": user_id,
            "last_reset": today,
            "pdf_count": 0,
            "translate_count": 0,
            "voice_count": 0
        }

        db_request(
            "POST",
            "daily_limits",
            json_data=limits
        )

        return limits

    row = data[0]

    if row["last_reset"] != today:

        reset_data = {
            "last_reset": today,
            "pdf_count": 0,
            "translate_count": 0,
            "voice_count": 0
        }

        db_request(
            "PATCH",
            "daily_limits",
            params={
                "user_id": f"eq.{user_id}"
            },
            json_data=reset_data
        )

        row.update(reset_data)

    return row

# =========================================================
# SUBSCRIPTION CHECK
# =========================================================

async def is_subscribed(bot, user_id):

    try:

        member = await bot.get_chat_member(
            PRIMARY_CHANNEL,
            int(user_id)
        )

        if member.status in ["left", "kicked"]:

            return False

        return True

    except Exception as e:

        logger.error(f"SUB CHECK ERROR: {e}")

        return False

# =========================================================
# MENUS
# =========================================================

def main_menu():

    keyboard = [
        ["📄 تلخيص PDF", "🎙️ تفريغ صوت"],
        ["🌐 ترجمة"],
        ["👤 حسابي"]
    ]

    return ReplyKeyboardMarkup(
        keyboard,
        resize_keyboard=True
    )

def services_menu_keyboard():

    keyboard = [

        [
            InlineKeyboardButton(
                "📄 تلخيص PDF",
                callback_data="srv_pdf"
            ),

            InlineKeyboardButton(
                "🎙️ تفريغ صوت",
                callback_data="srv_voice"
            )
        ],

        [
            InlineKeyboardButton(
                "🌐 ترجمة",
                callback_data="srv_translate"
            )
        ],

        [
            InlineKeyboardButton(
                "👤 حسابي",
                callback_data="my_account"
            )
        ]
    ]

    return InlineKeyboardMarkup(keyboard)

# =========================================================
# START
# =========================================================

async def cmd_start(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    if not update.message:
        return

    user_id = str(update.effective_user.id)

    args = context.args

    referrer_id = args[0] if args else None

    await get_or_create_user(
        update.effective_user,
        context,
        referrer_id
    )

    if not await is_subscribed(
        context.bot,
        user_id
    ):

        keyboard = [[
            InlineKeyboardButton(
                "📢 الاشتراك",
                url=f"https://t.me/{PRIMARY_CHANNEL.replace('@', '')}"
            )
        ]]

        await update.message.reply_text(
            "⚠️ يجب الاشتراك بالقناة أولاً.",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )

        return

    context.user_data["awaiting_input"] = None

    await update.message.reply_text(
        "👋 أهلاً بك في بوت الخدمات الذكي.",
        reply_markup=main_menu()
    )

# =========================================================
# MENU HANDLER
# =========================================================

async def menu_handler(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    if not update.message:
        return

    text = update.message.text

    if text == "📄 تلخيص PDF":

        context.user_data["awaiting_input"] = "pdf"

        await update.message.reply_text(
            "📄 أرسل ملف PDF الآن."
        )

    elif text == "🎙️ تفريغ صوت":

        context.user_data["awaiting_input"] = "voice"

        await update.message.reply_text(
            "🎙️ أرسل الملف الصوتي الآن."
        )

    elif text == "🌐 ترجمة":

        context.user_data["awaiting_input"] = "translate"

        await update.message.reply_text(
            "🌐 أرسل النص المطلوب ترجمته."
        )

    elif text == "👤 حسابي":

        user = await get_or_create_user(
            update.effective_user,
            context
        )

        limits = check_and_reset_limits(
            str(update.effective_user.id)
        )

        text = f"""
👤 حسابك

🪙 النقاط: {user.get('points', 0)}

📄 PDF:
{3 - limits.get('pdf_count', 0)}/3

🌐 ترجمة:
{3 - limits.get('translate_count', 0)}/3

🎙️ صوت:
{1 - limits.get('voice_count', 0)}/1
"""

        await update.message.reply_text(text)

# =========================================================
# GEMINI FILE WAIT
# =========================================================

async def wait_for_file_ready(file_obj):

    while file_obj.state.name == "PROCESSING":

        await asyncio.sleep(2)

        file_obj = ai_client.files.get(
            name=file_obj.name
        )

    return file_obj

# =========================================================
# PDF SUMMARY
# =========================================================

async def run_pdf_summary(
    bot,
    chat_id,
    file_path
):

    try:

        uploaded_file = ai_client.files.upload(
            file=file_path
        )

        uploaded_file = await wait_for_file_ready(
            uploaded_file
        )

        prompt = """
قم بتحليل ملف PDF بدقة عالية.

المطلوب:

- استخراج الأفكار الأساسية
- كتابة ملخص احترافي
- تقسيم المحتوى بعناوين واضحة
- إبراز أهم النقاط
- تبسيط المعلومات المعقدة
"""

        response = ai_client.models.generate_content(
            model="gemini-2.5-flash",
            contents=[
                uploaded_file,
                prompt
            ]
        )

        await bot.send_message(
            chat_id=chat_id,
            text=response.text[:4000]
        )

        return True

    except Exception as e:

        logger.error(f"PDF ERROR: {e}")

        traceback.print_exc()

        await bot.send_message(
            chat_id=chat_id,
            text="❌ فشل تحليل ملف PDF."
        )

        return False

    finally:

        if os.path.exists(file_path):
            os.remove(file_path)

# =========================================================
# VOICE TRANSCRIPTION
# =========================================================

async def run_voice_transcription(
    bot,
    chat_id,
    file_path
):

    try:

        uploaded_file = ai_client.files.upload(
            file=file_path
        )

        uploaded_file = await wait_for_file_ready(
            uploaded_file
        )

        prompt = """
قم بتحويل الملف الصوتي إلى نص مكتوب باحترافية.
"""

        response = ai_client.models.generate_content(
            model="gemini-2.5-flash",
            contents=[
                uploaded_file,
                prompt
            ]
        )

        await bot.send_message(
            chat_id=chat_id,
            text=response.text[:4000]
        )

        return True

    except Exception as e:

        logger.error(f"VOICE ERROR: {e}")

        traceback.print_exc()

        await bot.send_message(
            chat_id=chat_id,
            text="❌ فشل تفريغ الصوت."
        )

        return False

    finally:

        if os.path.exists(file_path):
            os.remove(file_path)

# =========================================================
# TRANSLATION
# =========================================================

async def run_text_translation(
    bot,
    chat_id,
    text_content
):

    try:

        prompt = f"""
قم بترجمة النص التالي ترجمة احترافية:

{text_content}
"""

        response = ai_client.models.generate_content(
            model="gemini-2.5-flash",
            contents=prompt
        )

        await bot.send_message(
            chat_id=chat_id,
            text=response.text[:4000]
        )

        return True

    except Exception as e:

        logger.error(f"TRANSLATION ERROR: {e}")

        traceback.print_exc()

        await bot.send_message(
            chat_id=chat_id,
            text="❌ فشل الترجمة."
        )

        return False

# =========================================================
# BILLING
# =========================================================

async def process_billing_and_run(
    update,
    context,
    service_key,
    free_limit,
    points_cost,
    worker_func,
    *args
):

    user_id = str(update.effective_user.id)

    user = await get_or_create_user(
        update.effective_user,
        context
    )

    limits = check_and_reset_limits(user_id)

    used = limits.get(service_key, 0)

    if used < free_limit:

        msg = await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text="⏳ جاري التنفيذ..."
        )

        success = await worker_func(
            context.bot,
            update.effective_chat.id,
            *args
        )

        if success:

            db_request(
                "PATCH",
                "daily_limits",
                params={
                    "user_id": f"eq.{user_id}"
                },
                json_data={
                    service_key: used + 1
                }
            )

        await msg.delete()

    else:

        if user.get("points", 0) < points_cost:

            await context.bot.send_message(
                chat_id=update.effective_chat.id,
                text=f"❌ تحتاج {points_cost} نقاط."
            )

            return

        msg = await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text=f"⏳ جاري التنفيذ - خصم {points_cost} نقاط..."
        )

        success = await worker_func(
            context.bot,
            update.effective_chat.id,
            *args
        )

        if success:

            db_request(
                "PATCH",
                "users",
                params={
                    "user_id": f"eq.{user_id}"
                },
                json_data={
                    "points": user["points"] - points_cost
                }
            )

        await msg.delete()

# =========================================================
# DOCUMENTS
# =========================================================

async def handle_docs(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    if not update.message:
        return

    if not update.message.document:
        return

    if context.user_data.get("awaiting_input") != "pdf":
        return

    try:

        document = update.message.document

        safe_name = "".join(
            c for c in document.file_name
            if c.isascii()
        )

        if not safe_name.endswith(".pdf"):
            safe_name = "file.pdf"

        file = await context.bot.get_file(
            document.file_id
        )

        file_path = f"temp_{safe_name}"

        await file.download_to_drive(file_path)

        await process_billing_and_run(
            update,
            context,
            "pdf_count",
            3,
            3,
            run_pdf_summary,
            file_path
        )

    except Exception as e:

        logger.error(f"PDF HANDLER ERROR: {e}")

        traceback.print_exc()

    finally:

        context.user_data["awaiting_input"] = None

# =========================================================
# AUDIO
# =========================================================

async def handle_audio(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    if not update.message:
        return

    if context.user_data.get("awaiting_input") != "voice":
        return

    try:

        file_id = None

        if update.message.voice:
            file_id = update.message.voice.file_id

        elif update.message.audio:
            file_id = update.message.audio.file_id

        else:
            return

        file = await context.bot.get_file(file_id)

        file_path = f"temp_{file_id}.ogg"

        await file.download_to_drive(file_path)

        await process_billing_and_run(
            update,
            context,
            "voice_count",
            1,
            2,
            run_voice_transcription,
            file_path
        )

    except Exception as e:

        logger.error(f"AUDIO HANDLER ERROR: {e}")

        traceback.print_exc()

    finally:

        context.user_data["awaiting_input"] = None

# =========================================================
# TEXT REQUESTS
# =========================================================

async def handle_text_requests(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    if not update.message:
        return

    if not update.message.text:
        return

    text = update.message.text.strip()

    awaiting = context.user_data.get(
        "awaiting_input"
    )

    if awaiting == "translate":

        await process_billing_and_run(
            update,
            context,
            "translate_count",
            3,
            2,
            run_text_translation,
            text
        )

        context.user_data["awaiting_input"] = None

# =========================================================
# ERROR HANDLER
# =========================================================

async def error_handler(
    update,
    context
):

    logger.error(
        msg="Unhandled exception",
        exc_info=context.error
    )

    traceback.print_exc()

    try:

        if update and update.effective_chat:

            await context.bot.send_message(
                chat_id=update.effective_chat.id,
                text="❌ حدث خطأ داخلي أثناء تنفيذ الطلب."
            )

    except:
        pass

# =========================================================
# REGISTER HANDLERS
# =========================================================

ptb_app.add_handler(
    CommandHandler(