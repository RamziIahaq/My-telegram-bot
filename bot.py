import os
import sys
import time
import random
import tempfile
import requests
import telebot

from dotenv import load_dotenv
from google import genai
from telebot import apihelper

# =========================================================
# تحميل المفاتيح من ملف .env الذي قمنا بإنشائه
# =========================================================
load_dotenv()

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")

if not TELEGRAM_TOKEN or not GEMINI_API_KEY:
    print("❌ خطأ: لم يتم العثور على المفاتيح! تأكد من إعداد ملف .env بشكل صحيح.")
    sys.exit(1)

# =========================================================
# الإعدادات الأساسية
# =========================================================
# الموديل الصحيح المدعوم للصوتيات
MODEL = "gemini-1.5-flash"

MAX_RETRIES = 8
FILE_TIMEOUT = 300
TELEGRAM_CHUNK = 3800
BOT_THREADS = 4

apihelper.CONNECT_TIMEOUT = 60
apihelper.READ_TIMEOUT = 300

# =========================================================
# إنشاء العملاء
# =========================================================
bot = telebot.TeleBot(
    TELEGRAM_TOKEN,
    threaded=True,
    num_threads=BOT_THREADS
)

client = genai.Client(
    api_key=GEMINI_API_KEY
)

# =========================================================
# الوظائف المساعدة
# =========================================================
def status(chat_id, message_id, text):
    try:
        bot.edit_message_text(text, chat_id=chat_id, message_id=message_id)
    except Exception:
        pass

def delete_message(chat_id, message_id):
    try:
        bot.delete_message(chat_id=chat_id, message_id=message_id)
    except Exception:
        pass

def retryable(error):
    text = str(error).upper()
    words = [
        "429", "500", "502", "503", "504",
        "RESOURCE_EXHAUSTED", "UNAVAILABLE", "OVERLOADED",
        "TIMEOUT", "TIMED OUT", "CONNECTION RESET", "CONNECTION ERROR"
    ]
    return any(word in text for word in words)

def wait_time(attempt):
    base = min(60, 5 * (2 ** (attempt - 1)))
    return base + random.uniform(0, 2)

def wait_for_file(file):
    start = time.time()
    while True:
        if time.time() - start > FILE_TIMEOUT:
            raise TimeoutError("انتهى وقت تجهيز الملف في Gemini.")

        current = client.files.get(name=file.name)
        state_str = str(getattr(current, "state", "")).upper()

        if "ACTIVE" in state_str:
            return current

        if "FAILED" in state_str:
            raise RuntimeError("Gemini فشل في معالجة الملف الصوتي.")

        time.sleep(4)

# =========================================================
# تفريغ الصوت
# =========================================================
def transcribe(file, chat_id, message_id):
    prompt = """
قم بتفريغ جميع الكلمات الموجودة في التسجيل الصوتي إلى نص كامل.

التعليمات:
1. لا تلخص التسجيل.
2. لا تحذف أي كلام مسموع.
3. حافظ على ترتيب الكلام.
4. استخدم نفس لغة المتحدث.
5. لا تضف معلومات من عندك.
6. إذا كان هناك أكثر من متحدث، حاول فصل كلامهم.
7. لا تكتب شرحًا أو مقدمة.
8. أعد النص المفرغ فقط متبوعاً بأهم النقاط المستخلصة إن أمكن.
""".strip()

    for attempt in range(1, MAX_RETRIES + 1):
        try:
            status(chat_id, message_id, f"✍️ جاري معالجة الصوت بالذكاء الاصطناعي...\nالمحاولة {attempt}/{MAX_RETRIES}")

            response = client.models.generate_content(
                model=MODEL,
                contents=[prompt, file]
            )

            text = getattr(response, "text", None)

            if text and text.strip():
                return text.strip()

            raise RuntimeError("Gemini لم يرجع نصًا.")

        except Exception as exc:
            print(f"Gemini Error: {exc}")
            if not retryable(exc):
                raise
            if attempt >= MAX_RETRIES:
                raise RuntimeError("تعذر التفريغ بعد عدة محاولات.")

            delay = wait_time(attempt)
            status(chat_id, message_id, f"⏳ الخدمة مشغولة حاليًا.\nإعادة المحاولة بعد {delay:.0f} ثانية...")
            time.sleep(delay)

    raise RuntimeError("فشل التفريغ.")

# =========================================================
# تقسيم وإرسال النص الطويل
# =========================================================
def split_text(text):
    if len(text) <= TELEGRAM_CHUNK:
        return [text]

    result = []
    current = ""

    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue

        if len(line) > TELEGRAM_CHUNK:
            if current:
                result.append(current.strip())
                current = ""
            while len(line) > TELEGRAM_CHUNK:
                result.append(line[:TELEGRAM_CHUNK])
                line = line[TELEGRAM_CHUNK:]
            current = line
            continue

        candidate = (current + "\n\n" + line) if current else line
        if len(candidate) <= TELEGRAM_CHUNK:
            current = candidate
        else:
            if current:
                result.append(current.strip())
            current = line

    if current:
        result.append(current.strip())
    return result

def send_text(chat_id, text):
    parts = split_text(text)
    total = len(parts)

    for number, part in enumerate(parts, start=1):
        if total == 1:
            message = "📝 النص المفرغ:\n\n" + part
        else:
            message = f"📝 النص المفرغ ({number}/{total})\n\n" + part

        bot.send_message(chat_id, message)
        time.sleep(0.3)

# =========================================================
# تنزيل الملف ومعالجته
# =========================================================
def download_file(file_id, path):
    telegram_file = bot.get_file(file_id)
    url = f"https://api.telegram.org/file/bot{TELEGRAM_TOKEN}/{telegram_file.file_path}"
    response = requests.get(url, timeout=(30, 300), stream=True)
    response.raise_for_status()

    with open(path, "wb") as output:
        for chunk in response.iter_content(chunk_size=1024 * 1024):
            if chunk:
                output.write(chunk)

def process_audio(message):
    chat_id = message.chat.id
    waiting = None
    uploaded = None
    temp_file = None

    try:
        waiting = bot.reply_to(message, "⏳ جاري استلام التسجيل...")

        if message.content_type == "voice":
            file_id = message.voice.file_id
            extension = ".ogg"
        elif message.content_type == "audio":
            file_id = message.audio.file_id
            filename = message.audio.file_name or "audio.mp3"
            extension = os.path.splitext(filename)[1] or ".mp3"
        else:
            raise RuntimeError("نوع الملف غير مدعوم.")

        fd, temp_file = tempfile.mkstemp(prefix="audio_", suffix=extension)
        os.close(fd)

        status(chat_id, waiting.message_id, "⬇️ جاري التنزيل والمعالجة...")
        download_file(file_id, temp_file)

        size = os.path.getsize(temp_file)
        if size <= 0:
            raise RuntimeError("الملف الصوتي فارغ.")

        status(chat_id, waiting.message_id, "☁️ يتم الآن الرفع إلى جيمناي للتحليل...")
        uploaded = client.files.upload(file=temp_file)

        active_file = wait_for_file(uploaded)
        text = transcribe(active_file, chat_id, waiting.message_id)

        if not text:
            raise RuntimeError("لم يتم استخراج نص من التسجيل.")

        delete_message(chat_id, waiting.message_id)
        send_text(chat_id, text)

    except Exception as exc:
        print(f"[ERROR] {exc}")
        error = str(exc)
        if len(error) > 500:
            error = error[:500] + "..."

        if waiting:
            status(chat_id, waiting.message_id, ("❌ حدث خطأ:\n\n" + error))
        else:
            try:
                bot.send_message(chat_id, "❌ حدث خطأ أثناء معالجة التسجيل.")
            except Exception:
                pass

    finally:
        if uploaded:
            try:
                client.files.delete(name=uploaded.name)
            except Exception:
                pass
        if temp_file and os.path.exists(temp_file):
            try:
                os.remove(temp_file)
            except Exception:
                pass

# =========================================================
# أوامر البوت
# =========================================================
@bot.message_handler(commands=["start", "help"])
def start(message):
    bot.reply_to(
        message,
        "🤖 أهلًا بك!\n\n"
        "🎙️ أرسل لي أي تسجيل صوتي أو ملف مقطع صوتي.\n"
        "وسأقوم بتفريغه كاملًا إلى نص بدقة عالية."
    )

@bot.message_handler(content_types=["audio", "voice"])
def audio(message):
    process_audio(message)

@bot.message_handler(func=lambda message: True)
def other(message):
    bot.reply_to(message, "🎙️ أرسل تسجيلًا صوتيًا حتى أقوم بتفريغه.")

# =========================================================
# تشغيل البوت
# =========================================================
def run():
    print("=" * 55)
    print("🤖 BOT STARTED SUCCESSFULLY")
    print(f"🧠 Model: {MODEL}")
    print(f"🧵 Workers: {BOT_THREADS}")
    print("🚀 البوت يعمل الآن ويقرأ من ملف .env...")
    print("📱 افتح Telegram وأرسل /start")
    print("=" * 55)

    while True:
        try:
            bot.infinity_polling(timeout=60, long_polling_timeout=60, skip_pending=True)
        except KeyboardInterrupt:
            print("🛑 تم إيقاف البوت.")
            break
        except Exception as exc:
            print(f"❌ Telegram Error: {exc}")
            print("🔄 إعادة التشغيل بعد 5 ثوانٍ...")
            time.sleep(5)

if __name__ == "__main__":
    run()