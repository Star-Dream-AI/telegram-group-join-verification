import asyncio
import hmac
import hashlib
import json
import logging
from contextlib import asynccontextmanager
from typing import Dict, Set
from urllib.parse import parse_qsl

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from aiogram import Bot, Dispatcher, F, types
from aiogram.filters.chat_member_updated import ChatMemberUpdatedFilter, JOIN_TRANSITION
from aiogram.types import ChatPermissions, InlineKeyboardButton, InlineKeyboardMarkup, WebAppInfo, ChatMemberUpdated

import config

# Configure Logging
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

# Pending verifications map: user_id -> set of chat_ids
pending_verifications: Dict[int, Set[int]] = {}

# Telegram Bot & Dispatcher setup
bot = Bot(token=config.BOT_TOKEN) if config.BOT_TOKEN else None
dp = Dispatcher()

def verify_telegram_init_data(init_data: str, bot_token: str) -> dict | None:
    """Validate Telegram WebApp initData using HMAC-SHA256 signature."""
    if not init_data or not bot_token:
        return None
    try:
        parsed_data = dict(parse_qsl(init_data, keep_blank_values=True))
        if "hash" not in parsed_data:
            return None
        received_hash = parsed_data.pop("hash")

        data_check_string = "\n".join(f"{k}={v}" for k, v in sorted(parsed_data.items()))
        secret_key = hmac.new(b"WebAppData", bot_token.encode("utf-8"), hashlib.sha256).digest()
        calculated_hash = hmac.new(secret_key, data_check_string.encode("utf-8"), hashlib.sha256).hexdigest()

        if hmac.compare_digest(calculated_hash, received_hash):
            user_json = parsed_data.get("user")
            return json.loads(user_json) if user_json else None
    except Exception as e:
        logger.error(f"Failed to verify initData: {e}")
    return None

async def verify_cloud_flare_turnstile(token: str) -> bool:
    """Verify Cloudflare Turnstile response token with Cloudflare API."""
    if not config.CF_SECRET_KEY:
        logger.warning("CF_SECRET_KEY is not set. Turnstile verification skipped/failed.")
        return False
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(
                "https://challenges.cloudflare.com/turnstile/v0/siteverify",
                data={
                    "secret": config.CF_SECRET_KEY,
                    "response": token
                }
            )
            result = resp.json()
            logger.info(f"Cloudflare Turnstile verification result: {result}")
            return result.get("success", False)
    except Exception as e:
        logger.error(f"Cloudflare verification network error: {e}")
        return False

# Simple deduplication cache: chat_id_user_id -> timestamp
recent_joins: Set[str] = set()

# Tracks the last group verification notice message_id per chat_id
last_group_verification_msg: Dict[int, int] = {}

async def delete_previous_group_message(chat_id: int):
    """Deletes the previous verification notice message sent by the bot in the given chat."""
    old_msg_id = last_group_verification_msg.get(chat_id)
    if old_msg_id and bot:
        try:
            await bot.delete_message(chat_id=chat_id, message_id=old_msg_id)
            logger.info(f"Deleted previous verification message {old_msg_id} in chat {chat_id}")
        except Exception as e:
            logger.warning(f"Could not delete message {old_msg_id} in chat {chat_id}: {e}")
        finally:
            last_group_verification_msg.pop(chat_id, None)

async def process_new_user(user: types.User, chat: types.Chat):
    """Core logic to restrict and notify a new user upon joining a group."""
    if user.is_bot:
        return

    join_key = f"{chat.id}_{user.id}"
    if join_key in recent_joins:
        logger.debug(f"Skipping duplicate join event for {join_key}")
        return
    recent_joins.add(join_key)
    asyncio.get_event_loop().call_later(10, recent_joins.discard, join_key)

    logger.info(f"Processing new user join: {user.full_name} ({user.id}) in chat: {chat.title} ({chat.id})")

    # 1. Restrict user in group immediately
    restricted_permissions = ChatPermissions(
        can_send_messages=False,
        can_send_audios=False,
        can_send_documents=False,
        can_send_photos=False,
        can_send_videos=False,
        can_send_video_notes=False,
        can_send_voice_notes=False,
        can_send_polls=False,
        can_send_other_messages=False,
        can_add_web_page_previews=False
    )

    try:
        await bot.restrict_chat_member(
            chat_id=chat.id,
            user_id=user.id,
            permissions=restricted_permissions
        )
        logger.info(f"Restricted user {user.id} in chat {chat.id}")
    except Exception as e:
        if "supergroups" in str(e).lower():
            logger.warning(f"⚠️ 无法在群组 {chat.title} ({chat.id}) 中禁言用户：Telegram 规定禁言功能仅支持【超级群组 (Supergroup)】。请在 Telegram 中将群组转为超级群组（如：设置历史消息对新成员可见/设置公开群后再转私密）。")
        else:
            logger.error(f"Failed to restrict user {user.id} in chat {chat.id}: {e}")

    # 2. Record pending verification
    if user.id not in pending_verifications:
        pending_verifications[user.id] = set()
    pending_verifications[user.id].add(chat.id)

    # 3. Get Bot info for Group Deep Link
    bot_info = await bot.get_me()
    bot_username = bot_info.username

    # Keyboard for Private Message (contains WebApp button)
    pm_keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="点击完成人机验证", web_app=WebAppInfo(url=config.WEBAPP_URL))]
    ])

    # Keyboard for Group Message (contains t.me link button, as web_app buttons are forbidden in groups by Telegram)
    group_keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="点击前往私聊完成验证", url=f"https://t.me/{bot_username}?start=verify")]
    ])

    # 4. Try sending private message to user, fallback to group message with group_keyboard
    welcome_text_pm = (
        f"欢迎 **{user.first_name}** 加入 **{chat.title}**！\n"
        f"请点击下方按钮完成验证以解除发言限制："
    )
    welcome_text_group = (
        f"欢迎 [{user.first_name}](tg://user?id={user.id})！\n"
        f"加入群组需要先进行人机验证。请点击下方按钮前往私聊完成验证以解除发言限制："
    )

    # Delete previous verification message in this group to keep group clean
    await delete_previous_group_message(chat.id)

    try:
        await bot.send_message(chat_id=user.id, text=welcome_text_pm, reply_markup=pm_keyboard, parse_mode="Markdown")
        # Send friendly notice in group pointing to PM
        sent_msg = await bot.send_message(
            chat_id=chat.id,
            text=f"欢迎 [{user.first_name}](tg://user?id={user.id})！请检查私聊完成验证解封发言。",
            parse_mode="Markdown"
        )
        last_group_verification_msg[chat.id] = sent_msg.message_id
    except Exception as pm_err:
        logger.warning(f"Could not send PM to user {user.id}, posting in group with deep link button: {pm_err}")
        sent_msg = await bot.send_message(chat_id=chat.id, text=welcome_text_group, reply_markup=group_keyboard, parse_mode="Markdown")
        last_group_verification_msg[chat.id] = sent_msg.message_id

if dp:
    @dp.chat_member(ChatMemberUpdatedFilter(member_status_changed=JOIN_TRANSITION))
    async def on_user_join_member_update(event: ChatMemberUpdated):
        """Handler for ChatMemberUpdated join events."""
        await process_new_user(event.new_chat_member.user, event.chat)

    @dp.message(F.new_chat_members)
    async def on_user_join_message(message: types.Message):
        """Handler for new_chat_members service messages."""
        for new_user in message.new_chat_members:
            await process_new_user(new_user, message.chat)

    @dp.message(F.text == "/start")
    async def cmd_start(message: types.Message):
        """Standard /start command handler."""
        verify_url = config.WEBAPP_URL
        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="打开人机安全验证页", web_app=WebAppInfo(url=verify_url))]
        ])
        await message.answer(
            "您好！ 加入社区需要先进行验证以确保你是人类 \n点击下方按钮可直接进行验证。",
            reply_markup=keyboard
        )

# --- FastAPI Web App Lifecycle ---

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup: Check configuration & launch Telegram Bot polling in background
    missing = config.validate_config()
    if missing:
        logger.warning(f"⚠️  Missing required environment variables: {', '.join(missing)}")
        logger.warning("Please copy .env.example to .env and set your credentials.")
    else:
        logger.info("✅ All environment variables loaded successfully.")

    bot_task = None
    if bot:
        logger.info("Starting Telegram Bot Polling with resolved update types...")
        bot_task = asyncio.create_task(dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types()))

    yield

    # Shutdown
    if bot_task:
        logger.info("Stopping Telegram Bot Polling...")
        bot_task.cancel()
        if bot:
            await bot.session.close()

app = FastAPI(title="Telegram Cloudflare Verification", lifespan=lifespan)

# Mount static files directory
app.mount("/static", StaticFiles(directory="static"), name="static")

@app.get("/", response_class=HTMLResponse)
async def serve_index():
    with open("static/index.html", "r", encoding="utf-8") as f:
        return HTMLResponse(content=f.read())

@app.get("/favicon.ico")
async def serve_favicon():
    return FileResponse("static/favicon.ico")

@app.get("/api/config")
async def get_config():
    return {"site_key": config.CF_SITE_KEY}

class VerifyRequest(BaseModel):
    init_data: str
    turnstile_response: str

@app.post("/api/verify")
async def verify_endpoint(req: VerifyRequest):
    # 1. Validate Cloudflare Turnstile token
    cf_valid = await verify_cloud_flare_turnstile(req.turnstile_response)
    if not cf_valid:
        return JSONResponse({"success": False, "message": "Cloudflare 旋转门验证未通过或已失效"}, status_code=400)

    # 2. Validate Telegram initData signature
    if not req.init_data:
        return JSONResponse({"success": False, "message": "请在 Telegram 客户端内部打开 Mini App 进行身份验证"}, status_code=400)

    user_info = verify_telegram_init_data(req.init_data, config.BOT_TOKEN)
    if not user_info:
        return JSONResponse({"success": False, "message": "Telegram 身份校验失败 (initData 签名无效)"}, status_code=400)

    user_id = user_info.get("id")
    if not user_id:
        return JSONResponse({"success": False, "message": "无法识别 Telegram 用户 ID"}, status_code=400)

    # 3. Unmute user in Telegram groups
    chats_to_unmute = pending_verifications.get(user_id, set())

    # Full messaging permissions
    unrestricted_permissions = ChatPermissions(
        can_send_messages=True,
        can_send_audios=True,
        can_send_documents=True,
        can_send_photos=True,
        can_send_videos=True,
        can_send_video_notes=True,
        can_send_voice_notes=True,
        can_send_polls=True,
        can_send_other_messages=True,
        can_add_web_page_previews=True
    )

    unmuted_count = 0
    if bot and chats_to_unmute:
        for chat_id in list(chats_to_unmute):
            try:
                await bot.restrict_chat_member(
                    chat_id=chat_id,
                    user_id=user_id,
                    permissions=unrestricted_permissions
                )
                logger.info(f"Unmuted user {user_id} in group {chat_id}")
                unmuted_count += 1
                # Clean up verification notice in group once user verifies successfully
                await delete_previous_group_message(chat_id)
            except Exception as e:
                logger.error(f"Failed to unmute user {user_id} in group {chat_id}: {e}")
        
        # Clear pending verification record
        pending_verifications.pop(user_id, None)

    return {
        "success": True,
        "message": "验证成功！发言限制已成功解除。",
        "user_id": user_id,
        "unmuted_chats": unmuted_count
    }

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host=config.HOST, port=config.PORT, reload=True)
