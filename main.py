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

FIREBASE_PROJECT_ID = os.getenv("FIREBASE_PROJECT_ID", "").strip()
FIREBASE_CLIENT_EMAIL = os.getenv("FIREBASE_CLIENT_EMAIL", "").strip()
FIREBASE_PRIVATE_KEY = os.getenv("FIREBASE_PRIVATE_KEY", "").strip()

# GitHub/Render Secret ထဲက \n ကို newline အဖြစ် ပြောင်းခြင်း
FIREBASE_PRIVATE_KEY = FIREBASE_PRIVATE_KEY.replace("\\n", "\n").strip()

# Secret ကို quotation marks နဲ့ ထည့်ထားမိရင် ဖယ်ခြင်း
if (
    len(FIREBASE_PRIVATE_KEY) >= 2
    and FIREBASE_PRIVATE_KEY[0] == '"'
    and FIREBASE_PRIVATE_KEY[-1] == '"'
):
    FIREBASE_PRIVATE_KEY = FIREBASE_PRIVATE_KEY[1:-1]

TELEGRAM_API = f"https://api.telegram.org/bot{BOT_TOKEN}"
OAUTH_TOKEN_URL = "https://oauth2.googleapis.com/token"
FCM_SCOPE = "https://www.googleapis.com/auth/firebase.messaging"

user_states = {}
polling_started = False
polling_lock = threading.Lock()

# Update တစ်ခုကို ထပ်မံလုပ်ဆောင်ခြင်းမှ ကာကွယ်ရန်
processed_update_ids = set()
processed_update_lock = threading.Lock()
MAX_PROCESSED_UPDATES = 2000


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
        raise RuntimeError("Telegram မှ JSON တုံ့ပြန်ချက် မရပါ။")

    if not response.ok or not result.get("ok"):
        description = result.get("description", response.text[:1000])
        raise RuntimeError(f"Telegram API Error ({response.status_code}): {description}")

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
                {"text": "📊 စနစ်အခြေအနေ", "callback_data": "status"},
                {"text": "➕ Noti အသစ်ပို့ရန်", "callback_data": "create"}
            ],
            [
                {"text": "❌ လုပ်ဆောင်ချက် ပယ်ဖျက်ရန်", "callback_data": "cancel"}
            ]
        ]
    }


# ==================================================
# FIREBASE OAUTH2 ACCESS TOKEN
# ==================================================

def get_fb_access_token():
    if not FIREBASE_CLIENT_EMAIL:
        raise RuntimeError("FIREBASE_CLIENT_EMAIL Secret မရှိပါ။")
    if not FIREBASE_PRIVATE_KEY:
        raise RuntimeError("FIREBASE_PRIVATE_KEY Secret မရှိပါ။")

    if (
        "-----BEGIN PRIVATE KEY-----" not in FIREBASE_PRIVATE_KEY
        or "-----END PRIVATE KEY-----" not in FIREBASE_PRIVATE_KEY
    ):
        raise RuntimeError("FIREBASE_PRIVATE_KEY ပုံစံမမှန်ပါ။")

    now = int(time.time())
    payload = {
        "iss": FIREBASE_CLIENT_EMAIL,
        "scope": FCM_SCOPE,
        "aud": OAUTH_TOKEN_URL,
        "iat": now,
        "exp": now + 3600
    }

    try:
        signed_jwt = jwt.encode(payload, FIREBASE_PRIVATE_KEY, algorithm="RS256")
    except Exception as e:
        raise RuntimeError(f"Firebase Private Key ဖြင့် JWT လက်မှတ်ထိုးမရပါ: {e}")

    try:
        response = requests.post(
            OAUTH_TOKEN_URL,
            data={
                "grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
                "assertion": signed_jwt
            },
            timeout=30
        )
    except requests.RequestException as e:
        raise RuntimeError(f"Google OAuth server သို့ ချိတ်ဆက်မရပါ: {e}")

    if not response.ok:
        try:
            detail = response.json()
        except ValueError:
            detail = response.text[:1000]
        raise RuntimeError(f"Firebase OAuth Error HTTP {response.status_code}: {detail}")

    return response.json().get("access_token")


# ==================================================
# FIREBASE CLOUD MESSAGING HTTP V1 (လူကြီးမင်းပေးသော လိပ်စာအမှန်)
# ==================================================

def send_fcm_notification(title, message):
    if not FIREBASE_PROJECT_ID:
        raise RuntimeError("FIREBASE_PROJECT_ID Secret မရှိပါ။")

    access_token = get_fb_access_token()
    url = f"https://fcm.googleapis.com/v1/projects/{FIREBASE_PROJECT_ID}/messages:send"

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
        response = requests.post(url, json=body, headers=headers, timeout=30)
    except requests.RequestException as e:
        raise RuntimeError(f"FCM server သို့ ချိတ်ဆက်မရပါ: {e}")

    if not response.ok:
        try:
            error_data = response.json()
        except ValueError:
            error_data = response.text[:1500]
        raise RuntimeError(f"Firebase FCM Error HTTP {response.status_code}: {error_data}")

    return response.json()


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
    chat_id = query.get("message", {}).get("chat", {}).get("id")

    try:
        telegram("answerCallbackQuery", {"callback_query_id": query["id"]})
    except Exception:
        logging.exception("Callback acknowledgement failed")

    if not is_admin(user_id):
        if chat_id:
            send_message(chat_id, "⛔ Admin သာ အသုံးပြုနိုင်ပါတယ်။")
        return

    if not chat_id:
        return

    try:
        if data == "cancel":
            user_states.pop(str(user_id), None)
            send_message(chat_id, "❌ လုပ်ဆောင်ချက်ကို ပယ်ဖျက်ပြီးပါပြီ။", main_menu())
        
        elif data == "status":
            send_message(
                chat_id,
                "📊 စနစ်အခြေအနေ\n\n🟢 Backend: Active (Render Server)\n🟢 Firebase FCM: ချိတ်ဆက်မှု အဆင်သင့်ရှိသည်",
                main_menu()
            )
            
        elif data == "create":
            user_states[str(user_id)] = {"step": "title"}
            send_message(chat_id, "➕ Push Notification အသစ်ဆောက်ပါမယ်။\n\nအရင်ဆုံး Notification **Title (ခေါင်းစဉ်)** ကို ရိုက်ပို့ပေးပါ။")
            
        elif data == "confirm_send":
            state = user_states.get(str(user_id))
            if state and state.get("step") == "confirm":
                title = state["title"]
                message = state["message"]
                user_states.pop(str(user_id), None)
                
                send_message(chat_id, "⏳ Firebase FCM သို့ တိုက်ရိုက်တွန်းပို့နေပါသည်...")
                try:
                    send_fcm_notification(title, message)
                    send_message(chat_id, f"🚀 **အောင်မြင်စွာ ပို့ဆောင်ပြီးပါပြီ။**\n\n📌 Title: {title}\n📝 Message: {message}", main_menu())
                except Exception as e:
                    send_message(chat_id, f"❌ ပို့ဆောင်မှု မအောင်မြင်ပါ- {str(e)}", main_menu())
            else:
                send_message(chat_id, "❌ သက်တမ်းကုန်ဆုံးသွားသော လုပ်ဆောင်ချက်ဖြစ်သည် သို့မဟုတ် ဒေတာမရှိပါ။", main_menu())
                
        elif data == "re_create":
            user_states[str(user_id)] = {"step": "title"}
            send_message(chat_id, "➕ ပြန်လည်ပြင်ဆင်ရေးသားပါမယ်။\n\nNotification **Title (ခေါင်းစဉ်)** ကို ပြန်လည်ရိုက်ပို့ပေးပါ။")

    except Exception as e:
        logging.exception("Callback handling error")
        send_message(chat_id, f"❌ အမှားအယွင်းရှိခဲ့သည်- {str(e)}", main_menu())


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

    if text == "/start" or text == "/menu":
        user_states.pop(str(user_id), None)
        send_message(chat_id, "⚙️ Admin Control Panel မှ ကြိုဆိုပါတယ်။", main_menu())
        return

    if text == "/cancel":
        user_states.pop(str(user_id), None)
        send_message(chat_id, "❌ လုပ်ဆောင်ချက်ကို ပယ်ဖျက်ပြီးပါပြီ။", main_menu())
        return

    state = user_states.get(str(user_id))
    if state:
        if state["step"] == "title":
            user_states[str(user_id)] = {"step": "message", "title": text}
            send_message(chat_id, f"📌 **Title:** {text}\n\nနောက်တစ်ဆင့်အနေနဲ့ Notification **Message (စာသား)** ကို ဆက်လက်ရိုက်ပို့ပေးပါ။")
            
        elif state["step"] == "message":
            title = state["title"]
            msg_body = text
            user_states[str(user_id)] = {"step": "confirm", "title": title, "message": msg_body}
            
            # သေချာပါသလား ဟု မေးမြန်းသည့် ကီးဘုတ်ခလုတ်
            confirm_keyboard = {
                "inline_keyboard": [
                    [
{"text": "✅ သေချာသည်၊ အခုပို့မယ်", "callback_data": "confirm_send"},
{"text": "✏️ ပြန်ပြင်မယ်", "callback_data": "re_create"}
],
[
{"text": "❌ ဖျက်သိမ်းမယ်", "callback_data": "cancel"}
]
]
}
send_message(
chat_id,
f"❓ ယခုအချက်အလက်များအတိုင်း ပို့ရန် သေချာပါသလား။\n\n📌 Title: {title}\n📝 Message: {msg_body}",
confirm_keyboard
)

==================================================


TELEGRAM POLLING LOOP


==================================================

def telegram_polling():
global polling_started
with polling_lock:
if polling_started:
return
polling_started = True
offset = 0
logging.info("Telegram Polling Engine Started...")
while True:
try:
updates = telegram("getUpdates", {"offset": offset, "timeout": 15}, timeout=25)
for update in updates:
update_id = update["update_id"]
offset = update_id + 1
with processed_update_lock:
if update_id in processed_update_ids:
continue
processed_update_ids.add(update_id)
if len(processed_update_ids) > MAX_PROCESSED_UPDATES:
processed_update_ids.pop()
if "callback_query" in update:
handle_callback(update)
elif "message" in update:
handle_message(update)
except Exception as e:
logging.error(f"Polling Network Exception: {e}")
time.sleep(3)
@app.route('/')
def index():
return jsonify({"status": "healthy", "engine": "render_active"})
if name == "main":
if not polling_started:
threading.Thread(target=telegram_polling, daemon=True).start()
port = int(os.environ.get("PORT", 10000))
app.run(host='0.0.0.0', port=port)
