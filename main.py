
import os
import time
import json
import requests
import threading
import asyncio
import logging
import base64
from flask import Flask, request, jsonify
from openai import OpenAI
from pymongo import MongoClient
from datetime import datetime, timezone
from dotenv import load_dotenv

# ===========================
# إعداد اللوجات بالعربي
# ===========================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    handlers=[logging.StreamHandler()]
)
logger = logging.getLogger(__name__)

logger.info("▶️ بدء تشغيل التطبيق...")

# ===========================
# تحميل الإعدادات من .env
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
    logger.error(f"❌ فشل الاتصال بقاعدة البيانات: {e}")
    raise

# ===========================
# إعداد Flask و OpenAI
# ===========================
app = Flask(__name__)
client = OpenAI(api_key=OPENAI_API_KEY)
logger.info("🚀 Flask و OpenAI جاهزين")

# ===========================
# متغيرات التحكم بالتجميع والقفل
# ===========================
pending_messages = {}      # user_id -> {"texts": [...], "session": session}
message_timers = {}        # user_id -> threading.Timer
queue_lock = threading.Lock()   # لحماية pending_messages و message_timers
run_locks = {}             # user_id -> threading.Lock() يمنع أكثر من run واحد لنفس المستخدم

BATCH_WAIT_TIME = 9.0      # تم رفعها إلى 4 ثواني بعد طلبك
RETRY_DELAY_WHEN_BUSY = 3.0  # ثانية لإعادة المحاولة لو فيه run شغال

# ===========================
# دوال مساعدة لإدارة السيشن
# ===========================
def get_or_create_session_from_contact(contact_data, platform):
    user_id = str(contact_data.get("id"))
    if not user_id:
        logger.error("❌ user_id غير موجود في data")
        return None

    session = sessions_collection.find_one({"_id": user_id})
    now_utc = datetime.now(timezone.utc)

    main_platform = "Instagram" if "instagram" in (contact_data.get("source","").lower()) else "Facebook"

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
        "tags": [f"source:{main_platform.lower()}"],
        "custom_fields": contact_data.get("custom_fields", {}),
        "conversation_summary": "",
        "status": "active",
        "first_contact_date": now_utc,
        "last_contact_date": now_utc
    }
    sessions_collection.insert_one(new_session)
    logger.info(f"🆕 إنشاء جلسة جديدة للمستخدم {user_id}")
    return new_session

# ===========================
# Vision + Whisper (كما كان)
# ===========================
async def get_image_description_for_assistant(base64_image):
    logger.info("🖼️ معالجة صورة...")
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
            max_tokens=500
        )
        return response.choices[0].message.content
    except Exception as e:
        logger.error(f"❌ خطأ في معالجة الصورة: {e}", exc_info=True)
        return None

def transcribe_audio(content, fmt="mp4"):
    filename = f"temp.{fmt}"
    with open(filename, "wb") as f:
        f.write(content)

    try:
        with open(filename, "rb") as f:
            tr = client.audio.transcriptions.create(model="whisper-1", file=f)
        os.remove(filename)
        return tr.text
    except Exception as e:
        logger.error(f"❌ فشل تحويل الصوت لنص: {e}")
        try:
            os.remove(filename)
        except:
            pass
        return None

def download_media_from_url(url):
    try:
        r = requests.get(url, timeout=15)
        r.raise_for_status()
        return r.content
    except Exception as e:
        logger.error(f"❌ فشل تحميل الوسائط من URL: {e}")
        return None

# ===========================
# استدعاءات OpenAI (كوروتين) — تعمل على أي Event Loop
# ===========================
async def get_assistant_reply_async(session, content):
    """
    دالة async تتعامل مع OpenAI Threads API:
    - تنشئ thread لو مش موجود
    - تضيف رسالة
    - تطلب تشغيل run وتنتظر لحد ما يكمل
    - ترجع نص الرد
    """
    user_id = session["_id"]
    thread_id = session.get("openai_thread_id")

    # إنشاء thread لو مش موجود (يتم في background thread باستخدام to_thread لأن المكتبة sync)
    if not thread_id:
        thread = await asyncio.to_thread(client.beta.threads.create)
        thread_id = thread.id
        sessions_collection.update_one({"_id": user_id}, {"$set": {"openai_thread_id": thread_id}})
        logger.info(f"🔧 تم إنشاء thread جديد: {thread_id} للمستخدم {user_id}")

    # أضف الرسالة للـ thread
    try:
        await asyncio.to_thread(
            client.beta.threads.messages.create,
            thread_id=thread_id,
            role="user",
            content=content
        )
    except Exception as e:
        logger.error(f"❌ خطأ أثناء إضافة رسالة إلى thread ({thread_id}): {e}", exc_info=True)
        raise

    # اطلب run
    run = await asyncio.to_thread(
        client.beta.threads.runs.create,
        thread_id=thread_id,
        assistant_id=ASSISTANT_ID_PREMIUM
    )

    # انتظر حتى يكتمل الـ run
    while run.status in ["queued", "in_progress"]:
        await asyncio.sleep(1)
        run = await asyncio.to_thread(
            client.beta.threads.runs.retrieve,
            thread_id=thread_id,
            run_id=run.id
        )

    if run.status == "completed":
        messages = await asyncio.to_thread(
            client.beta.threads.messages.list,
            thread_id=thread_id,
            limit=1
        )
        try:
            return messages.data[0].content[0].text.value.strip()
        except Exception:
            return "⚠️ تمت المعالجة لكن لم يتم استرجاع نص الرد."
    else:
        logger.error(f"❌ Run انتهى بحالة غير مكتملة: {run.status}")
        return "⚠️ حدث خطأ أثناء معالجة الرسالة."

# ===========================
# إرسال رد واحد متكامل لـ ManyChat (بدون تقسيم)
# ===========================
def send_manychat_reply(subscriber_id, text_message, platform):
    logger.info(f"💬 إرسال رد للعميل {subscriber_id}")

    if not MANYCHAT_API_KEY:
        logger.error("❌ MANYCHAT_API_KEY غير مضبوطة")
        return

    url = "https://api.manychat.com/fb/sending/sendContent"
    headers = {
        "Authorization": f"Bearer {MANYCHAT_API_KEY}",
        "Content-Type": "application/json"
    }

    channel = "instagram" if platform == "Instagram" else "facebook"

    msgs = [{"type": "text", "text": text_message}]  # رسالة واحدة فقط

    payload = {
        "subscriber_id": str(subscriber_id),
        "data": {"version": "v2", "content": {"messages": msgs}},
        "channel": channel,
    }

    try:
        resp = requests.post(url, headers=headers, data=json.dumps(payload), timeout=15)
        resp.raise_for_status()
    except Exception as e:
        logger.error(f"❌ فشل إرسال الرد لـ ManyChat: {e}", exc_info=True)

# ===========================
# دالة الجدولة التي تعمل في Thread (باتش للإرسال)
# ===========================
def schedule_assistant_response(user_id):
    """
    تعمل داخل Thread (Timer). خطوات الأمان:
    - نحصل على البيانات تحت queue_lock
    - نحاول نأخذ run_lock للمستخدم (non-blocking)
      - لو مش فاضية: نعيد جدولة بعد RETRY_DELAY_WHEN_BUSY ثانية
    - لو اخدنا القفل: ننشئ event loop محلي وننفذ get_assistant_reply_async
    - نحرر القفل بعد الانتهاء
    """
    # أولاً خذ البيانات المجمعة بأمان
    with queue_lock:
        data = pending_messages.get(user_id)
        if not data:
            return

        session = data["session"]
        merged = "\n".join(data["texts"])

    # تأكد إن عندنا قفل Run للمستخدم
    user_run_lock = run_locks.setdefault(user_id, threading.Lock())

    # لو في Run شغال الآن — اعادة جدولة
    if not user_run_lock.acquire(blocking=False):
        logger.info(f"⏳ يوجد رد شغال للمستخدم {user_id} — إعادة جدولة بعد {RETRY_DELAY_WHEN_BUSY}s")
        # ضع مؤقت جديد لإعادة المحاولة
        with queue_lock:
            # أكد إن البيانات لا تزال موجودة (قد تكون اضيفت رسائل إضافية)
            if user_id in message_timers:
                try:
                    message_timers[user_id].cancel()
                except Exception:
                    pass
            t = threading.Timer(RETRY_DELAY_WHEN_BUSY, schedule_assistant_response, args=[user_id])
            message_timers[user_id] = t
            t.start()
        return

    # إذا وصلنا هنا — نملك القفل ونمضي للأمام
    try:
        # نزيل البيانات من الـ queue تحت القفل كي لا نرسلها مرتين
        with queue_lock:
            data = pending_messages.pop(user_id, None)
            try:
                message_timers.pop(user_id, None)
            except KeyError:
                pass

        if not data:
            logger.info(f"ℹ️ لا توجد رسائل للمستخدم {user_id} بعد.")
            return

        session = data["session"]
        merged = "\n".join(data["texts"])
        # === لوج مفصل للرسائل المجمعة قبل الإرسال ===
        logger.info("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
        logger.info(f"📦 الرسائل المجمعة قبل الإرسال للمساعد (المستخدم: {user_id}):")
        for i, msg in enumerate(data["texts"], start=1):
            logger.info(f"{i}) {msg}")
        logger.info("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
        logger.info(f"📝 النص النهائي المرسل للمساعد:\n{merged}")

        # === تشغيل event loop آمن داخل هذا الـ Thread ===
        loop = asyncio.new_event_loop()
        try:
            asyncio.set_event_loop(loop)
            # ننفذ الكوروتين الذي يتعامل مع OpenAI
            try:
                reply = loop.run_until_complete(get_assistant_reply_async(session, merged))
            except Exception as e:
                logger.error(f"❌ خطأ أثناء طلب المساعد: {e}", exc_info=True)
                reply = "⚠️ فشل الاتصال بخدمة المساعد."
        finally:
            try:
                loop.close()
            except:
                pass

        # أرسل الرد إلى ManyChat
        send_manychat_reply(user_id, reply, session["platform"])
        logger.info("✅ تم إرسال رد المساعد للعميل")
    finally:
        # إحرر قفل الـ run بعد كل شيء حتى لو حصل استثناء
        try:
            user_run_lock.release()
        except RuntimeError:
            # لو تم تحريره بالفعل أو لم يكن مؤمّنًا، نتجاهل
            pass

# ===========================
# إضافة رسالة إلى الطابور (Thread-safe)
# ===========================
def add_to_queue(session, text):
    uid = session["_id"]

    with queue_lock:
        if uid not in pending_messages:
            pending_messages[uid] = {"texts": [], "session": session}

        pending_messages[uid]["texts"].append(text)

        logger.info(f"📩 استلام رسالة جديدة من {uid}: {text}")
        logger.info(f"📊 إجمالي الرسائل المنتظرة لـ {uid}: {len(pending_messages[uid]['texts'])}")
        logger.info(f"⏳ تم إعادة ضبط التايمر على: {BATCH_WAIT_TIME} ثانية")

        # إلغاء أي تايمر سابق وإعادة جدولة تايمر جديد بعد آخر رسالة
        if uid in message_timers:
            try:
                message_timers[uid].cancel()
            except Exception:
                pass

        timer = threading.Timer(BATCH_WAIT_TIME, schedule_assistant_response, args=[uid])
        message_timers[uid] = timer
        timer.start()

# ===========================
# Webhook ManyChat
# ===========================
@app.route("/manychat_webhook", methods=["POST"])
def mc_webhook():
    # تحقق من الـ secret إذا موجود
    if MANYCHAT_SECRET_KEY:
        auth = request.headers.get("Authorization")
        if auth != f"Bearer {MANYCHAT_SECRET_KEY}":
            return jsonify({"error": "unauthorized"}), 403

    data = request.get_json()
    if not data:
        return jsonify({"error": "bad request"}), 400

    contact = data.get("full_contact")
    if not contact:
        return jsonify({"error": "missing contact"}), 400

    session = get_or_create_session_from_contact(contact, "ManyChat")
    if not session:
        return jsonify({"error": "no session"}), 400

    txt = contact.get("last_text_input") or contact.get("last_input_text")
    if not txt:
        return jsonify({"ok": True}), 200

    logger.info(f"📥 رسالة واردة من {session['_id']}: {txt}")

    is_url = isinstance(txt, str) and txt.startswith("http")
    is_media = is_url and ("cdn.fbsbx.com" in txt or "scontent" in txt)

    def bg():
        if is_media:
            media = download_media_from_url(txt)
            if not media:
                send_manychat_reply(session["_id"], "لم أتمكن من تحميل الوسائط.", session["platform"])
                return

            if any(ext in txt for ext in [".mp3", ".mp4", ".ogg"]):
                tr = transcribe_audio(media)
                if tr:
                    add_to_queue(session, f"[رسالة صوتية]: {tr}")
            else:
                desc = asyncio.run(get_image_description_for_assistant(base64.b64encode(media).decode()))
                if desc:
                    add_to_queue(session, f"[صورة]: {desc}")
        else:
            add_to_queue(session, txt)

    threading.Thread(target=bg, daemon=True).start()
    return jsonify({"ok": True}), 200

# ===========================
# صفحة رئيسية بسيطة
# ===========================
@app.route("/")
def home():
    return "Bot running (V3) - Arabic logs."

# ===========================
# تشغيل السيرفر
# ===========================
if __name__ == "__main__":
    logger.info("🚀 السيرفر جاهز للعمل")
    # على Render عادة لا تحتاج لتمرير host/port لكن لنستخدم القيم المحلية للـ debug
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", 5000)))
