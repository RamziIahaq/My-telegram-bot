import io
import os
import re
import sys
import time
import zlib
import random
import shutil
import tempfile
import threading
import subprocess
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import requests
import telebot
from telebot import types, apihelper
from flask import Flask
from dotenv import load_dotenv
from google import genai
from google.genai import types as genai_types

# =========================================================
# إعدادات البيئة والأمان
# =========================================================
load_dotenv()


def env_int(name, default, low=None, high=None):
    try:
        value = int(os.getenv(name, str(default)).strip())
    except ValueError:
        value = default
    if low is not None:
        value = max(low, value)
    if high is not None:
        value = min(high, value)
    return value


def env_flag(name, default="0"):
    return os.getenv(name, default).strip().lower() in ("1", "true", "yes", "on")


TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "").strip()
PORT = env_int("PORT", 8080)
MAX_FILE_MB = env_int("MAX_FILE_MB", 19)
MAX_FILE_BYTES = MAX_FILE_MB * 1024 * 1024
FILE_TIMEOUT = 300
TELEGRAM_CHUNK = 3500
BOT_THREADS = 6
MAX_CONTENT_CHARS = 500000
PREVIEW_CHARS = 1200

# --- إعدادات الذكاء الاصطناعي ---
MAX_ATTEMPTS = 8            # أقصى عدد محاولات لكل طلب إلى Gemini
OUTPUT_TOKENS = 32000       # حد المخرجات (التفكير الداخلي يُحسب منه أيضًا)
THINKING_LEVEL = os.getenv("THINKING_LEVEL", "low").strip().lower()  # low / medium / high / فارغ = افتراضي

# --- إعدادات التفريغ الصوتي ---
AUDIO_CHUNK_SECONDS = env_int("AUDIO_CHUNK_SECONDS", 240, 60, 600)  # طول المقطع المستهدف
AUDIO_PARALLEL = env_int("AUDIO_PARALLEL", 2, 1, 4)    # عدد المقاطع التي تُفرَّغ بالتوازي
AUDIO_JOBS = env_int("AUDIO_JOBS", 2, 1, 4)            # عدد التسجيلات التي تُعالج في نفس الوقت
MAX_AUDIO_CHUNKS = 150
SILENCE_DB = -35            # ما دونه يُعد سكوتًا عند البحث عن نقاط القص
SILENCE_MIN_SECONDS = 0.35
SILENT_CHUNK_MAX_DB = -50   # مقطع أعلى صوته أقل من هذا يُعد صامتًا ولا يُرسل للنموذج
SEND_TXT_FILE = env_flag("SEND_TXT_FILE", "1")          # إرسال التفريغ الطويل كملف txt أيضًا
AUTO_AUDIO_SUMMARY = env_flag("AUTO_AUDIO_SUMMARY", "0")  # ملخص تلقائي بعد التفريغ (مغلق افتراضيًا)

MODEL_CANDIDATES = []
for name in (
    os.getenv("GEMINI_MODEL", "").strip(),
    "gemini-3.8-flash",
    "gemini-3.6-flash",
    "gemini-3.5-flash",
):
    if name and name not in MODEL_CANDIDATES:
        MODEL_CANDIDATES.append(name)


if not TELEGRAM_TOKEN or not GEMINI_API_KEY:
    print("خطأ: اضبط TELEGRAM_BOT_TOKEN و GEMINI_API_KEY في ملف .env")
    sys.exit(1)

apihelper.CONNECT_TIMEOUT = 60
apihelper.READ_TIMEOUT = 300
bot = telebot.TeleBot(TELEGRAM_TOKEN, threaded=True, num_threads=BOT_THREADS)
try:
    client = genai.Client(
        api_key=GEMINI_API_KEY,
        http_options=genai_types.HttpOptions(timeout=300_000),  # بالميلي ثانية
    )
except Exception:
    client = genai.Client(api_key=GEMINI_API_KEY)
CURRENT_MODEL = MODEL_CANDIDATES[0]  # للعرض فقط؛ الاحتياط يتم لكل طلب على حدة

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


def source_label(name):
    """إرجاع اسم/نوع المصدر بصيغة آمنة للعرض في رسائل البوت."""
    labels = {
        "audio": "التسجيل الصوتي",
        "pdf": "ملف PDF",
        "image": "الصورة",
        "text": "النص المرسل",
    }
    value = str(name or "الملف").strip()
    return labels.get(value, value.replace("\r", " ").replace("\n", " ")[:100])



# =========================================================
# طبقة Gemini: محاولات ذكية + نماذج احتياطية لكل طلب
# =========================================================
class IncompleteTranscriptionError(RuntimeError):
    """وصل الناتج لحد المخرجات (غالبًا حلقة تكرار) فلا يُقبل كتفريغ كامل."""


class BlockedContentError(RuntimeError):
    """رفض النموذج المحتوى لأسباب تتعلق بالسلامة."""


BAD_MODELS = set()       # نماذج أكّدت Google أنها غير موجودة (تُتجاهل)
THINKING_BROKEN = False  # إذا رفض النموذج إعداد التفكير نوقفه تلقائيًا


def error_code(exc):
    code = getattr(exc, "code", None)
    if isinstance(code, int):
        return code
    match = re.search(r"\b([45]\d\d)\b", str(exc))
    return int(match.group(1)) if match else None


def classify_error(exc):
    """model_missing / thinking / rate / server / fatal"""
    text = str(exc).upper()
    code = error_code(exc)
    if code == 404 or "NOT_FOUND" in text or "IS NOT FOUND" in text or "NOT SUPPORTED FOR GENERATECONTENT" in text:
        return "model_missing"
    if "THINKING" in text and code in (None, 400):
        return "thinking"
    if code == 429 or "RESOURCE_EXHAUSTED" in text or "QUOTA" in text:
        return "rate"
    if code in (400, 401, 403):
        return "fatal"
    return "server"  # 5xx والانقطاعات والأخطاء غير المعروفة نعيد المحاولة فيها


def retry_delay(exc, default):
    match = re.search(r"retry(?:delay)?\W{0,6}(\d+(?:\.\d+)?)\s*s", str(exc), re.I)
    if match:
        return min(90.0, float(match.group(1)) + 1)
    return float(default)


def finish_name(response):
    candidates = getattr(response, "candidates", None) or []
    if not candidates:
        return ""
    reason = getattr(candidates[0], "finish_reason", None)
    return getattr(reason, "name", str(reason or "")).upper().split(".")[-1]


def build_config(max_tokens):
    # مهم: لا نضبط temperature ولا top_p. Google تنصح بترك الحرارة 1.0 في Gemini 3،
    # وخفضها (مثل 0.0) يسبب حلقات تكرار وتدهورًا في الجودة.
    config = {"max_output_tokens": max_tokens}
    if THINKING_LEVEL in ("low", "medium", "high") and not THINKING_BROKEN:
        config["thinking_config"] = {"thinking_level": THINKING_LEVEL.upper()}
    return config


def generate_text(prompt, file_part=None, *, require_complete=False, allow_empty=False, max_tokens=OUTPUT_TOKENS):
    global THINKING_BROKEN
    models = [m for m in MODEL_CANDIDATES if m not in BAD_MODELS] or list(MODEL_CANDIDATES)
    payload = [prompt] if file_part is None else [prompt, file_part]
    model_pos = 0
    fails_on_model = 0
    last_error = None

    for attempt in range(1, MAX_ATTEMPTS + 1):
        model = models[min(model_pos, len(models) - 1)]
        try:
            print(f"[AI] محاولة {attempt} باستخدام {model}...")
            response = client.models.generate_content(
                model=model,
                contents=payload,
                config=build_config(max_tokens),
            )
        except Exception as exc:
            last_error = exc
            kind = classify_error(exc)
            print(f"[AI Error] ({kind}) {safe_error(exc)}")
            if kind == "model_missing":
                BAD_MODELS.add(model)
                if model_pos + 1 < len(models):
                    model_pos += 1
                    continue
                break
            if kind == "thinking" and not THINKING_BROKEN:
                THINKING_BROKEN = True
                continue
            if kind == "fatal":
                raise RuntimeError(f"رفضت Gemini الطلب: {safe_error(exc)[:300]}") from None
            fails_on_model += 1
            if fails_on_model >= 2 and model_pos + 1 < len(models):
                model_pos += 1
                fails_on_model = 0
                print(f"[AI] أجرب نموذجًا احتياطيًا لهذا الطلب: {models[model_pos]}")
                continue
            time.sleep(retry_delay(exc, min(60, 2 ** min(attempt, 6))) + random.uniform(0, 1))
            continue

        reason = finish_name(response)
        try:
            text = (response.text or "").strip()
        except Exception:
            text = ""

        if reason in ("MAX_TOKENS", "MAX_OUTPUT_TOKENS"):
            if require_complete:
                raise IncompleteTranscriptionError("وصل الناتج إلى حد المخرجات قبل اكتمال التفريغ.")
            if text:
                return text + "\n\n⚠️ (انقطع الناتج لأنه وصل إلى الحد الأقصى)"
        if text:
            return text
        if reason in ("SAFETY", "PROHIBITED_CONTENT", "BLOCKLIST", "SPII", "RECITATION", "IMAGE_SAFETY"):
            raise BlockedContentError("رفض النموذج معالجة هذا المحتوى بسبب سياسات السلامة.")
        block = getattr(getattr(response, "prompt_feedback", None), "block_reason", None)
        if block:
            raise BlockedContentError("رفض النموذج معالجة هذا المحتوى بسبب سياسات السلامة.")
        if allow_empty and reason in ("STOP", ""):
            return ""
        last_error = RuntimeError("لم يرجع النموذج نصًا.")
        time.sleep(2)

    detail = safe_error(last_error)[:200] if last_error else ""
    raise RuntimeError(f"تعذرت المعالجة بعد عدة محاولات. قد يكون هناك ضغط على الخدمة. {detail}".strip())



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


def safe_send(chat_id, text, **kwargs):
    """إرسال رسالة مع إعادة المحاولة عند حد معدل تيليجرام (429)."""
    for attempt in range(4):
        try:
            return bot.send_message(chat_id, text, **kwargs)
        except Exception as exc:
            if getattr(exc, "error_code", None) == 429 and attempt < 3:
                wait = 5
                try:
                    wait = int(exc.result_json["parameters"]["retry_after"])
                except Exception:
                    pass
                time.sleep(min(wait, 60) + 1)
                continue
            raise


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
        safe_send(chat_id, body, reply_markup=main_keyboard(), timeout=60)
        time.sleep(0.4)

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

    if kind == "pdf":
        return f"""استخرج النص من ملف PDF كما هو بدقة عالية. حافظ على ترتيب الفقرات والعناوين.
لا تلخّص ولا تضف شيئًا من عندك، واكتب [غير واضح] عند أي جزء لا يمكن قراءته. أخرج النص فقط دون مقدمة. {extra}"""

    return f"""استخرج كل النصوص الموجودة في هذه الصورة كما هي بدقة. إذا كان هناك خط يد فاقرأه بعناية.
لا تضف شيئًا من عندك، واكتب [غير واضح] عند أي كلمة لا يمكن قراءتها. أخرج النص فقط دون مقدمة. {extra}"""


# =========================================================
# التفريغ الصوتي: تطبيع → قص عند فترات السكوت → تفريغ كل مقطع → فحص الجودة
# =========================================================
def find_ffmpeg():
    exe = shutil.which("ffmpeg")
    if exe:
        return exe
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        return None


FFMPEG = find_ffmpeg()
if not FFMPEG:
    print("[Audio] تحذير: ffmpeg غير متوفر. نفّذ: pip install imageio-ffmpeg")

AUDIO_SLOTS = threading.Semaphore(AUDIO_JOBS)


def run_ffmpeg(args, timeout=900):
    return subprocess.run(
        [FFMPEG, "-hide_banner", "-nostdin", *args],
        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout,
    )


def fmt_time(seconds):
    seconds = int(max(0, seconds))
    h, rest = divmod(seconds, 3600)
    m, s = divmod(rest, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


def normalize_audio(src, work_dir):
    """تحويل أي صيغة إلى mp3 أحادي 16kHz (ما يحتاجه النموذج فعلًا) وبحجم صغير."""
    out = Path(work_dir) / "normalized.mp3"
    proc = run_ffmpeg([
        "-y", "-i", str(src), "-vn", "-ac", "1", "-ar", "16000",
        "-c:a", "libmp3lame", "-b:a", "48k", str(out),
    ])
    if proc.returncode != 0 or not out.exists() or out.stat().st_size < 500:
        print(f"[ffmpeg] {proc.stderr[-500:]}")
        raise RuntimeError("تعذر قراءة الملف الصوتي. تأكد أنه تسجيل صوتي سليم.")
    return str(out)


def audio_duration(path):
    proc = run_ffmpeg(["-i", str(path)])
    match = re.search(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)", proc.stderr)
    if not match:
        return None
    h, m, s = match.groups()
    return int(h) * 3600 + int(m) * 60 + float(s)


def detect_silences(path, duration):
    """قائمة (بداية، نهاية) لفترات السكوت؛ تُستخدم لاختيار نقاط قص لا تقطع الكلام."""
    proc = run_ffmpeg([
        "-i", str(path), "-af",
        f"silencedetect=noise={SILENCE_DB}dB:d={SILENCE_MIN_SECONDS}",
        "-f", "null", "-",
    ])
    starts = [float(x) for x in re.findall(r"silence_start:\s*(-?[\d.]+)", proc.stderr)]
    ends = [float(x) for x in re.findall(r"silence_end:\s*(-?[\d.]+)", proc.stderr)]
    silences = []
    for i, start in enumerate(starts):
        end = ends[i] if i < len(ends) else duration
        silences.append((max(0.0, start), min(end, duration)))
    return silences


def best_cut(silences, ideal, window):
    """أفضل نقطة قص قرب الموضع المثالي: أطول فترة سكوت داخل النافذة."""
    best = None
    for start, end in silences:
        mid = (start + end) / 2
        distance = abs(mid - ideal)
        if distance <= window:
            score = (end - start) - distance / 100.0
            if best is None or score > best[0]:
                best = (score, mid)
    return best[1] if best else ideal


def plan_ranges(duration, silences):
    target = AUDIO_CHUNK_SECONDS
    window = min(40.0, target * 0.25)
    ranges = []
    pos = 0.0
    while duration - pos > target * 1.3:
        cut = best_cut(silences, pos + target, window)
        cut = min(max(cut, pos + target * 0.5), duration - 5)
        ranges.append((pos, cut))
        pos = cut
    ranges.append((pos, duration))
    return ranges


def speech_seconds(silences, start, end):
    silent = sum(max(0.0, min(e, end) - max(s, start)) for s, e in silences)
    return max(0.0, (end - start) - silent)


def export_chunk(src, start, end, out_path):
    proc = run_ffmpeg([
        "-y", "-ss", f"{start:.3f}", "-t", f"{end - start:.3f}", "-i", str(src),
        "-vn", "-ac", "1", "-ar", "16000", "-c:a", "libmp3lame", "-b:a", "48k", str(out_path),
    ])
    if proc.returncode != 0 or not Path(out_path).exists():
        print(f"[ffmpeg] {proc.stderr[-500:]}")
        raise RuntimeError("فشل قص المقطع الصوتي.")


def is_silent(path):
    proc = run_ffmpeg(["-i", str(path), "-af", "volumedetect", "-f", "null", "-"])
    match = re.search(r"max_volume:\s*(-inf|-?[\d.]+)\s*dB", proc.stderr)
    if not match:
        return False
    value = match.group(1)
    return value == "-inf" or float(value) < SILENT_CHUNK_MAX_DB


def audio_prompt(caption, index, total, start, end):
    extra = ""
    if caption:
        extra = f"\nسياق من المستخدم (للفهم فقط وليس للتفريغ): {caption}"
    position = ""
    if total > 1:
        position = (
            f"\nهذا المقطع {index} من {total} من تسجيل واحد متصل (من {fmt_time(start)} إلى {fmt_time(end)}). "
            "قد يبدأ المقطع أو ينتهي في منتصف جملة؛ اكتب ما يُسمع فقط ولا تكمل الجملة من عندك."
        )
    return f"""أنت نظام تفريغ صوتي حرفي (Speech-to-Text) ولست مساعدًا محادثيًا.
اكتب كل كلمة مسموعة في هذا التسجيل بنفس ترتيب نطقها، من أول كلمة إلى آخر كلمة.

القواعد:
1. اكتب ما نُطق فقط. لا تضف كلمة لم تُسمع، ولا تكمل كلامًا ناقصًا، ولا تخمّن من السياق أو من معلوماتك.
2. لا تلخّص ولا تعِد صياغة ولا تحذف التكرار الحقيقي، ولا تصلح أخطاء المتحدث أو لهجته.
3. اكتب [غير واضح] بدل أي كلمة لا يمكن تمييزها.
4. إذا لم يوجد كلام (سكوت أو ضوضاء أو موسيقى فقط) فلا تكتب شيئًا أبدًا ولا تخترع كلامًا.
5. لا تترجم؛ اكتب كل لغة كما نُطقت.
6. إذا نطق المتحدث آية قرآنية واضحة فضع الكلمات المسموعة فقط بين قوسين (مثل: (نص الآية كما سُمِع)). لا تكمل الآية ولا تصححها من الذاكرة، ولا تضع الكلام العادي بين أقواس.
7. أضف علامات ترقيم وقسّم الفقرات بحسب الوقفات الطبيعية.
8. لا تكرر أي جملة لم تتكرر في الصوت. لا مقدمة ولا تعليق ولا خاتمة: التفريغ فقط.{position}{extra}"""


META_NO_SPEECH = re.compile(
    r"(لا\s+يوجد\s+(كلام|صوت|حديث)|لا\s+(يمكن|أستطيع)\s+(سماع|تفريغ)|"
    r"\bno\s+(speech|audio)\b|\bsilence\b|^\s*[\[\(]?\s*(صمت|موسيقى|music)\s*[\]\)]?\s*$)",
    re.I,
)


def clean_transcript(text):
    t = (text or "").strip()
    t = re.sub(r"^```[a-zA-Z]*\n?", "", t)
    t = re.sub(r"\n?```$", "", t).strip()
    lines = t.splitlines()
    if lines and len(lines[0]) < 50 and lines[0].rstrip().endswith(":") and "تفريغ" in lines[0]:
        t = "\n".join(lines[1:]).strip()
    if len(t) < 80 and META_NO_SPEECH.search(t):
        return ""
    return t


def looks_repetitive(text):
    raw = (text or "").strip()
    if len(raw) < 200:
        return False
    lines = [line.strip() for line in raw.splitlines() if line.strip()]
    run = 1
    for a, b in zip(lines, lines[1:]):
        run = run + 1 if a == b else 1
        if run >= 4:
            return True
    words = raw.split()
    if len(words) >= 40:
        grams = Counter(tuple(words[i:i + 6]) for i in range(len(words) - 5))
        if grams.most_common(1)[0][1] >= 8:
            return True
    data = raw.encode("utf-8")
    return len(data) >= 1200 and len(zlib.compress(data, 6)) / len(data) < 0.15


def transcript_problem(text, seconds, speech_secs):
    """loop / too_fast / too_short / '' (سليم)"""
    words = len(text.split())
    if seconds >= 20 and words / seconds > 6.5:
        return "too_fast"        # أسرع من أي كلام بشري: هلوسة غالبًا
    if looks_repetitive(text):
        return "loop"
    if speech_secs >= 60 and words / speech_secs < 0.4:
        return "too_short"       # كلام كثير في الصوت ونص قليل: نموذج تجاوز أجزاء
    return ""


RETRY_NOTE = (
    "\n\nتنبيه: محاولة سابقة لهذا المقطع كانت غير سليمة (تكرار أو نقص أو إضافة). "
    "استمع بعناية وفرّغ ما يُسمع فقط، دون تكرار ودون تخطّي أي كلام."
)


def transcribe_range(src, start, end, work_dir, caption, index, total, silences, notes, depth=0):
    """يفرّغ المجال [start, end]. إذا فشل فحص الجودة مرتين يقسمه إلى نصفين ويعيد."""
    seconds = end - start
    out_path = Path(work_dir) / f"chunk_{int(start * 1000)}_{int(end * 1000)}.mp3"
    export_chunk(src, start, end, out_path)
    try:
        if is_silent(out_path):
            return ""
        part = genai_types.Part.from_bytes(data=out_path.read_bytes(), mime_type="audio/mpeg")
        speech_secs = speech_seconds(silences, start, end)
        base_prompt = audio_prompt(caption, index, total, start, end)
        prompt = base_prompt
        attempts = []  # (text, problem)

        for _ in range(2):
            try:
                raw = generate_text(prompt, part, require_complete=True, allow_empty=True)
            except IncompleteTranscriptionError:
                attempts.append(("", "truncated"))
            else:
                text = clean_transcript(raw)
                problem = transcript_problem(text, seconds, speech_secs)
                if not problem:
                    return text
                attempts.append((text, problem))
            prompt = base_prompt + RETRY_NOTE

        needs_split = any(p in ("truncated", "loop", "too_fast") for _, p in attempts)
        if needs_split and seconds > 50 and depth < 3:
            mid = best_cut(silences, start + seconds / 2, seconds * 0.15)
            mid = min(max(mid, start + 10), end - 10)
            left = transcribe_range(src, start, mid, work_dir, caption, index, total, silences, notes, depth + 1)
            right = transcribe_range(src, mid, end, work_dir, caption, index, total, silences, notes, depth + 1)
            return "\n".join(t for t in (left, right) if t)

        candidates = [t for t, p in attempts if p not in ("truncated", "too_fast")]
        if candidates:
            best = max(candidates, key=len)
            label = f"المقطع {index} ({fmt_time(start)}–{fmt_time(end)})"
            if not best:
                notes.append(f"ℹ️ {label}: لم أسمع كلامًا واضحًا فيه.")
            elif any(p in ("loop", "too_short") for _, p in attempts):
                notes.append(f"⚠️ {label}: فيه تكرار أو نقص محتمل، يُفضّل مراجعته.")
            return best
        raise RuntimeError("لم يتجاوز التفريغ فحص الجودة.")
    finally:
        try:
            out_path.unlink()
        except OSError:
            pass


def transcribe_audio_file(audio_path, caption, chat_id, status_id):
    """يرجع (النص الكامل، قائمة ملاحظات). لا يفشل كله بسبب مقطع واحد."""
    if not FFMPEG:
        raise RuntimeError("ffmpeg غير متوفر على السيرفر. ثبّت الحزمة: pip install imageio-ffmpeg")

    notes = []
    with tempfile.TemporaryDirectory(prefix="study_audio_") as work_dir:
        normalized = normalize_audio(audio_path, work_dir)
        duration = audio_duration(normalized)
        if not duration or duration < 0.5:
            raise RuntimeError("الملف الصوتي فارغ أو غير مقروء.")
        silences = detect_silences(normalized, duration)
        ranges = plan_ranges(duration, silences)
        total = len(ranges)
        if total > MAX_AUDIO_CHUNKS:
            raise RuntimeError("التسجيل طويل جدًا؛ قسّمه إلى ملفات أقصر.")

        if total > 1:
            update_status(chat_id, status_id, f"🎧 مدة التسجيل {fmt_time(duration)}. سأفرّغه على {total} مقاطع...")
        else:
            update_status(chat_id, status_id, "🎧 جاري تفريغ الصوت...")

        failed = []

        def job(i):
            start, end = ranges[i]
            try:
                return transcribe_range(normalized, start, end, work_dir, caption, i + 1, total, silences, notes)
            except Exception as exc:
                label = f"المقطع {i + 1} ({fmt_time(start)}–{fmt_time(end)})"
                failed.append(i)
                notes.append(f"⚠️ تعذر تفريغ {label}: {safe_error(exc)[:150]}")
                return f"[⚠️ تعذر تفريغ {label}]"

        results = [""] * total
        done = 0
        with ThreadPoolExecutor(max_workers=min(AUDIO_PARALLEL, total)) as pool:
            futures = {pool.submit(job, i): i for i in range(total)}
            for future in as_completed(futures):
                results[futures[future]] = future.result()
                done += 1
                if total > 1:
                    update_status(chat_id, status_id, f"🎧 تم تفريغ {done} من {total} مقاطع...")

    if failed and len(failed) == total:
        raise RuntimeError(notes[0] if notes else "فشل تفريغ كل المقاطع.")
    return "\n".join(t for t in results if t).strip(), notes


def quick_audio_summary(transcript):
    prompt = f"""اكتب ملخصًا سريعًا وأمينًا للصوت اعتمادًا على التفريغ التالي فقط.
اذكر موضوع الصوت وأهم النقاط في 3 إلى 6 نقاط. لا تضف أي معلومة غير موجودة في التفريغ. إذا وُجدت أجزاء غير واضحة فنبّه لذلك.

التفريغ:
{transcript[:MAX_CONTENT_CHARS]}"""
    return generate_text(prompt)


def send_typing_continuous(chat_id, stop_event):
    """إبقاء حالة 'جاري الكتابة' ظاهرة أثناء المعالجة الطويلة."""
    while not stop_event.is_set():
        try:
            bot.send_chat_action(chat_id, "typing")
        except Exception:
            pass
        stop_event.wait(4)


def send_full_transcript(chat_id, text, notes):
    """يرسل التفريغ كاملًا على رسائل متتابعة، ونسخة txt إذا كان طويلًا."""
    send_long_text(chat_id, text, prefix="📝 التفريغ الكامل")
    if SEND_TXT_FILE and len(text) > TELEGRAM_CHUNK:
        try:
            buffer = io.BytesIO(text.encode("utf-8"))
            buffer.name = "transcript.txt"
            bot.send_document(chat_id, buffer, caption="📄 نفس التفريغ كملف نصي", timeout=120)
        except Exception as exc:
            print(f"[Document Error] {safe_error(exc)}")
    if notes:
        safe_send(chat_id, "ملاحظات على التفريغ:\n" + "\n".join(notes[:10]), reply_markup=main_keyboard())


def process_media(message):
    chat_id = message.chat.id
    user_id = message.from_user.id
    waiting = bot.reply_to(message, "⏳ بدأت باستلام الملف، لحظات من فضلك...", reply_markup=main_keyboard())
    local_path = None
    uploaded = None
    slot_taken = False
    stop_typing = threading.Event()
    threading.Thread(target=send_typing_continuous, args=(chat_id, stop_typing), daemon=True).start()

    try:
        media = detect_media(message)
        if not media:
            update_status(chat_id, waiting.message_id, "⚠️ نوع الملف غير مدعوم.")
            return

        file_id, extension, media_kind, mime_type, file_size = media

        if file_size and file_size > MAX_FILE_BYTES:
            raise RuntimeError(f"الملف كبير جدًا ({file_size / (1024 * 1024):.1f}MB). الحد هو {MAX_FILE_MB}MB.")

        update_status(chat_id, waiting.message_id, "⬇️ جاري تحميل الملف من تيليجرام...")
        fd, local_path = tempfile.mkstemp(prefix="study_", suffix=extension)
        os.close(fd)
        download_telegram_file(file_id, local_path)

        notes = []
        if media_kind == "audio":
            if not AUDIO_SLOTS.acquire(blocking=False):
                update_status(chat_id, waiting.message_id, "⏳ يوجد تفريغ آخر قيد العمل، سأبدأ تسجيلك عند حلول دورك...")
                AUDIO_SLOTS.acquire()
            slot_taken = True
            content, notes = transcribe_audio_file(local_path, message.caption, chat_id, waiting.message_id)
        else:
            update_status(chat_id, waiting.message_id, "☁️ جاري الرفع إلى Gemini للتحليل...")
            uploaded = client.files.upload(file=local_path, config={"mime_type": mime_type})
            update_status(chat_id, waiting.message_id, "⚙️ جاري تجهيز الملف للتحليل...")
            active_file = wait_for_file(uploaded)
            update_status(chat_id, waiting.message_id, "🧠 جاري استخراج النص النهائي...")
            content = generate_text(prompt_for_media(media_kind, message.caption), active_file)

        if not content or len(content.strip()) < 2:
            if media_kind == "audio":
                raise RuntimeError("لم أسمع كلامًا واضحًا في هذا التسجيل.")
            raise RuntimeError("فشل النموذج في استخراج نص واضح.")

        save_content(user_id, content, source_label(media_kind))

        try:
            bot.delete_message(chat_id, waiting.message_id)
        except Exception:
            pass

        if media_kind == "audio":
            send_full_transcript(chat_id, content.strip(), notes)
            if AUTO_AUDIO_SUMMARY:
                try:
                    send_long_text(chat_id, quick_audio_summary(content), prefix="⚡ ملخص سريع عن الصوت")
                except Exception as summary_error:
                    print(f"[Summary Error] {safe_error(summary_error)}")
        else:
            send_content_preview(chat_id, content, source_label(media_kind))
        bot.send_message(chat_id, "✅ اكتمل التجهيز! اختر ماذا تريد أن أفعل بالنص:", reply_markup=task_keyboard())

    except Exception as exc:
        err_msg = safe_error(exc)
        print(f"[Process Error] {err_msg}")
        update_status(chat_id, waiting.message_id, f"❌ حدث خطأ: {err_msg}")
    finally:
        stop_typing.set()
        if slot_taken:
            AUDIO_SLOTS.release()
        if uploaded:
            try:
                client.files.delete(name=uploaded.name)
            except Exception:
                pass
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