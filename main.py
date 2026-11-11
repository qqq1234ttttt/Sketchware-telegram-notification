
import os
import time
import threading
import logging
from datetime import datetime, timezone

import requests
import firebase_admin
from firebase_admin import credentials, db, messaging
from flask import Flask, jsonify

app = Flask(__name__)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s"
)

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
ADMIN_ID = os.getenv("TELEGRAM_ADMIN_ID", "").strip()

FIREBASE_PROJECT_ID = os.getenv(
    "FIREBASE_PROJECT_ID", ""
).strip()

FIREBASE_CLIENT_EMAIL = os.getenv(
    "FIREBASE_CLIENT_EMAIL", ""
).strip()

FIREBASE_PRIVATE_KEY = os.getenv(
    "FIREBASE_PRIVATE_KEY", ""
).strip().replace("\\n", "\n")

FIREBASE_DATABASE_URL = os.getenv(
    "FIREBASE_DATABASE_URL", ""
).strip().rstrip("/")

if (
    len(FIREBASE_PRIVATE_KEY) >= 2
    and FIREBASE_PRIVATE_KEY[0] == '"'
    and FIREBASE_PRIVATE_KEY[-1] == '"'
):
    FIREBASE_PRIVATE_KEY = FIREBASE_PRIVATE_KEY[1:-1]

TELEGRAM_API = (
    f"https://api.telegram.org/bot{BOT_TOKEN}"
    if BOT_TOKEN else ""
)

firebase_app = None
firebase_lock = threading.Lock()

user_states = {}
sending_users = set()
sending_users_lock = threading.Lock()

polling_started = False
polling_lock = threading.Lock()


# ==================================================
# FIREBASE ADMIN SDK
# ==================================================

def get_firebase_app():
    global firebase_app

    if firebase_app is not None:
        return firebase_app

    with firebase_lock:
        if firebase_app is not None:
            return firebase_app

        required = {
            "FIREBASE_PROJECT_ID": FIREBASE_PROJECT_ID,
            "FIREBASE_CLIENT_EMAIL": FIREBASE_CLIENT_EMAIL,
            "FIREBASE_PRIVATE_KEY": FIREBASE_PRIVATE_KEY,
            "FIREBASE_DATABASE_URL": FIREBASE_DATABASE_URL,
        }

        missing = [
            name for name, value in required.items() if not value
        ]

        if missing:
            raise RuntimeError(
                "Render Environment မှာ မရှိသော Variable များ: "
                + ", ".join(missing)
            )

        if (
            "-----BEGIN PRIVATE KEY-----" not in FIREBASE_PRIVATE_KEY
            or "-----END PRIVATE KEY-----" not in FIREBASE_PRIVATE_KEY
        ):
            raise RuntimeError(
                "FIREBASE_PRIVATE_KEY ပုံစံမမှန်ပါ။"
            )

        service_account_info = {
            "type": "service_account",
            "project_id": FIREBASE_PROJECT_ID,
            "private_key": FIREBASE_PRIVATE_KEY,
            "client_email": FIREBASE_CLIENT_EMAIL,
            "token_uri": "https://oauth2.googleapis.com/token",
        }

        try:
            cred = credentials.Certificate(service_account_info)

            firebase_app = firebase_admin.initialize_app(
                cred,
                {
                    "projectId": FIREBASE_PROJECT_ID,
                    "databaseURL": FIREBASE_DATABASE_URL,
                },
            )

            logging.info("Firebase Admin SDK initialized")
            return firebase_app

        except Exception:
            logging.exception("Firebase initialization failed")
            raise


def database_reference(path):
    fb_app = get_firebase_app()
    return db.reference(path, app=fb_app)


def save_last_notification(title, message, fcm_name):
    record = {
        "title": title,
        "message": message,
        "sent_at": datetime.now(timezone.utc).isoformat(),
        "firebase_message_id": fcm_name or "",
    }

    database_reference("admin/last_notification").set(record)
    return record


def get_last_notification():
    return database_reference("admin/last_notification").get()


def delete_last_notification():
    database_reference("admin/last_notification").delete()


def format_last_notification(record):
    if not isinstance(record, dict) or not record:
        return "🔔 နောက်ဆုံး Noti မှတ်တမ်း မရှိသေးပါ။"

    return (
        "🔔 နောက်ဆုံးပို့ခဲ့သော Noti\n\n"
        f"📌 Title: {record.get('title', '')}\n"
        f"📝 Message: {record.get('message', '')}\n"
        f"🕒 ပို့ချိန် UTC: {record.get('sent_at', '')}"
    )


# ==================================================
# FIREBASE CLOUD MESSAGING
# ==================================================

def send_fcm_notification(title, message):
    fb_app = get_firebase_app()

    fcm_message = messaging.Message(
        topic="all",
        notification=messaging.Notification(
            title=title,
            body=message,
        ),
        android=messaging.AndroidConfig(
            priority="high",
            notification=messaging.AndroidNotification(
                channel_id="default",
                sound="default",
            ),
        ),
    )

    message_id = messaging.send(
        fcm_message,
        app=fb_app,
    )

    return {"name": message_id}


# ==================================================
# TELEGRAM HELPERS
# ==================================================

def is_admin(user_id):
    return (
        bool(ADMIN_ID)
        and user_id is not None
        and str(user_id) == ADMIN_ID
    )


def telegram(method, data=None, timeout=25):
    if not BOT_TOKEN:
        raise RuntimeError("TELEGRAM_BOT_TOKEN မရှိပါ။")

    response = requests.post(
        f"{TELEGRAM_API}/{method}",
        json=data or {},
        timeout=timeout,
    )

    try:
        result = response.json()
    except ValueError:
        response.raise_for_status()
        raise RuntimeError("Telegram response မမှန်ပါ။")

    if not response.ok or not result.get("ok"):
        raise RuntimeError(
            f"Telegram API Error {response.status_code}: "
            f"{result.get('description', response.text[:800])}"
        )

    return result.get("result")


def send_message(chat_id, text, keyboard=None):
    data = {
        "chat_id": chat_id,
        "text": str(text)[:4000],
    }

    if keyboard:
        data["reply_markup"] = keyboard

    return telegram("sendMessage", data)


def main_menu():
    return {
        "inline_keyboard": [
            [
                {"text": "📊 စနစ်အခြေအနေ", "callback_data": "status"},
                {"text": "➕ Noti ပို့ရန်", "callback_data": "create"},
            ],
            [
                {"text": "🔔 နောက်ဆုံး Noti", "callback_data": "last"},
            ],
            [
                {"text": "🗑️ မှတ်တမ်းဖျက်ရန်", "callback_data": "delete"},
            ],
            [
                {"text": "❌ ပယ်ဖျက်ရန်", "callback_data": "cancel"},
            ],
        ]
    }


def confirm_menu():
    return {
        "inline_keyboard": [
            [
                {"text": "✅ အခုပို့မယ်", "callback_data": "confirm_send"},
                {"text": "✏️ ပြန်ပြင်မယ်", "callback_data": "re_create"},
            ],
            [
                {"text": "❌ မပို့တော့ပါ", "callback_data": "cancel"},
            ],
        ]
    }


def delete_confirm_menu():
    return {
        "inline_keyboard": [
            [
                {
                    "text": "🗑️ ဖျက်ရန် အတည်ပြုမယ်",
                    "callback_data": "confirm_delete",
                }
            ],
            [
                {"text": "↩️ မဖျက်တော့ပါ", "callback_data": "cancel"},
            ],
        ]
    }


# ==================================================
# CALLBACK HANDLER
# ==================================================

def handle_callback(update):
    query = update.get("callback_query", {})
    if not query:
        return

    query_id = query.get("id")
    data = query.get("data", "")
    user_id = query.get("from", {}).get("id")
    chat_id = query.get("message", {}).get("chat", {}).get("id")

    try:
        telegram(
            "answerCallbackQuery",
            {"callback_query_id": query_id},
        )
    except Exception:
        logging.exception("Callback acknowledgement failed")

    if not is_admin(user_id):
        if chat_id:
            send_message(chat_id, "⛔ Admin သာ အသုံးပြုနိုင်ပါတယ်။")
        return

    if not chat_id:
        return

    user_key = str(user_id)

    try:
        if data == "status":
            try:
                record = get_last_notification()
                status_text = (
                    "🟢 Firebase Database ချိတ်ဆက်မှု အောင်မြင်"
                )
                last_text = format_last_notification(record)
            except Exception as exc:
                logging.exception("Database status failed")
                status_text = (
                    f"🔴 Database အမှား: {str(exc)[:500]}"
                )
                last_text = ""

            send_message(
                chat_id,
                "📊 စနစ်အခြေအနေ\n\n"
                "🟢 Telegram Backend အလုပ်လုပ်နေသည်\n"
                f"{status_text}\n\n{last_text}",
                main_menu(),
            )

        elif data == "last":
            send_message(
                chat_id,
                format_last_notification(get_last_notification()),
                main_menu(),
            )

        elif data == "delete":
            record = get_last_notification()

            if not isinstance(record, dict) or not record:
                send_message(
                    chat_id,
                    "ℹ️ ဖျက်ရန် မှတ်တမ်းမရှိပါ။",
                    main_menu(),
                )
                return

            user_states[user_key] = {"step": "delete_confirm"}

            send_message(
                chat_id,
                "⚠️ နောက်ဆုံး Noti မှတ်တမ်းကို ဖျက်မှာ သေချာပါသလား။\n\n"
                f"{format_last_notification(record)}\n\n"
                "Database မှတ်တမ်းကိုသာ ဖျက်ပါမယ်။ "
                "ဖုန်းမှာ ရောက်ပြီးသား Noti ကို ပြန်ဖျက်မှာ မဟုတ်ပါ။",
                delete_confirm_menu(),
            )

        elif data == "confirm_delete":
            state = user_states.get(user_key, {})

            if state.get("step") != "delete_confirm":
                send_message(
                    chat_id,
                    "ℹ️ ဖျက်ရန် အတည်ပြုချက်မရှိပါ။",
                    main_menu(),
                )
                return

            delete_last_notification()
            user_states.pop(user_key, None)

            send_message(
                chat_id,
                "🗑️ Noti မှတ်တမ်းကို ဖျက်ပြီးပါပြီ။",
                main_menu(),
            )

        elif data == "create":
            user_states[user_key] = {"step": "title"}
            send_message(
                chat_id,
                "➕ Noti အသစ်ပြုလုပ်ပါမယ်။\n\n"
                "Notification Title ကို ရိုက်ပို့ပါ။\n"
                "/cancel ဖြင့် ပယ်ဖျက်နိုင်ပါတယ်။",
            )

        elif data == "cancel":
            user_states.pop(user_key, None)
            send_message(
                chat_id,
                "❌ ပယ်ဖျက်ပြီးပါပြီ။",
                main_menu(),
            )

        elif data == "re_create":
            user_states[user_key] = {"step": "title"}
            send_message(
                chat_id,
                "✏️ Title အသစ်ကို ရိုက်ပို့ပါ။",
            )

        elif data == "confirm_send":
            with sending_users_lock:
                if user_key in sending_users:
                    return

                state = user_states.get(user_key)

                if not state or state.get("step") != "confirm":
                    state = None
                else:
                    sending_users.add(user_key)
                    user_states.pop(user_key, None)

            if not state:
                send_message(
                    chat_id,
                    "ℹ️ ပို့ရန်အချက်အလက် မရှိတော့ပါ။",
                    main_menu(),
                )
                return

            try:
                title = state["title"]
                body = state["message"]

                send_message(
                    chat_id,
                    "⏳ Notification ပို့နေပါသည်...",
                )

                result = send_fcm_notification(title, body)

                history_text = ""
                try:
                    save_last_notification(
                        title,
                        body,
                        result.get("name", ""),
                    )
                    history_text = "💾 Noti မှတ်တမ်း သိမ်းပြီးပါပြီ။"
                except Exception as exc:
                    logging.exception("History save failed")
                    history_text = (
                        "⚠️ FCM က ပို့ရန် လက်ခံပြီးပါပြီ၊ "
                        "သို့သော် Database မှတ်တမ်း သိမ်းမရပါ။\n"
                        f"{str(exc)[:500]}"
                    )

                send_message(
                    chat_id,
                    "🚀 FCM က Notification ကို ပို့ရန် လက်ခံပြီးပါပြီ။\n\n"
                    f"📌 Title: {title}\n"
                    f"📝 Message: {body}\n\n"
                    f"{history_text}\n\n"
                    "မှတ်ချက်: ဖုန်းအားလုံးသို့ ရောက်ရှိကြောင်း "
                    "အာမခံခြင်းမဟုတ်ပါ။",
                    main_menu(),
                )

            except Exception as exc:
                logging.exception("Notification send failed")
                send_message(
                    chat_id,
                    f"❌ Noti ပို့မရပါ။\n\n{str(exc)[:1200]}",
                    main_menu(),
                )

            finally:
                with sending_users_lock:
                    sending_users.discard(user_key)

    except Exception as exc:
        logging.exception("Callback handler error")
        try:
            send_message(
                chat_id,
                f"❌ အမှားအယွင်း: {str(exc)[:1000]}",
                main_menu(),
            )
        except Exception:
            logging.exception("Error reply failed")


# ==================================================
# MESSAGE HANDLER
# ==================================================

def handle_message(update):
    message = update.get("message", {})
    user_id = message.get("from", {}).get("id")
    chat_id = message.get("chat", {}).get("id")
    text = (message.get("text") or "").strip()

    if not is_admin(user_id) or not chat_id:
        return

    user_key = str(user_id)

    if text in ("/start", "/menu"):
        user_states.pop(user_key, None)
        send_message(
            chat_id,
            "⚙️ Admin Control Panel မှ ကြိုဆိုပါတယ်။",
            main_menu(),
        )
        return

    if text == "/cancel":
        user_states.pop(user_key, None)
        send_message(
            chat_id,
            "❌ ပယ်ဖျက်ပြီးပါပြီ။",
            main_menu(),
        )
        return

    state = user_states.get(user_key)
    if not state:
        return

    if not text:
        send_message(chat_id, "စာသားကို ရိုက်ပို့ပေးပါ။")
        return

    if state["step"] == "title":
        user_states[user_key] = {
            "step": "message",
            "title": text,
        }
        send_message(
            chat_id,
            f"📌 Title: {text}\n\n"
            "ယခု Notification Message ကို ရိုက်ပို့ပါ။",
        )
        return

    if state["step"] == "message":
        user_states[user_key] = {
            "step": "confirm",
            "title": state["title"],
            "message": text,
        }
        send_message(
            chat_id,
            "❓ ဒီအချက်အလက်တွေအတိုင်း ပို့မှာ သေချာပါသလား။\n\n"
            f"📌 Title: {state['title']}\n"
            f"📝 Message: {text}",
            confirm_menu(),
        )


# ==================================================
# TELEGRAM POLLING
# ==================================================

def telegram_polling():
    global polling_started

    with polling_lock:
        if polling_started:
            return
        polling_started = True

    offset = 0
    logging.info("Telegram polling started")

    while True:
        try:
            updates = telegram(
                "getUpdates",
                {
                    "offset": offset,
                    "timeout": 15,
                    "allowed_updates": ["message", "callback_query"],
                },
                timeout=25,
            )

            for update in updates:
                update_id = update.get("update_id")
                if update_id is None:
                    continue

                offset = max(offset, update_id + 1)

                try:
                    if "callback_query" in update:
                        handle_callback(update)
                    elif "message" in update:
                        handle_message(update)
                except Exception:
                    logging.exception("Update handler error")

        except Exception as exc:
            logging.exception("Telegram polling error")

            if "409" in str(exc) or "Conflict" in str(exc):
                logging.error(
                    "409 Conflict: stop other polling processes "
                    "and GitHub Actions bot workflows."
                )
                time.sleep(10)
            else:
                time.sleep(3)


# ==================================================
# FLASK ROUTES
# ==================================================

@app.route("/")
def index():
    return jsonify({
        "status": "healthy",
        "engine": "render_active",
    })


@app.route("/health")
def health():
    return jsonify({"status": "ok"}), 200


threading.Thread(
    target=telegram_polling,
    daemon=True,
    name="telegram-polling",
).start()


if __name__ == "__main__":
    app.run(
        host="0.0.0.0",
        port=int(os.environ.get("PORT", "10000")),
        threaded=True,
    )
