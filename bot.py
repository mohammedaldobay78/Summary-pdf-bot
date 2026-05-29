import os
import asyncio
import datetime
import logging
from io import BytesIO
from contextlib import asynccontextmanager
from typing import List, Optional

import httpx
from fastapi import FastAPI, Request, Response, status

from google import genai
from google.genai import types
from google.genai.errors import APIError

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    ReplyKeyboardMarkup,
    LabeledPrice,
    User,
)
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    PreCheckoutQueryHandler,
    ContextTypes,
    filters,
)

# =========================================================
# CONFIGURATION & LOGGING
# =========================================================

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
    handlers=[logging.StreamHandler()]
)
logger = logging.getLogger(__name__)

ADMIN_IDS = [int(id.strip()) for id in os.getenv("ADMIN_IDS", "123456789").split(",")]

PRIMARY_CHANNEL = "@Axia_Tech"
DAILY_FREE_LIMITS = {
    "pdf_count": 3,
    "translate_count": 3,
    "voice_count": 1,
    "image_count": 0
}
POINTS_COSTS = {
    "pdf_count": 3,
    "translate_count": 2,
    "voice_count": 2,
    "image_count": 5
}

STAR_PACKAGES = {
    "pkg_1": {"stars": 1, "points": 3, "title": "باقة البداية"},
    "pkg_2": {"stars": 10, "points": 30, "title": "الباقة الأساسية"},
    "pkg_3": {"stars": 25, "points": 100, "title": "الباقة المتقدمة"},
    "pkg_4": {"stars": 100, "points": 500, "title": "الباقة الاحترافية"}
}

# ربط أسماء الخدمات الداخلية بمفاتيح الحدود والتكاليف في قاعدة البيانات
SERVICE_TO_LIMIT_KEY = {
    "pdf": "pdf_count",
    "translate": "translate_count",
    "voice": "voice_count",
    "infographic": "image_count"
}

# =========================================================
# ENV VALIDATION
# =========================================================

def get_env(key, default=None, required=True):
    val = os.getenv(key, default)
    if required and not val:
        raise ValueError(f"Missing mandatory environment variable: {key}")
    return val

TOKEN = get_env("TELEGRAM_BOT_TOKEN")
SUPABASE_URL = get_env("SUPABASE_URL")
SUPABASE_KEY = get_env("SUPABASE_KEY")
GEMINI_API_KEYS_RAW = get_env("GEMINI_API_KEYS")
GEMINI_API_KEYS = [key.strip() for key in GEMINI_API_KEYS_RAW.split(",") if key.strip()]
if not GEMINI_API_KEYS:
    raise ValueError("GEMINI_API_KEYS must contain at least one key")
RENDER_EXTERNAL_URL = get_env("RENDER_EXTERNAL_URL")

# =========================================================
# CLIENTS
# =========================================================

http_client: httpx.AsyncClient = None

# =========================================================
# GEMINI KEY MANAGER
# =========================================================

class GeminiKeyManager:
    def __init__(self, keys: List[str]):
        self.keys = keys
        self.index = 0
        self.lock = asyncio.Lock()
        self.clients = [genai.Client(api_key=k) for k in keys]

    async def get_current_client(self) -> genai.Client:
        async with self.lock:
            return self.clients[self.index]

    async def handle_error_and_rotate(self):
        """ينتظر 30 ثانية ثم ينتقل للمفتاح التالي بعد خطأ 429 أو 404."""
        logger.warning(f"Key {self.index} hit rate limit/not found. Waiting 30s before switching.")
        await asyncio.sleep(30)
        async with self.lock:
            self.index = (self.index + 1) % len(self.keys)
            logger.info(f"Switched to key index {self.index}")

key_manager = GeminiKeyManager(GEMINI_API_KEYS)

# =========================================================
# DATABASE LAYER
# =========================================================

async def db_request(method, table, params=None, json_data=None):
    url = f"{SUPABASE_URL}/rest/v1/{table}"
    headers = {
        "apikey": SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}",
        "Content-Type": "application/json",
        "Prefer": "return=representation"
    }
    try:
        response = await http_client.request(
            method, url, headers=headers, params=params, json=json_data, timeout=20.0
        )
        if response.status_code in [200, 201, 204]:
            return response.json() if response.text else []
        logger.error(f"Supabase Error [{response.status_code}]: {response.text}")
        return []
    except Exception as e:
        logger.exception(f"DB Error in {method} {table}: {e}")
        return []

async def update_points(user_id: str, amount: int):
    user_data = await db_request("GET", "users", params={"user_id": f"eq.{user_id}"})
    if not user_data:
        logger.error(f"User {user_id} not found while updating points")
        return False
    new_points = max(0, user_data[0].get("points", 0) + amount)
    await db_request("PATCH", "users", params={"user_id": f"eq.{user_id}"}, json_data={"points": new_points})
    return True

async def get_or_init_user(tg_user: User, context: ContextTypes.DEFAULT_TYPE = None, referrer_id=None):
    user_id = str(tg_user.id)
    data = await db_request("GET", "users", params={"user_id": f"eq.{user_id}"})

    if data:
        return data[0]

    new_user = {
        "user_id": user_id,
        "username": tg_user.username or "Unknown",
        "points": 5,
        "referred_by": str(referrer_id) if referrer_id else None
    }
    created = await db_request("POST", "users", json_data=new_user)

    limits = {"user_id": user_id, "last_reset": str(datetime.date.today()), **{k: 0 for k in DAILY_FREE_LIMITS.keys()}}
    await db_request("POST", "daily_limits", json_data=limits)

    if referrer_id and str(referrer_id) != user_id:
        await update_points(str(referrer_id), 2)
        if context:
            try:
                await context.bot.send_message(
                    chat_id=int(referrer_id),
                    text="🎉 قام صديقك بالتسجيل عبر رابطك! تمت إضافة `2` نقطة لحسابك.",
                    parse_mode="Markdown"
                )
            except Exception as e:
                logger.exception(f"Failed to notify referrer {referrer_id}: {e}")

    return created[0] if created else new_user

async def check_and_reset_limits(user_id):
    today = str(datetime.date.today())
    data = await db_request("GET", "daily_limits", params={"user_id": f"eq.{user_id}"})
    if not data:
        limits = {"user_id": user_id, "last_reset": today, **{k: 0 for k in DAILY_FREE_LIMITS.keys()}}
        await db_request("POST", "daily_limits", json_data=limits)
        return limits

    row = data[0]
    if row["last_reset"] != today:
        reset_data = {"last_reset": today, **{k: 0 for k in DAILY_FREE_LIMITS.keys()}}
        await db_request("PATCH", "daily_limits", params={"user_id": f"eq.{user_id}"}, json_data=reset_data)
        row.update(reset_data)
    return row

# =========================================================
# MENUS & KEYBOARDS
# =========================================================

def get_main_keyboard():
    return ReplyKeyboardMarkup([
        ["📄 تلخيص PDF", "🖼️ تصميم إنفوجرافيك"],
        ["🎙️ تفريغ صوت", "🌐 ترجمة"],
        ["🛒 شحن نقاط", "🔗 دعوة الأصدقاء"],
        ["👤 حسابي"]
    ], resize_keyboard=True)

# =========================================================
# AI CORE WORKERS
# =========================================================

async def call_gemini_with_retry(api_call):
    max_attempts = len(GEMINI_API_KEYS) * 3
    for attempt in range(1, max_attempts + 1):
        client = await key_manager.get_current_client()
        try:
            return await api_call(client)
        except APIError as e:
            if e.code in [429, 404]:
                logger.warning(f"Attempt {attempt}: Gemini API error {e.code}, rotating key.")
                await key_manager.handle_error_and_rotate()
                continue
            else:
                logger.exception(f"Gemini API fatal error (code {e.code})")
                raise
        except Exception as e:
            logger.exception(f"Unexpected error during Gemini call on attempt {attempt}")
            raise
    raise Exception("All Gemini API keys exhausted or failed after retries.")

async def process_text_with_gemini(file_path=None, text=None, prompt=""):
    async def api_call(client):
        if file_path:
            uploaded_file = client.files.upload(file=file_path)
            while uploaded_file.state.name == "PROCESSING":
                await asyncio.sleep(2)
                uploaded_file = client.files.get(name=uploaded_file.name)
            contents = [uploaded_file, prompt]
        else:
            contents = f"{prompt}\n\n{text}"

        response = client.models.generate_content(
            model="gemini-2.0-flash",
            contents=contents
        )
        return response.text

    return await call_gemini_with_retry(api_call)

async def generate_infographic(file_path=None, text_content=None):
    async def api_call(client):
        extract_prompt = (
            "اكتب وصفاً مفصلاً باللغة الإنجليزية (Prompt) لتصميم إنفوجرافيك احترافي يلخص المعلومات التالية. "
            "الوصف يجب أن يركز على الألوان، الأيقونات، التوزيع البصري ولا يحتوي على نصوص معقدة، فقط عناوين رئيسية: "
        )
        if file_path:
            uploaded_file = client.files.upload(file=file_path)
            while uploaded_file.state.name == "PROCESSING":
                await asyncio.sleep(2)
                uploaded_file = client.files.get(name=uploaded_file.name)
            response_text = client.models.generate_content(
                model="gemini-2.0-flash",
                contents=[uploaded_file, extract_prompt]
            ).text
        else:
            response_text = client.models.generate_content(
                model="gemini-2.0-flash",
                contents=f"{extract_prompt}\n\n{text_content}"
            ).text

        image_prompt = response_text.strip()
        result = client.models.generate_images(
            model='imagen-3.0-generate-001',
            prompt=f"Professional infographic vector art, clean design, highly detailed, modern flat style, 8k resolution. {image_prompt}",
            config=types.GenerateImagesConfig(
                number_of_images=1,
                output_mime_type="image/jpeg",
                aspect_ratio="3:4"
            )
        )
        return result.generated_images[0].image.image_bytes

    return await call_gemini_with_retry(api_call)

# =========================================================
# QUEUE SYSTEM
# =========================================================

job_queue = asyncio.Queue()

async def ai_worker():
    """معالج خلفي يعالج الطلبات من الطابور بالتسلسل."""
    logger.info("AI worker started")
    while True:
        job = await job_queue.get()
        try:
            chat_id = job["chat_id"]
            user_id = job["user_id"]
            service_type = job["service_type"]
            is_image = job["is_image"]
            file_id = job.get("file_id")
            text = job.get("text")
            bot = job["bot"]
            limit_key = job.get("limit_key")  # المفتاح المستخدم في الحدود والتكاليف

            # تحميل الملف إذا لزم
            file_path = None
            if file_id:
                file_path = f"tmp_{user_id}_{file_id}"
                try:
                    tg_file = await bot.get_file(file_id)
                    await tg_file.download_to_drive(file_path)
                except Exception as e:
                    logger.exception(f"Failed to download file {file_id} for user {user_id}")
                    await bot.send_message(chat_id=chat_id, text="❌ فشل تحميل الملف، أعد المحاولة.")
                    continue

            # تنفيذ المهمة حسب نوع الخدمة
            try:
                if service_type == "pdf":
                    result = await process_text_with_gemini(
                        file_path=file_path,
                        prompt="حلل الملف وقدم ملخصاً تنفيذياً باللغة العربية."
                    )
                elif service_type == "translate":
                    result = await process_text_with_gemini(
                        text=text,
                        prompt="ترجم النص إلى العربية باحترافية:"
                    )
                elif service_type == "voice":
                    # يمكن استخدام نفس معالجة الصوت إن دعمها Gemini لاحقاً، حالياً نستخدم النص المستخرج إن وجد
                    result = await process_text_with_gemini(
                        file_path=file_path,
                        prompt="فرغ هذا المقطع الصوتي إلى نص مع ترجمته للعربية إن لم يكن عربياً."
                    )
                elif service_type == "infographic":
                    result = await generate_infographic(file_path=file_path, text_content=text)
                else:
                    logger.error(f"Unknown service type: {service_type}")
                    continue

                # تحديث الحدود أو خصم النقاط بعد النجاح
                limits = await check_and_reset_limits(user_id)
                # إذا لم يتم تمرير limit_key في الوظيفة، نشتقه من service_type
                if not limit_key:
                    limit_key = SERVICE_TO_LIMIT_KEY.get(service_type, service_type)

                is_free = limits.get(limit_key, 0) < DAILY_FREE_LIMITS.get(limit_key, 0)

                if is_free:
                    await db_request("PATCH", "daily_limits",
                                     params={"user_id": f"eq.{user_id}"},
                                     json_data={limit_key: limits[limit_key] + 1})
                else:
                    await update_points(user_id, -POINTS_COSTS[limit_key])

                # إرسال النتيجة للمستخدم
                if is_image:
                    await bot.send_photo(chat_id=chat_id, photo=result, caption="✨ تم تصميم الإنفوجرافيك!")
                else:
                    text_result = result[:4096]
                    await bot.send_message(chat_id=chat_id, text=text_result)

                logger.info(f"Job completed for user {user_id}, type {service_type}")

            except Exception as e:
                logger.exception(f"AI processing error for user {user_id}, type {service_type}")
                await bot.send_message(chat_id=chat_id, text="❌ حدث خطأ أثناء المعالجة، حاول مرة أخرى لاحقاً.")
            finally:
                # تنظيف الملف المؤقت
                if file_path and os.path.exists(file_path):
                    os.remove(file_path)

        except Exception as e:
            logger.exception(f"Worker exception: {e}")
        finally:
            job_queue.task_done()

# =========================================================
# TELEGRAM HANDLERS: START & MENU
# =========================================================

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    referrer = context.args[0] if context.args else None
    try:
        await get_or_init_user(user, context, referrer)
    except Exception as e:
        logger.exception(f"Error in start for user {user.id}")
        await update.message.reply_text("❌ حدث خطأ أثناء تهيئة حسابك. حاول مجدداً.")
        return

    try:
        member = await context.bot.get_chat_member(PRIMARY_CHANNEL, user.id)
        if member.status in ["left", "kicked"]:
            keyboard = [[InlineKeyboardButton("📢 اشترك في القناة", url=f"https://t.me/{PRIMARY_CHANNEL[1:]}")]]
            await update.message.reply_text("⚠️ يجب الاشتراك بالقناة أولاً.", reply_markup=InlineKeyboardMarkup(keyboard))
            return
    except Exception as e:
        logger.exception(f"Error checking channel membership for {user.id}")
        pass

    await update.message.reply_text(
        f"مرحباً بك {user.first_name}! 👋\nاختر الخدمة المطلوبة:",
        reply_markup=get_main_keyboard()
    )

async def menu_logic(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    user_id = str(update.effective_user.id)
    bot_username = context.bot.username

    try:
        if text == "👤 حسابي":
            user = await get_or_init_user(update.effective_user)
            limits = await check_and_reset_limits(user_id)
            msg = (
                f"👤 **معلومات حسابك**\n"
                f"🆔 الآيدي: `{user_id}`\n"
                f"🪙 رصيد النقاط: `{user.get('points', 0)}` نقطة\n\n"
                f"📊 **المجاني المتبقي لليوم:**\n"
                f"• تلخيص: `{DAILY_FREE_LIMITS['pdf_count'] - limits['pdf_count']}`\n"
                f"• ترجمة: `{DAILY_FREE_LIMITS['translate_count'] - limits['translate_count']}`\n"
                f"• صوتيات: `{DAILY_FREE_LIMITS['voice_count'] - limits['voice_count']}`\n\n"
                f"💡 *لتحويل النقاط استخدم الأمر:*\n`/transfer {user_id} 10`"
            )
            await update.message.reply_text(msg, parse_mode="Markdown")
            return

        if text == "🔗 دعوة الأصدقاء":
            ref_link = f"https://t.me/{bot_username}?start={user_id}"
            await update.message.reply_text(
                f"🎁 **اربح نقاط مجانية!**\n"
                f"شارك هذا الرابط مع أصدقائك، وستحصل على `2` نقطة لكل شخص يدخل البوت عن طريقك:\n\n{ref_link}",
                parse_mode="Markdown"
            )
            return

        if text == "🛒 شحن نقاط":
            keyboard = []
            for key, pkg in STAR_PACKAGES.items():
                keyboard.append([InlineKeyboardButton(
                    f"⭐️ {pkg['stars']} نجمة = 🪙 {pkg['points']} نقطة",
                    callback_data=f"buy_{key}"
                )])
            await update.message.reply_text("اختر الباقة المناسبة لك:", reply_markup=InlineKeyboardMarkup(keyboard))
            return

        mapping = {
            "📄 تلخيص PDF": ("pdf", "📄 أرسل ملف PDF للتلخيص:"),
            "🎙️ تفريغ صوت": ("voice", "🎙️ أرسل التسجيل الصوتي:"),
            "🌐 ترجمة": ("translate", "🌐 أرسل النص للترجمة:"),
            "🖼️ تصميم إنفوجرافيك": ("infographic", "🖼️ أرسل نصاً أو ملف PDF وسأقوم بتصميم إنفوجرافيك له (التكلفة: 5 نقاط):")
        }

        if text in mapping:
            state, msg = mapping[text]
            context.user_data["state"] = state
            await update.message.reply_text(msg)

    except Exception as e:
        logger.exception(f"Error in menu_logic for user {user_id}")
        await update.message.reply_text("❌ حدث خطأ غير متوقع. الرجاء المحاولة لاحقاً.")

# =========================================================
# SERVICE ENQUEUEING
# =========================================================

async def enqueue_service(update: Update, context: ContextTypes.DEFAULT_TYPE, service_type,
                          file_id=None, text=None, is_image=False):
    user_id = str(update.effective_user.id)
    try:
        limits = await check_and_reset_limits(user_id)
        user = await get_or_init_user(update.effective_user)

        # تحويل اسم الخدمة إلى المفتاح المستخدم في الحدود والتكاليف
        limit_key = SERVICE_TO_LIMIT_KEY.get(service_type)
        if not limit_key:
            logger.error(f"Invalid service_type enqueued: {service_type}")
            await update.message.reply_text("❌ نوع خدمة غير معروف.")
            return

        is_free = limits.get(limit_key, 0) < DAILY_FREE_LIMITS.get(limit_key, 0)
        cost = POINTS_COSTS.get(limit_key, 0)

        if not is_free and user.get("points", 0) < cost:
            await update.message.reply_text(
                f"❌ رصيدك غير كافٍ. (تحتاج {cost} نقاط)\nاضغط '🛒 شحن نقاط' أو '🔗 دعوة الأصدقاء'."
            )
            return

        # إضافة للطابور مع تخزين limit_key للاستخدام لاحقاً
        job = {
            "chat_id": update.effective_chat.id,
            "user_id": user_id,
            "service_type": service_type,
            "limit_key": limit_key,
            "is_image": is_image,
            "file_id": file_id,
            "text": text,
            "bot": context.bot
        }
        await job_queue.put(job)
        await update.message.reply_text("⏳ تمت إضافة طلبك إلى قائمة الانتظار. سيتم إشعارك عند الانتهاء.")

        context.user_data["state"] = None

    except Exception as e:
        logger.exception(f"Error enqueuing service for user {user_id}, type {service_type}")
        await update.message.reply_text("❌ حدث خطأ أثناء إرسال الطلب. حاول مجدداً.")

# =========================================================
# MEDIA & TEXT HANDLERS
# =========================================================

async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    state = context.user_data.get("state")
    if state not in ["pdf", "infographic"] or not update.message.document:
        return

    doc = update.message.document
    if not doc.file_name.lower().endswith(".pdf"):
        await update.message.reply_text("❌ أرسل ملف PDF فقط.")
        return

    await enqueue_service(update, context, state, file_id=doc.file_id, is_image=(state == "infographic"))

async def handle_text_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    state = context.user_data.get("state")
    text = update.message.text

    if state == "translate":
        await enqueue_service(update, context, "translate", text=text)
    elif state == "infographic":
        await enqueue_service(update, context, "infographic", text=text, is_image=True)

async def handle_voice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if context.user_data.get("state") == "voice":
        voice = update.message.voice
        await enqueue_service(update, context, "voice", file_id=voice.file_id)

# =========================================================
# POINTS TRANSFER & PAYMENTS
# =========================================================

async def cmd_transfer(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    try:
        target_id = context.args[0]
        amount = int(context.args[1])
        if amount <= 0:
            raise ValueError
    except:
        await update.message.reply_text("❌ الصيغة الخاطئة. الاستخدام:\n`/transfer <ID> <الكمية>`", parse_mode="Markdown")
        return

    if user_id == target_id:
        await update.message.reply_text("❌ لا يمكنك التحويل لنفسك.")
        return

    try:
        sender = await get_or_init_user(update.effective_user)
        if sender.get("points", 0) < amount:
            await update.message.reply_text("❌ رصيدك غير كافٍ لإتمام التحويل.")
            return

        target_user = await db_request("GET", "users", params={"user_id": f"eq.{target_id}"})
        if not target_user:
            await update.message.reply_text("❌ المستخدم غير مسجل في البوت.")
            return

        await update_points(user_id, -amount)
        await update_points(target_id, amount)

        await update.message.reply_text(f"✅ تم تحويل {amount} نقطة بنجاح إلى المستخدم {target_id}.")
        try:
            await context.bot.send_message(chat_id=int(target_id), text=f"🎉 لقد تلقيت تحويلاً بقيمة {amount} نقطة!")
        except Exception as e:
            logger.exception(f"Failed to notify transfer target {target_id}")

    except Exception as e:
        logger.exception(f"Transfer error from {user_id} to {target_id}")
        await update.message.reply_text("❌ حدث خطأ أثناء التحويل. الرجاء المحاولة لاحقاً.")

async def buy_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    pkg_key = query.data.split("_", 1)[1]
    pkg = STAR_PACKAGES.get(pkg_key)
    if not pkg:
        return

    title = pkg["title"]
    description = f"شراء {pkg['points']} نقطة لاستخدام خدمات الذكاء الاصطناعي."
    payload = f"buy_points_{update.effective_user.id}_{pkg['points']}"

    try:
        await context.bot.send_invoice(
            chat_id=update.effective_chat.id,
            title=title,
            description=description,
            payload=payload,
            provider_token="",
            currency="XTR",
            prices=[LabeledPrice("نجوم", pkg["stars"])]
        )
    except Exception as e:
        logger.exception(f"Failed to send invoice to user {update.effective_user.id}")

async def precheckout_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.pre_checkout_query
    if query.invoice_payload.startswith("buy_points_"):
        await query.answer(ok=True)
    else:
        await query.answer(ok=False, error_message="طلب غير صالح.")

async def successful_payment_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    payload = update.message.successful_payment.invoice_payload
    parts = payload.split("_")

    if len(parts) == 4 and parts[1] == "points":
        user_id = parts[2]
        points_to_add = int(parts[3])

        try:
            await update_points(user_id, points_to_add)
            await update.message.reply_text(f"✅ شكراً لك! تمت إضافة {points_to_add} نقطة إلى حسابك بنجاح.")
        except Exception as e:
            logger.exception(f"Failed to add points after payment for user {user_id}")
            await update.message.reply_text("❌ حدث خطأ أثناء إضافة النقاط. تم استلام دفعتك وسيتم إضافتها يدوياً قريباً.")

# =========================================================
# ADMIN PANEL
# =========================================================

async def check_admin(update: Update):
    if update.effective_user.id not in ADMIN_IDS:
        await update.message.reply_text("❌ ليس لديك صلاحية.")
        return False
    return True

async def cmd_admin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await check_admin(update):
        return
    try:
        users = await db_request("GET", "users")
        count = len(users) if users else 0
        await update.message.reply_text(
            f"🛠 **لوحة التحكم**\n"
            f"👥 إجمالي المستخدمين: {count}\n\n"
            f"الأوامر المتاحة:\n"
            f"`/addpoints <ID> <Amount>` - إضافة نقاط لمستخدم\n"
            f"`/broadcast <Text>` - إرسال رسالة للجميع",
            parse_mode="Markdown"
        )
    except Exception as e:
        logger.exception("Admin panel error")

async def cmd_addpoints(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await check_admin(update):
        return
    try:
        uid = context.args[0]
        amt = int(context.args[1])
        await update_points(uid, amt)
        await update.message.reply_text(f"✅ تمت إضافة {amt} نقطة للمستخدم {uid}.")
    except Exception as e:
        logger.exception("Error in addpoints command")
        await update.message.reply_text("❌ خطأ بالصيغة: `/addpoints id 50`")

async def cmd_broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await check_admin(update):
        return
    text = " ".join(context.args)
    if not text:
        await update.message.reply_text("❌ اكتب الرسالة بعد الأمر.")
        return

    users = await db_request("GET", "users")
    sent = 0
    msg = await update.message.reply_text("⏳ جاري الإرسال...")
    for u in users:
        try:
            await context.bot.send_message(chat_id=int(u["user_id"]), text=f"📢 **إعلان:**\n{text}", parse_mode="Markdown")
            sent += 1
            await asyncio.sleep(0.05)
        except Exception as e:
            logger.exception(f"Failed to send broadcast to user {u.get('user_id')}")
    await msg.edit_text(f"✅ تم الإرسال إلى {sent} مستخدم.")

# =========================================================
# FASTAPI & WEBHOOK
# =========================================================

ptb_app = Application.builder().token(TOKEN).build()

@asynccontextmanager
async def lifespan(app: FastAPI):
    global http_client, worker_task
    http_client = httpx.AsyncClient()
    await ptb_app.initialize()

    # بدء معالج الطابور
    worker_task = asyncio.create_task(ai_worker())

    webhook_url = f"{RENDER_EXTERNAL_URL.rstrip('/')}/webhook"
    await ptb_app.bot.set_webhook(url=webhook_url, allowed_updates=Update.ALL_TYPES)

    yield

    # إيقاف المعالج عند الخروج
    worker_task.cancel()
    await ptb_app.shutdown()
    await http_client.aclose()

fast_app = FastAPI(lifespan=lifespan)

@fast_app.post("/webhook")
async def webhook(request: Request):
    try:
        data = await request.json()
        update = Update.de_json(data, ptb_app.bot)
        await ptb_app.process_update(update)
    except Exception as e:
        logger.exception("Error processing webhook update")
    return Response(status_code=status.HTTP_200_OK)

@fast_app.get("/health")
async def health():
    return {"status": "running"}

@fast_app.get("/")
async def root():
    return {"status": "bot is running"}

# Register Handlers
ptb_app.add_handler(CommandHandler("start", cmd_start))
ptb_app.add_handler(CommandHandler("transfer", cmd_transfer))

# Admin
ptb_app.add_handler(CommandHandler("admin", cmd_admin))
ptb_app.add_handler(CommandHandler("addpoints", cmd_addpoints))
ptb_app.add_handler(CommandHandler("broadcast", cmd_broadcast))

# Menus & Media
ptb_app.add_handler(MessageHandler(
    filters.Regex("^(📄 تلخيص PDF|🖼️ تصميم إنفوجرافيك|🎙️ تفريغ صوت|🌐 ترجمة|🛒 شحن نقاط|🔗 دعوة الأصدقاء|👤 حسابي)$"),
    menu_logic
))
ptb_app.add_handler(MessageHandler(filters.Document.PDF, handle_document))
ptb_app.add_handler(MessageHandler(filters.VOICE, handle_voice))
ptb_app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text_input))

# Payments
ptb_app.add_handler(CallbackQueryHandler(buy_callback, pattern="^buy_"))
ptb_app.add_handler(PreCheckoutQueryHandler(precheckout_handler))
ptb_app.add_handler(MessageHandler(filters.SUCCESSFUL_PAYMENT, successful_payment_handler))

if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run(fast_app, host="0.0.0.0", port=port)