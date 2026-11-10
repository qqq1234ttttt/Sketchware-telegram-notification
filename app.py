
import os
import time
import threading
import logging
import requests

from flask import Flask, jsonify

app = Flask(__name__)
logging.basicConfig(level=logging.INFO)

# Environment Variables
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
ADMIN_ID = os.getenv("TELEGRAM_ADMIN_ID", "").strip()
SUPABASE_URL = os.getenv("SUPABASE_URL", "").strip().rstrip("/")
SUPABASE_SECRET_KEY = os.getenv("SUPABASE_SECRET_KEY", "").strip()

# Telegram / Supabase settings
TELEGRAM_API = f"https://api.telegram.org/bot{BOT_TOKEN}"
NOTIFICATIONS_URL = f"{SUPABASE_URL}/rest/v1/notifications"

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
                {"text": "📊 အခြေအနေ", "callback_data": "status"},
                {"text": "➕ Noti ရေးရန်", "callback_data": "create"}
            ],
            [
                {"text": "📋 Noti ကြည့်ရန်", "callback_data": "list"},
                {"text": "🗑️ Noti ဖျက်ရန်", "callback_data": "delete"}
            ],
            [
                {"text": "❌ လုပ်ဆောင်ချက် ပယ်ဖျက်ရန်",
                 "callback_data": "cancel"}
            ]
        ]
    }


def supabase_headers(extra=None):
    if not SUPABASE_URL or not SUPABASE_SECRET_KEY:
        raise RuntimeError("Supabase environment variables are missing")

    headers = {
        "apikey": SUPABASE_SECRET_KEY,
        "Authorization": f"Bearer {SUPABASE_SECRET_KEY}",
        "Content-Type": "application/json"
    }

    if extra:
        headers.update(extra)

    return headers


def get_notifications(limit=10):
    response = requests.get(
        NOTIFICATIONS_URL,
        headers=supabase_headers(),
        params={
            "select": "id,title,message,created_at",
            "order": "id.desc",
            "limit": limit
        },
        timeout=15
    )
    response.raise_for_status()
    return response.json()


def create_notification(title, message):
    response = requests.post(
        NOTIFICATIONS_URL,
        headers=supabase_headers({
            "Prefer": "return=representation"
        }),
        json={
            "title": title,
            "message": message
        },
        timeout=15
    )
    response.raise_for_status()
    return response.json()


def delete_notification(notification_id):
    response = requests.delete(
        NOTIFICATIONS_URL,
        headers=supabase_headers({
            "Prefer": "return=minimal"
        }),
        params={
            "id": f"eq.{notification_id}"
        },
        timeout=15
    )
    response.raise_for_status()


def database_status():
    response = requests.get(
        NOTIFICATIONS_URL,
        headers=supabase_headers({
            "Prefer": "count=exact",
            "Range": "0-0"
        }),
        params={"select": "id"},
        timeout=15
    )
    response.raise_for_status()

    content_range = response.headers.get("Content-Range", "")
    if "/" in content_range:
        count = content_range.rsplit("/", 1)[1]
        if count.isdigit():
            return int(count)

    # Fallback if the server doesn't return Content-Range
    return len(response.json())


def show_notification_list(chat_id, deleting=False):
    items = get_notifications(10)

    if not items:
        send_message(chat_id, "📭 Noti မရှိသေးပါ။",
                     main_menu())
        return

    if deleting:
        keyboard = []
        for item in items:
            title = str(item.get("title") or "Untitled")
            title = title[:35]
            keyboard.append([
                {
                    "text": f"🗑️ {title} (ID: {item['id']})",
                    "callback_data": f"deleteask:{item['id']}"
                }
            ])

        keyboard.append([
            {"text": "⬅️ Menu", "callback_data": "menu"}
        ])

        send_message(
            chat_id,
            "ဖျက်ချင်တဲ့ Noti ကို ရွေးပါ။\n"
            "နောက်ဆုံး Noti ၁၀ ခုကို ပြထားပါတယ်။",
            {"inline_keyboard": keyboard}
        )
        return

    lines = ["📋 **Noti စာရင်း**\n"]

    for item in items:
        title = str(item.get("title") or "")
        message = str(item.get("message") or "")

        # Telegram plain text; don't interpret user text as Markdown.
        lines.append(
            f"ID: {item.get('id')}\n"
            f"Title: {title[:150]}\n"
            f"Message: {message[:250]}\n"
            "──────────────"
        )

    send_message(
        chat_id,
        "\n\n".join(lines),
        main_menu()
    )


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
            send_message(chat_id, "လုပ်ဆောင်ချက် ပယ်ဖျက်ပြီးပါပြီ။",
                         main_menu())

        elif data == "status":
            count = database_status()
            send_message(
                chat_id,
                "📊 စနစ်အခြေအနေ\n\n"
                "🟢 Backend: အသုံးပြုနိုင်သည်\n"
                "🟢 Supabase: ချိတ်ဆက်မှု အောင်မြင်သည်\n"
                f"📨 သိမ်းထားသော Noti: {count} ခု",
                main_menu()
            )

        elif data == "create":
            user_states[str(user_id)] = {"step": "title"}
            send_message(
                chat_id,
                "➕ Noti အသစ်ရေးမယ်။\n\n"
                "အရင်ဆုံး Noti Title ကို ရိုက်ပို့ပါ။\n"
                "ပယ်ဖျက်ရန် /cancel ကို ရိုက်ပါ။"
            )

        elif data == "list":
            show_notification_list(chat_id, deleting=False)

        elif data == "delete":
            show_notification_list(chat_id, deleting=True)

        elif data.startswith("deleteask:"):
            notification_id = data.split(":", 1)[1]

            keyboard = {
                "inline_keyboard": [[
                    {
                        "text": "✅ ဖျက်မယ်",
                        "callback_data": f"deleteyes:{notification_id}"
                    },
                    {
                        "text": "❌ မဖျက်တော့ဘူး",
                        "callback_data": "delete"
                    }
                ]]
            }

            send_message(
                chat_id,
                f"Noti ID {notification_id} ကို တကယ်ဖျက်မှာ သေချာပါသလား?",
                keyboard
            )

        elif data.startswith("deleteyes:"):
            notification_id = data.split(":", 1)[1]

            if not notification_id.isdigit():
                send_message(chat_id, "❌ Noti ID မမှန်ပါ။", main_menu())
                return

            delete_notification(notification_id)
            send_message(
                chat_id,
                f"✅ Noti ID {notification_id} ကို ဖျက်ပြီးပါပြီ။",
                main_menu()
            )

    except Exception:
        logging.exception("Callback operation failed")
        send_message(
            chat_id,
            "⚠️ လုပ်ဆောင်မှု မအောင်မြင်ပါ။ Database Settings နဲ့ "
            "notifications table ကို စစ်ဆေးပါ။",
            main_menu()
        )


def handle_message(update):
    message = update.get("message", {})
    user = message.get("from", {})
    user_id = user.get("id")
    chat_id = message.get("chat", {}).get("id")
    text = message.get("text", "")

    if not chat_id or not user_id:
        return

    if text == "/start":
        if not is_admin(user_id):
            send_message(
                chat_id,
                "⛔ ဒီ Bot ကို Admin သာ အသုံးပြုနိုင်ပါတယ်။"
            )
            return

        user_states.pop(str(user_id), None)
        send_message(
            chat_id,
            "🤖 မင်္ဂလာပါ Admin!\n\n"
            "Notification Management System မှ ကြိုဆိုပါတယ်။\n"
            "အောက်က Menu ကို ရွေးချယ်ပါ။",
            main_menu()
        )
        return

    if not is_admin(user_id):
        send_message(chat_id, "⛔ Admin သာ အသုံးပြုနိုင်ပါတယ်။")
        return

    if text == "/cancel":
        user_states.pop(str(user_id), None)
        send_message(chat_id, "လုပ်ဆောင်ချက် ပယ်ဖျက်ပြီးပါပြီ။",
                     main_menu())
        return

    state = user_states.get(str(user_id))

    if not state:
        send_message(chat_id, "Menu ကို အသုံးပြုရန် /start ကို နှိပ်ပါ။",
                     main_menu())
        return

    if not text.strip():
        send_message(chat_id, "စာသားကို ရိုက်ထည့်ပေးပါ။")
        return

    if state["step"] == "title":
        title = text.strip()

        if len(title) > 200:
            send_message(chat_id, "Title ကို စာလုံး ၂၀၀ ထက်မပိုစေပါနဲ့။")
            return

        state["title"] = title
        state["step"] = "message"

        send_message(
            chat_id,
            "✅ Title ရပါပြီ။\n\n"
            "အခု Noti Message ကို ရိုက်ပို့ပါ။"
        )

    elif state["step"] == "message":
        body = text.strip()

        if len(body) > 3500:
            send_message(chat_id, "Message ကို စာလုံး ၃၅၀၀ ထက်မပိုစေပါနဲ့။")
            return

        state["message"] = body
        state["step"] = "confirm"

        keyboard = {
            "inline_keyboard": [
                [
                    {"text": "✅ သိမ်းမယ်", "callback_data": "save"},
                    {"text": "❌ မသိမ်းတော့ဘူး", "callback_data": "cancel"}
                ]
            ]
        }

        send_message(
            chat_id,
            "📝 Noti ကို စစ်ဆေးပါ\n\n"
            f"Title: {state['title']}\n\n"
            f"Message:\n{state['message']}\n\n"
            "Supabase Database ထဲ သိမ်းမှာ သေချာပါသလား?",
            keyboard
        )


def handle_update(update):
    if "callback_query" in update:
        # Save is handled here so it can access the admin's pending draft.
        query = update["callback_query"]
        data = query.get("data", "")
        user_id = query.get("from", {}).get("id")
        chat_id = query.get("message", {}).get("chat", {}).get("id")

        if data == "save":
            telegram("answerCallbackQuery", {
                "callback_query_id": query["id"]
            })

            if not is_admin(user_id):
                if chat_id:
                    send_message(chat_id, "⛔ Admin သာ အသုံးပြုနိုင်ပါတယ်။")
                return

            state = user_states.get(str(user_id), {})

            if state.get("step") != "confirm":
                send_message(chat_id, "Draft မတွေ့ပါ။ /start မှ ပြန်စပါ။",
                             main_menu())
                return

            try:
                created = create_notification(
                    state["title"],
                    state["message"]
                )
                user_states.pop(str(user_id), None)

                new_id = created[0].get("id", "?") if created else "?"
                send_message(
                    chat_id,
                    f"✅ Noti သိမ်းပြီးပါပြီ။\nID: {new_id}",
                    main_menu()
                )
            except Exception:
                logging.exception("Save notification failed")
                send_message(
                    chat_id,
                    "❌ Noti သိမ်းမရပါ။ Supabase URL၊ Secret Key နဲ့ "
                    "Table columns တွေကို စစ်ဆေးပါ။",
                    main_menu()
                )
            return

        handle_callback(update)

    elif "message" in update:
        handle_message(update)


def polling_loop():
    offset = None

    while True:
        try:
            if not BOT_TOKEN:
                logging.error("TELEGRAM_BOT_TOKEN is not configured")
                time.sleep(10)
                continue

            response = requests.get(
                f"{TELEGRAM_API}/getUpdates",
                params={
                    "timeout": 25,
                    "offset": offset
                },
                timeout=35
            )
            response.raise_for_status()
            result = response.json()

            if not result.get("ok"):
                raise RuntimeError(str(result))

            for update in result.get("result", []):
                offset = update["update_id"] + 1

                try:
                    handle_update(update)
                except Exception:
                    logging.exception("Telegram update failed")

        except Exception:
            logging.exception("Telegram polling error")
            time.sleep(3)


@app.get("/")
def home():
    return jsonify({
        "status": "ok",
        "service": "Telegram Notification Backend"
    })


@app.get("/health")
def health():
    return jsonify({"status": "ok"})


# Start one Telegram polling thread per application process.
if not polling_started:
    polling_started = True
    threading.Thread(
        target=polling_loop,
        daemon=True,
        name="telegram-poller"
    ).start()
