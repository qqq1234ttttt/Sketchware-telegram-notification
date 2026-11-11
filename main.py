import os
import time
import threading
import logging
from datetime import datetime, timezone

import requests
import jwt
from flask import Flask, jsonify

app = Flask(__name__)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s"
)

# ==================================================
# ENVIRONMENT VARIABLES
# ==================================================

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

TELEGRAM_API = f"https://api.telegram.org/bot{BOT_TOKEN}"
OAUTH_TOKEN_URL = "https://oauth2.googleapis.com/token"

# FCM နှင့် Realtime Database အတွက် OAuth scopes
FIREBASE_SCOPES = (
    "https://www.googleapis.com/auth/firebase.messaging "
    "https://www.googleapis.com/auth/firebase.database"
)

user_states = {}

polling_started = False
polling_lock = threading.Lock()

sending_users = set()
sending_users_lock = threading.Lock()

# ==================================================
# COMMON HELPERS
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
        timeout=timeout
    )

    try:
        result = response.json()
    except ValueError:
        response.raise_for_status()
        raise RuntimeError("Telegram JSON response မရပါ။")

    if not response.ok or not result.get("ok"):
        raise RuntimeError(
            f"Telegram API Error ({response.status_code}): "
            f"{result.get('description', response.text[:1000])}"
        )

    return result.get("result")


def send_message(chat_id, text, keyboard=None):
    data = {
        "chat_id": chat_id,
        "text": str(text)[:4000]
    }

    if keyboard:
        data["reply_markup"] = keyboard

    return telegram("sendMessage", data)


def main_menu():
    return {
        "inline_keyboard": [
            [
                {
                    "text": "📊 စနစ်အခြေအနေ",
                    "callback_data": "status"
                },
                {
                    "text": "➕ Noti အသစ်ပို့ရန်",
                    "callback_data": "create"
                }
            ],
            [
                {
                    "text": "🔔 နောက်ဆုံး Noti ကြည့်ရန်",
                    "callback_data": "last_notification"
                }
            ],
            [
                {
                    "text": "🗑️ Noti မှတ်တမ်းဖျက်ရန်",
                    "callback_data": "delete_last"
                }
            ],
            [
                {
                    "text": "❌ ပယ်ဖျက်ရန်",
                    "callback_data": "cancel"
                }
            ]
        ]
    }


def confirm_menu():
    return {
        "inline_keyboard": [
            [
                {
                    "text": "✅ အခုပို့မယ်",
                    "callback_data": "confirm_send"
                },
                {
                    "text": "✏️ ပြန်ပြင်မယ်",
                    "callback_data": "re_create"
                }
            ],
            [
                {
                    "text": "❌ ဖျက်သိမ်းမယ်",
                    "callback_data": "cancel"
                }
            ]
        ]
    ]


def delete_confirm_menu():
    return {
        "inline_keyboard": [
            [
                {
                    "text": "🗑️ ဖျက်ရန် အတည်ပြုမယ်",
                    "callback_data": "confirm_delete_last"
                }
            ],
            [
                {
                    "text": "↩️ မဖျက်တော့ပါ",
                    "callback_data": "cancel"
                }
            ]
        ]
    }


# ==================================================
# FIREBASE ACCESS TOKEN
# ==================================================

def get_fb_access_token():
    if not FIREBASE_CLIENT_EMAIL:
        raise RuntimeError("FIREBASE_CLIENT_EMAIL မရှိပါ။")

    if not FIREBASE_PRIVATE_KEY:
        raise RuntimeError("FIREBASE_PRIVATE_KEY မရှိပါ။")

    if (
        "-----BEGIN PRIVATE KEY-----" not in FIREBASE_PRIVATE_KEY
        or "-----END PRIVATE KEY-----" not in FIREBASE_PRIVATE_KEY
    ):
        raise RuntimeError("FIREBASE_PRIVATE_KEY ပုံစံမမှန်ပါ။")

    now = int(time.time())

    payload = {
        "iss": FIREBASE_CLIENT_EMAIL,
        "scope": FIREBASE_SCOPES,
        "aud": OAUTH_TOKEN_URL,
        "iat": now,
        "exp": now + 3600
    }

    signed_jwt = jwt.encode(
        payload,
        FIREBASE_PRIVATE_KEY,
        algorithm="RS256"
    )

    response = requests.post(
        OAUTH_TOKEN_URL,
        data={
            "grant_type":
                "urn:ietf:params:oauth:grant-type:jwt-bearer",
            "assertion": signed_jwt
        },
        timeout=30
    )

    if not response.ok:
        raise RuntimeError(
            f"Google OAuth HTTP {response.status_code}: "
            f"{response.text[:1200]}"
        )

    token = response.json().get("access_token")

    if not token:
        raise RuntimeError("Firebase Access Token မရပါ။")

    return token


# ==================================================
# FIREBASE REALTIME DATABASE
# ==================================================

def database_request(method, path, data=None):
    if not FIREBASE_DATABASE_URL:
        raise RuntimeError("FIREBASE_DATABASE_URL မရှိပါ။")

    access_token = get_fb_access_token()

    url = f"{FIREBASE_DATABASE_URL}/{path}.json"

    response = requests.request(
        method,
        url,
        headers={
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json"
        },
        json=data,
        timeout=30
    )

    if not response.ok:
        raise RuntimeError(
            f"Firebase Database HTTP {response.status_code}: "
            f"{response.text[:1200]}"
        )

    if response.status_code == 204 or not response.content:
        return None

    return response.json()


def save_last_notification(title, message, fcm_name):
    record = {
        "title": title,
        "message": message,
        "sent_at": datetime.now(timezone.utc).isoformat(),
        "firebase_message_id": fcm_name or ""
    }

    database_request(
        "PUT",
        "admin/last_notification",
        record
    )

    return record


def get_last_notification():
    return database_request(
        "GET",
        "admin/last_notification"
    )


def delete_last_notification():
    database_request(
        "DELETE",
        "admin/last_notification"
    )


def format_last_notification(record):
    if not isinstance(record, dict) or not record:
        return (
            "🔔 နောက်ဆုံး Noti မှတ်တမ်း မရှိသေးပါ။\n\n"
            "Noti တစ်ခု အောင်မြင်စွာပို့ပြီးမှ မှတ်တမ်းသိမ်းပါမယ်။"
        )

    title = str(record.get("title", "မရှိပါ"))
    message = str(record.get("message", "မရှိပါ"))
    sent_at = str(record.get("sent_at", "မသိရပါ"))

    return (
        "🔔 နောက်ဆုံးပို့ခဲ့သော Noti\n\n"
        f"📌 Title: {title}\n"
        f"📝 Message: {message}\n"
        f"🕒 ပို့ချိန် (UTC): {sent_at}"
    )


# ==================================================
# FIREBASE FCM
# ==================================================

def send_fcm_notification(title, message):
    if not FIREBASE_PROJECT_ID:
        raise RuntimeError("FIREBASE_PROJECT_ID မရှိပါ။")

    access_token = get_fb_access_token()

    url = (
        "https://fcm.googleapis.com/v1/projects/"
        f"{FIREBASE_PROJECT_ID}/messages:send"
    )

    response = requests.post(
        url,
        json={
            "message": {
                "topic": "all",
                "notification": {
                    "title": title,
                    "body": message
                }
            }
        },
        headers={
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json"
        },
        timeout=30
    )

    if not response.ok:
        raise RuntimeError(
            f"FCM HTTP {response.status_code}: "
            f"{response.text[:1500]}"
        )

    return response.json()


# ==================================================
# TELEGRAM CALLBACK HANDLER
# ==================================================

def handle_callback(update):
    query = update.get("callback_query", {})

    if not query:
        return

    query_id = query.get("id")
    data = query.get("data", "")
    user_id = query.get("from", {}).get("id")

    chat_id = (
        query.get("message", {})
        .get("chat", {})
        .get("id")
    )

    try:
        telegram(
            "answerCallbackQuery",
            {"callback_query_id": query_id}
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
                last_text = format_last_notification(record)
                db_status = "🟢 Database ချိတ်ဆက်မှု အောင်မြင်"
            except Exception as e:
                logging.exception("Database status check failed")
                last_text = "⚠️ နောက်ဆုံး Noti မှတ်တမ်းကို ဖတ်မရပါ။"
                db_status = f"🔴 Database အမှား: {str(e)[:500]}"

            send_message(
                chat_id,
                "📊 စနစ်အခြေအနေ\n\n"
                "🟢 Telegram Backend: အလုပ်လုပ်နေသည်\n"
                f"{db_status}\n\n"
                f"{last_text}",
                main_menu()
            )

        elif data == "last_notification":
            record = get_last_notification()

            send_message(
                chat_id,
                format_last_notification(record),
                main_menu()
            )

        elif data == "delete_last":
            record = get_last_notification()

            if not isinstance(record, dict) or not record:
                send_message(
                    chat_id,
                    "ℹ️ ဖျက်ရန် Noti မှတ်တမ်း မရှိပါ။",
                    main_menu()
                )
                return

            user_states[user_key] = {
                "step": "delete_last_confirm"
            }

            send_message(
                chat_id,
                "⚠️ နောက်ဆုံး Noti မှတ်တမ်းကို ဖျက်မှာ သေချာပါသလား။\n\n"
                f"{format_last_notification(record)}\n\n"
                "မှတ်ချက်: Database မှတ်တမ်းကိုသာ ဖျက်ပါမယ်။ "
                "ဖုန်းတွေမှာ ရောက်ပြီးသား Noti ကို ပြန်ဖျက်မှာ မဟုတ်ပါ။",
                delete_confirm_menu()
            )

        elif data == "confirm_delete_last":
            state = user_states.get(user_key)

            if not state or state.get("step") != "delete_last_confirm":
                send_message(
                    chat_id,
                    "ℹ️ ဖျက်ရန် အတည်ပြုချက်မရှိပါ။ Menu ကနေ ပြန်ရွေးပါ။",
                    main_menu()
                )
                return

            user_states.pop(user_key, None)
            delete_last_notification()

            send_message(
                chat_id,
                "🗑️ နောက်ဆုံး Noti မှတ်တမ်းကို ဖျက်ပြီးပါပြီ။",
                main_menu()
            )

        elif data == "create":
            user_states[user_key] = {"step": "title"}

            send_message(
                chat_id,
                "➕ Notification အသစ်ပြုလုပ်ပါမယ်။\n\n"
                "Notification Title ကို ရိုက်ပို့ပါ။\n"
                "ပယ်ဖျက်ရန် /cancel ကို ရိုက်ပါ။"
            )

        elif data == "cancel":
            user_states.pop(user_key, None)

            send_message(
                chat_id,
                "❌ လုပ်ဆောင်ချက်ကို ပယ်ဖျက်ပြီးပါပြီ။",
                main_menu()
            )

        elif data == "re_create":
            user_states[user_key] = {"step": "title"}

            send_message(
                chat_id,
                "✏️ ပြန်လည်ရေးသားပါမယ်။\n\n"
                "Notification Title အသစ်ကို ရိုက်ပို့ပါ။"
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
                    "ℹ️ ပို့ရန်အချက်အလက် မရှိတော့ပါ။ "
                    "Menu ကနေ အသစ်ပြုလုပ်ပါ။",
                    main_menu()
                )
                return

            title = state["title"]
            msg_body = state["message"]

            try:
                send_message(
                    chat_id,
                    "⏳ Firebase သို့ Notification ပို့နေပါသည်..."
                )

                # FCM အောင်မြင်မှသာ မှတ်တမ်းသိမ်းမည်
                fcm_result = send_fcm_notification(title, msg_body)

                try:
                    save_last_notification(
                        title,
                        msg_body,
                        fcm_result.get("name", "")
                    )

                    history_status = (
                        "💾 နောက်ဆုံး Noti မှတ်တမ်း သိမ်းပြီးပါပြီ။"
                    )

                except Exception as db_error:
                    logging.exception(
                        "FCM succeeded but history save failed"
                    )

                    history_status = (
                        "⚠️ Noti ကို Firebase FCM က လက်ခံပြီးပါပြီ။\n"
                        "သို့သော် Database မှတ်တမ်းသိမ်းမရပါ။\n"
                        f"အမှား: {str(db_error)[:500]}"
                    )

                send_message(
                    chat_id,
                    "🚀 Firebase FCM က Notification ကို "
                    "ပို့ရန် လက်ခံပြီးပါပြီ။\n\n"
                    f"📌 Title: {title}\n"
                    f"📝 Message: {msg_body}\n\n"
                    f"{history_status}\n\n"
                    "မှတ်ချက်: ဖုန်းအားလုံးသို့ ရောက်ရှိကြောင်း "
                    "အာမခံခြင်းမဟုတ်ပါ။",
                    main_menu()
                )

            except Exception as e:
                logging.exception("FCM notification failed")

                send_message(
                    chat_id,
                    "❌ Notification ပို့မရပါ။\n\n"
                    f"အမှား: {str(e)[:1500]}",
                    main_menu()
                )

            finally:
                with sending_users_lock:
                    sending_users.discard(user_key)

    except Exception as e:
        logging.exception("Callback handler error")

        try:
            send_message(
                chat_id,
                f"❌ အမှားအယွင်း: {str(e)[:1200]}",
                main_menu()
            )
        except Exception:
            logging.exception("Failed to send callback error")


# ==================================================
# TELEGRAM MESSAGE HANDLER
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
            main_menu()
        )
        return

    if text == "/cancel":
        user_states.pop(user_key, None)

        send_message(
            chat_id,
            "❌ လုပ်ဆောင်ချက်ကို ပယ်ဖျက်ပြီးပါပြီ။",
            main_menu()
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
            "title": text
        }

        send_message(
            chat_id,
            f"📌 Title: {text}\n\n"
            "ယခု Notification Message ကို ရိုက်ပို့ပါ။"
        )
        return

    if state["step"] == "message":
        user_states[user_key] = {
            "step": "confirm",
            "title": state["title"],
            "message": text
        }

        send_message(
            chat_id,
            "❓ ဒီအချက်အလက်တွေအတိုင်း ပို့မှာ သေချာပါသလား။\n\n"
            f"📌 Title: {state['title']}\n"
            f"📝 Message: {text}",
            confirm_menu()
        )


# ==================================================
# TELEGRAM LONG POLLING
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
                    "allowed_updates": [
                        "message",
                        "callback_query"
                    ]
                },
                timeout=25
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

        except Exception as e:
            logging.exception("Telegram polling error")

            # 409 ဆိုလျှင် အခြား process တစ်ခုလည်း
            # ဒီ Bot Token နဲ့ polling လုပ်နေနိုင်သည်။
            if "409" in str(e) or "Conflict" in str(e):
                logging.error(
                    "409 Conflict: stop other bot instances "
                    "and GitHub Actions runners."
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
        "engine": "render_active"
    })


@app.route("/health")
def health():
    return jsonify({"status": "ok"}), 200


# ==================================================
# START TELEGRAM POLLING
# ==================================================

threading.Thread(
    target=telegram_polling,
    daemon=True,
    name="telegram-polling"
).start()


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "10000"))

    app.run(
        host="0.0.0.0",
        port=port,
        threaded=True
    )
