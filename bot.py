import uvloop
import asyncio
import io
import subprocess
import sys
import os
import time
import logging
import psutil
from pyrogram import Client, filters
from pyrogram.errors import FloodWait
from pyrogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from pymongo import MongoClient
import re
from os import environ

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    handlers=[logging.FileHandler("bot.txt"), logging.StreamHandler()]
)
log = logging.getLogger(__name__)

print("Starting Bot...")
uvloop.install()

# --- CONFIGURATION ---
DEFAULT_BATCH_SIZE = 1000
ADMINS = [int(a) for a in environ.get("ADMINS", "").split()]
if not ADMINS:
    log.warning("ADMINS is empty - no one can control the bot. Set the ADMINS env var.")

id_pattern = re.compile(r'^-?\d+$')
quality_pattern = re.compile(r'\b(480p|720p|1080p|2160p|4k)\b', re.IGNORECASE)

SESSION = environ.get("SESSION", "")
BOT_TOKEN = environ.get("BOT_TOKEN", "")
API_ID = int(environ.get("API_ID", "0") or "0")
API_HASH = environ.get("API_HASH", "")
MONGO_URI = environ.get("MONGO_URI", "")

TARGET_CHANNELS = []
SOURCE_CHANNELS = []
BATCH_SIZE = DEFAULT_BATCH_SIZE
CHECK_DUPLICATES = True
LINKS_CHANNEL = None
LINKS_FORWARDING_ENABLED = False
LINKS_SOURCE_CHANNELS = []

mongo = MongoClient(MONGO_URI)
db = mongo["forwarding_bot"]
state_collection = db["forward_state"]
distribution_collection = db["distribution_state"]
config_collection = db["bot_config"]
hash_collection = db["processed_hashes"]
stats_collection = db["bot_stats"]

# Serializes the read->copy->save critical section (race condition fix)
dist_lock = asyncio.Lock()

# --- MongoDB Helpers (sync; called via asyncio.to_thread) ---

def load_all_settings():
    global SOURCE_CHANNELS, TARGET_CHANNELS, BATCH_SIZE, CHECK_DUPLICATES
    global LINKS_CHANNEL, LINKS_FORWARDING_ENABLED, LINKS_SOURCE_CHANNELS
    doc = config_collection.find_one({"_id": "settings"})
    if doc:
        SOURCE_CHANNELS = doc.get("source_ids", [])
        TARGET_CHANNELS = doc.get("target_ids", [])
        BATCH_SIZE = doc.get("batch_size", DEFAULT_BATCH_SIZE)
        CHECK_DUPLICATES = doc.get("check_duplicates", True)
        LINKS_CHANNEL = doc.get("links_channel", None)
        LINKS_FORWARDING_ENABLED = doc.get("links_forwarding_enabled", False)
        LINKS_SOURCE_CHANNELS = doc.get("links_source_ids", [])
    else:
        SOURCE_CHANNELS = [int(ch) if id_pattern.search(ch) else ch for ch in environ.get("SOURCE_CHANNELS", "").split()]
        TARGET_CHANNELS = [int(ch) if id_pattern.search(ch) else ch for ch in environ.get("TARGET_CHANNELS", "").split()]
        save_db_settings()

def save_db_settings():
    config_collection.update_one(
        {"_id": "settings"},
        {"$set": {
            "source_ids": SOURCE_CHANNELS,
            "target_ids": TARGET_CHANNELS,
            "batch_size": BATCH_SIZE,
            "check_duplicates": CHECK_DUPLICATES,
            "links_channel": LINKS_CHANNEL,
            "links_forwarding_enabled": LINKS_FORWARDING_ENABLED,
            "links_source_ids": LINKS_SOURCE_CHANNELS
        }},
        upsert=True
    )

def is_duplicate(file_hash):
    return hash_collection.find_one({"_id": file_hash}) is not None

def save_hash(file_hash):
    hash_collection.update_one({"_id": file_hash}, {"$set": {"seen": True}}, upsert=True)

def increment_stats():
    stats_collection.update_one({"_id": "total_forwarded"}, {"$inc": {"count": 1}}, upsert=True)

def get_total_stats():
    doc = stats_collection.find_one({"_id": "total_forwarded"})
    return doc["count"] if doc else 0

def get_last_forwarded(chat_id):
    doc = state_collection.find_one({"_id": str(chat_id)})
    return doc["last_message_id"] if doc else 0

def save_last_forwarded(chat_id, message_id):
    state_collection.update_one({"_id": str(chat_id)}, {"$set": {"last_message_id": message_id}}, upsert=True)

def get_distribution_state():
    doc = distribution_collection.find_one({"_id": "batch_distribution_state"})
    if doc:
        return doc.get("current_target_index", 0), doc.get("message_count", 0)
    return 0, 0

def save_distribution_state(index, count):
    distribution_collection.update_one(
        {"_id": "batch_distribution_state"},
        {"$set": {"current_target_index": index, "message_count": count}},
        upsert=True
    )

# --- Pyrogram clients ---
app = Client(name="forwarder", session_string=SESSION, api_id=API_ID, api_hash=API_HASH)
bot = Client(name="bot_commands", bot_token=BOT_TOKEN, api_id=API_ID, api_hash=API_HASH)

# --- START / MENU ---

def main_menu():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("\U0001F4C2 Sources", callback_data="menu_sources"),
         InlineKeyboardButton("\U0001F4CD Targets", callback_data="menu_targets")],
        [InlineKeyboardButton("\U0001F517 Links", callback_data="menu_links"),
         InlineKeyboardButton("\u2699\uFE0F Settings", callback_data="menu_settings")],
        [InlineKeyboardButton("\U0001F4CA Bot Status", callback_data="cmd_botstatus"),
         InlineKeyboardButton("\U0001F5A5\uFE0F Server Status", callback_data="cmd_serverstatus")],
        [InlineKeyboardButton("\U0001F4CB View IDs", callback_data="cmd_view_ids"),
         InlineKeyboardButton("\U0001F4C4 Logs", callback_data="cmd_logs")],
        [InlineKeyboardButton("\u267B\uFE0F Update & Restart", callback_data="cmd_update")],
    ])

def back_btn():
    return InlineKeyboardMarkup([[InlineKeyboardButton("\u00AB Back", callback_data="menu_main")]])

@bot.on_message(filters.command("start") & filters.user(ADMINS))
async def start_cmd(client, message):
    await message.reply("\U0001F44B **AFB Forward Bot**\nChoose an option:", reply_markup=main_menu())

@bot.on_callback_query(filters.user(ADMINS))
async def callback_handler(client, query):
    data = query.data
    if data == "menu_main":
        await query.edit_message_text("\U0001F44B **AFB Forward Bot**\nChoose an option:", reply_markup=main_menu())
    elif data == "menu_sources":
        src_list = "\n".join(map(str, SOURCE_CHANNELS)) or "(none)"
        await query.edit_message_text(
            f"\U0001F4C2 **Source Channels** ({len(SOURCE_CHANNELS)}):\n`{src_list}`\n\n"
            f"Use commands:\n`/add_source ID1 ID2`\n`/del_source ID1 ID2`",
            reply_markup=back_btn())
    elif data == "menu_targets":
        tgt_list = "\n".join(map(str, TARGET_CHANNELS)) or "(none)"
        await query.edit_message_text(
            f"\U0001F4CD **Target Channels** ({len(TARGET_CHANNELS)}):\n`{tgt_list}`\n\n"
            f"Use commands:\n`/add_target ID1 ID2`\n`/del_target ID1 ID2`",
            reply_markup=back_btn())
    elif data == "menu_links":
        lch = str(LINKS_CHANNEL) if LINKS_CHANNEL else "(not set)"
        lstatus = "ON" if LINKS_FORWARDING_ENABLED else "OFF"
        lsrc = "\n".join(map(str, LINKS_SOURCE_CHANNELS)) or "(using main sources)"
        await query.edit_message_text(
            f"\U0001F517 **Links Forwarding**\n\nStatus: **{lstatus}**\nChannel: `{lch}`\nSources: `{lsrc}`",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("Set Links Channel", callback_data="ask_set_links_channel"),
                 InlineKeyboardButton("Remove", callback_data="cmd_del_links_channel")],
                [InlineKeyboardButton("Add Links Source", callback_data="ask_add_links_source"),
                 InlineKeyboardButton("Del Links Source", callback_data="ask_del_links_source")],
                [InlineKeyboardButton("Toggle Links", callback_data="cmd_toggle_links")],
                [InlineKeyboardButton("\u00AB Back", callback_data="menu_main")],
            ]))
    elif data == "menu_settings":
        dup = "ON" if CHECK_DUPLICATES else "OFF"
        await query.edit_message_text(
            f"\u2699\uFE0F **Settings**\n\nBatch Size: `{BATCH_SIZE}`\nDuplicate Check: **{dup}**",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("Set Batch Size", callback_data="ask_set_batch")],
                [InlineKeyboardButton("Toggle Duplicates", callback_data="cmd_toggle_dup")],
                [InlineKeyboardButton("\u00AB Back", callback_data="menu_main")],
            ]))
    elif data == "cmd_botstatus":
        await query.answer("Loading...")
        await client.send_message(query.from_user.id, "/botstatus")
    elif data == "cmd_serverstatus":
        await query.answer("Loading...")
        await client.send_message(query.from_user.id, "/serverstatus")
    elif data == "cmd_view_ids":
        await query.answer("Exporting...")
        await client.send_message(query.from_user.id, "/view_ids")
    elif data == "cmd_logs":
        await query.answer("Fetching logs...")
        await client.send_message(query.from_user.id, "/logs")
    elif data == "cmd_update":
        await query.answer("Starting update...")
        await client.send_message(query.from_user.id, "/update")
    elif data == "cmd_toggle_links":
        await query.answer()
        await client.send_message(query.from_user.id, "/toggle_links")
    elif data == "cmd_toggle_dup":
        await query.answer()
        await client.send_message(query.from_user.id, "/toggle_dup")
    elif data == "cmd_del_links_channel":
        await query.answer()
        await client.send_message(query.from_user.id, "/del_links_channel")
    elif data == "ask_set_links_channel":
        await query.edit_message_text("Send the links channel ID:\n`/set_links_channel -100XXXXXXXXX`", reply_markup=back_btn())
    elif data == "ask_set_batch":
        await query.edit_message_text("Send the batch size:\n`/set_batch 500`", reply_markup=back_btn())
    elif data == "ask_add_links_source":
        await query.edit_message_text("Send source channel IDs:\n`/add_links_source -100XXX -100YYY`", reply_markup=back_btn())
    elif data == "ask_del_links_source":
        await query.edit_message_text("Send source channel IDs to remove:\n`/del_links_source -100XXX`", reply_markup=back_btn())
    else:
        await query.answer("Unknown action.")

# --- ADMIN COMMANDS ---

@bot.on_message(filters.command(["add_source", "add_target", "del_source", "del_target"]) & filters.user(ADMINS))
async def manage_ids(client, message):
    global SOURCE_CHANNELS, TARGET_CHANNELS
    cmd = message.command[0]
    if len(message.command) < 2:
        return await message.reply(f"Usage: `/{cmd} ID1 ID2 ID3 ...`")
    input_ids = message.command[1:]
    success_ids = []
    failed_ids = []
    for raw_id in input_ids:
        try:
            clean_id = int(re.sub(r'[\[\],]', '', raw_id))
            if cmd == "add_source":
                if clean_id not in SOURCE_CHANNELS:
                    SOURCE_CHANNELS.append(clean_id)
                    success_ids.append(str(clean_id))
            elif cmd == "add_target":
                if clean_id not in TARGET_CHANNELS:
                    TARGET_CHANNELS.append(clean_id)
                    success_ids.append(str(clean_id))
            elif cmd == "del_source":
                if clean_id in SOURCE_CHANNELS:
                    SOURCE_CHANNELS.remove(clean_id)
                    success_ids.append(str(clean_id))
            elif cmd == "del_target":
                if clean_id in TARGET_CHANNELS:
                    TARGET_CHANNELS.remove(clean_id)
                    success_ids.append(str(clean_id))
        except ValueError:
            failed_ids.append(raw_id)
    if success_ids or failed_ids:
        await asyncio.to_thread(save_db_settings)
        response = ""
        if success_ids:
            response += f"\u2705 **Processed:** `{len(success_ids)} IDs`\n"
        if failed_ids:
            response += f"\u274C **Invalid IDs:** `{len(failed_ids)} entries`"
        await message.reply(response)

@bot.on_message(filters.command("set_batch") & filters.user(ADMINS))
async def update_batch(client, message):
    global BATCH_SIZE
    if len(message.command) < 2:
        return await message.reply("Usage: `/set_batch 1000`")
    try:
        BATCH_SIZE = int(message.command[1])
        await asyncio.to_thread(save_db_settings)
        await message.reply(f"\u2705 BATCH_SIZE updated to `{BATCH_SIZE}`.")
    except ValueError:
        await message.reply("Invalid number.")

@bot.on_message(filters.command("toggle_dup") & filters.user(ADMINS))
async def toggle_duplicate_cmd(client, message):
    global CHECK_DUPLICATES
    CHECK_DUPLICATES = not CHECK_DUPLICATES
    await asyncio.to_thread(save_db_settings)
    status = "ENABLED" if CHECK_DUPLICATES else "DISABLED"
    await message.reply(f"\U0001F504 Duplicate Checking is now **{status}**.")

# --- LINKS CHANNEL COMMANDS ---

@bot.on_message(filters.command("set_links_channel") & filters.user(ADMINS))
async def set_links_channel_cmd(client, message):
    global LINKS_CHANNEL
    if len(message.command) < 2:
        return await message.reply("Usage: `/set_links_channel -100XXXXXXXXXX`")
    raw = message.command[1]
    try:
        LINKS_CHANNEL = int(re.sub(r'[\[\],]', '', raw))
    except ValueError:
        LINKS_CHANNEL = raw
    await asyncio.to_thread(save_db_settings)
    await message.reply(f"\u2705 Links channel set to `{LINKS_CHANNEL}`.")

@bot.on_message(filters.command("del_links_channel") & filters.user(ADMINS))
async def del_links_channel_cmd(client, message):
    global LINKS_CHANNEL, LINKS_FORWARDING_ENABLED
    LINKS_CHANNEL = None
    LINKS_FORWARDING_ENABLED = False
    await asyncio.to_thread(save_db_settings)
    await message.reply("\U0001F5D1\uFE0F Links channel removed. Links forwarding disabled.")

@bot.on_message(filters.command("toggle_links") & filters.user(ADMINS))
async def toggle_links_cmd(client, message):
    global LINKS_FORWARDING_ENABLED
    if not LINKS_CHANNEL:
        return await message.reply("\u26A0\uFE0F No links channel set yet.\nUse `/set_links_channel ID` first.")
    LINKS_FORWARDING_ENABLED = not LINKS_FORWARDING_ENABLED
    await asyncio.to_thread(save_db_settings)
    status = "ENABLED" if LINKS_FORWARDING_ENABLED else "DISABLED"
    await message.reply(f"\U0001F517 Links & text forwarding is now **{status}**.")

@bot.on_message(filters.command(["add_links_source", "del_links_source"]) & filters.user(ADMINS))
async def manage_links_sources(client, message):
    global LINKS_SOURCE_CHANNELS
    cmd = message.command[0]
    if len(message.command) < 2:
        return await message.reply(f"Usage: `/{cmd} ID1 ID2 ...`")
    added, removed, invalid = [], [], []
    for raw in message.command[1:]:
        raw = re.sub(r'[\[\],]', '', raw)
        try:
            ch_id = int(raw)
        except ValueError:
            invalid.append(raw)
            continue
        if cmd == "add_links_source":
            if ch_id not in LINKS_SOURCE_CHANNELS:
                LINKS_SOURCE_CHANNELS.append(ch_id)
                added.append(ch_id)
        else:
            if ch_id in LINKS_SOURCE_CHANNELS:
                LINKS_SOURCE_CHANNELS.remove(ch_id)
                removed.append(ch_id)
    await asyncio.to_thread(save_db_settings)
    lines = []
    if added:   lines.append(f"Added: {added}")
    if removed: lines.append(f"Removed: {removed}")
    if invalid: lines.append(f"Invalid: {invalid}")
    await message.reply("\n".join(lines) or "Nothing changed.")

@bot.on_message(filters.command("botstatus") & filters.user(ADMINS))
async def show_status(client, message):
    curr_idx, curr_count = await asyncio.to_thread(get_distribution_state)
    total_targets = len(TARGET_CHANNELS)
    total_sources = len(SOURCE_CHANNELS)
    total_fwd = await asyncio.to_thread(get_total_stats)
    progress = round(((curr_idx + (curr_count / BATCH_SIZE)) / total_targets) * 100, 2) if total_targets > 0 else 0
    next_target = TARGET_CHANNELS[curr_idx % total_targets] if total_targets > 0 else "N/A"
    dup_status = "ON" if CHECK_DUPLICATES else "OFF"
    links_status = "OFF"
    if LINKS_CHANNEL:
        links_status = f"ON -> `{LINKS_CHANNEL}`" if LINKS_FORWARDING_ENABLED else f"OFF (set: `{LINKS_CHANNEL}`)"
    status_text = (
        f"**\U0001F4CA Bot Statistics**\n\n"
        f"**Total Forwarded:** `{total_fwd}`\n"
        f"**Rotation:** `{progress}%` complete\n"
        f"**Next Target ID:** `{next_target}`\n"
        f"**Batch Status:** `{curr_count}/{BATCH_SIZE}`\n"
        f"**Duplicates Checking:** `{dup_status}`\n"
        f"**Links Forwarding:** `{links_status}`\n\n"
        f"**Sources:** `{total_sources}` channels\n"
        f"**Targets:** `{total_targets}` channels\n\n"
        f"*To see full lists, use* `/view_ids`"
    )
    await message.reply(status_text)

@bot.on_message(filters.command("view_ids") & filters.user(ADMINS))
async def view_ids(client, message):
    source_list = "\n".join(map(str, SOURCE_CHANNELS)) or "(none)"
    target_list = "\n".join(map(str, TARGET_CHANNELS)) or "(none)"
    links_src_list = "\n".join(map(str, LINKS_SOURCE_CHANNELS)) or "(none)"
    links_ch_str = str(LINKS_CHANNEL) if LINKS_CHANNEL else "(not set)"
    links_enabled_str = "ON" if LINKS_FORWARDING_ENABLED else "OFF"
    full_text = (
        f"BOT ID CONFIGURATION\n========================\n\n"
        f"SOURCE CHANNELS ({len(SOURCE_CHANNELS)}):\n------------------------\n{source_list}\n\n"
        f"TARGET CHANNELS ({len(TARGET_CHANNELS)}):\n------------------------\n{target_list}\n\n"
        f"LINKS CHANNEL:\n------------------------\n{links_ch_str}\nStatus: {links_enabled_str}\n\n"
        f"LINKS SOURCE CHANNELS ({len(LINKS_SOURCE_CHANNELS)}):\n------------------------\n{links_src_list}"
    )
    file_buffer = io.BytesIO(full_text.encode())
    file_buffer.name = "channel_ids.txt"
    await message.reply_document(document=file_buffer, caption=f"\u2705 **ID List Exported**")

@bot.on_message(filters.command("logs") & filters.user(ADMINS))
async def send_logs(client, message):
    log_file = "bot.txt"
    if not os.path.exists(log_file):
        return await message.reply("\U0001F4ED No log file found yet.")
    with open(log_file, "rb") as f:
        file_buffer = io.BytesIO(f.read())
    file_buffer.name = "bot.txt"
    await message.reply_document(document=file_buffer, caption="\U0001F4CB bot.txt")

@bot.on_message(filters.command("serverstatus") & filters.user(ADMINS))
async def server_status(client, message):
    cpu_percent = await asyncio.to_thread(psutil.cpu_percent, 1)
    cpu_count = psutil.cpu_count()
    ram = psutil.virtual_memory()
    ram_used = ram.used / (1024 ** 3)
    ram_total = ram.total / (1024 ** 3)
    ram_percent = ram.percent
    disk = psutil.disk_usage('/')
    disk_used = disk.used / (1024 ** 3)
    disk_total = disk.total / (1024 ** 3)
    disk_percent = disk.percent
    net = psutil.net_io_counters()
    net_sent = net.bytes_sent / (1024 ** 3)
    net_recv = net.bytes_recv / (1024 ** 3)
    boot_time = psutil.boot_time()
    uptime_seconds = int(time.time() - boot_time)
    uptime_hours = uptime_seconds // 3600
    uptime_minutes = (uptime_seconds % 3600) // 60
    def bar(percent):
        filled = int(percent / 10)
        return "\u2588" * filled + "\u2591" * (10 - filled)
    status_text = (
        f"**\U0001F5A5\uFE0F Server Status**\n\n"
        f"**CPU**\n`{bar(cpu_percent)}` {cpu_percent}%\nCores: `{cpu_count}`\n\n"
        f"**RAM**\n`{bar(ram_percent)}` {ram_percent}%\nUsed: `{ram_used:.2f} GB` / `{ram_total:.2f} GB`\n\n"
        f"**Disk**\n`{bar(disk_percent)}` {disk_percent}%\nUsed: `{disk_used:.2f} GB` / `{disk_total:.2f} GB`\n\n"
        f"**Network**\n\u2191 Sent: `{net_sent:.2f} GB`\n\u2193 Recv: `{net_recv:.2f} GB`\n\n"
        f"**Uptime:** `{uptime_hours}h {uptime_minutes}m`"
    )
    await message.reply(status_text)

@bot.on_message(filters.command("update") & filters.user(ADMINS))
async def update_and_restart(client, message):
    msg = await message.reply("\U0001F504 **Pulling latest code from git...**")
    try:
        subprocess.run(["git", "fetch", "origin", "main"], capture_output=True, text=True, timeout=60, check=True)
        reset_result = subprocess.run(["git", "reset", "--hard", "origin/main"], capture_output=True, text=True, timeout=60)
        pull_output = reset_result.stdout.strip() or reset_result.stderr.strip()
    except FileNotFoundError:
        return await msg.edit("\u274C `git` not found in the container.")
    except subprocess.TimeoutExpired:
        return await msg.edit("\u274C git fetch timed out after 60s.")
    except subprocess.CalledProcessError as e:
        return await msg.edit(f"\u274C git fetch failed:\n`{e.stderr.strip()}`")
    except Exception as e:
        return await msg.edit(f"\u274C Update failed:\n`{e}`")
    if reset_result.returncode != 0:
        return await msg.edit(f"\u274C **git reset failed:**\n```\n{pull_output}\n```")
    already_up = "already up to date" in pull_output.lower()
    status_line = "\u2705 Already up to date." if already_up else f"\u2705 Updated:\n```\n{pull_output}\n```"
    pip_note = ""
    if not already_up and "requirements.txt" in pull_output:
        try:
            pip_result = subprocess.run([sys.executable, "-m", "pip", "install", "-r", "requirements.txt", "-q"], capture_output=True, text=True, timeout=120)
            pip_note = "\n\U0001F4E6 Dependencies re-installed." if pip_result.returncode == 0 else f"\n\u26A0\uFE0F pip install failed:\n```{pip_result.stderr[:300]}```"
        except Exception as e:
            pip_note = f"\n\u26A0\uFE0F pip install error: `{e}`"
    await msg.edit(f"{status_line}{pip_note}\n\n\u267B\uFE0F **Restarting bot...**")
    await asyncio.sleep(1)
    try:
        await app.stop()
        await bot.stop()
    except Exception:
        pass
    os.execv(sys.executable, [sys.executable] + sys.argv)

# --- FORWARDER ---

@app.on_message()
async def forward_messages(client, message):
    if getattr(message, "edit_date", None):
        return
    if message.text and message.text.startswith("/"):
        return
    if message.chat.id not in SOURCE_CHANNELS:
        return
    if LINKS_FORWARDING_ENABLED and LINKS_CHANNEL:
        active_links_sources = LINKS_SOURCE_CHANNELS if LINKS_SOURCE_CHANNELS else SOURCE_CHANNELS
        if message.chat.id in active_links_sources:
            content = message.text or message.caption or ""
            is_text = bool(message.text and not message.photo)
            is_photo = bool(message.photo)
            if (is_text or is_photo) and quality_pattern.search(content):
                while True:
                    try:
                        await message.copy(LINKS_CHANNEL)
                        break
                    except FloodWait as e:
                        await asyncio.sleep(e.value)
                    except Exception as e:
                        log.error(f"[Links] forward error: {e}")
                        break
    if not (message.video or message.document):
        return
    media = message.video or message.document
    file_hash = media.file_unique_id
    if CHECK_DUPLICATES and await asyncio.to_thread(is_duplicate, file_hash):
        return
    chat_id = str(message.chat.id)
    async with dist_lock:
        last_id = await asyncio.to_thread(get_last_forwarded, chat_id)
        if message.id <= last_id:
            return
        current_target_index, message_count = await asyncio.to_thread(get_distribution_state)
        total_targets = len(TARGET_CHANNELS)
        if total_targets == 0:
            return
        target_chat_id = TARGET_CHANNELS[current_target_index % total_targets]
        next_message_count = message_count + 1
        next_target_index = current_target_index
        if next_message_count >= BATCH_SIZE:
            next_message_count = 0
            next_target_index = (current_target_index + 1) % total_targets
        while True:
            try:
                await message.copy(target_chat_id)
                await asyncio.to_thread(save_last_forwarded, chat_id, message.id)
                await asyncio.to_thread(save_distribution_state, next_target_index, next_message_count)
                if CHECK_DUPLICATES:
                    await asyncio.to_thread(save_hash, file_hash)
                await asyncio.to_thread(increment_stats)
                break
            except FloodWait as e:
                await asyncio.sleep(e.value)
            except Exception as e:
                log.error(f"Error forwarding: {e}")
                break

# --- Health Check (Koyeb) ---

async def handle_health_check(reader, writer):
    try:
        await reader.read(1024)
        body = b"OK"
        response = (
            b"HTTP/1.1 200 OK\r\n"
            b"Content-Type: text/plain\r\n"
            b"Content-Length: " + str(len(body)).encode() + b"\r\n"
            b"Connection: close\r\n"
            b"\r\n" + body
        )
        writer.write(response)
        await writer.drain()
    except Exception:
        pass
    finally:
        writer.close()

async def start_health_server():
    server = await asyncio.start_server(handle_health_check, "0.0.0.0", 8080)
    log.info("Health check server listening on port 8080")
    return server

# --- Start ---

async def main():
    await asyncio.to_thread(load_all_settings)
    await start_health_server()
    await app.start()
    await bot.start()
    me = await bot.get_me()
    log.info(f"Logged in as: {me.first_name}")
    startup_text = (
        f"\U0001F916 **Bot Started!**\n\n**Name:** {me.first_name}\n**Username:** @{me.username}\n"
        f"**Sources:** `{len(SOURCE_CHANNELS)}` channels\n**Targets:** `{len(TARGET_CHANNELS)}` channels\n"
        f"**Links Channel:** `{'Set' if LINKS_CHANNEL else 'Not Set'}`\n\n\u2705 Bot is online and ready!"
    )
    for admin in ADMINS:
        try:
            await bot.send_message(admin, startup_text)
        except Exception:
            pass
    await asyncio.Event().wait()

if __name__ == "__main__":
    asyncio.get_event_loop().run_until_complete(main())
