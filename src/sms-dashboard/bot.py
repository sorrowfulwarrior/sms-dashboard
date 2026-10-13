import html
import os
import socket
import ipaddress
import re
import json
import threading
import time
import urllib.request
from collections import Counter
from datetime import timedelta
from urllib.parse import urlparse
from dotenv import load_dotenv
from telegram import Update, ReplyKeyboardMarkup, KeyboardButton, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.error import BadRequest
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
APP_PUBLIC_URL = os.environ.get("APP_PUBLIC_URL", "http://127.0.0.1:5000")
BOT_STARTED_AT = time.time()


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


def _ids_placeholders(message_ids):
    return ', '.join(['%s'] * len(message_ids))


def mark_message_as_read(message_ids: list[int]) -> bool:
    """Mark the given message IDs (all parts of one SMS) as read."""
    conn = get_db_connection()
    if not conn:
        return False
    cursor = conn.cursor()
    try:
        cursor.execute(
            f"UPDATE inbox SET Processed = 'true' WHERE ID IN ({_ids_placeholders(message_ids)}) AND Processed = 'false'",
            tuple(message_ids),
        )
        conn.commit()
        return cursor.rowcount > 0
    except mysql.connector.Error as err:
        logger.error("Error updating messages %s: %s", message_ids, err)
        return False
    finally:
        cursor.close()
        conn.close()


def delete_message(message_ids: list[int]) -> bool:
    """Delete the given message IDs (all parts of one SMS) from the database."""
    conn = get_db_connection()
    if not conn:
        return False
    cursor = conn.cursor()
    try:
        cursor.execute(f"DELETE FROM inbox WHERE ID IN ({_ids_placeholders(message_ids)})", tuple(message_ids))
        conn.commit()
        return cursor.rowcount > 0
    except mysql.connector.Error as err:
        logger.error("Error deleting messages %s: %s", message_ids, err)
        return False
    finally:
        cursor.close()
        conn.close()


def mark_all_as_read() -> list[int] | None:
    """Mark every unread message as read. Returns the affected IDs, or None on error."""
    conn = get_db_connection()
    if not conn:
        return None
    cursor = conn.cursor()
    try:
        cursor.execute("SELECT ID FROM inbox WHERE Processed = 'false'")
        ids = [row[0] for row in cursor.fetchall()]
        if ids:
            cursor.execute(f"UPDATE inbox SET Processed = 'true' WHERE ID IN ({_ids_placeholders(ids)})", tuple(ids))
            conn.commit()
        return ids
    except mysql.connector.Error as err:
        logger.error("Error marking all messages as read: %s", err)
        return None
    finally:
        cursor.close()
        conn.close()


def message_ids_of(message) -> list[int]:
    """All inbox row IDs that make up a (possibly multipart) message."""
    return message.get('_part_ids') or [message['ID']]


def callback_ids(message) -> str:
    """Comma-joined part IDs for callback data, kept within Telegram's 64-byte limit."""
    ids = ','.join(str(i) for i in message_ids_of(message))
    return ids if len(ids) <= 56 else str(message['ID'])


def parse_callback_ids(value: str) -> list[int]:
    return [int(part) for part in value.split(',')]


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

    action, _, payload = query.data.partition('_')

    if action == 'dash':
        await handle_dashboard_callback(update, context, payload)
        return

    await query.answer()  # Acknowledge the button press

    if action == 'read':
        try:
            message_ids = parse_callback_ids(payload)
            if mark_message_as_read(message_ids):
                # Edit the original message to remove the button
                await query.edit_message_text(
                    text=query.message.text + "\n\n---\n✅ Marked as Read",
                    reply_markup=None  # Remove keyboard
                )
                logger.info("Marked message IDs %s as read.", message_ids)
                remove_sent_ids(message_ids)
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
            message_ids = parse_callback_ids(payload)
            if delete_message(message_ids):
                await query.edit_message_text(
                    text=query.message.text + "\n\n---\n🗑️ Message Deleted",
                    reply_markup=None
                )
                logger.info("Deleted message IDs %s.", message_ids)
                remove_sent_ids(message_ids)
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


# --- Dashboard ---

def _ago(delta: timedelta) -> str:
    seconds = max(0, int(delta.total_seconds()))
    if seconds < 60:
        return f"{seconds}s ago"
    if seconds < 3600:
        return f"{seconds // 60} min ago"
    if seconds < 86400:
        return f"{seconds // 3600} h ago"
    return f"{seconds // 86400} d ago"


def _duration(seconds: float) -> str:
    seconds = int(seconds)
    days, rem = divmod(seconds, 86400)
    hours, rem = divmod(rem, 3600)
    minutes = rem // 60
    if days:
        return f"{days}d {hours}h"
    if hours:
        return f"{hours}h {minutes}m"
    return f"{minutes}m"


def _signal_bars(percent: int) -> str:
    if percent < 0:
        return "unknown"
    filled = min(4, (percent + 24) // 25)
    return "▂▄▆█"[:filled] + "░" * (4 - filled) + f" {percent}%"


def fetch_dashboard_stats() -> dict | None:
    """Collect inbox, outbox and modem stats. Returns None if the DB is unreachable."""
    conn = get_db_connection()
    if not conn:
        return None
    cursor = conn.cursor(dictionary=True)
    stats: dict = {}
    try:
        cursor.execute("SELECT NOW() AS now")
        now = cursor.fetchone()['now']
        stats['now'] = now

        cursor.execute("""
            SELECT ID, SenderNumber, TextDecoded, ReceivingDateTime, Processed, UDH
            FROM inbox
        """)
        messages = assemble_inbox_rows(cursor.fetchall())
        week_ago = now - timedelta(days=7)
        stats['total'] = len(messages)
        stats['unread'] = sum(1 for m in messages if m.get('Processed') == 'false')
        stats['today'] = sum(1 for m in messages if m.get('ReceivingDateTime') and m['ReceivingDateTime'].date() == now.date())
        stats['week'] = sum(1 for m in messages if m.get('ReceivingDateTime') and m['ReceivingDateTime'] >= week_ago)
        stats['latest'] = messages[0] if messages else None
        stats['top_senders'] = Counter(
            m['SenderNumber'] for m in messages
            if m.get('ReceivingDateTime') and m['ReceivingDateTime'] >= week_ago
        ).most_common(3)
    except mysql.connector.Error as err:
        logger.error("Dashboard inbox query failed: %s", err)
        cursor.close()
        conn.close()
        return None

    # Optional Gammu tables: missing tables shouldn't break the dashboard
    try:
        cursor.execute("SELECT COUNT(*) AS n FROM outbox")
        stats['outbox'] = cursor.fetchone()['n']
    except mysql.connector.Error:
        stats['outbox'] = None
    try:
        cursor.execute("""
            SELECT ID, NetName, NetCode, `Signal`, Battery, Sent, Received, UpdatedInDB,
                   TimeOut >= NOW() AS online
            FROM phones
            ORDER BY UpdatedInDB DESC
        """)
        stats['phones'] = cursor.fetchall()
    except mysql.connector.Error:
        stats['phones'] = None
    finally:
        cursor.close()
        conn.close()
    return stats


def format_dashboard(stats: dict | None) -> str:
    """Render dashboard stats as Telegram HTML."""
    e = html.escape
    lines = ["📊 <b>SMS Dashboard</b>"]

    if stats is None:
        lines += ["", "❌ <b>Database unreachable</b>", f"<code>{e(DB_NAME or 'unknown')} @ {e(DB_HOST)}</code>"]
        return "\n".join(lines)

    now = stats['now']
    lines.append(f"<i>Updated {now.strftime('%H:%M:%S')}</i>")

    lines += ["", "📬 <b>Inbox</b>"]
    unread = stats['unread']
    lines.append(f"• Unread: <b>{unread}</b>" + (" 🔔" if unread else ""))
    lines.append(f"• Total: {stats['total']}")
    lines.append(f"• Today: {stats['today']} · Last 7 days: {stats['week']}")
    latest = stats['latest']
    if latest and latest.get('ReceivingDateTime'):
        lines.append(
            f"• Last received: {_ago(now - latest['ReceivingDateTime'])} "
            f"from <code>{e(str(latest['SenderNumber']))}</code>"
        )
    if stats['outbox']:
        lines.append(f"• Outbox: {stats['outbox']} queued")

    if stats['top_senders']:
        lines += ["", "👥 <b>Top senders (7 days)</b>"]
        for i, (sender, count) in enumerate(stats['top_senders'], 1):
            lines.append(f"{i}. <code>{e(str(sender))}</code> — {count}")

    lines += ["", "📶 <b>Modem</b>"]
    phones = stats['phones']
    if phones is None:
        lines.append("• No modem info (phones table unavailable)")
    elif not phones:
        lines.append("• ⚠️ No modem has reported in. Is gammu-smsd running?")
    else:
        for ph in phones:
            status = "🟢 Online" if ph['online'] else "🔴 Offline"
            lines.append(f"{status} · <code>{e(str(ph['ID']))}</code>")
            net = ph.get('NetName') or 'unknown'
            if ph.get('NetCode') and ph['NetCode'] != 'ERROR':
                net += f" ({ph['NetCode']})"
            lines.append(f"• Network: {e(net)}")
            signal = f"• Signal: {_signal_bars(ph['Signal'])}"
            if ph['Battery'] and ph['Battery'] > 0:
                signal += f" · Battery: {ph['Battery']}%"
            lines.append(signal)
            lines.append(f"• Sent: {ph['Sent']} · Received: {ph['Received']}")
            if ph.get('UpdatedInDB'):
                lines.append(f"• Last seen: {_ago(now - ph['UpdatedInDB'])}")

    lines += [
        "",
        "🖥 <b>System</b>",
        f"• Server: {e(socket.gethostname())} ({e(get_server_ip())})",
        f"• Database: <code>{e(DB_NAME or 'unknown')} @ {e(DB_HOST)}</code>",
        f"• Bot uptime: {_duration(time.time() - BOT_STARTED_AT)}",
    ]
    return "\n".join(lines)


def _is_public_url(url: str) -> bool:
    """Telegram rejects URL buttons pointing at localhost/loopback addresses."""
    try:
        parsed = urlparse(url)
        if parsed.scheme not in ('http', 'https') or not parsed.hostname:
            return False
        if parsed.hostname == 'localhost':
            return False
        try:
            return not ipaddress.ip_address(parsed.hostname).is_loopback
        except ValueError:
            return True
    except Exception:
        return False


def dashboard_keyboard(stats: dict | None, with_url: bool = True) -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton("🔄 Refresh", callback_data="dash_refresh")]]
    if stats and stats['unread']:
        rows[0].append(InlineKeyboardButton(f"📬 Show unread ({stats['unread']})", callback_data="dash_unread"))
        rows.append([InlineKeyboardButton("✅ Mark all as read", callback_data="dash_readall")])
    if with_url and _is_public_url(APP_PUBLIC_URL):
        rows.append([InlineKeyboardButton("🌐 Open web dashboard", url=APP_PUBLIC_URL)])
    return InlineKeyboardMarkup(rows)


async def _send_or_edit_dashboard(update: Update, edit: bool):
    stats = fetch_dashboard_stats()
    text = format_dashboard(stats)
    if stats is not None and not _is_public_url(APP_PUBLIC_URL):
        text += f"\n• Web: <code>{html.escape(APP_PUBLIC_URL)}</code>"

    async def send(with_url: bool):
        keyboard = dashboard_keyboard(stats, with_url)
        if edit:
            await update.callback_query.edit_message_text(text, parse_mode="HTML", reply_markup=keyboard)
        else:
            await update.effective_message.reply_text(text, parse_mode="HTML", reply_markup=keyboard)

    try:
        await send(with_url=True)
    except BadRequest as err:
        if "not modified" in str(err).lower():
            return
        # Most likely Telegram rejected the dashboard URL; retry without the button
        logger.warning("Dashboard send failed (%s); retrying without URL button", err)
        await send(with_url=False)


async def cmd_dashboard(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await _send_or_edit_dashboard(update, edit=False)


async def handle_dashboard_callback(update: Update, context: ContextTypes.DEFAULT_TYPE, payload: str):
    query = update.callback_query

    if payload == 'refresh':
        await query.answer("Refreshed")
        await _send_or_edit_dashboard(update, edit=True)
    elif payload == 'unread':
        await query.answer()
        await send_unread_messages(query.message)
    elif payload == 'readall':
        await query.answer()
        await query.edit_message_reply_markup(InlineKeyboardMarkup([[
            InlineKeyboardButton("Yes, mark all as read", callback_data="dash_readallyes"),
            InlineKeyboardButton("Cancel", callback_data="dash_refresh"),
        ]]))
    elif payload == 'readallyes':
        ids = mark_all_as_read()
        if ids is None:
            await query.answer("Database error — nothing changed.", show_alert=True)
            return
        remove_sent_ids(ids)
        await query.answer("All messages marked as read.")
        await _send_or_edit_dashboard(update, edit=True)
    else:
        await query.answer()


MENU_KEYBOARD = ReplyKeyboardMarkup(
    [
        [KeyboardButton("📬 Unread Messages"), KeyboardButton("📥 Last 10 Messages")],
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
    if text == "📬 Unread Messages":
        await cmd_unread(update, context)
    elif text == "📥 Last 10 Messages":
        await show_last10(update, context)
    # Old keyboard button, still shown in chats until /menu is sent again
    elif text == "📥 Last 5 Messages":
        await cmd_last5(update, context)
    elif text == "📊 Dashboard":
        await cmd_dashboard(update, context)
    elif text == "❓ Help":
        await update.effective_message.reply_text(
            "Available commands:\n/menu - Show menu\n/dashboard - Inbox, modem and system status\n/unread - Unread messages\n/last5 - Last 5 messages\n/start - Bot info"
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


def fetch_unread_messages():
    """Fetch all unread messages (multipart assembled), newest first."""
    conn = get_db_connection()
    if not conn:
        return []
    cursor = conn.cursor(dictionary=True)
    try:
        cursor.execute("""
            SELECT ID, SenderNumber, TextDecoded, ReceivingDateTime, Processed, UDH
            FROM inbox
            WHERE Processed = 'false'
        """)
        return assemble_inbox_rows(cursor.fetchall())
    except mysql.connector.Error as err:
        logger.error("Failed to fetch unread messages: %s", err)
        return []
    finally:
        cursor.close()
        conn.close()


async def send_message_list(target_message, messages):
    """Reply with each message (oldest first) and Mark as Read / Delete buttons for unread ones."""
    for m in reversed(messages):
        status = "✅" if m['Processed'] == 'true' else "🆕"
        text = f"{status} From: {m['SenderNumber']}\n{m['TextDecoded']}\nReceived: {m['ReceivingDateTime']}"

        keyboard = None
        if m['Processed'] == 'false':
            ids = callback_ids(m)
            keyboard = InlineKeyboardMarkup([
                [
                    InlineKeyboardButton("Mark as Read", callback_data=f"read_{ids}"),
                    InlineKeyboardButton("Delete", callback_data=f"delete_{ids}")
                ]
            ])

        await target_message.reply_text(text, reply_markup=keyboard)


async def send_messages_with_button(update: Update, context: ContextTypes.DEFAULT_TYPE, limit: int):
    """Helper to fetch and send messages with a 'Mark as Read' button."""
    messages = fetch_last_messages(limit)
    if not messages:
        await update.effective_message.reply_text("No recent messages found.")
        return
    await send_message_list(update.effective_message, messages)


UNREAD_LIST_LIMIT = 10


async def send_unread_messages(target_message):
    """Reply with the newest unread messages, each with Mark as Read / Delete buttons."""
    messages = fetch_unread_messages()
    if not messages:
        await target_message.reply_text("🎉 No unread messages.")
        return
    await send_message_list(target_message, messages[:UNREAD_LIST_LIMIT])
    if len(messages) > UNREAD_LIST_LIMIT:
        await target_message.reply_text(
            f"Showing the latest {UNREAD_LIST_LIMIT} of {len(messages)} unread messages."
        )


async def cmd_unread(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await send_unread_messages(update.effective_message)


async def show_last10(update: Update, context: ContextTypes.DEFAULT_TYPE):
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
                    {"text": "Mark as Read", "callback_data": f"read_{callback_ids(message)}"},
                    {"text": "Delete", "callback_data": f"delete_{callback_ids(message)}"}
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
                        {"text": "Mark as Read", "callback_data": f"read_{callback_ids(message)}"},
                        {"text": "Delete", "callback_data": f"delete_{callback_ids(message)}"}
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
    app.add_handler(CommandHandler("unread", cmd_unread))
    app.add_handler(CommandHandler("last5", cmd_last5))
    app.add_handler(CommandHandler("menu", cmd_menu))
    app.add_handler(CommandHandler("dashboard", cmd_dashboard))
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
