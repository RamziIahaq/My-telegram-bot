import os
import sys
import time
import random
import tempfile
import threading

import requests
import telebot
from flask import Flask
from dotenv import load_dotenv
from google import genai
from telebot import apihelper


# =========================================================
# 1. تحميل الإعدادات والمفاتيح السرية
# =========================================================

# تحميل ملف .env إن كان موجودًا في مجلد التشغيل الحالي.
# في الاستضافة، يمكن تعريف المتغيرات من لوحة التحكم مباشرة.
load_dotenv()

TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")

if not TELEGRAM_TOKEN or not GEMINI_API_KEY:
    print(
        "خطأ: تأكد من ضبط TELEGRAM_BOT_TOKEN "
        "وGEMINI_API_KEY في متغيرات البيئة."
    )
    sys.exit(1)


# =========================================================
# 2. الإعدادات الأساسية
# =========================================================

MODEL = "gemini-3.8-flash"

MAX_RETRIES = 5
FILE_TIMEOUT = 300
TELEGRAM_CHUNK = 3800
BOT_THREADS = 4

apihelper.CONNECT_TIMEOUT = 60
apihelper.READ_TIMEOUT = 300


# =========================================================
# 3. إنشاء العملاء
# =========================================================

bot = telebot.TeleBot(
    TELEGRAM_TOKEN,
    threaded=True,
    num_threads=BOT_THREADS
)

client = genai.Client(api_key=GEMINI_API_KEY)


# =========================================================
# 4. وظائف مساعدة
# =========================================================

def safe_error(error):
    """إخفاء المفاتيح السرية من رسائل الأخطاء."""
    text = str(error)

    for secret in (TELEGRAM_TOKEN, GEMINI_API_KEY):
        if secret:
            text = text.replace(secret, "[REDACTED]")

    return text


def status(chat_id, message_id, text):
    """تحديث رسالة حالة المعالجة."""
    try:
        bot.edit_message_text(
            text,
            chat_id=chat_id,
            message_id=message_id
        )
    except Exception:
        pass


def delete_message(chat_id, message_id):
    """حذف رسالة الحالة."""
    try:
        bot.delete_message(
            chat_id=chat_id,
            message_id=message_id
        )
    except Exception:
        pass


def retryable(error):
    """تحديد بعض الأخطاء التي قد تكون مؤقتة."""
    text = safe_error(error).upper()

    words = [
        "408",
        "425",
        "429",
        "500",
        "502",
        "503",
        "504",
        "RESOURCE_EXHAUSTED",
        "UNAVAILABLE",
        "OVERLOADED",
        "DEADLINE_EXCEEDED",
        "INTERNAL",
        "TIMEOUT",
        "TIMED OUT",
        "CONNECTION RESET",
        "CONNECTION ERROR",
        "SERVICE UNAVAILABLE"
    ]

    return any(word in text for word in words)


def wait_time(attempt):
    """حساب وقت الانتظار بين المحاولات."""
    base = min(60, 5 * (2 ** (attempt - 1)))
    return base + random.uniform(0, 2)


# =========================================================
# 5. انتظار تجهيز الملف في Gemini
# =========================================================

def wait_for_file(file):
    start = time.monotonic()

    while True:
        if time.monotonic() - start > FILE_TIMEOUT:
            raise TimeoutError(
                "انتهت مهلة تجهيز الملف في Gemini."
            )

        current = client.files.get(name=file.name)

        state = getattr(current, "state", None)
        state_name = getattr(state, "name", str(state or ""))
        state_name = state_name.upper().split(".")[-1]

        if state_name == "ACTIVE":
            return current

        if state_name in {"FAILED", "CANCELLED"}:
            raise RuntimeError(
                f"تعذرت معالجة الملف في Gemini: {state_name}"
            )

        time.sleep(4)


# =========================================================
# 6. تفريغ التسجيل الصوتي
# =========================================================

def transcribe(file, chat_id, message_id):

    prompt = """
قم بتفريغ جميع الكلمات المسموعة في التسجيل الصوتي إلى نص كامل.

التعليمات:
1. لا تلخص التسجيل.
2. لا تحذف أي كلام مسموع.
3. حافظ على ترتيب الكلام.
4. استخدم لغة المتحدث الأصلية.
5. لا تضف معلومات من عندك.
6. إذا كان هناك أكثر من متحدث، فحاول فصل كلامهم.
7. لا تكتب مقدمة أو شرحًا خارج النص المفرغ.
8. أعد النص المفرغ كاملًا فقط، دون تلخيص أو استخراج نقاط.
9. استخدم علامات الترقيم والفقرات المناسبة عندما يكون ذلك ممكنًا.
""".strip()

    for attempt in range(1, MAX_RETRIES + 1):
        try:
            status(
                chat_id,
                message_id,
                "✍️ جارٍ تفريغ التسجيل الصوتي...\n"
                f"المحاولة {attempt}/{MAX_RETRIES}"
            )

            response = client.models.generate_content(
                model=MODEL,
                contents=[prompt, file]
            )

            text = getattr(response, "text", None)

            if text and text.strip():
                return text.strip()

            raise RuntimeError(
                "لم يرجع Gemini نصًا من التسجيل."
            )

        except Exception as exc:
            print(
                f"[Gemini Error] {safe_error(exc)}"
            )

            if not retryable(exc):
                raise

            if attempt >= MAX_RETRIES:
                raise RuntimeError(
                    "تعذر التفريغ بعد عدة محاولات."
                ) from exc

            delay = wait_time(attempt)

            status(
                chat_id,
                message_id,
                "⏳ الخدمة مشغولة مؤقتًا.\n"
                f"ستتم إعادة المحاولة بعد {delay:.0f} ثانية."
            )

            time.sleep(delay)

    raise RuntimeError("فشل تفريغ التسجيل.")


# =========================================================
# 7. تقسيم النص الطويل مع المحافظة على الفقرات
# =========================================================

def split_text(text):
    text = text.strip()

    if not text:
        return []

    result = []

    while len(text) > TELEGRAM_CHUNK:

        # محاولة التقسيم عند نهاية سطر أولًا.
        split_at = text.rfind(
            "\n",
            0,
            TELEGRAM_CHUNK + 1
        )

        # إذا كان السطر بعيدًا جدًا، نحاول التقسيم عند مسافة.
        if split_at < TELEGRAM_CHUNK // 2:
            split_at = text.rfind(
                " ",
                0,
                TELEGRAM_CHUNK + 1
            )

        # إذا لم نجد موضعًا مناسبًا، نستخدم حد الأحرف.
        if split_at < TELEGRAM_CHUNK // 2:
            split_at = TELEGRAM_CHUNK

        part = text[:split_at].strip()

        if part:
            result.append(part)

        text = text[split_at:].strip()

    if text:
        result.append(text)

    return result


def send_text(chat_id, text):
    parts = split_text(text)

    if not parts:
        raise RuntimeError("النص المفرغ فارغ.")

    total = len(parts)

    for number, part in enumerate(parts, start=1):

        if total == 1:
            message = "📝 النص المفرغ:\n\n" + part
        else:
            message = (
                f"📝 النص المفرغ ({number}/{total})\n\n"
                + part
            )

        bot.send_message(
            chat_id,
            message,
            timeout=60
        )

        time.sleep(0.3)


# =========================================================
# 8. تنزيل الملف الصوتي من Telegram
# =========================================================

def download_file(file_id, path):
    telegram_file = bot.get_file(file_id)

    url = (
        "https://api.telegram.org/file/bot"
        f"{TELEGRAM_TOKEN}/{telegram_file.file_path}"
    )

    try:
        with requests.get(
            url,
            timeout=(30, 300),
            stream=True
        ) as response:

            response.raise_for_status()

            with open(path, "wb") as output:
                for chunk in response.iter_content(
                    chunk_size=1024 * 1024
                ):
                    if chunk:
                        output.write(chunk)

    except requests.RequestException as exc:
        # لا نطبع عنوان الطلب الذي يحتوي على توكن البوت.
        raise RuntimeError(
            "فشل تنزيل الملف من Telegram. "
            "تحقق من الاتصال وحجم الملف."
        ) from None


# =========================================================
# 9. معالجة التسجيل كاملًا
# =========================================================

def process_audio(message):
    chat_id = message.chat.id

    waiting = None
    uploaded = None
    temp_file = None

    try:
        waiting = bot.reply_to(
            message,
            "⏳ جارٍ استلام التسجيل الصوتي..."
        )

        if message.content_type == "voice":
            file_id = message.voice.file_id
            extension = ".ogg"

        elif message.content_type == "audio":
            file_id = message.audio.file_id

            filename = (
                message.audio.file_name or "audio.mp3"
            )

            extension = (
                os.path.splitext(filename)[1] or ".mp3"
            )

            # منع امتداد غير مناسب كاسم لملف مؤقت.
            if (
                not extension.startswith(".")
                or len(extension) > 16
                or not extension[1:].isalnum()
            ):
                extension = ".audio"

        else:
            raise RuntimeError(
                "نوع الملف غير مدعوم."
            )

        fd, temp_file = tempfile.mkstemp(
            prefix="audio_",
            suffix=extension
        )

        os.close(fd)

        status(
            chat_id,
            waiting.message_id,
            "⬇️ جارٍ تنزيل التسجيل..."
        )

        download_file(file_id, temp_file)

        size = os.path.getsize(temp_file)

        if size <= 0:
            raise RuntimeError(
                "الملف الصوتي فارغ."
            )

        status(
            chat_id,
            waiting.message_id,
            "☁️ جارٍ رفع التسجيل إلى Gemini..."
        )

        uploaded = client.files.upload(
            file=temp_file
        )

        active_file = wait_for_file(uploaded)

        text = transcribe(
            active_file,
            chat_id,
            waiting.message_id
        )

        if not text:
            raise RuntimeError(
                "لم يتم استخراج نص من التسجيل."
            )

        delete_message(
            chat_id,
            waiting.message_id
        )

        send_text(chat_id, text)

    except Exception as exc:
        error = safe_error(exc)

        # تسجيل الخطأ دون كشف المفاتيح السرية.
        print(
            f"[Processing Error] {error}"
        )

        if len(error) > 500:
            error = error[:500] + "..."

        if waiting:
            status(
                chat_id,
                waiting.message_id,
                "❌ حدث خطأ أثناء المعالجة:\n\n" + error
            )
        else:
            try:
                bot.send_message(
                    chat_id,
                    "❌ حدث خطأ أثناء معالجة التسجيل."
                )
            except Exception:
                pass

    finally:
        # حذف الملف البعيد من Gemini إن أمكن.
        if uploaded:
            try:
                client.files.delete(
                    name=uploaded.name
                )
            except Exception as exc:
                print(
                    "[Cleanup Warning] تعذر حذف ملف Gemini: "
                    f"{type(exc).__name__}"
                )

        # حذف الملف المحلي المؤقت.
        if temp_file and os.path.exists(temp_file):
            try:
                os.remove(temp_file)
            except OSError as exc:
                print(
                    "[Cleanup Warning] تعذر حذف الملف المحلي: "
                    f"{type(exc).__name__}"
                )


# =========================================================
# 10. أوامر Telegram
# =========================================================

@bot.message_handler(commands=["start", "help"])
def start(message):
    bot.reply_to(
        message,
        "🤖 أهلًا بك!\n\n"
        "🎙️ أرسل لي تسجيلًا صوتيًا أو ملفًا صوتيًا.\n"
        "وسأعمل على تفريغه إلى نص كامل.\n\n"
        "ملاحظة: يعتمد التفريغ على وضوح الصوت وجودته."
    )


@bot.message_handler(
    content_types=["audio", "voice"]
)
def audio(message):
    process_audio(message)


@bot.message_handler(
    func=lambda message: True
)
def other(message):
    bot.reply_to(
        message,
        "🎙️ أرسل تسجيلًا صوتيًا حتى أقوم بتفريغه."
    )


# =========================================================
# 11. تشغيل البوت وإعادة الاتصال
# =========================================================

def run():
    print("=" * 55)
    print("BOT STARTING")
    print(f"Model: {MODEL}")
    print(f"Workers: {BOT_THREADS}")
    print("تم تحميل إعدادات البيئة.")
    print("البوت جاهز لاستقبال التسجيلات بعد الاتصال.")
    print("=" * 55)

    while True:
        try:
            bot.infinity_polling(
                timeout=60,
                long_polling_timeout=20
            )

        except KeyboardInterrupt:
            print("تم طلب إيقاف البوت.")
            break

        except Exception as exc:
            print(
                "[Telegram Error] "
                f"{safe_error(exc)}"
            )

            print(
                "ستتم محاولة إعادة الاتصال بعد 5 ثوانٍ."
            )

            time.sleep(5)


# =========================================================
# 12. خادم Flask للاستضافة
# =========================================================

app = Flask(__name__)


@app.route("/")
def home():
    return "I am alive!", 200


@app.route("/health")
def health():
    return {"status": "ok"}, 200


def run_flask():
    port = int(
        os.environ.get("PORT", "8080")
    )

    app.run(
        host="0.0.0.0",
        port=port,
        debug=False,
        use_reloader=False
    )


def keep_alive():
    thread = threading.Thread(
        target=run_flask,
        daemon=True
    )

    thread.start()


# =========================================================
# 13. نقطة التشغيل النهائية
# =========================================================

if __name__ == "__main__":
    keep_alive()
    run()