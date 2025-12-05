# ============================================================
# main.py — FINAL VERSION (Messages Aggregation FIXED)
# ============================================================

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
# LOGGING
# ===========================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)

def debug(title, data=None):
    logger.info("\n" + "="*70)
    logger.info(f"🔍 {title}")
    if data is not None:
        try:
            logger.info(json.dumps(data, indent=2, ensure_ascii=False))
        except:
            logger.info(str(data))
    logger.info("="*70)

# ===========================
# LOAD ENV
# ===========================
load_dotenv()
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
ASSISTANT_ID_PREMIUM = os.getenv("ASSISTANT_ID_PREMIUM")
MONGO_URI = os.getenv("MONGO_URI")
MANYCHAT_API_KEY = os.getenv("MANYCHAT_API_KEY")
MANYCHAT_SECRET_KEY = os.getenv("MANYCHAT_SECRET_KEY")
PORT = int(os.getenv("PORT", 5000))

# ===========================
# DB (Mongo)
# ===========================
client_db = MongoClient(MONGO_URI)
db = client_db["multi_platform_bot"]
sessions_collection = db["sessions"]

# ===========================
# Flask + OpenAI
# ===========================
app = Flask(__name__)
client = OpenAI(api_key=OPENAI_API_KEY)

# ===========================
# QUEUE VARIABLES
# ===========================
pending_messages = {"Facebook": {}, "Instagram": {}}
message_timers = {"Facebook": {}, "Instagram": {}}
run_locks = {"Facebook": {}, "Instagram": {}}
queue_lock = threading.Lock()

BATCH_WAIT_TIME = 8.0
RETRY_DELAY_WHEN_BUSY = 6.5

# ============================================================
# 1) UNIVERSAL MESSAGE EXTRACTOR (FIX)
# ============================================================
def extract_message_text(data):
    """
    يلتقط كل أنواع الرسائل من ManyChat مهما كان نوعها.
    """
    contact = data.get("full_contact", {})
    msg = data.get("message", {})

    # أفضل مصدر لنص المستخدم
    for key in ["last_text_input", "last_input_text", "last_input"]:
        if contact.get(key):
            return contact[key]

    # نص مباشر
    if msg.get("text"):
        return msg["text"]

    # Quick Replies / Buttons
    if msg.get("payload"):
        return str(msg["payload"])

    # IMAGE
    if msg.get("attachment") and msg["attachment"].get("type") == "image":
        return msg["attachment"]["payload"]["url"]

    # AUDIO
    if msg.get("attachment") and msg["attachment"].get("type") == "audio":
        return msg["attachment"]["payload"]["url"]

    return ""

# ============================================================
# Media Helpers
# ============================================================
def download_media_from_url(url, timeout=20):
    try:
        r = requests.get(url, timeout=timeout, stream=True)
        r.raise_for_status()
        return r.content
    except:
        return None

def transcribe_audio_bytes(content_bytes, fmt="mp4"):
    try:
        with tempfile.NamedTemporaryFile(delete=False, suffix=f".{fmt}") as tmp:
            tmp.write(content_bytes)
            filepath = tmp.name

        with open(filepath, "rb") as f:
            res = client.audio.transcriptions.create(model="whisper-1", file=f)

        os.remove(filepath)
        return res.text
    except:
        return None

async def get_image_description(base64_image):
    try:
        res = await asyncio.to_thread(
            client.chat.completions.create,
            model="gpt-4o",
            messages=[
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "حلل الصورة وأخرج وصفًا دقيقًا."},
                        {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{base64_image}"}}
                    ]
                }
            ]
        )
        return res.choices[0].message.content
    except:
        return None

# ============================================================
# Content Processor
# ============================================================
def is_image_url(url):
    url = url.lower()
    return any(x in url for x in [".jpg", ".jpeg", ".png", ".webp", ".gif"])

def is_audio_url(url):
    url = url.lower()
    return any(x in url for x in [".mp3", ".wav", ".m4a", ".ogg", ".mp4"])

def process_incoming_payload_text(data):
    raw = extract_message_text(data)
    debug("📝 RAW USER INPUT", raw)

    if not raw:
        return "[EMPTY]"

    parts = []

    tokens = raw.split()
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    try:
        for token in tokens:
            if token.startswith("http"):
                # IMAGE
                if is_image_url(token):
                    bytes_img = download_media_from_url(token)
                    if bytes_img:
                        b64 = base64.b64encode(bytes_img).decode()
                        desc = loop.run_until_complete(get_image_description(b64))
                        parts.append(f"صورة: {desc}")
                    else:
                        parts.append("فشل تحميل الصورة")
                # AUDIO
                elif is_audio_url(token):
                    bytes_audio = download_media_from_url(token)
                    if bytes_audio:
                        txt = transcribe_audio_bytes(bytes_audio, fmt="mp4")
                        parts.append(f"صوت: {txt}")
                    else:
                        parts.append("فشل تحميل الصوت")
                else:
                    parts.append(f"رابط: {token}")
            else:
                parts.append(token)
    finally:
        loop.close()

    merged = " ".join(parts)
    debug("➡ MERGED TEXT", merged)
    return merged

# ============================================================
# SESSION HANDLER
# ============================================================
def get_or_create_session(contact):
    user_id = str(contact.get("id"))
    platform = "Instagram" if contact.get("ig_id") else "Facebook"

    session = sessions_collection.find_one({"_id": user_id})
    now = datetime.now(timezone.utc)

    if session:
        sessions_collection.update_one(
            {"_id": user_id},
            {"$set": {"last_contact_date": now, "platform": platform}}
        )
        return sessions_collection.find_one({"_id": user_id})

    new_session = {
        "_id": user_id,
        "platform": platform,
        "openai_conversation_id": None,
        "first_contact_date": now,
        "last_contact_date": now
    }
    sessions_collection.insert_one(new_session)
    return new_session

# ============================================================
# ASSISTANT REQUEST
# ============================================================
async def get_assistant_reply(session, text):
    conv_id = session.get("openai_conversation_id")

    if not conv_id:
        conv = await asyncio.to_thread(client.conversations.create, items=[], metadata={"user": session["_id"]})
        conv_id = conv.id
        sessions_collection.update_one({"_id": session["_id"]}, {"$set": {"openai_conversation_id": conv_id}})

    payload = {
        "prompt": {"id": "pmpt_691df223bd3881909e4e9c544a56523b006e1332a5ce0f11", "version": "5"},
        "conversation": conv_id,
        "store": True,
        "input": [{"role": "user", "content": text}]
    }

    res = await asyncio.to_thread(client.responses.create, **payload)

    if hasattr(res, "output_text") and res.output_text:
        return res.output_text

    return "⚠ خطأ في توليد الرد."

# ============================================================
# SEND TO MANYCHAT
# ============================================================
def send_manychat_reply(user_id, text, platform="Facebook"):
    url = "https://api.manychat.com/fb/sending/sendContent"
    headers = {"Authorization": f"Bearer {MANYCHAT_API_KEY}", "Content-Type": "application/json"}

    msg = {
        "subscriber_id": str(user_id),
        "channel": "instagram" if platform == "Instagram" else "facebook",
        "data": {"version": "v2", "content": {"messages": [{"type": "text", "text": text}]}}
    }

    requests.post(url, json=msg)

# ============================================================
# QUEUE SYSTEM
# ============================================================
def schedule_assistant_response(platform, user_id):
    lock = run_locks[platform].setdefault(user_id, threading.Lock())

    if not lock.acquire(blocking=False):
        threading.Timer(RETRY_DELAY_WHEN_BUSY, schedule_assistant_response, args=[platform, user_id]).start()
        return

    try:
        with queue_lock:
            data = pending_messages[platform].pop(user_id, None)
            if not data:
                return
            merged = "\n".join(data["texts"])
            session = data["session"]

        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        reply = loop.run_until_complete(get_assistant_reply(session, merged))
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

        if uid in message_timers[platform]:
            try: message_timers[platform][uid].cancel()
            except: pass

        timer = threading.Timer(BATCH_WAIT_TIME, schedule_assistant_response, args=[platform, uid])
        message_timers[platform][uid] = timer
        timer.start()

# ============================================================
# WEBHOOK
# ============================================================
@app.route("/manychat_webhook", methods=["POST"])
def mc_webhook():
    data = request.get_json()
    debug("📩 NEW WEBHOOK", data)

    if not data:
        return {"error": "bad_payload"}, 400

    contact = data.get("full_contact")
    if not contact:
        return {"error": "no contact"}, 400

    session = get_or_create_session(contact)

    processed = process_incoming_payload_text(data)
    add_to_queue(session, processed)

    return {"ok": True}

# ============================================================
# HOME
# ============================================================
@app.route("/")
def home():
    return "Bot running (Final Version)"

# ============================================================
# RUN
# ============================================================
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=PORT)
