import os
import datetime
import io
import asyncio
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

# تهيئة عميل Google GenAI SDK
ai_client = genai.Client(api_key=GOOGLE_API_KEY)

PRIMARY_CHANNEL = "@Axia_Tech"

# تهيئة تطبيق البوت
ptb_app = Application.builder().token(TOKEN).build()

# ----------------------------------------------------------------
# 2. محرك الاتصال المباشر بـ Supabase REST API
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
                # إضافة نقطتين للشخص الذي قام بالدعوة وتخزين المعرف في حقل مخصص للاحالات إن وجد
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
            "referred_by": referrer_id if referrer_id else None  # حفظ من قام بدعوته لحساب الإحصائيات بدقة
        }
        db_request("POST", "users", json_data=new_user)
        
        new_limits = {
            "user_id": user_id,
            "last_reset": str(datetime.date.today()),
            "pdf_count": 0,
            "translate_count": 0,
            "voice_count": 0,
            "info_count": 0,
            "mind_count": 0
        }
        db_request("POST", "daily_limits", json_data=new_limits)
        
        data = db_request("GET", "users", params={"user_id": f"eq.{user_id}"})
    
    return data[0] if data else {"user_id": user_id, "points": 0, "last_daily_gift": "1970-01-01"}

def get_invited_count(user_id):
    # جلب عدد الأشخاص الذين يمتلكون حقل referred_by يساوي المعرف الحالي لحساب الإحالات الحقيقية
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
    if not data:
        return None
    
    limit_row = data[0]
    if limit_row["last_reset"] != today:
        updated_limits = {
            "last_reset": today,
            "pdf_count": 0,
            "translate_count": 0,
            "voice_count": 0,
            "info_count": 0,
            "mind_count": 0
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
        print(f"⚠️ Error checking primary channel sub: {e}")
        if "Chat not found" in str(e) or "Not member" in str(e):
            return False

    dyn_channel = get_dynamic_channel()
    if dyn_channel:
        chk_dyn = dyn_channel if dyn_channel.startswith("@") else f"@{dyn_channel}"
        try:
            member = await bot.get_chat_member(chk_dyn, int(user_id))
            if member.status in ['left', 'kicked']: 
                return False
        except Exception as e:
            print(f"⚠️ Error checking dynamic channel sub: {e}")
            if "Chat not found" in str(e) or "Not member" in str(e):
                return False
            
    return True

# ----------------------------------------------------------------
# 3. لوحات التحكم والقوائم (Inline Keyboards)
# ----------------------------------------------------------------
def main_menu_keyboard():
    keyboard = [
        [InlineKeyboardButton("👤 حسابي", callback_data="my_account")],
        [InlineKeyboardButton("🎁 الهدية اليومية", callback_data="daily_gift")]
    ]
    return InlineKeyboardMarkup(keyboard)

def account_keyboard():
    keyboard = [
        [
            InlineKeyboardButton("🔗 رابط الإحالة", callback_data="referral_link"),
            InlineKeyboardButton("⭐ شحن نقاط (نجوم)", callback_data="buy_stars")
        ],
        [InlineKeyboardButton("🔙 العودة للقائمة الرئيسية", callback_data="main_menu")]
    ]
    return InlineKeyboardMarkup(keyboard)

def admin_keyboard():
    keyboard = [
        [InlineKeyboardButton("📢 إرسال إذاعة (Broadcast)", callback_data="admin_broadcast")],
        [InlineKeyboardButton("➕ تغيير القناة الإجبارية الإضافية", callback_data="admin_set_channel")],
        [InlineKeyboardButton("📊 إحصائيات النظام", callback_data="admin_stats")]
    ]
    return InlineKeyboardMarkup(keyboard)

# ----------------------------------------------------------------
# 4. معالجة الأوامر الأساسية
# ----------------------------------------------------------------
def get_services_text():
    return (
        "👋 *أهلاً بك في بوت معالجة وتلخيص المحاضرات الذكي!*\n\n"
        "💡 *قائمة الخدمات المتاحة وكيفية استخدامها:*\n"
        "• 📄 *تلخيص ملفات الـ PDF:* أرسل أي ملف محاضرة بصيغة PDF مباشرة ليقوم البوت بتلخيصه بذكاء.\n"
        "• 🎙️ *التفريغ الصوتي الذكي:* أرسل أي ملف صوتي أو ريكورد ليقوم البوت بتحويله إلى نص مكتوب.\n"
        "• 🌐 *الترجمة الأكاديمية:* أرسل كلمة (ترجم) متبوعة بنصك لترجمته فوراً، مثال: `ترجم Hello world`\n"
        "• 📊 *إنفوجرافيك ذكي:* أرسل كلمة (انفوجرافيك) متبوعة بالموضوع لتوليد رسم بياني وتوضيحي.\n"
        "• 🧠 *المخططات الذهنية:* أرسل كلمة (مخطط ذهني) متبوعة بالفكرة لتوليد صورة خارطة مفاهيم متكاملة.\n\n"
        "👇 استخدم الأزرار أدناه للتحكم بحسابك واستلام جوائزك اليومية:"
    )

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    args = context.args
    referrer_id = args[0] if args else None
    
    await get_or_create_user(update.effective_user, context, referrer_id)
    
    if not await is_subscribed(context.bot, user_id):
        await send_subscription_requirement(update.effective_chat.id, context.bot)
        return

    await context.bot.send_message(
        chat_id=update.effective_chat.id,
        text=get_services_text(),
        parse_mode="Markdown",
        reply_markup=main_menu_keyboard()
    )

async def cmd_admin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if str(update.effective_user.id) != str(ADMIN_ID): return
    await context.bot.send_message(
        chat_id=update.effective_chat.id, 
        text="🛠️ لوحة تحكم الإدارة الصارمة:", 
        reply_markup=admin_keyboard()
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
# 5. معالجة ضغطات الأزرار (Callback Queries)
# ----------------------------------------------------------------
async def handle_callbacks(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    user_id = str(query.from_user.id)
    chat_id = query.message.chat.id
    
    if query.data == "verify_sub":
        if await is_subscribed(context.bot, user_id):
            try:
                await query.message.delete()
            except Exception:
                pass
            await context.bot.send_message(chat_id=chat_id, text=get_services_text(), parse_mode="Markdown", reply_markup=main_menu_keyboard())
        else:
            await context.bot.answer_callback_query(callback_query_id=query.id, text="❌ لم تشترك في جميع القنوات بعد! تأكد من انضمامك ثم اضغط مجدداً.", show_alert=True)

    elif query.data == "my_account":
        user = await get_or_create_user(query.from_user, context)
        invited_count = get_invited_count(user_id)
        limits = check_and_reset_limits(user_id)
        
        display_name = query.from_user.full_name or query.from_user.username or "مستخدم غامض"
        
        text = (
            f"👤 *تفاصيل حسابي الأساسية*\n\n"
            f"👤 الإسم: *{display_name}*\n"
            f"🆔 معرف الحساب: `{user_id}`\n"
            f"🪙 رصيد النقاط: *{user['points']}* نقطة\n"
            f"👥 عدد الأشخاص المدعوين: *{invited_count}* شخص\n\n"
            f"📊 المحاولات المجانية المتبقية اليوم:\n"
            f"📄 تلخيص PDF: {3 - limits['pdf_count']}/3\n"
            f"🌐 الترجمة الأكاديمية: {3 - limits['translate_count']}/3\n"
            f"🎙️ تفريغ الصوت: {1 - limits['voice_count']}/1\n"
            f"📊 إنفوجرافيك: {2 - limits['info_count']}/2\n"
            f"🧠 مخطط ذهني: {2 - limits['mind_count']}/2"
        )
        await query.edit_message_text(text=text, parse_mode="Markdown", reply_markup=account_keyboard())

    elif query.data == "main_menu":
        await query.edit_message_text(text=get_services_text(), parse_mode="Markdown", reply_markup=main_menu_keyboard())

    elif query.data == "referral_link":
        link = f"https://t.me/{BOT_USERNAME}?start={user_id}"
        text = f"🔗 *رابط الإحالة الخاص بك:*\n\n`{link}`\n\nقم بنشره! لكل صديق يسجل في البوت من خلال رابطك، ستحصل تلقائياً على *2 نقاط* إضافية مجاناً."
        keyboard = [[InlineKeyboardButton("🔙 العودة إلى حسابي", callback_data="my_account")]]
        await query.edit_message_text(text=text, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(keyboard))

    elif query.data == "daily_gift":
        user = await get_or_create_user(query.from_user, context)
        today_str = str(datetime.date.today())
        
        if user["last_daily_gift"] == today_str:
            await context.bot.answer_callback_query(callback_query_id=query.id, text="❌ لقد حصلت على هديتك اليومية بالفعل! عد غداً لمكافأة جديدة.", show_alert=True)
        else:
            db_request("PATCH", "users", params={"user_id": f"eq.{user_id}"}, json_data={"points": user["points"] + 2, "last_daily_gift": today_str})
            await context.bot.answer_callback_query(callback_query_id=query.id, text="🎉 مبروك! تم استلام 2 نقاط بنجاح كمكافأة يومية لحسابك.", show_alert=True)

    elif query.data == "buy_stars":
        prices = [LabeledPrice(label="شحن رصيد نقاط", amount=1)]
        await context.bot.send_invoice(
            chat_id=chat_id, 
            title="شحن نقاط", 
            description="1 نجمة تلجرام تعادل 3 نقاط داخل البوت.", 
            provider_token=PROVIDER_TOKEN, 
            currency="XTR", 
            prices=prices, 
            start_parameter="buy-points", 
            payload=f"user_upgrade_{user_id}"
        )

    elif query.data == "admin_stats":
        if str(query.from_user.id) != str(ADMIN_ID): return
        res = requests.get(f"{SUPABASE_URL}/rest/v1/users", headers={"apikey": SUPABASE_KEY, "Authorization": f"Bearer {SUPABASE_KEY}", "Prefer": "count=exact"})
        count = res.headers.get("Content-Range", "0-0/0").split("/")[-1]
        await context.bot.send_message(chat_id=chat_id, text=f"📊 إجمالي عدد المستخدمين المسجلين: {count}")

    elif query.data in ["admin_set_channel", "admin_broadcast"]:
        if str(query.from_user.id) != str(ADMIN_ID): return
        context.user_data["admin_action"] = query.data
        msg = "قم بإرسال معرف القناة الجديد الآن مع الـ @:" if query.data == "admin_set_channel" else "أرسل نص الرسالة التي تريد بثها:"
        await context.bot.send_message(chat_id=chat_id, text=msg)

# ----------------------------------------------------------------
# 6. وظائف الإدارة
# ----------------------------------------------------------------
async def handle_admin_replies(update: Update, context: ContextTypes.DEFAULT_TYPE):
    action = context.user_data.get("admin_action")
    if not action or str(update.effective_user.id) != str(ADMIN_ID):
        return
        
    text = update.message.text.strip()
    
    if action == "admin_set_channel":
        if text.startswith("@"):
            db_request("POST", "settings", json_data={"key": "dynamic_channel", "value": text}, custom_headers={"Prefer": "resolution=merge-duplicates"})
            await update.message.reply_text(f"✅ تم تحديث القناة الإضافية إلى: {text}")
        else:
            await update.message.reply_text("❌ خطأ في الصيغة. يجب أن تبدأ بـ @.")
            
    elif action == "admin_broadcast":
        users = db_request("GET", "users")
        success, fail = 0, 0
        for u in users:
            try:
                await context.bot.send_message(chat_id=int(u["user_id"]), text=text)
                success += 1
            except Exception:
                fail += 1
        await update.message.reply_text(f"📢 اكتملت الإذاعة.\n✅ بنجاح: {success}\n❌ فشل: {fail}")
        
    context.user_data["admin_action"] = None

# ----------------------------------------------------------------
# 7. محرك إدارة واستهلاك النقاط
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
        await context.bot.send_message(chat_id=chat_id, text="⏳ جاري المعالجة ضمن المحاولات المجانية اليومية...")
        if await worker_func(context.bot, chat_id, *args):
            db_request("PATCH", "daily_limits", params={"user_id": f"eq.{user_id}"}, json_data={service_key: current_used + 1})
    else:
        if user["points"] >= points_cost:
            await context.bot.send_message(chat_id=chat_id, text=f"⏳ جاري المعالجة وخصم {points_cost} نقاط...")
            if await worker_func(context.bot, chat_id, *args):
                db_request("PATCH", "users", params={"user_id": f"eq.{user_id}"}, json_data={"points": user["points"] - points_cost})
        else:
            await context.bot.send_message(chat_id=chat_id, text=f"❌ نفدت محاولاتك المجانية اليومية لهذا القسم. تحتاج إلى {points_cost} نقاط لإتمام العملية.")

# ----------------------------------------------------------------
# 8. التكامل مع خدمات Google GenAI SDK
# ----------------------------------------------------------------
async def run_pdf_summary(bot, chat_id, file_path):
    try:
        uploaded_file = ai_client.files.upload(file=file_path)
        response = ai_client.models.generate_content(
            model='gemini-1.5-flash',
            contents=[uploaded_file, "قم بتلخيص هذه المحاضرة بدقة تامة وبصيغة نقاط رئيسية منظمة."]
        )
        await bot.send_message(chat_id=chat_id, text=f"📋 *ملخص المحاضرة:*\n\n{response.text}", parse_mode="Markdown")
        return True
    except Exception:
        await bot.send_message(chat_id=chat_id, text="❌ حدث خطأ أثناء معالجة ملف PDF.")
        return False
    finally:
        if os.path.exists(file_path): os.remove(file_path)

async def run_voice_transcription(bot, chat_id, file_path):
    try:
        uploaded_file = ai_client.files.upload(file=file_path)
        response = ai_client.models.generate_content(
            model='gemini-1.5-flash',
            contents=[uploaded_file, "قم بتحويل هذا الصوت إلى نص مكتوب بدقة."]
        )
        await bot.send_message(chat_id=chat_id, text=f"🎙️ *النص المفرغ من الصوت:*\n\n{response.text}", parse_mode="Markdown")
        return True
    except Exception:
        await bot.send_message(chat_id=chat_id, text="❌ فشل تحويل الملف الصوتي إلى نص.")
        return False
    finally:
        if os.path.exists(file_path): os.remove(file_path)

async def run_text_translation(bot, chat_id, text_content):
    try:
        response = ai_client.models.generate_content(
            model='gemini-1.5-flash',
            contents=[f"ترجم النص التالي إلى اللغة العربية ترجمة احترافية أكاديمية:\n\n{text_content}"]
        )
        await bot.send_message(chat_id=chat_id, text=f"🌐 *الترجمة الاحترافية:*\n\n{response.text}", parse_mode="Markdown")
        return True
    except Exception:
        await bot.send_message(chat_id=chat_id, text="❌ حدث خطأ أثناء معالجة الترجمة.")
        return False

async def run_generate_image(bot, chat_id, prompt_text, image_type):
    try:
        refined_prompt = f"Professional {image_type}, highly detailed, clean diagram design about: {prompt_text}"
        result = ai_client.models.generate_images(
            model='imagen-3.0-generate-002',
            prompt=refined_prompt,
            config=types.GenerateImagesConfig(number_of_images=1, output_mime_type="image/jpeg")
        )
        for generated_image in result.generated_images:
            image_bytes = io.BytesIO(generated_image.image.image_bytes)
            await bot.send_photo(chat_id=chat_id, photo=image_bytes, caption=f"✅ تم توليد الـ {image_type} بنجاح.")
        return True
    except Exception:
        await bot.send_message(chat_id=chat_id, text="❌ حدث خطأ أثناء توليد الصورة الذكية.")
        return False

# ----------------------------------------------------------------
# 9. معالجة الرسائل الواردة
# ----------------------------------------------------------------
async def handle_docs(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message.document.mime_type == 'application/pdf':
        file = await context.bot.get_file(update.message.document.file_id)
        file_path = f"temp_{update.message.document.file_name}"
        await file.download_to_drive(file_path)
        
        await process_billing_and_run(update, context, "pdf_count", 3, 3, run_pdf_summary, file_path)

async def handle_audio(update: Update, context: ContextTypes.DEFAULT_TYPE):
    file_id = update.message.voice.file_id if update.message.voice else update.message.audio.file_id
    file = await context.bot.get_file(file_id)
    file_path = f"temp_{file_id}.ogg"
    await file.download_to_drive(file_path)
        
    await process_billing_and_run(update, context, "voice_count", 1, 2, run_voice_transcription, file_path)

async def handle_text_requests(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if context.user_data.get("admin_action"):
        await handle_admin_replies(update, context)
        return

    text = update.message.text.strip()
    
    if text.startswith("ترجم "):
        target_text = text.replace("ترجم ", "", 1)
        await process_billing_and_run(update, context, "translate_count", 3, 2, run_text_translation, target_text)
    elif text.startswith("انفوجرافيك "):
        prompt = text.replace("انفوجرافيك ", "", 1)
        await process_billing_and_run(update, context, "info_count", 2, 5, run_generate_image, prompt, "infographic")
    elif text.startswith("مخطط ذهني "):
        prompt = text.replace("مخطط ذهني ", "", 1)
        await process_billing_and_run(update, context, "mind_count", 2, 5, run_generate_image, prompt, "mindmap")
    else:
        await update.message.reply_text(
            "ℹ️ الصيغ المدعومة والمباشرة للخدمات:\n• `ترجم [النص]`\n• `انفوجرافيك [الموضوع]`\n• `مخطط ذهني [الموضوع]`\n• أو قم بإرسال ملف PDF أو تسجيل صوتي فوراً لتبدأ المعالجة."
        )

# ----------------------------------------------------------------
# 10. معالجة شحن النجوم التلقائي
# ----------------------------------------------------------------
async def checkout(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.pre_checkout_query
    await query.answer(ok=True)

async def got_payment(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    payload = update.message.successful_payment.invoice_payload
    
    if "user_upgrade_" in payload:
        user = await get_or_create_user(update.effective_user, context)
        stars_received = update.message.successful_payment.total_amount
        added_points = int(stars_received) * 3
        
        db_request("PATCH", "users", params={"user_id": f"eq.{user_id}"}, json_data={"points": user["points"] + added_points})
        await update.message.reply_text(f"🎉 تم شحن حسابك بـ {added_points} نقاط بنجاح.")

# تسجيل معالجات أحداث البوت
ptb_app.add_handler(CommandHandler("start", cmd_start))
ptb_app.add_handler(CommandHandler("admin", cmd_admin))
ptb_app.add_handler(CallbackQueryHandler(handle_callbacks))
ptb_app.add_handler(PreCheckoutQueryHandler(checkout))
ptb_app.add_handler(MessageHandler(filters.SUCCESSFUL_PAYMENT, got_payment))
ptb_app.add_handler(MessageHandler(filters.Document.PDF, handle_docs))
ptb_app.add_handler(MessageHandler(filters.VOICE | filters.AUDIO, handle_audio))
ptb_app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text_requests))

# ----------------------------------------------------------------
# 11. إعدادات خادم Webhook و Flask وتوافق الـ Cron Job المنفصل
# ----------------------------------------------------------------

async def init_bot_and_webhook():
    await ptb_app.initialize()
    render_url = os.getenv("RENDER_EXTERNAL_URL")
    if render_url:
        print(f"🔗 Setting webhook to: {render_url}/{TOKEN}")
        await ptb_app.bot.set_webhook(url=f"{render_url}/{TOKEN}")
    else:
        print("⚠️ RENDER_EXTERNAL_URL not found, skipping webhook setup.")

# تنفيذ التهيئة والربط في بيئة عمل موحدة ومستقرة قبل تشغيل Flask
asyncio.run(init_bot_and_webhook())

@app.route('/' + TOKEN, methods=['POST'])
def getMessage():
    if request.method == "POST":
        update = Update.de_json(request.get_json(force=True), ptb_app.bot)
        asyncio.run(ptb_app.process_update(update))
        return "!", 200

@app.route("/")
def webhook():
    return "Bot Core Status: ALIVE & MONITORING", 200

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get('PORT', 5000)))
