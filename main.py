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
import subprocess

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
# متغيرات التحكم
# ===========================
pending_messages = {}
message_timers = {}
queue_lock = threading.Lock()
run_locks = {}

BATCH_WAIT_TIME = 9.0
RETRY_DELAY_WHEN_BUSY = 3.0

# ===========================
# السيشن
# ===========================
def get_or_create_session_from_contact(contact_data, platform):
    user_id = str(contact_data.get("id"))
    if not user_id:
        logger.error("❌ user_id غير موجود")
        return None

    session = sessions_collection.find_one({"_id": user_id})
    now_utc = datetime.now(timezone.utc)

    # ManyChat يرسل حقل "source" الذي يحتوي على "instagram" أو "facebook"
    source_lower = contact_data.get("source", "").lower()
    main_platform = "Instagram" if "instagram" in source_lower else "Facebook"
    # إضافة حقل جديد لتخزين مصدر الرسالة
    contact_data["platform_source"] = main_platform

    if session:
        sessions_collection.update_one(
            {"_id": user_id},
            {"$set": {
                "last_contact_date": now_utc,
                "platform": main_platform,
                "platform_source": main_platform, # تحديث حقل مصدر المنصة
                "profile.name": contact_data.get("name"),
                "profile.profile_pic": contact_data.get("profile_pic"),
                "status": "active"
            }}
        )
        return sessions_collection.find_one({"_id": user_id})

    new_session = {
        "_id": user_id,
        "platform": main_platform,
        "platform_source": main_platform, # إضافة حقل مصدر المنصة للجلسة الجديدة
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
    logger.info(f"🆕 إنشاء جلسة جديدة للمستخدم {user_id}")
    return new_session

# ===========================
# Vision + Whisper
# ===========================
def upload_file_to_url(file_path):
    try:
        # استخدام أداة manus-upload-file لرفع الملف والحصول على رابط عام
        result = subprocess.run(
            ["manus-upload-file", file_path],
            capture_output=True,
            text=True,
            check=True
        )
        # الرابط العام هو آخر سطر في الإخراج
        return result.stdout.strip().split('\n')[-1]
    except subprocess.CalledProcessError as e:
        logger.error(f"❌ فشل رفع الملف: {e.stderr}")
        return None

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
            max_tokens=300
        )
        return response.choices[0].message.content
    except Exception as e:
        logger.error(f"❌ خطأ معالجة الصورة: {e}")
        return None

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

def download_media_from_url(url):
    try:
        r = requests.get(url, timeout=15)
        r.raise_for_status()
        return r.content
    except:
        return None

# ===========================
# OpenAI Thread Runner
# ===========================
async def get_assistant_reply_async(session, content):
    user_id = session["_id"]
    thread_id = session.get("openai_thread_id")

    if not thread_id:
        thread = await asyncio.to_thread(client.beta.threads.create)
        thread_id = thread.id
        sessions_collection.update_one({"_id": user_id}, {"$set": {"openai_thread_id": thread_id}})
        logger.info(f"🔧 إنشاء thread جديد: {thread_id}")

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
        return "⚠️ حصل خطأ أثناء المعالجة."

    msgs = await asyncio.to_thread(
        client.beta.threads.messages.list,
        thread_id=thread_id,
        limit=1
    )

    try:
        return msgs.data[0].content[0].text.value.strip()
    except:
        return "⚠️ لم أستطع استخراج الرد من المساعد."

# ===========================
# إرسال ManyChat (إصلاح 400)
# ===========================
def send_manychat_reply(subscriber_id, text_message, platform):
    logger.info(f"💬 إرسال رد للعميل {subscriber_id}")

    if not MANYCHAT_API_KEY:
        logger.error("❌ MANYCHAT_API_KEY غير موجود")
        return

    # الإصلاح النهائي:
    # ManyChat يستخدم /fb/ لإرسال رسائل FB + IG معًا، ولكن يمكن تحديد القناة في حمولة الـ webhook
    # ملاحظة: ManyChat API v2 يستخدم "facebook" كقناة موحدة لـ FB و IG
    channel = "facebook"
    url = "https://api.manychat.com/fb/sending/sendContent"

    payload = {
        "subscriber_id": str(subscriber_id),
        "channel": channel,
        "data": {
            "version": "v2",
            "content": {
                "messages": [
                    {"type": "text", "text": text_message}
                ]
            }
        }
    }

    headers = {
        "Authorization": f"Bearer {MANYCHAT_API_KEY}",
        "Content-Type": "application/json"
    }

    try:
        r = requests.post(url, headers=headers, data=json.dumps(payload))
        r.raise_for_status()
        logger.info("✅ تم إرسال الرد بنجاح")
    except Exception as e:
        logger.error(f"❌ فشل إرسال ManyChat: {e}")

# ===========================
# جدولة الردود
# ===========================
def schedule_assistant_response(user_id):
    with queue_lock:
        data = pending_messages.get(user_id)
        if not data:
            return

    lock = run_locks.setdefault(user_id, threading.Lock())

    if not lock.acquire(blocking=False):
        timer = threading.Timer(RETRY_DELAY_WHEN_BUSY, schedule_assistant_response, args=[user_id])
        timer.start()
        return

    try:
        with queue_lock:
            data = pending_messages.pop(user_id, None)
            message_timers.pop(user_id, None)

        if not data:
            return

        session = data["session"]
        merged = "\n".join(data["texts"])

        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
	        reply = loop.run_until_complete(get_assistant_reply_async(session, merged))
	        loop.close()
	
	        # حفظ رد المساعد في سجل المحادثات
	        sessions_collection.update_one(
	            {"_id": user_id},
	            {"$push": {
	                "history": {
	                    "role": "assistant",
	                    "content": reply,
	                    "timestamp": datetime.now(timezone.utc)
	                }
	            }}
	        )
	
	        send_manychat_reply(user_id, reply, session["platform"])

    finally:
        lock.release()

# ===========================
# إضافة رسالة لل큐
# ===========================
def add_to_queue(session, text):
    uid = session["_id"]

    with queue_lock:
	    if uid not in pending_messages:
	        pending_messages[uid] = {"texts": [], "session": session}

	    pending_messages[uid]["texts"].append(text)
	    
	    # حفظ رسالة المستخدم في سجل المحادثات
	    sessions_collection.update_one(
	        {"_id": uid},
	        {"$push": {
	            "history": {
	                "role": "user",
	                "content": text,
	                "timestamp": datetime.now(timezone.utc)
	            }
	        }}
	    )

        if uid in message_timers:
            message_timers[uid].cancel()

        timer = threading.Timer(BATCH_WAIT_TIME, schedule_assistant_response, args=[uid])
        message_timers[uid] = timer
        timer.start()

# ===========================
# Webhook
# ===========================
@app.route("/manychat_webhook", methods=["POST"])
def mc_webhook():
    if MANYCHAT_SECRET_KEY:
        auth = request.headers.get("Authorization")
        if auth != f"Bearer {MANYCHAT_SECRET_KEY}":
            return jsonify({"error": "unauthorized"}), 403

    data = request.get_json()
    contact = data.get("full_contact")

    session = get_or_create_session_from_contact(contact, "ManyChat")

    # استخراج النص والوسائط
    txt = contact.get("last_text_input") or contact.get("last_input_text")
    media_url = data.get("media_url") # افتراض أن ManyChat يرسل media_url مباشرة في حمولة الـ webhook

    message_content = []

    # 1. معالجة الوسائط (الصورة)
    if media_url:
        logger.info(f"🖼️ تم استلام وسائط: {media_url}")
        
        # تحميل محتوى الوسائط
        media_content = download_media_from_url(media_url)
        
        if media_content:
            # حفظ الوسائط مؤقتًا
            with tempfile.NamedTemporaryFile(delete=False) as tmp:
                tmp.write(media_content)
                path = tmp.name
            
            # رفع الملف للحصول على رابط عام
            public_url = upload_file_to_url(path)
            os.remove(path) # حذف الملف المؤقت
            
            if public_url:
                # إضافة الرابط العام إلى محتوى الرسالة للمساعد
                message_content.append(f"[صورة مرفقة: {public_url}]")
                logger.info(f"✅ تم تحويل الصورة إلى رابط: {public_url}")
            else:
                logger.warning("⚠️ فشل الحصول على رابط عام للصورة.")
        else:
            logger.warning("⚠️ فشل تحميل محتوى الوسائط.")

    # 2. معالجة النص
    if txt:
        message_content.append(txt)

    # 3. إرسال المحتوى المدمج إلى قائمة الانتظار
    if message_content:
        merged_content = "\n".join(message_content)
        add_to_queue(session, merged_content)
        logger.info(f"✉️ إرسال المحتوى المدمج للمساعد: {merged_content}")

    return jsonify({"ok": True}), 200

    return jsonify({"ok": True}), 200

# ===========================
# Home
# ===========================
	@app.route("/")
	def home():
	    return "Bot running V3 Final – عربي"
	
	# ===========================
	# طباعة المحادثات
	# ===========================
	@app.route("/print_history/<user_id>", methods=["GET"])
	def print_history(user_id):
	    session = sessions_collection.find_one({"_id": user_id})
	
	    if not session:
	        return f"لا يوجد سجل محادثات للمستخدم: {user_id}", 404
	
	    history = session.get("history", [])
	    
	    if not history:
	        return f"سجل المحادثات فارغ للمستخدم: {user_id}", 200
	
	    # تنسيق المحادثة في HTML لسهولة الطباعة
	    html_content = f"""
	    <!DOCTYPE html>
	    <html lang="ar" dir="rtl">
	    <head>
	        <meta charset="UTF-8">
	        <title>سجل المحادثات للمستخدم {user_id}</title>
	        <style>
	            body {{ font-family: 'Arial', sans-serif; line-height: 1.6; padding: 20px; direction: rtl; }}
	            .message {{ margin-bottom: 15px; padding: 10px; border-radius: 8px; }}
	            .user {{ background-color: #e6f7ff; border-left: 5px solid #1890ff; }}
	            .assistant {{ background-color: #f6ffed; border-right: 5px solid #52c41a; text-align: right; }}
	            .role {{ font-weight: bold; margin-bottom: 5px; }}
	            .timestamp {{ font-size: 0.8em; color: #8c8c8c; }}
	            .content {{ white-space: pre-wrap; }}
	            h1 {{ border-bottom: 2px solid #eee; padding-bottom: 10px; }}
	        </style>
	    </head>
	    <body>
	        <h1>سجل المحادثات</h1>
	        <p><strong>معرف المستخدم:</strong> {user_id}</p>
	        <p><strong>المنصة:</strong> {session.get("platform_source", "غير محدد")}</p>
	        <p><strong>الاسم:</strong> {session.get("profile", {}).get("name", "غير متوفر")}</p>
	        <hr>
	    """
	
	    for msg in history:
	        role = "المستخدم" if msg["role"] == "user" else "المساعد"
	        css_class = "user" if msg["role"] == "user" else "assistant"
	        timestamp = msg["timestamp"].strftime("%Y-%m-%d %H:%M:%S") if isinstance(msg["timestamp"], datetime) else str(msg["timestamp"])
	        
	        html_content += f"""
	        <div class="message {css_class}">
	            <div class="role">{role} <span class="timestamp">({timestamp})</span></div>
	            <div class="content">{msg["content"]}</div>
	        </div>
	        """
	
	    html_content += "</body></html>"
	
	    return html_content, 200, {'Content-Type': 'text/html; charset=utf-8'}

# ===========================
# Run
# ===========================
if __name__ == "__main__":
    logger.info("🚀 السيرفر جاهز")
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", 5000)))
