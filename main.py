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
# DEBUG
# ===========================
def debug(title, data=None):
    logger.info("\n" + "="*70)
    logger.info(f"🔍 {title}")
    if data is not None:
        try:
            logger.info(json.dumps(data, indent=2, ensure_ascii=False))
        except:
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


# ===========================
# قاعدة البيانات
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

BATCH_WAIT_TIME = 9.0
RETRY_DELAY_WHEN_BUSY = 3.0

# ===========================
# Utilities
# ===========================
def download_media_from_url(url, timeout=15):
    debug("🌐 Downloading Media", url)
    try:
        r = requests.get(url, timeout=timeout)
        r.raise_for_status()
        return r.content
    except Exception as e:
        debug("❌ فشل تحميل الميديا", str(e))
        return None

def transcribe_audio(content_bytes, fmt="mp4"):
    debug("🎤 Converting Audio To Text", {"format": fmt})
    try:
        with tempfile.NamedTemporaryFile(delete=False, suffix=f".{fmt}") as tmp:
            tmp.write(content_bytes)
            path = tmp.name
        with open(path, "rb") as f:
            tr = client.audio.transcriptions.create(model="whisper-1", file=f)
        os.remove(path)
        return tr.text
    except Exception as e:
        debug("❌ خطأ تحويل الصوت", str(e))
        return None

async def get_image_description_for_assistant(base64_image):
    debug("🖼️ وصف صورة", "")
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
        debug("❌ خطأ رؤية الصورة", str(e))
        return None

# ===========================
# جلسة المستخدم
# ===========================
def get_or_create_session_from_contact(contact_data, platform_hint=None):
    debug("🧾 FULL CONTACT DATA", contact_data)

    user_id = str(contact_data.get("id"))
    if not user_id:
        debug("❌ user_id مفقود", "")
        return None

    # اكتشاف المنصة من ManyChat
    if platform_hint is None or platform_hint == "ManyChat":
        if contact_data.get("ig_id") or contact_data.get("ig_last_interaction"):
            main_platform = "Instagram"
        else:
            main_platform = "Facebook"
    else:
        main_platform = platform_hint

    debug("📱 PLATFORM DETECTED", {
        "user_id": user_id,
        "platform": main_platform
    })

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
        debug("♻ SESSION UPDATED", session)
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
        "openai_thread_id": None,
        "custom_fields": contact_data.get("custom_fields", {}),
        "tags": [f"source:{main_platform.lower()}"],
        "status": "active",
        "conversation_summary": "",
        "first_contact_date": now_utc,
        "last_contact_date": now_utc
    }

    sessions_collection.insert_one(new_session)
    debug("🆕 SESSION CREATED", new_session)
    return new_session

# ===========================
# OpenAI Assistant
# ===========================
async def get_assistant_reply_async(session, content):
    debug("🤖 Assistant Processing", {"user": session["_id"]})

    user_id = session["_id"]
    thread_id = session.get("openai_thread_id")

    if not thread_id:
        thread = await asyncio.to_thread(client.beta.threads.create)
        thread_id = thread.id
        sessions_collection.update_one({"_id": user_id}, {"$set": {"openai_thread_id": thread_id}})
        debug("🧵 NEW THREAD CREATED", {"thread_id": thread_id})

    await asyncio.to_thread(
        client.beta.threads.messages.create,
        thread_id=thread_id,
        role="user",
        content=content
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
        debug("❌ RUN FAILED", run.status)
        return "⚠️ حصل خطأ."

    msgs = await asyncio.to_thread(
        client.beta.threads.messages.list,
        thread_id=thread_id,
        limit=1
    )
    reply = msgs.data[0].content[0].text.value.strip()

    debug("💬 Assistant Reply Ready", reply)
    return reply

# ===========================
# إرسال ManyChat (مع إصلاح مشكلة 24 ساعة بالـ tag fallback)
# ===========================
def send_manychat_reply(subscriber_id, text_message, platform, fallback_tag="post_sale"):
    """
    يحاول أولًا إرسال رسالة عادية. لو ManyChat رجع خطأ 3011 (خارج نافذة الـ 24 ساعة)
    يعيد المحاولة مع إضافة الحقل 'tag' داخل كل رسالة (مثال: "post_sale").
    """
    debug("📤 Sending ManyChat Reply", {
        "subscriber_id": subscriber_id,
        "message": text_message,
        "platform": platform
    })

    channel = "instagram" if platform == "Instagram" else "facebook"
    url = "https://api.manychat.com/fb/sending/sendContent"

    # payload بدون tag أولًا (يحافظ على السلوك الحالي لو الرسائل ضمن الـ24 ساعة)
    base_payload = {
        "subscriber_id": str(subscriber_id),
        "channel": channel,
        "data": {
            "version": "v2",
            "content": {
                "messages": [{"type": "text", "text": text_message}]
            }
        }
    }

    headers = {
        "Authorization": f"Bearer {MANYCHAT_API_KEY}",
        "Content-Type": "application/json"
    }

    try:
        r = requests.post(url, headers=headers, data=json.dumps(base_payload), timeout=15)
    except Exception as e:
        debug("❌ Network error when sending to ManyChat", str(e))
        return {"ok": False, "error": str(e)}

    debug("📥 MANYCHAT RESPONSE", {"status": r.status_code, "body": r.text})

    # If success -> return
    if r.status_code == 200:
        return {"ok": True, "status": r.status_code, "body": r.text}

    # If ManyChat returned 400 with code 3011 -> retry with message tag (post_sale)
    try:
        body_json = r.json()
    except Exception:
        body_json = {"raw": r.text}

    if r.status_code == 400 and (str(body_json).find("3011") != -1 or "24 ساعة" in r.text or "last interaction" in r.text):
        debug("⚠️ ManyChat 24h window error detected — retrying with tag", {"original_response": body_json})

        tagged_payload = {
            "subscriber_id": str(subscriber_id),
            "channel": channel,
            "data": {
                "version": "v2",
                "content": {
                    # Important: add "tag" field inside each message as manychat expects in some flows
                    "messages": [{"type": "text", "text": text_message, "tag": fallback_tag}]
                }
            }
        }

        try:
            r2 = requests.post(url, headers=headers, data=json.dumps(tagged_payload), timeout=15)
            debug("📥 MANYCHAT RETRY RESPONSE", {"status": r2.status_code, "body": r2.text})
            return {"ok": r2.status_code == 200, "status": r2.status_code, "body": r2.text}
        except Exception as e:
            debug("❌ Network error on retry with tag", str(e))
            return {"ok": False, "error": str(e)}

    # For other errors, return raw response
    return {"ok": False, "status": r.status_code, "body": r.text}

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

    debug("📥 ADDING TO QUEUE", {
        "user": uid,
        "platform": platform,
        "incoming_text": text
    })

    with queue_lock:
        if uid not in pending_messages[platform]:
            pending_messages[platform][uid] = {"texts": [], "session": session}

        pending_messages[platform][uid]["texts"].append(text)

        if uid in message_timers[platform]:
            try:
                message_timers[platform][uid].cancel()
            except:
                pass

        timer = threading.Timer(BATCH_WAIT_TIME, schedule_assistant_response, args=[platform, uid])
        message_timers[platform][uid] = timer
        timer.start()

        debug("⏳ QUEUE UPDATED", {
            "platform": platform,
            "user": uid,
            "pending_texts": pending_messages[platform][uid]["texts"]
        })

# ===========================
# ManyChat Webhook
# ===========================
@app.route("/manychat_webhook", methods=["POST"])
def mc_webhook():

    debug("📩 Webhook Received", "")

    if MANYCHAT_SECRET_KEY:
        auth = request.headers.get("Authorization")
        if auth != f"Bearer {MANYCHAT_SECRET_KEY}":
            debug("❌ Unauthorized Webhook", auth)
            return jsonify({"error": "unauthorized"}), 403

    data = request.get_json()
    debug("📦 RAW WEBHOOK DATA", data)

    contact = data.get("full_contact")
    if not contact:
        debug("❌ Missing Contact", "")
        return jsonify({"error": "missing contact"}), 400

    user_id = str(contact.get("id"))
    existing_session = sessions_collection.find_one({"_id": user_id})

    # ===========================
    # IG DEBUG BLOCK
    # ===========================
    debug("📌 IG DEBUG CHECKPOINT", {
        "user_id": user_id,
        "ig_id": contact.get("ig_id"),
        "ig_last_interaction": contact.get("ig_last_interaction"),
        "session_platform": existing_session["platform"] if existing_session else None,
        "detected_platform": "Instagram" if (contact.get("ig_id") or contact.get("ig_last_interaction")) else "Facebook",
        "last_text_input": contact.get("last_text_input"),
        "last_input_text": contact.get("last_input_text"),
        "last_input": contact.get("last_input"),
    })

    # ===========================
    # حماية إنستغرام من FB Webhook
    # ===========================
    if existing_session and existing_session["platform"] == "Instagram" and not contact.get("ig_id"):
        debug("⛔ IG BLOCK TRIGGERED", {
            "reason": "Webhook جاء بدون ig_id",
            "existing_session": existing_session,
            "contact": contact
        })
        return jsonify({"ignored": True}), 200

    # ===========================
    # جلسة جديدة / تحديث
    # ===========================
    session = get_or_create_session_from_contact(contact, platform_hint="ManyChat")

    # ===========================
    # استخراج النص
    # ===========================
    txt = (
        contact.get("last_text_input")
        or contact.get("last_input_text")
        or contact.get("last_input")
    )

    debug("📥 TEXT EXTRACTED", txt)

    if txt:
        add_to_queue(session, txt)
    else:
        debug("⚠ NO TEXT FOUND", contact)

    return jsonify({"ok": True}), 200

# ===========================
# Home Route
# ===========================
@app.route("/")
def home():
    return "Bot running with FULL IG DEBUG – FB & IG isolated"

# ===========================
# Run
# ===========================
if __name__ == "__main__":
    logger.info("🚀 السيرفر جاهز للعمل")
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", 5000)))
