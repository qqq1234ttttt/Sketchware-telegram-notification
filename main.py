
import os
import hmac
import logging
import threading
from datetime import datetime, timezone

import requests
import firebase_admin
from firebase_admin import credentials, messaging, db
from flask import Flask, jsonify, request, abort

# ================ CONFIG ================

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
ADMIN_ID = os.getenv("TELEGRAM_ADMIN_ID", "").strip()

SUPABASE_URL = os.getenv("SUPABASE_URL", "").rstrip("/")
SUPABASE_KEY = os.getenv("SUPABASE_KEY", "").strip()
SUPABASE_TABLE_NAME = os.getenv(
    "SUPABASE_TABLE_NAME", "notifications"
).strip()

FIREBASE_PROJECT_ID = os.getenv("FIREBASE_PROJECT_ID", "").strip()
FIREBASE_CLIENT_EMAIL = os.getenv(
    "FIREBASE_CLIENT_EMAIL", ""
).strip()
FIREBASE_PRIVATE_KEY = os.getenv(
    "FIREBASE_PRIVATE_KEY", ""
).replace("\\n", "\n").strip()

FIREBASE_DB_URL = os.getenv(
    "FIREBASE_DB_URL",
    "https://kmt-all-software-bc807-default-rtdb.asia-southeast1.firebasedatabase.app"
).rstrip("/")

WEBHOOK_URL = os.getenv("WEBHOOK_URL", "").rstrip("/")
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "").strip()

TELEGRAM_API = (
    f"https://api.telegram.org/bot{BOT_TOKEN}"
    if BOT_TOKEN else ""
)

SUPABASE_TABLE = (
    f"{SUPABASE_URL}/rest/v1/{SUPABASE_TABLE_NAME}"
    if SUPABASE_URL else ""
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s"
)

app = Flask(__name__)

user_states = {}
drafts = {}
state_lock = threading.RLock()

# Prevent repeated processing of recently received Telegram updates.
processed_updates = set()
processed_lock = threading.Lock()
MAX_PROCESSED_UPDATES = 5000


# ================ TELEGRAM API ================

def telegram(method, data=None):
    if not BOT_TOKEN:
        raise RuntimeError("TELEGRAM_BOT_TOKEN is missing")

    response = requests.post(
        f"{TELEGRAM_API}/{method}",
        json=data or {},
        timeout=25
    )

    try:
        result = response.json()
    except ValueError:
        logging.error(
            "Telegram returned invalid JSON, HTTP %s",
            response.status_code
        )
        response.raise_for_status()
        raise RuntimeError("Invalid Telegram API response")

    if not response.ok or not result.get("ok"):
        code = result.get("error_code", response.status_code)
        description = result.get(
            "description", "Unknown Telegram error"
        )
        logging.error(
            "Telegram API %s failed: %s: %s",
            method, code, description
        )
        raise RuntimeError(
            f"Telegram {method} failed ({code}): {description}"
        )

    return result


def send_message(chat_id, text, keyboard=None):
    data = {
        "chat_id": chat_id,
        "text": str(text)[:4096]
    }
    if keyboard:
        data["reply_markup"] = keyboard
    return telegram("sendMessage", data)


def answer_callback(callback_id, text=""):
    try:
        telegram("answerCallbackQuery", {
            "callback_query_id": callback_id,
            "text": text[:200]
        })
    except Exception:
        logging.exception("Callback answer failed")


# ================ FIREBASE ================

def init_firebase():
    if firebase_admin._apps:
        return

    required = {
        "FIREBASE_PROJECT_ID": FIREBASE_PROJECT_ID,
        "FIREBASE_CLIENT_EMAIL": FIREBASE_CLIENT_EMAIL,
        "FIREBASE_PRIVATE_KEY": FIREBASE_PRIVATE_KEY
    }

    missing = [key for key, value in required.items() if not value]
    if missing:
        raise RuntimeError(
            "Missing Firebase environment variables: "
            + ", ".join(missing)
        )

    service_account = {
        "type": "service_account",
        "project_id": FIREBASE_PROJECT_ID,
        "private_key": FIREBASE_PRIVATE_KEY,
        "client_email": FIREBASE_CLIENT_EMAIL,
        "token_uri": "https://oauth2.googleapis.com/token"
    }

    firebase_admin.initialize_app(
        credentials.Certificate(service_account),
        {"databaseURL": FIREBASE_DB_URL}
    )


def get_device_stats():
    try:
        init_firebase()

        online_data = db.reference("online_users").get()
        total_data = db.reference("all_devices").get()

        online_count = (
            len(online_data)
            if isinstance(online_data, (dict, list))
            else 0
        )
        total_count = (
            len(total_data)
            if isinstance(total_data, (dict, list))
            else 0
        )

        return online_count, total_count

    except Exception:
        logging.exception("Firebase device stats failed")
        return 0, 0


def send_fcm_notification(title, body):
    init_firebase()

    message = messaging.Message(
        notification=messaging.Notification(
            title=title,
            body=body
        ),
        android=messaging.AndroidConfig(
            priority="high",
            notification=messaging.AndroidNotification(
                channel_id="fcm_default_channel",
                sound="default"
            )
        ),
        topic="all"
    )

    return messaging.send(message)


# ================ SUPABASE ================

def supabase_headers():
    if not SUPABASE_KEY:
        raise RuntimeError("SUPABASE_KEY is missing")

    return {
        "apikey": SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}",
        "Content-Type": "application/json",
        "Prefer": "return=representation"
    }


def supabase_request(method, params=None, data=None):
    if not SUPABASE_URL or not SUPABASE_KEY:
        raise RuntimeError(
            "SUPABASE_URL or SUPABASE_KEY is missing"
        )

    response = requests.request(
        method,
        SUPABASE_TABLE,
        headers=supabase_headers(),
        params=params,
        json=data,
        timeout=20
    )

    if not response.ok:
        # Avoid logging request headers or secret keys.
        logging.error(
            "Supabase request failed: HTTP %s; response=%s",
            response.status_code,
            response.text[:1000]
        )
        response.raise_for_status()

    if response.text.strip():
        return response.json()

    return []


def save_notification(title, body, status="sending"):
    rows = supabase_request(
        "POST",
        data={
            "title": title,
            "message": body,
            "status": status
        }
    )

    if not rows:
        raise RuntimeError(
            "Supabase did not return the inserted record"
        )

    return rows[0]


def update_notification(notification_id, values):
    payload = dict(values)
    payload["updated_at"] = datetime.now(
        timezone.utc
    ).isoformat()

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
            "limit": max(1, min(int(limit), 100))
        }
    )


def get_notification(notification_id):
    rows = supabase_request(
        "GET",
        params={
            "id": f"eq.{notification_id}",
            "select": "*",
            "limit": 1
        }
    )
    return rows[0] if rows else None


def delete_notification(notification_id):
    return supabase_request(
        "DELETE",
        params={"id": f"eq.{notification_id}"}
    )


# ================ ACCESS CONTROL ================

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
                {"text": "➕ Create", "callback_data": "create"}
            ],
            [
                {"text": "📚 History", "callback_data": "history"},
                {"text": "❌ Cancel", "callback_data": "cancel"}
            ]
        ]
    }


# ================ DASHBOARD ================

def show_dashboard(chat_id):
    try:
        records = get_notifications(100)

        sent = sum(
            1 for item in records
            if item.get("status") == "sent"
        )
        failed = sum(
            1 for item in records
            if item.get("status") == "failed"
        )
        pending = sum(
            1 for item in records
            if item.get("status") == "sending"
        )

        online_count, total_count = get_device_stats()

        text = (
            "📊 NOTIFICATION DASHBOARD\n\n"
            f"🟢 Online Devices: {online_count}\n"
            f"📱 Total Devices: {total_count}\n"
            "────────────────────\n"
            f"📦 Latest records: {len(records)}\n"
            f"✅ Sent: {sent}\n"
            f"❌ Failed: {failed}\n"
            f"⏳ Sending: {pending}\n\n"
            "Select an action below."
        )

    except Exception:
        logging.exception("Dashboard failed")
        text = (
            "⚠️ Could not load dashboard.\n"
            "Please check Render Logs and Supabase settings."
        )

    send_message(chat_id, text, main_keyboard())


# ================ CREATE NOTIFICATION ================

def start_create(chat_id):
    with state_lock:
        user_states[chat_id] = {"step": "title"}
        drafts.pop(chat_id, None)

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
        with state_lock:
            user_states.pop(chat_id, None)
            drafts.pop(chat_id, None)

        send_message(chat_id, "❌ Cancelled.", main_keyboard())
        return

    if text.startswith("/"):
        send_message(
            chat_id,
            "Use the menu buttons or send /cancel."
        )
        return

    action = None
    preview = None
    notification_id = None
    title = None

    with state_lock:
        state = user_states.get(chat_id)

        if not state:
            return

        step = state.get("step")

        if step == "title":
            drafts[chat_id] = {"title": text}
            user_states[chat_id] = {"step": "message"}
            action = "new_message"

        elif step == "message":
            draft = drafts.get(chat_id)

            if not draft:
                user_states.pop(chat_id, None)
                action = "missing_draft"
            else:
                draft["message"] = text
                user_states.pop(chat_id, None)
                preview = dict(draft)
                action = "preview"

        elif step == "edit_title":
            state["title"] = text
            state["step"] = "edit_message"
            action = "edit_message"

        elif step == "edit_message":
            notification_id = state["id"]
            title = state["title"]
            user_states.pop(chat_id, None)
            action = "save_edit"

    if action == "new_message":
        send_message(chat_id, "✉️ Enter notification message:")

    elif action == "missing_draft":
        send_message(
            chat_id,
            "⚠️ Draft expired. Please create again.",
            main_keyboard()
        )

    elif action == "preview":
        keyboard = {
            "inline_keyboard": [[
                {
                    "text": "✅ Confirm & Send",
                    "callback_data": "confirm_send"
                },
                {
                    "text": "❌ Cancel",
                    "callback_data": "cancel"
                }
            ]]
        }

        send_message(
            chat_id,
            "🔍 NOTIFICATION PREVIEW\n\n"
            f"📌 Title:\n{preview['title']}\n\n"
            f"✉️ Message:\n{preview['message']}\n\n"
            "Send to all users?",
            keyboard
        )

    elif action == "edit_message":
        send_message(chat_id, "✉️ Enter the new message:")

    elif action == "save_edit":
        try:
            item = get_notification(notification_id)

            if not item:
                send_message(
                    chat_id,
                    "❌ Notification not found.",
                    main_keyboard()
                )
                return

            update_notification(notification_id, {
                "title": title,
                "message": text
            })

            send_message(
                chat_id,
                "✅ Notification history updated.",
                main_keyboard()
            )

        except Exception:
            logging.exception("Edit failed")
            send_message(
                chat_id,
                "❌ Could not update history. Check Render Logs.",
                main_keyboard()
            )


# ================ SEND FCM ================

def confirm_send(chat_id):
    with state_lock:
        draft = drafts.pop(chat_id, None)

    if not draft or not draft.get("message"):
        send_message(
            chat_id,
            "⚠️ No draft found. Create a new notification.",
            main_keyboard()
        )
        return

    title = draft["title"]
    body = draft["message"]
    notification_id = None

    try:
        record = save_notification(title, body, "sending")
        notification_id = record["id"]

        fcm_id = send_fcm_notification(title, body)

        update_notification(notification_id, {
            "status": "sent",
            "fcm_message_id": fcm_id,
            "error": None
        })

        send_message(
            chat_id,
            "✅ NOTIFICATION SENT\n\n"
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
                logging.exception("Failed to update failed status")

        send_message(
            chat_id,
            "❌ Sending failed:\n"
            f"{str(error)[:800]}",
            main_keyboard()
        )


# ================ HISTORY ================

def show_history(chat_id):
    try:
        records = get_notifications(10)

        if not records:
            send_message(
                chat_id,
                "📚 No notification history.",
                main_keyboard()
            )
            return

        for item in records:
            notification_id = item.get("id")
            title = item.get("title", "")
            body = item.get("message", "")
            created = item.get("created_at", "")

            text = (
                f"📌 {title}\n\n"
                f"✉️ {body}\n\n"
                f"🆔 ID: {notification_id}\n"
                f"📊 Status: {item.get('status', 'unknown')}\n"
                f"🕒 Created: {created}"
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

            send_message(chat_id, text[:4000], keyboard)

        send_message(
            chat_id,
            "📚 End of history.",
            main_keyboard()
        )

    except Exception:
        logging.exception("History loading failed")
        send_message(
            chat_id,
            "❌ Could not load history. Check Render Logs.",
            main_keyboard()
        )


# ================ CALLBACK HANDLER ================

def process_callback(callback):
    callback_id = callback.get("id")
    message = callback.get("message") or {}
    chat = message.get("chat") or {}

    if not chat.get("id"):
        answer_callback(
            callback_id,
            "This action is not supported here."
        )
        return

    chat_id = chat["id"]
    data = callback.get("data", "")

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
        with state_lock:
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
            item = get_notification(notification_id)

            if not item:
                send_message(chat_id, "❌ Notification not found.")
                return

            with state_lock:
                user_states[chat_id] = {
                    "step": "edit_title",
                    "id": notification_id,
                    "title": ""
                }

            send_message(
                chat_id,
                "✏️ EDIT NOTIFICATION\n\n"
                f"Current title: {item.get('title', '')}\n\n"
                "Enter the new title:"
            )

        except Exception:
            logging.exception("Edit lookup failed")
            send_message(
                chat_id,
                "❌ Could not load notification."
            )

    elif data.startswith("delete_confirm:"):
        notification_id = data.split(":", 1)[1]

        try:
            item = get_notification(notification_id)

            if not item:
                send_message(chat_id, "❌ Notification not found.")
                return

            delete_notification(notification_id)

            send_message(
                chat_id,
                "🗑 History record deleted.",
                main_keyboard()
            )

        except Exception:
            logging.exception("Delete failed")
            send_message(
                chat_id,
                "❌ Could not delete history record.",
                main_keyboard()
            )

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
            f"⚠️ Delete history record ID {notification_id}?",
            keyboard
        )


# ================ UPDATE HANDLER ================

def process_update(update):
    if not is_admin(update):
        logging.warning("Ignored update from non-admin user")
        return

    if "callback_query" in update:
        process_callback(update["callback_query"])
        return

    message = update.get("message")

    if not message or not message.get("chat"):
        return

    text = message.get("text", "").strip()
    chat_id = message["chat"]["id"]
    command = text.split()[0].split("@")[0] if text else ""

    if command in ("/start", "/menu"):
        with state_lock:
            user_states.pop(chat_id, None)
            drafts.pop(chat_id, None)

        show_dashboard(chat_id)

    elif command == "/cancel":
        with state_lock:
            user_states.pop(chat_id, None)
            drafts.pop(chat_id, None)

        send_message(
            chat_id,
            "❌ Cancelled.",
            main_keyboard()
        )

    else:
        process_text(message)


# ================ WEBHOOK SETUP ================

def setup_webhook():
    if not BOT_TOKEN:
        logging.error(
            "Webhook not configured: TELEGRAM_BOT_TOKEN is missing"
        )
        return False

    if not WEBHOOK_URL:
        logging.error(
            "Webhook not configured: WEBHOOK_URL is missing"
        )
        return False

    if WEBHOOK_SECRET and not (
        1 <= len(WEBHOOK_SECRET) <= 256
        and all(
            c.isalnum() or c in "_-"
            for c in WEBHOOK_SECRET
        )
    ):
        logging.error(
            "WEBHOOK_SECRET must contain only letters, numbers, _ or -"
        )
        return False

    webhook = f"{WEBHOOK_URL}/telegram-webhook"

    payload = {
        "url": webhook,
        "allowed_updates": ["message", "callback_query"],
        "drop_pending_updates": False
    }

    if WEBHOOK_SECRET:
        payload["secret_token"] = WEBHOOK_SECRET

    try:
        result = telegram("setWebhook", payload)

        logging.info(
            "Webhook registration successful: %s",
            result.get("description", "OK")
        )

        info = telegram("getWebhookInfo").get("result", {})

        logging.info(
            "Webhook info: url=%s, pending=%s, last_error=%s",
            info.get("url", ""),
            info.get("pending_update_count", 0),
            info.get("last_error_message", "")
        )

        return True

    except Exception:
        logging.exception("Webhook registration failed")
        return False


# ================ FLASK ROUTES ================

@app.route("/", methods=["GET"])
def home():
    return jsonify({
        "status": "live",
        "service": "KMT FCM Notification Bot",
        "mode": "webhook"
    })


@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok"})


@app.route("/telegram-webhook", methods=["POST"])
def telegram_webhook():
    if WEBHOOK_SECRET:
        supplied_secret = request.headers.get(
            "X-Telegram-Bot-Api-Secret-Token", ""
        )

        if not hmac.compare_digest(
            supplied_secret, WEBHOOK_SECRET
        ):
            abort(403)

    update = request.get_json(silent=True)

    if not isinstance(update, dict):
        return jsonify({
            "ok": False,
            "error": "Invalid JSON update"
        }), 400

    update_id = update.get("update_id")

    if update_id is not None:
        with processed_lock:
            if update_id in processed_updates:
                return jsonify({"ok": True, "duplicate": True})

            processed_updates.add(update_id)

            if len(processed_updates) > MAX_PROCESSED_UPDATES:
                processed_updates.clear()
                processed_updates.add(update_id)

    try:
        process_update(update)
        return jsonify({"ok": True})

    except Exception:
        logging.exception("Failed to process Telegram update")

        # Remove the ID so Telegram can retry this update.
        if update_id is not None:
            with processed_lock:
                processed_updates.discard(update_id)

        return jsonify({
            "ok": False,
            "error": "Update processing failed"
        }), 500


# Register the webhook when Gunicorn imports this module.
setup_webhook()


if __name__ == "__main__":
    app.run(
        host="0.0.0.0",
        port=int(os.getenv("PORT", "10000"))
    )
