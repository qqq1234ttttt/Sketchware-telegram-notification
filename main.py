import os
import time
import threading
import logging
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
).strip()

FIREBASE_PRIVATE_KEY = FIREBASE_PRIVATE_KEY.replace(
    "\\n", "\n"
).strip()

if (
    len(FIREBASE_PRIVATE_KEY) >= 2
    and FIREBASE_PRIVATE_KEY[0] == '"'
    and FIREBASE_PRIVATE_KEY[-1] == '"'
):
    FIREBASE_PRIVATE_KEY = FIREBASE_PRIVATE_KEY[1:-1]

TELEGRAM_API = (
    f"https://api.telegram.org/bot{BOT_TOKEN}"
)

OAUTH_TOKEN_URL = "https://oauth2.googleapis.com/token"

FCM_SCOPE = (
    "https://www.googleapis.com/auth/firebase.messaging"
)

user_states = {}

polling_started = False
polling_lock = threading.Lock()

# Duplicate update protection within this process
processed_update_ids = set()
processed_update_lock = threading.Lock()
MAX_PROCESSED_UPDATES = 2000

# Confirm button protection
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
        raise RuntimeError(
            "TELEGRAM_BOT_TOKEN Secret မရှိပါ။"
        )

    response = requests.post(
        f"{TELEGRAM_API}/{method}",
        json=data or {},
        timeout=timeout
    )

    try:
        result = response.json()
    except ValueError:
        response.raise_for_status()
        raise RuntimeError(
            "Telegram မှ JSON တုံ့ပြန်ချက် မရပါ။"
        )

    if not response.ok or not result.get("ok"):
        description = result.get(
            "description",
            response.text[:1000]
        )

        raise RuntimeError(
            f"Telegram API Error ({response.status_code}): "
            f"{description}"
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
    }


# ==================================================
# FIREBASE ACCESS TOKEN
# ==================================================

def get_fb_access_token():
    if not FIREBASE_CLIENT_EMAIL:
        raise RuntimeError(
            "FIREBASE_CLIENT_EMAIL Secret မရှိပါ။"
        )

    if not FIREBASE_PRIVATE_KEY:
        raise RuntimeError(
            "FIREBASE_PRIVATE_KEY Secret မရှိပါ။"
        )

    if (
        "-----BEGIN PRIVATE KEY-----"
        not in FIREBASE_PRIVATE_KEY
        or "-----END PRIVATE KEY-----"
        not in FIREBASE_PRIVATE_KEY
    ):
        raise RuntimeError(
            "FIREBASE_PRIVATE_KEY ပုံစံမမှန်ပါ။"
        )

    now = int(time.time())

    payload = {
        "iss": FIREBASE_CLIENT_EMAIL,
        "scope": FCM_SCOPE,
        "aud": OAUTH_TOKEN_URL,
        "iat": now,
        "exp": now + 3600
    }

    try:
        signed_jwt = jwt.encode(
            payload,
            FIREBASE_PRIVATE_KEY,
            algorithm="RS256"
        )
    except Exception as e:
        raise RuntimeError(
            f"Firebase Private Key အမှား: {e}"
        ) from e

    try:
        response = requests.post(
            OAUTH_TOKEN_URL,
            data={
                "grant_type":
                    "urn:ietf:params:oauth:grant-type:jwt-bearer",
                "assertion": signed_jwt
            },
            timeout=30
        )
    except requests.RequestException as e:
        raise RuntimeError(
            f"Google OAuth ချိတ်ဆက်မှုအမှား: {e}"
        ) from e

    if not response.ok:
        try:
            detail = response.json()
        except ValueError:
            detail = response.text[:1000]

        raise RuntimeError(
            f"OAuth HTTP {response.status_code}: {detail}"
        )

    access_token = response.json().get("access_token")

    if not access_token:
        raise RuntimeError(
            "Firebase Access Token မရရှိပါ။"
        )

    return access_token


# ==================================================
# FIREBASE FCM NOTIFICATION
# ==================================================

def send_fcm_notification(title, message):
    if not FIREBASE_PROJECT_ID:
        raise RuntimeError(
            "FIREBASE_PROJECT_ID Secret မရှိပါ။"
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

    try:
        response = requests.post(
            url,
            json=body,
            headers=headers,
            timeout=30
        )
    except requests.RequestException as e:
        raise RuntimeError(
            f"Firebase ချိတ်ဆက်မှုအမှား: {e}"
        ) from e

    if not response.ok:
        try:
            detail = response.json()
        except ValueError:
            detail = response.text[:1500]

        raise RuntimeError(
            f"FCM HTTP {response.status_code}: {detail}"
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
    user = query.get("from", {})
    user_id = user.get("id")

    chat_id = (
        query.get("message", {})
        .get("chat", {})
        .get("id")
    )

    # Button ကို နှိပ်ပြီးကြောင်း Telegram ကို ချက်ချင်းပြောရန်
    try:
        telegram(
            "answerCallbackQuery",
            {"callback_query_id": query_id}
        )
    except Exception:
        logging.exception(
            "Callback acknowledgement failed"
        )

    if not is_admin(user_id):
        if chat_id:
            send_message(
                chat_id,
                "⛔ Admin သာ အသုံးပြုနိုင်ပါတယ်။"
            )
        return

    if not chat_id:
        return

    user_key = str(user_id)

    try:
        if data == "status":
            send_message(
                chat_id,
                "📊 စနစ်အခြေအနေ\n\n"
                "🟢 Telegram Backend: အလုပ်လုပ်နေသည်\n"
                "ℹ️ Firebase အခြေအနေကို အမှန်သိရန် "
                "Notification စမ်းပို့ရပါမည်။",
                main_menu()
            )

        elif data == "create":
            user_states[user_key] = {
                "step": "title"
            }

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
            user_states[user_key] = {
                "step": "title"
            }

            send_message(
                chat_id,
                "✏️ ပြန်လည်ရေးသားပါမယ်။\n\n"
                "Notification Title အသစ်ကို ရိုက်ပို့ပါ။"
            )

        elif data == "confirm_send":
            # User တစ်ဦးက အကြိမ်ကြိမ်နှိပ်ခြင်းကို တားရန်
            with sending_users_lock:
                if user_key in sending_users:
                    return

                state = user_states.get(user_key)

                if (
                    not state
                    or state.get("step") != "confirm"
                ):
                    state = None
                else:
                    sending_users.add(user_key)
                    # ပို့မီ state ကို ဖယ်ရှားပြီး
                    # ထပ်နှိပ်၍ ပို့ခြင်းကို ကာကွယ်သည်
                    user_states.pop(user_key, None)

            if not state:
                send_message(
                    chat_id,
                    "ℹ️ ဒီ Notification ကို ပို့ပြီးဖြစ်နိုင်ပါတယ်၊ "
                    "သို့မဟုတ် အတည်ပြုရန် အချက်အလက်မရှိတော့ပါ။",
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

                send_fcm_notification(
                    title,
                    msg_body
                )

                send_message(
                    chat_id,
                    "🚀 Firebase က Notification ကို "
                    "ပို့ရန် လက်ခံပြီးပါပြီ။\n\n"
                    f"📌 Title: {title}\n"
                    f"📝 Message: {msg_body}\n\n"
                    "မှတ်ချက်: စက်အားလုံးသို့ "
                    "ရောက်ရှိပြီးကြောင်း အာမခံခြင်းမဟုတ်ပါ။",
                    main_menu()
                )

            except Exception as e:
                logging.exception(
                    "FCM notification failed"
                )

                send_message(
                    chat_id,
                    "❌ Notification ပို့မရပါ။\n\n"
                    f"အမှား: {str(e)[:2000]}",
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
                f"❌ အမှားအယွင်း: {str(e)[:1500]}",
                main_menu()
            )
        except Exception:
            logging.exception(
                "Failed to send callback error"
            )


# ==================================================
# TELEGRAM MESSAGE HANDLER
# ==================================================

def handle_message(update):
    message = update.get("message", {})
    user = message.get("from", {})
    user_id = user.get("id")
    chat_id = message.get("chat", {}).get("id")
    text = message.get("text", "").strip()

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
        send_message(
            chat_id,
            "စာသားကို ရိုက်ပို့ပေးပါ။"
        )
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

    logging.info("Telegram Polling Started")

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

                # Telegram Update တစ်ခုကို process တစ်ခုအတွင်း
                # နှစ်ခါမကိုင်တွယ်စေရန်
                with processed_update_lock:
                    if update_id in processed_update_ids:
                        offset = max(offset, update_id + 1)
                        continue

                    processed_update_ids.add(update_id)

                    if len(processed_update_ids) > MAX_PROCESSED_UPDATES:
                        processed_update_ids.clear()
                        processed_update_ids.add(update_id)

                offset = max(offset, update_id + 1)

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
            logging.exception(
                "Telegram polling network error"
            )
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
    return jsonify({
        "status": "ok"
    }), 200


# ==================================================
# START TELEGRAM POLLING
# Gunicorn ဖြင့် Run လုပ်သည့်အခါလည်း စတင်ရန်
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
