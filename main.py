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
logger.info("▶️ بدء تشغيل التطبيق — FINAL VERSION WITH CORRECT SEND MANYCHAT")

def debug(title, data=None):
    logger.info("\n" + "="*70)
    logger.info(f"🔍 {title}")
    if data is not None:
        try: logger.info(json.dumps(data, indent=2, ensure_ascii=False))
        except: logger.info(str(data))
    logger.info("="*70 + "\n")

# ===========================
# تحميل الإعدادات
# ===========================
load_dotenv()
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
MONGO_URI = os.getenv("MONGO_URI")
MANYCHAT_API_KEY = os.getenv("MANYCHAT_API_KEY")
MANYCHAT_SECRET_KEY = os.getenv("MANYCHAT_SECRET_KEY")

# ===========================
# إعداد قواعد البيانات
# ===========================
client_db = MongoClient(MONGO_URI) if MONGO_URI else None
db = client_db["multi_platform_bot"] if client_db else None
sessions_collection = db["sessions"] if db else None

# ===========================
# Flask + OpenAI
# ===========================
app = Flask(__name__)
client = OpenAI(api_key=OPENAI_API_KEY)

# ===========================
# مخازن الانتظار
# ===========================
pending_messages = {"Facebook": {}, "Instagram": {}}
message_timers = {"Facebook": {}, "Instagram": {}}
run_locks = {"Facebook": {}, "Instagram": {}}
queue_lock = threading.Lock()
BATCH_WAIT_TIME = 2.2
RETRY_DELAY_WHEN_BUSY = 2.5

# ===========================
# تحميل الميديا
# ===========================
def download_media_from_url(url, timeout=20):
    try:
        r = requests.get(url, timeout=timeout, stream=True)
        r.raise_for_status()
        return r.content, r.headers.get("Content-Type", "")
    except:
        return None, None

# ===========================
# تحويل الصوت
# ===========================
def transcribe_audio(content_bytes, fmt="mp4"):
    try:
        with tempfile.NamedTemporaryFile(delete=False, suffix=f".{fmt}") as tmp:
            tmp.write(content_bytes)
            path = tmp.name
        with open(path, "rb") as f:
            tr = client.audio.transcriptions.create(model="whisper-1", file=f)
        os.remove(path)
        return tr.text
    except:
        return None

# ===========================
# رؤية الصور
# ===========================
async def get_image_description_for_assistant(base64_image):
    try:
        response = await asyncio.to_thread(
            client.chat.completions.create,
            model="gpt-4o",
            messages=[{
                "role": "user",
                "content": [
                    {"type": "text", "text": "اقرأ محتوى الصورة وحوله لنص مفصل."},
                    {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{base64_image}"}}
                ]
            }],
            max_tokens=400
        )
        return response.choices[0].message.content
    except:
        return None

# ===========================
# استخراج النصوص من الروابط
# ===========================
def detect_and_extract_text_from_input(raw_text):
    if not raw_text or not isinstance(raw_text, str):
        return ""

    import re
    url_pattern = re.compile(r'(https?://\S+)')
    urls = url_pattern.findall(raw_text)
    extracted = []

    if not urls:
        return raw_text.strip()

    for u in urls:
        content, ctype = download_media_from_url(u)
        if not content: continue

        if "image" in ctype:
            try:
                b64 = base64.b64encode(content).decode()
                loop = asyncio.new_event_loop()
                asyncio.set_event_loop(loop)
                desc = loop.run_until_complete(get_image_description_for_assistant(b64))
                loop.close()
                extracted.append("وصف الصورة: " + desc if desc else "فشل وصف الصورة")
            except:
                extracted.append("خطأ في معالجة الصورة")
            continue

        if "audio" in ctype or "video" in ctype:
            fmt = "mp4" if "mp4" in ctype else "mp3"
            text = transcribe_audio(content, fmt)
            extracted.append("نص التسجيل: " + text if text else "فشل استخراج نص الصوت")
            continue

        extracted.append(f"رابط: {u}")

    remaining = url_pattern.sub('', raw_text).strip()
    if remaining: extracted.insert(0, remaining)

    return "\n".join(extracted)

# ===========================
# الجلسة
# ===========================
def get_or_create_session_from_contact(contact, platform_hint=None):
    user_id = str(contact.get("id"))

    if contact.get("ig_id") or contact.get("ig_last_interaction"):
        platform = "Instagram"
    else:
        platform = "Facebook"

    now = datetime.now(timezone.utc)

    session = sessions_collection.find_one({"_id": user_id})
    if session:
        sessions_collection.update_one({"_id": user_id}, {"$set": {
            "platform": platform,
            "last_contact_date": now
        }})
        return sessions_collection.find_one({"_id": user_id})

    new = {
        "_id": user_id,
        "platform": platform,
        "profile": {
            "name": contact.get("name"),
            "profile_pic": contact.get("profile_pic")
        },
        "openai_conversation_id": None,
        "first_contact_date": now,
        "last_contact_date": now
    }
    sessions_collection.insert_one(new)
    return new

# ===========================
# typing indicator
# ===========================
def send_typing_action(subscriber_id, platform):
    url = "https://api.manychat.com/fb/sending/sendContent"
    headers = {"Authorization": f"Bearer {MANYCHAT_API_KEY}", "Content-Type": "application/json"}

    payload = {
        "subscriber_id": str(subscriber_id),
        "data": {"version": "v2", "content": {"type": "typing_on"}}
    }
    try:
        requests.post(url, headers=headers, data=json.dumps(payload), timeout=2)
    except:
        pass

# ===========================
# Assistant (Responses API)
# ===========================
async def get_assistant_reply_async(session, content):
    conv_id = session.get("openai_conversation_id")

    if not conv_id:
        conv = await asyncio.to_thread(client.conversations.create, items=[], metadata={"user": session["_id"]})
        conv_id = conv.id
        sessions_collection.update_one({"_id": session["_id"]}, {"$set": {"openai_conversation_id": conv_id}})

    payload = {
        "prompt": {"id": "pmpt_691df223bd3881909e4e9c544a56523b006e1332a5ce0f11", "version": "4"},
        "input": [{"role": "user", "content": content}],
        "store": True,
        "conversation": conv_id
    }

    r = await asyncio.to_thread(client.responses.create, **payload)

    # استخراج النص بدقة
    if hasattr(r, "output_text") and r.output_text:
        return r.output_text

    if hasattr(r, "output"):
        for item in r.output:
            if hasattr(item, "content"):
                for c in item.content:
                    if c.get("type") == "output_text":
                        return c["text"]["value"]

    return "⚠️ حصل خطأ في الرد."

# ===========================
# إرسال ManyChat (الصحيح 100%)
# ===========================
def send_manychat_reply(subscriber_id, text_message, platform):
    debug("📤 Sending reply", text_message)

    url = "https://api.manychat.com/fb/sending/sendContent"
    headers = {"Authorization": f"Bearer {MANYCHAT_API_KEY}", "Content-Type": "application/json"}
    channel = "instagram" if platform == "Instagram" else "facebook"

    # 1) إرسال بدون تاج
    payload_clean = {
        "subscriber_id": str(subscriber_id),
        "channel": channel,
        "data": {
            "version": "v2",
            "content": {"messages": [{"type": "text", "text": text_message}]}
        }
    }

    try:
        r = requests.post(url, headers=headers, data=json.dumps(payload_clean), timeout=10)
        if r.status_code == 200:
            return {"ok": True}
    except:
        pass

    # 2) إرسال بالتاج الصحيح
    tags = ["HUMAN_AGENT", "ACCOUNT_UPDATE", "CONFIRMED_EVENT_UPDATE"]

    for tag in tags:
        payload_tag = {
            "subscriber_id": str(subscriber_id),
            "channel": channel,
            "data": {
                "version": "v2",
                "message_tag": tag,
                "content": {"messages": [{"type": "text", "text": text_message}]}
            }
        }
        try:
            r2 = requests.post(url, headers=headers, data=json.dumps(payload_tag), timeout=10)
            if r2.status_code == 200:
                return {"ok": True}
        except:
            pass

    return {"ok": False}

# ===========================
# Queue System
# ===========================
def schedule_assistant_response(platform, user_id):
    lock = run_locks[platform].setdefault(user_id, threading.Lock())

    if not lock.acquire(blocking=False):
        threading.Timer(RETRY_DELAY_WHEN_BUSY, schedule_assistant_response, args=[platform, user_id]).start()
        return

    try:
        with queue_lock:
            data = pending_messages[platform].pop(user_id, None)
            if user_id in message_timers[platform]:
                message_timers[platform][user_id].cancel()
        if not data: return

        session = data["session"]
        merged = "\n".join(data["texts"])

        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        reply = loop.run_until_complete(get_assistant_reply_async(session, merged))
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
            threading.Thread(target=send_typing_action, args=(uid, platform)).start()

        processed = detect_and_extract_text_from_input(text)
        pending_messages[platform][uid]["texts"].append(processed)

        if uid in message_timers[platform]:
            message_timers[platform][uid].cancel()

        timer = threading.Timer(BATCH_WAIT_TIME, schedule_assistant_response, args=[platform, uid])
        message_timers[platform][uid] = timer
        timer.start()

# ===========================
# Webhook
# ===========================
@app.route("/manychat_webhook", methods=["POST"])
def mc_webhook():
    data = request.get_json()
    contact = data.get("full_contact")
    if not contact:
        return jsonify({"error": "no contact"}), 400

    user_id = str(contact.get("id"))
    session = get_or_create_session_from_contact(contact)

    txt = (
        contact.get("last_text_input")
        or contact.get("last_input_text")
        or contact.get("last_input")
    )

    if txt:
        add_to_queue(session, txt)

    return jsonify({"ok": True})

@app.route("/")
def home():
    return "Bot running — FINAL VERSION"

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", 5000)))
