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
# إعداد اللوجات
# ===========================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)

logger.info("▶️ بدء تشغيل التطبيق...")

# ===========================
# تحميل الإعدادات
# ===========================
load_dotenv()

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
ASSISTANT_ID_PREMIUM = os.getenv("ASSISTANT_ID_PREMIUM")
MONGO_URI = os.getenv("MONGO_URI")

MANYCHAT_API_KEY = os.getenv("MANYCHAT_API_KEY")
MANYCHAT_SECRET_KEY = os.getenv("MANYCHAT_SECRET_KEY")

# ===========================
# اتصال بقاعدة البيانات
# ===========================
try:
    client_db = MongoClient(MONGO_URI)
    db = client_db["multi_platform_bot"]
    sessions_collection = db["sessions"]
    logger.info("✅ متصل بقاعدة البيانات")
except Exception as e:
    logger.error(f"❌ خطأ DB: {e}")
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
pending_messages = {}
message_timers = {}
queue_lock = threading.Lock()
run_locks = {}

BATCH_WAIT_TIME = 9.0
RETRY_DELAY_WHEN_BUSY = 3.0

# ===========================
# السيشن
# ===========================
def get_or_create_session_from_contact(contact_data, platform):

    logger.info("====== 🧾 DEBUG CONTACT DATA ======")
    logger.info(json.dumps(contact_data, indent=2, ensure_ascii=False))

    user_id = str(contact_data.get("id"))

    if not user_id:
        logger.error("❌ user_id غير موجود")
        return None

    logger.info(f"📌 subscriber_id = {user_id}")

    source = contact_data.get("source", "").lower()
    if "instagram" in source:
        main_platform = "Instagram"
    else:
        main_platform = "Facebook"

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
                "status": "active"
            }}
        )
        return sessions_collection.find_one({"_id": user_id})

    session = {
        "_id": user_id,
        "platform": main_platform,
        "profile": {
            "name": contact_data.get("name"),
            "profile_pic": contact_data.get("profile_pic")
        },
        "openai_thread_id": None,
        "status": "active",
        "first_contact_date": now_utc,
        "last_contact_date": now_utc
    }

    sessions_collection.insert_one(session)
    logger.info(f"🆕 إنشاء جلسة جديدة للمستخدم {user_id}")

    return session


# ===========================
# Vision + Whisper
# ===========================
async def get_assistant_reply_async(session, content):
    user_id = session["_id"]
    logger.info(f"🤖 بدء تشغيل Assistant للعميل {user_id}")

    thread_id = session.get("openai_thread_id")

    if not thread_id:
        thread = await asyncio.to_thread(client.beta.threads.create)
        thread_id = thread.id
        sessions_collection.update_one({"_id": user_id}, {"$set": {"openai_thread_id": thread_id}})
        logger.info(f"🔧 إنشاء Thread جديد: {thread_id}")

    await asyncio.to_thread(
        client.beta.threads.messages.create,
        thread_id=thread_id,
        role="user",
        content=content
    )

    logger.info("⌛ تشغيل المعالجة...")

    run = await asyncio.to_thread(
        client.beta.threads.runs.create,
        thread_id=thread_id,
        assistant_id=ASSISTANT_ID_PREMIUM
    )

    while run.status in ["in_progress", "queued"]:
        await asyncio.sleep(1)
        run = await asyncio.to_thread(
            client.beta.threads.runs.retrieve,
            thread_id=thread_id,
            run_id=run.id
        )

    if run.status != "completed":
        logger.error("❌ Assistant run لم يكتمل")
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
    except:
        logger.error("❌ لم أستطع استخراج رد OpenAI")
        return "⚠️ لم أستطع استخراج الرد."


# ===========================
# إرسال ManyChat + Debug
# ===========================
def send_manychat_reply(subscriber_id, text_message, platform):

    logger.info("====== 📤 DEBUG MANYCHAT SEND ======")
    logger.info(f"📌 subscriber_id: {subscriber_id}")
    logger.info(f"📌 platform: {platform}")
    logger.info(f"📩 message: {text_message}")

    url = "https://api.manychat.com/fb/sending/sendContent"

    payload = {
        "subscriber_id": str(subscriber_id),
        "channel": "facebook",
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
        logger.error(f"❌ خطأ إرسال ManyChat: {e}")


# ===========================
# Queue + Scheduler
# ===========================
def schedule_assistant_response(user_id):
    lock = run_locks.setdefault(user_id, threading.Lock())

    if not lock.acquire(blocking=False):
        threading.Timer(RETRY_DELAY_WHEN_BUSY, schedule_assistant_response, args=[user_id]).start()
        return

    try:
        with queue_lock:
            data = pending_messages.pop(user_id, None)
            message_timers.pop(user_id, None)

        if not data:
            return

        session = data["session"]
        text = "\n".join(data["texts"])

        logger.info(f"📨 دمج الرسائل للعميل {user_id}: {text}")

        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        reply = loop.run_until_complete(get_assistant_reply_async(session, text))
        loop.close()

        send_manychat_reply(user_id, reply, session["platform"])

    finally:
        lock.release()


def add_to_queue(session, text):
    uid = session["_id"]

    with queue_lock:
        if uid not in pending_messages:
            pending_messages[uid] = {"texts": [], "session": session}

        pending_messages[uid]["texts"].append(text)

        if uid in message_timers:
            message_timers[uid].cancel()

        timer = threading.Timer(BATCH_WAIT_TIME, schedule_assistant_response, args=[uid])
        message_timers[uid] = timer
        timer.start()

    logger.info(f"📝 إضافة رسالة للطابور → {uid}: {text}")


# ===========================
# Webhook
# ===========================
@app.route("/manychat_webhook", methods=["POST"])
def mc_webhook():

    logger.info("====== 🔔 NEW MANYCHAT WEBHOOK RECEIVED ======")

    if MANYCHAT_SECRET_KEY:
        auth = request.headers.get("Authorization")
        if auth != f"Bearer {MANYCHAT_SECRET_KEY}":
            logger.error("🚫 Authorization failed")
            return jsonify({"error": "unauthorized"}), 403

    data = request.get_json()

    logger.info("====== 📥 RAW WEBHOOK BODY ======")
    logger.info(json.dumps(data, indent=2, ensure_ascii=False))

    contact = data.get("full_contact")

    if not contact:
        logger.error("❌ full_contact مفقود")
        return jsonify({"error": "missing contact"}), 400

    session = get_or_create_session_from_contact(contact, "ManyChat")

    txt = contact.get("last_text_input") or contact.get("last_input_text")

    if txt:
        logger.info(f"📩 نص مستلم: {txt}")
        add_to_queue(session, txt)
    else:
        logger.warning("⚠️ لا توجد رسالة نصية")

    return jsonify({"ok": True}), 200


@app.route("/")
def home():
    return "Bot running – DEBUG MODE"


if __name__ == "__main__":
    logger.info("🚀 السيرفر جاهز للعمل")
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", 5000)))
