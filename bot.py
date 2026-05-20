import os
import datetime
import io
import asyncio
from threading import Thread
from flask import Flask, request
from google import genai
from google.genai import types
import requests

# استيراد مكتبة python-telegram-bot
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, LabeledPrice, User
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    PreCheckoutQueryHandler,
    ContextTypes,
    filters,
)

# ----------------------------------------------------------------
# 1. الإعدادات والمتغيرات البيئية
# ----------------------------------------------------------------
TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
BOT_USERNAME = os.getenv("TELEGRAM_BOT_USERNAME")
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")
GOOGLE_API_KEY = os.getenv("GEMINI_API_KEY")
ADMIN_ID = os.getenv("ADMIN_ID")
PROVIDER_TOKEN = "" 

app = Flask(__name__)
ai_client = genai.Client(api_key=GOOGLE_API_KEY)
PRIMARY_CHANNEL = "@Axia_Tech"
ptb_app = Application.builder().token(TOKEN).build()

# ----------------------------------------------------------------
# إعداد خيط (Thread) مخصص لبيئة Asyncio لمنع مشكلة Event Loop Closed
# ----------------------------------------------------------------
bot_loop = asyncio.new_event_loop()

def start_background_loop(loop):
    asyncio.set_event_loop(loop)
    loop.run_forever()

Thread(target=start_background_loop, args=(bot_loop,), daemon=True).start()

# ----------------------------------------------------------------
# 2. قواعد البيانات والوظائف المساعدة
# ----------------------------------------------------------------
def db_request(method, table, params=None, json_data=None, custom_headers=None):
    url = f"{SUPABASE_URL}/rest/v1/{table}"
    headers = {
        "apikey": SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}",
        "Content-Type": "application/json",
        "Prefer": "return=representation"
    }
    if custom_headers:
        headers.update(custom_headers)

    try:
        if method == "GET":
            res = requests.get(url, headers=headers, params=params)
        elif method == "POST":
            res = requests.post(url, headers=headers, json=json_data)
        elif method == "PATCH":
            res = requests.patch(url, headers=headers, params=params, json=json_data)
        
        return res.json() if res.status_code in [200, 201] else []
    except Exception:
        return []

async def get_or_create_user(tg_user: User, context: ContextTypes.DEFAULT_TYPE, referrer_id=None):
    user_id = str(tg_user.id)
    data = db_request("GET", "users", params={"user_id": f"eq.{user_id}"})
    
    if not data:
        if referrer_id and referrer_id != user_id:
            ref_data = db_request("GET", "users", params={"user_id": f"eq.{referrer_id}"})
            if ref_data:
                current_points = ref_data[0]["points"]
                db_request("PATCH", "users", params={"user_id": f"eq.{referrer_id}"}, json_data={"points": current_points + 2})
                try:
                    await context.bot.send_message(chat_id=int(referrer_id), text="🎉 سجل مستخدم جديد عن طريق رابطك! تم إضافة نقطتين إلى رصيدك.")
                except Exception:
                    pass

        new_user = {
            "user_id": user_id,
            "username": tg_user.username or "",
            "points": 0,
            "last_daily_gift": "1970-01-01",
            "referred_by": referrer_id if referrer_id else None
        }
        db_request("POST", "users", json_data=new_user)
        
        new_limits = {
            "user_id": user_id,
            "last_reset": str(datetime.date.today()),
            "pdf_count": 0, "translate_count": 0, "voice_count": 0, "info_count": 0, "mind_count": 0
        }
        db_request("POST", "daily_limits", json_data=new_limits)
        
        data = db_request("GET", "users", params={"user_id": f"eq.{user_id}"})
    
    return data[0] if data else {"user_id": user_id, "points": 0, "last_daily_gift": "1970-01-01"}

def get_invited_count(user_id):
    res = requests.get(
        f"{SUPABASE_URL}/rest/v1/users", 
        headers={"apikey": SUPABASE_KEY, "Authorization": f"Bearer {SUPABASE_KEY}", "Prefer": "count=exact"},
        params={"referred_by": f"eq.{user_id}"}
    )
    count = res.headers.get("Content-Range", "0-0/0").split("/")[-1]
    return count

def check_and_reset_limits(user_id):
    today = str(datetime.date.today())
    data = db_request("GET", "daily_limits", params={"user_id": f"eq.{user_id}"})
    
    # تفادي الخطأ 'NoneType' بإنشاء السجل فوراً إن كان مفقوداً
    if not data:
        new_limits = {
            "user_id": user_id, "last_reset": today,
            "pdf_count": 0, "translate_count": 0, "voice_count": 0, "info_count": 0, "mind_count": 0
        }
        db_request("POST", "daily_limits", json_data=new_limits)
        return new_limits
    
    limit_row = data[0]
    if limit_row["last_reset"] != today:
        updated_limits = {
            "last_reset": today, "pdf_count": 0, "translate_count": 0, "voice_count": 0, "info_count": 0, "mind_count": 0
        }
        db_request("PATCH", "daily_limits", params={"user_id": f"eq.{user_id}"}, json_data=updated_limits)
        data = db_request("GET", "daily_limits", params={"user_id": f"eq.{user_id}"})
        return data[0]
    
    return limit_row

def get_dynamic_channel():
    data = db_request("GET", "settings", params={"key": "eq.dynamic_channel"})
    return data[0]["value"] if data else None

async def is_subscribed(bot, user_id):
    if ADMIN_ID and str(user_id) == str(ADMIN_ID):
        return True
        
    chk_channel = PRIMARY_CHANNEL if PRIMARY_CHANNEL.startswith("@") else f"@{PRIMARY_CHANNEL}"
    try:
        member = await bot.get_chat_member(chk_channel, int(user_id))
        if member.status in ['left', 'kicked']: 
            return False
    except Exception as e:
        if "Chat not found" in str(e) or "Not member" in str(e): return False

    dyn_channel = get_dynamic_channel()
    if dyn_channel:
        chk_dyn = dyn_channel if dyn_channel.startswith("@") else f"@{dyn_channel}"
        try:
            member = await bot.get_chat_member(chk_dyn, int(user_id))
            if member.status in ['left', 'kicked']: return False
        except Exception as e:
            if "Chat not found" in str(e) or "Not member" in str(e): return False
            
    return True

# ----------------------------------------------------------------
# 3. لوحات التحكم والقوائم (Inline Keyboards)
# ----------------------------------------------------------------
def services_menu_keyboard():
    keyboard = [
        [InlineKeyboardButton("📄 تلخيص PDF", callback_data="srv_pdf"), InlineKeyboardButton("🎙️ تفريغ صوتي", callback_data="srv_voice")],
        [InlineKeyboardButton("🌐 الترجمة الأكاديمية", callback_data="srv_translate")],
        [InlineKeyboardButton("📊 إنفوجرافيك", callback_data="srv_info"), InlineKeyboardButton("🧠 مخطط ذهني", callback_data="srv_mind")],
        [InlineKeyboardButton("👤 حسابي", callback_data="my_account")],
        [InlineKeyboardButton("🎁 الهدية اليومية", callback_data="daily_gift")]
    ]
    return InlineKeyboardMarkup(keyboard)

def account_keyboard():
    keyboard = [
        [InlineKeyboardButton("🔗 رابط الإحالة", callback_data="referral_link"), InlineKeyboardButton("⭐ شحن نقاط", callback_data="buy_stars")],
        [InlineKeyboardButton("🔙 العودة للقائمة الرئيسية", callback_data="main_menu")]
    ]
    return InlineKeyboardMarkup(keyboard)

# ----------------------------------------------------------------
# 4. معالجة الأوامر الأساسية
# ----------------------------------------------------------------
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    args = context.args
    referrer_id = args[0] if args else None
    
    await get_or_create_user(update.effective_user, context, referrer_id)
    context.user_data["awaiting_input"] = None
    
    if not await is_subscribed(context.bot, user_id):
        await send_subscription_requirement(update.effective_chat.id, context.bot)
        return

    await context.bot.send_message(
        chat_id=update.effective_chat.id,
        text="👋 *أهلاً بك في بوت الخدمات الذكي!*\n\nاختر الخدمة التي تريدها من الأزرار أدناه:",
        parse_mode="Markdown",
        reply_markup=services_menu_keyboard()
    )

async def send_subscription_requirement(chat_id, bot):
    dyn_channel = get_dynamic_channel()
    clean_primary = PRIMARY_CHANNEL.replace('@','')
    keyboard = [[InlineKeyboardButton("1️⃣ القناة الأساسية", url=f"https://t.me/{clean_primary}")]]
    
    if dyn_channel:
        clean_dyn = dyn_channel.replace('@','')
        keyboard.append([InlineKeyboardButton("2️⃣ القناة الإضافية", url=f"https://t.me/{clean_dyn}")])
        
    keyboard.append([InlineKeyboardButton("✅ تحقق من الاشتراك", callback_data="verify_sub")])
    markup = InlineKeyboardMarkup(keyboard)
    
    await bot.send_message(
        chat_id=chat_id, 
        text="⚠️ عذراً، يجب عليك الاشتراك في القنوات الرسمية للبوت أولاً لتتمكن من استخدامه.", 
        reply_markup=markup
    )

# ----------------------------------------------------------------
# 5. معالجة ضغطات الأزرار
# ----------------------------------------------------------------
async def handle_callbacks(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    user_id = str(query.from_user.id)
    chat_id = query.message.chat.id
    
    if query.data == "verify_sub":
        if await is_subscribed(context.bot, user_id):
            try: await query.message.delete()
            except: pass
            await context.bot.send_message(
                chat_id=chat_id, 
                text="✅ تم تأكيد الاشتراك بنجاح!\n\nاختر الخدمة المطلوبة:", 
                reply_markup=services_menu_keyboard()
            )
        else:
            await context.bot.answer_callback_query(callback_query_id=query.id, text="❌ لم تشترك في جميع القنوات بعد!", show_alert=True)

    # معالجة أزرار الخدمات
    elif query.data.startswith("srv_"):
        service = query.data.split("_")[1]
        context.user_data["awaiting_input"] = service
        
        msgs = {
            "pdf": "📄 الرجاء إرسال ملف الـ PDF الآن لتبدأ عملية التلخيص...",
            "voice": "🎙️ الرجاء إرسال الملف الصوتي أو الريكورد (المقطع الصوتي) الآن...",
            "translate": "🌐 الرجاء إرسال النص الذي تريد ترجمته الآن...",
            "info": "📊 الرجاء إرسال موضوع الإنفوجرافيك الذي تريده الآن...",
            "mind": "🧠 الرجاء إرسال الفكرة لإنشاء المخطط الذهني الآن..."
        }
        keyboard = [[InlineKeyboardButton("🔙 رجوع للقائمة", callback_data="main_menu")]]
        await query.edit_message_text(text=msgs[service], reply_markup=InlineKeyboardMarkup(keyboard))

    elif query.data == "my_account":
        context.user_data["awaiting_input"] = None
        user = await get_or_create_user(query.from_user, context)
        invited_count = get_invited_count(user_id)
        limits = check_and_reset_limits(user_id)
        
        display_name = query.from_user.full_name or query.from_user.username or "مستخدم"
        
        text = (
            f"👤 *حسابي*\n\n"
            f"👤 الإسم: *{display_name}*\n"
            f"🪙 رصيد النقاط: *{user['points']}*\n"
            f"👥 الأشخاص المدعوين: *{invited_count}*\n\n"
            f"📊 المجاني المتبقي اليوم:\n"
            f"• تلخيص: {3 - limits['pdf_count']}/3\n"
            f"• ترجمة: {3 - limits['translate_count']}/3\n"
            f"• صوت: {1 - limits['voice_count']}/1\n"
            f"• إنفوجرافيك: {2 - limits['info_count']}/2\n"
            f"• مخطط ذهني: {2 - limits['mind_count']}/2"
        )
        await query.edit_message_text(text=text, parse_mode="Markdown", reply_markup=account_keyboard())

    elif query.data == "main_menu":
        context.user_data["awaiting_input"] = None
        await query.edit_message_text(text="👋 اختر الخدمة التي تريدها من الأزرار أدناه:", reply_markup=services_menu_keyboard())

    elif query.data == "referral_link":
        link = f"https://t.me/{BOT_USERNAME}?start={user_id}"
        text = f"🔗 *رابطك:*\n\n`{link}`\n\nلكل صديق يسجل ستحصل على *2 نقاط*."
        keyboard = [[InlineKeyboardButton("🔙 العودة إلى حسابي", callback_data="my_account")]]
        await query.edit_message_text(text=text, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(keyboard))

    elif query.data == "daily_gift":
        user = await get_or_create_user(query.from_user, context)
        today_str = str(datetime.date.today())
        if user["last_daily_gift"] == today_str:
            await context.bot.answer_callback_query(callback_query_id=query.id, text="❌ حصلت على هديتك اليوم بالفعل!", show_alert=True)
        else:
            db_request("PATCH", "users", params={"user_id": f"eq.{user_id}"}, json_data={"points": user["points"] + 2, "last_daily_gift": today_str})
            await context.bot.answer_callback_query(callback_query_id=query.id, text="🎉 تم استلام 2 نقاط كمكافأة يومية!", show_alert=True)

# ----------------------------------------------------------------
# 6. محرك إدارة واستهلاك النقاط والتنفيذ
# ----------------------------------------------------------------
async def process_billing_and_run(update: Update, context: ContextTypes.DEFAULT_TYPE, service_key, free_limit, points_cost, worker_func, *args):
    user_id = str(update.effective_user.id)
    chat_id = update.effective_chat.id

    if not await is_subscribed(context.bot, user_id):
        await send_subscription_requirement(chat_id, context.bot)
        return

    user = await get_or_create_user(update.effective_user, context)
    limits = check_and_reset_limits(user_id)
    current_used = limits[service_key]
    
    if current_used < free_limit:
        msg = await context.bot.send_message(chat_id=chat_id, text="⏳ جاري المعالجة (مجاني)...")
        success = await worker_func(context.bot, chat_id, *args)
        if success:
            db_request("PATCH", "daily_limits", params={"user_id": f"eq.{user_id}"}, json_data={service_key: current_used + 1})
        await msg.delete()
    else:
        if user["points"] >= points_cost:
            msg = await context.bot.send_message(chat_id=chat_id, text=f"⏳ جاري المعالجة (خصم {points_cost} نقاط)...")
            success = await worker_func(context.bot, chat_id, *args)
            if success:
                db_request("PATCH", "users", params={"user_id": f"eq.{user_id}"}, json_data={"points": user["points"] - points_cost})
            await msg.delete()
        else:
            await context.bot.send_message(chat_id=chat_id, text=f"❌ رصيدك والمجاني نفد! تحتاج {points_cost} نقاط.")

# ----------------------------------------------------------------
# 7. دوال Google GenAI
# ----------------------------------------------------------------
async def run_pdf_summary(bot, chat_id, file_path):
    try:
        uploaded_file = ai_client.files.upload(file=file_path)
        response = ai_client.models.generate_content(model='gemini-1.5-flash', contents=[uploaded_file, "لخص هذا بدقة في نقاط رئيسية."])
        await bot.send_message(chat_id=chat_id, text=f"📋 *الملخص:*\n\n{response.text}", parse_mode="Markdown")
        return True
    except:
        await bot.send_message(chat_id=chat_id, text="❌ خطأ في معالجة الـ PDF.")
        return False
    finally:
        if os.path.exists(file_path): os.remove(file_path)

async def run_voice_transcription(bot, chat_id, file_path):
    try:
        uploaded_file = ai_client.files.upload(file=file_path)
        response = ai_client.models.generate_content(model='gemini-1.5-flash', contents=[uploaded_file, "حول الصوت إلى نص مكتوب بدقة."])
        await bot.send_message(chat_id=chat_id, text=f"🎙️ *التفريغ:*\n\n{response.text}", parse_mode="Markdown")
        return True
    except:
        await bot.send_message(chat_id=chat_id, text="❌ فشل تحويل الصوت.")
        return False
    finally:
        if os.path.exists(file_path): os.remove(file_path)

async def run_text_translation(bot, chat_id, text_content):
    try:
        response = ai_client.models.generate_content(model='gemini-1.5-flash', contents=[f"ترجم هذا أكاديمياً للعربية:\n{text_content}"])
        await bot.send_message(chat_id=chat_id, text=f"🌐 *الترجمة:*\n\n{response.text}", parse_mode="Markdown")
        return True
    except:
        await bot.send_message(chat_id=chat_id, text="❌ خطأ في الترجمة.")
        return False

async def run_generate_image(bot, chat_id, prompt_text, image_type):
    try:
        prompt = f"Professional {image_type} diagram about: {prompt_text}"
        res = ai_client.models.generate_images(model='imagen-3.0-generate-002', prompt=prompt, config=types.GenerateImagesConfig(number_of_images=1, output_mime_type="image/jpeg"))
        for img in res.generated_images:
            await bot.send_photo(chat_id=chat_id, photo=io.BytesIO(img.image.image_bytes))
        return True
    except:
        await bot.send_message(chat_id=chat_id, text="❌ فشل توليد الصورة.")
        return False

# ----------------------------------------------------------------
# 8. استلام الرسائل بناءً على حالة الزر المضغوط
# ----------------------------------------------------------------
async def handle_docs(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message.document.mime_type == 'application/pdf':
        file = await context.bot.get_file(update.message.document.file_id)
        file_path = f"temp_{update.message.document.file_name}"
        await file.download_to_drive(file_path)
        await process_billing_and_run(update, context, "pdf_count", 3, 3, run_pdf_summary, file_path)
        context.user_data["awaiting_input"] = None # تفريغ الحالة

async def handle_audio(update: Update, context: ContextTypes.DEFAULT_TYPE):
    file_id = update.message.voice.file_id if update.message.voice else update.message.audio.file_id
    file = await context.bot.get_file(file_id)
    file_path = f"temp_{file_id}.ogg"
    await file.download_to_drive(file_path)
    await process_billing_and_run(update, context, "voice_count", 1, 2, run_voice_transcription, file_path)
    context.user_data["awaiting_input"] = None

async def handle_text_requests(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    awaiting = context.user_data.get("awaiting_input")

    if awaiting == "translate":
        await process_billing_and_run(update, context, "translate_count", 3, 2, run_text_translation, text)
        context.user_data["awaiting_input"] = None
    elif awaiting == "info":
        await process_billing_and_run(update, context, "info_count", 2, 5, run_generate_image, text, "infographic")
        context.user_data["awaiting_input"] = None
    elif awaiting == "mind":
        await process_billing_and_run(update, context, "mind_count", 2, 5, run_generate_image, text, "mindmap")
        context.user_data["awaiting_input"] = None
    else:
        # إذا أرسل المستخدم رسالة بدون الضغط على زر الخدمة أولاً
        await update.message.reply_text("👇 الرجاء اختيار الخدمة المطلوبة من القائمة أولاً:", reply_markup=services_menu_keyboard())

# تسجيل الأحداث
ptb_app.add_handler(CommandHandler("start", cmd_start))
ptb_app.add_handler(CallbackQueryHandler(handle_callbacks))
ptb_app.add_handler(MessageHandler(filters.Document.PDF, handle_docs))
ptb_app.add_handler(MessageHandler(filters.VOICE | filters.AUDIO, handle_audio))
ptb_app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text_requests))

# ----------------------------------------------------------------
# 9. إعدادات الـ Webhook و Flask الآمنة (بدون مشكلة الـ Loop)
# ----------------------------------------------------------------
def setup_webhook_sync():
    # تشغيل دوال الـ async المطلوبة داخل الـ Loop الذي تم إنشاؤه في الخلفية
    future1 = asyncio.run_coroutine_threadsafe(ptb_app.initialize(), bot_loop)
    future1.result()
    future2 = asyncio.run_coroutine_threadsafe(ptb_app.start(), bot_loop)
    future2.result()
    
    render_url = os.getenv("RENDER_EXTERNAL_URL")
    if render_url:
        future3 = asyncio.run_coroutine_threadsafe(ptb_app.bot.set_webhook(url=f"{render_url}/{TOKEN}"), bot_loop)
        future3.result()

# ننفذ التهيئة قبل تشغيل السيرفر
setup_webhook_sync()

@app.route(f'/{TOKEN}', methods=['POST'])
def getMessage():
    if request.method == "POST":
        update = Update.de_json(request.get_json(force=True), ptb_app.bot)
        # نرسل التحديث للـ Loop الذي يعمل باستمرار بالخلفية بدلاً من خلق وإغلاق Loop جديد
        asyncio.run_coroutine_threadsafe(ptb_app.process_update(update), bot_loop)
        return "OK", 200

@app.route("/")
def webhook():
    return "Bot Core Status: ALIVE & MONITORING", 200

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get('PORT', 5000)))
