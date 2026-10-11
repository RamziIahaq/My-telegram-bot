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
MODEL = os.getenv("GEMINI_MODEL", "gemini-3.8-flash").strip()
PORT = int(os.getenv("PORT", "8080"))
MAX_FILE_MB = int(os.getenv("MAX_FILE_MB", "19"))
MAX_FILE_BYTES = MAX_FILE_MB * 1024 * 1024
MAX_RETRIES = 3
FILE_TIMEOUT = 300
TELEGRAM_CHUNK = 3500
BOT_THREADS = 4

if not TELEGRAM_TOKEN or not GEMINI_API_KEY:
    print("خطأ: اضبط TELEGRAM_BOT_TOKEN و GEMINI_API_KEY في متغيرات البيئة.")
    sys.exit(1)

apihelper.CONNECT_TIMEOUT = 60
apihelper.READ_TIMEOUT = 300
bot = telebot.TeleBot(TELEGRAM_TOKEN, threaded=True, num_threads=BOT_THREADS)
client = genai.Client(api_key=GEMINI_API_KEY)

# جلسات في الذاكرة؛ تُمسح عند إعادة تشغيل البرنامج.
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


def safe_error(error):
    text = str(error)
    for secret in (TELEGRAM_TOKEN, GEMINI_API_KEY):
        if secret:
            text = text.replace(secret, "[REDACTED]")
    # لا نعرض تفاصيل داخلية طويلة للمستخدم
    return text[:700]


def get_session(user_id):
    with SESSION_LOCK:
        return SESSIONS.setdefault(user_id, {"content": "", "language": "auto", "source": ""})


def main_keyboard():
    keyboard = types.ReplyKeyboardMarkup(resize_keyboard=True, row_width=2)
    keyboard.add(types.KeyboardButton("🏠 القائمة"), types.KeyboardButton("🌍 اللغة"))
    keyboard.add(types.KeyboardButton("📖 كيف أستخدمه؟"), types.KeyboardButton("🧹 مسح المحتوى"))
    return keyboard


def task_keyboard():
    keyboard = types.InlineKeyboardMarkup(row_width=2)
    buttons = [types.InlineKeyboardButton(label, callback_data=f"task:{key}") for key, (label, _) in TASKS.items()]
    keyboard.add(*buttons)
    keyboard.add(types.InlineKeyboardButton("🌍 تغيير لغة النتيجة", callback_data="menu:language"))
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
        bot.send_message(chat_id, f"{header}\n\n{part}" if header else part, reply_markup=main_keyboard(), timeout=60)
        time.sleep(0.15)


def update_status(chat_id, message_id, text):
    try:
        bot.edit_message_text(text, chat_id, message_id)
    except Exception:
        pass


def retryable(error):
    value = safe_error(error).upper()
    return any(term in value for term in ["408", "425", "429", "500", "502", "503", "504", "RESOURCE_EXHAUSTED", "UNAVAILABLE", "OVERLOADED", "DEADLINE_EXCEEDED", "TIMEOUT", "TIMED OUT", "CONNECTION RESET", "CONNECTION ERROR"])


def generate_text(prompt, contents=None):
    last_error = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            payload = [prompt] if contents is None else [prompt, contents]
            response = client.models.generate_content(model=MODEL, contents=payload)
            result = getattr(response, "text", None)
            if result and result.strip():
                return result.strip()
            raise RuntimeError("لم يرجع النموذج نصًا.")
        except Exception as exc:
            last_error = exc
            if attempt >= MAX_RETRIES or not retryable(exc):
                break
            time.sleep(min(20, 2 ** attempt) + random.uniform(0, 1))
    raise RuntimeError(f"تعذرت معالجة الطلب: {safe_error(last_error)}") from last_error


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


def detect_media(message):
    if message.content_type == "voice":
        return message.voice.file_id, ".ogg", "audio"
    if message.content_type == "audio":
        name = message.audio.file_name or "audio.mp3"
        ext = Path(name).suffix.lower()
        if not ext or len(ext) > 10 or not ext[1:].isalnum():
            ext = ".audio"
        return message.audio.file_id, ext, "audio"
    if message.content_type == "document":
        doc = message.document
        name = doc.file_name or ""
        mime = (doc.mime_type or "").lower()
        if mime == "application/pdf" or name.lower().endswith(".pdf"):
            return doc.file_id, ".pdf", "pdf"
    return None


def process_media(message):
    chat_id = message.chat.id
    user_id = message.from_user.id
    waiting = bot.reply_to(message, "⏳ استلمت الملف، جارٍ التجهيز...", reply_markup=main_keyboard())
    local_path = None
    uploaded = None
    try:
        media = detect_media(message)
        if not media:
            bot.send_message(chat_id, "أستطيع حاليًا معالجة التسجيلات الصوتية وملفات PDF. أرسل ملف PDF أو صوتًا.", reply_markup=main_keyboard())
            return
        file_id, extension, media_kind = media
        fd, local_path = tempfile.mkstemp(prefix="studybot_", suffix=extension)
        os.close(fd)
        update_status(chat_id, waiting.message_id, "⬇️ جارٍ تنزيل الملف...")
        download_telegram_file(file_id, local_path)
        if os.path.getsize(local_path) == 0:
            raise RuntimeError("الملف فارغ.")
        update_status(chat_id, waiting.message_id, "☁️ جارٍ إرسال الملف إلى خدمة الذكاء الاصطناعي...")
        uploaded = client.files.upload(file=local_path)
        active_file = wait_for_file(uploaded)
        if media_kind == "audio":
            prompt = """فرّغ التسجيل الصوتي إلى نص كامل قدر الإمكان.
التعليمات: لا تلخص ولا تحذف الكلام المسموع، حافظ على لغة المتحدث الأصلية وترتيب الأفكار، استخدم علامات ترقيم وفقرات مناسبة، وافصل المتحدثين إذا أمكن. لا تخترع كلمات غير مسموعة؛ ضع [غير واضح] عند تعذر الفهم. أخرج النص المفرغ فقط."""
            source_name = "تسجيل صوتي"
        else:
            prompt = """استخرج النص والمعلومات الدراسية من ملف PDF بدقة وبالترتيب. حافظ على العناوين والنقاط والمعادلات بقدر الإمكان. إذا كان الملف ممسوحًا ضوئيًا فحاول قراءة النص الظاهر. لا تلخص المحتوى في هذه الخطوة ولا تخترع أجزاء غير واضحة؛ أشر إلى مواضع النص غير المقروء عند الحاجة. أخرج النص المستخرج فقط."""
            source_name = "ملف PDF"
        update_status(chat_id, waiting.message_id, "🧠 جارٍ قراءة المحتوى وتجهيزه للمذاكرة...")
        content = generate_text(prompt, active_file)
        if len(content.strip()) < 5:
            raise RuntimeError("لم أتمكن من استخراج نص كافٍ من الملف.")
        with SESSION_LOCK:
            SESSIONS[user_id] = {"content": content, "language": get_session(user_id).get("language", "auto"), "source": source_name}
        try:
            bot.delete_message(chat_id, waiting.message_id)
        except Exception:
            pass
        # أرسل النص المستخرج ثم خيارات الدراسة
        send_long_text(chat_id, content, prefix=f"📝 النص المستخرج من {source_name}")
        bot.send_message(chat_id, "✅ جهّزت المحتوى! اختر كيف تبي تذاكره:", reply_markup=task_keyboard())
    except Exception as exc:
        print(f"[Media Error] {safe_error(exc)}")
        update_status(chat_id, waiting.message_id, "❌ ما قدرت أعالج الملف.\n\n" + safe_error(exc))
    finally:
        if uploaded:
            try:
                client.files.delete(name=uploaded.name)
            except Exception as exc:
                print(f"[Cleanup] تعذر حذف الملف البعيد: {type(exc).__name__}")
        if local_path and os.path.exists(local_path):
            try:
                os.remove(local_path)
            except OSError:
                pass


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
        bot.answer_callback_query(call.id, "أرسل تسجيلًا أو PDF أو نصًا أولًا.", show_alert=True)
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
        "📝 أو الصق نص الدرس مباشرة\n\n"
        "بعد قراءة المحتوى، تقدر تختار ملخصًا أو أسئلة اختبار أو بطاقات مذاكرة أو شرحًا مبسطًا.\n\n"
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
    bot.send_message(call.message.chat.id, "أرسل صوتًا أو PDF أو نصًا، ثم اختر طريقة المذاكرة.", reply_markup=main_keyboard())


@bot.message_handler(content_types=["voice", "audio", "document"])
def media_handler(message):
    process_media(message)


@bot.message_handler(content_types=["text"])
def text_handler(message):
    text = (message.text or "").strip()
    chat_id = message.chat.id
    user_id = message.from_user.id
    if text == "🏠 القائمة":
        bot.send_message(chat_id, "🏠 القائمة الرئيسية\nأرسل تسجيلًا صوتيًا أو PDF أو الصق نص الدرس.", reply_markup=main_keyboard())
        return
    if text == "🌍 اللغة":
        bot.send_message(chat_id, "اختر لغة النتائج التي تريدها:", reply_markup=language_keyboard())
        return
    if text == "📖 كيف أستخدمه؟":
        bot.send_message(chat_id, "1) أرسل تسجيلًا صوتيًا أو PDF أو نصًا.\n2) انتظر تجهيز المحتوى.\n3) اختر إحدى أدوات الدراسة.\n4) تستطيع إعادة استخدام المحتوى واختيار أداة أخرى.\n\nملاحظة: الجلسة تحفظ المحتوى في ذاكرة البرنامج مؤقتًا، وتُمسح عند إعادة تشغيله.", reply_markup=main_keyboard())
        return
    if text == "🧹 مسح المحتوى":
        with SESSION_LOCK:
            SESSIONS[user_id] = {"content": "", "language": get_session(user_id).get("language", "auto"), "source": ""}
        bot.send_message(chat_id, "🧹 تم مسح المحتوى المحفوظ لهذه الجلسة.", reply_markup=main_keyboard())
        return
    if text.startswith("/"):
        return
    if len(text) < 3:
        bot.reply_to(message, "أرسل نصًا أطول قليلًا أو ملفًا صوتيًا أو PDF.", reply_markup=main_keyboard())
        return
    if len(text) > 50000:
        bot.reply_to(message, "النص طويل جدًا لرسالة واحدة. أرسل جزءًا أقصر أو ملف PDF.", reply_markup=main_keyboard())
        return
    with SESSION_LOCK:
        old_language = get_session(user_id).get("language", "auto")
        SESSIONS[user_id] = {"content": text, "language": old_language, "source": "نص مرسل"}
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
    return {"status": "ok"}, 200


def run_flask():
    app.run(host="0.0.0.0", port=PORT, debug=False, use_reloader=False)


def keep_alive():
    threading.Thread(target=run_flask, daemon=True).start()


def run():
    print("=" * 55)
    print("Study bot starting")
    print(f"Gemini model: {MODEL}")
    print(f"Max file size: {MAX_FILE_MB} MB")
    print("Ready to receive audio, PDF, and text.")
    print("=" * 55)
    while True:
        try:
            bot.infinity_polling(timeout=60, long_polling_timeout=20, skip_pending=False)
        except KeyboardInterrupt:
            print("Bot stopped by user.")
            break
        except Exception as exc:
            print(f"[Telegram Error] {safe_error(exc)}")
            time.sleep(5)


if __name__ == "__main__":
    keep_alive()
    run()