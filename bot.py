import os
import datetime
from flask import Flask, request, abort
import telebot
from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton, LabeledPrice
from google import genai
from google.genai import types
import requests

# ----------------------------------------------------------------
# 1. الإعدادات والمتغيرات البيئية
# ----------------------------------------------------------------
TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
BOT_USERNAME = os.getenv("TELEGRAM_BOT_USERNAME")
SUPABASE_URL = os.getenv("SUPABASE_URL")  # مثال: https://xyz.supabase.co
SUPABASE_KEY = os.getenv("SUPABASE_KEY")
GOOGLE_API_KEY = os.getenv("GEMINI_API_KEY")
PROVIDER_TOKEN = "" 

bot = telebot.TeleBot(TOKEN, parse_mode="Markdown")
app = Flask(__name__)

# تهيئة عميل Google GenAI SDK لعام 2026
ai_client = genai.Client(api_key=GOOGLE_API_KEY)

PRIMARY_CHANNEL = "@Axia_Tech"

# ----------------------------------------------------------------
# 2. محرك الاتصال المباشر بـ Supabase REST API (بديل المكتبة المتعارضة)
# ----------------------------------------------------------------
def db_request(method, table, params=None, json_data=None, custom_headers=None):
    """دالة موحدة للتعامل مع قاعدة البيانات عبر REST API مباشرة وبدون حزم متعارضة"""
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

def get_or_create_user(tg_user, referrer_id=None):
    user_id = str(tg_user.id)
    data = db_request("GET", "users", params={"user_id": f"eq.{user_id}"})
    
    if not data:
        # معالجة نظام الإحالة (الداعي)
        if referrer_id and referrer_id != user_id:
            ref_data = db_request("GET", "users", params={"user_id": f"eq.{referrer_id}"})
            if ref_data:
                current_points = ref_data[0]["points"]
                db_request("PATCH", "users", params={"user_id": f"eq.{referrer_id}"}, json_data={"points": current_points + 2})
                try:
                    bot.send_message(referrer_id, "🎉 سجل مستخدم جديد عن طريق رابطك! تم إضافة نقطتين إلى رصيدك.")
                except Exception:
                    pass

        # إنشاء سجل الحساب الجديد
        new_user = {
            "user_id": user_id,
            "username": tg_user.username or "",
            "points": 0,
            "last_daily_gift": "1970-01-01"
        }
        db_request("POST", "users", json_data=new_user)
        
        # إنشاء سجل القيود اليومية
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

def is_subscribed(user_id):
    try:
        member = bot.get_chat_member(PRIMARY_CHANNEL, user_id)
        if member.status in ['left', 'kicked']: return False
    except Exception:
        return False
    
    dyn_channel = get_dynamic_channel()
    if dyn_channel:
        try:
            member = bot.get_chat_member(dyn_channel, user_id)
            if member.status in ['left', 'kicked']: return False
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
@bot.message_reaction_handler(commands=['start'])
def cmd_start(message):
    user_id = str(message.from_user.id)
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

@bot.message_reaction_handler(commands=['admin'])
def cmd_admin(message):
    admin_id = os.getenv("ADMIN_ID")
    if str(message.from_user.id) != str(admin_id): return
    bot.send_message(message.chat.id, "🛠️ لوحة تحكم الإدارة الصارمة:", reply_markup=admin_keyboard())

def send_subscription_requirement(chat_id):
    markup = InlineKeyboardMarkup()
    dyn_channel = get_dynamic_channel()
    
    markup.row(InlineKeyboardButton("1️⃣ القناة الأساسية", url=f"https://t.me/{PRIMARY_CHANNEL.replace('@','')}" ))
    if dyn_channel:
        markup.row(InlineKeyboardButton("2️⃣ القناة الإضافية", url=f"https://t.me/{dyn_channel.replace('@','')}"))
        
    markup.row(InlineKeyboardButton("✅ تحقق من الاشتراك", callback_data="verify_sub"))
    bot.send_message(chat_id, "⚠️ عذراً، يجب عليك الاشتراك في القنوات الرسمية للبوت أولاً لتتمكن من استخدامه.", reply_markup=markup)

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
            bot.send_message(chat_id, "✅ تم تأكيد الاشتراك بنجاح.", reply_markup=main_menu_keyboard())
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
        text = f"🔗 *رابط الإحالة الخاص بك:*\n\n`{link}`\n\n لكل صديق يسجل ستحصل على *2 نقاط*."
        markup = InlineKeyboardMarkup()
        markup.row(InlineKeyboardButton("🔙 العودة", callback_data="main_menu"))
        bot.edit_message_text(text, chat_id, call.message.message_id, reply_markup=markup)

    elif call.data == "daily_gift":
        user = get_or_create_user(call.from_user)
        today_str = str(datetime.date.today())
        
        if user["last_daily_gift"] == today_str:
            bot.answer_callback_query(call.id, "❌ لقد حصلت على هديتك اليومية بالفعل!", show_alert=True)
        else:
            db_request("PATCH", "users", params={"user_id": f"eq.{user_id}"}, json_data={"points": user["points"] + 2, "last_daily_gift": today_str})
            bot.answer_callback_query(call.id, "🎉 تم استلام 2 نقاط بنجاح كمكافأة يومية!", show_alert=True)

    elif call.data == "buy_stars":
        prices = [LabeledPrice(label="شحن رصيد نقاط", amount=1)]
        bot.send_invoice(chat_id, title="شحن نقاط", description="1 نجمة = 3 نقاط.", provider_token=PROVIDER_TOKEN, currency="XTR", prices=prices, start_parameter="buy-points", payload=f"user_upgrade_{user_id}")
        bot.answer_callback_query(call.id)

    elif call.data == "admin_stats":
        if str(call.from_user.id) != os.getenv("ADMIN_ID"): return
        res = requests.get(f"{SUPABASE_URL}/rest/v1/users", headers={"apikey": SUPABASE_KEY, "Authorization": f"Bearer {SUPABASE_KEY}", "Prefer": "count=exact"})
        count = res.headers.get("Content-Range", "0-0/0").split("/")[-1]
        bot.send_message(chat_id, f"📊 إجمالي عدد المستخدمين المسجلين: {count}")
        bot.answer_callback_query(call.id)

    elif call.data == "admin_set_channel":
        if str(call.from_user.id) != os.getenv("ADMIN_ID"): return
        msg = bot.send_message(chat_id, "قم بإرسال معرف القناة الجديد الآن مع الـ @:")
        bot.register_next_step_handler(msg, save_dynamic_channel)
        bot.answer_callback_query(call.id)

    elif call.data == "admin_broadcast":
        if str(call.from_user.id) != os.getenv("ADMIN_ID"): return
        msg = bot.send_message(chat_id, "أرسل نص الرسالة التي تريد بثها:")
        bot.register_next_step_handler(msg, process_broadcast)
        bot.answer_callback_query(call.id)

# ----------------------------------------------------------------
# 6. وظائف خطوة بخطوة للإدارة (Admin Steps)
# ----------------------------------------------------------------
def save_dynamic_channel(message):
    channel_user = message.text.strip()
    if channel_user.startswith("@"):
        db_request("POST", "settings", json_data={"key": "dynamic_channel", "value": channel_user}, custom_headers={"Prefer": "resolution=merge-duplicates"})
        bot.send_message(message.chat.id, f"✅ تم تحديث القناة الإضافية إلى: {channel_user}")
    else:
        bot.send_message(message.chat.id, "❌ خطأ في الصيغة. يجب أن تبدأ بـ @.")

def process_broadcast(message):
    text_to_send = message.text
    users = db_request("GET", "users")
    success, fail = 0, 0
    for u in users:
        try:
            bot.send_message(u["user_id"], text_to_send)
            success += 1
        except Exception:
            fail += 1
    bot.send_message(message.chat.id, f"📢 اكتملت الإذاعة.\n✅ بنجاح: {success}\n❌ فشل: {fail}")

# ----------------------------------------------------------------
# 7. محرك إدارة واستهلاك النقاط
# ----------------------------------------------------------------
def process_billing_and_run(user_id, chat_id, service_key, free_limit, points_cost, worker_func, *args):
    if not is_subscribed(user_id):
        send_subscription_requirement(chat_id)
        return

    user = get_or_create_user(telebot.types.User(id=int(user_id), is_bot=False, first_name=""))
    limits = check_and_reset_limits(user_id)
    current_used = limits[service_key]
    
    if current_used < free_limit:
        bot.send_message(chat_id, "⏳ جاري المعالجة ضمن المحاولات المجانية اليومية...")
        if worker_func(*args):
            db_request("PATCH", "daily_limits", params={"user_id": f"eq.{user_id}"}, json_data={service_key: current_used + 1})
    else:
        if user["points"] >= points_cost:
            bot.send_message(chat_id, f"⏳ جاري المعالجة وخصم {points_cost} نقاط...")
            if worker_func(*args):
                db_request("PATCH", "users", params={"user_id": f"eq.{user_id}"}, json_data={"points": user["points"] - points_cost})
        else:
            bot.send_message(chat_id, f"❌ نفدت محاولاتك المجانية. تحتاج {points_cost} نقاط لإتمام العملية.")

# ----------------------------------------------------------------
# 8. التكامل مع خدمات Google GenAI SDK
# ----------------------------------------------------------------
def run_pdf_summary(chat_id, file_path):
    try:
        uploaded_file = ai_client.files.upload(file=file_path)
        response = ai_client.models.generate_content(
            model='gemini-1.5-flash',
            contents=[uploaded_file, "قم بتلخيص هذه المحاضرة بدقة تامة وبصيغة نقاط رئيسية منظمة."]
        )
        bot.send_message(chat_id, f"📋 *ملخص المحاضرة:*\n\n{response.text}")
        return True
    except Exception:
        bot.send_message(chat_id, "❌ حدث خطأ أثناء معالجة ملف PDF.")
        return False
    finally:
        if os.path.exists(file_path): os.remove(file_path)

def run_voice_transcription(chat_id, file_path):
    try:
        uploaded_file = ai_client.files.upload(file=file_path)
        response = ai_client.models.generate_content(
            model='gemini-1.5-flash',
            contents=[uploaded_file, "قم بتحويل هذا الصوت إلى نص مكتوب بدقة."]
        )
        bot.send_message(chat_id, f"🎙️ *النص المفرغ من الصوت:*\n\n{response.text}")
        return True
    except Exception:
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
        refined_prompt = f"Professional {image_type}, highly detailed, clean diagram design about: {prompt_text}"
        result = ai_client.models.generate_images(
            model='imagen-3.0-generate-002',
            prompt=refined_prompt,
            config=types.GenerateImagesConfig(number_of_images=1, output_mime_type="image/jpeg")
        )
        import io
        for generated_image in result.generated_images:
            image_bytes = io.BytesIO(generated_image.image.image_bytes)
            bot.send_photo(chat_id, image_bytes, caption=f"✅ تم توليد الـ {image_type} بنجاح.")
        return True
    except Exception:
        bot.send_message(chat_id, "❌ حدث خطأ أثناء توليد الصورة الذكية.")
        return False

# ----------------------------------------------------------------
# 9. معالجة الرسائل الواردة
# ----------------------------------------------------------------
@bot.message_reaction_handler(content_types=['document'])
def handle_docs(message):
    if message.document.mime_type == 'application/pdf':
        file_info = bot.get_file(message.document.file_id)
        downloaded_file = bot.download_file(file_info.file_path)
        file_path = f"temp_{message.document.file_name}"
        with open(file_path, 'wb') as f: f.write(downloaded_file)
        
        process_billing_and_run(str(message.from_user.id), message.chat.id, "pdf_count", 3, 3, run_pdf_summary, message.chat.id, file_path)

@bot.message_reaction_handler(content_types=['voice', 'audio'])
def handle_audio(message):
    file_id = message.voice.file_id if message.voice else message.audio.file_id
    file_info = bot.get_file(file_id)
    downloaded_file = bot.download_file(file_info.file_path)
    file_path = f"temp_{file_id}.ogg"
    with open(file_path, 'wb') as f: f.write(downloaded_file)
        
    process_billing_and_run(str(message.from_user.id), message.chat.id, "voice_count", 1, 2, run_voice_transcription, message.chat.id, file_path)

@bot.message_reaction_handler(content_types=['text'])
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
        bot.send_message(message.chat.id, "ℹ️ الصيغ المدعومة:\n• `ترجم [النص]`\n• `انفوجرافيك [الموضوع]`\n• `مخطط ذهني [الموضوع]`\n• أو أرسل ملف PDF أو تسجيل صوتي مباشرة.")

# ----------------------------------------------------------------
# 10. معالجة شحن النجوم التلقائي
# ----------------------------------------------------------------
@bot.pre_checkout_query_handler(func=lambda query: True)
def checkout(pre_checkout_query):
    bot.answer_pre_checkout_query(pre_checkout_query.id, ok=True)

@bot.message_reaction_handler(content_types=['successful_payment'])
def got_payment(message):
    user_id = str(message.from_user.id)
    payload = message.successful_payment.invoice_payload
    
    if "user_upgrade_" in payload:
        user = get_or_create_user(message.from_user)
        stars_received = message.successful_payment.total_amount
        added_points = int(stars_received) * 3
        
        db_request("PATCH", "users", params={"user_id": f"eq.{user_id}"}, json_data={"points": user["points"] + added_points})
        bot.send_message(message.chat.id, f"🎉 تم شحن حسابك بـ {added_points} نقاط بنجاح.")

# ----------------------------------------------------------------
# 11. إعدادات خادم Webhook و Flask وتوافق Render
# ----------------------------------------------------------------
@app.route('/' + TOKEN, methods=['POST'])
def getMessage():
    json_string = request.get_data().decode('utf-8')
    update = telebot.types.Update.de_json(json_string)
    bot.process_new_updates([update])
    return "!", 200

@app.route("/")
def webhook():
    bot.remove_webhook()
    render_url = os.getenv("RENDER_EXTERNAL_URL")
    bot.set_webhook(url=render_url + "/" + TOKEN)
    return "Bot Core Status: ACTIVE & MONITORING", 200

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get('PORT', 5000)))
