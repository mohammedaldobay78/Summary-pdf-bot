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

SUPABASE_KEY = os.getenv("SUPABASE_KEY")

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
    raise ValueError("SUPABASE_KEY missing")

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

            return res.json()

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

    if not data:

        new_user = {
            "user_id": user_id,
            "username": tg_user.username or "",
            "points": 0,
            "last_daily_gift": "1970-01-01",
            "referred_by": referrer_id
        }

        db_request(
            "POST",
            "users",
            json_data=new_user
        )

        limits = {
            "user_id": user_id,
            "last_reset": str(datetime.date.today()),
            "pdf_count": 0,
            "translate_count": 0,
            "voice_count": 0,
            "info_count": 0,
            "mind_count": 0
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

    return data[0]

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
            "voice_count": 0,
            "info_count": 0,
            "mind_count": 0
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
            "voice_count": 0,
            "info_count": 0,
            "mind_count": 0
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
                "📊 إنفوجرافيك",
                callback_data="srv_info"
            ),

            InlineKeyboardButton(
                "🧠 مخطط ذهني",
                callback_data="srv_mind"
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
        "👋 أهلاً بك في بوت الخدمات الذكي.\n\nاختر الخدمة المطلوبة:",
        reply_markup=services_menu_keyboard()
    )

# =========================================================
# CALLBACKS
# =========================================================

async def handle_callbacks(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    query = update.callback_query

    if not query:
        return

    await query.answer()

    data = query.data

    if data.startswith("srv_"):

        service = data.split("_")[1]

        context.user_data["awaiting_input"] = service

        messages = {

            "pdf":
                "📄 أرسل ملف PDF الآن.",

            "voice":
                "🎙️ أرسل الملف الصوتي الآن.",

            "translate":
                "🌐 أرسل النص المطلوب ترجمته.",

            "info":
                "📊 أرسل موضوع الإنفوجرافيك.",

            "mind":
                "🧠 أرسل فكرة المخطط الذهني."
        }

        await query.edit_message_text(
            messages[service]
        )

    elif data == "my_account":

        user = await get_or_create_user(
            query.from_user,
            context
        )

        limits = check_and_reset_limits(
            str(query.from_user.id)
        )

        text = f"""
👤 حسابك

🪙 النقاط: {user['points']}

📄 PDF:
{3 - limits['pdf_count']}/3

🌐 ترجمة:
{3 - limits['translate_count']}/3

🎙️ صوت:
{1 - limits['voice_count']}/1

📊 إنفوجرافيك:
{2 - limits['info_count']}/2

🧠 مخطط ذهني:
{2 - limits['mind_count']}/2
"""

        await query.edit_message_text(text)

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
- الحفاظ على المعنى الأكاديمي

استخدم تنسيقاً منظماً وواضحاً.
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

المطلوب:

- تفريغ الكلام كاملاً
- تصحيح الأخطاء اللغوية
- إزالة التكرار غير الضروري
- تنسيق النص بشكل مرتب
- تقسيم الفقرات
- الحفاظ على المعنى الأصلي
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
قم بترجمة النص التالي ترجمة احترافية دقيقة.

المطلوب:

- الحفاظ على المعنى
- تحسين الصياغة
- استخدام لغة أكاديمية واضحة
- تجنب الترجمة الحرفية

النص:

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
# IMAGE GENERATION
# =========================================================

async def run_generate_image(
    bot,
    chat_id,
    prompt_text,
    image_type
):

    try:

        prompt = f"""
Create a premium quality professional {image_type}.

Topic:
{prompt_text}

Requirements:

- Modern design
- Clean layout
- Professional typography
- High readability
- Structured visual hierarchy
- Attractive composition
- Detailed informative content
- 16:9 aspect ratio
"""

        result = ai_client.models.generate_images(

            model="imagen-3.0-generate-002",

            prompt=prompt,

            config=types.GenerateImagesConfig(
                number_of_images=1,
                output_mime_type="image/jpeg",
                aspect_ratio="16:9"
            )
        )

        for image in result.generated_images:

            await bot.send_photo(
                chat_id=chat_id,
                photo=io.BytesIO(
                    image.image.image_bytes
                )
            )

        return True

    except Exception as e:

        logger.error(f"IMAGE ERROR: {e}")

        traceback.print_exc()

        await bot.send_message(
            chat_id=chat_id,
            text="❌ فشل توليد الصورة."
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

    used = limits[service_key]

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

        if user["points"] < points_cost:

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

        file = await context.bot.get_file(
            update.message.document.file_id
        )

        file_path = (
            f"temp_{update.message.document.file_name}"
        )

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

    elif awaiting == "info":

        await process_billing_and_run(
            update,
            context,
            "info_count",
            2,
            5,
            run_generate_image,
            text,
            "infographic"
        )

    elif awaiting == "mind":

        await process_billing_and_run(
            update,
            context,
            "mind_count",
            2,
            5,
            run_generate_image,
            text,
            "mind map"
        )

    else:

        await update.message.reply_text(
            "👇 اختر الخدمة أولاً.",
            reply_markup=services_menu_keyboard()
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
        "start",
        cmd_start
    )
)

ptb_app.add_handler(
    CallbackQueryHandler(
        handle_callbacks
    )
)

ptb_app.add_handler(
    MessageHandler(
        filters.Document.PDF,
        handle_docs
    )
)

ptb_app.add_handler(
    MessageHandler(
        filters.VOICE | filters.AUDIO,
        handle_audio
    )
)

ptb_app.add_handler(
    MessageHandler(
        filters.TEXT & ~filters.COMMAND,
        handle_text_requests
    )
)

ptb_app.add_error_handler(
    error_handler
)

# =========================================================
# WEBHOOK
# =========================================================

def setup_webhook():

    asyncio.run_coroutine_threadsafe(
        ptb_app.initialize(),
        bot_loop
    ).result()

    asyncio.run_coroutine_threadsafe(
        ptb_app.start(),
        bot_loop
    ).result()

    if RENDER_EXTERNAL_URL:

        webhook_url = (
            f"{RENDER_EXTERNAL_URL}/{TOKEN}"
        )

        asyncio.run_coroutine_threadsafe(
            ptb_app.bot.set_webhook(
                url=webhook_url
            ),
            bot_loop
        ).result()

        logger.info("Webhook configured")

setup_webhook()

# =========================================================
# FLASK ROUTES
# =========================================================

@app.route(
    f"/{TOKEN}",
    methods=["POST"]
)
def telegram_webhook():

    try:

        data = request.get_json(force=True)

        update = Update.de_json(
            data,
            ptb_app.bot
        )

        asyncio.run_coroutine_threadsafe(
            ptb_app.process_update(update),
            bot_loop
        )

        return "OK", 200

    except Exception as e:

        logger.error(f"WEBHOOK ERROR: {e}")

        traceback.print_exc()

        return "ERROR", 500

@app.route("/")
def home():

    return "BOT RUNNING", 200

# =========================================================
# MAIN
# =========================================================

if __name__ == "__main__":

    app.run(
        host="0.0.0.0",
        port=int(
            os.environ.get("PORT", 5000)
        )
    )