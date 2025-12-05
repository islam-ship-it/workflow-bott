import os
import re
import json
import time
import base64
import tempfile
import threading
import logging
import requests
import asyncio
from datetime import datetime, timezone
from flask import Flask, request, jsonify
from pymongo import MongoClient
from dotenv import load_dotenv
from openai import OpenAI

# ============================================================
# LOGGING
# ============================================================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger("bot")

def debug(title, data=None):
    logger.info("\n" + "="*60)
    logger.info(f"🔍 {title}")
    if data is not None:
        try:
            logger.info(json.dumps(data, indent=2, ensure_ascii=False))
        except:
            logger.info(str(data))
    logger.info("="*60 + "\n")

# ============================================================
# ENV
# ============================================================
load_dotenv()

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
MONGO_URI = os.getenv("MONGO_URI")
MANYCHAT_API_KEY = os.getenv("MANYCHAT_API_KEY")
MANYCHAT_SECRET_KEY = os.getenv("MANYCHAT_SECRET_KEY")
PORT = int(os.getenv("PORT", 5000))

client = OpenAI(api_key=OPENAI_API_KEY)

# ============================================================
# DB INIT
# ============================================================
sessions_collection = None
try:
    mongo_client = MongoClient(MONGO_URI)
    db = mongo_client["multi_platform_bot"]
    sessions_collection = db["sessions"]
    logger.info("✅ متصل بقاعدة البيانات")
except:
    logger.warning("⚠ لا يمكن الاتصال بـ Mongo (تشغيل بدون DB)")

# ============================================================
# FLASK
# ============================================================
app = Flask(__name__)

# ============================================================
# QUEUE SYSTEM
# ============================================================
pending_messages = {"Facebook": {}, "Instagram": {}}
message_timers = {"Facebook": {}, "Instagram": {}}
queue_lock = threading.Lock()

BATCH_WAIT_TIME = 2.0

# ============================================================
# UTILITIES
# ============================================================
def download_media(url):
    try:
        debug("📥 Downloading Media", url)
        r = requests.get(url, timeout=20, headers={"User-Agent": "Mozilla"})
        r.raise_for_status()
        return r.content
    except Exception as e:
        debug("❌ media download failed", str(e))
        return None

def whisper_transcribe(path):
    try:
        with open(path, "rb") as f:
            tr = client.audio.transcriptions.create(model="whisper-1", file=f)
        return tr.text or ""
    except Exception as e:
        debug("❌ whisper failed", str(e))
        return ""

async def vision_describe(b64_img):
    try:
        def call():
            r = client.chat.completions.create(
                model="gpt-4o",
                messages=[{
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "حلّل الصورة في نص واضح."},
                        {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64_img}"}}
                    ]
                }]
            )
            return r.choices[0].message.content

        return await asyncio.to_thread(call)
    except Exception as e:
        debug("❌ vision failed", str(e))
        return ""

def detect_type(text):
    """
    يحدد النوع:
    - ريكورد صوت (mp3/m4a/wav/audioclip.mp4)
    - صورة
    - فيديو (ignored completely)
    - نص
    """
    if not text:
        return ("text", text)

    # نبحث عن لينك
    url_re = r"(https?://[^\s]+)"
    m = re.search(url_re, text)
    if not m:
        return ("text", text)

    url = m.group(1).strip()
    low = url.lower()

    # 🔊 الصوت (بما فيها audioclip mp4)
    if "audioclip" in low or low.endswith(".mp3") or low.endswith(".m4a") or low.endswith(".wav"):
        return ("audio", url)

    # 🖼 صورة
    if any(x in low for x in [".jpg", ".jpeg", ".png", ".webp"]):
        return ("image", url)

    # 🎬 فيديو (نتجاهله تمامًا)
    if low.endswith(".mp4") or low.endswith(".mov") or low.endswith(".mkv"):
        # BUT… لو مش audioclip → تجاهل
        if "audioclip" not in low:
            return ("video", url)
        return ("audio", url)

    return ("text", text)

# ============================================================
# SESSION
# ============================================================
def get_or_create_session(contact):
    user_id = str(contact.get("id"))
    if not sessions_collection:
        return {"_id": user_id, "platform": "Facebook"}

    s = sessions_collection.find_one({"_id": user_id})
    if s:
        return s

    new_s = {
        "_id": user_id,
        "platform": "Facebook",
        "openai_conversation_id": None
    }
    sessions_collection.insert_one(new_s)
    return new_s

# ============================================================
# TYPING SIGNAL
# ============================================================
def send_typing(user_id):
    try:
        url = "https://api.manychat.com/fb/sending/sendContent"
        headers = {"Authorization": f"Bearer {MANYCHAT_API_KEY}", "Content-Type": "application/json"}
        payload = {
            "subscriber_id": str(user_id),
            "data": {"version": "v2", "content": {"type": "typing_on"}}
        }
        requests.post(url, headers=headers, json=payload, timeout=2)
    except:
        pass

# ============================================================
# OPENAI ASSISTANT
# ============================================================
async def ask_ai(session, text):
    conv = session.get("openai_conversation_id")

    # create conversation if needed
    if not conv and sessions_collection:
        c = client.conversations.create(items=[])
        conv = c.id
        sessions_collection.update_one({"_id": session["_id"]}, {"$set": {"openai_conversation_id": conv}})

    payload = {
        "prompt": {
            "id": "pmpt_691df223bd3881909e4e9c544a56523b006e1332a5ce0f11",
            "version": "4",
        },
        "input": [{"role": "user", "content": text}],
        "store": True,
    }

    if conv:
        payload["conversation"] = conv

    r = await asyncio.to_thread(client.responses.create, **payload)

    if hasattr(r, "output_text") and r.output_text:
        return r.output_text

    # fallback
    try:
        return r.output[0].content[0]["text"]["value"]
    except:
        return "⚠ خطأ أثناء الرد"

# ============================================================
# QUEUE
# ============================================================
def schedule(platform, user_id):
    session = sessions_collection.find_one({"_id": user_id})

    with queue_lock:
        data = pending_messages[platform].pop(user_id, None)
        message_timers[platform].pop(user_id, None)

    if not data:
        return

    merged = "\n".join(data["texts"])
    debug("📝 MERGED", merged)

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    reply = loop.run_until_complete(ask_ai(session, merged))
    loop.close()

    send_reply(user_id, reply)

def add_to_queue(session, text):
    platform = session.get("platform", "Facebook")
    uid = session["_id"]

    with queue_lock:
        if uid not in pending_messages[platform]:
            threading.Thread(target=send_typing, args=(uid,)).start()
            pending_messages[platform][uid] = {"texts": [], "session": session}

        pending_messages[platform][uid]["texts"].append(text)

        if uid in message_timers[platform]:
            try: message_timers[platform][uid].cancel()
            except: pass

        t = threading.Timer(BATCH_WAIT_TIME, schedule, args=[platform, uid])
        message_timers[platform][uid] = t
        t.start()

# ============================================================
# SEND MANYCHAT REPLY
# ============================================================
def send_reply(user_id, msg):
    url = "https://api.manychat.com/fb/sending/sendContent"
    headers = {"Authorization": f"Bearer {MANYCHAT_API_KEY}", "Content-Type": "application/json"}

    payload = {
        "subscriber_id": str(user_id),
        "channel": "facebook",
        "data": {
            "version": "v2",
            "content": {"messages": [{"type": "text", "text": msg}]}
        }
    }
    requests.post(url, headers=headers, json=payload)

# ============================================================
# WEBHOOK
# ============================================================
@app.route("/manychat_webhook", methods=["POST"])
def webhook():
    data = request.get_json()
    contact = data.get("full_contact", {})
    user_id = contact.get("id")

    session = get_or_create_session(contact)

    txt = (
        contact.get("last_text_input")
        or contact.get("last_input_text")
        or contact.get("last_input")
        or contact.get("last_media_url")
        or contact.get("last_attachment_url")
    )

    debug("RAW INPUT", txt)

    if not txt:
        return jsonify({"ok": True})

    ctype, value = detect_type(txt)
    debug("TYPE", {"type": ctype, "value": value})

    parts = []

    # 1) النص الأصلي (لو موجود)
    url_re = r"(https?://[^\s]+)"
    if not re.fullmatch(url_re, txt.strip()):  # يعني مش لينك فقط
        parts.append(f"النص الأصلي:\n{txt}\n")

    # 2) لو ريكورد صوت
    if ctype == "audio":
        audio_bytes = download_media(value)
        if audio_bytes:
            with tempfile.NamedTemporaryFile(delete=False, suffix=".mp3") as tmp:
                tmp.write(audio_bytes)
                path = tmp.name

            text = whisper_transcribe(path)
            os.remove(path)

            if text:
                parts.append(f"تفريغ الصوت:\n{text}\n")

    # 3) لو صورة
    if ctype == "image":
        img_bytes = download_media(value)
        if img_bytes:
            b64 = base64.b64encode(img_bytes).decode()
            vision = asyncio.run(vision_describe(b64))
            if vision:
                parts.append(f"تحليل الصورة:\n{vision}\n")

    # 4) لو فيديو → تجاهل كامل
    if ctype == "video":
        pass  # لا نضيف شيئًا

    final = "\n".join(parts).strip()
    if not final:
        final = "."

    add_to_queue(session, final)

    return jsonify({"ok": True})

# ============================================================
# RUN
# ============================================================
@app.route("/")
def home():
    return "Bot running — Sound & Image only → Text (A-format)."

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=PORT)
