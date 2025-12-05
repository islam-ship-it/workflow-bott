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
logger.info("▶️ بدء تشغيل التطبيق (نسخة Responses + Conversations)...")

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
# ASSISTANT_ID_PREMIUM لم نعد نحتاجه مع Responses/Prompts، موجود لو بتستخدمه في مكان تاني
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

# وقت التجميع (ثانيتين كافية جداً)
BATCH_WAIT_TIME = 2.0 
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
            model="gpt-4o",
            messages=[{
                "role": "user",
                "content": [
                    {"type": "text", "text": "اقرأ محتوى الصورة بدقة."},
                    {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{base64_image}"}}
                ]
            }],
            max_tokens=300
        )
        # ملاحظة: قد تتغير البنية حسب نسخة الـ SDK؛ هنا نحاول الوصول إلى المحتوى
        try:
            return response.choices[0].message.content
        except Exception:
            return getattr(response, "output_text", None)
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
        return None

    # اكتشاف المنصة
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
        # Note: نعِد تخزين conversation id بدلاً من thread id
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
# دالة إرسال إشارة "جاري الكتابة" (الحل السحري)
# ===========================
def send_typing_action(subscriber_id, platform):
    """
    بتبعت إشارة لـ ManyChat فوراً عشان "تفتح الشات" وتعرف العميل إننا موجودين.
    """
    debug("⚡ Sending Typing/Open Signal...", {"user": subscriber_id})
    
    url = "https://api.manychat.com/fb/sending/sendContent"
    headers = {
        "Authorization": f"Bearer {MANYCHAT_API_KEY}",
        "Content-Type": "application/json"
    }
    
    # محاولة إرسال أكشن (Typing) لإنعاش المحادثة
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
    except:
        pass

# ===========================
# OpenAI Assistant (Responses + Conversations)
# ===========================
async def get_assistant_reply_async(session, content):
    debug("🤖 Responses + Conversations Processing", {"user": session["_id"]})

    user_id = session["_id"]
    conversation_id = session.get("openai_conversation_id")

    # 1) إنشاء Conversation لو مش موجود
    if not conversation_id:
        try:
            conv = await asyncio.to_thread(
                client.conversations.create,
                items=[],
                metadata={"user_id": user_id}
            )
            conversation_id = conv.id
            sessions_collection.update_one(
                {"_id": user_id},
                {"$set": {"openai_conversation_id": conversation_id}}
            )
            debug("✅ تم إنشاء محادثة جديدة", {"conversation_id": conversation_id})
        except Exception as e:
            debug("❌ فشل إنشاء المحادثة", str(e))
            conversation_id = None

    # 2) بناء الـ Payload الصحيح
    payload = {
        "prompt": {
            "id": "pmpt_691df223bd3881909e4e9c544a56523b006e1332a5ce0f11",
            "version": "1"
        },
        "input": [
            {
                "role": "user",
                "content": content
            }
        ],
        "store": True,
        "reasoning": {"summary": "auto"}
    }

    # إضافة Conversation ID لو موجود (لعمل سياق للمحادثة)
    if conversation_id:
        payload["conversation"] = conversation_id

    try:
        # 3) تنفيذ الطلب بشكل Async
        response = await asyncio.to_thread(
            client.responses.create,
            **payload
        )

        # 4) استخراج النص النهائي
        reply = None

        # الطريقة الأساسية
        if hasattr(response, "output_text") and response.output_text:
            reply = response.output_text

        # fallback في حال تغيّر الهيكل
        if not reply and hasattr(response, "output"):
            for item in response.output:
                content_list = getattr(item, "content", None)
                if content_list:
                    for c in content_list:
                        if c.get("type") == "output_text":
                            reply = c.get("text", {}).get("value")
                            break

        if not reply:
            return "⚠️ حصل خطأ أثناء توليد الرد."

        return reply.strip()

    except Exception as e:
        debug("❌ خطأ في Responses API", str(e))
        return "⚠️ حصل خطأ أثناء المعالجة."

# ===========================
# إرسال ManyChat (المحارب)
# ===========================
def send_manychat_reply(subscriber_id, text_message, platform, fallback_tag="HUMAN_AGENT"):
    debug("📤 Sending ManyChat Reply", {
        "subscriber_id": subscriber_id,
        "message": text_message
    })

    channel = "instagram" if platform == "Instagram" else "facebook"
    url = "https://api.manychat.com/fb/sending/sendContent"
    
    headers = {
        "Authorization": f"Bearer {MANYCHAT_API_KEY}",
        "Content-Type": "application/json"
    }

    # 1. إرسال عادي
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

    # 2. الهجوم بالتاجات
    debug("⚠️ Retry with FORCE TAGS...", "")
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
                        {
                            "type": "text", 
                            "text": text_message,
                            "tag": tag 
                        }
                    ]
                }
            }
        }
        try:
            r2 = requests.post(url, headers=headers, data=json.dumps(payload_force), timeout=15)
            if r2.status_code == 200:
                debug(f"✅ Success with {tag}", r2.status_code)
                return {"ok": True}
        except:
            pass

    # 3. Legacy
    payload_v1 = {
        "subscriber_id": str(subscriber_id),
        "data": {
            "version": "v2",
            "content": {
                "messages": [{"type": "text", "text": text_message}]
            }
        },
        "message_tag": "HUMAN_AGENT"
    }
    try:
        requests.post(url, headers=headers, data=json.dumps(payload_v1), timeout=15)
    except:
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

    debug("📥 ADDING TO QUEUE", {
        "user": uid,
        "platform": platform,
        "incoming_text": text
    })

    with queue_lock:
        # ========================================================
        # اللمسة السحرية: لو دي أول رسالة، ابعت "إشارة" لماني شات فوراً
        # ========================================================
        if uid not in pending_messages[platform]:
            # بنشغلها في ثريد عشان متعطلش الكود
            threading.Thread(target=send_typing_action, args=(uid, platform)).start()

        if uid not in pending_messages[platform]:
            pending_messages[platform][uid] = {"texts": [], "session": session}

        pending_messages[platform][uid]["texts"].append(text)

        if uid in message_timers[platform]:
            try:
                message_timers[platform][uid].cancel()
            except:
                pass

        # التايمر للتجميع (2 ثانية)
        timer = threading.Timer(BATCH_WAIT_TIME, schedule_assistant_response, args=[platform, uid])
        message_timers[platform][uid] = timer
        timer.start()

        debug("⏳ QUEUE UPDATED", {
            "platform": platform,
            "user": uid,
            "note": "Typing signal sent immediately"
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
            return jsonify({"error": "unauthorized"}), 403

    data = request.get_json()
    debug("📦 RAW WEBHOOK DATA", data)

    contact = data.get("full_contact")
    if not contact:
        return jsonify({"error": "missing contact"}), 400

    user_id = str(contact.get("id"))
    existing_session = sessions_collection.find_one({"_id": user_id})

    # حماية إنستغرام
    if existing_session and existing_session["platform"] == "Instagram" and not contact.get("ig_id"):
        debug("⛔ IG BLOCK TRIGGERED", "No IG ID")
        return jsonify({"ignored": True}), 200

    session = get_or_create_session_from_contact(contact, platform_hint="ManyChat")

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
    return "Bot running with INSTANT SIGNAL & Responses/Conversations"

# ===========================
# Run
# ===========================
if __name__ == "__main__":
    logger.info("🚀 السيرفر جاهز للعمل")
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", 5000)))
