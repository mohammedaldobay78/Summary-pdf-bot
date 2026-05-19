import asyncio
import logging
import json
import os
import tempfile
import contextlib
import sqlite3
import io
from typing import List, Optional

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

# مسار قاعدة البيانات لإدارة الحالات
DB_PATH = "users.db"
# مسار تخزين الملفات المؤقتة
TEMP_FILE_PATH = tempfile.gettempdir()

# تهيئة تسجيل الأخطاء (Logging) بشكل احترافي للسيرفر
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# تهيئة عميل Gemini الحديث
client = genai.Client(api_key=GEMINI_API_KEY)


# --- 1. قاعدة البيانات (SQLite) ---
def setup_db():
    """تنشئ جدول الحالات إذا لم يكن موجودًا."""
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
    """تحصل على حالة المستخدم الحالية بشكل غير متزامن."""
    loop = asyncio.get_running_loop()
    def get():
        with sqlite3.connect(DB_PATH) as conn:
            cursor = conn.cursor()
            cursor.execute('SELECT current_state, current_text FROM user_states WHERE user_id = ?', (user_id,))
            return cursor.fetchone()
    return await loop.run_in_executor(None, get)

async def set_user_state_async(user_id: int, state: str, text: Optional[str] = None):
    """تحفظ حالة المستخدم الحالية بشكل غير متزامن."""
    loop = asyncio.get_running_loop()
    def set():
        with sqlite3.connect(DB_PATH) as conn:
            cursor = conn.cursor()
            cursor.execute('INSERT OR REPLACE INTO user_states (user_id, current_state, current_text) VALUES (?, ?, ?)', 
                           (user_id, state, text))
            conn.commit()
    await loop.run_in_executor(None, set)


# --- 2. معالجة الملفات (PDF) - محليًا في Thread منفصل ---
async def extract_text_from_pdf_async(file_path: str):
    """تستخرج النص من ملف PDF بشكل غير متزامن لتجنب الجمود."""
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


# --- 3. دوال التواصل مع Gemini (تحليل وتلخيص وترجمة) ---
async def call_gemini_async(model: str, contents: list):
    """تتصل بـ Gemini API لإنشاء النصوص بشكل غير متزامن."""
    loop = asyncio.get_running_loop()
    def call():
        response = client.models.generate_content(
            model=model,
            contents=contents
        )
        return response.text
    return await loop.run_in_executor(None, call)


# --- 4. دالة توليد الصور الحقيقية باستخدام Imagen 3 عبر Gemini API ---
async def generate_mindmap_image_async(markdown_text: str) -> bytes:
    """تتصل بـ API الحقيقي (Imagen 3) لتوليد صورة الخريطة الذهنية وإعادتها كـ Bytes."""
    
    image_prompt = f"""
Create a professional, modern, and beautifully formatted visual mind map based on this structure.
The central topic must be bold and located in the dead center of the image.
Main branches should extend outwards in distinct colored boxes (blues, greens, reds, yellows).
Sub-branches must be smaller, clear white boxes containing concise text and relevant small icons/emojis (like currency for debt, open book for history).
The layout must be perfectly clean, high-resolution, organized, and corporate-presentation ready.
Do NOT include any raw markdown characters like # or ## or asterisks in the final image.

Structure to visualize:
{markdown_text}
"""
    loop = asyncio.get_running_loop()
    
    def call_imagen():
        logger.info("Calling Imagen API for image generation...")
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


# --- 5. طابور المهام (Queue) والعامل (Worker) المستقل ---
mindmap_queue = asyncio.Queue()

async def queue_mindmap_task(user_id: int, chat_id: int, markdown: str):
    """ترسل مهمة إنشاء الخريطة الذهنية إلى الطابور."""
    await mindmap_queue.put((user_id, chat_id, markdown))
    await set_user_state_async(user_id, "WAITING_FOR_IMAGE", markdown)

async def banana_worker(context: ContextTypes.DEFAULT_TYPE):
    """عامل مستقل يعالج طابور مهام الصور بشكل متسلسل ومستقر للسيرفر."""
    while True:
        user_id, chat_id, markdown = await mindmap_queue.get()
        logger.info(f"Worker started processing mindmap image for user {user_id}")
        
        try:
            await context.bot.send_message(
                chat_id=chat_id, 
                text="🤖 بدأ نموذج التصوير الذكي العمل على تصميم خريطتك الذهنية المرئية... يستغرق الأمر عادةً من 10 إلى 20 ثانية."
            )
            
            image_bytes = await generate_mindmap_image_async(markdown)
            image_file = io.BytesIO(image_bytes)
            image_file.name = "mindmap.jpg"
            
            await context.bot.send_photo(
                chat_id=chat_id, 
                photo=InputFile(image_file), 
                caption="🎯 إليك خريطتك الذهنية المرئية المصممة باحترافية!"
            )
            
            await set_user_state_async(user_id, "DONE")
            
        except Exception as e:
            logger.error(f"Error in image generation worker for user {user_id}: {e}")
            await context.bot.send_message(
                chat_id=chat_id, 
                text="عذرًا، حدث خطأ أثناء تواصل البوت مع خوادم توليد الصور. يرجى المحاولة مرة أخرى لاحقًا."
            )
            await set_user_state_async(user_id, "ERROR_DURING_GENERATION")
            
        finally:
            mindmap_queue.task_done()
            await asyncio.sleep(1)


# --- 6. معالجات تيليجرام (Bot Handlers) ---
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """يرسل رسالة ترحيبية تشرح وظائف البوت."""
    user = update.effective_user
    setup_db()
    await set_user_state_async(user.id, "START")
    
    welcome_text = (
        f"مرحباً {user.first_name}! 👋\n\n"
        "أنا بوت تيليجرام متقدم مدعوم بذكاء Gemini الحقيقي لتحليل ملفات PDF والنصوص وإعادة صياغتها بصرياً.\n\n"
        "**الخدمات المتاحة:**\n"
        "1. 📝 **التلخيص السريع:** استخراج الأفكار الأساسية بذكاء.\n"
        "2. 🌐 **الترجمة الاحترافية:** ترجمة دقيقة للنصوص المستخرجة.\n"
        "3. 🖼️ **خريطة ذهنية مرئية:** تحويل الهيكل إلى خريطة مرئية حقيقية عبر ذكاء Imagen 3.\n\n"
        "أرسل لي ملف PDF الآن لنبدأ!"
    )
    await update.message.reply_text(welcome_text, parse_mode="Markdown")

async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """يستدعى عندما يرسل المستخدم ملف PDF."""
    user = update.effective_user
    document = update.message.document

    if document.mime_type != 'application/pdf':
        await update.message.reply_text("عذرًا، أنا أدعم فقط ملفات PDF في الوقت الحالي.")
        return

    await update.message.reply_text("جاري تحميل ملف PDF واستخراج النص منه... انتظر قليلاً.")
    
    with tempfile.NamedTemporaryFile(dir=TEMP_FILE_PATH, delete=False) as tmp_file:
        file = await context.bot.get_file(document.file_id)
        await file.download_to_drive(custom_path=tmp_file.name)
        file_path = tmp_file.name

    try:
        extracted_text = await extract_text_from_pdf_async(file_path)
        
        if not extracted_text.strip():
            await update.message.reply_text("عذرًا، يبدو أن ملف PDF هذا لا يحتوي على نص مقروء رقمياً.")
            return

        await set_user_state_async(user.id, "RECEIVED_PDF", extracted_text)
        
        keyboard = [
            [
                InlineKeyboardButton("📝 تلخيص النص", callback_data='summarize_flash'),
                InlineKeyboardButton("🌐 ترجمة للعربية", callback_data='translate_flash'),
            ],
            [
                InlineKeyboardButton("🖼️ خريطة ذهنية مرئية (Imagen 3)", callback_data='mindmap_image'),
            ],
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        await update.message.reply_text(
            f"✅ تم استخراج النص بنجاح! ({len(extracted_text)} حرفاً).\n"
            "اختر الإجراء الذي ترغب في تنفيذه:",
            reply_markup=reply_markup
        )
        
    except Exception as e:
        logger.error(f"Error handling PDF from user {user.id}: {e}")
        await update.message.reply_text("عذرًا، حدث خطأ أثناء تحليل ملف PDF. يرجى المحاولة مرة أخرى.")
        
    finally:
        if os.path.exists(file_path):
            os.remove(file_path)

async def handle_callback_query(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """يستدعى عند الضغط على أزرار التحكم الخيارية."""
    user = update.effective_user
    query = update.callback_query
    await query.answer()
    
    user_data = await get_user_state_async(user.id)
    if not user_data or user_data[0] != "RECEIVED_PDF":
        await query.edit_message_text("عذرًا، انتهت الجلسة أو يجب عليك إرسال ملف PDF جديد أولاً.")
        return
        
    current_text = user_data[1]
    
    if query.data == 'summarize_flash':
        await query.edit_message_text("⚡ جاري تلخيص النص بدقة...")
        prompt = f"قم بتلخيص النص التالي بدقة وإيجاز، مع ذكر أهم النقاط الرئيسية كنقاط محددة:\n\n{current_text}"
        summary = await call_gemini_async(model='gemini-1.5-flash', contents=[prompt])
        await context.bot.send_message(chat_id=user.id, text=f"📋 **إليك التلخيص:**\n\n{summary}", parse_mode="Markdown")
        await set_user_state_async(user.id, "DONE")
        
    elif query.data == 'translate_flash':
        await query.edit_message_text("🌐 جاري الترجمة إلى العربية...")
        prompt = f"ترجم النص التالي إلى اللغة العربية، مع الحفاظ على الأسلوب المهني والوضوح التام وعلامات الترقيم:\n\n{current_text}"
        translation = await call_gemini_async(model='gemini-1.5-flash', contents=[prompt])
        await context.bot.send_message(chat_id=user.id, text=f"🔮 **إليك الترجمة:**\n\n{translation}")
        await set_user_state_async(user.id, "DONE")
        
    elif query.data == 'mindmap_image':
        await query.edit_message_text("🧠 جاري بناء هيكل خريطة ذهنية نصي أولاً...")
        
        markdown_prompt = f"""
        حلل النص التالي لإنشاء خريطة ذهنية هرمية دقيقة بتنسيق Markdown. يجب أن يكون الهيكل كالتالي:
        # الموضوع المركزي
        ## الفرع الرئيسي 1
        ### الفرع الفرعي 1.1
        - نقطة فرعية مفصلة

        استخدم كلمات دلالية واضحة وأيقونات تعبيرية مناسبة جداً للسياق. وزع الفروع بتوازن.
        أعد الهيكل النصي فقط بدون أي مقدمات أو مؤخرات.

        النص المراد تحليله:
        {current_text}
        """
        markdown = await call_gemini_async(model='gemini-1.5-flash', contents=[markdown_prompt])
        
        # السطر بعد إصلاح المشكلة النصية السابقة تماماً هنا:
        await context.bot.send_message(
            chat_id=user.id, 
            text=f"📋 **تم إنشاء هيكل الخريطة النصي:**\n\n```markdown\n{markdown}\n```\n\n⏳ جاري إرسال البيانات الآن لنموذج التصوير المتقدم لتوليد خريطتك المرئية...",
            parse_mode="Markdown"
        )
        
        await queue_mindmap_task(user.id, user.id, markdown)


# --- 7. تشغيل البوت الحقيقي (Main) ---
def main():
    """تهيئة وتشغيل البوت والعمل في الخلفية بشكل متزامن متوافق مع سيرفر Render."""
    setup_db()
    
    # بناء تطبيق تيليجرام
    application = Application.builder().token(BOT_TOKEN).build()
    
    # تسجيل معالجات الأحداث
    application.add_handler(CommandHandler("start", start))
    application.add_handler(MessageHandler(filters.Document.ALL, handle_document))
    application.add_handler(CallbackQueryHandler(handle_callback_query))
    
    # تشغيل عامل توليد الصور الخلفي كمهام خلفية عبر الـ loop المدمج بالتطبيق
    application.job_queue.run_once(lambda ctx: asyncio.create_task(banana_worker(ctx)), when=0)

    logger.info("Bot is deploying on production server...")
    
    # قراءة الـ Port من متغيرات البيئة تلقائياً كما يطلب سيرفر Render
    port = int(os.environ.get("PORT", 8443))
    
    # تشغيل الـ Webhook بدون إغلاق الـ loop يدوياً
    application.run_webhook(
        listen="0.0.0.0",
        port=port,
        url_path=BOT_TOKEN,
        webhook_url=f"https://yourdomain.render.com/{BOT_TOKEN}" 
    )

if __name__ == '__main__':
    main()
