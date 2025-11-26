import os
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
# LOGGING بالعربي
# ===========================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s"
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
# اتصال MongoDB
# ===========================
try:
    client_db = MongoClient(MONGO_URI)
    db = client_db["multi_platform_bot"]
    sessions_collection = db["sessions"]
    logger.info("✅ تم الاتصال بقاعدة البيانات")
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
# GLOBAL QUEUE + LOCKS
# ===========================
pending_messages = {}      # user_id → {texts:[], session:{}}
message_timers = {}        # user_id → Timer
queue_lock = threading.Lock()
run_locks = {}             # user_id → threading.Lock()

BATCH_WAIT_TIME = 9.0
RETRY_DELAY = 3.0

# ===========================
# معرفة المنصة الفعلية
# ===========================
def detect_platform(contact):
    source = (contact.get("source") or "").lower()

    if "ig" in source or "instagram" in source:
        return "Instagram"

    if "fb" in source or "facebook" in source:
        return "Facebook"

    return "Facebook"

# ===========================
# إدارة السيشن
# ===========================
def get_or_create_session(contact):
    user_id = str(contact.get("id"))

    platform = detect_platform(contact)
    now = datetime.now(timezone.utc)

    session = sessions_collection.find_one({"_id": user_id})
    if session:
        sessions_collection.update_one(
            {"_id": user_id},
            {"$set": {
                "last_contact_date": now,
                "platform": platform,
                "profile.name": contact.get("name"),
                "profile.pic": contact.get("profile_pic"),
                "status": "active"
            }}
        )
        return sessions_collection.find_one({"_id": user_id})

    new_session = {
        "_id": user_id,
        "platform": platform,
        "profile": {
            "name": contact.get("name"),
            "pic": contact.get("profile_pic"),
        },
        "openai_thread_id": None,
        "status": "active",
        "first_contact_date": now,
        "last_contact_date": now
    }
    sessions_collection.insert_one(new_session)
    logger.info(f"🆕 إنشاء جلسة جديدة للمستخدم {user_id}")
    return new_session

# ===========================
# Vision + Whisper
# ===========================
async def get_image_description(base64_img):
    try:
        res = await asyncio.to_thread(
            client.chat.completions.create,
            model="gpt-4.1",
            messages=[
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "اقرأ محتوى الصورة بدقة."},
                        {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{base64_img}"}}
                    ]
                }
            ]
        )
        return res.choices[0].message.content
    except:
        return None

def transcribe_audio(content_bytes, fmt="mp4"):
    try:
        tmp = tempfile.NamedTemporaryFile(delete=False, suffix=f".{fmt}")
        tmp.write(content_bytes)
        tmp.close()

        with open(tmp.name, "rb") as f:
            tr = client.audio.transcriptions.create(model="whisper-1", file=f)

        os.remove(tmp.name)
        return tr.text
    except:
        return None

# ===========================
# OpenAI THREAD HANDLER
# ===========================
async def openai_reply(session, message):
    uid = session["_id"]
    thread_id = session.get("openai_thread_id")

    if not thread_id:
        t = await asyncio.to_thread(client.beta.threads.create)
        thread_id = t.id
        sessions_collection.update_one({"_id": uid}, {"$set": {"openai_thread_id": thread_id}})
        logger.info(f"🔧 Thread جديد للمستخدم {uid}: {thread_id}")

    await asyncio.to_thread(
        client.beta.threads.messages.create,
        thread_id=thread_id,
        role="user",
        content=message
    )

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
        return "⚠️ حصل خطأ أثناء المعالجة."

    msgs = await asyncio.to_thread(
        client.beta.threads.messages.list,
        thread_id=thread_id,
        limit=1
    )

    try:
        return msgs.data[0].content[0].text.value
    except:
        return "⚠️ لم أستطع استخراج رد المساعد."

# ===========================
# إرسال ManyChat (نهائي)
# ===========================
def send_manychat_reply(uid, text, platform):
    if platform.lower() == "instagram":
        url = "https://api.manychat.com/ig/sending/sendContent"
        channel = "instagram"
    else:
        url = "https://api.manychat.com/fb/sending/sendContent"
        channel = "facebook"

    payload = {
        "subscriber_id": str(uid),
        "channel": channel,
        "data": {
            "version": "v2",
            "content": {
                "messages": [{"type": "text", "text": text}]
            }
        }
    }

    headers = {
        "Authorization": f"Bearer {MANYCHAT_API_KEY}",
        "Content-Type": "application/json"
    }

    try:
        r = requests.post(url, headers=headers, data=json.dumps(payload))
        r.raise_for_status()
        logger.info("✅ تم إرسال الرد بنجاح")
    except Exception as e:
        logger.error(f"❌ فشل ManyChat: {e}", exc_info=True)

# ===========================
# BATCHING + SCHEDULING
# ===========================
def add_to_queue(session, text):
    uid = session["_id"]
    with queue_lock:
        if uid not in pending_messages:
            pending_messages[uid] = {"texts": [], "session": session}
        pending_messages[uid]["texts"].append(text)

        t = message_timers.get(uid)
        if t and t.is_alive():
            t.cancel()

        new_t = threading.Timer(BATCH_WAIT_TIME, schedule_response, args=[uid])
        new_t.daemon = True
        message_timers[uid] = new_t
        new_t.start()

def schedule_response(uid):
    lock = run_locks.setdefault(uid, threading.Lock())

    if not lock.acquire(blocking=False):
        t = threading.Timer(RETRY_DELAY, schedule_response, args=[uid])
        t.daemon = True
        t.start()
        return

    try:
        with queue_lock:
            data = pending_messages.pop(uid, None)
            message_timers.pop(uid, None)

        if not data:
            return

        session = data["session"]
        merged = "\n".join(data["texts"])

        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        reply = loop.run_until_complete(openai_reply(session, merged))
        loop.close()

        send_manychat_reply(uid, reply, session["platform"])
    finally:
        lock.release()

# ===========================
# WEBHOOK
# ===========================
@app.route("/manychat_webhook", methods=["POST"])
def webhook():
    if MANYCHAT_SECRET_KEY:
        if request.headers.get("Authorization") != f"Bearer {MANYCHAT_SECRET_KEY}":
            return jsonify({"error": "unauthorized"}), 403

    data = request.get_json()
    contact = data.get("full_contact")

    session = get_or_create_session(contact)
    txt = contact.get("last_text_input")

    if txt and txt.strip():
        add_to_queue(session, txt)

    return jsonify({"ok": True})

# ===========================
# HOME
# ===========================
@app.route("/")
def home():
    return "Bot Running V4 Final – عربي"

# ===========================
# RUN SERVER
# ===========================
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", 5000)))
