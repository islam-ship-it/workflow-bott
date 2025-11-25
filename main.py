import os
import time
import json
import requests
import threading
import asyncio
import logging
import tempfile
from functools import wraps
from flask import Flask, request, jsonify
from openai import OpenAI
from pymongo import MongoClient, ASCENDING
from datetime import datetime, timezone
from dotenv import load_dotenv

# ===========================
# إعداد اللوجات
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
client_db = MongoClient(MONGO_URI)
db = client_db["multi_platform_bot"]
sessions_collection = db["sessions"]
sessions_collection.create_index([("_id", ASCENDING)], unique=True)
logger.info("✅ متصل بقاعدة البيانات")

# ===========================
# Flask + OpenAI
# ===========================
app = Flask(__name__)
client = OpenAI(api_key=OPENAI_API_KEY)
logger.info("🚀 Flask و OpenAI جاهزين")

# ===========================
# التجميع والقفل
# ===========================
pending_messages = {}  # user_id -> {"items": [...], "session": session}
message_timers = {}    # user_id -> Timer
queue_lock = threading.Lock()
run_locks = {}         # user_id -> Lock

BATCH_WAIT_TIME = 9
RETRY_DELAY_WHEN_BUSY = 3

# ===========================
# retry decorator
# ===========================
def retry_on_exception(max_attempts=3, initial_delay=0.8, backoff=2.0, allowed_exceptions=(Exception,)):
    def decorator(fn):
        @wraps(fn)
        def wrapper(*args, **kwargs):
            delay = initial_delay
            for attempt in range(1, max_attempts + 1):
                try:
                    return fn(*args, **kwargs)
                except allowed_exceptions as e:
                    if attempt == max_attempts:
                        raise
                    logger.warning(f"⚠️ محاولة {attempt} فشلت لـ {fn.__name__}: {e}")
                    time.sleep(delay)
                    delay *= backoff
        return wrapper
    return decorator

# ===========================
# الجلسات
# ===========================
def get_or_create_session_from_contact(contact_data, platform):
    user_id = str(contact_data.get("id"))
    if not user_id:
        return None

    session = sessions_collection.find_one({"_id": user_id})
    now = datetime.now(timezone.utc)

    main_platform = "Instagram" if "instagram" in (contact_data.get("source", "").lower()) else "Facebook"

    if session:
        sessions_collection.update_one(
            {"_id": user_id},
            {"$set": {
                "last_contact_date": now,
                "platform": main_platform,
                "profile.name": contact_data.get("name"),
                "profile.profile_pic": contact_data.get("profile_pic"),
                "status": "active",
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
        "first_contact_date": now,
        "last_contact_date": now,
    }
    sessions_collection.insert_one(new_session)
    return new_session

# ===========================
# Whisper للصوت
# ===========================
@retry_on_exception()
def transcribe_audio(content, fmt="mp4"):
    with tempfile.NamedTemporaryFile(delete=False, suffix=f".{fmt}") as tmp:
        tmp_name = tmp.name
        tmp.write(content)

    try:
        with open(tmp_name, "rb") as f:
            tr = client.audio.transcriptions.create(model="whisper-1", file=f)
        return tr.text
    finally:
        try:
            os.remove(tmp_name)
        except:
            pass

# ===========================
# إرسال الصورة/النص كمحتوى مُجمّع للمساعد
# ===========================
async def get_assistant_reply_async(session, content):
    """
    content: يمكن أن يكون سلسلة نصية أو قائمة عناصر بصيغة OpenAI threads message content
    مثال لقائمة content:
    [ {"type":"text","text":"مرحبًا"}, {"type":"image_url","image_url":{"url":"https://..."}} ]
    """
    user_id = session["_id"]
    thread_id = session.get("openai_thread_id")

    if not thread_id:
        thread = await asyncio.to_thread(client.beta.threads.create)
        thread_id = thread.id
        sessions_collection.update_one({"_id": user_id}, {"$set": {"openai_thread_id": thread_id}})

    # أضف الرسالة للـ thread
    try:
        await asyncio.to_thread(
            client.beta.threads.messages.create,
            thread_id=thread_id,
            role="user",
            content=content,
        )
    except Exception as e:
        logger.error(f"❌ خطأ أثناء إضافة رسالة إلى thread ({thread_id}): {e}", exc_info=True)
        raise

    # اطلب run
    run = await asyncio.to_thread(
        client.beta.threads.runs.create,
        thread_id=thread_id,
        assistant_id=ASSISTANT_ID_PREMIUM,
    )

    # انتظر حتى يكتمل الـ run
    while run.status in ["queued", "in_progress"]:
        await asyncio.sleep(1)
        run = await asyncio.to_thread(
            client.beta.threads.runs.retrieve,
            thread_id=thread_id,
            run_id=run.id,
        )

    if run.status != "completed":
        logger.error(f"❌ Run انتهى بحالة غير مكتملة: {run.status}")
        return "⚠️ حدث خطأ أثناء معالجة الرسالة."

    msgs = await asyncio.to_thread(
        client.beta.threads.messages.list,
        thread_id=thread_id,
        limit=1,
    )

    try:
        return msgs.data[0].content[0].text.value.strip()
    except Exception:
        return "⚠️ لم أستقبل رد المساعد"

# ===========================
# إرسال ManyChat
# ===========================
@retry_on_exception(max_attempts=3, allowed_exceptions=(requests.RequestException,))
def send_manychat_reply(subscriber_id, text, platform):
    url = "https://api.manychat.com/fb/sending/sendContent"
    headers = {
        "Authorization": f"Bearer {MANYCHAT_API_KEY}",
        "Content-Type": "application/json",
    }

    payload = {
        "subscriber_id": str(subscriber_id),
        "channel": "instagram" if platform == "Instagram" else "facebook",
        "data": {
            "version": "v2",
            "content": {"messages": [{"type": "text", "text": text}]},
        },
    }

    resp = requests.post(url, headers=headers, data=json.dumps(payload), timeout=15)
    resp.raise_for_status()

# ===========================
# المعالجة بالتايمر (Batch)
# ===========================
def schedule_assistant_response(user_id):
    # احصل على البيانات المجمعة بأمان
    with queue_lock:
        data = pending_messages.get(user_id)
        if not data:
            return
        session = data["session"]
        items = data["items"]

        # تأكد من وجود قفل run للمستخدم
        user_run_lock = run_locks.setdefault(user_id, threading.Lock())

    # إذا هناك Run شغال — إعادة جدولة
    if not user_run_lock.acquire(blocking=False):
        logger.info(f"⏳ يوجد رد شغال للمستخدم {user_id} — إعادة جدولة بعد {RETRY_DELAY_WHEN_BUSY}s")
        with queue_lock:
            if user_id in message_timers:
                try:
                    message_timers[user_id].cancel()
                except:
                    pass
            t = threading.Timer(RETRY_DELAY_WHEN_BUSY, schedule_assistant_response, args=[user_id])
            message_timers[user_id] = t
            t.start()
        return

    # امسك البيانات (ثم احذفها من الطابور)
    try:
        with queue_lock:
            data = pending_messages.pop(user_id, None)
            try:
                message_timers.pop(user_id, None)
            except KeyError:
                pass

        if not data:
            return

        session = data["session"]
        items = data["items"]

        # بناء content للقِسم الواحد — مجموعة من كائنات text/image_url
        content = []
        for it in items:
            if isinstance(it, dict) and it.get("type") == "image":
                content.append({"type": "image_url", "image_url": {"url": it.get("url")}})
            else:
                # نص عادي
                txt = it if isinstance(it, str) else it.get("text") if isinstance(it, dict) else str(it)
                content.append({"type": "text", "text": txt})

        logger.info("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
        logger.info(f"📦 إرسال محتوى مجمّع للمساعد (المستخدم: {user_id})، العناصر: {len(items)}")
        for i, it in enumerate(items, start=1):
            logger.info(f"{i}) {it}")
        logger.info("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")

        # تشغيل event loop محلي لاستدعاء الـ async
        loop = asyncio.new_event_loop()
        try:
            asyncio.set_event_loop(loop)
            try:
                reply = loop.run_until_complete(get_assistant_reply_async(session, content))
            except Exception as e:
                logger.error(f"❌ خطأ أثناء طلب المساعد: {e}", exc_info=True)
                reply = "⚠️ فشل الاتصال بخدمة المساعد."
        finally:
            try:
                loop.close()
            except:
                pass

        # أرسل الرد إلى ManyChat
        try:
            send_manychat_reply(user_id, reply, session.get("platform", "Facebook"))
            logger.info("✅ تم إرسال رد المساعد للعميل")
        except Exception:
            logger.exception("❌ حدث خطأ أثناء إرسال الرد إلى ManyChat")
    finally:
        try:
            user_run_lock.release()
        except RuntimeError:
            pass

# ===========================
# إضافة إلى الطابور (يمكن إضافة نص أو dict لصورة)
# ===========================
def add_to_queue(session, item):
    uid = session["_id"]

    with queue_lock:
        if uid not in pending_messages:
            pending_messages[uid] = {"items": [], "session": session}

        pending_messages[uid]["items"].append(item)

        logger.info(f"📩 استلام عنصر جديد من {uid}: {item}")
        logger.info(f"📊 إجمالي العناصر المنتظرة لـ {uid}: {len(pending_messages[uid]['items'])}")
        logger.info(f"⏳ تم إعادة ضبط التايمر على: {BATCH_WAIT_TIME} ثانية")

        if uid in message_timers:
            try:
                message_timers[uid].cancel()
            except:
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
        try:
            if is_media:
                # بدل ما ننزل الصورة — نحفظ الرابط كمهمّة بالصورة داخل الـ batch
                add_to_queue(session, {"type": "image", "url": txt})
            elif is_url and any(ext in txt for ext in [".mp3", ".mp4", ".ogg"]):
                # تنزيل ونسخ صوتي ثم اضافته كنص
                try:
                    media = requests.get(txt, timeout=15).content
                    tr = transcribe_audio(media)
                    if tr:
                        add_to_queue(session, tr)
                except Exception:
                    logger.exception("❌ فشل تنزيل أو نسخ الصوت")
                    add_to_queue(session, "[رسالة صوتية]: لم أتمكن من معالجة الملف الصوتي.")
            else:
                # نص عادي
                add_to_queue(session, txt)
        except Exception:
            logger.exception("❌ خطأ في معالجة الخلفية للـ webhook")

    threading.Thread(target=bg, daemon=True).start()
    return jsonify({"ok": True}), 200

# ===========================
# صفحة رئيسية
# ===========================
@app.route("/")
def home():
    return "Bot running (V3) - Arabic logs. Batch includes images as links."

# ===========================
# تشغيل السيرفر
# ===========================
if __name__ == "__main__":
    logger.info("🚀 السيرفر جاهز للعمل")
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", 5000)))
