import asyncio
import json
import logging
import os
import re
import tempfile
from contextlib import suppress

import httpx
from aiohttp import web
from aiogram import Bot, Dispatcher, Router, F, html
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandObject
from aiogram.types import Message, CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup
from aiogram.webhook.aiohttp_server import SimpleRequestHandler, setup_application
from dotenv import load_dotenv

# ========== Load Environment Variables ==========
load_dotenv()

# ========== Logging Configuration ==========
logging.basicConfig(
    format="%(asctime)s - %(levelname)s - %(message)s", level=logging.WARNING
)

# ========== Configuration & Constants ==========
TOKEN = os.getenv("BOT_TOKEN")
ADMIN_ID = os.getenv("ADMIN_ID")
TARGET_URL = os.getenv("URL")
WEBHOOK_HOST = os.getenv("WEBHOOK_HOST")

if not all([TOKEN, ADMIN_ID, WEBHOOK_HOST]):
    raise ValueError("Missing essential environment variables.")

MAX_MSG_LEN = 15
BLOCK_FILE = "/app/data/blocked_users.json"
WHITE_FILE = "/app/data/whitelist.json"

router = Router()

# ========== File/Set Management ==========
def load_set(path):
    with suppress(Exception):
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                return set(json.load(f))
    return set()

def _sync_save_atomic(path, data_set):
    # Perform the actual blocking file operations atomically
    dir_name = os.path.dirname(path) or "."
    fd, temp_path = tempfile.mkstemp(dir=dir_name, text=True)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(list(data_set), f)
    # Atomic rename to prevent data corruption
    os.replace(temp_path, path)

async def save_set(path, data_set):
    dataset_copy = data_set.copy()
    with suppress(Exception):
        await asyncio.to_thread(_sync_save_atomic, path, dataset_copy)

blocked_users = load_set(BLOCK_FILE)
whitelist_users = load_set(WHITE_FILE)

async def update_user_status(user_id: str, to_block: bool):
    if to_block:
        blocked_users.add(user_id)
        whitelist_users.discard(user_id)
    else:
        whitelist_users.add(user_id)
        blocked_users.discard(user_id)
    await save_set(BLOCK_FILE, blocked_users)
    await save_set(WHITE_FILE, whitelist_users)


# ========== Helpers ==========
async def send_temp_message(message: Message, text: str, delay: int = 30, **kwargs):
    # Send message and schedule an asyncio task to delete it later
    msg = await message.reply(text, **kwargs)
    
    async def delete_message_job():
        await asyncio.sleep(delay)
        with suppress(Exception):
            await msg.delete()
            
    asyncio.create_task(delete_message_job())
    return msg

def extract_user_id(text: str):
    # Strict extraction
    match = re.search(r"(?:User ID:|用户ID:)\s*(\d+)", text or "")
    return match.group(1) if match else None

def render_notice(action: str, user_id: str, user_obj=None, reason: str = ""):
    lines = [f"{action} user"]
    if user_obj:
        name = html.quote(getattr(user_obj, "first_name", "") or "")
        uname = html.quote(getattr(user_obj, "username", "") or "")
        if name or uname:
            lines.append(f"Name: {name} (@{uname})" if uname else f"Name: {name}")
    lines.append(f"User ID: {user_id}")
    if reason:
        lines.append(f"Reason: {html.quote(reason)}")
    return "\n".join(lines)


# ========== Core Bot Actions ==========
async def handle_ban_action(
    bot: Bot,
    user_id: str,
    reason: str,
    is_ban: bool,
    notify_admin: bool = False,
):
    await update_user_status(user_id, to_block=is_ban)

    if not is_ban:
        with suppress(Exception):
            await bot.send_message(user_id, "Unbanned!")

    if notify_admin:
        user_obj = None
        with suppress(Exception):
            user_obj = await bot.get_chat(int(user_id))
        action_str = "Banned" if is_ban else "Unbanned"
        await bot.send_message(
            ADMIN_ID, render_notice(action_str, user_id, user_obj, reason)
        )


# ========== Command Handlers ==========
@router.message(Command("start"))
async def start(message: Message, bot: Bot):
    user = message.from_user
    if not user or message.chat.type != "private":
        return
    uid = str(user.id)

    if uid == ADMIN_ID:
        return await send_temp_message(message, "I'm online Master!")
    if uid in blocked_users:
        return
    if uid in whitelist_users:
        return await send_temp_message(message, "Verified.")
    await message.reply('Send "Hi" to complete verification (case-sensitive)')

@router.message(Command("s"))
async def s_command(message: Message, bot: Bot):
    uid = str(message.from_user.id)
    if uid in blocked_users:
        return
    if uid != ADMIN_ID:
        return await handle_ban_action(
            bot, uid, "Unauthorized /s command", is_ban=True, notify_admin=True
        )

    with suppress(httpx.RequestError):
        async with httpx.AsyncClient(follow_redirects=True) as client:
            res = await client.get(TARGET_URL, timeout=10.0)
            return await send_temp_message(
                message, "OK" if res.status_code == 200 else "Er"
            )
    await send_temp_message(message, "Er")

@router.message(Command("ban", "unban"))
async def toggle_ban(message: Message, command: CommandObject, bot: Bot):
    if str(message.from_user.id) != ADMIN_ID:
        return

    is_ban = command.command == "ban"
    
    # Try getting target ID from reply first, then from args
    uid_target = None
    if message.reply_to_message and message.reply_to_message.text:
        uid_target = extract_user_id(message.reply_to_message.text)
    
    if not uid_target and command.args:
        uid_target = command.args.split()[0]

    if not uid_target:
        return await send_temp_message(
            message,
            f"Usage: /{command.command} [user_id] or reply",
        )

    if not is_ban and uid_target not in blocked_users:
        return await send_temp_message(
            message, f"User {uid_target} was not banned"
        )

    reason = f"Manual {'ban' if is_ban else 'unban'}"
    await handle_ban_action(bot, uid_target, reason, is_ban, notify_admin=False)

    chat = None
    with suppress(Exception):
        chat = await bot.get_chat(int(uid_target))
    await send_temp_message(
        message,
        render_notice("Banned" if is_ban else "Unbanned", uid_target, chat, reason),
    )

# Matches any text message that does not start with "/"
@router.message(F.text & ~F.text.startswith("/"))
async def forward_to_admin(message: Message, bot: Bot):
    user = message.from_user
    if not message.text or not user:
        return

    chat_id = str(message.chat.id)
    if chat_id in blocked_users:
        return

    # User Logic
    if chat_id != ADMIN_ID:
        if message.text == "Hi" and chat_id not in whitelist_users:
            await update_user_status(chat_id, to_block=False)
            await send_temp_message(message, "Success!")
            return

        if chat_id not in whitelist_users:
            return await handle_ban_action(
                bot, chat_id, "Immediate Ban: Unauthorized message", is_ban=True, notify_admin=False
            )

        if len(message.text) > MAX_MSG_LEN:
            return await handle_ban_action(
                bot, chat_id, "Message too long", is_ban=True, notify_admin=False
            )

        # Ban immediately if verified user sends "Hi" again
        if message.text == "Hi":
            return await handle_ban_action(
                bot, chat_id, "Immediate Ban: Sent 'Hi' after verification", is_ban=True, notify_admin=False
            )

        # Forwarding (Escape HTML to prevent bad requests)
        safe_first_name = html.quote(user.first_name)
        safe_username = html.quote(user.username) if user.username else 'No username'
        safe_text = html.quote(message.text)
        
        content = f"From user: {safe_first_name} (@{safe_username})\nUser ID: {chat_id}\n---\n{safe_text}"
        markup = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="Ban", callback_data=f"ban_{chat_id}")]
        ])
        with suppress(Exception):
            await bot.send_message(ADMIN_ID, content, reply_markup=markup)
            await send_temp_message(message, "Forwarded, please wait for a reply.")
        return

    # Admin Logic
    if message.reply_to_message:
        target_id = extract_user_id(message.reply_to_message.text)
        if not target_id:
            return await send_temp_message(message, "Please reply to a message with a User ID.")

        with suppress(Exception):
            # Send plain text back to user (not affected by global ParseMode if not specified)
            await bot.send_message(target_id, message.text)
            await send_temp_message(message, "Replied.")

@router.callback_query(F.data.startswith("ban_"))
async def button_callback(query: CallbackQuery, bot: Bot):
    await query.answer()
    if str(query.from_user.id) != ADMIN_ID:
        return

    target_id = query.data.split("_")[1]
    await handle_ban_action(
        bot, target_id, "Quick ban via button", is_ban=True, notify_admin=True
    )
    with suppress(Exception):
        if query.message:
            await query.message.edit_reply_markup(reply_markup=None)

async def on_startup(bot: Bot):
    # Setup webhook and send startup notification
    port = int(os.getenv("WEBHOOK_PORT", 5005))
    path = f"/{os.getenv('WEBHOOK_PATH', '').lstrip('/')}"
    url = f"{WEBHOOK_HOST.rstrip('/')}{path}"
    
    await bot.set_webhook(
        url=url,
        secret_token=os.getenv("WEBHOOK_SECRET_TOKEN"),
        allowed_updates=["message", "callback_query"]
    )
    with suppress(Exception):
        await bot.send_message(chat_id=ADMIN_ID, text="Hi Master I'm online")
    print(f"Bot is working (Webhook URL: {url})")

# ========== Main ==========
def main():
    bot = Bot(token=TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher()
    dp.include_router(router)
    dp.startup.register(on_startup)

    # Initialize aiohttp Application for Webhook
    app = web.Application()
    
    port = int(os.getenv("WEBHOOK_PORT", 5005))
    path = f"/{os.getenv('WEBHOOK_PATH', '').lstrip('/')}"
    
    webhook_requests_handler = SimpleRequestHandler(
        dispatcher=dp,
        bot=bot,
        secret_token=os.getenv("WEBHOOK_SECRET_TOKEN")
    )
    webhook_requests_handler.register(app, path=path)
    setup_application(app, dp, bot=bot)

    # Start the web server
    web.run_app(
        app, 
        host=os.getenv("WEBHOOK_LISTEN", "0.0.0.0"), 
        port=port
    )

if __name__ == "__main__":
    main()