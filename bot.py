import os
import asyncio
import datetime
import logging
from io import BytesIO
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI, Request, Response, status

from google import genai
from google.genai import types

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
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# ضع معرفات حسابات الأدمن هنا (ID)
ADMIN_IDS = [int(id.strip()) for id in os.getenv("ADMIN_IDS", "123456789").split(",")]

PRIMARY_CHANNEL = "@Axia_Tech"
DAILY_FREE_LIMITS = {
    "pdf_count": 3,
    "translate_count": 3,
    "voice_count": 1,
    "image_count": 0 # لا يوجد مجاني للصور لتكلفتها
}
POINTS_COSTS = {
    "pdf_count": 3,
    "translate_count": 2,
    "voice_count": 2,
    "image_count": 5
}

# باقات نجوم تلغرام (Stars: Points)
STAR_PACKAGES = {
    "pkg_1": {"stars": 1, "points": 3, "title": "باقة البداية"},
    "pkg_2": {"stars": 10, "points": 30, "title": "الباقة الأساسية"},
    "pkg_3": {"stars": 25, "points": 100, "title": "الباقة المتقدمة"},
    "pkg_4": {"stars": 100, "points": 500, "title": "الباقة الاحترافية"}
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
SUPABASE_KEY = get_env("SUPABASE_SERVICE_KEY") or get_env("SUPABASE_KEY")
GOOGLE_API_KEY = get_env("GEMINI_API_KEY")
RENDER_EXTERNAL_URL = get_env("RENDER_EXTERNAL_URL")

# =========================================================
# CLIENTS
# =========================================================

ai_client = genai.Client(api_key=GOOGLE_API_KEY)
http_client: httpx.AsyncClient = None

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
        logger.error(f"DB Error: {e}")
        return []

async def update_points(user_id: str, amount: int):
    """إضافة أو خصم نقاط بشكل مباشر"""
    user_data = await db_request("GET", "users", params={"user_id": f"eq.{user_id}"})
    if not user_data: return False
    new_points = max(0, user_data[0].get("points", 0) + amount)
    await db_request("PATCH", "users", params={"user_id": f"eq.{user_id}"}, json_data={"points": new_points})
    return True

async def get_or_init_user(tg_user: User, context: ContextTypes.DEFAULT_TYPE = None, referrer_id=None):
    user_id = str(tg_user.id)
    data = await db_request("GET", "users", params={"user_id": f"eq.{user_id}"})
    
    if data:
        return data[0]

    # مستخدم جديد
    new_user = {
        "user_id": user_id,
        "username": tg_user.username or "Unknown",
        "points": 5, 
        "referred_by": str(referrer_id) if referrer_id else None
    }
    created = await db_request("POST", "users", json_data=new_user)
    
    # تهيئة حدود الاستخدام اليومي
    limits = {"user_id": user_id, "last_reset": str(datetime.date.today()), **{k: 0 for k in DAILY_FREE_LIMITS.keys()}}
    await db_request("POST", "daily_limits", json_data=limits)
    
    # مكافأة الإحالة (نقطتين للمُحيل)
    if referrer_id and str(referrer_id) != user_id:
        await update_points(str(referrer_id), 2)
        if context:
            try:
                await context.bot.send_message(
                    chat_id=int(referrer_id),
                    text="🎉 قام صديقك بالتسجيل عبر رابطك! تمت إضافة `2` نقطة لحسابك.",
                    parse_mode="Markdown"
                )
            except: pass

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

async def process_text_with_gemini(file_path=None, text=None, prompt=""):
    if file_path:
        uploaded_file = ai_client.files.upload(file=file_path)
        while uploaded_file.state.name == "PROCESSING":
            await asyncio.sleep(2)
            uploaded_file = ai_client.files.get(name=uploaded_file.name)
        contents = [uploaded_file, prompt]
    else:
        contents = f"{prompt}\n\n{text}"

    response = ai_client.models.generate_content(
        model="gemini-2.0-flash",
        contents=contents
    )
    return response.text

async def generate_infographic(file_path=None, text_content=None):
    """
    1. يقرأ النص/الملف ويستخرج النقاط الرئيسية كـ 'وصف للصورة'
    2. يرسل الوصف إلى نموذج Imagen 3 لتوليد الإنفوجرافيك
    """
    extract_prompt = "اكتب وصفاً مفصلاً باللغة الإنجليزية (Prompt) لتصميم إنفوجرافيك احترافي يلخص المعلومات التالية. الوصف يجب أن يركز على الألوان، الأيقونات، التوزيع البصري ولا يحتوي على نصوص معقدة، فقط عناوين رئيسية: "
    
    # 1. استخراج فكرة التصميم (Prompt)
    image_prompt = await process_text_with_gemini(file_path, text_content, extract_prompt)
    
    # 2. توليد الصورة
    result = ai_client.models.generate_images(
        model='imagen-3.0-generate-001',
        prompt=f"Professional infographic vector art, clean design, highly detailed, modern flat style, 8k resolution. {image_prompt}",
        config=types.GenerateImagesConfig(
            number_of_images=1,
            output_mime_type="image/jpeg",
            aspect_ratio="3:4"
        )
    )
    return result.generated_images[0].image.image_bytes

# =========================================================
# TELEGRAM HANDLERS: START & MENU
# =========================================================

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    referrer = context.args[0] if context.args else None
    await get_or_init_user(user, context, referrer)

    # التحقق من الاشتراك
    try:
        member = await context.bot.get_chat_member(PRIMARY_CHANNEL, user.id)
        if member.status in ["left", "kicked"]:
            keyboard = [[InlineKeyboardButton("📢 اشترك في القناة", url=f"https://t.me/{PRIMARY_CHANNEL[1:]}")]]
            await update.message.reply_text("⚠️ يجب الاشتراك بالقناة أولاً.", reply_markup=InlineKeyboardMarkup(keyboard))
            return
    except Exception: pass

    await update.message.reply_text(
        f"مرحباً بك {user.first_name}! 👋\nاختر الخدمة المطلوبة:",
        reply_markup=get_main_keyboard()
    )

async def menu_logic(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    user_id = str(update.effective_user.id)
    bot_username = context.bot.username

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

# =========================================================
# SERVICE EXECUTOR
# =========================================================

async def execute_service(update: Update, context: ContextTypes.DEFAULT_TYPE, service_type, task_coro, is_image=False):
    user_id = str(update.effective_user.id)
    limits = await check_and_reset_limits(user_id)
    user = await get_or_init_user(update.effective_user)
    
    is_free = limits.get(service_type, 0) < DAILY_FREE_LIMITS.get(service_type, 0)
    cost = POINTS_COSTS[service_type]
    
    if not is_free and user.get("points", 0) < cost:
        await update.message.reply_text(f"❌ رصيدك غير كافٍ. (تحتاج {cost} نقاط)\nاضغط '🛒 شحن نقاط' أو '🔗 دعوة الأصدقاء'.")
        return

    status_msg = await update.message.reply_text("⏳ جاري المعالجة بواسطة الذكاء الاصطناعي... قد يستغرق دقيقة.")

    try:
        result = await task_coro
        
        if is_free:
            await db_request("PATCH", "daily_limits", params={"user_id": f"eq.{user_id}"}, 
                             json_data={service_type: limits[service_type] + 1})
        else:
            await update_points(user_id, -cost)

        await status_msg.delete()
        if is_image:
            await update.message.reply_photo(photo=result, caption="✨ تم تصميم الإنفوجرافيك!")
        else:
            await update.message.reply_text(result[:4096])
            
    except Exception as e:
        logger.error(f"Service Error: {e}")
        await status_msg.edit_text("❌ حدث خطأ، يرجى المحاولة لاحقاً.")
    finally:
        context.user_data["state"] = None

# =========================================================
# MEDIA & TEXT HANDLERS
# =========================================================

async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    state = context.user_data.get("state")
    if state not in ["pdf", "infographic"] or not update.message.document: return
    
    doc = update.message.document
    if not doc.file_name.lower().endswith(".pdf"):
        await update.message.reply_text("❌ أرسل ملف PDF فقط.")
        return

    file_path = f"tmp_{update.effective_user.id}_{doc.file_id}.pdf"
    
    async def task_pdf():
        try:
            tg_file = await context.bot.get_file(doc.file_id)
            await tg_file.download_to_drive(file_path)
            if state == "pdf":
                return await process_text_with_gemini(file_path, prompt="حلل الملف وقدم ملخصاً تنفيذياً باللغة العربية.")
            elif state == "infographic":
                return await generate_infographic(file_path=file_path)
        finally:
            if os.path.exists(file_path): os.remove(file_path)

    if state == "infographic":
        await execute_service(update, context, "image_count", task_pdf(), is_image=True)
    else:
        await execute_service(update, context, "pdf_count", task_pdf())

async def handle_text_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    state = context.user_data.get("state")
    text = update.message.text

    if state == "translate":
        async def task():
            return await process_text_with_gemini(text=text, prompt="ترجم النص إلى العربية باحترافية:")
        await execute_service(update, context, "translate_count", task())
        
    elif state == "infographic":
        async def task():
            return await generate_infographic(text_content=text)
        await execute_service(update, context, "image_count", task(), is_image=True)

# =========================================================
# POINTS TRANSFER & PAYMENTS (TELEGRAM STARS)
# =========================================================

async def cmd_transfer(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/transfer 123456789 10"""
    user_id = str(update.effective_user.id)
    try:
        target_id = context.args[0]
        amount = int(context.args[1])
        if amount <= 0: raise ValueError
    except:
        await update.message.reply_text("❌ الصيغة الخاطئة. الاستخدام:\n`/transfer <ID> <الكمية>`", parse_mode="Markdown")
        return

    if user_id == target_id:
        await update.message.reply_text("❌ لا يمكنك التحويل لنفسك.")
        return

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
    except: pass

async def buy_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    pkg_key = query.data.split("_", 1)[1]
    pkg = STAR_PACKAGES.get(pkg_key)
    if not pkg: return

    title = pkg["title"]
    description = f"شراء {pkg['points']} نقطة لاستخدام خدمات الذكاء الاصطناعي."
    payload = f"buy_points_{update.effective_user.id}_{pkg['points']}"
    
    await context.bot.send_invoice(
        chat_id=update.effective_chat.id,
        title=title,
        description=description,
        payload=payload,
        provider_token="", # فارغ دائماً لنجوم تلغرام
        currency="XTR",
        prices=[LabeledPrice("نجوم", pkg["stars"])]
    )

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
        
        await update_points(user_id, points_to_add)
        await update.message.reply_text(f"✅ شكراً لك! تمت إضافة {points_to_add} نقطة إلى حسابك بنجاح.")

# =========================================================
# ADMIN PANEL
# =========================================================

async def check_admin(update: Update):
    if update.effective_user.id not in ADMIN_IDS:
        await update.message.reply_text("❌ ليس لديك صلاحية.")
        return False
    return True

async def cmd_admin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await check_admin(update): return
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

async def cmd_addpoints(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await check_admin(update): return
    try:
        uid = context.args[0]
        amt = int(context.args[1])
        await update_points(uid, amt)
        await update.message.reply_text(f"✅ تمت إضافة {amt} نقطة للمستخدم {uid}.")
    except:
        await update.message.reply_text("❌ خطأ بالصيغة: `/addpoints id 50`")

async def cmd_broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await check_admin(update): return
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
            await asyncio.sleep(0.05) # تجنب الحظر
        except: pass
    await msg.edit_text(f"✅ تم الإرسال إلى {sent} مستخدم.")

# =========================================================
# FASTAPI & WEBHOOK
# =========================================================

ptb_app = Application.builder().token(TOKEN).build()

@asynccontextmanager
async def lifespan(app: FastAPI):
    global http_client
    http_client = httpx.AsyncClient()
    await ptb_app.initialize()
    
    webhook_url = f"{RENDER_EXTERNAL_URL.rstrip('/')}/webhook"
    await ptb_app.bot.set_webhook(url=webhook_url, allowed_updates=Update.ALL_TYPES)
    yield
    await ptb_app.shutdown()
    await http_client.aclose()

fast_app = FastAPI(lifespan=lifespan)

@fast_app.post("/webhook")
async def webhook(request: Request):
    data = await request.json()
    update = Update.de_json(data, ptb_app.bot)
    await ptb_app.process_update(update)
    return Response(status_code=status.HTTP_200_OK)

# Register Handlers
ptb_app.add_handler(CommandHandler("start", cmd_start))
ptb_app.add_handler(CommandHandler("transfer", cmd_transfer))

# Admin
ptb_app.add_handler(CommandHandler("admin", cmd_admin))
ptb_app.add_handler(CommandHandler("addpoints", cmd_addpoints))
ptb_app.add_handler(CommandHandler("broadcast", cmd_broadcast))

# Menus & Media
ptb_app.add_handler(MessageHandler(filters.Regex("^(📄 تلخيص PDF|🖼️ تصميم إنفوجرافيك|🎙️ تفريغ صوت|🌐 ترجمة|🛒 شحن نقاط|🔗 دعوة الأصدقاء|👤 حسابي)$"), menu_logic))
ptb_app.add_handler(MessageHandler(filters.Document.PDF, handle_document))
ptb_app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text_input))

# Payments
ptb_app.add_handler(CallbackQueryHandler(buy_callback, pattern="^buy_"))
ptb_app.add_handler(PreCheckoutQueryHandler(precheckout_handler))
ptb_app.add_handler(MessageHandler(filters.SUCCESSFUL_PAYMENT, successful_payment_handler))

if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run(fast_app, host="0.0.0.0", port=port)