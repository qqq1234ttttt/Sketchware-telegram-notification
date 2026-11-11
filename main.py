import os
import time
import threading
import logging
from datetime import datetime, timezone

import requests
import firebase_admin
from firebase_admin import credentials, messaging
from flask import Flask, jsonify

# ================= CONFIG =================

BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
ADMIN_ID = os.environ.get("TELEGRAM_ADMIN_ID", "")
PROJECT_ID = os.environ.get("FIREBASE_PROJECT_ID", "")
CLIENT_EMAIL = os.environ.get("FIREBASE_CLIENT_EMAIL", "")
PRIVATE_KEY = os.environ.get("FIREBASE_PRIVATE_KEY", "").replace("\\n", "\n")

API = f"https://api.telegram.org/bot{BOT_TOKEN}"
app = Flask(__name__)
logging.basicConfig(level=logging.INFO)

# ================= STATE =================

lock = threading.RLock()
offset = 0
drafts = {}
history = []
current_job = None
next_id = 1
last_activity = "Bot started"
started_at = time.time()

# ================= FIREBASE =================

firebase_ready = False

try:
    if all([PROJECT_ID, CLIENT_EMAIL, PRIVATE_KEY]):
        cred = credentials.Certificate({
            "type": "service_account",
            "project_id": PROJECT_ID,
            "private_key": PRIVATE_KEY,
            "client_email": CLIENT_EMAIL,
            "token_uri": "https://oauth2.googleapis.com/token",
        })
        firebase_admin.initialize_app(cred)
        firebase_ready = True
        logging.info("Firebase initialized")
    else:
        logging.warning("Firebase credentials are not configured")
except Exception:
    logging.exception("Firebase initialization failed")


# ================= TELEGRAM =================

def tg(method, data=None):
    if not BOT_TOKEN:
        raise RuntimeError("TELEGRAM_BOT_TOKEN is missing")

    response = requests.post(
        f"{API}/{method}",
        json=data or {},
        timeout=30
    )
    response.raise_for_status()
    result = response.json()

    if not result.get("ok"):
        raise RuntimeError(result.get("description", "Telegram API error"))

    return result["result"]


def send_text(chat_id, text, keyboard=None):
    data = {
        "chat_id": chat_id,
        "text": text,
    }

    if keyboard:
        data["reply_markup"] = {"inline_keyboard": keyboard}

    return tg("sendMessage", data)


def edit_text(chat_id, message_id, text, keyboard=None):
    data = {
        "chat_id": chat_id,
        "message_id": message_id,
        "text": text,
    }

    if keyboard:
        data["reply_markup"] = {"inline_keyboard": keyboard}

    try:
        tg("editMessageText", data)
    except Exception:
        send_text(chat_id, text, keyboard)


def is_admin(update):
    user = update.get("from", {})
    return bool(ADMIN_ID) and str(user.get("id", "")) == ADMIN_ID


def main_menu():
    return [
        [
            {"text": "📊 Dashboard", "callback_data": "dashboard"},
            {"text": "📝 Create Notification", "callback_data": "create"},
        ],
        [
            {"text": "📚 Notification History", "callback_data": "history"},
            {"text": "❌ Cancel", "callback_data": "cancel"},
        ],
    ]


def home(chat_id, text="KMT Notification Admin"):
    send_text(chat_id, text, main_menu())


def answer_callback(callback_id, text=""):
    try:
        tg("answerCallbackQuery", {
            "callback_query_id": callback_id,
            "text": text,
        })
    except Exception:
        logging.exception("Callback answer failed")


# ================= DASHBOARD =================

def dashboard(chat_id):
    with lock:
        count = len(history)
        job = dict(current_job) if current_job else None
        activity = last_activity

    firebase_status = "✅ Connected" if firebase_ready else "❌ Not configured"

    job_text = "No active sending job"

    if job:
        job_text = (
            f"Status: {job['status']}\n"
            f"Progress: {job['done']}/{job['total']}\n"
            f"Success: {job['success']}\n"
            f"Failed: {job['failed']}"
        )

    text = (
        "📊 ADMIN DASHBOARD\n\n"
        f"🤖 Telegram: {'✅ Configured' if BOT_TOKEN else '❌ Missing token'}\n"
        f"🔥 Firebase: {firebase_status}\n"
        f"📚 History records: {count}\n"
        f"⏱ Uptime: {int(time.time() - started_at)} seconds\n"
        f"📌 Last activity: {activity}\n\n"
        f"🚀 Sending status:\n{job_text}"
    )

    send_text(chat_id, text, main_menu())


# ================= CREATE / EDIT =================

def start_create(chat_id, user_id):
    with lock:
        drafts[user_id] = {
            "title": "",
            "body": "",
            "step": "title",
            "editing_id": None,
        }

    send_text(
        chat_id,
        "📝 CREATE NOTIFICATION\n\n"
        "Notification Title ကို ပို့ပါ။\n"
        "ပယ်ဖျက်ရန် /cancel ကို ပို့ပါ။"
    )


def start_edit(chat_id, user_id, notification_id):
    with lock:
        item = next(
            (x for x in history if x["id"] == notification_id),
            None
        )

        if not item:
            send_text(chat_id, "❌ Notification မတွေ့ပါ။")
            return

        drafts[user_id] = {
            "title": item["title"],
            "body": item["body"],
            "step": "edit_title",
            "editing_id": notification_id,
        }

    send_text(
        chat_id,
        "✏️ EDIT NOTIFICATION\n\n"
        f"လက်ရှိ Title: {item['title']}\n"
        "Title အသစ်ပို့ပါ။ မပြောင်းလိုလျှင် /skip ပို့ပါ။"
    )


def show_preview(chat_id, user_id):
    with lock:
        draft = drafts.get(user_id)

        if not draft:
            send_text(chat_id, "Draft မရှိတော့ပါ။ /start နှိပ်ပါ။")
            return

        title = draft["title"]
        body = draft["body"]

    keyboard = [
        [
            {"text": "✅ Confirm & Send", "callback_data": "confirm"},
        ],
        [
            {"text": "✏️ Edit Title", "callback_data": "edit_title"},
            {"text": "✏️ Edit Message", "callback_data": "edit_body"},
        ],
        [
            {"text": "❌ Cancel", "callback_data": "cancel"},
        ],
    ]

    send_text(
        chat_id,
        f"👁 NOTIFICATION PREVIEW\n\n"
        f"📌 Title: {title}\n\n"
        f"💬 Message:\n{body}\n\n"
        "အချက်အလက်တွေ မှန်ကန်ပါက Confirm & Send နှိပ်ပါ။",
        keyboard
    )


def handle_draft_message(chat_id, user_id, text):
    with lock:
        draft = drafts.get(user_id)

    if not draft:
        return False

    if text == "/cancel":
        with lock:
            drafts.pop(user_id, None)
        home(chat_id, "❌ Draft cancelled.")
        return True

    if text == "/skip":
        if draft["step"] == "edit_title":
            with lock:
                drafts[user_id]["step"] = "edit_body"
            send_text(
                chat_id,
                "Message အသစ်ပို့ပါ။ မပြောင်းလိုလျှင် /skip ပို့ပါ။"
            )
            return True

        if draft["step"] == "edit_body":
            show_preview(chat_id, user_id)
            return True

    step = draft["step"]

    if step in ("title", "edit_title"):
        if len(text) > 100:
            send_text(chat_id, "Title ကို စာလုံး 100 ထက်မကျော်ပါစေနဲ့။")
            return True

        with lock:
            drafts[user_id]["title"] = text
            drafts[user_id]["step"] = (
                "body" if step == "title" else "edit_body"
            )

        send_text(chat_id, "💬 Notification Message ကို ပို့ပါ။")
        return True

    if step in ("body", "edit_body"):
        if not text.strip():
            send_text(chat_id, "Message အလွတ်မဖြစ်ရပါ။")
            return True

        with lock:
            drafts[user_id]["body"] = text
            drafts[user_id]["step"] = "preview"

        show_preview(chat_id, user_id)
        return True

    return False


# ================= FCM SENDING =================

def send_fcm_notification(title, body):
    if not firebase_ready:
        raise RuntimeError("Firebase is not configured")

    message = messaging.Message(
        notification=messaging.Notification(
            title=title,
            body=body,
        ),
        topic="all",
        android=messaging.AndroidConfig(
            priority="high",
            notification=messaging.AndroidNotification(
                sound="default",
                channel_id="default",
            ),
        ),
    )

    return messaging.send(message)


def send_job(chat_id, user_id):
    global current_job, next_id, last_activity

    with lock:
        draft = drafts.get(user_id)

        if not draft:
            send_text(chat_id, "Draft မရှိပါ။")
            return

        if current_job and current_job["status"] == "sending":
            send_text(chat_id, "⚠️ Notification ပို့နေဆဲပါ။")
            return

        title = draft["title"].strip()
        body = draft["body"].strip()
        editing_id = draft.get("editing_id")

        if not title or not body:
            send_text(chat_id, "Title နဲ့ Message နှစ်ခုလုံး လိုပါတယ်။")
            return

        if editing_id:
            item = next(
                (x for x in history if x["id"] == editing_id),
                None
            )

            if item:
                item["title"] = title
                item["body"] = body
                item["updated_at"] = datetime.now(timezone.utc).isoformat()
                last_activity = f"Edited notification #{editing_id}"
                drafts.pop(user_id, None)
                send_text(chat_id, "✅ Notification ကို ပြင်ဆင်ပြီးပါပြီ။")
                return

        notification_id = next_id
        next_id += 1

        item = {
            "id": notification_id,
            "title": title,
            "body": body,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "status": "sending",
            "message_id": None,
            "error": None,
        }

        history.insert(0, item)
        current_job = {
            "id": notification_id,
            "status": "sending",
            "total": 1,
            "done": 0,
            "success": 0,
            "failed": 0,
            "cancel": False,
        }

        drafts.pop(user_id, None)
        last_activity = f"Sending notification #{notification_id}"

    send_text(
        chat_id,
        "🚀 Sending notification...\n"
        "Topic: all\n"
        "Progress: 0%"
    )

    try:
        message_id = send_fcm_notification(title, body)

        with lock:
            item["status"] = "sent"
            item["message_id"] = message_id

            current_job["done"] = 1
            current_job["success"] = 1
            current_job["status"] = "completed"

            last_activity = f"Sent notification #{notification_id}"

        send_text(
            chat_id,
            "✅ NOTIFICATION SENT\n\n"
            f"📌 Title: {title}\n"
            f"🆔 History ID: {notification_id}\n"
            f"📨 FCM Message ID: {message_id}\n"
            "👥 Target: Topic all\n"
            "📊 Progress: 100%"
        )

    except Exception as exc:
        logging.exception("FCM send failed")

        with lock:
            item["status"] = "failed"
            item["error"] = str(exc)

            current_job["done"] = 1
            current_job["failed"] = 1
            current_job["status"] = "failed"

            last_activity = f"Failed notification #{notification_id}"

        send_text(
            chat_id,
            "❌ NOTIFICATION FAILED\n\n"
            f"Error: {str(exc)[:800]}\n\n"
            "Firebase credentials နဲ့ Project ID ကို စစ်ပါ။"
        )


# ================= HISTORY =================

def show_history(chat_id):
    with lock:
        records = [dict(x) for x in history[:10]]

    if not records:
        send_text(chat_id, "📚 Notification History မရှိသေးပါ။")
        return

    for item in records:
        status = item["status"].upper()

        keyboard = [
            [
                {
                    "text": "✏️ Edit",
                    "callback_data": f"edit:{item['id']}",
                },
                {
                    "text": "🗑 Delete",
                    "callback_data": f"delete:{item['id']}",
                },
            ]
        ]

        send_text(
            chat_id,
            f"🆔 ID: {item['id']}\n"
            f"📌 Title: {item['title']}\n"
            f"💬 Message: {item['body']}\n"
            f"📊 Status: {status}\n"
            f"🕒 Created: {item['created_at']}",
            keyboard
        )


def delete_history_item(chat_id, notification_id):
    with lock:
        item = next(
            (x for x in history if x["id"] == notification_id),
            None
        )

        if not item:
            send_text(chat_id, "❌ Notification မတွေ့ပါ။")
            return

        if item["status"] == "sending":
            send_text(chat_id, "⚠️ ပို့နေဆဲ Notification ကို မဖျက်နိုင်ပါ။")
            return

        history.remove(item)

    send_text(
        chat_id,
        f"🗑 History #{notification_id} ကို ဖျက်ပြီးပါပြီ။\n"
        "သတိပြုရန်: ဖုန်းထဲရောက်ပြီးသား Notification ကို ဒီလုပ်ဆောင်ချက်က "
        "ပြန်မဖျက်နိုင်ပါ။"
    )


# ================= CALLBACKS =================

def handle_callback(query):
    data = query.get("data", "")
    chat_id = query["message"]["chat"]["id"]
    user_id = query["from"]["id"]

    answer_callback(query["id"])

    if data == "dashboard":
        dashboard(chat_id)
        return

    if data == "create":
        start_create(chat_id, user_id)
        return

    if data == "history":
        show_history(chat_id)
        return

    if data == "cancel":
        with lock:
            drafts.pop(user_id, None)
        home(chat_id, "❌ Current draft cancelled.")
        return

    if data == "confirm":
        with lock:
            draft = drafts.get(user_id)

            if not draft or draft.get("step") != "preview":
                send_text(chat_id, "Draft မရှိပါ။ ပြန်စပါ။")
                return

        threading.Thread(
            target=send_job,
            args=(chat_id, user_id),
            daemon=True
        ).start()
        return

    if data == "edit_title":
        with lock:
            if user_id not in drafts:
                send_text(chat_id, "Draft မရှိပါ။")
                return
            drafts[user_id]["step"] = "edit_title"

        send_text(chat_id, "Title အသစ်ပို့ပါ။")
        return

    if data == "edit_body":
        with lock:
            if user_id not in drafts:
                send_text(chat_id, "Draft မရှိပါ။")
                return
            drafts[user_id]["step"] = "edit_body"

        send_text(chat_id, "Message အသစ်ပို့ပါ။")
        return

    if data.startswith("edit:"):
        try:
            notification_id = int(data.split(":")[1])
            start_edit(chat_id, user_id, notification_id)
        except ValueError:
            send_text(chat_id, "ID မှားနေပါတယ်။")
        return

    if data.startswith("delete:"):
        try:
            notification_id = int(data.split(":")[1])
            delete_history_item(chat_id, notification_id)
        except ValueError:
            send_text(chat_id, "ID မှားနေပါတယ်။")
        return


# ================= UPDATE HANDLER =================

def process_update(update):
    if not is_admin(update):
        message = update.get("message", {})
        query = update.get("callback_query", {})
        chat = message.get("chat", {}).get("id")

        if chat:
            send_text(chat, "⛔ Access denied.")
        elif query:
            answer_callback(query["id"], "Access denied")
        return

    if "callback_query" in update:
        handle_callback(update["callback_query"])
        return

    message = update.get("message")

    if not message or "text" not in message:
        return

    chat_id = message["chat"]["id"]
    user_id = message["from"]["id"]
    text = message["text"].strip()

    if text == "/start":
        home(chat_id, "🤖 KMT Notification Admin Bot")
        return

    if text == "/cancel":
        with lock:
            drafts.pop(user_id, None)
        home(chat_id, "❌ Cancelled.")
        return

    if text == "/dashboard":
        dashboard(chat_id)
        return

    if text == "/history":
        show_history(chat_id)
        return

    if handle_draft_message(chat_id, user_id, text):
        return

    home(chat_id, "Menu ကနေ ရွေးချယ်ပါ။")


# ================= TELEGRAM POLLING =================

def polling_loop():
    global offset

    if not BOT_TOKEN:
        logging.error("TELEGRAM_BOT_TOKEN missing; polling disabled")
        return

    while True:
        try:
            updates = tg("getUpdates", {
                "offset": offset,
                "timeout": 25,
                "allowed_updates": ["message", "callback_query"],
            })

            for update in updates:
                offset = max(offset, update["update_id"] + 1)

                try:
                    process_update(update)
                except Exception:
                    logging.exception("Update processing failed")

        except Exception:
            logging.exception("Telegram polling error")
            time.sleep(5)


# ================= RENDER HEALTH =================

@app.route("/")
def index():
    return jsonify({
        "service": "KMT Telegram FCM Notification Bot",
        "status": "running",
        "firebase_ready": firebase_ready,
    })


@app.route("/health")
def health():
    return jsonify({"status": "ok"})


if __name__ == "__main__":
    threading.Thread(
        target=polling_loop,
        daemon=True
    ).start()

    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 10000)))
