import asyncio
import aiohttp
import re
import os
import tempfile
import time
import logging
from aiogram import Bot, Dispatcher, Router, types
from aiogram.types import Message, FSInputFile, BotCommand
from aiogram.filters import Command
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.client.telegram import TelegramAPIServer
from aiogram.exceptions import TelegramBadRequest
from motor.motor_asyncio import AsyncIOMotorClient
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery
from dotenv import load_dotenv
from aiohttp import ClientTimeout

# Load .env file
load_dotenv()

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler('teradownloader.log')
    ]
)
logger = logging.getLogger(__name__)

# TeraBox-specific URL regex
LINK_REGEX = re.compile(
    r"https?://[^\s]*?(?:terabox|teraboxapp|teraboxshare|nephobox|1024tera|teraboxurl|teraboxurl|1024terabox|freeterabox|terasharefile|terasharelink|mirrobox|momerybox|teraboxlink|teraboxurl)\.[^\s]+",
    re.IGNORECASE
)

# Configuration
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
API_BASE = "https://terabox.itxarshman.workers.dev"
SELF_HOSTED_API = "http://tgapi.arshman.space:8088"

# MongoDB setup
MONGO_URI = os.getenv("MONGO_URI", "")
mongo = AsyncIOMotorClient(MONGO_URI)
db = mongo["teradownloader"]
config_col = db["config"]
broadcast_col = db["broadcasted"]
admins_col = db["admins"]

# Default global config
DEFAULT_CONFIG = {
    "_id": "global",
    "admin_broadcast_enabled": False,
    "channel_broadcast_enabled": False,
    "broadcast_chats": [-1002780909369],
    "admin_password": "11223344"
}

# Validate required environment variables
if not BOT_TOKEN:
    logger.error("BOT_TOKEN is not set in environment variables")
    raise ValueError("BOT_TOKEN is required")
if not MONGO_URI:
    logger.error("MONGO_URI is not set in environment variables")
    raise ValueError("MONGO_URI is required")
    
session = AiohttpSession(api=TelegramAPIServer.from_base(SELF_HOSTED_API))
bot = Bot(token=BOT_TOKEN, session=session)
dp = Dispatcher()
router = Router(name="terabox_listener")
sem = asyncio.Semaphore(50)
pending_auth = {}

async def get_config():
    config = await config_col.find_one({"_id": "global"})
    if not config:
        await config_col.insert_one(DEFAULT_CONFIG)
        logger.info("Inserted new global configuration.")
        return DEFAULT_CONFIG
    needs_update = False
    for key, default_value in DEFAULT_CONFIG.items():
        if key not in config:
            config[key] = default_value
            needs_update = True
            logger.warning(f"Config missing key '{key}'. Added default value: {default_value}")
    if needs_update:
        await config_col.update_one({"_id": "global"}, {"$set": {k: v for k, v in config.items() if k != "_id"}})
        logger.info("Updated existing global configuration with missing keys.")
    return config

async def update_config(update: dict):
    await config_col.update_one({"_id": "global"}, {"$set": update})

async def is_admin(user_id: int) -> bool:
    admin = await admins_col.find_one({"user_id": user_id})
    return admin is not None

async def add_admin(user_id: int, username: str = None, full_name: str = None):
    await admins_col.update_one(
        {"user_id": user_id},
        {"$set": {
            "user_id": user_id,
            "username": username,
            "full_name": full_name,
            "added_at": time.time()
        }},
        upsert=True
    )

async def set_bot_commands(user_id: int = None):
    is_user_admin = user_id and await is_admin(user_id)
    if is_user_admin:
        commands = [
            BotCommand(command="start", description="Start the bot"),
            BotCommand(command="settings", description="Bot Settings (Admin Only)")
        ]
        await bot.set_my_commands(commands, scope=types.BotCommandScopeChat(chat_id=user_id))
    if not user_id:
        commands = [
            BotCommand(command="start", description="Start the bot")
        ]
        await bot.set_my_commands(commands)
    elif user_id and not is_user_admin:
        commands = [
            BotCommand(command="start", description="Start the bot")
        ]
        await bot.set_my_commands(commands, scope=types.BotCommandScopeChat(chat_id=user_id))

async def get_links(source_url: str):
    api_url = f"{API_BASE}/api?url={source_url}"
    async with aiohttp.ClientSession() as session:
        async with session.get(api_url) as resp:
            data = await resp.json()

    if not data.get("success") or not data.get("files"):
        return None

    links = []
    for f in data["files"]:
        size_str = f.get("size", "0 MB").split()[0]
        size_mb = float(size_str) if "MB" in f.get("size", "") else float(size_str) * 1024
        links.append({
            "name": f.get("file_name"),
            "size_mb": size_mb,
            "proxified_url": f.get("proxified_download_url"),  # 🆕 primary URL
            "direct_url": f.get("download_url"),                # 🆕 fallback
        })
    return {"links": links}


PARALLEL_SEGMENTS = 4
CHUNK_SIZE = 4 * 1024 * 1024  # 4 MB

async def fetch_range(session, url, start, end, part_path, status_message=None, idx=0):
    """Download a specific byte range of a file."""
    headers = {"Range": f"bytes={start}-{end}"}
    downloaded = 0
    t0 = time.time()

    async with session.get(url, headers=headers, timeout=ClientTimeout(total=None)) as resp:
        if resp.status not in (200, 206):
            raise Exception(f"Bad status {resp.status} for range {start}-{end}")

        async with aiofiles.open(part_path, "wb") as f:
            async for chunk in resp.content.iter_chunked(CHUNK_SIZE):
                await f.write(chunk)
                downloaded += len(chunk)

    duration = time.time() - t0
    speed = downloaded / (1024 * 1024) / duration if duration > 0 else 0
    print(f"✅ Segment {idx+1} done ({downloaded/1024/1024:.1f} MB @ {speed:.1f} MB/s)")
    return downloaded

async def download_file(url, filename, size_mb, status_message=None):
    """Optimized downloader using parallel ranged requests."""
    total_size = int(size_mb * 1024 * 1024)
    temp_dir = tempfile.gettempdir()
    temp_path = os.path.join(temp_dir, filename)
    part_paths = [f"{temp_path}.part{i}" for i in range(PARALLEL_SEGMENTS)]

    print(f"🚀 Starting download of {filename} ({size_mb:.2f} MB) using {PARALLEL_SEGMENTS} segments...")

    # Prepare byte ranges
    segment_size = math.ceil(total_size / PARALLEL_SEGMENTS)
    ranges = [(i * segment_size, min((i + 1) * segment_size - 1, total_size - 1))
              for i in range(PARALLEL_SEGMENTS)]

    connector = aiohttp.TCPConnector(limit=PARALLEL_SEGMENTS * 2)
    async with aiohttp.ClientSession(connector=connector) as session:
        tasks = [
            fetch_range(session, url, start, end, part_paths[i], status_message, i)
            for i, (start, end) in enumerate(ranges)
        ]

        try:
            results = await asyncio.gather(*tasks)
        except Exception as e:
            print(f"❌ Parallel download failed: {e}")
            for p in part_paths:
                if os.path.exists(p):
                    os.remove(p)
            return False, None

    # Merge all parts
    print("🧩 Merging segments...")
    async with aiofiles.open(temp_path, "wb") as outfile:
        for p in part_paths:
            async with aiofiles.open(p, "rb") as infile:
                while chunk := await infile.read(CHUNK_SIZE):
                    await outfile.write(chunk)
            os.remove(p)

    total_downloaded = sum(results)
    duration = sum(os.path.getsize(p) for p in part_paths) / (1024 * 1024)
    print(f"✅ Download complete: {filename} ({total_downloaded/1024/1024:.2f} MB total)")

    return True, temp_path

async def broadcast_video(file_path: str, video_name: str, broadcast_type: str):
    config = await get_config()
    if broadcast_type == 'admin' and not config["admin_broadcast_enabled"]:
        logger.info(f"Admin broadcast disabled - skipping {video_name}")
        return False
    if broadcast_type == 'channel' and not config["channel_broadcast_enabled"]:
        logger.info(f"Channel broadcast disabled - skipping {video_name}")
        return False
    if await broadcast_col.find_one({"name": video_name}):
        logger.info(f"Duplicate broadcast skipped: {video_name}")
        return False
    chats = config.get("broadcast_chats", [])
    if not chats:
        logger.warning("No broadcast chats configured")
        return False
    broadcast_count = 0
    for bc_chat_id in chats:
        try:
            input_file = FSInputFile(file_path, filename=video_name)
            await bot.send_video(chat_id=bc_chat_id, video=input_file, supports_streaming=True)
            await broadcast_col.insert_one({"name": video_name, "chat_id": bc_chat_id, "timestamp": time.time()})
            broadcast_count += 1
            logger.info(f"📤 Broadcasted {video_name} to chat {bc_chat_id}")
        except Exception as e:
            logger.error(f"❌ Broadcast failed for chat {bc_chat_id}: {str(e)[:100]}")
    if broadcast_count > 0:
        logger.info(f"✅ Broadcast complete: {broadcast_count}/{len(chats)} chats")
        return True
    return False

async def send_video_to_user(file_path: str, video_name: str, chat_id: int, reply_to_message_id: int = None):
    try:
        input_file = FSInputFile(file_path, filename=video_name)
        await bot.send_video(
            chat_id=chat_id,
            video=input_file,
            supports_streaming=True,
            caption=video_name,
            reply_to_message_id=reply_to_message_id,
            parse_mode="Markdown"
        )
        logger.info(f"📤 Sent {video_name} to chat {chat_id}")
        return True
    except Exception as e:
        logger.error(f"❌ Failed to send to chat {chat_id}: {str(e)[:100]}")
        await bot.send_message(chat_id, f"❌ Failed to send `{video_name}`: {str(e)[:100]}", parse_mode="Markdown")
        return False

async def process_file(link: dict, source_url: str, original_chat_id: int = None,
                       source_type: str = "user", status_message: Message = None,
                       original_message: Message = None):
    name = link.get("name", "unknown")
    size_mb = link.get("size_mb", 0)
    size_gb = size_mb / 1024
    logger.info(f"Processing file: {name}, size: {size_mb} MB, source: {source_type}")

    config = await get_config()

    # Notify user before download
    if status_message and source_type != "channel":
        if size_gb > 2:
            await status_message.edit_text(
                f"❌ File `{name}` is too large (**{size_gb:.2f} GB**). Max 2 GB.",
                parse_mode="Markdown",
            )
            return
        if not name.lower().endswith(('.mp4', '.mkv', '.avi', '.mov', '.webm')):
            await status_message.edit_text(
                f"ℹ️ Skipped non-video file: `{name}`. Only video files are processed.",
                parse_mode="Markdown",
            )
            return
        await status_message.edit_text(f"📥 Found: `{name}`. Starting download...", parse_mode="Markdown")

    file_path = None
    new_link = None

    async with sem:
        try:
            for attempt in range(4):
                # Attempt sequence: proxified → direct → refreshed proxified → refreshed direct
                if attempt == 0:
                    dl_url = link.get("proxified_url")
                    label = "proxified"
                elif attempt == 1:
                    dl_url = link.get("direct_url")
                    label = "direct fallback"
                elif attempt == 2:
                    logger.info(f"Refreshing links for {name}")
                    new_resp = await get_links(source_url)
                    if not new_resp or "links" not in new_resp:
                        logger.error(f"Failed to refresh links for {name}")
                        break
                    new_link = next((l for l in new_resp["links"] if l.get("name") == name), None)
                    if not new_link:
                        logger.error(f"File {name} not found in refreshed links")
                        break
                    dl_url = new_link.get("proxified_url")
                    label = "refreshed proxified"
                elif attempt == 3 and new_link:
                    dl_url = new_link.get("direct_url")
                    label = "refreshed direct"
                else:
                    break

                if not dl_url:
                    logger.warning(f"⚠️ Missing URL for {label} attempt of {name}")
                    continue

                logger.info(f"Attempting {label} download for {name}")
                success, file_path = await download_file(dl_url, name, size_mb, status_message)
                if success:
                    break
                logger.warning(f"{label.capitalize()} failed for {name}, retrying...")

            if not file_path:
                logger.error(f"File {name} failed to download after all retries")
                if status_message or source_type != "channel" or config["channel_broadcast_enabled"]:
                    await bot.send_message(
                        original_chat_id,
                        f"❌ Failed to download `{name}` from `{source_url}` after all attempts.",
                        parse_mode="Markdown"
                    )
                return

            # Send video to appropriate destination
            if source_type == "user" or source_type == "admin":
                await send_video_to_user(
                    file_path,
                    name,
                    original_chat_id,
                    reply_to_message_id=original_message.message_id if original_message else None
                )
            if source_type == "admin":
                await broadcast_video(file_path, name, 'admin')
            elif source_type == "channel" and config["channel_broadcast_enabled"]:
                await broadcast_video(file_path, name, 'channel')

        except Exception as e:
            logger.error(f"Error processing {name}: {str(e)}")
            if status_message or source_type != "channel" or config["channel_broadcast_enabled"]:
                await bot.send_message(
                    original_chat_id,
                    f"❌ Error processing `{name}`: {str(e)[:100]}",
                    parse_mode="Markdown"
                )
        finally:
            if file_path and os.path.exists(file_path):
                logger.debug(f"Cleaning up temporary file: {file_path}")
                os.unlink(file_path)




async def process_url(source_url: str, chat_id: int, source_type: str = "user", original_message: Message = None):
    logger.info(f"Processing URL: {source_url} from {source_type} {chat_id}")
    config = await get_config()
    response = await get_links(source_url)
    if not response or "links" not in response:
        logger.error(f"Failed to retrieve links for {source_url}")
        if source_type != "channel" or config["channel_broadcast_enabled"]:
            await bot.send_message(chat_id, f"❌ Failed to retrieve links for `{source_url}`", parse_mode="Markdown")
        return
    links = [link for link in response["links"] if link.get("name", "").lower().endswith(('.mp4', '.mkv', '.avi', '.mov', '.webm'))]
    if not links:
        logger.info(f"No video files found for {source_url}")
        if source_type != "channel" or config["channel_broadcast_enabled"]:
            await bot.send_message(chat_id, f"⚠️ No video files found in `{source_url}`", parse_mode="Markdown")
        return
    for link in links:
        status_message = None
        if source_type != "channel" or config["channel_broadcast_enabled"]:
            name = link.get("name", "unknown")
            status_message = await bot.send_message(chat_id, f"🔍 **Processing:** `{name}`. Initializing...", parse_mode="Markdown")
        asyncio.create_task(process_file(link, source_url, chat_id, source_type, status_message, original_message))

@router.message(Command("start"))
async def start(message: Message):
    logger.info(f"Start command received from user {message.from_user.id}")
    user_is_admin = await is_admin(message.from_user.id)
    welcome_msg = "🤖 **TeraBox Downloader Bot**\n\n"
    if user_is_admin:
        welcome_msg += "👑 Welcome back, Admin!\n\n"
        welcome_msg += "• Send TeraBox links to download videos\n"
        welcome_msg += "• Use /settings to configure bot\n"
    else:
        welcome_msg += "📥 Send me TeraBox links and I'll download videos for you!\n\n"
    await message.answer(welcome_msg, parse_mode="Markdown")

@router.message(Command("settings"))
async def settings_command(message: Message):
    user_id = message.from_user.id
    if await is_admin(user_id):
        await show_settings(message)
    else:
        pending_auth[user_id] = "awaiting_password"
        await message.answer("🔐 Enter admin password to access settings:")

async def show_settings(message: Message):
    config = await get_config()
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(
            text=f"📡 Admin Broadcast: {'✅ ON' if config['admin_broadcast_enabled'] else '❌ OFF'}",
            callback_data="toggle_admin_broadcast"
        )],
        [InlineKeyboardButton(
            text=f"📺 Channel Broadcast: {'✅ ON' if config['channel_broadcast_enabled'] else '❌ OFF'}",
            callback_data="toggle_channel_broadcast"
        )],
        [InlineKeyboardButton(
            text="🆔 Set Broadcast Chat ID(s)",
            callback_data="set_broadcast_id"
        )],
    ])
    try:
        await message.answer(
            build_settings_text(config),
            reply_markup=keyboard,
            parse_mode="Markdown"
        )
    except TelegramBadRequest:
        pass

def build_settings_text(config):
    return (
        f"⚙️ **Bot Settings**\n\n"
        f"📡 Admin Broadcast: {'✅ Enabled' if config['admin_broadcast_enabled'] else '❌ Disabled'}\n"
        f"  _(When enabled, videos from admin links are broadcasted)_\n\n"
        f"📺 Channel Broadcast: {'✅ Enabled' if config['channel_broadcast_enabled'] else '❌ Disabled'}\n"
        f"  _(When enabled, videos from channel posts are broadcasted)_\n\n"
        f"🆔 Broadcast Chats: {', '.join(map(str, config['broadcast_chats'])) if config['broadcast_chats'] else 'None'}"
    )

@router.callback_query()
async def settings_callback(callback: CallbackQuery):
    user_id = callback.from_user.id
    data = callback.data
    if not await is_admin(user_id):
        await callback.answer("❌ You need admin access!", show_alert=True)
        return
    config = await get_config()
    if data == "toggle_admin_broadcast":
        new_state = not config["admin_broadcast_enabled"]
        await update_config({"admin_broadcast_enabled": new_state})
        await update_settings_message(callback, "📡 Admin Broadcast", new_state)
    elif data == "toggle_channel_broadcast":
        new_state = not config["channel_broadcast_enabled"]
        await update_config({"channel_broadcast_enabled": new_state})
        await update_settings_message(callback, "📺 Channel Broadcast", new_state)
    elif data == "set_broadcast_id":
        await callback.message.answer("📨 Send new broadcast chat ID(s), comma-separated:")
        pending_auth[user_id] = "await_broadcast_ids"
        await callback.answer()

async def update_settings_message(callback: CallbackQuery, label: str, state: bool):
    config = await get_config()
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(
            text=f"📡 Admin Broadcast: {'✅ ON' if config['admin_broadcast_enabled'] else '❌ OFF'}",
            callback_data="toggle_admin_broadcast"
        )],
        [InlineKeyboardButton(
            text=f"📺 Channel Broadcast: {'✅ ON' if config['channel_broadcast_enabled'] else '❌ OFF'}",
            callback_data="toggle_channel_broadcast"
        )],
        [InlineKeyboardButton(
            text="🆔 Set Broadcast Chat ID(s)",
            callback_data="set_broadcast_id"
        )],
    ])
    await callback.message.edit_text(
        build_settings_text(config),
        reply_markup=keyboard,
        parse_mode="Markdown"
    )
    await callback.answer(f"{label} turned {'ON' if state else 'OFF'} ✅")

@router.message()
async def handle_message(message: Message):
    user_id = message.from_user.id
    text = (message.text or message.caption or "").strip()
    if text.startswith("/"):
        return
    if user_id in pending_auth:
        state = pending_auth[user_id]
        config = await get_config()
        if state == "awaiting_password":
            if text == config["admin_password"]:
                await add_admin(user_id, message.from_user.username, message.from_user.full_name)
                await message.answer("✅ Password accepted! You are now an admin.")
                await set_bot_commands(user_id)
                del pending_auth[user_id]
                await show_settings(message)
            else:
                await message.answer("❌ Wrong password. Try /settings again.")
                del pending_auth[user_id]
            return
        if state == "await_broadcast_ids":
            try:
                ids = [int(x.strip()) for x in text.split(",") if x.strip()]
                await update_config({"broadcast_chats": ids})
                await message.answer(f"✅ Updated broadcast chats: {ids}")
                del pending_auth[user_id]
                await show_settings(message)
            except ValueError:
                await message.answer("❌ Invalid format. Please enter numeric chat IDs separated by commas.")
                del pending_auth[user_id]
            return
    urls = LINK_REGEX.findall(text)
    if not urls:
        return
    chat_id = message.chat.id
    user_is_admin = await is_admin(user_id)
    source_type = "admin" if user_is_admin else "user"
    logger.info(f"{source_type.capitalize()} {user_id} sent TeraBox URL(s)")
    for url in urls:
        url = url.rstrip('.,!?')
        logger.info(f"Processing {source_type} URL: {url}")
        asyncio.create_task(process_url(url, chat_id, source_type, message))

@router.channel_post()
async def handle_channel_post(message: Message):
    config = await get_config()
    if not config["channel_broadcast_enabled"]:
        logger.debug("Channel broadcast disabled - ignoring channel post")
        return
    text = (message.text or message.caption or "")
    urls = LINK_REGEX.findall(text)
    if not urls:
        return
    chat_id = message.chat.id
    logger.info(f"🔔 Detected TeraBox URL(s) in channel {chat_id}")
    for url in urls:
        url = url.rstrip('.,!?')
        logger.info(f"📥 Processing channel URL: {url}")
        asyncio.create_task(process_url(url, chat_id, "channel", message))

# Attach router
dp.include_router(router)

if __name__ == "__main__":
    async def main():
        await get_config()
        await set_bot_commands()
        logger.info("🚀 Starting TeraDownloader bot")
        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
    asyncio.run(main())
