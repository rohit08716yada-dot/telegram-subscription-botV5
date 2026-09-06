import os
import json
import time
import hmac
import hashlib
import logging
from datetime import datetime, timedelta, timezone
from threading import Thread, Lock

import telebot
from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton
from pymongo import MongoClient, ASCENDING
from pymongo.errors import DuplicateKeyError
from apscheduler.schedulers.background import BackgroundScheduler
from flask import Flask, request, jsonify
import razorpay

# ============================================================
# CONFIG
# ============================================================

def env_required(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise RuntimeError(f"Missing environment variable: {name}")
    return value.strip()

BOT_TOKEN = env_required("BOT_TOKEN")
MONGO_URI = env_required("MONGO_URI")
RAZORPAY_KEY_ID = env_required("RAZORPAY_KEY_ID")
RAZORPAY_KEY_SECRET = env_required("RAZORPAY_KEY_SECRET")
RAZORPAY_WEBHOOK_SECRET = env_required("RAZORPAY_WEBHOOK_SECRET")

try:
    ADMIN_ID = int(env_required("ADMIN_ID"))
except ValueError:
    raise RuntimeError("ADMIN_ID must be a numeric Telegram user ID.")

CONTACT_USERNAME = os.getenv("CONTACT_USERNAME", "").strip().lstrip("@")
PORT = int(os.getenv("PORT", "5000"))

DB_NAME = os.getenv("MONGO_DB_NAME", "sub_management")
EXPIRY_CHECK_SECONDS = max(30, int(os.getenv("EXPIRY_CHECK_SECONDS", "60")))

# ============================================================
# LOGGING
# ============================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s"
)
logger = logging.getLogger("subscription_bot")

# ============================================================
# TELEGRAM / MONGO / RAZORPAY
# ============================================================

bot = telebot.TeleBot(BOT_TOKEN, parse_mode="HTML")
razorpay_client = razorpay.Client(auth=(RAZORPAY_KEY_ID, RAZORPAY_KEY_SECRET))

mongo_client = MongoClient(MONGO_URI, serverSelectionTimeoutMS=10000)
db = mongo_client[DB_NAME]

channels_col = db["channels"]
users_col = db["subscriptions"]
orders_col = db["orders"]
events_col = db["webhook_events"]

# Unique indexes make duplicate activations much harder.
channels_col.create_index([("channel_id", ASCENDING)], unique=True)
users_col.create_index(
    [("user_id", ASCENDING), ("channel_id", ASCENDING)],
    unique=True
)
orders_col.create_index([("razorpay_payment_link_id", ASCENDING)], unique=True)
orders_col.create_index([("reference_id", ASCENDING)], unique=True)
events_col.create_index([("event_id", ASCENDING)], unique=True)

state_lock = Lock()

# ============================================================
# FLASK / RENDER
# ============================================================

app = Flask(__name__)

@app.get("/")
def health():
    return jsonify({
        "ok": True,
        "service": "telegram-subscription-bot",
        "time": datetime.now(timezone.utc).isoformat()
    })

@app.get("/health")
def health2():
    return "OK", 200

# ============================================================
# HELPERS
# ============================================================

def utcnow():
    return datetime.now(timezone.utc)

def ts(dt: datetime) -> int:
    return int(dt.timestamp())

def safe_username(username: str) -> str:
    return username.lstrip("@") if username else ""

def contact_button(markup: InlineKeyboardMarkup):
    if CONTACT_USERNAME:
        markup.add(
            InlineKeyboardButton(
                "📞 Contact Admin",
                url=f"https://t.me/{safe_username(CONTACT_USERNAME)}"
            )
        )

def admin_only(user_id: int) -> bool:
    return user_id == ADMIN_ID

def plan_label(minutes: int) -> str:
    if minutes < 60:
        return f"{minutes} min"
    if minutes % 1440 == 0:
        days = minutes // 1440
        return f"{days} day" if days == 1 else f"{days} days"
    if minutes % 60 == 0:
        hours = minutes // 60
        return f"{hours} hour" if hours == 1 else f"{hours} hours"
    return f"{minutes} min"

def make_start_link(channel_id: int) -> str:
    username = bot.get_me().username
    return f"https://t.me/{username}?start=ch_{channel_id}"

def parse_plans(raw: str):
    if not raw or not raw.strip():
        raise ValueError("Plans cannot be empty.")

    plans = {}
    parts = raw.split(",")

    for part in parts:
        part = part.strip()
        if not part:
            continue

        if ":" not in part:
            raise ValueError("Every plan must be Minutes:Price.")

        minutes_text, price_text = part.split(":", 1)
        minutes_text = minutes_text.strip()
        price_text = price_text.strip()

        if not minutes_text.isdigit():
            raise ValueError(f"Invalid minutes: {minutes_text}")

        # Price can be integer rupees for this bot.
        if not price_text.isdigit():
            raise ValueError(f"Invalid price: {price_text}")

        minutes = int(minutes_text)
        price = int(price_text)

        if minutes <= 0:
            raise ValueError("Minutes must be greater than 0.")
        if price <= 0:
            raise ValueError("Price must be greater than 0.")
        if price > 10000000:
            raise ValueError("Price is too large.")

        plans[str(minutes)] = price

    if not plans:
        raise ValueError("No valid plans found.")

    return dict(sorted(plans.items(), key=lambda x: int(x[0])))

def build_plan_keyboard(channel_id: int, plans: dict):
    markup = InlineKeyboardMarkup(row_width=1)

    for minutes_text, price in plans.items():
        minutes = int(minutes_text)
        # callback data is intentionally compact
        markup.add(
            InlineKeyboardButton(
                f"💳 {plan_label(minutes)} — ₹{price}",
                callback_data=f"buy:{channel_id}:{minutes}"
            )
        )

    contact_button(markup)
    return markup

def send_or_edit(message, text, markup=None):
    try:
        bot.edit_message_text(
            text,
            message.chat.id,
            message.message_id,
            reply_markup=markup,
            parse_mode="HTML"
        )
    except Exception:
        bot.send_message(
            message.chat.id,
            text,
            reply_markup=markup,
            parse_mode="HTML"
        )

def get_channel(channel_id: int):
    return channels_col.find_one({
        "channel_id": channel_id,
        "admin_id": ADMIN_ID
    })

# ============================================================
# SUBSCRIPTION ACTIVATION
# ============================================================

def activate_subscription(user_id: int, channel_id: int, minutes: int,
                          payment_link_id: str, payment_id: str = ""):
    channel = get_channel(channel_id)
    if not channel:
        raise ValueError("Channel configuration no longer exists.")

    now = utcnow()

    existing = users_col.find_one({
        "user_id": user_id,
        "channel_id": channel_id
    })

    if existing and existing.get("expiry"):
        old_expiry = existing["expiry"]
        if isinstance(old_expiry, datetime):
            base = max(old_expiry, now)
        else:
            base = now
    else:
        base = now

    new_expiry = base + timedelta(minutes=minutes)

    # Try to create a one-user invite that expires at subscription expiry.
    invite = bot.create_chat_invite_link(
        chat_id=channel_id,
        name=f"sub-{user_id}"[:32],
        expire_date=ts(new_expiry),
        member_limit=1
    )

    update = {
        "user_id": user_id,
        "channel_id": channel_id,
        "expiry": new_expiry,
        "status": "active",
        "last_payment_link_id": payment_link_id,
        "last_payment_id": payment_id,
        "updated_at": now,
        "created_at": existing.get("created_at", now) if existing else now
    }

    users_col.update_one(
        {"user_id": user_id, "channel_id": channel_id},
        {"$set": update},
        upsert=True
    )

    return new_expiry, invite.invite_link

def process_paid_order(order_doc: dict, payment_id: str = ""):
    """
    Idempotent activation:
    1. Verify order exists and is paid.
    2. Mark order processed atomically.
    3. Activate subscription once.
    """
    link_id = order_doc.get("razorpay_payment_link_id")
    if not link_id:
        raise ValueError("Missing Razorpay payment link ID.")

    with state_lock:
        current = orders_col.find_one({"razorpay_payment_link_id": link_id})
        if not current:
            raise ValueError("Order record not found.")

        if current.get("processed") is True:
            return current.get("expiry"), current.get("invite_link"), True

        user_id = int(current["user_id"])
        channel_id = int(current["channel_id"])
        minutes = int(current["minutes"])

        expiry, invite_link = activate_subscription(
            user_id=user_id,
            channel_id=channel_id,
            minutes=minutes,
            payment_link_id=link_id,
            payment_id=payment_id
        )

        orders_col.update_one(
            {"razorpay_payment_link_id": link_id},
            {
                "$set": {
                    "processed": True,
                    "status": "paid",
                    "payment_id": payment_id,
                    "expiry": expiry,
                    "invite_link": invite_link,
                    "processed_at": utcnow()
                }
            }
        )

    channel = get_channel(channel_id)
    channel_name = channel["name"] if channel else "channel"

    markup = InlineKeyboardMarkup()
    markup.add(
        InlineKeyboardButton("🚀 Join Channel", url=invite_link)
    )
    markup.add(
        InlineKeyboardButton(
            "🔄 Renew",
            url=make_start_link(channel_id)
        )
    )

    bot.send_message(
        user_id,
        f"✅ <b>Payment Successful</b>\n\n"
        f"📢 <b>{channel_name}</b>\n"
        f"⏱ Plan: <b>{plan_label(minutes)}</b>\n"
        f"📅 Expires: <b>{expiry.strftime('%d %b %Y, %I:%M %p UTC')}</b>\n\n"
        f"Tap below to join:",
        reply_markup=markup
    )

    bot.send_message(
        ADMIN_ID,
        f"💰 <b>Payment Received</b>\n\n"
        f"User ID: <code>{user_id}</code>\n"
        f"Channel: <b>{channel_name}</b>\n"
        f"Plan: <b>{plan_label(minutes)}</b>\n"
        f"Expires: <b>{expiry.strftime('%d %b %Y, %I:%M %p UTC')}</b>"
    )

    return expiry, invite_link, False

# ============================================================
# RAZORPAY WEBHOOK
# ============================================================

def verify_razorpay_webhook(raw_body: bytes, signature: str) -> bool:
    if not signature:
        return False

    expected = hmac.new(
        RAZORPAY_WEBHOOK_SECRET.encode("utf-8"),
        raw_body,
        hashlib.sha256
    ).hexdigest()

    return hmac.compare_digest(expected, signature)

@app.post("/razorpay/webhook")
def razorpay_webhook():
    raw_body = request.get_data()
    signature = request.headers.get("X-Razorpay-Signature", "")
    event_id = request.headers.get("x-razorpay-event-id", "")

    if not verify_razorpay_webhook(raw_body, signature):
        logger.warning("Rejected webhook: invalid signature")
        return jsonify({"ok": False}), 401

    # Razorpay recommends using event ID to handle duplicate deliveries.
    if event_id:
        try:
            events_col.insert_one({
                "event_id": event_id,
                "received_at": utcnow()
            })
        except DuplicateKeyError:
            return jsonify({"ok": True, "duplicate": True}), 200

    try:
        payload = json.loads(raw_body.decode("utf-8"))
        event = payload.get("event", "")

        if event == "payment_link.paid":
            pl_entity = (
                payload.get("payload", {})
                .get("payment_link", {})
                .get("entity", {})
            )

            payment_entity = (
                payload.get("payload", {})
                .get("payment", {})
                .get("entity", {})
            )

            link_id = pl_entity.get("id")
            payment_id = payment_entity.get("id", "")

            if not link_id:
                logger.error("payment_link.paid without payment link ID")
                return jsonify({"ok": False}), 400

            order_doc = orders_col.find_one({
                "razorpay_payment_link_id": link_id
            })

            if not order_doc:
                logger.error("Unknown payment link: %s", link_id)
                return jsonify({"ok": False}), 404

            # Server-side amount check.
            paid_amount = payment_entity.get("amount")
            expected_amount = int(order_doc["amount_paise"])

            if paid_amount is not None and int(paid_amount) != expected_amount:
                logger.error(
                    "Amount mismatch for %s: paid=%s expected=%s",
                    link_id, paid_amount, expected_amount
                )
                orders_col.update_one(
                    {"razorpay_payment_link_id": link_id},
                    {"$set": {"status": "amount_mismatch"}}
                )
                return jsonify({"ok": False}), 400

            process_paid_order(order_doc, payment_id)
            return jsonify({"ok": True}), 200

        if event in ("payment_link.cancelled", "payment_link.expired"):
            pl_entity = (
                payload.get("payload", {})
                .get("payment_link", {})
                .get("entity", {})
            )
            link_id = pl_entity.get("id")
            if link_id:
                orders_col.update_one(
                    {"razorpay_payment_link_id": link_id},
                    {"$set": {"status": event}}
                )
            return jsonify({"ok": True}), 200

        return jsonify({"ok": True, "ignored": event}), 200

    except Exception:
        logger.exception("Webhook processing failed")
        # 500 makes Razorpay retry the webhook.
        return jsonify({"ok": False}), 500

# ============================================================
# RAZORPAY PAYMENT LINK CREATION
# ============================================================

def create_payment_link(user, channel_id: int, minutes: int, price: int):
    channel = get_channel(channel_id)
    if not channel:
        raise ValueError("Channel not found.")

    # Prevent accidental duplicate pending links for the same user/channel/plan.
    existing = orders_col.find_one({
        "user_id": user.id,
        "channel_id": channel_id,
        "minutes": minutes,
        "processed": {"$ne": True},
        "status": {"$in": ["created", "pending"]}
    })

    if existing and existing.get("short_url"):
        return existing["short_url"], existing["razorpay_payment_link_id"]

    reference_id = f"tg{user.id}_{channel_id}_{minutes}_{int(time.time())}"
    amount_paise = price * 100

    notes = {
        "telegram_user_id": str(user.id),
        "telegram_channel_id": str(channel_id),
        "subscription_minutes": str(minutes)
    }

    data = {
        "amount": amount_paise,
        "currency": "INR",
        "accept_partial": False,
        "description": f"{channel['name']} - {plan_label(minutes)}",
        "reference_id": reference_id[:40],
        "expire_by": int((utcnow() + timedelta(hours=2)).timestamp()),
        "notes": notes
    }

    if user.username:
        data["customer"] = {
            "name": user.first_name or "Telegram User",
            "email": f"telegram{user.id}@example.invalid",
            "contact": ""
        }

    link = razorpay_client.payment_link.create(data=data)

    link_id = link["id"]
    short_url = link["short_url"]

    orders_col.insert_one({
        "reference_id": reference_id[:40],
        "razorpay_payment_link_id": link_id,
        "short_url": short_url,
        "user_id": user.id,
        "channel_id": channel_id,
        "minutes": minutes,
        "price": price,
        "amount_paise": amount_paise,
        "currency": "INR",
        "status": "created",
        "processed": False,
        "created_at": utcnow()
    })

    return short_url, link_id

# ============================================================
# USER START
# ============================================================

@bot.message_handler(commands=["start"])
def start_handler(message):
    user_id = message.from_user.id
    parts = message.text.split(maxsplit=1)

    if len(parts) == 2 and parts[1].startswith("ch_"):
        try:
            channel_id = int(parts[1][3:])
        except ValueError:
            channel_id = None

        if channel_id is not None:
            channel = get_channel(channel_id)

            if channel:
                markup = build_plan_keyboard(
                    channel_id,
                    channel.get("plans", {})
                )

                bot.send_message(
                    message.chat.id,
                    f"👋 <b>Welcome</b>\n\n"
                    f"📢 Channel: <b>{channel['name']}</b>\n\n"
                    f"Select your subscription plan:",
                    reply_markup=markup
                )
                return

    if admin_only(user_id):
        admin_panel(message.chat.id)
    else:
        bot.send_message(
            message.chat.id,
            "👋 Welcome!\n\n"
            "Please open the subscription link provided by the channel admin."
        )

# ============================================================
# ADMIN PANEL
# ============================================================

def admin_panel(chat_id):
    markup = InlineKeyboardMarkup(row_width=2)
    markup.add(
        InlineKeyboardButton("📢 Channels", callback_data="admin:channels"),
        InlineKeyboardButton("📊 Stats", callback_data="admin:stats")
    )
    markup.add(
        InlineKeyboardButton("➕ Add Channel", callback_data="admin:add")
    )

    bot.send_message(
        chat_id,
        "🛠 <b>Admin Panel</b>\n\n"
        "/add — Add/update channel\n"
        "/channels — Manage channels\n"
        "/stats — Statistics\n"
        "/users — Active subscriptions",
        reply_markup=markup
    )

@bot.message_handler(commands=["channels"])
def channels_command(message):
    if not admin_only(message.from_user.id):
        return

    list_channels(message.chat.id)

@bot.message_handler(commands=["stats"])
def stats_command(message):
    if not admin_only(message.from_user.id):
        return

    active = users_col.count_documents({
        "status": "active",
        "expiry": {"$gt": utcnow()}
    })
    channels = channels_col.count_documents({"admin_id": ADMIN_ID})
    orders = orders_col.count_documents({"status": "paid"})
    pending = orders_col.count_documents({
        "status": {"$in": ["created", "pending"]},
        "processed": False
    })

    bot.send_message(
        message.chat.id,
        f"📊 <b>Statistics</b>\n\n"
        f"📢 Channels: <b>{channels}</b>\n"
        f"👥 Active subscriptions: <b>{active}</b>\n"
        f"💰 Paid orders: <b>{orders}</b>\n"
        f"⏳ Pending payments: <b>{pending}</b>"
    )

@bot.message_handler(commands=["users"])
def users_command(message):
    if not admin_only(message.from_user.id):
        return

    cursor = users_col.find({
        "status": "active",
        "expiry": {"$gt": utcnow()}
    }).sort("expiry", ASCENDING).limit(50)

    lines = ["👥 <b>Active subscriptions</b>\n"]
    count = 0

    for sub in cursor:
        count += 1
        lines.append(
            f"• <code>{sub['user_id']}</code> | "
            f"<code>{sub['channel_id']}</code> | "
            f"{sub['expiry'].strftime('%d-%m-%Y %H:%M UTC')}"
        )

    if count == 0:
        lines.append("No active subscriptions.")

    bot.send_message(message.chat.id, "\n".join(lines))

@bot.message_handler(commands=["add"])
def add_channel_command(message):
    if not admin_only(message.from_user.id):
        return

    msg = bot.send_message(
        ADMIN_ID,
        "📢 <b>Add / Update Channel</b>\n\n"
        "1. Make this bot an administrator in your private channel.\n"
        "2. Forward any message from that channel to this bot.\n\n"
        "The bot needs permission to manage invite links and members."
    )
    bot.register_next_step_handler(msg, get_forwarded_channel)

def list_channels(chat_id):
    markup = InlineKeyboardMarkup(row_width=1)
    cursor = channels_col.find({"admin_id": ADMIN_ID})
    count = 0

    for ch in cursor:
        count += 1
        markup.add(
            InlineKeyboardButton(
                f"📢 {ch['name']}",
                callback_data=f"manage:{ch['channel_id']}"
            )
        )

    markup.add(
        InlineKeyboardButton("➕ Add New Channel", callback_data="admin:add")
    )

    bot.send_message(
        chat_id,
        "📢 <b>Your Channels</b>\n\n"
        + ("Select a channel:" if count else "No channels configured."),
        reply_markup=markup
    )

def get_forwarded_channel(message):
    forwarded = message.forward_from_chat

    if not forwarded:
        bot.send_message(
            ADMIN_ID,
            "❌ I couldn't detect a forwarded channel message.\n"
            "Please use /add and forward a message directly from the channel."
        )
        return

    if forwarded.type != "channel":
        bot.send_message(ADMIN_ID, "❌ That message is not from a channel.")
        return

    channel_id = forwarded.id
    channel_name = forwarded.title or "Unnamed Channel"

    # Check bot permissions.
    try:
        me = bot.get_me()
        member = bot.get_chat_member(channel_id, me.id)

        if member.status not in ("administrator", "creator"):
            bot.send_message(
                ADMIN_ID,
                "❌ Bot is not an administrator in this channel."
            )
            return

    except Exception as e:
        logger.exception("Channel permission check failed")
        bot.send_message(
            ADMIN_ID,
            f"❌ Cannot access the channel.\n\n"
            f"Make sure the bot is an admin.\n"
            f"Error: <code>{str(e)[:500]}</code>"
        )
        return

    msg = bot.send_message(
        ADMIN_ID,
        f"✅ Channel detected: <b>{channel_name}</b>\n\n"
        "Send plans like:\n"
        "<code>1440:49, 43200:129, 129600:299</code>\n\n"
        "Format = <b>Minutes:Price</b>"
    )

    bot.register_next_step_handler(
        msg,
        finalize_channel,
        channel_id,
        channel_name
    )

def finalize_channel(message, channel_id, channel_name):
    try:
        plans = parse_plans(message.text or "")

        channels_col.update_one(
            {"channel_id": channel_id},
            {
                "$set": {
                    "channel_id": channel_id,
                    "name": channel_name,
                    "plans": plans,
                    "admin_id": ADMIN_ID,
                    "updated_at": utcnow()
                },
                "$setOnInsert": {
                    "created_at": utcnow()
                }
            },
            upsert=True
        )

        link = make_start_link(channel_id)

        bot.send_message(
            ADMIN_ID,
            f"✅ <b>Channel saved successfully.</b>\n\n"
            f"📢 {channel_name}\n"
            f"🔗 <code>{link}</code>\n\n"
            f"Plans:\n" +
            "\n".join(
                f"• {plan_label(int(m))} — ₹{p}"
                for m, p in plans.items()
            )
        )

    except Exception as e:
        logger.exception("Plan parsing failed")
        bot.send_message(
            ADMIN_ID,
            "❌ Invalid plan format.\n\n"
            "Use:\n"
            "<code>1440:49, 43200:129, 129600:299</code>\n\n"
            f"Error: <code>{str(e)[:300]}</code>"
        )

# ============================================================
# CALLBACKS
# ============================================================

@bot.callback_query_handler(func=lambda call: call.data == "admin:add")
def callback_admin_add(call):
    if not admin_only(call.from_user.id):
        bot.answer_callback_query(call.id, "Not authorized.", show_alert=True)
        return

    bot.answer_callback_query(call.id)
    msg = bot.send_message(
        ADMIN_ID,
        "📢 Forward any message from the channel."
    )
    bot.register_next_step_handler(msg, get_forwarded_channel)

@bot.callback_query_handler(func=lambda call: call.data == "admin:channels")
def callback_admin_channels(call):
    if not admin_only(call.from_user.id):
        bot.answer_callback_query(call.id, "Not authorized.", show_alert=True)
        return

    bot.answer_callback_query(call.id)
    list_channels(call.message.chat.id)

@bot.callback_query_handler(func=lambda call: call.data == "admin:stats")
def callback_admin_stats(call):
    if not admin_only(call.from_user.id):
        bot.answer_callback_query(call.id, "Not authorized.", show_alert=True)
        return

    bot.answer_callback_query(call.id)

    active = users_col.count_documents({
        "status": "active",
        "expiry": {"$gt": utcnow()}
    })
    total = users_col.count_documents({})
    paid = orders_col.count_documents({"status": "paid"})

    bot.send_message(
        ADMIN_ID,
        f"📊 <b>Stats</b>\n\n"
        f"Active: <b>{active}</b>\n"
        f"Subscriptions ever created: <b>{total}</b>\n"
        f"Paid orders: <b>{paid}</b>"
    )

@bot.callback_query_handler(func=lambda call: call.data.startswith("manage:"))
def callback_manage(call):
    if not admin_only(call.from_user.id):
        bot.answer_callback_query(call.id, "Not authorized.", show_alert=True)
        return

    try:
        channel_id = int(call.data.split(":", 1)[1])
    except Exception:
        bot.answer_callback_query(call.id, "Invalid channel.", show_alert=True)
        return

    channel = get_channel(channel_id)
    if not channel:
        bot.answer_callback_query(call.id, "Channel not found.", show_alert=True)
        return

    bot.answer_callback_query(call.id)

    markup = InlineKeyboardMarkup(row_width=1)
    markup.add(
        InlineKeyboardButton(
            "🔗 User Subscription Link",
            url=make_start_link(channel_id)
        )
    )

    plans_text = "\n".join(
        f"• {plan_label(int(m))} — ₹{p}"
        for m, p in channel.get("plans", {}).items()
    )

    bot.send_message(
        ADMIN_ID,
        f"⚙️ <b>{channel['name']}</b>\n\n"
        f"{plans_text}\n\n"
        f"To change plans, use /add and forward this channel again.",
        reply_markup=markup
    )

@bot.callback_query_handler(func=lambda call: call.data.startswith("buy:"))
def callback_buy(call):
    try:
        _, channel_text, minutes_text = call.data.split(":", 2)
        channel_id = int(channel_text)
        minutes = int(minutes_text)
    except Exception:
        bot.answer_callback_query(call.id, "Invalid request.", show_alert=True)
        return

    channel = get_channel(channel_id)
    if not channel:
        bot.answer_callback_query(call.id, "Channel unavailable.", show_alert=True)
        return

    price = channel.get("plans", {}).get(str(minutes))
    if price is None:
        bot.answer_callback_query(call.id, "Plan unavailable.", show_alert=True)
        return

    # If user already has active subscription, tell them current expiry.
    existing = users_col.find_one({
        "user_id": call.from_user.id,
        "channel_id": channel_id
    })

    bot.answer_callback_query(call.id)

    try:
        url, link_id = create_payment_link(
            call.from_user,
            channel_id,
            minutes,
            int(price)
        )

        markup = InlineKeyboardMarkup(row_width=1)
        markup.add(
            InlineKeyboardButton("💳 Pay with Razorpay", url=url)
        )
        markup.add(
            InlineKeyboardButton(
                "🔄 Check Payment",
                callback_data=f"check:{link_id}"
            )
        )
        contact_button(markup)

        extra = ""
        if existing and existing.get("expiry") and existing["expiry"] > utcnow():
            extra = (
                f"\n\nℹ️ Your current subscription expires at "
                f"<b>{existing['expiry'].strftime('%d %b %Y, %I:%M %p UTC')}</b>."
                f"\nThis purchase will be added after the current expiry."
            )

        bot.send_message(
            call.message.chat.id,
            f"💳 <b>Razorpay Checkout</b>\n\n"
            f"📢 {channel['name']}\n"
            f"⏱ Plan: <b>{plan_label(minutes)}</b>\n"
            f"💰 Amount: <b>₹{price}</b>"
            f"{extra}\n\n"
            "Tap <b>Pay with Razorpay</b> and complete the payment.\n"
            "After Razorpay confirms the payment, the bot will automatically activate your subscription.",
            reply_markup=markup
        )

    except Exception as e:
        logger.exception("Payment link creation failed")
        bot.send_message(
            call.message.chat.id,
            "❌ Payment link could not be created right now.\n"
            "Please try again later."
        )
        bot.send_message(
            ADMIN_ID,
            f"⚠️ Razorpay error:\n<code>{str(e)[:1000]}</code>"
        )

@bot.callback_query_handler(func=lambda call: call.data.startswith("check:"))
def callback_check_payment(call):
    link_id = call.data.split(":", 1)[1]

    order = orders_col.find_one({
        "razorpay_payment_link_id": link_id,
        "user_id": call.from_user.id
    })

    if not order:
        bot.answer_callback_query(
            call.id, "Payment record not found.", show_alert=True
        )
        return

    try:
        link = razorpay_client.payment_link.fetch(link_id)
        status = link.get("status", "")

        if status == "paid" and not order.get("processed"):
            process_paid_order(order)
            bot.answer_callback_query(
                call.id,
                "Payment verified. Check your messages.",
                show_alert=True
            )
        elif order.get("processed"):
            bot.answer_callback_query(
                call.id,
                "Already activated.",
                show_alert=True
            )
        else:
            bot.answer_callback_query(
                call.id,
                f"Payment status: {status or 'pending'}",
                show_alert=True
            )

    except Exception:
        logger.exception("Payment check failed")
        bot.answer_callback_query(
            call.id,
            "Could not check payment right now.",
            show_alert=True
        )

# ============================================================
# EXPIRY / KICK
# ============================================================

def kick_expired_users():
    now = utcnow()

    cursor = users_col.find({
        "status": "active",
        "expiry": {"$lte": now}
    }).limit(100)

    for sub in cursor:
        user_id = int(sub["user_id"])
        channel_id = int(sub["channel_id"])

        try:
            # Revoke old invite if possible.
            old_link = sub.get("invite_link")
            if old_link:
                try:
                    bot.revoke_chat_invite_link(channel_id, old_link)
                except Exception:
                    pass

            # Ban + immediately unban prevents the expired user from
            # remaining in the private channel and lets them renew later.
            try:
                bot.ban_chat_member(channel_id, user_id)
                bot.unban_chat_member(channel_id, user_id)
            except Exception:
                logger.exception(
                    "Could not remove user %s from channel %s",
                    user_id, channel_id
                )

            users_col.update_one(
                {"_id": sub["_id"]},
                {
                    "$set": {
                        "status": "expired",
                        "expired_at": now,
                        "updated_at": now
                    }
                }
            )

            markup = InlineKeyboardMarkup()
            markup.add(
                InlineKeyboardButton(
                    "🔄 Renew Subscription",
                    url=make_start_link(channel_id)
                )
            )

            bot.send_message(
                user_id,
                "⚠️ <b>Your subscription has expired.</b>\n\n"
                "Your channel access has been removed.\n"
                "You can renew your subscription using the button below.",
                reply_markup=markup
            )

        except Exception:
            logger.exception(
                "Expiry worker failed for user=%s channel=%s",
                user_id, channel_id
            )

# ============================================================
# STARTUP
# ============================================================

def run_web():
    # Render supplies PORT.
    app.run(
        host="0.0.0.0",
        port=PORT,
        threaded=True
    )

def keep_alive():
    thread = Thread(target=run_web, daemon=True)
    thread.start()

def startup_checks():
    mongo_client.admin.command("ping")
    me = bot.get_me()

    logger.info("Telegram bot: @%s", me.username)
    logger.info("MongoDB connection: OK")
    logger.info("Razorpay client: configured")

if __name__ == "__main__":
    startup_checks()
    keep_alive()

    scheduler = BackgroundScheduler(timezone="UTC")
    scheduler.add_job(
        kick_expired_users,
        "interval",
        seconds=EXPIRY_CHECK_SECONDS,
        id="expiry_worker",
        max_instances=1,
        coalesce=True,
        misfire_grace_time=30
    )
    scheduler.start()

    bot.remove_webhook()

    logger.info("Bot is running...")
    bot.infinity_polling(
        timeout=30,
        long_polling_timeout=30,
        allowed_updates=["message", "callback_query"]
    )
