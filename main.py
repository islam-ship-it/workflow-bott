# main.py — جاهز للنشر
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
from datetime import datetime, timezone
from dotenv import load_dotenv

# Optional OpenAI / Mongo imports (import only when keys present)
try:
    from openai import OpenAI
except Exception:
    OpenAI = None

try:
    from pymongo import MongoClient
except Exception:
    MongoClient = None

# ===========================
# إعداد اللوجات
# ===========================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)
logger.info("▶️ بدء تشغيل التطبيق — Prepared for deployment")

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
MONGO_URI = os.getenv("MONGO_URI")
MANYCHAT_API_KEY = os.getenv("MANYCHAT_API_KEY")
MANYCHAT_SECRET_KEY = os.getenv("MANYCHAT_SECRET_KEY")

# ===========================
# تهيئة قواعد البيانات (محمي من خطأ truth-value)
# ===========================
client_db = None
db = None
sessions_collection = None

if MongoClient is not None and MONGO_URI:
    try:
        client_db = MongoClient(MONGO_URI)
        # لا تستخدم شرط truthy على كائنات Mongo — استخدم مقارنة None
        db = client_db["multi_platform_bot"] if client_db is not None else None
        sessions_collection = db["sessions"] if db is not None else None
        logger.info("✅ MongoDB client initialized")
    except Exception as e:
        logger.error(f"❌ Failed to initialize MongoDB client: {e}")
        client_db = None
        db = None
        sessions_collection = None
else:
    logger.info("ℹ️ MongoClient not available or MONGO_URI not set — running without DB")

# ===========================
# Flask + OpenAI client (محمي)
# ===========================
app = Flask(__name__)
client = None
if OpenAI is not None and OPENAI_API_KEY:
    try:
        client = OpenAI(api_key=OPENAI_API_KEY)
        logger.info("✅ OpenAI client initialized")
    except Exception as e:
        logger.error(f"❌ Failed to initialize OpenAI client: {e}")
        client = None
else:
    logger.info("ℹ️ OpenAI client not initialized (OPENAI_API_KEY missing or SDK unavailable)")

# ===========================
# إعداد مخازن الانتظار ووقت التجميع
# ===========================
pending_messages = {"Facebook": {}, "Instagram": {}}
message_timers = {"Facebook": {}, "Instagram": {}}
run_locks = {"Facebook": {}, "Instagram": {}}
queue_lock = threading.Lock()
BATCH_WAIT_TIME = float(os.getenv("BATCH_WAIT_TIME", "2.2"))
RETRY_DELAY_WHEN_BUSY = float(os.getenv("RETRY_DELAY_WHEN_BUSY", "2.5"))

# ===========================
# Utilities: تحميل ميديا، تحويل صوت، رؤية صورة
# ===========================
def download_media_from_url(url, timeout=20):
    debug("🌐 Downloading Media", url)
    try:
        r = requests.get(url, timeout=timeout, stream=True)
        r.raise_for_status()
        content = r.content
        ctype = r.headers.get("Content-Type", "")
        return content, ctype
    except Exception as e:
        debug("❌ فشل تحميل الميديا", str(e))
        return None, None

def transcribe_audio(content_bytes, fmt="mp4"):
    debug("🎤 Transcribing audio", {"format": fmt})
    if client is None:
        debug("ℹ️ OpenAI client not available — skipping transcription")
        return None
    try:
        with tempfile.NamedTemporaryFile(delete=False, suffix=f".{fmt}") as tmp:
            tmp.write(content_bytes)
            path = tmp.name
        with open(path, "rb") as f:
            tr = client.audio.transcriptions.create(model="whisper-1", file=f)
        try:
            os.remove(path)
        except:
            pass
        # قد تأتي الاستجابة ككائن أو dict — فحص مرن
        if hasattr(tr, "text"):
            return tr.text
        if isinstance(tr, dict):
            return tr.get("text")
        return None
    except Exception as e:
        debug("❌ خطأ تحويل الصوت", str(e))
        return None

async def get_image_description_for_assistant(base64_image):
    debug("🖼️ Describing image (vision->text)", "")
    if client is None:
        debug("ℹ️ OpenAI client not available — skipping image description")
        return None
    try:
        response = await asyncio.to_thread(
            client.chat.completions.create,
            model="gpt-4o",
            messages=[{
                "role": "user",
                "content": [
                    {"type": "text", "text": "اقرأ محتوى الصورة بدقة وحولها إلى نص موجز يصف العناصر والنصوص الظاهرة."},
                    {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{base64_image}"}}
                ]
            }],
            max_tokens=400
        )
        # محاولة استخراج بصيغ مختلفة
        try:
            return response.choices[0].message.content
        except Exception:
            if hasattr(response, "output_text"):
                return response.output_text
            if isinstance(response, dict):
                # fallbacks
                o = response.get("choices") or response.get("output")
                return str(o)
        return None
    except Exception as e:
        debug("❌ خطأ رؤية الصورة", str(e))
        return None

# ===========================
# إدارة الجلسة
# ===========================
def get_or_create_session_from_contact(contact_data, platform_hint=None):
    debug("🧾 Contact data", contact_data)
    user_id = contact_data.get("id")
    if user_id is None:
        debug("❌ No user id in contact")
        return {"_id": "unknown", "platform": platform_hint or "Facebook", "profile": {}}

    user_id = str(user_id)
    # اكتشاف المنصة
    if platform_hint is None or platform_hint == "ManyChat":
        if contact_data.get("ig_id") or contact_data.get("ig_last_interaction"):
            main_platform = "Instagram"
        else:
            main_platform = "Facebook"
    else:
        main_platform = platform_hint

    now_utc = datetime.now(timezone.utc)
    session = None
    if sessions_collection is not None:
        try:
            session = sessions_collection.find_one({"_id": user_id})
        except Exception as e:
            debug("❌ Mongo find_one error", str(e))
            session = None

    if session:
        try:
            if sessions_collection is not None:
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
        except Exception as e:
            debug("❌ Failed to update session", str(e))
        # read back latest if possible
        if sessions_collection is not None:
            try:
                return sessions_collection.find_one({"_id": user_id})
            except:
                return session
        return session

    # create new
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
    if sessions_collection is not None:
        try:
            sessions_collection.insert_one(new_session)
        except Exception as e:
            debug("❌ Failed to insert session", str(e))
    return new_session

# ===========================
# إرسال typing indicator
# ===========================
def send_typing_action(subscriber_id, platform):
    if not MANYCHAT_API_KEY:
        return
    url = "https://api.manychat.com/fb/sending/sendContent"
    headers = {"Authorization": f"Bearer {MANYCHAT_API_KEY}", "Content-Type": "application/json"}
    payload = {"subscriber_id": str(subscriber_id), "data": {"version": "v2", "content": {"type": "typing_on"}}}
    try:
        requests.post(url, headers=headers, data=json.dumps(payload), timeout=2)
    except Exception:
        pass

# ===========================
# Responses / Conversations (OpenAI)
# ===========================
async def get_assistant_reply_async(session, content):
    debug("🤖 Generating assistant reply", {"user": session.get("_id"), "len": len(content or "")})
    if client is None:
        debug("ℹ️ OpenAI client not configured — returning fallback text")
        return "⚠️ المساعد غير متاح حالياً."

    conv_id = session.get("openai_conversation_id")
    # إنشاء محادثة إذا لم تكن موجودة
    if not conv_id:
        try:
            conv = await asyncio.to_thread(client.conversations.create, items=[], metadata={"user_id": session["_id"]})
            conv_id = getattr(conv, "id", None) or (conv.get("id") if isinstance(conv, dict) else None)
            if conv_id and sessions_collection is not None:
                try:
                    sessions_collection.update_one({"_id": session["_id"]}, {"$set": {"openai_conversation_id": conv_id}})
                except Exception:
                    pass
        except Exception as e:
            debug("❌ Failed creating conversation", str(e))
            conv_id = None

    payload = {
        "prompt": {"id": "pmpt_691df223bd3881909e4e9c544a56523b006e1332a5ce0f11", "version": "4"},
        "input": [{"role": "user", "content": content}],
        "store": True
    }
    if conv_id:
        payload["conversation"] = conv_id

    try:
        r = await asyncio.to_thread(client.responses.create, **payload)
    except Exception as e:
        debug("❌ OpenAI Responses API error", str(e))
        return "⚠️ خطأ أثناء توليد الرد."

    # استخراج النص من الاستجابة بصورة مرنة
    reply = None
    try:
        if hasattr(r, "output_text") and r.output_text:
            reply = r.output_text
        elif hasattr(r, "output") and r.output:
            for item in r.output:
                content_list = getattr(item, "content", None) or item.get("content") if isinstance(item, dict) else None
                if content_list:
                    for c in content_list:
                        if isinstance(c, dict) and c.get("type") == "output_text":
                            t = c.get("text", {})
                            reply = t.get("value") if isinstance(t, dict) else None
                            if not reply:
                                reply = c.get("text")
                            if reply:
                                break
                    if reply:
                        break
    except Exception as e:
        debug("❌ Error extracting reply", str(e))

    if not reply:
        return "⚠️ حصل خطأ أثناء توليد الرد."
    return reply.strip()

# ===========================
# إرسال ManyChat — نسخة مُحسّنة وآمنة (تجارب بدون تاج ثم مع تاج)
# ===========================
def send_manychat_reply(subscriber_id, text_message, platform):
    debug("📤 Sending ManyChat reply", {"user": subscriber_id, "platform": platform})
    if not MANYCHAT_API_KEY:
        debug("❌ MANYCHAT_API_KEY not set")
        return {"ok": False, "error": "MANYCHAT_API_KEY missing"}

    url = "https://api.manychat.com/fb/sending/sendContent"
    headers = {"Authorization": f"Bearer {MANYCHAT_API_KEY}", "Content-Type": "application/json"}
    channel = "instagram" if platform == "Instagram" else "facebook"

    # Attempt 1: send clean (no tag)
    payload_clean = {
        "subscriber_id": str(subscriber_id),
        "channel": channel,
        "data": {"version": "v2", "content": {"messages": [{"type": "text", "text": text_message}]}}
    }
    try:
        r = requests.post(url, headers=headers, data=json.dumps(payload_clean), timeout=10)
        debug("↩️ response (clean)", {"status": getattr(r, "status_code", None), "body": getattr(r, "text", None)})
        if getattr(r, "status_code", None) == 200:
            return {"ok": True, "status": r.status_code, "body": r.text}
    except Exception as e:
        debug("❌ Network error (clean send)", str(e))

    # Attempt 2: try several permitted tags — one by one
    tags_to_try = ["HUMAN_AGENT", "ACCOUNT_UPDATE", "CONFIRMED_EVENT_UPDATE"]
    for tag in tags_to_try:
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
            debug("↩️ response (with tag)", {"tag": tag, "status": getattr(r2, "status_code", None), "body": getattr(r2, "text", None)})
            if getattr(r2, "status_code", None) == 200:
                return {"ok": True, "status": r2.status_code, "body": r2.text}
        except Exception as e:
            debug("❌ Network error (tag send)", {"tag": tag, "err": str(e)})

    return {"ok": False, "error": "all attempts failed"}

# ===========================
# كشف الميديا داخل النص وتحويلها إلى نص
# ===========================
def detect_and_extract_text_from_input(raw_text):
    debug("🔎 processing input", raw_text)
    if not isinstance(raw_text, str):
        return ""

    import re
    url_pattern = re.compile(r'(https?://\S+)')
    urls = url_pattern.findall(raw_text)
    extracted_parts = []

    if not urls:
        return raw_text.strip()

    for u in urls:
        content, ctype = download_media_from_url(u)
        if content is None:
            extracted_parts.append(f"تم استلام رابط لكن فشل جلب المحتوى: {u}")
            continue

        if ctype and "image" in ctype:
            try:
                b64 = base64.b64encode(content).decode("utf-8")
                loop = asyncio.new_event_loop()
                asyncio.set_event_loop(loop)
                desc = loop.run_until_complete(get_image_description_for_assistant(b64))
                try:
                    loop.close()
                except:
                    pass
                extracted_parts.append("وصف الصورة: " + (desc or "فشل استخراج وصف"))
            except Exception as e:
                debug("❌ image handling error", str(e))
                extracted_parts.append("تم استلام صورة ولكن فشل استخراج الوصف")
            continue

        if ctype and ("audio" in ctype or "video" in ctype):
            fmt = "mp4" if "mp4" in (ctype or "").lower() or u.lower().endswith(".mp4") else "mp3"
            try:
                text = transcribe_audio(content, fmt=fmt)
                extracted_parts.append("نص من الرسالة الصوتية: " + (text or "فشل تحويل الصوت لنص"))
            except Exception as e:
                debug("❌ audio handling error", str(e))
                extracted_parts.append("تم استلام تسجيل صوتي لكن فشل تحويله لنص")
            continue

        extracted_parts.append(f"رابط تم استلامه: {u}")

    remaining = url_pattern.sub('', raw_text).strip()
    if remaining:
        extracted_parts.insert(0, remaining)

    return "\n".join(extracted_parts)

# ===========================
# Queue System
# ===========================
def schedule_assistant_response(platform, user_id):
    debug("⚙ Queue run", {"platform": platform, "user": user_id})
    lock = run_locks[platform].setdefault(user_id, threading.Lock())

    if not lock.acquire(blocking=False):
        debug("⏳ assistant busy — will retry", {"user": user_id})
        threading.Timer(RETRY_DELAY_WHEN_BUSY, schedule_assistant_response, args=[platform, user_id]).start()
        return

    try:
        with queue_lock:
            data = pending_messages[platform].pop(user_id, None)
            if user_id in message_timers[platform]:
                try:
                    message_timers[platform][user_id].cancel()
                except:
                    pass
        if not data:
            return

        session = data["session"]
        merged = "\n".join(data["texts"])
        debug("📝 merged texts", merged[:200] + ("..." if len(merged) > 200 else ""))

        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            reply = loop.run_until_complete(get_assistant_reply_async(session, merged))
        finally:
            try:
                loop.close()
            except:
                pass

        send_manychat_reply(user_id, reply, session.get("platform", "Facebook"))
    finally:
        try:
            lock.release()
        except:
            pass

def add_to_queue(session, text):
    platform = session.get("platform", "Facebook")
    uid = session.get("_id", "unknown")
    debug("📥 Adding to queue", {"user": uid, "platform": platform})

    with queue_lock:
        first_time = uid not in pending_messages[platform]
        if first_time:
            pending_messages[platform][uid] = {"texts": [], "session": session}
            # send typing indicator asynchronously
            threading.Thread(target=send_typing_action, args=(uid, platform), daemon=True).start()

        processed = detect_and_extract_text_from_input(text)
        pending_messages[platform][uid]["texts"].append(processed)

        if uid in message_timers[platform]:
            try:
                message_timers[platform][uid].cancel()
            except:
                pass

        timer = threading.Timer(BATCH_WAIT_TIME, schedule_assistant_response, args=[platform, uid])
        message_timers[platform][uid] = timer
        timer.start()

# ===========================
# Webhook endpoint
# ===========================
@app.route("/manychat_webhook", methods=["POST"])
def mc_webhook():
    debug("📩 Webhook received", "")
    # verify secret if provided
    if MANYCHAT_SECRET_KEY:
        auth = request.headers.get("Authorization")
        if auth != f"Bearer {MANYCHAT_SECRET_KEY}":
            debug("❌ Unauthorized webhook call", auth)
            return jsonify({"error": "unauthorized"}), 403

    data = request.get_json(silent=True)
    if not data:
        debug("❌ Empty webhook payload")
        return jsonify({"error": "empty payload"}), 400

    debug("📦 raw webhook data", data)
    contact = data.get("full_contact")
    if not contact:
        debug("❌ missing contact in payload")
        return jsonify({"error": "missing contact"}), 400

    user_id = str(contact.get("id", "unknown"))
    existing_session = None
    if sessions_collection is not None:
        try:
            existing_session = sessions_collection.find_one({"_id": user_id})
        except Exception as e:
            debug("❌ session lookup error", str(e))

    # protect IG forwarded webhooks
    if existing_session and existing_session.get("platform") == "Instagram" and not contact.get("ig_id"):
        debug("⛔ IG block triggered", "incoming webhook missing ig_id")
        return jsonify({"ignored": True}), 200

    session = get_or_create_session_from_contact(contact, platform_hint="ManyChat")

    txt = (
        contact.get("last_text_input")
        or contact.get("last_input_text")
        or contact.get("last_input")
    )

    debug("📥 extracted text", txt)
    if txt:
        add_to_queue(session, txt)
    else:
        debug("⚠ no text found in contact", contact)

    return jsonify({"ok": True}), 200

# ===========================
# Home
# ===========================
@app.route("/")
def home():
    return "Bot running — Ready for production"

# ===========================
# Run
# ===========================
if __name__ == "__main__":
    logger.info("🚀 starting Flask server")
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", 5000)))

