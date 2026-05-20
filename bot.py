import os
import datetime
from flask import Flask, request, abort
import telebot
from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton, LabeledPrice
from google import genai
from google.genai import types
from supabase import create_client, Client

# ----------------------------------------------------------------
# 1. الإعدادات والمتغيرات البيئية
# ----------------------------------------------------------------
TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
BOT_USERNAME = os.getenv("TELEGRAM_BOT_USERNAME")  # بدون @ (مثال: MyLecturesBot)
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")
GOOGLE_API_KEY = os.getenv("GEMINI_API_KEY")
PROVIDER_TOKEN = "" # يترك فارغاً عند استخدام نجوم تيليجرام (Telegram Stars)

bot = telebot.TeleBot(TOKEN, parse_mode="Markdown")
app = Flask(__name__)

# تهيئة عملاء الخدمات الخارجية بناءً على توثيقات 2026
supabase_client: Client = create_client(SUPABASE_URL, SUPABASE_KEY)
ai_client = genai.Client(api_key=GOOGLE_API_KEY)

# القناة الأساسية الثابتة
PRIMARY_CHANNEL = "@Axia_Tech"

# ----------------------------------------------------------------
# 2. دالات المساعدة وإدارة قاعدة البيانات (Supabase)
# ----------------------------------------------------------------
def get_or_create_user(tg_user, referrer_id=None):
    user_id = str(tg_user.id)
    res = supabase_client.table("users").select("*").eq("user_id", user_id).execute()
    
    if not res.data:
        # إذا كان مستخدماً جديداً وتمت دعوته عبر رابط إحالة
        if referrer_id and referrer_id != user_id:
            # التحقق من وجود الحساب الداعي
            ref_res = supabase_client.table("users").select("*").eq("user_id", referrer_id).execute()
            if ref_res.data:
                # إضافة نقطتين للحساب الداعي
                current_points = ref_res.data[0]["points"]
                supabase_client.table("users").update({"points": current_points + 2}).eq("user_id", referrer_id).execute()
                try:
                    bot.send_message(referrer_id, f"🎉 سجل مستخدم جديد عن طريق رابطك! تم إضافة نقطتين إلى رصيدك.")
                except Exception:
                    pass

        # إنشاء سجل الحساب الجديد
        data = {
            "user_id": user_id,
            "username": tg_user.username or "",
            "points": 0,
            "last_daily_gift": "1970-01-01"
        }
        supabase_client.table("users").insert(data).execute()
        
        # إنشاء سجل القيود اليومية المجانية للحساب الجديد
        limits_data = {
            "user_id": user_id,
            "last_reset": str(datetime.date.today()),
            "pdf_count": 0,
            "translate_count": 0,
            "voice_count": 0,
            "info_count": 0,
            "mind_count": 0
        }
        supabase_client.table("daily_limits").insert(limits_data).execute()
        
        res = supabase_client.table("users").select("*").eq("user_id", user_id).execute()
    
    return res.data[0]

def check_and_reset_limits(user_id):
    today = str(datetime.date.today())
    res = supabase_client.table("daily_limits").select("*").eq("user_id", user_id).execute()
    if not res.data:
        return None
    
    limit_row = res.data[0]
    if limit_row["last_reset"] != today:
        # تصفير العدادات ليوم جديد
        supabase_client.table("daily_limits").update({
            "last_reset": today,
            "pdf_count": 0,
            "translate_count": 0,
            "voice_count": 0,
            "info_count": 0,
            "mind_count": 0
        }).eq("user_id", user_id).execute()
        res = supabase_client.table("daily_limits").select("*").eq("user_id", user_id).execute()
        return res.data[0]
    
    return limit_row

def get_dynamic_channel():
    res = supabase_client.table("settings").select("value").eq("key", "dynamic_channel").execute()
    if res.data:
        return res.data[0]["value"]
    return None

def is_subscribed(user_id):
    # 1. التحقق من القناة الأساسية
    try:
        member = bot.get_chat_member(PRIMARY_CHANNEL, user_id)
        if member.status in ['left', 'kicked']:
            return False
    except Exception:
        return False
    
    # 2. التحقق من القناة الديناميكية المضافة من لوحة التحكم (إن وجدت)
    dyn_channel = get_dynamic_channel()
    if dyn_channel:
        try:
            member = bot.get_chat_member(dyn_channel, user_id)
            if member.status in ['left', 'kicked']:
                return False
        except Exception:
            return False
            
    return True

# ----------------------------------------------------------------
# 3. لوحات التحكم والقوائم (Inline Keyboards)
# ----------------------------------------------------------------
def main_menu_keyboard():
    markup = InlineKeyboardMarkup()
    markup.row(InlineKeyboardButton("⚙️ لوحة الإعدادات", callback_data="user_settings"))
    markup.row(
        InlineKeyboardButton("🎁 الهدية اليومية", callback_data="daily_gift"),
        InlineKeyboardButton("🔗 رابط الإحالة", callback_data="referral_link")
    )
    markup.row(InlineKeyboardButton("⭐ شحن نقاط (نجوم)", callback_data="buy_stars"))
    return markup

def admin_keyboard():
    markup = InlineKeyboardMarkup()
    markup.row(InlineKeyboardButton("📢 إرسال إذاعة (Broadcast)", callback_data="admin_broadcast"))
    markup.row(InlineKeyboardButton("➕ تغيير القناة الإجبارية الإضافية", callback_data="admin_set_channel"))
    markup.row(InlineKeyboardButton("📊 إحصائيات النظام", callback_data="admin_stats"))
    return markup

# ----------------------------------------------------------------
# 4. معالجة الأوامر الأساسية
# ----------------------------------------------------------------
@bot.message_with_type_handler(commands=['start'])
def cmd_start(message):
    user_id = str(message.from_user.id)
    
    # التحقق من وجود كود الإحالة
    args = message.text.split()
    referrer_id = args[1] if len(args) > 1 else None
    
    get_or_create_user(message.from_user, referrer_id)
    
    if not is_subscribed(user_id):
        send_subscription_requirement(message.chat.id)
        return

    bot.send_message(
        message.chat.id,
        "👋 أهلاً بك في بوت معالجة وتلخيص المحاضرات الذكي.\n\n"
        "قم بإرسال الملفات أو التسجيلات الصوتية مباشرة، أو استخدم القائمة أدناه لإدارة حسابك:",
        reply_markup=main_menu_keyboard()
    )

@bot.message_with_type_handler(commands=['admin'])
def cmd_admin(message):
    # مراجعة معرف الأدمن عبر المتغيرات البيئية
    admin_id = os.getenv("ADMIN_ID")
    if str(message.from_user.id) != str(admin_id):
        return
    bot.send_message(message.chat.id, "🛠️ لوحة تحكم الإدارة الصارمة:", reply_markup=admin_keyboard())

def send_subscription_requirement(chat_id):
    markup = InlineKeyboardMarkup()
    dyn_channel = get_dynamic_channel()
    
    markup.row(InlineKeyboardButton("1️⃣ القناة الأساسية", url=f"https://t.me/{PRIMARY_CHANNEL.replace('@','')}" ))
    if dyn_channel:
        markup.row(InlineKeyboardButton("2️⃣ القناة الإضافية", url=f"https://t.me/{dyn_channel.replace('@','')}"))
        
    markup.row(InlineKeyboardButton("✅ تحقق من الاشتراك", callback_data="verify_sub"))
    
    bot.send_message(
        chat_id,
        "⚠️ عذراً، يجب عليك الاشتراك في القنوات الرسمية للبوت أولاً لتتمكن من استخدامه.",
        reply_markup=markup
    )

# ----------------------------------------------------------------
# 5. معالجة ضغطات الأزرار (Callback Queries)
# ----------------------------------------------------------------
@bot.callback_query_with_type_handler(func=lambda call: True)
def handle_callbacks(call):
    user_id = str(call.from_user.id)
    chat_id = call.message.chat.id
    
    if call.data == "verify_sub":
        if is_subscribed(user_id):
            bot.delete_message(chat_id, call.message.message_id)
            bot.send_message(chat_id, "✅ تم تأكيد الاشتراك بنجاح. يمكنك الآن استخدام كافة وظائف البوت.", reply_markup=main_menu_keyboard())
        else:
            bot.answer_callback_query(call.id, "❌ لم تشترك في جميع القنوات بعد!", show_alert=True)

    elif call.data == "user_settings":
        user = get_or_create_user(call.from_user)
        limits = check_and_reset_limits(user_id)
        
        text = (
            f"👤 *لوحة إعدادات المستخدم*\n\n"
            f"🆔 معرف الحساب: `{user_id}`\n"
            f"🪙 رصيد النقاط المدفوعة: *{user['points']}* نقطة\n\n"
            f"📊 الاستهلاك المجاني المتبقي اليوم:\n"
            f"📄 تلخيص PDF: {3 - limits['pdf_count']}/3\n"
            f"🌐 الترجمة: {3 - limits['translate_count']}/3\n"
            f"🎙️ تفريغ الصوت: {1 - limits['voice_count']}/1\n"
            f"📊 إنفوجرافيك: {2 - limits['info_count']}/2\n"
            f"🧠 مخطط ذهني: {2 - limits['mind_count']}/2"
        )
        markup = InlineKeyboardMarkup()
        markup.row(InlineKeyboardButton("🔙 العودة للقائمة الرئيسية", callback_data="main_menu"))
        bot.edit_message_text(text, chat_id, call.message.message_id, reply_markup=markup)

    elif call.data == "main_menu":
        bot.edit_message_text("👋 قائمة البوت الرئيسية لمساعدتك في تلخيص المحاضرات:", chat_id, call.message.message_id, reply_markup=main_menu_keyboard())

    elif call.data == "referral_link":
        link = f"https://t.me/{BOT_USERNAME}?start={user_id}"
        text = f"🔗 *رابط الإحالة الخاص بك:*\n\n`{link}`\n\n قم بنشر الرابط، لكل صديق يقوم بالدخول والاشتراك ستحصل على *2 نقاط* بشكل تلقائي."
        markup = InlineKeyboardMarkup()
        markup.row(InlineKeyboardButton("🔙 العودة", callback_data="main_menu"))
        bot.edit_message_text(text, chat_id, call.message.message_id, reply_markup=markup)

    elif call.data == "daily_gift":
        user = get_or_create_user(call.from_user)
        today_str = str(datetime.date.today())
        
        if user["last_daily_gift"] == today_str:
            bot.answer_callback_query(call.id, "❌ لقد حصلت على هديتك اليومية بالفعل! عد غداً.", show_alert=True)
        else:
            new_points = user["points"] + 2
            supabase_client.table("users").update({"points": new_points, "last_daily_gift": today_str}).eq("user_id", user_id).execute()
            bot.answer_callback_query(call.id, "🎉 تم استلام 2 نقاط بنجاح كمكافأة يومية!", show_alert=True)

    elif call.data == "buy_stars":
        # إرسال فاتورة دفع بالنجوم للمستخدم
        prices = [LabeledPrice(label="شحن رصيد نقاط", amount=1)] # 1 نجمة
        bot.send_invoice(
            chat_id,
            title="شحن نقاط البوت الذكي",
            description="شراء نقاط لاستخدام الخدمات بعد انتهاء الحد المجاني اليومي. 1 نجمة = 3 نقاط.",
            provider_token=PROVIDER_TOKEN,
            currency="XTR", # الكود الرسمي لنجوم تيليجرام
            prices=prices,
            start_parameter="buy-points",
            payload=f"user_upgrade_{user_id}"
        )
        bot.answer_callback_query(call.id)

    # أقسام لوحة تحكم الإدارة
    elif call.data == "admin_stats":
        if str(call.from_user.id) != os.getenv("ADMIN_ID"): return
        total_users = supabase_client.table("users").select("*", count="exact").execute().count
        bot.send_message(chat_id, f"📊 إجمالي عدد المستخدمين المسجلين: {total_users}")
        bot.answer_callback_query(call.id)

    elif call.data == "admin_set_channel":
        if str(call.from_user.id) != os.getenv("ADMIN_ID"): return
        msg = bot.send_message(chat_id, "قم بإرسال معرف القناة الجديد الآن مع الـ @ (مثال: @MyNewChannel):")
        bot.register_next_step_handler(msg, save_dynamic_channel)
        bot.answer_callback_query(call.id)

    elif call.data == "admin_broadcast":
        if str(call.from_user.id) != os.getenv("ADMIN_ID"): return
        msg = bot.send_message(chat_id, "أرسل نص الرسالة التي تريد بثها لجميع المشتركين الآن:")
        bot.register_next_step_handler(msg, process_broadcast)
        bot.answer_callback_query(call.id)

# ----------------------------------------------------------------
# 6. وظائف خطوة بخطوة للإدارة (Admin Steps)
# ----------------------------------------------------------------
def save_dynamic_channel(message):
    channel_user = message.text.strip()
    if channel_user.startswith("@"):
        supabase_client.table("settings").upsert({"key": "dynamic_channel", "value": channel_user}).execute()
        bot.send_message(message.chat.id, f"✅ تم تحديث القناة الإجبارية الإضافية بنجاح إلى: {channel_user}")
    else:
        bot.send_message(message.chat.id, "❌ خطأ في الصيغة. يجب أن تبدأ بـ @.")

def process_broadcast(message):
    text_to_send = message.text
    users = supabase_client.table("users").select("user_id").execute()
    success, fail = 0, 0
    for u in users.data:
        try:
            bot.send_message(u["user_id"], text_to_send)
            success += 1
        except Exception:
            fail += 1
    bot.send_message(message.chat.id, f"📢 اكتملت الإذاعة.\n✅ بنجاح: {success}\n❌ فشل (حظر أو توقف): {fail}")

# ----------------------------------------------------------------
# 7. محرك إدارة واستهلاك النقاط
# ----------------------------------------------------------------
def process_billing_and_run(user_id, chat_id, service_key, free_limit, points_cost, worker_func, *args):
    """
    دالة موحدة لمعالجة الخصم والفحص المالي قبل استدعاء خدمات الذكاء الاصطناعي
    """
    if not is_subscribed(user_id):
        send_subscription_requirement(chat_id)
        return

    user = get_or_create_user(telebot.types.User(id=user_id, is_bot=False, first_name=""))
    limits = check_and_reset_limits(user_id)
    
    current_used = limits[service_key]
    
    if current_used < free_limit:
        # استهلاك مجاني
        bot.send_message(chat_id, "⏳ جاري المعالجة ضمن المحاولات المجانية اليومية...")
        if worker_func(*args):
            supabase_client.table("daily_limits").update({service_key: current_used + 1}).eq("user_id", user_id).execute()
    else:
        # استهلاك مدفوع بالنقاط
        if user["points"] >= points_cost:
            bot.send_message(chat_id, f"⏳ جاري المعالجة وخصم {points_cost} نقاط من رصيدك المدفوع...")
            if worker_func(*args):
                supabase_client.table("users").update({"points": user["points"] - points_cost}).eq("user_id", user_id).execute()
        else:
            bot.send_message(chat_id, f"❌ نفدت محاولاتك المجانية اليومية لهذه الخدمة، ورصيد نقاطك غير كافٍ (تحتاج {points_cost} نقاط). يمكنك الشحن أو دعوة الأصدقاء.")

# ----------------------------------------------------------------
# 8. التكامل الفعلي مع خدمات Google GenAI SDK الأساسية
# ----------------------------------------------------------------
def run_pdf_summary(chat_id, file_path):
    try:
        # رفع الملف عبر واجهة المزامنة الحديثة لـ 2026 لقراءة المحتوى
        uploaded_file = ai_client.files.upload(file=file_path)
        response = ai_client.models.generate_content(
            model='gemini-1.5-flash',
            contents=[uploaded_file, "قم بتلخيص هذه المحاضرة بدقة تامة وبصيغة نقاط رئيسية منظمة."]
        )
        bot.send_message(chat_id, f"📋 *ملخص المحاضرة:*\n\n{response.text}")
        return True
    except Exception as e:
        bot.send_message(chat_id, "❌ حدث خطأ غير متوقع أثناء معالجة مستند PDF.")
        return False
    finally:
        if os.path.exists(file_path): os.remove(file_path)

def run_voice_transcription(chat_id, file_path):
    try:
        uploaded_file = ai_client.files.upload(file=file_path)
        response = ai_client.models.generate_content(
            model='gemini-1.5-flash',
            contents=[uploaded_file, "قم بتحويل هذا الصوت إلى نص مكتوب باللغة المصطحبة له بدقة وبدون تعديل سياق."]
        )
        bot.send_message(chat_id, f"🎙️ *النص المفرغ من الصوت:*\n\n{response.text}")
        return True
    except Exception as e:
        bot.send_message(chat_id, "❌ فشل تحويل الملف الصوتي إلى نص.")
        return False
    finally:
        if os.path.exists(file_path): os.remove(file_path)

def run_text_translation(chat_id, text_content):
    try:
        response = ai_client.models.generate_content(
            model='gemini-1.5-flash',
            contents=[f"ترجم النص التالي إلى اللغة العربية ترجمة احترافية أكاديمية:\n\n{text_content}"]
        )
        bot.send_message(chat_id, f"🌐 *الترجمة الاحترافية:*\n\n{response.text}")
        return True
    except Exception:
        bot.send_message(chat_id, "❌ حدث خطأ أثناء معالجة الترجمة.")
        return False

def run_generate_image(chat_id, prompt_text, image_type):
    try:
        refined_prompt = f"Professional business {image_type}, highly detailed, clean diagram design about: {prompt_text}"
        # توليد الصور باستخدام الموديل الرسمي الحديث المستقر Imagen 3
        result = ai_client.models.generate_images(
            model='imagen-3.0-generate-002',
            prompt=refined_prompt,
            config=types.GenerateImagesConfig(number_of_images=1, output_mime_type="image/jpeg")
        )
        
        for generated_image in result.generated_images:
            import io
            image_bytes = io.BytesIO(generated_image.image.image_bytes)
            bot.send_photo(chat_id, image_bytes, caption=f"✅ تم توليد الـ {image_type} بنجاح.")
        return True
    except Exception as e:
        bot.send_message(chat_id, "❌ حدث خطأ أثناء معالجة وتوليد الصورة الذكية.")
        return False

# ----------------------------------------------------------------
# 9. معالجة الرسائل الواردة (الملفات، النصوص، الصوتيات)
# ----------------------------------------------------------------
@bot.message_with_type_handler(content_types=['document'])
def handle_docs(message):
    if message.document.mime_type == 'application/pdf':
        file_info = bot.get_file(message.document.file_id)
        downloaded_file = bot.download_file(file_info.file_path)
        file_path = f"temp_{message.document.file_name}"
        with open(file_path, 'wb') as f:
            f.write(downloaded_file)
        
        process_billing_and_run(str(message.from_user.id), message.chat.id, "pdf_count", 3, 3, run_pdf_summary, message.chat.id, file_path)

@bot.message_with_type_handler(content_types=['voice', 'audio'])
def handle_audio(message):
    file_id = message.voice.file_id if message.voice else message.audio.file_id
    file_info = bot.get_file(file_id)
    downloaded_file = bot.download_file(file_info.file_path)
    file_path = f"temp_{file_id}.ogg"
    with open(file_path, 'wb') as f:
        f.write(downloaded_file)
        
    process_billing_and_run(str(message.from_user.id), message.chat.id, "voice_count", 1, 2, run_voice_transcription, message.chat.id, file_path)

@bot.message_with_type_handler(content_types=['text'])
def handle_text_requests(message):
    text = message.text.strip()
    user_id = str(message.from_user.id)
    
    if text.startswith("ترجم "):
        target_text = text.replace("ترجم ", "", 1)
        process_billing_and_run(user_id, message.chat.id, "translate_count", 3, 2, run_text_translation, message.chat.id, target_text)
    
    elif text.startswith("انفوجرافيك "):
        prompt = text.replace("انفوجرافيك ", "", 1)
        process_billing_and_run(user_id, message.chat.id, "info_count", 2, 5, run_generate_image, message.chat.id, prompt, "infographic")
        
    elif text.startswith("مخطط ذهني "):
        prompt = text.replace("مخطط ذهني ", "", 1)
        process_billing_and_run(user_id, message.chat.id, "mind_count", 2, 5, run_generate_image, message.chat.id, prompt, "mindmap")
    else:
        bot.send_message(message.chat.id, "ℹ️ يرجى استخدام الأوامر بصيغتها الصحيحة:\n• لبدء ترجمة: `ترجم [النص]`\n• لعمل إنفوجرافيك: `انفوجرافيك [الموضوع]`\n• لعمل مخطط ذهني: `مخطط ذهني [الموضوع]`\n• أو أرسل ملفات PDF وصوتيات مباشرة لتلخيصها.")

# ----------------------------------------------------------------
# 10. معالجة شحن الـ Telegram Stars (النجوم) تلقائياً
# ----------------------------------------------------------------
@bot.pre_checkout_query_handler(func=lambda query: True)
def checkout(pre_checkout_query):
    bot.answer_pre_checkout_query(pre_checkout_query.id, ok=True)

@bot.message_with_type_handler(content_types=['successful_payment'])
def got_payment(message):
    user_id = str(message.from_user.id)
    payload = message.successful_payment.invoice_payload
    
    if "user_upgrade_" in payload:
        user = get_or_create_user(message.from_user)
        # 1 نجمة تعطي 3 نقاط
        stars_received = message.successful_payment.total_amount / 1 # تيليجرام يحسبها بالوحدات الأساسية للنجم
        added_points = int(stars_received) * 3
        
        new_points = user["points"] + added_points
        supabase_client.table("users").update({"points": new_points}).eq("user_id", user_id).execute()
        
        bot.send_message(message.chat.id, f"🎉 شكراً لك! تم تأكيد عمليتك بنجاح وشحن حسابك بـ {added_points} نقاط.")

# ----------------------------------------------------------------
# 11. إعدادات خادم Webhook و Flask وتوافق الاستضافة على Render
# ----------------------------------------------------------------
@app.route('/' + TOKEN, methods=['POST'])
def getMessage():
    json_string = request.get_data().decode('utf-8')
    update = telebot.types.Update.de_json(json_string)
    bot.process_new_updates([update])
    return "!", 200

@app.route("/")
def webhook():
    # نقطة النهاية للـ Webhook والـ Cron-Job لمنع السيرفر من النوم
    bot.remove_webhook()
    render_url = os.getenv("RENDER_EXTERNAL_URL") # يسحب الرابط تلقائياً من بيئة ريندر
    bot.set_webhook(url=render_url + "/" + TOKEN)
    return "Bot Core Status: ACTIVE & MONITORING", 200

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get('PORT', 5000)))
