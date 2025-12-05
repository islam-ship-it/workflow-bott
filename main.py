# main.py
import os
import time
import json
import requests
import threading
import asyncio
import logging
import base64
import tempfile
from flask import Flask, request, jsonify
from openai import OpenAI
from pymongo import MongoClient
from datetime import datetime, timezone
from dotenv import load_dotenv

# ===========================
# إعداد اللوجات (بالعربي)
# ===========================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)
logger.info("▶️ بدء تشغيل التطبيق (نسخة Responses + Conversations) ...")

# ===========================
# DEBUG
# ===========================
def debug(title, data=None):
    logger.info("\n" + "="*70)
    logger.info(f"🔍 {title}")
    if data is not None:
        try:
            logger.info(json.dumps(data, indent=2, ensure_ascii=False))
        except Exception:
            logger.info(str(data))
    logger.info("="*70 + "\n")

# ===========================
# تحميل الإعدادات
# ===========================
load_dotenv()
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
ASSISTANT_ID_PREMIUM = os.getenv("ASSISTANT_ID_PREMIUM")
MONGO_URI = os.getenv("MONGO_URI")
MANYCHAT_API_KEY = os.getenv("MANYCHAT_API_KEY")
MANYCHAT_SECRET_KEY = os.getenv("MANYCHAT_SECRET_KEY")
PORT = int(os.getenv("PORT", 5000))

# ===========================
# قاعدة البيانات (MongoDB)
# ===========================
if not MONGO_URI:
    logger.error("❌ MONGO_URI غير معرفة في المتغيرات البيئة")
    raise SystemExit("MONGO_URI required")

try:
    client_db = MongoClient(MONGO_URI)
    db = client_db["multi_platform_bot"]
    sessions_collection = db["sessions"]
    logger.info("✅ متصل بقاعدة البيانات (MongoDB)")
except Exception as e:
    logger.error(f"❌ فشل الاتصال بقاعدة البيانات: {e}")
    raise

# ===========================
# Flask + OpenAI
# ===========================
app = Flask(__name__)
client = OpenAI(api_key=OPENAI_API_KEY)
logger.info("🚀 Flask و OpenAI جاهزين")

# ===========================
# متغيرات التحكم
# ===========================
pending_messages = {"Facebook": {}, "Instagram": {}}
message_timers = {"Facebook": {}, "Instagram": {}}
run_locks = {"Facebook": {}, "Instagram": {}}
queue_lock = threading.Lock()

# وقت التجميع (ثانيتين كافية جداً)
BATCH_WAIT_TIME = 8.0
RETRY_DELAY_WHEN_BUSY = 6.5

# ===========================
# Utilities
# ===========================
def download_media_from_url(url, timeout=20):
    debug("🌐 Downloading Media", url)
    try:
        r = requests.get(url, timeout=timeout, stream=True)
        r.raise_for_status()
        return r.content
    except Exception as e:
        debug("❌ فشل تحميل الميديا", str(e))
        return None

def transcribe_audio_bytes(content_bytes, fmt="mp4"):
    debug("🎤 Converting Audio To Text (Whisper)", {"format": fmt})
    try:
        with tempfile.NamedTemporaryFile(delete=False, suffix=f".{fmt}") as tmp:
            tmp.write(content_bytes)
            path = tmp.name
        # استخدام واجهة OpenAI Whisper (client.audio.transcriptions)
        with open(path, "rb") as f:
            tr = client.audio.transcriptions.create(model="whisper-1", file=f)
        os.remove(path)
        return tr.text if hasattr(tr, "text") else getattr(tr, "transcription", None)
    except Exception as e:
        debug("❌ خطأ تحويل الصوت", str(e))
        return None

async def get_image_description_for_assistant(base64_image):
    """
    يعالج الصورة عن طريق طلب وصف من الموديل (نستخدم chat/completions style مع محتوى صورة).
    قد تختلف بنية الاستجابة حسب SDK؛ نعطي fallback.
    """
    debug("🖼️ وصف صورة (Vision)", "")
    try:
        # نستخدم chat completions أو responses حسب إعداد SDK؛ هنا نستخدم chat-like payload
        response = await asyncio.to_thread(
            client.chat.completions.create,
            model="gpt-4o",
            messages=[{
                "role": "user",
                "content": [
                    {"type": "text", "text": "اقرأ محتوى الصورة بدقة واطلع لي أبرز النقاط كنص."},
                    {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{base64_image}"}}
                ]
            }],
            max_tokens=400
        )
        # Try to extract response text
        try:
            return response.choices[0].message.content
        except Exception:
            return getattr(response, "output_text", None) or getattr(response, "text", None)
    except Exception as e:
        debug("❌ خطأ رؤية الصورة", str(e))
        return None

# ===========================
# Helpers: نوع الرابط
# ===========================
def is_image_url(url: str):
    url = (url or "").lower()
    img_ext = (".jpg", ".jpeg", ".png", ".gif", ".bmp", ".webp", ".heic")
    return any(url.endswith(ext) or ext in url for ext in img_ext)

def is_audio_url(url: str):
    url = (url or "").lower()
    audio_indicators = (".mp3", ".wav", ".m4a", ".ogg", ".mp4", "audioclip", "audio")
    return any(ind in url for ind in audio_indicators)

def safe_text(value):
    return value.strip() if isinstance(value, str) else ""

# ===========================
# جلسة المستخدم
# ===========================
def get_or_create_session_from_contact(contact_data, platform_hint=None):
    debug("🧾 FULL CONTACT DATA", contact_data)

    user_id = str(contact_data.get("id"))
    if not user_id:
        return None

    # اكتشاف المنصة
    if platform_hint is None or platform_hint == "ManyChat":
        if contact_data.get("ig_id") or contact_data.get("ig_last_interaction"):
            main_platform = "Instagram"
        else:
            main_platform = "Facebook"
    else:
        main_platform = platform_hint

    debug("📱 PLATFORM DETECTED", {"user_id": user_id, "platform": main_platform})

    now_utc = datetime.now(timezone.utc)
    session = sessions_collection.find_one({"_id": user_id})

    if session is not None:
        sessions_collection.update_one(
            {"_id": user_id},
            {"$set": {
                "last_contact_date": now_utc,
                "platform": main_platform,
                "profile.name": contact_data.get("name"),
                "profile.profile_pic": contact_data.get("profile_pic"),
                "status": "active"
            }}
        )
        return sessions_collection.find_one({"_id": user_id})

    # جلسة جديدة
    new_session = {
        "_id": user_id,
        "platform": main_platform,
        "profile": {
            "name": contact_data.get("name"),
            "first_name": contact_data.get("first_name"),
            "last_name": contact_data.get("last_name"),
            "profile_pic": contact_data.get("profile_pic"),
        },
        "openai_conversation_id": None,
        "custom_fields": contact_data.get("custom_fields", {}),
        "tags": [f"source:{main_platform.lower()}"],
        "status": "active",
        "conversation_summary": "",
        "first_contact_date": now_utc,
        "last_contact_date": now_utc
    }

    sessions_collection.insert_one(new_session)
    return new_session

# ===========================
# دالة إرسال إشارة "جاري الكتابة"
# ===========================
def send_typing_action(subscriber_id, platform):
    debug("⚡ Sending Typing/Open Signal...", {"user": subscriber_id})
    url = "https://api.manychat.com/fb/sending/sendContent"
    headers = {
        "Authorization": f"Bearer {MANYCHAT_API_KEY}",
        "Content-Type": "application/json"
    }
    payload = {
        "subscriber_id": str(subscriber_id),
        "data": {
            "version": "v2",
            "content": {
                "type": "typing_on"
            }
        }
    }
    try:
        requests.post(url, headers=headers, data=json.dumps(payload), timeout=2)
    except Exception:
        pass

# ===========================
# OpenAI Assistant (Responses + Conversations) مع retry على conversation_locked
# ===========================
async def get_assistant_reply_async(session, content_text):
    debug("🤖 Responses + Conversations Processing", {"user": session["_id"]})
    user_id = session["_id"]
    conversation_id = session.get("openai_conversation_id")

    # 1) إنشاء Conversation لو مش موجود
    if not conversation_id:
        try:
            conv = await asyncio.to_thread(client.conversations.create, items=[], metadata={"user_id": user_id})
            conversation_id = conv.id
            sessions_collection.update_one({"_id": user_id}, {"$set": {"openai_conversation_id": conversation_id}})
            debug("✅ تم إنشاء محادثة جديدة", {"conversation_id": conversation_id})
        except Exception as e:
            debug("❌ فشل إنشاء المحادثة", str(e))
            conversation_id = None

    # 2) بناء الـ Payload
    payload = {
        "prompt": {
            "id": "pmpt_691df223bd3881909e4e9c544a56523b006e1332a5ce0f11",
            "version": "5"
        },
        "input": [
            {
                "role": "user",
                "content": content_text
            }
        ],
        "store": True,
        "reasoning": {"summary": "auto"}
    }
    if conversation_id:
        payload["conversation"] = conversation_id

    # 3) محاولات عند conversation_locked
    attempts = 0
    while attempts < 4:
        attempts += 1
        try:
            response = await asyncio.to_thread(client.responses.create, **payload)
            # استخراج النص النهائي
            reply = None
            if hasattr(response, "output_text") and response.output_text:
                reply = response.output_text
            if not reply and hasattr(response, "output"):
                for item in response.output:
                    content_list = getattr(item, "content", None)
                    if content_list:
                        for c in content_list:
                            if isinstance(c, dict) and c.get("type") == "output_text":
                                reply = c.get("text", {}).get("value")
                                break
            if not reply:
                return "⚠️ حصل خطأ أثناء توليد الرد."
            return reply.strip()
        except Exception as e:
            s = str(e)
            debug("❌ خطأ في Responses API", s)
            if "conversation_locked" in s or "another operation is currently running on this conversation" in s.lower():
                sleep_for = 0.7 * attempts
                debug("⏳ Conversation locked, retrying after sleep", {"sleep": sleep_for, "attempt": attempts})
                time.sleep(sleep_for)
                continue
            return "⚠️ حصل خطأ أثناء المعالجة."

# ===========================
# إرسال ManyChat
# ===========================
def send_manychat_reply(subscriber_id, text_message, platform, fallback_tag="HUMAN_AGENT"):
    debug("📤 Sending ManyChat Reply", {"subscriber_id": subscriber_id, "message": text_message})
    channel = "instagram" if platform == "Instagram" else "facebook"
    url = "https://api.manychat.com/fb/sending/sendContent"
    headers = {"Authorization": f"Bearer {MANYCHAT_API_KEY}", "Content-Type": "application/json"}

    payload_1 = {
        "subscriber_id": str(subscriber_id),
        "channel": channel,
        "data": {
            "version": "v2",
            "content": {
                "messages": [{"type": "text", "text": text_message}]
            }
        }
    }
    try:
        r = requests.post(url, headers=headers, data=json.dumps(payload_1), timeout=15)
        if r.status_code == 200:
            debug("✅ Sent Normally", r.status_code)
            return {"ok": True}
    except Exception as e:
        debug("❌ Network Error", str(e))

    # Retry with tags
    tags_to_try = ["HUMAN_AGENT", "ACCOUNT_UPDATE", "CONFIRMED_EVENT_UPDATE"]
    for tag in tags_to_try:
        payload_force = {
            "subscriber_id": str(subscriber_id),
            "channel": channel,
            "data": {
                "version": "v2",
                "message_tag": tag,
                "content": {
                    "messages": [
                        {"type": "text", "text": text_message, "tag": tag}
                    ]
                }
            }
        }
        try:
            r2 = requests.post(url, headers=headers, data=json.dumps(payload_force), timeout=15)
            if r2.status_code == 200:
                debug(f"✅ Success with {tag}", r2.status_code)
                return {"ok": True}
        except Exception:
            pass

    # Legacy fallback
    payload_v1 = {
        "subscriber_id": str(subscriber_id),
        "data": {"version": "v2", "content": {"messages": [{"type": "text", "text": text_message}]}},
        "message_tag": "HUMAN_AGENT"
    }
    try:
        requests.post(url, headers=headers, data=json.dumps(payload_v1), timeout=10)
    except Exception:
        pass

    return {"ok": False}

# ===========================
# Queue System
# ===========================
def schedule_assistant_response(platform, user_id):
    debug("⚙ Queue Run Started", {"platform": platform, "user": user_id})
    lock = run_locks[platform].setdefault(user_id, threading.Lock())
    if not lock.acquire(blocking=False):
        debug("⏳ Assistant Busy – Retrying", {"user": user_id})
        threading.Timer(RETRY_DELAY_WHEN_BUSY, schedule_assistant_response, args=[platform, user_id]).start()
        return

    try:
        with queue_lock:
            data = pending_messages[platform].pop(user_id, None)
            message_timers[platform].pop(user_id, None)

        if not data:
            return

        session = data["session"]
        merged = "\n".join(data["texts"])
        debug("📝 MERGED USER MESSAGES", merged)

        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            reply = loop.run_until_complete(get_assistant_reply_async(session, merged))
        finally:
            loop.close()

        send_manychat_reply(user_id, reply, session["platform"])

    finally:
        lock.release()

def add_to_queue(session, text):
    platform = session["platform"]
    uid = session["_id"]

    debug("📥 ADDING TO QUEUE", {"user": uid, "platform": platform, "incoming_text": text})

    with queue_lock:
        if uid not in pending_messages[platform]:
            threading.Thread(target=send_typing_action, args=(uid, platform)).start()

        if uid not in pending_messages[platform]:
            pending_messages[platform][uid] = {"texts": [], "session": session}

        pending_messages[platform][uid]["texts"].append(text)

        if uid in message_timers[platform]:
            try:
                message_timers[platform][uid].cancel()
            except Exception:
                pass

        timer = threading.Timer(BATCH_WAIT_TIME, schedule_assistant_response, args=[platform, uid])
        message_timers[platform][uid] = timer
        timer.start()

        debug("⏳ QUEUE UPDATED", {"platform": platform, "user": uid, "note": "Typing signal sent immediately"})

# ===========================
# Content detection + processing
# ===========================
def process_incoming_payload_text(contact):
    """
    نحاول أن نتعامل مع:
    - نص عادي => keep
    - رابط صورة => download -> vision -> تحويل لنص
    - رابط صوت/ريكورد => download -> whisper -> تحويل لنص
    - رابط موقع (غير صورة/صوت) => نتركه كـ نص (أو نعامل كـ website text لاحقاً)
    النتيجة: سلسلة نصية واحدة تمثل المحتوى المفهوم من المرسل.
    """
    raw = contact.get("last_text_input") or contact.get("last_input_text") or contact.get("last_input") or ""
    raw = safe_text(raw)
    debug("📥 TEXT EXTRACTED (raw)", raw)

    parts = []
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    try:
        # لو في رابط واحد أو عدة روابط مفصولة فراغات: نعالج كل واحدة
        tokens = raw.split()
        for token in tokens:
            if token.startswith("http://") or token.startswith("https://"):
                if is_image_url(token):
                    # تحميل الصورة وتحويلها لوصف نصي (Vision)
                    debug("🔍 Detected image URL", token)
                    img_bytes = download_media_from_url(token)
                    if img_bytes:
                        b64 = base64.b64encode(img_bytes).decode("utf-8")
                        desc = loop.run_until_complete(get_image_description_for_assistant(b64))
                        if desc:
                            parts.append(f"نص من الصورة: {desc}")
                        else:
                            parts.append("نص من الصورة: [فشل استخراج وصف الصورة]")
                    else:
                        parts.append("نص من الصورة: [فشل تنزيل الصورة]")
                elif is_audio_url(token):
                    # تحميل الصوت وتحويله لنص (Whisper)
                    debug("🔍 Detected audio URL", token)
                    audio_bytes = download_media_from_url(token)
                    if audio_bytes:
                        txt = transcribe_audio_bytes(audio_bytes, fmt="mp4")
                        if txt:
                            parts.append(f"نص من الريكورد/الصوت: {txt}")
                        else:
                            parts.append("نص من الريكورد/الصوت: [فشل تحويل الصوت]")
                    else:
                        parts.append("نص من الريكورد/الصوت: [فشل تنزيل الملف الصوتي]")
                else:
                    # رابط موقع عادي -> نحتفظ به كنص
                    debug("🔍 Detected generic website URL", token)
                    parts.append(f"رابط: {token}")
            else:
                # نص عادي
                parts.append(token)
    finally:
        try:
            loop.close()
        except Exception:
            pass

    # ندمج الأجزاء معًا كسطر نصي واحد مع الحفاظ على ترتيب المرسل
    merged_text = " ".join([p for p in parts if p])
    if not merged_text:
        merged_text = "[لا يوجد نص قابل للمعالجة]"
    debug("✅ Processed incoming to merged text", merged_text)
    return merged_text

# ===========================
# ManyChat Webhook
# ===========================
@app.route("/manychat_webhook", methods=["POST"])
def mc_webhook():
    debug("📩 Webhook Received", "")
    if MANYCHAT_SECRET_KEY:
        auth = request.headers.get("Authorization")
        if auth != f"Bearer {MANYCHAT_SECRET_KEY}":
            return jsonify({"error": "unauthorized"}), 403

    data = request.get_json(silent=True)
    debug("📦 RAW WEBHOOK DATA", data)
    if not data:
        return jsonify({"error": "invalid_payload"}), 400

    contact = data.get("full_contact")
    if not contact:
        return jsonify({"error": "missing contact"}), 400

    # حماية إنستغرام: لو الجلسة موجودة و platform = Instagram ثم لم يرسل ig_id -> نتجاهل
    user_id = str(contact.get("id"))
    existing_session = sessions_collection.find_one({"_id": user_id})
    if existing_session is not None and existing_session.get("platform") == "Instagram" and not contact.get("ig_id"):
        debug("⛔ IG BLOCK TRIGGERED", "No IG ID")
        return jsonify({"ignored": True}), 200

    session = get_or_create_session_from_contact(contact, platform_hint="ManyChat")
    if session is None:
        return jsonify({"error": "session_error"}), 500

    # عملية تحليل المدخل وتحويل الصور/صوت إلى نص
    processed_text = process_incoming_payload_text(contact)
    debug("📥 TEXT PROCESSED FOR QUEUE", processed_text)

    # نضيف للنظام (سينك التجميع)
    add_to_queue(session, processed_text)

    return jsonify({"ok": True}), 200

# ===========================
# Home Route
# ===========================
@app.route("/")
def home():
    return "Bot running with INSTANT SIGNAL & Responses/Conversations (MongoDB memory enabled)"

# ===========================
# Run
# ===========================
if __name__ == "__main__":
    logger.info("🚀 السيرفر جاهز للعمل")
    app.run(host="0.0.0.0", port=PORT)

