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
from aiohttp import ClientPayloadError, ContentLengthError

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
    r"https?://[^\s]*?(?:terabox|teraboxapp|teraboxshare|nephobox|1024tera|teraboxurl|1024terabox|freeterabox|terasharefile|terasharelink|mirrobox|momerybox|teraboxlink|teraboxurl)\.[^\s]+",
    re.IGNORECASE
)

# Configuration
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
API_ENDPOINT = "https://terabox.itxarshman.workers.dev/api"
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
    logger.info(f"Requesting links for URL: {source_url}")
    async with aiohttp.ClientSession() as session:
        try:
            async with session.get(f"{API_ENDPOINT}?url={source_url}") as resp:
                if resp.status == 200:
                    logger.info(f"Successfully retrieved links for {source_url}")
                    return await resp.json()
                logger.error(f"API request failed for {source_url}, status: {resp.status}")
        except Exception as e:
            logger.error(f"Error fetching links for {source_url}: {str(e)}")
    return None

async def download_file(dl_url: str, filename: str, size_mb: float, status_message: Message, attempt: int = 0, max_retries: int = 3):
    """
    Improved resilient downloader with resume support, progress updates,
    and robust retry/backoff for ContentLengthError.
    """
    temp_path = os.path.join(tempfile.gettempdir(), filename)
    start_time = time.time()
    backoff = 2 ** attempt

    try:
        headers = {}
        if os.path.exists(temp_path):
            existing_size = os.path.getsize(temp_path)
            headers["Range"] = f"bytes={existing_size}-"
        else:
            existing_size = 0

        async with sem:
            async with aiohttp.ClientSession() as session:
                async with session.get(dl_url, headers=headers, timeout=aiohttp.ClientTimeout(total=None)) as resp:
                    if resp.status not in (200, 206):
                        raise Exception(f"HTTP {resp.status}")

                    total_size = int(resp.headers.get("Content-Length", 0)) + existing_size
                    total_mb = total_size / (1024 * 1024) if total_size else size_mb
                    mode = "ab" if existing_size else "wb"

                    logger.info(f"Starting download: {filename} (attempt {attempt+1}) from {dl_url}")

                    downloaded = existing_size
                    last_update_time = 0

                    async with aiofiles.open(temp_path, mode) as f:
                        async for chunk in resp.content.iter_chunked(1024 * 256):
                            if not chunk:
                                break
                            await f.write(chunk)
                            downloaded += len(chunk)

                            # Progress update every 5 seconds
                            if status_message and time.time() - last_update_time > 5:
                                elapsed = time.time() - start_time
                                speed_mb_s = downloaded / 1024 / 1024 / elapsed if elapsed > 0 else 0
                                percent = (downloaded / total_size * 100) if total_size else 0
                                try:
                                    await status_message.edit_text(
                                        f"📥 **Downloading** `{filename}`\n"
                                        f"📦 Size: **{total_mb:.2f} MB**\n"
                                        f"⬇️ Progress: **{downloaded / 1024 / 1024:.2f}/{total_mb:.2f} MB** (**{percent:.0f}%**)\n"
                                        f"⚡ Speed: **{speed_mb_s:.2f} MB/s**",
                                        parse_mode="Markdown"
                                    )
                                except TelegramBadRequest as e:
                                    if "message is not modified" not in str(e):
                                        logger.warning(f"Telegram edit error: {e}")
                                last_update_time = time.time()

        # Validate size
        final_size = os.path.getsize(temp_path)
        if total_size and final_size < total_size * 0.9:
            raise ClientPayloadError("Incomplete file: size mismatch")

        logger.info(f"✅ Download complete: {filename} ({final_size / 1024 / 1024:.2f} MB)")

        if status_message:
            try:
                await bot.delete_message(status_message.chat.id, status_message.message_id)
            except Exception:
                pass

        return True, temp_path

    except (ClientPayloadError, ContentLengthError) as e:
        logger.error(f"Download error for {filename}: {e}")
        if attempt < max_retries - 1:
            logger.info(f"Retrying {filename} after {backoff}s...")
            await asyncio.sleep(backoff)
            return await download_file(dl_url, filename, size_mb, status_message, attempt + 1, max_retries)
        else:
            logger.error(f"❌ Failed to download {filename} after {max_retries} retries.")
            if os.path.exists(temp_path):
                os.remove(temp_path)
            if status_message:
                await status_message.edit_text(
                    f"❌ Failed to download `{filename}` after {max_retries} attempts.",
                    parse_mode="Markdown"
                )
            return False, None

    except Exception as e:
        logger.error(f"Unexpected error downloading {filename}: {e}")
        if os.path.exists(temp_path):
            os.remove(temp_path)
        if attempt < max_retries - 1:
            await asyncio.sleep(backoff)
            return await download_file(dl_url, filename, size_mb, status_message, attempt + 1, max_retries)
        return False, None


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

async def process_file(
    link: dict,
    source_url: str,
    original_chat_id: int = None,
    source_type: str = "user",
    status_message: Message = None,
    original_message: Message = None
):
    name = link.get("name", "unknown")
    size_mb = link.get("size_mb", 0)
    size_gb = size_mb / 1024
    logger.info(f"Processing file: {name}, size: {size_mb} MB, source: {source_type}")

    config = await get_config()

    # File validation and user feedback
    if status_message and source_type != "channel":
        if size_gb > 2:
            logger.warning(f"File {name} size {size_gb:.2f} GB exceeds 2 GB limit")
            await status_message.edit_text(
                f"❌ File `{name}` is too large (**{size_gb:.2f} GB**). Max 2 GB.",
                parse_mode="Markdown"
            )
            return

        if not name.lower().endswith(('.mp4', '.mkv', '.avi', '.mov', '.webm')):
            logger.info(f"Skipping non-video file: {name}")
            await status_message.edit_text(
                f"ℹ️ Skipped non-video file: `{name}`. Only video files are processed.",
                parse_mode="Markdown"
            )
            return

        await status_message.edit_text(f"📥 Found: `{name}`. Starting download...", parse_mode="Markdown")

    file_path = None
    new_link = None

    async with sem:
        try:
            for attempt in range(4):
                if attempt == 0:
                    dl_url = link.get("download_url") or link.get("original_url")
                    label = "proxied primary"
                elif attempt == 1:
                    dl_url = link.get("original_url")
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
                    dl_url = new_link.get("download_url") or new_link.get("original_url")
                    label = "new proxied"
                elif attempt == 3:
                    if not new_link:
                        break
                    dl_url = new_link.get("original_url")
                    label = "new direct"
                else:
                    break

                logger.info(f"Attempting {label} download for {name}")
                success, file_path = await download_file(dl_url, name, size_mb, status_message)
                if success:
                    break
                logger.warning(f"{label.capitalize()} failed for {name}, retrying...")

            if not file_path:
                logger.error(f"File {name} failed to download after all retries")
                if status_message or source_type != "channel" or config.get("channel_broadcast_enabled"):
                    await bot.send_message(
                        original_chat_id,
                        f"❌ Failed to download `{name}` from `{source_url}` after all attempts.",
                        parse_mode="Markdown"
                    )
                return

            logger.info(f"Successfully downloaded {name}")

            # Sending / Broadcasting logic
            if source_type in ("user", "admin"):
                await send_video_to_user(
                    file_path,
                    name,
                    original_chat_id,
                    reply_to_message_id=original_message.message_id if original_message else None
                )

            if source_type == "admin":
                await broadcast_video(file_path, name, 'admin')
            elif source_type == "channel" and config.get("channel_broadcast_enabled"):
                await broadcast_video(file_path, name, 'channel')

        except Exception as e:
            logger.error(f"Error processing {name}: {str(e)}")
            if status_message or source_type != "channel" or config.get("channel_broadcast_enabled"):
                await bot.send_message(
                    original_chat_id,
                    f"❌ Error processing `{name}` from `{source_url}`: {str(e)[:100]}",
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
