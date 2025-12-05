import os
import re
import time
import logging
import requests
from flask import Flask, request, jsonify
from datetime import datetime, timezone
from openai import OpenAI
from pymongo import MongoClient

# -------------------------------------------------
# CONFIG
# -------------------------------------------------

app = Flask(__name__)

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
MANYCHAT_TOKEN = os.getenv("MANYCHAT_TOKEN")
MONGO_URI = os.getenv("MONGO_URI")

client = OpenAI(api_key=OPENAI_API_KEY)

mongo = MongoClient(MONGO_URI)
db = mongo["chatbot_db"]
sessions_collection = db["sessions"]
queue_collection = db["queue"]

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - INFO - %(message)s"
)

# -------------------------------------------------
# HELPERS
# -------------------------------------------------

URL_REGEX = r"(https?://\S+)"

def extract_urls(text):
    return re.findall(URL_REGEX, text)


def get_or_create_session(contact):
    user_id = contact.get("id")
    platform = "Facebook"

    session = sessions_collection.find_one({"user_id": user_id})

    if session:
        return session

    new_session = {
        "user_id": user_id,
        "platform": platform,
        "conversation_id": None,
        "messages": []
    }
    sessions_collection.insert_one(new_session)
    return new_session


def save_session(session):
    sessions_collection.update_one(
        {"user_id": session["user_id"]},
        {"$set": session}
    )


def add_to_queue(user_id, platform, text):
    queue_collection.insert_one({
        "user_id": user_id,
        "platform": platform,
        "incoming_text": text,
        "timestamp": time.time()
    })


def get_oldest_queue(user_id):
    return queue_collection.find_one(
        {"user_id": user_id},
        sort=[("timestamp", 1)]
    )


def remove_from_queue(item_id):
    queue_collection.delete_one({"_id": item_id})


def send_typing(user_id):
    url = "https://api.manychat.com/fb/sending/sendAction"
    headers = {
        "Authorization": f"Bearer {MANYCHAT_TOKEN}",
        "Content-Type": "application/json"
    }
    payload = {
        "recipient": {"user_id": user_id},
        "action": "typing_on"
    }
    requests.post(url, json=payload, headers=headers)


def send_manychat(user_id, text):
    url = "https://api.manychat.com/fb/sending/sendContent"
    headers = {
        "Authorization": f"Bearer {MANYCHAT_TOKEN}",
        "Content-Type": "application/json"
    }
    payload = {
        "recipient": {"user_id": user_id},
        "message": {"text": text}
    }
    r = requests.post(url, json=payload, headers=headers)
    logging.info(f"🔍 📤 ManyChat Response: {r.status_code}")
    return r.status_code


# -------------------------------------------------
# BUILD GPT MESSAGE
# -------------------------------------------------

def build_combined_message(text):
    urls = extract_urls(text)

    combined = "النص الأصلي:\n"
    combined += text.strip() + "\n\n"

    if urls:
        combined += "روابط مستلمة:\n"
        for u in urls:
            combined += f"- {u}\n"
        combined += "\nتنبيه للمساعد: حلّل الصورة أو الصوت من الروابط مباشرة.\n"

    return combined


# -------------------------------------------------
# GPT CALL
# -------------------------------------------------

def ask_gpt(session, message):
    conv_id = session.get("conversation_id")

    if conv_id is None:
        conv = client.conversations.create()
        conv_id = conv.id
        session["conversation_id"] = conv_id
        save_session(session)

    try:
        response = client.responses.create(
            model="gpt-5-mini",
            conversation=conv_id,
            input=message
        )
        return response.output_text

    except Exception as e:
        logging.info(f"❌ GPT Error: {e}")
        return "حصل خطأ أثناء معالجة الرسالة، جرّب تبعتها تاني."


# -------------------------------------------------
# PROCESS QUEUE
# -------------------------------------------------

def process_queue_for(user_id):
    queue_item = get_oldest_queue(user_id)
    if not queue_item:
        return

    text = queue_item["incoming_text"]
    platform = queue_item["platform"]

    session = sessions_collection.find_one({"user_id": user_id})

    merged = text

    reply = ask_gpt(session, merged)

    send_manychat(user_id, reply)

    remove_from_queue(queue_item["_id"])


# -------------------------------------------------
# WEBHOOK
# -------------------------------------------------

@app.route("/manychat_webhook", methods=["POST"])
def webhook():
    data = request.json

    full_contact = data.get("full_contact", {})
    user_id = full_contact.get("id")
    text = full_contact.get("last_input_text", "")

    session = get_or_create_session(full_contact)

    combined = build_combined_message(text)

    send_typing(user_id)
    add_to_queue(user_id, "Facebook", combined)

    time.sleep(2)
    process_queue_for(user_id)

    return jsonify({"status": "ok"})


# -------------------------------------------------
# RUN
# -------------------------------------------------

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=10000)

