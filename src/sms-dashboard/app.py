import os
from datetime import timedelta

import mysql.connector
from dotenv import load_dotenv
from flask import Flask, render_template, redirect, url_for, flash, request
from .multipart import assemble_inbox_rows

# Load environment variables from .env file
load_dotenv()

# --- Configuration ---
# Securely load credentials from environment variables
DB_CONFIG = {
    'host': os.environ.get('DB_HOST', 'localhost'),
    'user': os.environ.get('DB_USER'),
    'password': os.environ.get('DB_PASSWORD'),
    'database': os.environ.get('DB_NAME'),
}

# --- Flask Application ---
app = Flask(__name__)
app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY', 'dev-default') # Used for flashing messages

# Securely set SECRET_KEY: require in production, allow default in development
secret_key = os.environ.get('SECRET_KEY')
flask_env = os.environ.get('FLASK_ENV', 'production').lower()
if not secret_key:
    if flask_env == 'development':
        secret_key = 'dev-default'
        print("WARNING: Using default SECRET_KEY in development mode. Do not use in production!", flush=True)
    else:
        raise RuntimeError("SECRET_KEY environment variable must be set in production.")
app.config['SECRET_KEY'] = secret_key  # Used for flashing messages
# --- Database Connection ---
def get_db_connection():
    """Establishes a connection to the MySQL database."""
    try:
        conn = mysql.connector.connect(**DB_CONFIG)
        return conn
    except mysql.connector.Error as err:
        print(f"Error connecting to database: {err}")
        return None

# --- View helpers ---
def _received_label(received, now):
    """Short, human-friendly timestamp relative to the database clock."""
    if not received:
        return ""
    delta = now - received
    seconds = delta.total_seconds()
    if seconds < 60:
        return "Just now"
    if seconds < 3600:
        return f"{int(seconds // 60)} min ago"
    if received.date() == now.date():
        return received.strftime('%H:%M')
    if received.date() == (now - timedelta(days=1)).date():
        return received.strftime('Yesterday %H:%M')
    if received.year == now.year:
        return received.strftime('%b %d')
    return received.strftime('%b %d, %Y')


def _parse_message_ids(values):
    """Flatten form values like ['1,2', '3'] into a list of ints, ignoring junk."""
    ids = []
    for value in values:
        for part in str(value).split(','):
            part = part.strip()
            if part.isdigit():
                ids.append(int(part))
    return ids


# --- App Routes ---

@app.route('/')
def index():
    """Main page, displays all messages from the inbox."""
    conn = get_db_connection()
    if not conn:
        flash("Database connection failed. Check console for errors.", "error")
        return render_template('index.html', messages=[], unread_count=0, db_error=True)

    cursor = conn.cursor(dictionary=True)
    db_error = False
    try:
        # Include UDH so multipart messages can be assembled
        cursor.execute("""
            SELECT ID, SenderNumber, TextDecoded, ReceivingDateTime, Processed,
                   UDH
            FROM inbox
            ORDER BY ReceivingDateTime DESC
        """)
        raw_messages = cursor.fetchall()
        # Use the DB clock (which also stamps ReceivingDateTime) for relative times
        cursor.execute("SELECT NOW() AS now")
        now = cursor.fetchone()['now']
        # Assemble multipart messages and drop empty/blank rows
        messages = assemble_inbox_rows(raw_messages)
        for m in messages:
            # Actions must target every part of a multipart message, not just the newest
            m['ids'] = m.get('_part_ids') or [m['ID']]
            m['received_label'] = _received_label(m.get('ReceivingDateTime'), now)
    except mysql.connector.Error as err:
        flash(f"Failed to fetch messages: {err}", "error")
        messages = []
        db_error = True
    finally:
        cursor.close()
        conn.close()

    unread_count = sum(1 for m in messages if m.get('Processed') == 'false')
    return render_template('index.html', messages=messages, unread_count=unread_count, db_error=db_error)

@app.route('/bulk_action', methods=['POST'])
def bulk_action():
    """Handles actions (delete, mark as read) on one or more messages."""
    action = request.form.get('action')
    message_ids = _parse_message_ids(request.form.getlist('message_ids'))

    if action not in ('read', 'delete') or not message_ids:
        flash("No action or no messages selected.", "error")
        return redirect(url_for('index'))

    conn = get_db_connection()
    if not conn:
        flash("Database connection failed.", "error")
        return redirect(url_for('index'))

    # Count logical messages (multipart groups) rather than raw DB rows
    message_count = len(request.form.getlist('message_ids'))
    noun = "message" if message_count == 1 else "messages"

    cursor = conn.cursor()
    try:
        placeholders = ', '.join(['%s'] * len(message_ids))

        if action == 'read':
            query = f"UPDATE inbox SET Processed = 'true' WHERE ID IN ({placeholders})"
            cursor.execute(query, tuple(message_ids))
            flash(f"{message_count} {noun} marked as read.", "success")
        elif action == 'delete':
            query = f"DELETE FROM inbox WHERE ID IN ({placeholders})"
            cursor.execute(query, tuple(message_ids))
            flash(f"{message_count} {noun} deleted.", "success")

        conn.commit()

    except mysql.connector.Error as err:
        flash(f"An error occurred: {err}", "error")
    finally:
        cursor.close()
        conn.close()

    return redirect(url_for('index'))


# --- Main Execution ---
if __name__ == '__main__':
    print("Starting Flask server...")
    print("Access the app at http://127.0.0.1:5000")
    app.run(host='0.0.0.0', port=5000, debug=True)
