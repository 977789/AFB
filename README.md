# AFB Forward Bot

A Pyrogram-based Telegram bot that forwards media from source channels to target channels with batch rotation, duplicate detection, and optional links/text forwarding.

## Features

- Forwards videos/documents from multiple source channels to target channels
- Batch-based round-robin distribution across targets
- MongoDB-backed duplicate detection and state persistence
- Optional links/text forwarding filtered by quality tags (480p/720p/1080p/2160p/4k)
- Inline admin menu (sources, targets, links, settings, status, logs)
- `/update` command to pull latest code from git and restart

## Bug fixes in this version

- Fixed `API_ID` crash when the env var is unset
- Fixed broken channel-ID regex (`^-?\\d+$`)
- Added an `asyncio.Lock` around the read → copy → save critical section to prevent the batch-distribution race condition / double-posting
- Moved blocking `pymongo` and `psutil` calls off the event loop via `asyncio.to_thread`
- Made `psutil.cpu_percent(interval=1)` non-blocking
- Clients are now stopped cleanly before re-exec in `/update`
- Fixed double-start by running `main()` via the event loop instead of `app.run()`
- Warns at startup when `ADMINS` is empty

## Commands

All commands are admin-only (restricted to the user IDs in `ADMINS`).

### General

| Command | Description |
|---|---|
| `/start` | Open the inline control menu |
| `/botstatus` | Show forwarding stats, rotation progress, and current settings |
| `/serverstatus` | Show CPU, RAM, disk, network, and uptime |
| `/view_ids` | Export all configured channel IDs as a `.txt` file |
| `/logs` | Send the `bot.txt` log file |
| `/update` | Pull latest code from git, reinstall deps if needed, and restart |

### Source channels

| Command | Description |
|---|---|
| `/add_source ID1 ID2 ...` | Add one or more source channel IDs |
| `/del_source ID1 ID2 ...` | Remove one or more source channel IDs |

### Target channels

| Command | Description |
|---|---|
| `/add_target ID1 ID2 ...` | Add one or more target channel IDs |
| `/del_target ID1 ID2 ...` | Remove one or more target channel IDs |

### Settings

| Command | Description |
|---|---|
| `/set_batch N` | Set the batch size (messages per target before rotating) |
| `/toggle_dup` | Toggle duplicate checking on/off |

### Links forwarding

| Command | Description |
|---|---|
| `/set_links_channel ID` | Set the channel where links/text are forwarded (accepts ID or @username) |
| `/del_links_channel` | Remove the links channel and disable links forwarding |
| `/toggle_links` | Toggle links/text forwarding on/off |
| `/add_links_source ID1 ID2 ...` | Add dedicated source channels for links forwarding |
| `/del_links_source ID1 ID2 ...` | Remove dedicated links source channels |

> If no links sources are set, the main `SOURCE_CHANNELS` are used. Only text/photo messages whose content matches a quality tag (`480p`, `720p`, `1080p`, `2160p`, `4k`) are forwarded to the links channel.

## Configuration

Copy `.env.example` to `.env` and fill in the values:

| Variable | Description |
|---|---|
| `API_ID` | Telegram API ID |
| `API_HASH` | Telegram API hash |
| `BOT_TOKEN` | Bot token from @BotFather |
| `SESSION` | Pyrogram user session string (for the forwarder client) |
| `MONGO_URI` | MongoDB connection string (use `mongodb://mongo:27017` with compose) |
| `ADMINS` | Space-separated admin user IDs |
| `SOURCE_CHANNELS` | Space-separated source channel IDs (initial seed) |
| `TARGET_CHANNELS` | Space-separated target channel IDs (initial seed) |

## Run with Docker Compose

```bash
cp .env.example .env
# edit .env
docker compose up -d --build
```

This starts the bot together with a MongoDB instance.

## Run with Docker only

```bash
docker build -t afb-forward-bot .
docker run -d --env-file .env afb-forward-bot
```

Provide an external `MONGO_URI` in this case.

## Run locally

```bash
pip install -r requirements.txt
python bot.py
```
