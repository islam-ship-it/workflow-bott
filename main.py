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
logger.info("▶️ بدء تشغيل التطبيق...")

# ===========================
# تحميل الإعدادات من .env
# ===========================
load_dotenv()

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
ASSISTANT_ID_PREMIUM = os.getenv("ASSISTANT_ID_PREMIUM")  # نفس الـ Assistant للـ FB و IG
MONGO_URI = os.getenv("MONGO_URI")

MANYCHAT_API_KEY = os.getenv("MANYCHAT_API_KEY")
MANYCHAT_SECRET_KEY = os.getenv("MANYCHAT_SECRET_KEY")

# تحقق من المتطلبات الأساسية
if not OPENAI_API_KEY:
    logger.error("❌ متغير OPENAI_API_KEY مفقود")
if not MANYCHAT_API_KEY:
    logger.error("❌ متغير MANYCHAT_API_KEY مفقود")
if not MONGO_URI:
    logger.error("❌ متغير MONGO_URI مفقود")
if not ASSISTANT_ID_PREMIUM:
    logger.error("❌ متغير ASSISTANT_ID_PREMIUM مفقود")

# ===========================
# اتصال بقاعدة البيانات
# ===========================
try:
    client_db = MongoClient(MONGO_URI)
    db = client_db["multi_platform_bot"]
    sessions_collection = db["sessions"]
    logger.info("✅ متصل بقاعدة البيانات")
except Exception as e:
    logger.error(f"❌ فشل الاتصال بقاعدة البيانات: {e}")
    raise

# ===========================
# Flask و OpenAI
# ===========================
app = Flask(__name__)
client = OpenAI(api_key=OPENAI_API_KEY)
logger.info("🚀 Flask و OpenAI جاهزين")

# ===========================
# متغيرات التحكم (مفصولة لكل منصة)
# ===========================
# هيكل البيانات لكل منصة يكون: pending_messages[platform] = { user_id: {texts: [], session: {...}} }
pending_messages = {"Facebook": {}, "Instagram": {}}
message_timers = {"Facebook": {}, "Instagram": {}}
run_locks = {"Facebook": {}, "Instagram": {}}

queue_lock = threading.Lock()

BATCH_WAIT_TIME = 9.0
RETRY_DELAY_WHEN_BUSY = 3.0

# ===========================
# Utilities: تحميل وسحب ميديا وملفات صوتية
# ===========================
def download_media_from_url(url, timeout=15):
    try:
        r = requests.get(url, timeout=timeout)
        r.raise_for_status()
        return r.content
    except Exception as e:
        logger.error(f"❌ فشل تحميل الميديا من URL: {e}")
        return None

def transcribe_audio(content_bytes, fmt="mp4"):
    try:
        with tempfile.NamedTemporaryFile(delete=False, suffix=f".{fmt}") as tmp:
            tmp.write(content_bytes)
            path = tmp.name
        with open(path, "rb") as f:
            tr = client.audio.transcriptions.create(model="whisper-1", file=f)
        os.remove(path)
        return tr.text
    except Exception as e:
        logger.error(f"❌ خطأ في تحويل الصوت للنص: {e}")
        return None

async def get_image_description_for_assistant(base64_image):
    logger.info("🖼️ معالجة صورة مع OpenAI (وصف)...")
    try:
        response = await asyncio.to_thread(
            client.chat.completions.create,
            model="gpt-4.1",
            messages=[{
                "role": "user",
                "content": [
                    {"type": "text", "text": "اقرأ محتوى الصورة بدقة."},
                    {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{base64_image}"}}
                ]
            }],
            max_tokens=300
        )
        return response.choices[0].message.content
    except Exception as e:
        logger.error(f"❌ خطأ معالجة الصورة: {e}")
        return None

# ===========================
# جلسة المستخدم (إنشاء أو استرجاع) + Detect platform by ig_id
# ===========================
def get_or_create_session_from_contact(contact_data, platform_hint=None):
    logger.info("====== 🧾 DEBUG CONTACT DATA ======")
    logger.info(json.dumps(contact_data, indent=2, ensure_ascii=False))

    user_id = str(contact_data.get("id"))
    if not user_id:
        logger.error("❌ user_id غير موجود في contact_data")
        return None

    # كشف المنصة الحقيقية: نستخدم ig_id أو ig_last_interaction كدليل على Instagram
    ig_id = contact_data.get("ig_id")
    ig_last = contact_data.get("ig_last_interaction")
    if ig_id or ig_last:
        main_platform = "Instagram"
    else:
        # fallback: لو المستخدم مرّر platform_hint استخدمه، وإلا Facebook افتراضياً
        main_platform = platform_hint if platform_hint in ("Instagram", "Facebook") else "Facebook"

    logger.info(f"📌 subscriber_id = {user_id}")
    logger.info(f"📱 رسالة جاية من: {main_platform}")

    now_utc = datetime.now(timezone.utc)
    session = sessions_collection.find_one({"_id": user_id})

    if session:
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

    # جديد
    new_session = {
        "_id": user_id,
        "platform": main_platform,
        "profile": {
            "name": contact_data.get("name"),
            "first_name": contact_data.get("first_name"),
            "last_name": contact_data.get("last_name"),
            "profile_pic": contact_data.get("profile_pic"),
        },
        "openai_thread_id": None,
        "custom_fields": contact_data.get("custom_fields", {}),
        "tags": [f"source:{main_platform.lower()}"],
        "status": "active",
        "conversation_summary": "",
        "first_contact_date": now_utc,
        "last_contact_date": now_utc
    }
    sessions_collection.insert_one(new_session)
    logger.info(f"🆕 إنشاء جلسة جديدة للمستخدم {user_id} على {main_platform}")
    return new_session

# ===========================
# OpenAI Assistant runner (shared Assistant ID)
# ===========================
async def get_assistant_reply_async(session, content):
    user_id = session["_id"]
    thread_id = session.get("openai_thread_id")

    logger.info(f"🤖 بدء تشغيل Assistant للعميل {user_id} (platform={session.get('platform')})")

    if not thread_id:
        thread = await asyncio.to_thread(client.beta.threads.create)
        thread_id = thread.id
        sessions_collection.update_one({"_id": user_id}, {"$set": {"openai_thread_id": thread_id}})
        logger.info(f"🔧 إنشاء Thread جديد: {thread_id}")

    # نضيف رسالة المستخدم للـ thread
    await asyncio.to_thread(
        client.beta.threads.messages.create,
        thread_id=thread_id,
        role="user",
        content=content
    )

    # طلب run باستخدام نفس Assistant ID (مشفر في env ASSISTANT_ID_PREMIUM)
    run = await asyncio.to_thread(
        client.beta.threads.runs.create,
        thread_id=thread_id,
        assistant_id=ASSISTANT_ID_PREMIUM
    )

    # انتظار انتهاء الـ run
    while run.status in ["in_progress", "queued"]:
        await asyncio.sleep(1)
        run = await asyncio.to_thread(
            client.beta.threads.runs.retrieve,
            thread_id=thread_id,
            run_id=run.id
        )

    if run.status != "completed":
        logger.error("❌ Assistant run لم يكتمل بنجاح")
        return "⚠️ حصل خطأ أثناء المعالجة."

    msgs = await asyncio.to_thread(
        client.beta.threads.messages.list,
        thread_id=thread_id,
        limit=1
    )

    try:
        reply = msgs.data[0].content[0].text.value.strip()
        logger.info(f"🤖 رد المساعد: {reply}")
        return reply
    except Exception as e:
        logger.error(f"❌ فشل استخراج الرد من OpenAI: {e}")
        return "⚠️ لم أستطع استخراج الرد."

# ===========================
# إرسال ManyChat (بالتفرقة على channel حسب المنصة)
# ===========================
def send_manychat_reply(subscriber_id, text_message, platform):
    logger.info("====== 📤 DEBUG MANYCHAT SEND ======")
    logger.info(f"📌 subscriber_id: {subscriber_id}")
    logger.info(f"📌 platform: {platform}")
    logger.info(f"📩 message: {text_message}")

    if platform == "Instagram":
        channel = "instagram"
    else:
        channel = "facebook"

    url = "https://api.manychat.com/fb/sending/sendContent"

    payload = {
        "subscriber_id": str(subscriber_id),
        "channel": channel,
        "data": {
            "version": "v2",
            "content": {
                "messages": [
                    {"type": "text", "text": text_message}
                ]
            }
        }
    }

    headers = {
        "Authorization": f"Bearer {MANYCHAT_API_KEY}",
        "Content-Type": "application/json"
    }

    try:
        r = requests.post(url, headers=headers, data=json.dumps(payload))
        logger.info(f"📥 ManyChat Response Code: {r.status_code}")
        logger.info(f"📥 ManyChat Response Body: {r.text}")
        r.raise_for_status()
        logger.info("✅ الرد اتبعت بنجاح")
    except Exception as e:
        logger.error(f"❌ فشل إرسال ManyChat: {e}")

# ===========================
# Queue & Scheduler (مفصولة حسب المنصة)
# ===========================
def schedule_assistant_response(platform, user_id):
    # lock خاص بالمستخدم داخل المنصة
    lock = run_locks[platform].setdefault(user_id, threading.Lock())

    if not lock.acquire(blocking=False):
        # retry لاحقًا
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

        logger.info(f"📨 دمج الرسائل للعميل {user_id} على {platform}: {merged}")

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

    with queue_lock:
        if uid not in pending_messages[platform]:
            pending_messages[platform][uid] = {"texts": [], "session": session}

        pending_messages[platform][uid]["texts"].append(text)

        # الغاء التايمر القديم لو موجود
        if uid in message_timers[platform]:
            try:
                message_timers[platform][uid].cancel()
            except Exception:
                pass

        timer = threading.Timer(BATCH_WAIT_TIME, schedule_assistant_response, args=[platform, uid])
        message_timers[platform][uid] = timer
        timer.start()

    logger.info(f"📝 إضافة رسالة للطابور → platform={platform} uid={uid}: {text}")

# ===========================
# Webhook Endpoint
# ===========================
@app.route("/manychat_webhook", methods=["POST"])
def mc_webhook():
    logger.info("====== 🔔 NEW MANYCHAT WEBHOOK RECEIVED ======")

    if MANYCHAT_SECRET_KEY:
        auth = request.headers.get("Authorization")
        if auth != f"Bearer {MANYCHAT_SECRET_KEY}":
            logger.error("🚫 Authorization failed للـ ManyChat webhook")
            return jsonify({"error": "unauthorized"}), 403

    data = request.get_json()
    logger.info("====== 📥 RAW WEBHOOK BODY ======")
    logger.info(json.dumps(data, indent=2, ensure_ascii=False))

    contact = data.get("full_contact")
    if not contact:
        logger.error("❌ full_contact مفقود في payload")
        return jsonify({"error": "missing contact"}), 400

    # إنشاء/استخراج الجلسة مع كشف المنصة الحقيقية
    session = get_or_create_session_from_contact(contact, platform_hint=None)
    if not session:
        return jsonify({"error": "session error"}), 400

    # قراءة آخر نص أو مدخل
    txt = contact.get("last_text_input") or contact.get("last_input_text")

    # لو فيه رابط صورة/ميديا في last_text_input نقدر نحاول تنزيلها - لكن هنا ببساطة ندخل النص
    if txt:
        logger.info(f"📩 نص مستلم: {txt}")
        add_to_queue(session, txt)
    else:
        logger.warning("⚠️ لا توجد رسالة نصية في payload")

    return jsonify({"ok": True}), 200

# ===========================
# Home
# ===========================
@app.route("/")
def home():
    return "Bot running – FB & IG isolated Queues – Same Assistant"

# ===========================
# Run
# ===========================
if __name__ == "__main__":
    logger.info("🚀 السيرفر جاهز للعمل")
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", 5000)))
