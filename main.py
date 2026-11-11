import os
import logging
import threading
from datetime import datetime, timezone

import requests
import firebase_admin
from firebase_admin import credentials, messaging
from flask import Flask, jsonify, request

# ================= CONFIG =================

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
ADMIN_ID = str(os.getenv("TELEGRAM_ADMIN_ID", ""))

SUPABASE_URL = os.getenv("SUPABASE_URL", "").rstrip("/")
SUPABASE_KEY = os.getenv("SUPABASE_KEY", "")

FIREBASE_PROJECT_ID = os.getenv("FIREBASE_PROJECT_ID", "")
FIREBASE_CLIENT_EMAIL = os.getenv("FIREBASE_CLIENT_EMAIL", "")
FIREBASE_PRIVATE_KEY = os.getenv("FIREBASE_PRIVATE_KEY", "").replace("\\n", "\n")

WEBHOOK_URL = os.getenv("WEBHOOK_URL", "").rstrip("/")
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "")

TELEGRAM_API = f"https://api.telegram.org/bot{BOT_TOKEN}"
SUPABASE_TABLE = f"{SUPABASE_URL}/rest/v1/notifications"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s"
)

app = Flask(__name__)

user_states = {}
drafts = {}

# Prevent concurrent processing of duplicate update IDs.
processed_updates = set()
processed_lock = threading.Lock()

# ================= FIREBASE =================

def init_firebase():
    if firebase_admin._apps:
        return

    if not all([
        FIREBASE_PROJECT_ID,
        FIREBASE_CLIENT_EMAIL,
        FIREBASE_PRIVATE_KEY
    ]):
        raise RuntimeError("Firebase environment variables are missing")

    service_account = {
        "type": "service_account",
        "project_id": FIREBASE_PROJECT_ID,
        "private_key": FIREBASE_PRIVATE_KEY,
        "client_email": FIREBASE_CLIENT_EMAIL,
        "token_uri": "https://oauth2.googleapis.com/token"
    }

    firebase_admin.initialize_app(
        credentials.Certificate(service_account)
    )


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
        raise RuntimeError("Supabase did not return inserted record")

    return result[0]


def update_notification(notification_id, data):
    payload = dict(data)
    payload["updated_at"] = datetime.now(timezone.utc).isoformat()

    return supabase_request(
        "PATCH",
        params={"id": f"eq.{notification_id}"},
        data=payload
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
    response.raise_for_status()

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
    try:
        telegram("answerCallbackQuery", {
            "callback_query_id": callback_id,
            "text": text
        })
    except Exception:
        logging.exception("Callback answer failed")


def is_admin(update):
    user = None

    if "message" in update:
        user = update["message"].get("from", {})
    elif "callback_query" in update:
        user = update["callback_query"].get("from", {})

    return bool(
        ADMIN_ID
        and user
        and str(user.get("id", "")) == ADMIN_ID
    )


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
            f"📦 Total (latest 100): {len(records)}\n"
            f"✅ Sent: {sent}\n"
            f"❌ Failed: {failed}\n\n"
            "Select an action below."
        )
    except Exception:
        logging.exception("Dashboard error")
        text = "⚠️ Could not load Supabase dashboard."

    send_message(chat_id, text, main_keyboard())


# ================= CREATE NOTIFICATION =================

def start_create(chat_id):
    user_states[chat_id] = {"step": "title"}

    send_message(
        chat_id,
        "📝 Enter notification title:\n\nSend /cancel to cancel."
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
            "inline_keyboard": [[
                {"text": "✅ Confirm & Send", "callback_data": "confirm_send"},
                {"text": "❌ Cancel", "callback_data": "cancel"}
            ]]
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
    notification_id = None

    send_message(chat_id, "⏳ Saving notification and sending...")

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
                logging.exception("Failed to update status")

        send_message(
            chat_id,
            f"❌ Sending failed:\n{str(error)[:1200]}",
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
            notification_id = item.get("id")

            text = (
                f"📌 {item.get('title', '')}\n\n"
                f"✉️ {item.get('message', '')}\n\n"
                f"🆔 ID: {notification_id}\n"
                f"📊 Status: {item.get('status', 'unknown')}\n"
                f"🕒 Created: {item.get('created_at', '')}"
            )

            keyboard = {
                "inline_keyboard": [[
                    {
                        "text": "✏️ Edit",
                        "callback_data": f"edit:{notification_id}"
                    },
                    {
                        "text": "🗑 Delete",
                        "callback_data": f"delete:{notification_id}"
                    }
                ]]
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

    answer_callback(callback["id"])

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
        send_message(chat_id, "❌ Operation cancelled.", main_keyboard())

    elif data.startswith("edit:"):
        notification_id = data.split(":", 1)[1]

        try:
            records = supabase_request(
                "GET",
                params={
                    "id": f"eq.{notification_id}",
                    "select": "*"
                }
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
            "inline_keyboard": [[
                {
                    "text": "✅ Yes, Delete",
                    "callback_data": f"delete_confirm:{notification_id}"
                },
                {
                    "text": "❌ Cancel",
                    "callback_data": "cancel"
                }
            ]]
        }

        send_message(
            chat_id,
            f"⚠️ Delete notification history ID {notification_id}?",
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
        logging.warning("Ignored update from non-admin user")
        return

    if "callback_query" in update:
        process_callback(update["callback_query"])
        return

    if "message" not in update:
        return

    message = update["message"]
    chat_id = message["chat"]["id"]
    text = message.get("text", "")

    if text.split("@")[0] in ("/start", "/menu"):
        user_states.pop(chat_id, None)
        drafts.pop(chat_id, None)
        show_dashboard(chat_id)

    elif text.split("@")[0] == "/cancel":
        user_states.pop(chat_id, None)
        drafts.pop(chat_id, None)
        send_message(chat_id, "❌ Cancelled.", main_keyboard())

    elif text.startswith("/"):
        send_message(chat_id, "Use /start to open dashboard.")

    else:
        process_text(message)


# ================= WEBHOOK =================

@app.route("/telegram-webhook", methods=["POST"])
def telegram_webhook():
    if not WEBHOOK_SECRET:
        return jsonify({"error": "Webhook secret is not configured"}), 503

    supplied_secret = request.headers.get(
        "X-Telegram-Bot-Api-Secret-Token", ""
    )

    if supplied_secret != WEBHOOK_SECRET:
        return jsonify({"error": "Unauthorized"}), 401

    update = request.get_json(silent=True)

    if not isinstance(update, dict):
        return jsonify({"ok": False, "error": "Invalid update"}), 400

    update_id = update.get("update_id")

    if update_id is not None:
        with processed_lock:
            if update_id in processed_updates:
                return jsonify({"ok": True, "duplicate": True})

            processed_updates.add(update_id)

            # Keep the in-memory duplicate cache bounded.
            if len(processed_updates) > 2000:
                processed_updates.clear()
                processed_updates.add(update_id)

    try:
        process_update(update)
    except Exception:
        logging.exception("Webhook update processing failed")

        # Telegram can retry the update if it receives a non-2xx response.
        return jsonify({"ok": False}), 500

    return jsonify({"ok": True})


# ================= REGISTER WEBHOOK =================

def setup_webhook():
    if not BOT_TOKEN or not WEBHOOK_URL or not WEBHOOK_SECRET:
        logging.error(
            "Missing TELEGRAM_BOT_TOKEN, WEBHOOK_URL or WEBHOOK_SECRET"
        )
        return

    try:
        result = telegram("setWebhook", {
            "url": WEBHOOK_URL + "/telegram-webhook",
            "secret_token": WEBHOOK_SECRET,
            "allowed_updates": ["message", "callback_query"],
            "drop_pending_updates": False
        })

        logging.info("Webhook registration result: %s", result)

    except Exception:
        logging.exception("Webhook registration failed")


# ================= FLASK ROUTES =================

@app.route("/")
def home():
    return jsonify({
        "status": "live",
        "service": "KMT FCM Notification Bot",
        "mode": "webhook"
    })


@app.route("/health")
def health():
    return jsonify({"status": "ok"})


# Register webhook when Gunicorn imports this module.
# Use one Gunicorn worker so process-local state is shared.
if BOT_TOKEN and WEBHOOK_URL and WEBHOOK_SECRET:
    setup_webhook()
else:
    logging.error("Webhook environment variables are missing")
