import os
import time
import json
import logging
import threading
from datetime import datetime, timezone

import requests
import firebase_admin
from firebase_admin import credentials, messaging
from flask import Flask, jsonify

# ================= CONFIG =================

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
ADMIN_ID = str(os.getenv("TELEGRAM_ADMIN_ID", ""))

SUPABASE_URL = os.getenv("SUPABASE_URL", "").rstrip("/")
SUPABASE_KEY = os.getenv("SUPABASE_KEY", "")

FIREBASE_PROJECT_ID = os.getenv("FIREBASE_PROJECT_ID", "")
FIREBASE_CLIENT_EMAIL = os.getenv("FIREBASE_CLIENT_EMAIL", "")
FIREBASE_PRIVATE_KEY = os.getenv("FIREBASE_PRIVATE_KEY", "").replace("\\n", "\n")

TELEGRAM_API = f"https://api.telegram.org/bot{BOT_TOKEN}"
SUPABASE_TABLE = f"{SUPABASE_URL}/rest/v1/notifications"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s"
)

app = Flask(__name__)

# ================= GLOBAL STATE =================

user_states = {}
drafts = {}
polling_started = False
polling_lock = threading.Lock()

# ================= FIREBASE =================

def init_firebase():
    if firebase_admin._apps:
        return

    required = [
        FIREBASE_PROJECT_ID,
        FIREBASE_CLIENT_EMAIL,
        FIREBASE_PRIVATE_KEY
    ]

    if not all(required):
        raise RuntimeError("Firebase environment variables are missing")

    service_account = {
        "type": "service_account",
        "project_id": FIREBASE_PROJECT_ID,
        "private_key": FIREBASE_PRIVATE_KEY,
        "client_email": FIREBASE_CLIENT_EMAIL,
        "token_uri": "https://oauth2.googleapis.com/token"
    }

    cred = credentials.Certificate(service_account)
    firebase_admin.initialize_app(cred)


def send_fcm_notification(title, message):
    init_firebase()

    notification = messaging.Message(
        notification=messaging.Notification(
            title=title,
            body=message
        ),
        topic="all"
    )

    return messaging.send(notification)


# ================= SUPABASE =================

def supabase_headers():
    return {
        "apikey": SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}",
        "Content-Type": "application/json",
        "Prefer": "return=representation"
    }


def supabase_request(method, params=None, data=None):
    if not SUPABASE_URL or not SUPABASE_KEY:
        raise RuntimeError("Supabase environment variables are missing")

    response = requests.request(
        method,
        SUPABASE_TABLE,
        headers=supabase_headers(),
        params=params,
        json=data,
        timeout=20
    )

    if not response.ok:
        logging.error(
            "Supabase error %s: %s",
            response.status_code,
            response.text
        )
        response.raise_for_status()

    if response.text.strip():
        return response.json()

    return []


def save_notification(title, message, status="sending"):
    result = supabase_request(
        "POST",
        data={
            "title": title,
            "message": message,
            "status": status
        }
    )

    if not result:
        raise RuntimeError("Supabase did not return the inserted record")

    return result[0]


def update_notification(notification_id, data):
    data["updated_at"] = datetime.now(timezone.utc).isoformat()

    return supabase_request(
        "PATCH",
        params={"id": f"eq.{notification_id}"},
        data=data
    )


def get_notifications(limit=10):
    return supabase_request(
        "GET",
        params={
            "select": "*",
            "order": "created_at.desc",
            "limit": limit
        }
    )


def delete_notification(notification_id):
    return supabase_request(
        "DELETE",
        params={"id": f"eq.{notification_id}"}
    )


# ================= TELEGRAM =================

def telegram(method, data=None):
    response = requests.post(
        f"{TELEGRAM_API}/{method}",
        json=data or {},
        timeout=35
    )

    result = response.json()

    if not result.get("ok"):
        logging.error("Telegram API error: %s", result)

    return result


def send_message(chat_id, text, keyboard=None):
    data = {
        "chat_id": chat_id,
        "text": text
    }

    if keyboard:
        data["reply_markup"] = keyboard

    return telegram("sendMessage", data)


def answer_callback(callback_id, text=""):
    telegram("answerCallbackQuery", {
        "callback_query_id": callback_id,
        "text": text
    })


def is_admin(update):
    user = None

    if "message" in update:
        user = update["message"].get("from", {})

    elif "callback_query" in update:
        user = update["callback_query"].get("from", {})

    if not user:
        return False

    return str(user.get("id", "")) == ADMIN_ID


def main_keyboard():
    return {
        "inline_keyboard": [
            [
                {"text": "📊 Dashboard", "callback_data": "dashboard"},
                {"text": "➕ Create Notification", "callback_data": "create"}
            ],
            [
                {"text": "📚 History", "callback_data": "history"},
                {"text": "❌ Cancel", "callback_data": "cancel"}
            ]
        ]
    }


def show_dashboard(chat_id):
    try:
        records = get_notifications(100)
        sent = sum(1 for x in records if x.get("status") == "sent")
        failed = sum(1 for x in records if x.get("status") == "failed")

        text = (
            "📊 NOTIFICATION DASHBOARD\n\n"
            f"📦 Total: {len(records)}\n"
            f"✅ Sent: {sent}\n"
            f"❌ Failed: {failed}\n\n"
            "Select an action below."
        )

    except Exception:
        logging.exception("Dashboard error")
        text = "⚠️ Supabase connection error."

    send_message(chat_id, text, main_keyboard())


# ================= CREATE NOTIFICATION =================

def start_create(chat_id):
    user_states[chat_id] = {"step": "title"}

    send_message(
        chat_id,
        "📝 Enter notification title:\n\n"
        "Send /cancel to cancel."
    )


def process_text(message):
    chat_id = message["chat"]["id"]
    text = message.get("text", "").strip()

    if not text:
        return

    if text == "/cancel":
        user_states.pop(chat_id, None)
        drafts.pop(chat_id, None)
        send_message(chat_id, "❌ Cancelled.", main_keyboard())
        return

    state = user_states.get(chat_id)

    if not state:
        return

    step = state.get("step")

    if step == "title":
        drafts[chat_id] = {"title": text}
        user_states[chat_id] = {"step": "message"}

        send_message(chat_id, "✉️ Enter notification message:")

    elif step == "message":
        drafts[chat_id]["message"] = text
        user_states.pop(chat_id, None)

        draft = drafts[chat_id]

        keyboard = {
            "inline_keyboard": [
                [
                    {"text": "✅ Confirm & Send", "callback_data": "confirm_send"},
                    {"text": "❌ Cancel", "callback_data": "cancel"}
                ]
            ]
        }

        send_message(
            chat_id,
            "🔍 NOTIFICATION PREVIEW\n\n"
            f"📌 Title:\n{draft['title']}\n\n"
            f"✉️ Message:\n{draft['message']}\n\n"
            "Send this notification to all users?",
            keyboard
        )

    elif step == "edit_title":
        state["title"] = text
        state["step"] = "edit_message"

        send_message(chat_id, "✉️ Enter the new message:")

    elif step == "edit_message":
        notification_id = state["id"]

        try:
            update_notification(notification_id, {
                "title": state["title"],
                "message": text
            })

            user_states.pop(chat_id, None)

            send_message(
                chat_id,
                "✅ Notification history updated.",
                main_keyboard()
            )

        except Exception:
            logging.exception("Edit error")
            send_message(chat_id, "❌ Could not update notification.")


def confirm_send(chat_id):
    draft = drafts.get(chat_id)

    if not draft:
        send_message(chat_id, "⚠️ No notification draft found.")
        return

    title = draft["title"]
    message = draft["message"]

    send_message(chat_id, "⏳ Saving notification and sending to users...")

    notification_id = None

    try:
        record = save_notification(title, message, "sending")
        notification_id = record["id"]

        fcm_id = send_fcm_notification(title, message)

        update_notification(notification_id, {
            "status": "sent",
            "fcm_message_id": fcm_id,
            "error": None
        })

        send_message(
            chat_id,
            "✅ NOTIFICATION SENT SUCCESSFULLY\n\n"
            f"📌 Title: {title}\n"
            f"🆔 Database ID: {notification_id}\n"
            f"🔥 FCM ID: {fcm_id}",
            main_keyboard()
        )

    except Exception as error:
        logging.exception("Notification sending failed")

        if notification_id is not None:
            try:
                update_notification(notification_id, {
                    "status": "failed",
                    "error": str(error)[:1000]
                })
            except Exception:
                logging.exception("Could not update failed status")

        send_message(
            chat_id,
            f"❌ Sending failed:\n{str(error)[:1500]}",
            main_keyboard()
        )

    finally:
        drafts.pop(chat_id, None)


# ================= HISTORY =================

def show_history(chat_id):
    try:
        records = get_notifications(10)

        if not records:
            send_message(chat_id, "📚 No notification history.", main_keyboard())
            return

        for item in records:
            status = item.get("status", "unknown")

            text = (
                f"📌 {item.get('title', '')}\n\n"
                f"✉️ {item.get('message', '')}\n\n"
                f"🆔 ID: {item.get('id')}\n"
                f"📊 Status: {status}\n"
                f"🕒 Created: {item.get('created_at', '')}"
            )

            notification_id = item.get("id")

            keyboard = {
                "inline_keyboard": [
                    [
                        {
                            "text": "✏️ Edit",
                            "callback_data": f"edit:{notification_id}"
                        },
                        {
                            "text": "🗑 Delete",
                            "callback_data": f"delete:{notification_id}"
                        }
                    ]
                ]
            }

            send_message(chat_id, text, keyboard)

        send_message(chat_id, "📚 End of history.", main_keyboard())

    except Exception:
        logging.exception("History error")
        send_message(chat_id, "❌ Could not load history.")


# ================= CALLBACK HANDLER =================

def process_callback(callback):
    chat_id = callback["message"]["chat"]["id"]
    data = callback.get("data", "")

    callback_id = callback["id"]

    answer_callback(callback_id)

    if data == "dashboard":
        show_dashboard(chat_id)

    elif data == "create":
        start_create(chat_id)

    elif data == "confirm_send":
        confirm_send(chat_id)

    elif data == "history":
        show_history(chat_id)

    elif data == "cancel":
        user_states.pop(chat_id, None)
        drafts.pop(chat_id, None)

        send_message(
            chat_id,
            "❌ Operation cancelled.",
            main_keyboard()
        )

    elif data.startswith("edit:"):
        notification_id = data.split(":", 1)[1]

        try:
            records = supabase_request(
                "GET",
                params={"id": f"eq.{notification_id}", "select": "*"}
            )

            if not records:
                send_message(chat_id, "❌ Notification not found.")
                return

            item = records[0]

            user_states[chat_id] = {
                "step": "edit_title",
                "id": notification_id,
                "title": ""
            }

            send_message(
                chat_id,
                "✏️ EDIT NOTIFICATION\n\n"
                f"Current title:\n{item.get('title', '')}\n\n"
                "Enter the new title:"
            )

        except Exception:
            logging.exception("Edit lookup error")
            send_message(chat_id, "❌ Could not load notification.")

    elif data.startswith("delete:"):
        notification_id = data.split(":", 1)[1]

        keyboard = {
            "inline_keyboard": [
                [
                    {
                        "text": "✅ Yes, Delete",
                        "callback_data": f"delete_confirm:{notification_id}"
                    },
                    {
                        "text": "❌ Cancel",
                        "callback_data": "cancel"
                    }
                ]
            ]
        }

        send_message(
            chat_id,
            f"⚠️ Delete notification ID {notification_id}?",
            keyboard
        )

    elif data.startswith("delete_confirm:"):
        notification_id = data.split(":", 1)[1]

        try:
            delete_notification(notification_id)

            send_message(
                chat_id,
                "🗑 Notification history deleted.",
                main_keyboard()
            )

        except Exception:
            logging.exception("Delete error")
            send_message(chat_id, "❌ Could not delete notification.")


# ================= UPDATE HANDLER =================

def process_update(update):
    if not is_admin(update):
        return

    if "callback_query" in update:
        process_callback(update["callback_query"])
        return

    if "message" not in update:
        return

    message = update["message"]
    chat_id = message["chat"]["id"]
    text = message.get("text", "")

    if text == "/start" or text == "/menu":
        user_states.pop(chat_id, None)
        drafts.pop(chat_id, None)

        show_dashboard(chat_id)

    elif text == "/cancel":
        user_states.pop(chat_id, None)
        drafts.pop(chat_id, None)
        send_message(chat_id, "❌ Cancelled.", main_keyboard())

    elif text.startswith("/"):
        send_message(chat_id, "Use /start to open dashboard.")

    else:
        process_text(message)


# ================= TELEGRAM POLLING =================

def telegram_polling():
    offset = None

    telegram("deleteWebhook", {"drop_pending_updates": False})

    logging.info("Telegram polling started")

    while True:
        try:
            params = {
                "timeout": 25,
                "allowed_updates": ["message", "callback_query"]
            }

            if offset is not None:
                params["offset"] = offset

            response = requests.get(
                f"{TELEGRAM_API}/getUpdates",
                params=params,
                timeout=35
            )

            result = response.json()

            if not result.get("ok"):
                logging.error("Polling error: %s", result)
                time.sleep(5)
                continue

            for update in result.get("result", []):
                offset = update["update_id"] + 1

                try:
                    process_update(update)
                except Exception:
                    logging.exception("Update processing error")

        except Exception:
            logging.exception("Telegram polling connection error")
            time.sleep(5)


def start_polling():
    global polling_started

    with polling_lock:
        if polling_started:
            return

        polling_started = True

    thread = threading.Thread(
        target=telegram_polling,
        daemon=True
    )
    thread.start()


# ================= FLASK =================

@app.route("/")
def home():
    return jsonify({
        "status": "live",
        "service": "KMT FCM Notification Bot"
    })


@app.route("/health")
def health():
    return jsonify({"status": "ok"})


# Start polling when Gunicorn imports this module
if BOT_TOKEN and ADMIN_ID:
    start_polling()
else:
    logging.error("Telegram bot environment variables are missing")
