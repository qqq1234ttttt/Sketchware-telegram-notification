
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

BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
ADMIN_ID = os.environ.get("TELEGRAM_ADMIN_ID", "").strip()
PROJECT_ID = os.environ.get("FIREBASE_PROJECT_ID", "").strip()
CLIENT_EMAIL = os.environ.get("FIREBASE_CLIENT_EMAIL", "").strip()
PRIVATE_KEY = os.environ.get("FIREBASE_PRIVATE_KEY", "").replace("\\n", "\n").strip()

API = f"https://api.telegram.org/bot{BOT_TOKEN}"
app = Flask(__name__)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s"
)

# ================= STATE =================

lock = threading.RLock()
offset = 0
drafts = {}
history = []
current_job = None
next_id = 1
last_activity = "Bot starting"
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

        if not firebase_admin._apps:
            firebase_admin.initialize_app(cred)

        firebase_ready = True
        logging.info("Firebase initialized successfully")
    else:
        logging.warning("Firebase environment variables are missing")

except Exception:
    logging.exception("Firebase initialization failed")

# ================= TELEGRAM API =================

def tg(method, data=None):
    if not BOT_TOKEN:
        raise RuntimeError("TELEGRAM_BOT_TOKEN is missing")

    response = requests.post(
        f"{API}/{method}",
        json=data or {},
        timeout=35
    )
    response.raise_for_status()

    result = response.json()

    if not result.get("ok"):
        raise RuntimeError(
            result.get("description", "Telegram API error")
        )

    return result["result"]


def send_text(chat_id, text, keyboard=None):
    data = {
        "chat_id": chat_id,
        "text": str(text)[:4000],
    }

    if keyboard:
        data["reply_markup"] = {
            "inline_keyboard": keyboard
        }

    return tg("sendMessage", data)


def answer_callback(callback_id, text=""):
    try:
        tg("answerCallbackQuery", {
            "callback_query_id": callback_id,
            "text": text[:180],
        })
    except Exception:
        logging.exception("Callback response failed")


# ================= ADMIN SECURITY =================

def is_admin(update):
    user = (
        update.get("message", {}).get("from")
        or update.get("callback_query", {}).get("from")
        or {}
    )

    return (
        bool(ADMIN_ID)
        and str(user.get("id", "")) == ADMIN_ID
    )


# ================= MENU =================

def main_menu():
    return [
        [
            {
                "text": "📊 Dashboard",
                "callback_data": "dashboard"
            },
            {
                "text": "📝 Create Notification",
                "callback_data": "create"
            },
        ],
        [
            {
                "text": "📚 Notification History",
                "callback_data": "history"
            },
            {
                "text": "❌ Cancel",
                "callback_data": "cancel"
            },
        ],
    ]


def home(chat_id, text="KMT Notification Admin"):
    send_text(chat_id, text, main_menu())


# ================= DASHBOARD =================

def dashboard(chat_id):
    with lock:
        count = len(history)
        job = dict(current_job) if current_job else None
        activity = last_activity

    firebase_status = (
        "✅ Connected" if firebase_ready
        else "❌ Not configured"
    )

    if job:
        job_text = (
            f"Status: {job['status']}\n"
            f"Progress: {job['done']}/{job['total']}\n"
            f"Success: {job['success']}\n"
            f"Failed: {job['failed']}"
        )
    else:
        job_text = "No sending job yet"

    text = (
        "📊 ADMIN DASHBOARD\n\n"
        f"🤖 Telegram token: "
        f"{'✅ Configured' if BOT_TOKEN else '❌ Missing'}\n"
        f"🔐 Admin ID: "
        f"{'✅ Configured' if ADMIN_ID else '❌ Missing'}\n"
        f"🔥 Firebase: {firebase_status}\n"
        f"📚 History records: {count}\n"
        f"⏱ Uptime: {int(time.time() - started_at)} seconds\n"
        f"📌 Last activity: {activity}\n\n"
        f"🚀 Sending status:\n{job_text}\n\n"
        "မှတ်ချက်: Progress သည် FCM API ပို့မှုအခြေအနေဖြစ်ပြီး "
        "ဖုန်းတိုင်းလက်ခံရရှိမှုကို မဆိုလိုပါ။"
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
        "ပယ်ဖျက်ရန် /cancel ပို့ပါ။"
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

        title = item["title"]

    send_text(
        chat_id,
        "✏️ EDIT HISTORY RECORD\n\n"
        f"လက်ရှိ Title: {title}\n"
        "Title အသစ်ပို့ပါ။ မပြောင်းလိုလျှင် /skip ပို့ပါ။"
    )


def show_preview(chat_id, user_id):
    with lock:
        draft = drafts.get(user_id)

        if not draft:
            send_text(chat_id, "❌ Draft မရှိပါ။ /start နှိပ်ပါ။")
            return

        title = draft["title"]
        body = draft["body"]
        is_edit = draft.get("editing_id") is not None

    confirm_label = (
        "💾 Save Edit" if is_edit
        else "✅ Confirm & Send"
    )

    keyboard = [
        [
            {
                "text": confirm_label,
                "callback_data": "confirm"
            }
        ],
        [
            {
                "text": "✏️ Edit Title",
                "callback_data": "edit_title"
            },
            {
                "text": "✏️ Edit Message",
                "callback_data": "edit_body"
            },
        ],
        [
            {
                "text": "❌ Cancel",
                "callback_data": "cancel"
            }
        ],
    ]

    send_text(
        chat_id,
        "👁 NOTIFICATION PREVIEW\n\n"
        f"📌 Title: {title}\n\n"
        f"💬 Message:\n{body}\n\n"
        + (
            "မှတ်တမ်းကိုသာ ပြင်ဆင်ပါမည်။ ဖုန်းများသို့ ထပ်မပို့ပါ။"
            if is_edit
            else "မှန်ကန်ပါက Confirm & Send နှိပ်ပါ။"
        ),
        keyboard
    )


def handle_draft_message(chat_id, user_id, text):
    with lock:
        draft = drafts.get(user_id)

        if not draft:
            return False

        step = draft["step"]

    if text == "/cancel":
        with lock:
            drafts.pop(user_id, None)

        home(chat_id, "❌ Draft cancelled.")
        return True

    if text == "/skip":
        if step == "edit_title":
            with lock:
                drafts[user_id]["step"] = "edit_body"

            send_text(
                chat_id,
                "Message အသစ်ပို့ပါ။ မပြောင်းလိုလျှင် /skip ပို့ပါ။"
            )
            return True

        if step == "edit_body":
            show_preview(chat_id, user_id)
            return True

    if step in ("title", "edit_title"):
        if not text.strip():
            send_text(chat_id, "❌ Title အလွတ်မဖြစ်ရပါ။")
            return True

        if len(text) > 100:
            send_text(chat_id, "Title ကို စာလုံး 100 ထက် မကျော်ပါနဲ့။")
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
            send_text(chat_id, "❌ Message အလွတ်မဖြစ်ရပါ။")
            return True

        if len(text) > 3500:
            send_text(chat_id, "Message အလွန်ရှည်နေပါတယ်။ တိုအောင်ပြင်ပါ။")
            return True

        with lock:
            drafts[user_id]["body"] = text
            drafts[user_id]["step"] = "preview"

        show_preview(chat_id, user_id)
        return True

    return step == "preview"


# ================= FIREBASE FCM =================

def send_fcm_notification(title, body):
    if not firebase_ready:
        raise RuntimeError(
            "Firebase is not configured. Check Render environment variables."
        )

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


# ================= SEND / SAVE =================

def send_job(chat_id, user_id):
    global current_job, next_id, last_activity

    with lock:
        draft = drafts.get(user_id)

        if not draft:
            send_text(chat_id, "❌ Draft မရှိပါ။")
            return

        if current_job and current_job["status"] == "sending":
            send_text(chat_id, "⚠️ Notification ပို့နေဆဲပါ။")
            return

        title = draft["title"].strip()
        body = draft["body"].strip()
        editing_id = draft.get("editing_id")

        if not title or not body:
            send_text(chat_id, "❌ Title နဲ့ Message လိုအပ်ပါတယ်။")
            return

        if editing_id is not None:
            item = next(
                (x for x in history if x["id"] == editing_id),
                None
            )

            if not item:
                drafts.pop(user_id, None)
                send_text(chat_id, "❌ ပြင်ဆင်မည့် History မတွေ့ပါ။")
                return

            item["title"] = title
            item["body"] = body
            item["updated_at"] = datetime.now(
                timezone.utc
            ).isoformat()

            drafts.pop(user_id, None)
            last_activity = f"Edited history #{editing_id}"

            send_text(
                chat_id,
                f"✅ History #{editing_id} ပြင်ဆင်ပြီးပါပြီ။\n"
                "ဖုန်းများသို့ Notification အသစ် မပို့ထားပါ။"
            )
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
        }

        drafts.pop(user_id, None)
        last_activity = f"Sending notification #{notification_id}"

    try:
        send_text(
            chat_id,
            "🚀 Sending notification...\n"
            "👥 Topic: all\n"
            "📊 Progress: 0%"
        )
    except Exception:
        logging.exception("Could not send progress message")

    try:
        message_id = send_fcm_notification(title, body)

        with lock:
            item["status"] = "sent"
            item["message_id"] = message_id

            current_job.update({
                "done": 1,
                "success": 1,
                "failed": 0,
                "status": "completed",
            })

            last_activity = f"Sent notification #{notification_id}"

        send_text(
            chat_id,
            "✅ NOTIFICATION SENT\n\n"
            f"📌 Title: {title}\n"
            f"🆔 History ID: {notification_id}\n"
            f"📨 FCM Message ID: {message_id}\n"
            "👥 Target: Topic all\n"
            "📊 Progress: 100%\n\n"
            "မှတ်ချက်: FCM က လက်ခံလိုက်ခြင်းဖြစ်ပြီး "
            "ဖုန်းတိုင်းရောက်ရှိကြောင်း အာမခံချက်မဟုတ်ပါ။",
            main_menu()
        )

    except Exception as exc:
        logging.exception("FCM send failed")

        with lock:
            item["status"] = "failed"
            item["error"] = str(exc)

            current_job.update({
                "done": 1,
                "success": 0,
                "failed": 1,
                "status": "failed",
            })

            last_activity = f"Failed notification #{notification_id}"

        send_text(
            chat_id,
            "❌ NOTIFICATION FAILED\n\n"
            f"Error: {str(exc)[:1500]}\n\n"
            "Render Environment Variables နဲ့ Firebase Service "
            "Account ကို စစ်ဆေးပါ။",
            main_menu()
        )


# ================= HISTORY =================

def show_history(chat_id):
    with lock:
        records = [dict(x) for x in history[:10]]

    if not records:
        send_text(chat_id, "📚 Notification History မရှိသေးပါ။")
        return

    for item in records:
        keyboard = [
            [
                {
                    "text": "✏️ Edit",
                    "callback_data": f"edit:{item['id']}"
                },
                {
                    "text": "🗑 Delete",
                    "callback_data": f"delete:{item['id']}"
                },
            ]
        ]

        send_text(
            chat_id,
            f"🆔 ID: {item['id']}\n"
            f"📌 Title: {item['title']}\n"
            f"💬 Message: {item['body']}\n"
            f"📊 Status: {item['status'].upper()}\n"
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
        f"🗑 History #{notification_id} ဖျက်ပြီးပါပြီ။\n"
        "ဖုန်းထဲရောက်ပြီးသား Notification ကိုတော့ "
        "ဒီလုပ်ဆောင်ချက်နဲ့ ပြန်ဖျက်လို့မရပါ။"
    )


# ================= CALLBACKS =================

def handle_callback(query):
    data = query.get("data", "")
    user_id = query.get("from", {}).get("id")
    message = query.get("message") or {}
    chat = message.get("chat") or {}
    chat_id = chat.get("id")

    answer_callback(query["id"])

    if chat_id is None or user_id is None:
        return

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
                send_text(chat_id, "❌ Draft မရှိပါ။ ပြန်စပါ။")
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
                send_text(chat_id, "❌ Draft မရှိပါ။")
                return

            drafts[user_id]["step"] = "edit_title"

        send_text(chat_id, "✏️ Title အသစ်ပို့ပါ။")
        return

    if data == "edit_body":
        with lock:
            if user_id not in drafts:
                send_text(chat_id, "❌ Draft မရှိပါ။")
                return

            drafts[user_id]["step"] = "edit_body"

        send_text(chat_id, "✏️ Message အသစ်ပို့ပါ။")
        return

    if data.startswith("edit:"):
        try:
            notification_id = int(data.split(":", 1)[1])
            start_edit(chat_id, user_id, notification_id)
        except (ValueError, IndexError):
            send_text(chat_id, "❌ ID မှားနေပါတယ်။")
        return

    if data.startswith("delete:"):
        try:
            notification_id = int(data.split(":", 1)[1])
            delete_history_item(chat_id, notification_id)
        except (ValueError, IndexError):
            send_text(chat_id, "❌ ID မှားနေပါတယ်။")
        return


# ================= UPDATE HANDLER =================

def process_update(update):
    if not is_admin(update):
        message = update.get("message", {})
        query = update.get("callback_query", {})

        chat_id = message.get("chat", {}).get("id")

        if chat_id is not None:
            send_text(chat_id, "⛔ Access denied.")
        elif query:
            answer_callback(query["id"], "Access denied")
        return

    global last_activity
    last_activity = "Telegram update received"

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
    global offset, last_activity

    if not BOT_TOKEN:
        logging.error("TELEGRAM_BOT_TOKEN is missing; polling stopped")
        return

    # Polling မစခင် Webhook အဟောင်းကို ဖယ်ရှားပါ။
    try:
        tg("deleteWebhook", {
            "drop_pending_updates": False
        })
        logging.info("Telegram webhook cleared; starting polling")
    except Exception:
        logging.exception("Could not clear Telegram webhook")

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
        "telegram_token_configured": bool(BOT_TOKEN),
        "admin_configured": bool(ADMIN_ID),
        "firebase_ready": firebase_ready,
    })


@app.route("/health")
def health():
    return jsonify({
        "status": "ok",
        "firebase_ready": firebase_ready,
    })


# Gunicorn imports main.py, so start polling at module load.
# Render တွင် Worker တစ်ခုတည်းသာ သုံးပါ။
polling_thread = threading.Thread(
    target=polling_loop,
    daemon=True,
    name="telegram-polling"
)
polling_thread.start()


if __name__ == "__main__":
    app.run(
        host="0.0.0.0",
        port=int(os.environ.get("PORT", "10000")),
        use_reloader=False
    )
