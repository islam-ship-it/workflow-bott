import os
import time
import json
import requests
import threading
import asyncio
import logging
import base64
import tempfile
import re
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
        except Exception:
            logger.info(str(data))
    logger.info("="*70 + "\n")

# ===========================
# تحميل الإعدادات
# ===========================
load_dotenv()
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
ASSISTANT_ID_PREMIUM = os.getenv("ASSISTANT_ID_PREMIUM")  # احتياطي لو بتستخدمه في مكان تاني
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
queue_lock = threading.Lock()

BATCH_WAIT_TIME = 2.0           # وقت تجميع الرسائل
RETRY_DELAY_WHEN_BUSY = 3.0     # وقت إعادة المحاولة لو المساعد مشغول

# ===========================
# Utilities
# ===========================
def download_media_from_url(url, timeout=30):
    debug("🌐 Downloading Media", url)
    try:
        headers = {"User-Agent": "Mozilla/5.0"}
        r = requests.get(url, timeout=timeout, headers=headers)
        r.raise_for_status()
        return r.content
    except Exception as e:
        debug("❌ فشل تحميل الميديا", str(e))
        return None

def transcribe_audio_bytes(content_bytes, fmt="mp3"):
    debug("🎤 Converting Audio Bytes To Text", {"format": fmt})
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
        try:
            return response.choices[0].message.content
        except Exception:
            return getattr(response, "output_text", None)
    except Exception as e:
        debug("❌ خطأ رؤية الصورة", str(e))
        return None

def detect_content_type(text):
    """
    يحدد نوع المحتوى: video / audio / image / website / text
    """
    if not isinstance(text, str):
        return ("text", text)
    url_pattern = r"(https?://[^\s]+)"
    match = re.search(url_pattern, text)
    if not match:
        return ("text", text)
    url = match.group(1).strip('",')
    low = url.lower()
    # check common extensions
    if any(low.endswith(x) for x in [".mp4", ".mov", ".m4v", ".webm"]):
        return ("video", url)
    if any(low.endswith(x) for x in [".mp3", ".wav", ".m4a", ".aac"]):
        return ("audio", url)
    if any(low.endswith(x) for x in [".jpg", ".jpeg", ".png", ".webp", ".gif"]):
        return ("image", url)
    # fallback: treat as website if it's a URL
    return ("website", url)

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
        "openai_conversation_id": None,
        "custom_fields": contact_data.get("custom_fields", {}),
        "tags": [f"source:{main_platform.lower()}"],
        "status": "active",
        "conversation_summary": "",
        "first_contact_date": now_utc,
        "last_contact_date": now_utc,
        "assistant_busy": False  # حالة المساعد لهذا المستخدم
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

    # 2) بناء الـ Payload
    payload = {
        "prompt": {
            "id": "pmpt_691df223bd3881909e4e9c544a56523b006e1332a5ce0f11",
            "version": "4"
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

    # إضافة Conversation ID لو موجود
    if conversation_id:
        payload["conversation"] = conversation_id

    try:
        response = await asyncio.to_thread(
            client.responses.create,
            **payload
        )

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
        debug("❌ خطأ في Responses API", str(e))
        return "⚠️ حصل خطأ أثناء المعالجة."

# ===========================
# إرسال ManyChat
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

    # 1) إرسال عادي
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

    # 2) إعادة المحاولة بالتاجات
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
        except Exception:
            pass

    # 3) Legacy V1
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
    except Exception:
        pass

    return {"ok": False}

# ===========================
# Queue System
# ===========================
def schedule_assistant_response(platform, user_id):
    debug("⚙ Queue Run Started", {"platform": platform, "user": user_id})

    # 1) جلب جلسة المستخدم
    session = sessions_collection.find_one({"_id": str(user_id)})
    if not session:
        return

    # 2) التحقق من حالة المساعد في الـ DB
    if session.get("assistant_busy") is True:
        debug("⏳ Assistant Busy (DB State) – Retrying", {"user": user_id})
        threading.Timer(RETRY_DELAY_WHEN_BUSY, schedule_assistant_response, args=[platform, user_id]).start()
        return

    # نضبط الحالة على مشغول
    sessions_collection.update_one(
        {"_id": str(user_id)},
        {"$set": {"assistant_busy": True}}
    )

    # 3) جلب الرسائل المجمعة من الكيو
    with queue_lock:
        data = pending_messages[platform].pop(user_id, None)
        message_timers[platform].pop(user_id, None)

    if not data:
        sessions_collection.update_one(
            {"_id": str(user_id)},
            {"$set": {"assistant_busy": False}}
        )
        return

    merged = "\n".join(data["texts"])
    debug("📝 MERGED USER MESSAGES", merged)

    # 4) استدعاء OpenAI
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        reply = loop.run_until_complete(get_assistant_reply_async(session, merged))
    finally:
        loop.close()

    # 5) إرسال الرد للعميل
    send_manychat_reply(user_id, reply, session["platform"])

    # 6) إعادة ضبط حالة المساعد
    sessions_collection.update_one(
        {"_id": str(user_id)},
        {"$set": {"assistant_busy": False}}
    )

def add_to_queue(session, text):
    platform = session["platform"]
    uid = session["_id"]

    debug("📥 ADDING TO QUEUE", {
        "user": uid,
        "platform": platform,
        "incoming_text": text
    })

    with queue_lock:
        # لو أول رسالة في الباتش → نبعت Typing فوراً
        if uid not in pending_messages[platform]:
            threading.Thread(target=send_typing_action, args=(uid, platform)).start()

        if uid not in pending_messages[platform]:
            pending_messages[platform][uid] = {"texts": [], "session": session}

        pending_messages[platform][uid]["texts"].append(text)

        # إلغاء التايمر القديم لو موجود
        if uid in message_timers[platform]:
            try:
                message_timers[platform][uid].cancel()
            except Exception:
                pass

        # إنشاء تايمر جديد لتجميع الرسائل خلال 2 ثانية
        timer = threading.Timer(BATCH_WAIT_TIME, schedule_assistant_response, args=[platform, uid])
        message_timers[platform][uid] = timer
        timer.start()

        debug("⏳ QUEUE UPDATED", {
            "platform": platform,
            "user": uid,
            "note": "Typing signal sent immediately"
        })

# ===========================
# ManyChat Webhook (مع معالجة المحتوى المتقدم)
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

    # حماية إنستغرام: لو الجلسة مسجلة كـ Instagram و مفيش ig_id → نتجاهل
    if existing_session and existing_session["platform"] == "Instagram" and not contact.get("ig_id"):
        debug("⛔ IG BLOCK TRIGGERED", "No IG ID")
        return jsonify({"ignored": True}), 200

    session = get_or_create_session_from_contact(contact, platform_hint="ManyChat")

    txt = (
        contact.get("last_text_input")
        or contact.get("last_input_text")
        or contact.get("last_input")
        or contact.get("last_media_url")
        or contact.get("last_attachment_url")
    )

    debug("📥 TEXT EXTRACTED (raw)", txt)

    if not txt:
        debug("⚠ NO TEXT FOUND", contact)
        return jsonify({"ok": True}), 200

    # ======= Detect content type =======
    content_type, content_value = detect_content_type(txt)
    debug("🔎 Detected Content", {"type": content_type, "value": content_value})

    # Simple sales trigger
    sales_keywords = ["متابعين", "متابع", "سعر", "باقة", "عرض", "عايز", "زيادة", "followers", "followers"]
    is_sales = any(k in str(txt).lower() for k in sales_keywords)

    # Process by content type
    processed_text = str(txt)
    try:
        if content_type == "video":
            debug("🎬 VIDEO DETECTED", content_value)
            video_bytes = download_media_from_url(content_value)
            if video_bytes:
                with tempfile.NamedTemporaryFile(delete=False, suffix=".mp4") as tmp:
                    tmp.write(video_bytes)
                    video_path = tmp.name
                audio_path = video_path.rsplit(".", 1)[0] + ".m4a"
                # Convert with ffmpeg (ensure ffmpeg is available on the host)
                os.system(f"ffmpeg -y -i {video_path} -vn -acodec aac {audio_path} >/dev/null 2>&1")
                if os.path.exists(audio_path):
                    with open(audio_path, "rb") as f:
                        tr = client.audio.transcriptions.create(model="whisper-1", file=f)
                    if hasattr(tr, "text"):
                        processed_text = "تفريغ الكلام من الفيديو:\n" + tr.text
                    else:
                        processed_text = "تم استلام فيديو، لكن التعذر في تفريغ الصوت."
                else:
                    processed_text = "تم استلام فيديو، ولم يتم تحويله لصوت على الخادم."
                # cleanup
                try:
                    os.remove(video_path)
                except:
                    pass

        elif content_type == "audio":
            debug("🎵 AUDIO DETECTED", content_value)
            audio_bytes = download_media_from_url(content_value)
            if audio_bytes:
                tr_text = transcribe_audio_bytes(audio_bytes, fmt="mp3")
                if tr_text:
                    processed_text = "تفريغ الكلام من الرسالة الصوتية:\n" + tr_text
                else:
                    processed_text = "تم استلام ملف صوتي، لكن فشل التفريغ."

        elif content_type == "image":
            debug("🖼️ IMAGE DETECTED", content_value)
            img_bytes = download_media_from_url(content_value)
            if img_bytes:
                b64 = base64.b64encode(img_bytes).decode()
                try:
                    vision = asyncio.run(get_image_description_for_assistant(b64))
                    processed_text = "تحليل الصورة:\n" + str(vision)
                except Exception as e:
                    debug("❌ Vision processing failed", str(e))
                    processed_text = "تم استلام صورة، لكن فشل التحليل."

        elif content_type == "website":
            debug("🌐 WEBSITE DETECTED", content_value)
            try:
                r = requests.get(content_value, timeout=10, headers={"User-Agent":"Mozilla/5.0"})
                if r.status_code == 200:
                    from bs4 import BeautifulSoup
                    soup = BeautifulSoup(r.text, "html.parser")
                    text_content = soup.get_text(separator="\n")
                    processed_text = "محتوى صفحة الويب:\n" + text_content[:3000]
                else:
                    processed_text = "تم استلام رابط، لكن الصفحة لم ترجع محتوى صالح."
            except Exception as e:
                debug("❌ Website fetch failed", str(e))
                processed_text = "تم استلام رابط، لكن فشل جلب محتواه."

    except Exception as e:
        debug("❌ Error in content processing", str(e))
        processed_text = str(txt)

    # If it's clearly a sales request, prefix it so assistant knows to run sales flow
    if is_sales:
        processed_text = "طلب خدمة مبيعات:\n" + processed_text

    # Add to queue (processed_text will be passed to assistant)
    add_to_queue(session, processed_text)

    return jsonify({"ok": True}), 200

# ===========================
# Home Route
# ===========================
@app.route("/")
def home():
    return "Bot running with INSTANT SIGNAL & Advanced Content Handling"

# ===========================
# Run
# ===========================
if __name__ == "__main__":
    logger.info("🚀 السيرفر جاهز للعمل")
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", 5000)))
