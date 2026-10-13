import os
import socket
import ipaddress
import re
import json
import threading
import time
import urllib.request
from urllib.parse import urlparse
from dotenv import load_dotenv
from telegram import Update, ReplyKeyboardMarkup, KeyboardButton, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, MessageHandler, ContextTypes, filters, CallbackQueryHandler
import mysql.connector
import logging

from .multipart import assemble_inbox_rows


# Load .env
load_dotenv()

# Minimal logging setup for this module. Honor LOG_LEVEL env var, default to INFO.
LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO").upper()
logging.basicConfig(
    level=LOG_LEVEL,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
)
logger = logging.getLogger(__name__)

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")
TELEGRAM_MASKAN_CHAT_ID = os.environ.get("TELEGRAM_MASKAN_CHAT_ID")

# App context info (for verification)
DB_HOST = os.environ.get("DB_HOST", "localhost")
DB_NAME = os.environ.get("DB_NAME", "")
DB_USER = os.environ.get("DB_USER")
DB_PASSWORD = os.environ.get("DB_PASSWORD")
SENT_IDS_FILE = os.environ.get("SENT_IDS_FILE") or os.path.join(os.path.dirname(__file__), "message_ids.txt")


def get_db_connection():
    """Establishes a connection to the MySQL database."""
    try:
        conn = mysql.connector.connect(
            host=DB_HOST,
            user=DB_USER,
            password=DB_PASSWORD,
            database=DB_NAME
        )
        return conn
    except mysql.connector.Error as err:
        logger.error("Error connecting to database: %s", err)
        return None


def mark_message_as_read(message_id: int) -> bool:
    """Mark a specific message ID as read in the database."""
    conn = get_db_connection()
    if not conn:
        return False
    cursor = conn.cursor()
    try:
        cursor.execute("UPDATE inbox SET Processed = 'true' WHERE ID = %s AND Processed = 'false'", (message_id,))
        conn.commit()
        return cursor.rowcount > 0
    except mysql.connector.Error as err:
        logger.error("Error updating message %s: %s", message_id, err)
        return False
    finally:
        cursor.close()
        conn.close()


def delete_message(message_id: int) -> bool:
    """Deletes a specific message ID from the database."""
    conn = get_db_connection()
    if not conn:
        return False
    cursor = conn.cursor()
    try:
        cursor.execute("DELETE FROM inbox WHERE ID = %s", (message_id,))
        conn.commit()
        return cursor.rowcount > 0
    except mysql.connector.Error as err:
        logger.error("Error deleting message %s: %s", message_id, err)
        return False
    finally:
        cursor.close()
        conn.close()


def remove_sent_ids(ids_to_remove):
    """Removes specified IDs from the sent IDs cache file."""
    try:
        if os.path.exists(SENT_IDS_FILE):
            with open(SENT_IDS_FILE, "r") as f:
                all_ids = [line.strip() for line in f if line.strip().isdigit()]
            # Convert both lists to integers for comparison
            ids_to_remove_set = set(int(i) for i in ids_to_remove)
            remaining_ids = [msg_id for msg_id in all_ids if int(msg_id) not in ids_to_remove_set]
            with open(SENT_IDS_FILE, "w") as f:
                for msg_id in remaining_ids:
                    f.write(f"{msg_id}\n")
    except Exception as e:
        logger.error("Error removing IDs from sent_message_ids.txt: %s", e)


async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle inline keyboard button clicks."""
    query = update.callback_query
    await query.answer()  # Acknowledge the button press

    action, _, message_id_str = query.data.partition('_')

    if action == 'read':
        try:
            message_id = int(message_id_str)
            if mark_message_as_read(message_id):
                # Edit the original message to remove the button
                await query.edit_message_text(
                    text=query.message.text + "\n\n---\n✅ Marked as Read",
                    reply_markup=None  # Remove keyboard
                )
                logger.info("Marked message ID %s as read.", message_id)
                remove_sent_ids([message_id])
            else:
                await query.edit_message_text(
                    text=query.message.text + "\n\n---\n⚠️ Already marked as read or error.",
                    reply_markup=None
                )
        except (ValueError, IndexError):
            await query.edit_message_text(
                text=query.message.text + "\n\n---\n❌ Error processing command.",
                reply_markup=None
            )
    elif action == 'delete':
        try:
            message_id = int(message_id_str)
            if delete_message(message_id):
                await query.edit_message_text(
                    text=query.message.text + "\n\n---\n🗑️ Message Deleted",
                    reply_markup=None
                )
                logger.info("Deleted message ID %s.", message_id)
                remove_sent_ids([message_id])
            else:
                await query.edit_message_text(
                    text=query.message.text + "\n\n---\n⚠️ Error deleting message or already deleted.",
                    reply_markup=None
                )
        except (ValueError, IndexError):
            await query.edit_message_text(
                text=query.message.text + "\n\n---\n❌ Error processing command.",
                reply_markup=None
            )


MENU_KEYBOARD = ReplyKeyboardMarkup(
    [
        [KeyboardButton("📥 Last 5 Messages"), KeyboardButton("📥 Last 10 Messages")],
        [KeyboardButton("📊 Dashboard"), KeyboardButton("❓ Help")]
    ],
    resize_keyboard=True
)


async def cmd_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.effective_message.reply_text(
        "Please choose an option:", reply_markup=MENU_KEYBOARD
    )


async def handle_menu_choice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.effective_message.text
    if text == "📥 Last 5 Messages":
        await cmd_last5(update, context)
    elif text == "📥 Last 10 Messages":
        await cmd_last10(update, context)
    elif text == "📊 Dashboard":
        app_url = os.environ.get("APP_PUBLIC_URL", "http://127.0.0.1:5000")
        await update.effective_message.reply_text(f"Dashboard: {app_url}")
    elif text == "❓ Help":
        await update.effective_message.reply_text(
            "Available commands:\n/menu - Show menu\n/last5 - Last 5 messages\n/last10 - Last 10 messages\n/start - Bot info"
        )
    else:
        await update.effective_message.reply_text("Unknown option. Use /menu to see available actions.")


def fetch_last_messages(limit=5):
    """Fetch the last N messages from the inbox table, skipping empty messages."""
    conn = get_db_connection()
    if not conn:
        return []
    cursor = conn.cursor(dictionary=True)
    try:
        # Fetch unread messages first, then read ones, up to the limit
        cursor.execute(
            """
            (SELECT ID, SenderNumber, TextDecoded, ReceivingDateTime, Processed, UDH
            FROM inbox
            WHERE TextDecoded IS NOT NULL AND TextDecoded != ''
            ORDER BY ReceivingDateTime DESC)
            LIMIT %s
            """,
            (limit,)
        )
        rows = cursor.fetchall()
        # Assemble multipart messages and drop empty/blank rows
        messages = assemble_inbox_rows(rows)
    except mysql.connector.Error as err:
        logger.error("Failed to fetch messages: %s", err)
        messages = []
    finally:
        cursor.close()
        conn.close()
    return messages


async def send_messages_with_button(update: Update, context: ContextTypes.DEFAULT_TYPE, limit: int):
    """Helper to fetch and send messages with a 'Mark as Read' button."""
    messages = fetch_last_messages(limit)
    if not messages:
        await update.effective_message.reply_text("No recent messages found.")
        return

    for m in reversed(messages):
        status = "✅" if m['Processed'] == 'true' else "🆕"
        text = f"{status} From: {m['SenderNumber']}\n{m['TextDecoded']}\nReceived: {m['ReceivingDateTime']}"

        keyboard = None
        if m['Processed'] == 'false':
            keyboard = InlineKeyboardMarkup([
                [
                    InlineKeyboardButton("Mark as Read", callback_data=f"read_{m['ID']}"),
                    InlineKeyboardButton("Delete", callback_data=f"delete_{m['ID']}")
                ]
            ])

        await update.effective_message.reply_text(text, reply_markup=keyboard)


async def cmd_last10(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await send_messages_with_button(update, context, 10)


async def cmd_last5(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await send_messages_with_button(update, context, 5)


def get_server_ip() -> str:
    env_ip = os.environ.get('SERVER_IP')
    if env_ip:
        return env_ip
    app_url_env = os.environ.get('APP_PUBLIC_URL')
    if app_url_env:
        try:
            netloc = urlparse(app_url_env).hostname
            if netloc:
                try:
                    return str(ipaddress.ip_address(netloc))
                except ValueError:
                    pass
        except Exception:
            pass
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("1.1.1.1", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "unknown"


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    bot = context.bot
    me = await bot.get_me()
    host = socket.gethostname()
    ip = get_server_ip()
    env = os.environ.get("FLASK_ENV", "production")
    app_url = os.environ.get("APP_PUBLIC_URL", "http://127.0.0.1:5000")
    chat = update.effective_chat

    lines = [
        "✅ Welcome! You're connected to:",
        "",
        f"Bot: @{me.username} (id: {me.id})",
        f"Server: {host} ({ip})",
        f"Environment: {env}",
        f"Database: {DB_NAME or 'unknown'} @ {DB_HOST}",
        f"Dashboard: {app_url}",
        "",
        f"This chat id: {chat.id}",
        f"For menu, type /menu",
    ]
    if TELEGRAM_CHAT_ID:
        try:
            configured = int(TELEGRAM_CHAT_ID)
            status = "MATCH" if configured == chat.id else "DIFFERENT"
            lines.append(f"Configured TELEGRAM_CHAT_ID: {configured} ({status})")
        except Exception:
            lines.append(f"Configured TELEGRAM_CHAT_ID: {TELEGRAM_CHAT_ID}")

    await update.effective_message.reply_text("\n".join(lines))


def _http_send_telegram_message(token: str, chat_id: str | None, text: str, parse_mode: str | None = None, reply_markup: dict | None = None):
    """Send a message via Telegram Bot API using standard library (no extra deps)."""
    api_url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {"chat_id": chat_id, "text": text}
    if parse_mode:
        payload["parse_mode"] = parse_mode
    if reply_markup:
        payload["reply_markup"] = json.dumps(reply_markup)
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(api_url, data=data, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            if resp.status != 200:
                logger.warning("Telegram API non-200: %s", resp.status)
    except Exception as e:
        logger.error("Telegram sendMessage error: %s", e)


def send_message_to_telegram(message):
    """Sends a formatted message to a Telegram chat for new SMS notifications."""
    if not TELEGRAM_BOT_TOKEN:
        return  # Silently fail if not configured

    is_maskan_message = bool(re.search(r"(?i)\bmaskan\b", message['SenderNumber']))
    
    chat_id = None
    keyboard = None

    if is_maskan_message and TELEGRAM_MASKAN_CHAT_ID:
        chat_id = TELEGRAM_MASKAN_CHAT_ID
        # No keyboard for Maskan messages
            
    elif TELEGRAM_CHAT_ID:
        chat_id = TELEGRAM_CHAT_ID
        # Create an inline keyboard with a "Mark as Read" button
        keyboard = {
            "inline_keyboard": [
                [
                    {"text": "Mark as Read", "callback_data": f"read_{message['ID']}"},
                    {"text": "Delete", "callback_data": f"delete_{message['ID']}"}
                ]
            ]
        }

    if not chat_id:
        return # No chat ID configured for this message type

    try:
        text = (
            f"New SMS from: {message['SenderNumber']}\n\n"
            f"{message['TextDecoded']}\n\n"
            f"Received: {message['ReceivingDateTime'].strftime('%B %d, %Y at %I:%M %p')}"
        )

        _http_send_telegram_message(
            TELEGRAM_BOT_TOKEN,
            chat_id,
            text,
            reply_markup=keyboard
        )
        if is_maskan_message:
            keyboard = {
                "inline_keyboard": [
                    [
                        {"text": "Mark as Read", "callback_data": f"read_{message['ID']}"},
                        {"text": "Delete", "callback_data": f"delete_{message['ID']}"}
                    ]
                ]
            }
            _http_send_telegram_message(
                TELEGRAM_BOT_TOKEN,
                TELEGRAM_CHAT_ID,
                text,
                reply_markup=keyboard
            )
        logger.info("Sent message to Telegram for SMS ID %s to chat ID %s", message['ID'], chat_id)
    except Exception as e:
        logger.error("Error sending message to Telegram: %s", e)


def pull_new_messages():
    """Pulls the database for new messages and sends them to Telegram, caching last sent ID."""
    logger.info("Starting background thread to pull for new messages...")

    def load_sent_ids():
        if not os.path.exists(SENT_IDS_FILE):
            return set()
        try:
            with open(SENT_IDS_FILE, "r") as f:
                return {line.strip() for line in f if line.strip().isdigit()}
        except Exception as e:
            print(f"Error loading sent IDs file: {e}")
            return set()

    def save_sent_ids(sent_ids):
        try:
            with open(SENT_IDS_FILE, "w") as f:
                for msg_id in sent_ids:
                    f.write(f"{msg_id}\n")
        except Exception as e:
            logger.error("Error saving sent IDs file: %s", e)
    
    sent_ids = load_sent_ids()

    while True:
        conn = get_db_connection()
        if not conn:
            logger.warning("Pulling thread: Database connection failed. Retrying in 60s.")
            time.sleep(60)
            continue

        cursor = conn.cursor(dictionary=True)
        try:
            # Clean up empty/blank messages first
            cursor.execute("DELETE FROM inbox WHERE TextDecoded IS NULL OR TRIM(TextDecoded) = ''")
            deleted = cursor.rowcount
            if deleted > 0:
                conn.commit()

            # Fetch unread messages that haven't been processed by the bot yet
            cursor.execute("""
                SELECT ID, SenderNumber, TextDecoded, ReceivingDateTime, Processed,
                       UDH
                FROM inbox
                WHERE Processed = 'false'
                ORDER BY ReceivingDateTime ASC
            """)
            raw_messages = cursor.fetchall()
            messages = assemble_inbox_rows(raw_messages)

            new_sent = False
            for message in messages:
                if str(message['ID']) not in sent_ids:
                    send_message_to_telegram(message)
                    sent_ids.add(str(message['ID']))
                    new_sent = True

            if new_sent:
                save_sent_ids(sent_ids)

        except mysql.connector.Error as err:
            logger.error("Pulling thread error: %s", err)
        finally:
            cursor.close()
            conn.close()

        time.sleep(10) # Pulls every 10 seconds


def main():
    if not TELEGRAM_BOT_TOKEN:
        raise SystemExit("TELEGRAM_BOT_TOKEN is not set")

    # Start the background thread for message polling
    polling_thread = threading.Thread(target=pull_new_messages, daemon=True)
    polling_thread.start()

    app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("last10", cmd_last10))
    app.add_handler(CommandHandler("last5", cmd_last5))
    app.add_handler(CommandHandler("menu", cmd_menu))
    app.add_handler(CallbackQueryHandler(button_callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_menu_choice))

    # Send a startup ping via HTTP helper (works outside event loop)
    if TELEGRAM_CHAT_ID:
        try:
            host = socket.gethostname()
            ip = get_server_ip()
            env = os.environ.get('FLASK_ENV', 'production')
            app_url = os.environ.get('APP_PUBLIC_URL', 'http://127.0.0.1:5000')
            text = (
                "🚀 Bot started successfully\n\n"
                f"Server: {host} ({ip})\n"
                f"Environment: {env}\n"
                f"Dashboard: {app_url}"
            )
            _http_send_telegram_message(TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, text)
        except Exception as e:
            logger.error("Startup ping failed: %s", e)

    logger.info("Starting Telegram bot (run_polling in main thread)...")
    app.run_polling(allowed_updates=["message", "callback_query", "chat_member", "my_chat_member"])


if __name__ == "__main__":
    main()
