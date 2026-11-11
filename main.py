import os
import time
import threading
import logging
import requests
from flask import Flask, jsonify

app = Flask(__name__)
logging.basicConfig(level=logging.INFO)

# Environment Variables (GitHub Secrets ကနေ ယူပါမယ်)
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
ADMIN_ID = os.getenv("TELEGRAM_ADMIN_ID", "").strip()

# Firebase V1 API အတွက် လိုအပ်သော Variable များ
FIREBASE_PROJECT_ID = os.getenv("FIREBASE_PROJECT_ID", "").strip()
# Firebase Service Account JSON ထဲက Private Key နဲ့ Client Email
FIREBASE_CLIENT_EMAIL = os.getenv("FIREBASE_CLIENT_EMAIL", "").strip()
FIREBASE_PRIVATE_KEY = os.getenv("FIREBASE_PRIVATE_KEY", "").strip().replace("\\n", "\n")

# Telegram API settings
TELEGRAM_API = f"https://api.telegram.org/bot{BOT_TOKEN}"

# Admin conversation states
user_states = {}
polling_started = False


def is_admin(user_id):
    return bool(ADMIN_ID) and str(user_id) == ADMIN_ID


def telegram(method, data=None, timeout=15):
    if not BOT_TOKEN:
        raise RuntimeError("TELEGRAM_BOT_TOKEN is missing")

    response = requests.post(
        f"{TELEGRAM_API}/{method}",
        json=data or {},
        timeout=timeout
    )
    response.raise_for_status()

    result = response.json()
    if not result.get("ok"):
        raise RuntimeError(str(result))

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
                {"text": "📊 စနစ်အခြေအနေ", "callback_data": "status"},
                {"text": "➕ Noti အသစ်ပို့ရန်", "callback_data": "create"}
            ],
            [
                {"text": "❌ လုပ်ဆောင်ချက် ပယ်ဖျက်ရန်", "callback_data": "cancel"}
            ]
        ]
    }


# Firebase OAuth2 Access Token ထုတ်ယူသည့် Function (FCM v1 အတွက် လိုအပ်သည်)
def get_fb_access_token():
    import time
    import jwt  # PyJWT library လိုအပ်ပါမယ်
    
    iat = int(time.time())
    exp = iat + 3600
    
    payload = {
        'iss': FIREBASE_CLIENT_EMAIL,
        'scope': 'https://googleapis.com',
        'aud': 'https://googleapis.com',
        'exp': exp,
        'iat': iat
    }
    
    # Private key သုံးပြီး JWT Sign လုပ်ခြင်း
    signed_jwt = jwt.encode(payload, FIREBASE_PRIVATE_KEY, algorithm='RS256')
    
    # Token တောင်းခြင်း
    url = 'https://googleapis.com'
    data = {
        'grant_type': 'urn:ietf:params:oauth:grant-type:jwt-bearer',
        'assertion': signed_jwt
    }
    
    req = requests.post(url, data=data, timeout=15)
    req.raise_for_status()
    return req.json()['access_token']


# Firebase FCM သို့ Push Notification တိုက်ရိုက် တွန်းပို့သည့် Function
def send_fcm_notification(title, message):
    access_token = get_fb_access_token()
    url = f"https://googleapis.com{FIREBASE_PROJECT_ID}/messages:send"
    
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json"
    }
    
    # Sketchware App ဘက်က Subscribe လုပ်ထားတဲ့ "all" topic ဆီ ပို့မှာဖြစ်ပါတယ်
    body = {
        "message": {
            "topic": "all",
            "notification": {
                "title": title,
                "body": message
            }
        }
    }
    
    response = requests.post(url, json=body, headers=headers, timeout=15)
    response.raise_for_status()
    return response.json()


def handle_callback(update):
    query = update["callback_query"]
    data = query.get("data", "")
    user = query.get("from", {})
    user_id = user.get("id")
    chat_id = query.get("message", {}).get("chat", {}).get("id")

    telegram("answerCallbackQuery", {
        "callback_query_id": query["id"]
    })

    if not is_admin(user_id):
        if chat_id:
            send_message(chat_id, "⛔ Admin သာ အသုံးပြုနိုင်ပါတယ်။")
        return

    if not chat_id:
        return

    try:
        if data == "menu":
            send_message(chat_id, "⚙️ Admin Menu", main_menu())

        elif data == "cancel":
            user_states.pop(str(user_id), None)
            send_message(chat_id, "လုပ်ဆောင်ချက် ပယ်ဖျက်ပြီးပါပြီ။", main_menu())

        elif data == "status":
            send_message(
                chat_id,
                "📊 စနစ်အခြေအနေ\n\n"
                "🟢 Backend: Active (GitHub)\n"
                "🟢 Firebase FCM: ချိတ်ဆက်မှု အဆင်သင့်ရှိသည်\n",
                main_menu()
            )

        elif data == "create":
            user_states[str(user_id)] = {"step": "title"}
            send_message(
                chat_id,
                "➕ Push Notification အသစ်ပို့မယ်။\n\n"
                "အရင်ဆုံး Notification Title ကို ရိုက်ပို့ပါ။\n"
                "ပယ်ဖျက်ရန် /cancel ကို ရိုက်ပါ။"
            )

    except Exception as e:
        logging.exception("Callback error")
        send_message(chat_id, f"❌ အမှားအယွင်းရှိခဲ့သည်- {str(e)}", main_menu())


def handle_message(update):
    message = update.get("message", {})
    user = message.get("from", {})
    user_id = user.get("id")
    chat_id = message.get("chat", {}).get("id")
    text = message.get("text", "").strip()

    if not is_admin(user_id) or not chat_id:
        return

    if text == "/start" or text == "/menu":
        user_states.pop(str(user_id), None)
        send_message(chat_id, "⚙️ Admin Panel မှ ကြိုဆိုပါတယ်။", main_menu())
        return

    if text == "/cancel":
        user_states.pop(str(user_id), None)
        send_message(chat_id, "လုပ်ဆောင်ချက် ပယ်ဖျက်ပြီးပါပြီ။", main_menu())
        return

    state = user_states.get(str(user_id))
    if state:
        if state["step"] == "title":
            user_states[str(user_id)] = {"step": "message", "title": text}
            send_message(chat_id, f"📌 Title: {text}\n\nနောက်တစ်ဆင့်အနေနဲ့ Notification Message (စာသား) ကို ရိုက်ပို့ပေးပါ။")
        
        elif state["step"] == "message":
            title = state["title"]
            msg_body = text
            user_states.pop(str(user_id), None)
            
            send_message(chat_id, "⏳ Firebase သို့ Notification စတင်တွန်းပို့နေပါသည်...")
            
            try:
                send_fcm_notification(title, msg_body)
                send_message(chat_id, f"🚀 အောင်မြင်ပါပြီ။\n\nTitle: {title}\nMessage: {msg_body}\n\nUser အားလုံးဆီ ပို့ပြီးပါပြီ။", main_menu())
            except Exception as e:
                logging.exception("FCM Send Error")
                send_message(chat_id, f"❌ Firebase သို့ ပို့ရာတွင် အမှားအယွင်းရှိခဲ့သည်- {str(e)}", main_menu())


def telegram_polling():
    global polling_started
    if polling_started:
        return
    polling_started = True
    
    offset = 0
    logging.info("Telegram Polling Started...")
    while True:
        try:
            updates = telegram("getUpdates", {"offset": offset, "timeout": 10}, timeout=15)
            for update in updates:
                offset = update["update_id"] + 1
                if "callback_query" in update:
                    handle_callback(update)
                elif "message" in update:
                    handle_message(update)
        except Exception as e:
            logging.error(f"Polling error: {e}")
            time.sleep(3)


@app.route('/')
def index():
    return jsonify({"status": "running", "bot": "active"})


if __name__ == "__main__":
    # Polling ကို Thread ဖြင့် ပတ်ထားပါမည်
    threading.Thread(target=telegram_polling, daemon=True).start()
    port = int(os.environ.get("PORT", 8080))
    app.run(host='0.0.0.0', port=port)
