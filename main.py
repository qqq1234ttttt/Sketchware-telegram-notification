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
# မူလ GitHub Secrets နာမည်များ မပြောင်းထားပါ
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

# GitHub Secret ထဲက \n ကို newline အဖြစ် ပြောင်းခြင်း
FIREBASE_PRIVATE_KEY = FIREBASE_PRIVATE_KEY.replace(
    "\\n", "\n"
).strip()

# Secret ကို quotation marks နဲ့ ထည့်ထားမိရင် ဖယ်ခြင်း
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


# ==================================================
# COMMON HELPERS
# ==================================================

def is_admin(user_id):
    return (
        bool(ADMIN_ID)
        and user_id is not None
        and str(user_id) == ADMIN_ID
    )


def telegram(method, data=None, timeout=20):
    if not BOT_TOKEN:
        raise RuntimeError(
            "GitHub Secret TELEGRAM_BOT_TOKEN မရှိပါ။"
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
        raise RuntimeError("Telegram မှ JSON တုံ့ပြန်ချက် မရပါ။")

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
                    "text": "❌ လုပ်ဆောင်ချက် ပယ်ဖျက်ရန်",
                    "callback_data": "cancel"
                }
            ]
        ]
    }


# ==================================================
# FIREBASE OAUTH2 ACCESS TOKEN
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
            "FIREBASE_PRIVATE_KEY ပုံစံမမှန်ပါ။ "
            "Service Account JSON ထဲက private_key "
            "တန်ဖိုးကို အပြည့်အစုံ ထည့်ထားကြောင်း စစ်ပါ။"
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
            "Firebase Private Key ဖြင့် JWT "
            f"လက်မှတ်ထိုးမရပါ။ Key ကို စစ်ပါ။ ({e})"
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
            f"Google OAuth server သို့ ချိတ်ဆက်မရပါ: {e}"
        ) from e

    if not response.ok:
        # Access token သို့မဟုတ် private key ကို log မထုတ်ပါ
        try:
            detail = response.json()
        except ValueError:
            detail = response.text[:1000]

        raise RuntimeError(
            f"Firebase OAuth Error HTTP {response.status_code}: "
            f"{detail}"
        )

    token_data = response.json()
    access_token = token_data.get("access_token")

    if not access_token:
        raise RuntimeError(
            "Google OAuth မှ Access Token မရရှိပါ။"
        )

    return access_token


# ==================================================
# FIREBASE CLOUD MESSAGING HTTP V1
# ==================================================

def send_fcm_notification(title, message):
    if not FIREBASE_PROJECT_ID:
        raise RuntimeError(
            "FIREBASE_PROJECT_ID Secret မရှိပါ။"
        )

    access_token = get_fb_access_token()

    # မှန်ကန်သော Firebase FCM HTTP v1 endpoint
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
            f"FCM server သို့ ချိတ်ဆက်မရပါ: {e}"
        ) from e

    if not response.ok:
        try:
            error_data = response.json()
        except ValueError:
            error_data = response.text[:1500]

        logging.error(
            "FCM HTTP %s: %s",
            response.status_code,
            error_data
        )

        raise RuntimeError(
            f"Firebase FCM Error HTTP {response.status_code}: "
            f"{error_data}"
        )

    result = response.json()

    logging.info(
        "FCM accepted message: %s",
        result.get("name", "unknown")
    )

    return result


# ==================================================
# TELEGRAM CALLBACK HANDLER
# ==================================================

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
        logging.exception("Callback acknowledgement failed")

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
                "🟢 Telegram Backend: အလုပ်လုပ်နေသည်\n"
                "ℹ️ Firebase ပို့ဆောင်မှုကို စမ်းသပ်မှသာ "
                "အမှန်တကယ် အခြေအနေ သိနိုင်ပါမည်။",
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
        logging.exception("Callback handler error")

        try:
            send_message(
                chat_id,
                f"❌ အမှားအယွင်း: {str(e)[:1500]}",
                main_menu()
            )
        except Exception:
            logging.exception("Could not send callback error")


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
        return

    if state["step"] == "message":
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

            send_message(
                chat_id,
                "🚀 Firebase က Notification ကို "
                "ပို့ရန် လက်ခံပြီးပါပြီ။\n\n"
                f"📌 Title: {title}\n"
                f"📝 Message: {msg_body}\n\n"
                "မှတ်ချက်: Firebase လက်ခံခြင်းသည် "
                "စက်အားလုံးသို့ ရောက်ရှိပြီးကြောင်း "
                "အာမခံခြင်းမဟုတ်ပါ။",
                main_menu()
            )

        except Exception as e:
            logging.exception("FCM send failed")

            try:
                send_message(
                    chat_id,
                    "❌ Firebase Notification ပို့မရပါ။\n\n"
                    f"အမှား: {str(e)[:2500]}",
                    main_menu()
                )
            except Exception:
                logging.exception("Could not send FCM error reply")


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
                    "timeout": 10,
                    "allowed_updates": [
                        "message",
                        "callback_query"
                    ]
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
                    logging.exception("Update handler error")

        except Exception:
            logging.exception("Telegram polling error")
            time.sleep(3)


# ==================================================
# FLASK HEALTH CHECK
# ==================================================

@app.route("/")
def index():
    return jsonify({
        "status": "running",
        "bot": "active",
        "firebase_configured": bool(
            FIREBASE_PROJECT_ID
            and FIREBASE_CLIENT_EMAIL
            and FIREBASE_PRIVATE_KEY
        )
    })


@app.route("/health")
def health():
    return jsonify({"status": "ok"}), 200


# ==================================================
# START APPLICATION
# ==================================================

if __name__ == "__main__":
    if not BOT_TOKEN:
        logging.error("TELEGRAM_BOT_TOKEN Secret မရှိပါ။")

    threading.Thread(
        target=telegram_polling,
        daemon=True
    ).start()

    port = int(os.environ.get("PORT", "8080"))

    app.run(
        host="0.0.0.0",
        port=port,
        threaded=True
    )
