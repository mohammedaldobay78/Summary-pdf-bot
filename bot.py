import asyncio
import logging
import json
import os
import tempfile
import contextlib
import sqlite3
import io
from typing import List, Optional

# مكتبات Flask للـ Webhook والـ Keep-Alive
from flask import Flask, request, jsonify

# مكتبات تيليجرام (الإصدار v20+)
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, InputFile
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters, CallbackQueryHandler

# مكتبات Gemini الجديدة
from google import genai
from google.genai import types

# مكتبات تحليل PDF
import PyPDF2

# --- الإعدادات والثوابت من متغيرات البيئة ---
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")
BOT_TOKEN = os.environ.get("BOT_TOKEN")

if not GEMINI_API_KEY or not BOT_TOKEN:
    raise ValueError("CRITICAL ERROR: GEMINI_API_KEY and BOT_TOKEN must be set as environment variables!")

DB_PATH = "users.db"
TEMP_FILE_PATH = tempfile.gettempdir()

logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# تهيئة عميل Gemini
client = genai.Client(api_key=GEMINI_API_KEY)

# تطبيق Flask
app = Flask(__name__)

# تهيئة تطبيق التيليجرام عالمياً لتسهيل مشاركته مع Flask
application = Application.builder().token(BOT_TOKEN).build()

# --- 1. قاعدة البيانات (SQLite) ---
def setup_db():
    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.cursor()
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS user_states (
                user_id INTEGER PRIMARY KEY,
                current_state TEXT,
                current_text TEXT
            )
        ''')
        conn.commit()

async def get_user_state_async(user_id: int):
    loop = asyncio.get_running_loop()
    def get():
        with sqlite3.connect(DB_PATH) as conn:
            cursor = conn.cursor()
            cursor.execute('SELECT current_state, current_text FROM user_states WHERE user_id = ?', (user_id,))
            return cursor.fetchone()
    return await loop.run_in_executor(None, get)

async def set_user_state_async(user_id: int, state: str, text: Optional[str] = None):
    loop = asyncio.get_running_loop()
    def set():
        with sqlite3.connect(DB_PATH) as conn:
            cursor = conn.cursor()
            cursor.execute('INSERT OR REPLACE INTO user_states (user_id, current_state, current_text) VALUES (?, ?, ?)', 
                           (user_id, state, text))
            conn.commit()
    await loop.run_in_executor(None, set)


# --- 2. معالجة الملفات (PDF) ---
async def extract_text_from_pdf_async(file_path: str):
    loop = asyncio.get_running_loop()
    def extract():
        text = ""
        with open(file_path, 'rb') as file:
            reader = PyPDF2.PdfReader(file)
            for page in reader.pages:
                extracted = page.extract_text()
                if extracted:
                    text += extracted
        return text
    return await loop.run_in_executor(None, extract)


# --- 3. دوال التواصل مع Gemini ---
async def call_gemini_async(model: str, contents: list):
    loop = asyncio.get_running_loop()
    def call():
        response = client.models.generate_content(
            model=model,
            contents=contents
        )
        return response.text
    return await loop.run_in_executor(None, call)


# --- 4. دالة توليد الصور (Imagen 3) ---
async def generate_mindmap_image_async(markdown_text: str) -> bytes:
    image_prompt = f"""
Create a professional, modern, and beautifully formatted visual mind map based on this structure.
The central topic must be bold and located in the dead center of the image.
Main branches should extend outwards in distinct colored boxes.
The layout must be perfectly clean, high-resolution, organized, and corporate-presentation ready.
Do NOT include any raw markdown characters like # or ## or asterisks in the final image.

Structure to visualize:
{markdown_text}
"""
    loop = asyncio.get_running_loop()
    def call_imagen():
        result = client.models.generate_images(
            model='imagen-3.0-generate-002',
            prompt=image_prompt,
            config=types.GenerateImagesConfig(
                number_of_images=1,
                output_mime_type="image/jpeg",
                aspect_ratio="16:9"
            )
        )
        return result.generated_images[0].image.image_bytes
    return await loop.run_in_executor(None, call_imagen)


# --- 5. طابور المهام (Queue) والعامل المستقل ---
mindmap_queue = asyncio.Queue()

async def banana_worker():
    """عامل مستقل يعالج طابور مهام الصور بشكل متسلسل ومتوافق مع Flask."""
    while True:
        try:
            user_id, chat_id, markdown = await mindmap_queue.get()
            logger.info(f"Worker processing mindmap image for user {user_id}")
            
            await application.bot.send_message(
                chat_id=chat_id, 
                text="🤖 بدأ نموذج التصوير الذكي العمل على تصميم خريطتك الذهنية المرئية... يستغرق الأمر عادةً من 10 إلى 20 ثانية."
            )
            
            image_bytes = await generate_mindmap_image_async(markdown)
            image_file = io.BytesIO(image_bytes)
            image_file.name = "mindmap.jpg"
            
            await application.bot.send_photo(
                chat_id=chat_id, 
                photo=InputFile(image_file), 
                caption="🎯 إليك خريطتك الذهنية المرئية المصممة باحترافية!"
            )
            await set_user_state_async(user_id, "DONE")
        except Exception as e:
            logger.error(f"Error in image worker: {e}")
            with contextlib.suppress(Exception):
                await application.bot.send_message(chat_id=chat_id, text="عذرًا، حدث خطأ أثناء توليد الصور.")
        finally:
            mindmap_queue.task_done()
            await asyncio.sleep(1)


# --- 6. معالجات تيليجرام (Bot Handlers) ---
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    setup_db()
    await set_user_state_async(user.id, "START")
    welcome_text = f"مرحباً {user.first_name}! 👋\n\nأرسل لي ملف PDF الآن لنبدأ تحليل وتلخيص المحتوى بصرياً وعبر النصوص!"
    await update.message.reply_text(welcome_text, parse_mode="Markdown")

async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    document = update.message.document
    if document.mime_type != 'application/pdf':
        await update.message.reply_text("عذرًا، أنا أدعم فقط ملفات PDF في الوقت الحالي.")
        return

    await update.message.reply_text("جاري تحميل ملف PDF واستخراج النص منه...")
    with tempfile.NamedTemporaryFile(dir=TEMP_FILE_PATH, delete=False) as tmp_file:
        file = await context.bot.get_file(document.file_id)
        await file.download_to_drive(custom_path=tmp_file.name)
        file_path = tmp_file.name

    try:
        extracted_text = await extract_text_from_pdf_async(file_path)
        if not extracted_text.strip():
            await update.message.reply_text("عذرًا، الملف فارغ أو لا يحتوي على نص مقروء.")
            return

        await set_user_state_async(user.id, "RECEIVED_PDF", extracted_text)
        keyboard = [
            [InlineKeyboardButton("📝 تلخيص النص", callback_data='summarize_flash'),
             InlineKeyboardButton("🌐 ترجمة للعربية", callback_data='translate_flash')],
            [InlineKeyboardButton("🖼️ خريطة ذهنية مرئية", callback_data='mindmap_image')]
        ]
        await update.message.reply_text("✅ تم استخراج النص بنجاح! اختر إجراءً:", reply_markup=InlineKeyboardMarkup(keyboard))
    except Exception as e:
        logger.error(f"Error handling PDF: {e}")
        await update.message.reply_text("حدث خطأ أثناء معالجة الملف.")
    finally:
        if os.path.exists(file_path):
            os.remove(file_path)

async def handle_callback_query(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    query = update.callback_query
    await query.answer()
    
    user_data = await get_user_state_async(user.id)
    if not user_data or user_data[0] != "RECEIVED_PDF":
        await query.edit_message_text("انتهت الجلسة، يرجى إرسال ملف جديد.")
        return
    current_text = user_data[1]
    
    if query.data == 'summarize_flash':
        await query.edit_message_text("⚡ جاري التلخيص...")
        prompt = f"قم بتلخيص النص التالي كنقاط محددة:\n\n{current_text}"
        summary = await call_gemini_async(model='gemini-1.5-flash', contents=[prompt])
        await context.bot.send_message(chat_id=user.id, text=f"📋 **التلخيص:**\n\n{summary}", parse_mode="Markdown")
        await set_user_state_async(user.id, "DONE")
    elif query.data == 'translate_flash':
        await query.edit_message_text("🌐 جاري الترجمة...")
        prompt = f"ترجم النص التالي إلى العربية:\n\n{current_text}"
        translation = await call_gemini_async(model='gemini-1.5-flash', contents=[prompt])
        await context.bot.send_message(chat_id=user.id, text=f"🔮 **الترجمة:**\n\n{translation}")
        await set_user_state_async(user.id, "DONE")
    elif query.data == 'mindmap_image':
        await query.edit_message_text("🧠 جاري بناء هيكل الخريطة...")
        markdown_prompt = f"حلل النص التالي لإنشاء خريطة ذهنية هرمية بتنسيق لغة الماركداون فقط بدون مقدمات:\n\n{current_text}"
        markdown = await call_gemini_async(model='gemini-1.5-flash', contents=[markdown_prompt])
        
        await context.bot.send_message(
            chat_id=user.id, 
            text=f"📋 **تم إنشاء الهيكل النصي:**\n\n```markdown\n{markdown}\n
```\n\n⏳ جاري توليد الصورة المرئية...",
            parse_mode="Markdown"
        )
        await mindmap_queue.put((user.id, user.id, markdown))
        await set_user_state_async(user.id, "WAITING_FOR_IMAGE", markdown)


# --- 7. مسارات Flask (Webhook & Keep-Alive) ---

@app.route('/')
def home():
    """المسار الرئيسي: استخدم هذا الرابط في cron-job لمنع السيرفر من النوم!"""
    return "Bot is running perfectly! (Keep-alive endpoint Active)", 200

@app.route(f'/{BOT_TOKEN}', methods=['POST'])
def webhook():
    """المستمع الرئيسي لتحديثات تيليجرام."""
    if request.method == "POST":
        update = Update.de_json(request.get_json(force=True), application.bot)
        # تشغيل معالجة الرسالة كـ Task غير حاصرة في الـ Loop الحالي
        asyncio.run_coroutine_threadsafe(application.process_update(update), asyncio.get_event_loop())
        return "OK", 200
    return "Invalid Method", 400


# --- 8. إعداد وتشغيل التطبيق المشترك ---
def main():
    setup_db()
    
    # تسجيلHandlers في تطبيق التيليجرام
    application.add_handler(CommandHandler("start", start))
    application.add_handler(MessageHandler(filters.Document.ALL, handle_document))
    application.add_handler(CallbackQueryHandler(handle_callback_query))
    
    # الحصول على الـ Event Loop لتهيئة بيئة العمل المتزامنة مع Flask
    loop = asyncio.get_event_loop()
    
    # البدء المبدئي لتطبيق التيليجرام (بدون تشغيل الرصد الخاص به Polling)
    loop.run_until_complete(application.initialize())
    loop.run_until_complete(application.start())
    
    # تشغيل عامل معالجة الصور بالخلفية بشكل مستمر
    loop.create_task(banana_worker())
    
    logger.info("Application initialized successfully. Starting Flask Server...")
    
    # تشغيل Flask ومطابقتها مع خوادم بورت Render
    port = int(os.environ.get("PORT", 8443))
    app.run(host="0.0.0.0", port=port)

if __name__ == '__main__':
    main()
