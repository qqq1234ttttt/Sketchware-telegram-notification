import os
import time
import threading
import logging
import requests
import jwt

from flask import Flask, jsonify

app = Flask(__name__)
logging.basicConfig(level=logging.INFO)

# Environment Variables (မူလ Key ID များ မပြောင်းထားပါ)
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
ADMIN_ID = os.getenv("TELEGRAM_ADMIN_ID", "").strip()

# Firebase V1 API
FIREBASE_PROJECT_ID = os.getenv(
    "FIREBASE_PROJECT_ID", ""
).strip()

FIREBASE_CLIENT_EMAIL = os.getenv(
    "FIREBASE_CLIENT_EMAIL", ""
).strip()

FIREBASE_PRIVATE_KEY = os.getenv(
    "FIREBASE_PRIVATE_KEY", ""
).strip().replace("\\n", "\n")

# Telegram API URL
TELEGRAM_API = f"https://api.telegram.org/bot{BOT_TOKEN}"

# Shared state
user_states = {}
polling_started = False
polling_lock = threading.Lock()


def is_admin(user_id):
    return bool(ADMIN_ID) and str(user_id) == ADMIN_ID


def telegram(method, data=None, timeout=20):
    if not BOT_TOKEN:
        raise RuntimeError("TELEGRAM_BOT_TOKEN မရှိပါ")

    response = requests.post(
        f"{TELEGRAM_API}/{method}",
        json=data or {},
        timeout=timeout
    )
    response.raise_for_status()

    result = response.json()

    if not result.get("ok"):
        raise RuntimeError(
            f"Telegram API Error: {result}"
        )

    return result.get("result")


def send_message(chat_id, text, keyboard=None):
    data = {
        "chat_id": chat_id,
        "text": text
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
                    "text": "❌ လုပ်ဆောင်ချက် ပယ်ဖျက်ရန်",
                    "callback_data": "cancel"
                }
            ]
        ]
    }


# Firebase OAuth2 Access Token
def get_fb_access_token():
    if not FIREBASE_CLIENT_EMAIL:
        raise RuntimeError(
            "FIREBASE_CLIENT_EMAIL မရှိပါ"
        )

    if not FIREBASE_PRIVATE_KEY:
        raise RuntimeError(
            "FIREBASE_PRIVATE_KEY မရှိပါ"
        )

    now = int(time.time())

    payload = {
        "iss": FIREBASE_CLIENT_EMAIL,
        "scope": (
            "https://www.googleapis.com/auth/"
            "firebase.messaging"
        ),
        "aud": "https://oauth2.googleapis.com/token",
        "iat": now,
        "exp": now + 3600
    }

    signed_jwt = jwt.encode(
        payload,
        FIREBASE_PRIVATE_KEY,
        algorithm="RS256"
    )

    response = requests.post(
        "https://oauth2.googleapis.com/token",
        data={
            "grant_type":
                "urn:ietf:params:oauth:grant-type:jwt-bearer",
            "assertion": signed_jwt
        },
        timeout=20
    )

    response.raise_for_status()

    token_data = response.json()
    access_token = token_data.get("access_token")

    if not access_token:
        raise RuntimeError(
            "Firebase OAuth2 Access Token မရရှိပါ"
        )

    return access_token


# Firebase FCM HTTP v1 Push Notification
def send_fcm_notification(title, message):
    if not FIREBASE_PROJECT_ID:
        raise RuntimeError(
            "FIREBASE_PROJECT_ID မရှိပါ"
        )

    access_token = get_fb_access_token()

    url = (
        "https://fcm.googleapis.com/v1/projects/"
        f"{FIREBASE_PROJECT_ID}/messages:send"
    )

    headers = {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json"
    }

    body = {
        "message": {
            "topic": "all",
            "notification": {
                "title": title,
                "body": message
            }
        }
    }

    response = requests.post(
        url,
        json=body,
        headers=headers,
        timeout=30
    )

    if not response.ok:
        logging.error(
            "FCM API Error: HTTP %s - %s",
            response.status_code,
            response.text[:2000]
        )

    response.raise_for_status()
    return response.json()


# Telegram Inline Keyboard Callback
def handle_callback(update):
    query = update.get("callback_query", {})
    if not query:
        return

    data = query.get("data", "")
    user = query.get("from", {})
    user_id = user.get("id")
    chat_id = (
        query.get("message", {})
        .get("chat", {})
        .get("id")
    )

    try:
        telegram("answerCallbackQuery", {
            "callback_query_id": query["id"]
        })
    except Exception:
        logging.exception("answerCallbackQuery error")

    if not is_admin(user_id):
        if chat_id:
            send_message(
                chat_id,
                "⛔ Admin သာ အသုံးပြုနိုင်ပါတယ်။"
            )
        return

    if not chat_id:
        return

    try:
        if data == "menu":
            send_message(
                chat_id,
                "⚙️ Admin Menu",
                main_menu()
            )

        elif data == "cancel":
            user_states.pop(str(user_id), None)
            send_message(
                chat_id,
                "လုပ်ဆောင်ချက် ပယ်ဖျက်ပြီးပါပြီ။",
                main_menu()
            )

        elif data == "status":
            send_message(
                chat_id,
                "📊 စနစ်အခြေအနေ\n\n"
                "🟢 Backend: Active\n"
                "🟡 Firebase: ပို့စမ်းသပ်မှသာ "
                "အမှန်တကယ်အခြေအနေ သိနိုင်ပါမယ်။",
                main_menu()
            )

        elif data == "create":
            user_states[str(user_id)] = {
                "step": "title"
            }

            send_message(
                chat_id,
                "➕ Push Notification အသစ်ပို့မယ်။\n\n"
                "Notification Title ကို ရိုက်ပို့ပါ။\n"
                "ပယ်ဖျက်ရန် /cancel ကို ရိုက်ပါ။"
            )

    except Exception as e:
        logging.exception("Callback error")
        try:
            send_message(
                chat_id,
                f"❌ အမှားအယွင်း: {str(e)[:1000]}",
                main_menu()
            )
        except Exception:
            logging.exception("Telegram error reply failed")


# Telegram Text Message Handler
def handle_message(update):
    message = update.get("message", {})
    user = message.get("from", {})
    user_id = user.get("id")
    chat_id = message.get("chat", {}).get("id")
    text = message.get("text", "").strip()

    if not is_admin(user_id) or not chat_id:
        return

    if text in ("/start", "/menu"):
        user_states.pop(str(user_id), None)
        send_message(
            chat_id,
            "⚙️ Admin Panel မှ ကြိုဆိုပါတယ်။",
            main_menu()
        )
        return

    if text == "/cancel":
        user_states.pop(str(user_id), None)
        send_message(
            chat_id,
            "လုပ်ဆောင်ချက် ပယ်ဖျက်ပြီးပါပြီ။",
            main_menu()
        )
        return

    state = user_states.get(str(user_id))

    if not state:
        return

    if not text:
        send_message(
            chat_id,
            "စာသားကို ရိုက်ပို့ပေးပါ။"
        )
        return

    if state["step"] == "title":
        user_states[str(user_id)] = {
            "step": "message",
            "title": text
        }

        send_message(
            chat_id,
            f"📌 Title: {text}\n\n"
            "ယခု Notification Message ကို ရိုက်ပို့ပါ။"
        )

    elif state["step"] == "message":
        title = state["title"]
        msg_body = text

        user_states.pop(str(user_id), None)

        send_message(
            chat_id,
            "⏳ Firebase သို့ Notification ပို့နေပါသည်..."
        )

        try:
            result = send_fcm_notification(
                title,
                msg_body
            )

            logging.info(
                "FCM notification accepted: %s",
                result.get("name", "unknown")
            )

            send_message(
                chat_id,
                "🚀 Firebase က Notification ပို့ရန် "
                "လက်ခံပြီးပါပြီ။\n\n"
                f"Title: {title}\n"
                f"Message: {msg_body}\n\n"
                "မှတ်ချက်: စက်အားလုံးသို့ အမှန်တကယ် "
                "ရောက်ရှိကြောင်းကို ဒီတုံ့ပြန်ချက်က "
                "အတည်မပြုပါ။",
                main_menu()
            )

        except Exception as e:
            logging.exception("FCM Send Error")

            send_message(
                chat_id,
                "❌ Firebase Notification ပို့မရပါ။\n\n"
                f"အမှား: {str(e)[:1500]}",
                main_menu()
            )


# Telegram Long Polling
def telegram_polling():
    global polling_started

    with polling_lock:
        if polling_started:
            return
        polling_started = True

    offset = 0
    logging.info("Telegram Polling Started")

    while True:
        try:
            updates = telegram(
                "getUpdates",
                {
                    "offset": offset,
                    "timeout": 10
                },
                timeout=20
            )

            for update in updates:
                offset = update["update_id"] + 1

                try:
                    if "callback_query" in update:
                        handle_callback(update)

                    elif "message" in update:
                        handle_message(update)

                except Exception:
                    logging.exception(
                        "Update handler error"
                    )

        except Exception:
            logging.exception("Telegram polling error")
            time.sleep(3)


@app.route("/")
def index():
    return jsonify({
        "status": "running",
        "bot": "active"
    })


if __name__ == "__main__":
    threading.Thread(
        target=telegram_polling,
        daemon=True
    ).start()

    port = int(os.environ.get("PORT", 8080))

    app.run(
        host="0.0.0.0",
        port=port
    )
