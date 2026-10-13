
# Gammu SMS Web Manager

A Flask web dashboard for reading and managing SMS messages that [Gammu](https://wammu.eu/gammu/) stores in a MySQL/MariaDB database, plus an optional Telegram bot that forwards new SMS and lets you manage them from your phone.

---

## Features

### Web dashboard

- **Clean inbox view:** responsive list layout with light and dark mode (follows your system setting).
- **Multipart SMS assembly:** long messages split by the network are shown as one message, and every action applies to all of its parts.
- **Search and filter:** instant search by sender or text, plus All / Unread / Read tabs with counts.
- **Copy helpers:** one-click copy of the message text, and a copy button for verification codes (OTP/PIN/TAN) detected in a message.
- **Bulk actions:** select messages (select-all only selects what the current filter shows) to mark as read or delete in one go.
- **Relative times:** "3 min ago", "Yesterday 13:00", with the full date on hover. Times come from the database clock, so server/browser time zones don't matter.
- **Keyboard shortcuts:** see [below](#keyboard-shortcuts).

### Telegram bot (optional)

- **New SMS notifications** with *Mark as Read* and *Delete* buttons.
- **Dashboard card** (`/dashboard` or the 📊 Dashboard button) showing:
  - Inbox: unread, total, today, last 7 days, last received, queued outbox messages
  - Top senders over the last 7 days
  - Modem status from Gammu's `phones` table: online/offline, network, signal strength, battery, sent/received counters, last seen
  - System: server, database, bot uptime
  - Buttons to refresh the card, show unread messages, mark everything as read (with confirmation) and open the web dashboard
- **Unread messages:** the 📬 Unread Messages menu button (or `/unread`) lists the newest 10 unread SMS, each with *Mark as Read* and *Delete* buttons.
- **Recent messages:** the 📥 Last 10 Messages menu button lists the 10 newest SMS (read and unread); `/last5` lists the newest 5.
- **Second channel:** SMS from senders matching `maskan` are also forwarded to a separate chat (`TELEGRAM_MASKAN_CHAT_ID`).

---

## Keyboard shortcuts

| Key | Action |
| --- | --- |
| `/` | Focus the search box |
| `D` | Delete the message under the mouse pointer (opens confirmation; press `D` again or `Enter` to confirm) |
| `R` | Refresh |
| `Esc` | Clear search, or clear selection |

Shortcuts are ignored while typing in the search box.

---

## Prerequisites

- Python 3.13+
- GSM modem or compatible mobile device (e.g., [SIM800 Module](https://de.aliexpress.com/item/4000890352364.html?spm=a2g0o.order_list.order_list_main.5.53971802mb0CD6&gatewayAdapt=glo2deu))
- MySQL or MariaDB server
- Poetry (dependency management)
- [asdf](https://asdf-vm.com/) (optional)
- Docker + Docker Compose (optional, see [Docker](#docker))

---

## 1. Gammu Installation & Configuration

### Install Gammu & SMSD

```bash
sudo apt-get update
sudo apt-get install gammu gammu-smsd
```

### Configure Gammu

Connect your modem (e.g., `/dev/ttyUSB0`). Create `/etc/gammu-smsdrc`:

```ini
[gammu]
device = /dev/ttyUSB0
connection = at115200

[smsd]
service = sql
driver = native_mysql
host = localhost
user = gammu_user
password = <password>
database = gammu_db
logfile = syslog
loglevel = debug
```

Start and enable the service:

```bash
sudo systemctl start gammu-smsd
sudo systemctl enable gammu-smsd
sudo systemctl status gammu-smsd
```

### Set Up the Database

```sql
CREATE DATABASE gammu_db;
CREATE USER 'gammu_user'@'localhost' IDENTIFIED BY 'your_secret_password';
GRANT ALL PRIVILEGES ON gammu_db.* TO 'gammu_user'@'localhost';
FLUSH PRIVILEGES;
```

Import Gammu's SQL schema (path may vary):

```bash
mysql -u gammu_user -p gammu_db < /usr/share/doc/gammu/examples/sql/mysql.sql
```

---

## 2. Web App Setup

### Clone & Install

```bash
git clone https://github.com/mohammad-ahmadi1/sms-dashboard.git
cd sms-dashboard
poetry install
```

### Configure Environment

Create a `.env` file:

```env
# Database
DB_HOST=localhost
DB_USER=gammu_user
DB_PASSWORD=your_secret_password
DB_NAME=gammu_db

# Flask
SECRET_KEY=<random_string>
FLASK_ENV=development

# Telegram (optional)
TELEGRAM_BOT_TOKEN=123456:ABC-YourBotToken
TELEGRAM_CHAT_ID=123456789
TELEGRAM_MASKAN_CHAT_ID=
APP_PUBLIC_URL=http://127.0.0.1:5000
SERVER_IP=192.168.1.100
```

Generate a secure `SECRET_KEY`:

```bash
python -c "import secrets; print(secrets.token_hex(32))"
```

| Variable | Required | Description |
| --- | --- | --- |
| `DB_HOST`, `DB_USER`, `DB_PASSWORD`, `DB_NAME` | yes | Gammu database connection |
| `SECRET_KEY` | in production | Flask session key. A default is only used when `FLASK_ENV=development` |
| `FLASK_ENV` | no | `development` or `production` (default) |
| `TELEGRAM_BOT_TOKEN` | for the bot | Token from BotFather. If unset, `sms-dev` skips the bot |
| `TELEGRAM_CHAT_ID` | for the bot | Chat that receives notifications |
| `TELEGRAM_MASKAN_CHAT_ID` | no | Extra chat for SMS from senders matching `maskan` |
| `APP_PUBLIC_URL` | no | Dashboard URL shown by the bot. The *Open web dashboard* button only appears when this is not `localhost`/`127.0.0.1`, because Telegram rejects those links |
| `SERVER_IP` | no | IP shown in bot messages. Falls back to the host in `APP_PUBLIC_URL`, then the active interface IP |
| `SENT_IDS_FILE` | no | Where the bot remembers which SMS it already forwarded (default: `src/sms-dashboard/message_ids.txt`) |
| `LOG_LEVEL` | no | Bot log level (default `INFO`) |

---

## 3. Telegram Bot (Optional)

1. Create a bot via [BotFather](https://t.me/botfather) and add `TELEGRAM_BOT_TOKEN` to `.env`.
2. Get your chat ID by messaging your bot and visiting:
   ```
   https://api.telegram.org/bot<YOUR_BOT_TOKEN>/getUpdates
   ```
   (or start the bot and send `/start`; it replies with the chat ID).
3. Add `TELEGRAM_CHAT_ID` to `.env`.
4. Optionally register the commands with BotFather (`/setcommands`) so Telegram suggests them:
   ```
   menu - Show menu
   dashboard - Inbox, modem and system status
   unread - Unread messages
   last5 - Last 5 messages
   start - Bot info
   ```

The bot checks the database for new unread SMS every 10 seconds. It also deletes inbox rows that have no text.

---

## 4. Running the Application

### Development

Run the dashboard and bot separately:

```bash
poetry run python -m sms-dashboard.app
poetry run python -m sms-dashboard.bot
```

Or run both with one command (process manager):

```bash
poetry run sms-dev
```

Open your browser at [http://127.0.0.1:5000](http://127.0.0.1:5000).

### Production (Gunicorn)

```bash
poetry run sms-prod
```

This runs the dashboard with Gunicorn (`sms-dashboard.app:app`) and starts the bot alongside it.

#### systemd Service Example

Create `/etc/systemd/system/gammu-sms-web.service`:

```ini
[Unit]
Description=Gammu SMS Web Manager (Gunicorn)
After=network.target

[Service]
User=<your-user>
Group=www-data
WorkingDirectory=/home/<your-user>/sms-dashboard
Restart=on-failure
Environment=POETRY_VIRTUALENVS_IN_PROJECT=true
ExecStart=/usr/bin/poetry run sms-prod

[Install]
WantedBy=multi-user.target
```

Reload and start:

```bash
sudo systemctl daemon-reload
sudo systemctl start gammu-sms-web
sudo systemctl enable gammu-sms-web
sudo systemctl status gammu-sms-web
```

### Docker

The Compose file runs the dashboard (`web`) and the bot (`bot`) from one image. It joins the external `docker-gammu_default` network so the containers can reach the Gammu stack's database as `db`, so **start the docker-gammu stack first**.

```bash
cp .env.docker.example .env.docker   # DB settings must match docker-gammu/.env
docker compose up -d --build
```

- Settings are read from `.env` (Telegram, `SECRET_KEY`, …) and then `.env.docker`, which overrides the database connection.
- The dashboard is published on port `5000`; change it with `DASHBOARD_PORT`.
- The bot keeps its forwarded-message list in the `bot_data` volume.

---

## Development

Run the tests:

```bash
poetry run pip install pytest   # not included in the project dependencies
poetry run pytest
```

The web page template lives in `src/sms-dashboard/templates/index.html`.

---

## License

MIT License. See [LICENSE](LICENSE).

---

## TODOs

- [x] Clean up the README
- [x] Clean up the code
- [ ] Long-term test
- [x] Telegram bot
- [x] Docker support
