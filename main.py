# main.py
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
# LOGGING (بالعربي عشان تحب)
# ============================================================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger("bot")

def debug(title, data=None):
    logger.info("\n" + "="*70)
    logger.info(f"🔍 {title}")
    if data is not None:
        try:
            logger.info(json.dumps(data, indent=2, ensure_ascii=False))
        except Exception:
            logger.info(str(data))
    logger.info("="*70 + "\n")

# ============================================================
# ENV
# ============================================================
load_dotenv()
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
MONGO_URI = os.getenv("MONGO_URI")
MANYCHAT_API_KEY = os.getenv("MANYCHAT_API_KEY")
MANYCHAT_SECRET_KEY = os.getenv("MANYCHAT_SECRET_KEY")
PORT = int(os.getenv("PORT", 5000))

# OpenAI client (Responses + Conversations + Whisper)
client = OpenAI(api_key=OPENAI_API_KEY)

# ============================================================
# MongoDB init (قد يفشل على بيئة dev — نتعامل معاه بحذر)
# ============================================================
sessions_collection = None
try:
    if MONGO_URI:
        mongo_client = MongoClient(MONGO_URI, serverSelectionTimeoutMS=5000)
        db = mongo_client.get_database("multi_platform_bot")
        sessions_collection = db.get_collection("sessions")
        logger.info("✅ متصل بقاعدة البيانات (MongoDB)")
    else:
        logger.warning("⚠ MONGO_URI غير موجود — تشغيل دون DB")
except Exception as e:
    logger.warning(f"⚠ فشل الاتصال بالـ MongoDB: {e}")
    sessions_collection = None

# ============================================================
# Flask app
# ============================================================
app = Flask(__name__)

# ============================================================
# Queue system (دمج رسائل قصيرة قبل الارسال للمساعد)
# ============================================================
pending_messages = {"Facebook": {}, "Instagram": {}}
message_timers = {"Facebook": {}, "Instagram": {}}
queue_lock = threading.Lock()
BATCH_WAIT_TIME = 2.0  # ثانيتين للتجميع

# ============================================================
# Utilities: تنزيل ميديا، تحويل Whisper، وصف صورة
# ============================================================
def download_media(url):
    """
    ينزل الملف من URL ويرجّع bytes أو None
    """
    try:
        debug("📥 Downloading Media", url)
        r = requests.get(url, timeout=20, headers={"User-Agent": "Mozilla/5.0"})
        r.raise_for_status()
        return r.content
    except Exception as e:
        debug("❌ media download failed", str(e))
        return None

def whisper_transcribe(path):
    """
    يستخدم OpenAI Whisper (client.audio.transcriptions.create) لإخراج نص من ملف صوت
    """
    try:
        debug("🎤 Whisper Transcribe", path)
        with open(path, "rb") as f:
            tr = client.audio.transcriptions.create(model="whisper-1", file=f)
        # tr.text أو getattr حسب الإصدار
        text = getattr(tr, "text", None) or getattr(tr, "transcript", None) or ""
        return text
    except Exception as e:
        debug("❌ whisper failed", str(e))
        return ""

async def vision_describe(b64_img):
    """
    يستدعي GPT-4o Chat Completion مع صورة (data URL) لعمل description/analysis
    """
    try:
        def call():
            # ملاحظة: قد تحتاج تعديل حسب نسخة SDK لكن هذا قالب شغال عادة
            r = client.chat.completions.create(
                model="gpt-4o",
                messages=[{
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "حلّل الصورة في نقاط واضحة وبالعربية، ركّز على التفاصيل المهمة."},
                        {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64_img}"}}
                    ]
                }],
                max_tokens=400
            )
            # محاولة استخراج المحتوى بطريقة مرنة
            try:
                return r.choices[0].message.content
            except Exception:
                return getattr(r, "output_text", "") or ""
        return await asyncio.to_thread(call)
    except Exception as e:
        debug("❌ vision failed", str(e))
        return ""

# ============================================================
# Content type detection
# ============================================================
def detect_type(text):
    """
    يحدد نوع المدخل:
    - audio: mp3, m4a, wav, audioclip-*.mp4
    - image: jpg, jpeg, png, webp
    - video: mp4/mov/mkv (وسيتم تجاهله لو مش audioclip)
    - text: غير URL أو غير المنصوص أعلاه
    """
    if not text:
        return ("text", text)

    url_re = r"(https?://[^\s]+)"
    m = re.search(url_re, text)
    if not m:
        return ("text", text)

    url = m.group(1).strip()
    low = url.lower()

    # صوت: includes facebook audioclip mp4
    if "audioclip" in low or low.endswith(".mp3") or low.endswith(".m4a") or low.endswith(".wav"):
        return ("audio", url)

    # صورة
    if any(low.endswith(ext) or (ext in low) for ext in [".jpg", ".jpeg", ".png", ".webp"]):
        return ("image", url)

    # فيديو عام
    if low.endswith(".mp4") or low.endswith(".mov") or low.endswith(".mkv"):
        # لو مش audioclip -> video ignore
        if "audioclip" not in low:
            return ("video", url)
        return ("audio", url)

    return ("text", text)

# ============================================================
# Session management (معالجة الحالة بدون DB)
# ============================================================
def get_or_create_session(contact):
    user_id = str(contact.get("id"))
    if not user_id:
        # fallback minimal session
        return {"_id": "unknown", "platform": "Facebook"}

    # إذا ما فيش DB ربط، نرجع session مصغرة
    if sessions_collection is None:
        return {"_id": user_id, "platform": "Facebook", "openai_conversation_id": None}

    # حاول تجيب الجلسة من DB
    try:
        s = sessions_collection.find_one({"_id": user_id})
    except Exception as e:
        debug("❌ mongo find_one failed", str(e))
        s = None

    if s:
        # حدث بعض الحقول الأساسية
        try:
            sessions_collection.update_one(
                {"_id": user_id},
                {"$set": {
                    "platform": "Facebook",
                    "profile.name": contact.get("name"),
                    "profile.profile_pic": contact.get("profile_pic"),
                    "last_contact_date": datetime.now(timezone.utc),
                    "status": "active"
                }}
            )
        except Exception:
            pass
        return s

    # إنشاء جلسة جديدة في DB
    new_s = {
        "_id": user_id,
        "platform": "Facebook",
        "profile": {
            "name": contact.get("name"),
            "first_name": contact.get("first_name"),
            "last_name": contact.get("last_name"),
            "profile_pic": contact.get("profile_pic"),
        },
        "openai_conversation_id": None,
        "custom_fields": contact.get("custom_fields", {}),
        "tags": ["source:facebook"],
        "status": "active",
        "conversation_summary": "",
        "first_contact_date": datetime.now(timezone.utc),
        "last_contact_date": datetime.now(timezone.utc)
    }

    try:
        sessions_collection.insert_one(new_s)
    except Exception as e:
        debug("❌ mongo insert failed", str(e))

    return new_s

# ============================================================
# Typing / Open signal to ManyChat
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
    except Exception:
        pass

# ============================================================
# OpenAI Responses (Responses + Conversations)
# ============================================================
async def ask_ai(session, text):
    """
    يرسل النص للمساعد باستخدام Responses API مع المحافظة على Conversation ID لو موجود.
    """
    conv = session.get("openai_conversation_id") if session else None

    # create conversation if needed (only if we have DB)
    if not conv and sessions_collection is not None:
        try:
            c = client.conversations.create(items=[], metadata={"user_id": session["_id"]})
            conv = c.id
            # update db
            try:
                sessions_collection.update_one({"_id": session["_id"]}, {"$set": {"openai_conversation_id": conv}})
            except Exception:
                pass
        except Exception as e:
            debug("❌ create conversation failed", str(e))
            conv = None

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

    try:
        r = await asyncio.to_thread(client.responses.create, **payload)
    except Exception as e:
        debug("❌ Responses API call failed", str(e))
        return "⚠️ حصل خطأ أثناء الاتصال بالمساعد."

    # استخراج النص النهائي
    try:
        if hasattr(r, "output_text") and r.output_text:
            return r.output_text.strip()
        # fallback
        if hasattr(r, "output"):
            for item in r.output:
                content_list = getattr(item, "content", None)
                if content_list:
                    for c in content_list:
                        if c.get("type") == "output_text":
                            return c.get("text", {}).get("value", "").strip()
    except Exception as e:
        debug("❌ error parsing response", str(e))

    return "⚠️ حصل خطأ أثناء توليد الرد."

# ============================================================
# ManyChat reply
# ============================================================
def send_manychat_reply(user_id, text_message):
    try:
        url = "https://api.manychat.com/fb/sending/sendContent"
        headers = {"Authorization": f"Bearer {MANYCHAT_API_KEY}", "Content-Type": "application/json"}
        payload = {
            "subscriber_id": str(user_id),
            "channel": "facebook",
            "data": {
                "version": "v2",
                "content": {"messages": [{"type": "text", "text": text_message}]}
            }
        }
        r = requests.post(url, headers=headers, json=payload, timeout=15)
        debug("📤 Sent ManyChat Reply", {"status": getattr(r, "status_code", None)})
    except Exception as e:
        debug("❌ failed to send manychat reply", str(e))

# ============================================================
# Queue handling
# ============================================================
def schedule(platform, user_id):
    debug("⚙ Queue Run Started", {"platform": platform, "user": user_id})

    with queue_lock:
        data = pending_messages[platform].pop(user_id, None)
        try:
            message_timers[platform].pop(user_id, None)
        except Exception:
            pass

    if not data:
        return

    # نستخدم الجلسة المخزنة في data إن وُجدت بدل إعادة جلب من DB
    session = data.get("session") or (sessions_collection.find_one({"_id": user_id}) if sessions_collection is not None else {"_id": user_id, "platform": platform})
    merged = "\n".join(data.get("texts", []))
    debug("📝 MERGED USER MESSAGES", merged)

    # استدعاء المساعد بشكل متزامن داخل event loop
    loop = asyncio.new_event_loop()
    try:
        asyncio.set_event_loop(loop)
        reply = loop.run_until_complete(ask_ai(session, merged))
    finally:
        try:
            loop.close()
        except:
            pass

    send_manychat_reply(user_id, reply)

def add_to_queue(session, text):
    platform = session.get("platform", "Facebook")
    uid = session["_id"]

    debug("📥 ADDING TO QUEUE", {"user": uid, "platform": platform, "incoming_text": text})

    with queue_lock:
        if uid not in pending_messages[platform]:
            # نرسل typing فورًا
            threading.Thread(target=send_typing, args=(uid,), daemon=True).start()
            pending_messages[platform][uid] = {"texts": [], "session": session}

        pending_messages[platform][uid]["texts"].append(text)

        # cancel old timer إن وُجد
        if uid in message_timers[platform]:
            try:
                message_timers[platform][uid].cancel()
            except Exception:
                pass

        t = threading.Timer(BATCH_WAIT_TIME, schedule, args=[platform, uid])
        message_timers[platform][uid] = t
        t.start()

        debug("⏳ QUEUE UPDATED", {"platform": platform, "user": uid, "note": "Typing signal sent immediately"})

# ============================================================
# Webhook route
# ============================================================
@app.route("/manychat_webhook", methods=["POST"])
def webhook():
    # تحقق من هيدر السر لو موضوع
    if MANYCHAT_SECRET_KEY:
        auth = request.headers.get("Authorization")
        if auth != f"Bearer {MANYCHAT_SECRET_KEY}":
            return jsonify({"error": "unauthorized"}), 403

    data = request.get_json(silent=True)
    debug("📩 Webhook Received", data)

    if not data:
        return jsonify({"ok": True})

    contact = data.get("full_contact")
    if not contact:
        return jsonify({"error": "missing contact"}), 400

    # حماية إنستغرام: لو جلسة موجودة ومخزنة كـ Instagram لكن الcontact مش فيه ig_id → نتجاهل
    if sessions_collection is not None:
        try:
            existing = sessions_collection.find_one({"_id": str(contact.get("id"))})
            if existing and existing.get("platform") == "Instagram" and not contact.get("ig_id"):
                debug("⛔ IG BLOCK TRIGGERED", "No IG ID")
                return jsonify({"ignored": True}), 200
        except Exception:
            pass

    session = get_or_create_session(contact)

    # نجمع النص من الحقول المحتملة
    txt = (
        contact.get("last_text_input")
        or contact.get("last_input_text")
        or contact.get("last_input")
        or contact.get("last_media_url")
        or contact.get("last_attachment_url")
    )

    debug("📥 TEXT EXTRACTED (raw)", txt)

    if not txt:
        return jsonify({"ok": True})

    # نحدد النوع
    ctype, value = detect_type(txt)
    debug("🔍 TYPE DETECTED", {"type": ctype, "value": value})

    parts = []

    # 1) النص الأصلي — نضيفه لو المدخل مش مجرد لينك
    url_re = r"^(https?://[^\s]+)$"
    if not re.match(url_re, txt.strip()):
        # يحتوي على نص حر أو كلام مع لينك
        parts.append(f"النص الأصلي:\n{txt}\n")

    # 2) لو audio — ننزل ونعمل Whisper
    if ctype == "audio":
        audio_bytes = download_media(value)
        if audio_bytes:
            # حفظ مؤقت بامتداد مناسب
            suffix = ".mp3"
            # لو اليو آر إل ينتهي بمعلوميات امتداد نحاول استخدامه
            if value.lower().endswith(".m4a"):
                suffix = ".m4a"
            elif value.lower().endswith(".wav"):
                suffix = ".wav"
            elif value.lower().endswith(".mp3"):
                suffix = ".mp3"
            elif value.lower().endswith(".mp4"):
                suffix = ".mp4"  # audioclip mp4 from FB

            try:
                with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
                    tmp.write(audio_bytes)
                    tmp_path = tmp.name
                text = whisper_transcribe(tmp_path)
            except Exception as e:
                debug("❌ write temp audio failed", str(e))
                text = ""
            finally:
                try:
                    os.remove(tmp_path)
                except:
                    pass

            if text:
                parts.append(f"تفريغ الصوت:\n{text}\n")

    # 3) لو image — ننزل ونحول base64 وبعدين Vision
    if ctype == "image":
        img_bytes = download_media(value)
        if img_bytes:
            try:
                b64 = base64.b64encode(img_bytes).decode()
                vision = asyncio.run(vision_describe(b64))
                if vision:
                    parts.append(f"تحليل الصورة:\n{vision}\n")
            except Exception as e:
                debug("❌ image process failed", str(e))

    # 4) لو video => تجاهل كامل (حسب اختيار A اللي اتفقنا عليه)
    if ctype == "video":
        debug("⛔ Ignoring video (not audioclip)", value)
        # لا نضيف أي شيء

    # بناء النص النهائي (A format)
    final = "\n".join(parts).strip()
    if not final:
        # عشان مبعتش فاضي للمساعد، نبعت نقطة بدلًا من ذلك
        final = "."

    # أضف للنظام للارسال للمساعد (يتجمع مع بقية الرسائل إن وُجد)
    add_to_queue(session, final)

    return jsonify({"ok": True}), 200

# ============================================================
# Home route
# ============================================================
@app.route("/")
def home():
    return "Bot running — Sound & Image only → Text (A-format)."

# ============================================================
# Run
# ============================================================
if __name__ == "__main__":
    logger.info("🚀 السيرفر جاهز للعمل")
    app.run(host="0.0.0.0", port=PORT)
