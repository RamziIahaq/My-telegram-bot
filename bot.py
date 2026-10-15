import os
import sys
import time
import random
import tempfile
import threading
from pathlib import Path

import requests
import telebot
from telebot import types, apihelper
from flask import Flask
from dotenv import load_dotenv
from google import genai

# =========================================================
# إعدادات البيئة والأمان
# =========================================================
load_dotenv()
TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "").strip()
PORT = int(os.getenv("PORT", "8080"))
MAX_FILE_MB = int(os.getenv("MAX_FILE_MB", "19"))
MAX_FILE_BYTES = MAX_FILE_MB * 1024 * 1024
MAX_RETRIES = 3
FILE_TIMEOUT = 300
TELEGRAM_CHUNK = 3500
BOT_THREADS = 4
MAX_CONTENT_CHARS = 120000
PREVIEW_CHARS = 1200

MODEL_CANDIDATES = []
for name in (
    os.getenv("GEMINI_MODEL", "").strip(),
    "gemini-2.0-flash",
    "gemini-1.5-flash",
    "gemini-1.5-flash-8b",
):
    if name and name not in MODEL_CANDIDATES:
        MODEL_CANDIDATES.append(name)


if not TELEGRAM_TOKEN or not GEMINI_API_KEY:
    print("خطأ: اضبط TELEGRAM_BOT_TOKEN و GEMINI_API_KEY في ملف .env")
    sys.exit(1)

apihelper.CONNECT_TIMEOUT = 60
apihelper.READ_TIMEOUT = 300
bot = telebot.TeleBot(TELEGRAM_TOKEN, threaded=True, num_threads=BOT_THREADS)
client = genai.Client(api_key=GEMINI_API_KEY)
CURRENT_MODEL = MODEL_CANDIDATES[0]

SESSIONS = {}
SESSION_LOCK = threading.RLock()

LANGUAGES = {
    "auto": "لغة المحتوى الأصلية",
    "ar": "العربية",
    "en": "English",
    "fr": "Français",
    "es": "Español",
    "de": "Deutsch",
    "tr": "Türkçe",
    "ur": "اردو",
    "id": "Bahasa Indonesia",
    "hi": "हिन्दी",
    "zh": "中文",
    "ja": "日本語",
    "pt": "Português",
    "ru": "Русский",
}

TASKS = {
    "quick": ("⚡ ملخص سريع", "لخّص المحتوى في نقاط موجزة، مع أهم الأفكار فقط."),
    "detail": ("📚 ملخص تفصيلي", "أنشئ ملخصًا تفصيليًا منظمًا بالعناوين والنقاط، ولا تُسقط الأفكار المهمة."),
    "quiz": ("📝 أسئلة اختبار", "أنشئ 10 أسئلة متنوعة من المحتوى (اختيار من متعدد وصح/خطأ وأسئلة قصيرة)، ثم ضع نموذج الإجابة في قسم منفصل في النهاية."),
    "cards": ("🃏 بطاقات مذاكرة", "أنشئ بطاقات مراجعة؛ كل بطاقة تحتوي سؤالًا واضحًا ثم الإجابة تحته. استخرجها من المحتوى فقط."),
    "explain": ("💡 شرح مبسّط", "اشرح المفاهيم الصعبة بلغة سهلة، مع أمثلة توضيحية. ميّز بين ما ورد في المحتوى وما هو مثال توضيحي منك."),
    "qa": ("❓ سؤال وجواب", "حوّل أهم معلومات المحتوى إلى أسئلة وأجوبة مرتبة للمراجعة."),
    "outline": ("📑 عناوين رئيسية", "رتّب المحتوى على شكل مخطط بعناوين رئيسية وفرعية ونقاط مرتبة."),
    "terms": ("📖 مصطلحات وتعريفات", "استخرج المصطلحات المهمة وعرّفها بحسب المحتوى. اكتب «لم يرد تعريف واضح» عند عدم وجود تعريف كافٍ."),
    "english": ("🌐 ملخص إنجليزي", "اكتب ملخصًا باللغة الإنجليزية للمحتوى، مع الحفاظ على المعنى والمصطلحات الأساسية."),
    "mindmap": ("🧠 خريطة ذهنية نصية", "حوّل المحتوى إلى خريطة ذهنية نصية واضحة تبدأ بالموضوع الرئيسي ثم الفروع والأفكار المرتبطة."),
}
MIME_BY_EXT = {
    ".ogg": "audio/ogg",
    ".opus": "audio/ogg",
    ".mp3": "audio/mpeg",
    ".m4a": "audio/mp4",
    ".wav": "audio/wav",
    ".aac": "audio/aac",
    ".flac": "audio/flac",
    ".pdf": "application/pdf",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".webp": "image/webp",
    ".gif": "image/gif",
}

AUDIO_EXTS = {".ogg", ".opus", ".mp3", ".m4a", ".wav", ".aac", ".flac"}
IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".gif"}


def safe_error(error):
    text = str(error)
    for secret in (TELEGRAM_TOKEN, GEMINI_API_KEY):
        if secret:
            text = text.replace(secret, "[REDACTED]")
    return text[:700]


def model_unavailable(error):
    value = safe_error(error).upper()
    return any(term in value for term in [
        "NOT_FOUND", "NOT FOUND", "INVALID_ARGUMENT", "UNKNOWN MODEL",
        "IS NOT FOUND", "DOES NOT HAVE ACCESS", "MODEL_NOT_FOUND",
    ])


def switch_model():
    global CURRENT_MODEL
    if CURRENT_MODEL in MODEL_CANDIDATES:
        index = MODEL_CANDIDATES.index(CURRENT_MODEL) + 1
    else:
        index = 1
    if index >= len(MODEL_CANDIDATES):
        return False
    CURRENT_MODEL = MODEL_CANDIDATES[index]
    print(f"[Model] switching to {CURRENT_MODEL}")
    return True


def get_session(user_id):
    with SESSION_LOCK:
        return SESSIONS.setdefault(user_id, {"content": "", "language": "auto", "source": ""})


def save_content(user_id, content, source):
    text = (content or "").strip()
    if len(text) > MAX_CONTENT_CHARS:
        text = text[:MAX_CONTENT_CHARS] + "\n\n[تم اختصار المحتوى لأنه طويل جدًا]"
    with SESSION_LOCK:
        language = get_session(user_id).get("language", "auto")
        SESSIONS[user_id] = {"content": text, "language": language, "source": source}


def main_keyboard():
    keyboard = types.ReplyKeyboardMarkup(resize_keyboard=True, row_width=2)
    keyboard.add(types.KeyboardButton("🏠 القائمة"), types.KeyboardButton("🌍 اللغة"))
    keyboard.add(types.KeyboardButton("📖 كيف أستخدمه؟"), types.KeyboardButton("🧹 مسح المحتوى"))
    return keyboard


def task_keyboard():
    keyboard = types.InlineKeyboardMarkup(row_width=2)
    buttons = [types.InlineKeyboardButton(label, callback_data=f"task:{key}") for key, (label, _) in TASKS.items()]
    keyboard.add(*buttons)
    keyboard.add(
        types.InlineKeyboardButton("📝 عرض النص الكامل", callback_data="menu:fulltext"),
        types.InlineKeyboardButton("🌍 لغة النتيجة", callback_data="menu:language"),
    )
    return keyboard


def language_keyboard():
    keyboard = types.InlineKeyboardMarkup(row_width=2)
    buttons = [types.InlineKeyboardButton(label, callback_data=f"lang:{key}") for key, label in LANGUAGES.items()]
    keyboard.add(*buttons)
    keyboard.add(types.InlineKeyboardButton("↩️ رجوع", callback_data="menu:home"))
    return keyboard


def split_text(text, limit=TELEGRAM_CHUNK):
    text = (text or "").strip()
    parts = []
    while len(text) > limit:
        cut = text.rfind("\n", 0, limit + 1)
        if cut < limit // 2:
            cut = text.rfind(" ", 0, limit + 1)
        if cut < limit // 2:
            cut = limit
        part = text[:cut].strip()
        if part:
            parts.append(part)
        text = text[cut:].strip()
    if text:
        parts.append(text)
    return parts


def send_long_text(chat_id, text, prefix=""):
    parts = split_text(text)
    if not parts:
        bot.send_message(chat_id, "لم يتم إنشاء نص صالح للإرسال.", reply_markup=main_keyboard())
        return
    for index, part in enumerate(parts, 1):
        header = prefix
        if len(parts) > 1:
            header = f"{prefix}\n({index}/{len(parts)})" if prefix else f"({index}/{len(parts)})"
        body = f"{header}\n\n{part}" if header else part
        bot.send_message(chat_id, body, reply_markup=main_keyboard(), timeout=60)
        time.sleep(0.15)


def send_content_preview(chat_id, content, source_name):
    clean = (content or "").strip()
    if len(clean) <= TELEGRAM_CHUNK:
        send_long_text(chat_id, clean, prefix=f"📝 النص المستخرج من {source_name}")
        return
    preview = clean[:PREVIEW_CHARS].rstrip() + "\n\n…"
    bot.send_message(
        chat_id,
        f"📝 معاينة النص المستخرج من {source_name} ({len(clean)} حرف):\n\n{preview}\n\nاضغط «عرض النص الكامل» إذا تبي النص كله.",
        reply_markup=main_keyboard(),
    )


def update_status(chat_id, message_id, text):
    try:
        bot.edit_message_text(text, chat_id, message_id)
    except Exception:
        pass


def retryable(error):
    value = safe_error(error).upper()
    return any(term in value for term in [
        "408", "425", "429", "500", "502", "503", "504",
        "RESOURCE_EXHAUSTED", "UNAVAILABLE", "OVERLOADED",
        "DEADLINE_EXCEEDED", "TIMEOUT", "TIMED OUT",
        "CONNECTION RESET", "CONNECTION ERROR",
    ])


def generate_text(prompt, file_part=None):
    last_error = None
    generation_config = {
        "temperature": 0.0,
        "top_p": 0.1,
        "max_output_tokens": 8000,
    }
    
    for attempt in range(1, MAX_RETRIES + 5): # زيادة عدد المحاولات
        try:
            print(f"[AI] محاولة توليد النص رقم {attempt} باستخدام {CURRENT_MODEL}...")
            payload = [prompt] if file_part is None else [prompt, file_part]
            response = client.models.generate_content(
                model=CURRENT_MODEL, 
                contents=payload,
                config=generation_config
            )
            result = getattr(response, "text", None)
            if result and result.strip():
                print("[AI] تم توليد النص بنجاح.")
                return result.strip()
            
            # إذا لم يرجع نصاً، قد يكون بسبب الفلترة
            if hasattr(response, 'candidates') and response.candidates:
                finish_reason = response.candidates[0].finish_reason
                if finish_reason == "SAFETY":
                    return "⚠️ عذراً، الموديل رفض معالجة هذا المحتوى لأسباب تتعلق بسياسات السلامة."

            raise RuntimeError("لم يرجع النموذج نصاً صالحاً.")
        except Exception as exc:
            last_error = exc
            err_str = str(exc).upper()
            print(f"[AI Error] المحاولة {attempt} فشلت: {safe_error(exc)}")
            
            # إذا انتهت الحصة (Quota)
            if "429" in err_str or "QUOTA" in err_str or "EXHAUSTED" in err_str:
                print("[AI] تم استهلاك الحصة المجانية، جاري التبديل للموديل التالي...")
                if switch_model(): continue
            
            if model_unavailable(exc) and switch_model():
                continue
                
            if attempt >= MAX_RETRIES + 4:
                break
            
            time.sleep(min(30, 2 ** attempt)) # انتظار أطول قليلاً بين المحاولات
            
    raise RuntimeError(f"تعذرت المعالجة بعد عدة محاولات. قد يكون هناك ضغط على الخدمة حالياً.")




def wait_for_file(uploaded):
    start = time.monotonic()
    while time.monotonic() - start < FILE_TIMEOUT:
        current = client.files.get(name=uploaded.name)
        state = getattr(current, "state", None)
        state_name = getattr(state, "name", str(state or "")).upper().split(".")[-1]
        if state_name in ("ACTIVE", "SUCCEEDED"):
            return current
        if state_name in ("FAILED", "CANCELLED"):
            raise RuntimeError(f"فشل تجهيز الملف لدى Gemini ({state_name}).")
        time.sleep(3)
    raise TimeoutError("استغرق تجهيز الملف وقتًا أطول من المتوقع.")


def download_telegram_file(file_id, destination):
    telegram_file = bot.get_file(file_id)
    file_path = getattr(telegram_file, "file_path", None)
    if not file_path:
        raise RuntimeError("تعذر الحصول على مسار الملف من Telegram.")
    url = f"https://api.telegram.org/file/bot{TELEGRAM_TOKEN}/{file_path}"
    try:
        with requests.get(url, timeout=(30, 240), stream=True) as response:
            response.raise_for_status()
            total = 0
            with open(destination, "wb") as output:
                for chunk in response.iter_content(chunk_size=1024 * 512):
                    if not chunk:
                        continue
                    total += len(chunk)
                    if total > MAX_FILE_BYTES:
                        raise RuntimeError(f"حجم الملف أكبر من الحد الحالي ({MAX_FILE_MB} MB).")
                    output.write(chunk)
    except requests.RequestException:
        raise RuntimeError("فشل تنزيل الملف من Telegram. تحقق من الاتصال وحجم الملف.") from None


def safe_ext(name, fallback):
    ext = Path(name or "").suffix.lower()
    if not ext or len(ext) > 10 or not ext[1:].replace("-", "").isalnum():
        return fallback
    return ext


def detect_media(message):
    if message.content_type == "voice":
        size = getattr(message.voice, "file_size", 0) or 0
        return message.voice.file_id, ".ogg", "audio", "audio/ogg", size
    if message.content_type == "audio":
        name = message.audio.file_name or "audio.mp3"
        ext = safe_ext(name, ".mp3")
        mime = MIME_BY_EXT.get(ext, "audio/mpeg")
        size = getattr(message.audio, "file_size", 0) or 0
        return message.audio.file_id, ext, "audio", mime, size
    if message.content_type == "photo":
        photo = message.photo[-1]
        size = getattr(photo, "file_size", 0) or 0
        return photo.file_id, ".jpg", "image", "image/jpeg", size
    if message.content_type == "document":
        doc = message.document
        name = (doc.file_name or "").lower()
        mime = (doc.mime_type or "").lower()
        ext = safe_ext(name, "")
        size = getattr(doc, "file_size", 0) or 0
        if mime == "application/pdf" or ext == ".pdf":
            return doc.file_id, ".pdf", "pdf", "application/pdf", size
        if mime.startswith("audio/") or ext in AUDIO_EXTS:
            return doc.file_id, ext or ".audio", "audio", mime or MIME_BY_EXT.get(ext, "audio/mpeg"), size
        if mime.startswith("image/") or ext in IMAGE_EXTS:
            return doc.file_id, ext or ".jpg", "image", mime or MIME_BY_EXT.get(ext, "image/jpeg"), size
    return None


def prompt_for_media(kind, caption):
    extra = ""
    if caption:
        extra = f"\nملاحظة من المستخدم (استخدمها للفهم فقط): {caption}"
    
    if kind == "audio":
        return f"""أنت خبير محترف في تفريغ الأشرطة الصوتية (Transcriber). 
مهمتك: كتابة كل كلمة تسمعها في الملف الصوتي بدقة 100% دون زيادة أو نقصان.
القيود الصارمة:
- لا تلخص المحتوى.
- لا تحاول تصحيح أخطاء المتحدث، اكتب ما يقوله حرفياً.
- إذا كان هناك جزء غير مفهوم، اكتب [غير واضح].
- لا تضف أي نص خارجي أو مقدمات مثل "إليك التفريغ".
- ابدأ بالتفريغ مباشرة.{extra}"""
    
    if kind == "pdf":
        return f"""استخرج النص من ملف PDF بدقة عالية جداً. حافظ على ترتيب الفقرات والعناوين. {extra}"""
    
    return f"""استخرج كل النصوص الموجودة في هذه الصورة بدقة. إذا كان هناك نص مكتوب بخط اليد حاول قراءته بعناية. {extra}"""


def send_typing_continuous(chat_id, stop_event):
    """دالة لإبقاء حالة 'جاري الكتابة' نشطة"""
    while not stop_event.is_set():
        try:
            bot.send_chat_action(chat_id, "typing")
            time.sleep(4)
        except:
            break

def process_media(message):
    chat_id = message.chat.id
    user_id = message.from_user.id
    waiting = bot.reply_to(message, "⏳ بدأت باستلام الملف، لحظات من فضلك...", reply_markup=main_keyboard())
    local_path = None
    uploaded = None
    stop_typing = threading.Event()
    
    # تشغيل مؤشر الكتابة في الخلفية لضمان عدم تعليق البوت
    typing_thread = threading.Thread(target=send_typing_continuous, args=(chat_id, stop_typing))
    typing_thread.start()

    try:
        media = detect_media(message)
        if not media:
            update_status(chat_id, waiting.message_id, "⚠️ نوع الملف غير مدعوم.")
            return

        file_id, extension, media_kind, mime_type, file_size = media
        
        if file_size and file_size > MAX_FILE_BYTES:
            raise RuntimeError(f"الملف كبير جداً ({file_size/(1024*1024):.1f}MB). الحد هو {MAX_FILE_MB}MB.")

        update_status(chat_id, waiting.message_id, "⬇️ جاري تحميل الملف من تليجرام...")
        fd, local_path = tempfile.mkstemp(prefix="study_", suffix=extension)
        os.close(fd)
        download_telegram_file(file_id, local_path)

        update_status(chat_id, waiting.message_id, "☁️ جاري الرفع لـ Gemini للتحليل...")
        uploaded = client.files.upload(file=local_path, config={"mime_type": mime_type})
        
        update_status(chat_id, waiting.message_id, "⚙️ جاري معالجة الملف صوتياً (قد يستغرق دقيقة)...")
        active_file = wait_for_file(uploaded)
        
        update_status(chat_id, waiting.message_id, "🧠 جاري استخراج النص النهائي...")
        content = generate_text(prompt_for_media(media_kind, message.caption), active_file)
        
        if not content or len(content.strip()) < 2:
            raise RuntimeError("فشل الموديل في استخراج نص واضح.")

        save_content(user_id, content, source_label(media_kind))
        
        bot.delete_message(chat_id, waiting.message_id)
        send_content_preview(chat_id, content, source_label(media_kind))
        bot.send_message(chat_id, "✅ اكتمل التجهيز! اختر ماذا تريد أن أفعل بالنص:", reply_markup=task_keyboard())

    except Exception as exc:
        err_msg = safe_error(exc)
        print(f"[Process Error] {err_msg}")
        update_status(chat_id, waiting.message_id, f"❌ حدث خطأ: {err_msg}")
    finally:
        stop_typing.set() # إيقاف مؤشر الكتابة
        if uploaded:
            try: client.files.delete(name=uploaded.name)
            except: pass
        if local_path and os.path.exists(local_path):
            try: os.remove(local_path)
            except: pass



def language_instruction(language_code):
    if language_code == "auto":
        return "اكتب النتيجة بلغة المحتوى الأصلية."
    language = LANGUAGES.get(language_code, "العربية")
    return f"اكتب النتيجة باللغة التالية فقط: {language}. حافظ على المعنى والمصطلحات المهمة."


def run_task(call, task_key):
    user_id = call.from_user.id
    chat_id = call.message.chat.id
    task = TASKS.get(task_key)
    with SESSION_LOCK:
        session = SESSIONS.get(user_id, {}).copy()
    content = session.get("content", "").strip()
    if not content:
        bot.answer_callback_query(call.id, "أرسل تسجيلًا أو PDF أو صورة أو نصًا أولًا.", show_alert=True)
        return
    if not task:
        bot.answer_callback_query(call.id, "الخيار غير معروف.", show_alert=True)
        return
    bot.answer_callback_query(call.id, "بدأت المعالجة...")
    wait = bot.send_message(chat_id, f"⏳ جارٍ إعداد: {task[0]}...")
    prompt = f"""أنت مساعد دراسة دقيق. استخدم المحتوى الذي يرسله المستخدم أساسًا للإجابة، ولا تضف حقائق على أنها من المصدر إذا لم ترد فيه. إذا كان المحتوى ناقصًا أو غير واضح فاذكر ذلك بصراحة.
المهمة: {task[1]}
{language_instruction(session.get('language', 'auto'))}
استخدم تنسيقًا واضحًا وعناوين مناسبة. لا تذكر أنك نموذج ذكاء اصطناعي، ولا تكرر الطلب.

المحتوى:
---
{content}
---"""
    try:
        try:
            bot.send_chat_action(chat_id, "typing")
        except Exception:
            pass
        result = generate_text(prompt)
        try:
            bot.delete_message(chat_id, wait.message_id)
        except Exception:
            pass
        send_long_text(chat_id, result, prefix=task[0])
        bot.send_message(chat_id, "تقدر تختار طريقة ثانية لنفس المحتوى من الأزرار:", reply_markup=task_keyboard())
    except Exception as exc:
        update_status(chat_id, wait.message_id, "❌ تعذرت معالجة الطلب.\n\n" + safe_error(exc))


# =========================================================
# أوامر Telegram والأزرار
# =========================================================
@bot.message_handler(commands=["start", "help"])
def start(message):
    text = (
        "🤖 أهلًا بك في مساعد الدراسة الذكي!\n\n"
        "أرسل لي أحد الأشياء التالية:\n"
        "🎙️ تسجيلًا صوتيًا أو ملفًا صوتيًا\n"
        "📄 ملف PDF\n"
        "🖼️ صورة لدرس أو سبورة أو شريحة\n"
        "📝 أو الصق نص الدرس مباشرة\n\n"
        "بعد قراءة المحتوى تقدر تختار ملخصًا أو أسئلة اختبار أو بطاقات مذاكرة أو شرحًا مبسطًا.\n\n"
        "🌍 تقدر تغيّر لغة النتائج من زر اللغة."
    )
    bot.send_message(message.chat.id, text, reply_markup=main_keyboard())


@bot.message_handler(commands=["cancel"])
def cancel(message):
    bot.send_message(message.chat.id, "تم. إذا تبي تبدأ من جديد أرسل نصًا أو ملفًا آخر.", reply_markup=main_keyboard())


@bot.callback_query_handler(func=lambda call: call.data.startswith("task:"))
def task_callback(call):
    run_task(call, call.data.split(":", 1)[1])


@bot.callback_query_handler(func=lambda call: call.data == "menu:language")
def language_menu_callback(call):
    bot.answer_callback_query(call.id)
    bot.send_message(call.message.chat.id, "اختر لغة النتائج:", reply_markup=language_keyboard())


@bot.callback_query_handler(func=lambda call: call.data == "menu:fulltext")
def fulltext_callback(call):
    user_id = call.from_user.id
    with SESSION_LOCK:
        session = SESSIONS.get(user_id, {}).copy()
    content = session.get("content", "").strip()
    if not content:
        bot.answer_callback_query(call.id, "ما فيه محتوى محفوظ حاليًا.", show_alert=True)
        return
    bot.answer_callback_query(call.id, "أرسل النص الكامل.")
    send_long_text(call.message.chat.id, content, prefix=f"📝 النص المستخرج من {session.get('source') or 'المحتوى'}")


@bot.callback_query_handler(func=lambda call: call.data.startswith("lang:"))
def language_callback(call):
    code = call.data.split(":", 1)[1]
    if code not in LANGUAGES:
        bot.answer_callback_query(call.id, "اللغة غير مدعومة.", show_alert=True)
        return
    with SESSION_LOCK:
        session = get_session(call.from_user.id)
        session["language"] = code
    bot.answer_callback_query(call.id, "تم حفظ اللغة.")
    bot.send_message(call.message.chat.id, f"✅ لغة النتائج: {LANGUAGES[code]}", reply_markup=main_keyboard())


@bot.callback_query_handler(func=lambda call: call.data == "menu:home")
def home_callback(call):
    bot.answer_callback_query(call.id)
    bot.send_message(call.message.chat.id, "أرسل صوتًا أو PDF أو صورة أو نصًا، ثم اختر طريقة المذاكرة.", reply_markup=main_keyboard())


@bot.message_handler(content_types=["voice", "audio", "document", "photo"])
def media_handler(message):
    process_media(message)


@bot.message_handler(content_types=["sticker", "video", "video_note", "animation", "contact", "location"])
def unsupported_handler(message):
    bot.reply_to(
        message,
        "هذا النوع غير مدعوم حاليًا. أرسل تسجيلًا صوتيًا أو PDF أو صورة أو نصًا.",
        reply_markup=main_keyboard(),
    )


@bot.message_handler(content_types=["text"])
def text_handler(message):
    text = (message.text or "").strip()
    chat_id = message.chat.id
    user_id = message.from_user.id
    if text == "🏠 القائمة":
        bot.send_message(chat_id, "🏠 القائمة الرئيسية\nأرسل تسجيلًا صوتيًا أو PDF أو صورة أو الصق نص الدرس.", reply_markup=main_keyboard())
        return
    if text == "🌍 اللغة":
        bot.send_message(chat_id, "اختر لغة النتائج التي تريدها:", reply_markup=language_keyboard())
        return
    if text == "📖 كيف أستخدمه؟":
        bot.send_message(
            chat_id,
            "1) أرسل تسجيلًا صوتيًا أو PDF أو صورة أو نصًا.\n"
            "2) انتظر تجهيز المحتوى.\n"
            "3) اختر إحدى أدوات الدراسة.\n"
            "4) تقدر تعيد استخدام نفس المحتوى وتختار أداة ثانية.\n\n"
            "ملاحظة: الجلسة تحفظ المحتوى مؤقتًا في ذاكرة البرنامج، وتُمسح عند إعادة تشغيله.",
            reply_markup=main_keyboard(),
        )
        return
    if text == "🧹 مسح المحتوى":
        with SESSION_LOCK:
            language = get_session(user_id).get("language", "auto")
            SESSIONS[user_id] = {"content": "", "language": language, "source": ""}
        bot.send_message(chat_id, "🧹 تم مسح المحتوى المحفوظ لهذه الجلسة.", reply_markup=main_keyboard())
        return
    if text.startswith("/"):
        return
    if len(text) < 3:
        bot.reply_to(message, "أرسل نصًا أطول قليلًا أو ملفًا صوتيًا أو PDF أو صورة.", reply_markup=main_keyboard())
        return
    if len(text) > 50000:
        bot.reply_to(message, "النص طويل جدًا لرسالة واحدة. أرسل جزءًا أقصر أو ملف PDF.", reply_markup=main_keyboard())
        return
    save_content(user_id, text, "نص مرسل")
    bot.send_message(chat_id, "✅ حفظت النص. اختر كيف تبي تذاكره:", reply_markup=task_keyboard())


# =========================================================
# خادم صحة للاستضافة
# =========================================================
app = Flask(__name__)


@app.route("/")
def home():
    return "Study bot is alive", 200


@app.route("/health")
def health():
    return {"status": "ok", "model": CURRENT_MODEL}, 200


def run_flask():
    app.run(host="0.0.0.0", port=PORT, debug=False, use_reloader=False)


def keep_alive():
    threading.Thread(target=run_flask, daemon=True).start()


def run():
    try:
        bot.set_my_commands([
            types.BotCommand("start", "بدء البوت"),
            types.BotCommand("help", "طريقة الاستخدام"),
            types.BotCommand("cancel", "إلغاء"),
        ])
    except Exception as exc:
        print(f"[Commands] {safe_error(exc)}")
    print("=" * 55)
    print("Study bot starting")
    print(f"Gemini model: {CURRENT_MODEL}")
    print(f"Max file size: {MAX_FILE_MB} MB")
    print("Ready: audio, PDF, photos, and text.")
    print("=" * 55)
    while True:
        try:
            bot.infinity_polling(timeout=60, long_polling_timeout=20, skip_pending=True)
        except KeyboardInterrupt:
            print("Bot stopped by user.")
            break
        except Exception as exc:
            print(f"[Telegram Error] {safe_error(exc)}")
            time.sleep(5)


if __name__ == "__main__":
    keep_alive()
    run()