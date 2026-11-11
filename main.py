import asyncio
import hmac
import hashlib
import html
import json
import logging
import os
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Dict, Optional, Set
from urllib.parse import parse_qsl

import httpx
from fastapi import FastAPI
from fastapi.responses import HTMLResponse, JSONResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from aiogram import Bot, Dispatcher, F, types
from aiogram.enums import ChatMemberStatus, ParseMode
from aiogram.filters import CommandStart
from aiogram.filters.chat_member_updated import ChatMemberUpdatedFilter, JOIN_TRANSITION, LEAVE_TRANSITION
from aiogram.types import ChatPermissions, InlineKeyboardButton, InlineKeyboardMarkup, WebAppInfo, ChatMemberUpdated

import config

# Configure Logging
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

STATIC_DIR = config.BASE_DIR / "static"

# All permission fields known to the installed aiogram version. Telegram treats any omitted
# ChatPermissions field as False, so "lift restrictions" must explicitly pass True for every field.
PERMISSION_FIELDS = list(ChatPermissions.model_fields)
MUTED_PERMISSIONS = ChatPermissions(**{f: False for f in PERMISSION_FIELDS})
UNRESTRICTED_PERMISSIONS = ChatPermissions(**{f: True for f in PERMISSION_FIELDS})


class PendingStore:
    """Pending verifications persisted to a JSON file.

    Layout: {user_id: {chat_id: {"restore": {perm: bool} | None, "until": int, "notice": int | None}}}
    "restore" holds a restriction an admin had already placed on the user before they (re)joined,
    so that passing verification does not wipe out an admin mute.
    """

    def __init__(self, path: Path):
        self.path = path
        self.lock = asyncio.Lock()
        self.data: Dict[str, Dict[str, dict]] = self._load()

    def _load(self) -> Dict[str, Dict[str, dict]]:
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                data = json.load(f)
            return data if isinstance(data, dict) else {}
        except FileNotFoundError:
            return {}
        except Exception as e:
            logger.error(f"Failed to load pending store {self.path}: {e}")
            return {}

    def _save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self.data, f, ensure_ascii=False)
        os.replace(tmp, self.path)

    async def has(self, user_id: int, chat_id: int) -> bool:
        async with self.lock:
            return str(chat_id) in self.data.get(str(user_id), {})

    async def add(self, user_id: int, chat_id: int, restore: Optional[dict], until: int):
        """Record a pending verification. An existing entry (and its restore info) is kept."""
        async with self.lock:
            chats = self.data.setdefault(str(user_id), {})
            if str(chat_id) not in chats:
                chats[str(chat_id)] = {"restore": restore, "until": until, "notice": None}
                self._save()

    async def swap_notice(self, user_id: int, chat_id: int, message_id: Optional[int]) -> Optional[int]:
        """Store a new group notice message id for this user, returning the previous one."""
        async with self.lock:
            entry = self.data.get(str(user_id), {}).get(str(chat_id))
            if entry is None:
                return None
            old = entry.get("notice")
            entry["notice"] = message_id
            self._save()
            return old

    async def replace_chat_notice(self, user_id: int, chat_id: int, message_id: int) -> list:
        """Make message_id the only tracked notice in this chat (keeps the group clean).

        Returns every previously tracked notice in the chat (any user's) so the caller can delete them.
        """
        async with self.lock:
            old = []
            for chats in self.data.values():
                entry = chats.get(str(chat_id))
                if entry and entry.get("notice"):
                    old.append(entry["notice"])
                    entry["notice"] = None
            entry = self.data.get(str(user_id), {}).get(str(chat_id))
            if entry is not None:
                entry["notice"] = message_id
            self._save()
            return old

    async def chats(self, user_id: int) -> Dict[int, dict]:
        async with self.lock:
            return {int(c): dict(e) for c, e in self.data.get(str(user_id), {}).items()}

    async def remove(self, user_id: int, chat_id: int):
        async with self.lock:
            chats = self.data.get(str(user_id))
            if chats and chats.pop(str(chat_id), None) is not None:
                if not chats:
                    self.data.pop(str(user_id), None)
                self._save()


store = PendingStore(config.DATA_FILE)

# Telegram Bot & Dispatcher setup
bot = Bot(token=config.BOT_TOKEN) if config.BOT_TOKEN else None
dp = Dispatcher()

_bot_username: Optional[str] = None


async def get_bot_username() -> str:
    global _bot_username
    if _bot_username is None:
        _bot_username = (await bot.get_me()).username
    return _bot_username


def verify_telegram_init_data(init_data: str, bot_token: str, max_age: int = 0) -> dict | None:
    """Validate Telegram WebApp initData using HMAC-SHA256 signature and auth_date freshness."""
    if not init_data or not bot_token:
        return None
    try:
        parsed_data = dict(parse_qsl(init_data, keep_blank_values=True, strict_parsing=True))
        received_hash = parsed_data.pop("hash", None)
        if not received_hash:
            return None

        data_check_string = "\n".join(f"{k}={v}" for k, v in sorted(parsed_data.items()))
        secret_key = hmac.new(b"WebAppData", bot_token.encode("utf-8"), hashlib.sha256).digest()
        calculated_hash = hmac.new(secret_key, data_check_string.encode("utf-8"), hashlib.sha256).hexdigest()

        if not hmac.compare_digest(calculated_hash, received_hash):
            return None

        if max_age > 0:
            auth_date = int(parsed_data.get("auth_date", "0"))
            if auth_date <= 0 or time.time() - auth_date > max_age:
                logger.info("Rejected expired initData")
                return None

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
    if not token:
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
            return result.get("success") is True
    except Exception as e:
        logger.error(f"Cloudflare verification network error: {e}")
        return False


# Short-lived dedup: both the chat_member update and the new_chat_members service message fire on join
recent_joins: Set[str] = set()


async def delete_message_quietly(chat_id: int, message_id: Optional[int]):
    if not message_id or not bot:
        return
    try:
        await bot.delete_message(chat_id=chat_id, message_id=message_id)
        logger.info(f"Deleted verification notice {message_id} in chat {chat_id}")
    except Exception as e:
        logger.warning(f"Could not delete message {message_id} in chat {chat_id}: {e}")


def _timestamp(value) -> int:
    if not value:
        return 0
    if isinstance(value, (int, float)):
        return int(value)
    return int(value.timestamp())


async def process_new_user(user: types.User, chat: types.Chat):
    """Core logic to restrict and notify a new user upon joining a group."""
    if user.is_bot or not bot:
        return

    join_key = f"{chat.id}_{user.id}"
    if join_key in recent_joins:
        logger.debug(f"Skipping duplicate join event for {join_key}")
        return
    recent_joins.add(join_key)
    asyncio.get_running_loop().call_later(10, recent_joins.discard, join_key)

    logger.info(f"Processing new user join: {user.full_name} ({user.id}) in chat: {chat.title} ({chat.id})")

    # 1. Look at the member's current state: skip admins, and remember an existing admin restriction
    #    (Telegram keeps restrictions across leave/rejoin) so verification cannot be used to bypass it.
    restore = None
    until = 0
    if not await store.has(user.id, chat.id):
        try:
            member = await bot.get_chat_member(chat_id=chat.id, user_id=user.id)
            if member.status in (ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.CREATOR):
                logger.info(f"User {user.id} is an admin of chat {chat.id}, skipping verification")
                return
            if member.status == ChatMemberStatus.RESTRICTED:
                restore = {f: bool(getattr(member, f)) for f in PERMISSION_FIELDS if getattr(member, f, None) is not None}
                until = _timestamp(getattr(member, "until_date", None))
                logger.info(f"User {user.id} already restricted in chat {chat.id}; will restore that restriction after verification")
        except Exception as e:
            logger.warning(f"Could not fetch member state for {user.id} in chat {chat.id}: {e}")

    # 2. Restrict user in group immediately
    try:
        await bot.restrict_chat_member(chat_id=chat.id, user_id=user.id, permissions=MUTED_PERMISSIONS)
        logger.info(f"Restricted user {user.id} in chat {chat.id}")
    except Exception as e:
        if "supergroup" in str(e).lower():
            logger.warning(f"⚠️ 无法在群组 {chat.title} ({chat.id}) 中禁言用户：Telegram 规定禁言功能仅支持【超级群组 (Supergroup)】。请在 Telegram 中将群组转为超级群组（如：设置历史消息对新成员可见/设置公开群后再转私密）。")
        else:
            logger.error(f"Failed to restrict user {user.id} in chat {chat.id}: {e}")

    # 3. Record pending verification (persisted)
    await store.add(user.id, chat.id, restore, until)

    # 4. Notify the user. Names/titles are user-controlled, so they are HTML-escaped.
    name = html.escape(user.first_name or user.full_name or str(user.id))
    title = html.escape(chat.title or "")
    mention = f'<a href="tg://user?id={user.id}">{name}</a>'

    pm_keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="点击完成人机验证", web_app=WebAppInfo(url=config.WEBAPP_URL))]
    ])
    welcome_text_pm = (
        f"欢迎 <b>{name}</b> 加入 <b>{title}</b>！\n"
        f"请点击下方按钮完成验证以解除发言限制："
    )

    group_text = f"欢迎 {mention}！请检查私聊完成验证解封发言。"
    group_keyboard = None
    try:
        await bot.send_message(chat_id=user.id, text=welcome_text_pm, reply_markup=pm_keyboard, parse_mode=ParseMode.HTML)
    except Exception as pm_err:
        # Usually the user has never started the bot. web_app buttons are not allowed in groups,
        # so point them to a private chat deep link instead.
        logger.info(f"Could not send PM to user {user.id}, posting deep link in group: {pm_err}")
        try:
            bot_username = await get_bot_username()
            group_keyboard = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="点击前往私聊完成验证", url=f"https://t.me/{bot_username}?start=verify")]
            ])
            group_text = (
                f"欢迎 {mention}！\n"
                f"加入群组需要先进行人机验证。请点击下方按钮前往私聊完成验证以解除发言限制："
            )
        except Exception as e:
            logger.error(f"Could not resolve bot username: {e}")

    try:
        sent_msg = await bot.send_message(chat_id=chat.id, text=group_text, reply_markup=group_keyboard, parse_mode=ParseMode.HTML)
        # Keep the group clean: only the latest verification notice stays visible
        for old_notice in await store.replace_chat_notice(user.id, chat.id, sent_msg.message_id):
            await delete_message_quietly(chat.id, old_notice)
    except Exception as e:
        logger.error(f"Failed to send verification notice in chat {chat.id}: {e}")


@dp.chat_member(ChatMemberUpdatedFilter(member_status_changed=JOIN_TRANSITION))
async def on_user_join_member_update(event: ChatMemberUpdated):
    """Handler for ChatMemberUpdated join events."""
    await process_new_user(event.new_chat_member.user, event.chat)


@dp.chat_member(ChatMemberUpdatedFilter(member_status_changed=LEAVE_TRANSITION))
async def on_user_leave(event: ChatMemberUpdated):
    """Clean up the group notice of a user who left before verifying.

    The pending entry is kept on purpose: our mute persists across leave/rejoin, and the entry is what
    tells a rejoin apart from an admin-imposed restriction.
    """
    user_id = event.old_chat_member.user.id
    old_notice = await store.swap_notice(user_id, event.chat.id, None)
    await delete_message_quietly(event.chat.id, old_notice)


@dp.message(F.new_chat_members)
async def on_user_join_message(message: types.Message):
    """Handler for new_chat_members service messages."""
    for new_user in message.new_chat_members:
        await process_new_user(new_user, message.chat)


@dp.message(CommandStart(), F.chat.type == "private")
async def cmd_start(message: types.Message):
    """/start and deep link (/start verify) handler, private chats only (web_app buttons are PM-only)."""
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="打开人机安全验证页", web_app=WebAppInfo(url=config.WEBAPP_URL))]
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
        # handle_signals=False: let uvicorn own SIGINT/SIGTERM and shut us down through lifespan
        bot_task = asyncio.create_task(dp.start_polling(
            bot,
            allowed_updates=dp.resolve_used_update_types(),
            handle_signals=False,
        ))

    yield

    # Shutdown
    if bot_task:
        logger.info("Stopping Telegram Bot Polling...")
        try:
            await dp.stop_polling()
        except RuntimeError:
            pass
        try:
            await asyncio.wait_for(bot_task, timeout=15)
        except (asyncio.CancelledError, asyncio.TimeoutError, Exception):
            bot_task.cancel()
        await bot.session.close()

app = FastAPI(title="Telegram Cloudflare Verification", lifespan=lifespan)

# Mount static files directory (absolute path, so the app works regardless of the working directory)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

@app.get("/", response_class=HTMLResponse)
async def serve_index():
    return FileResponse(STATIC_DIR / "index.html", media_type="text/html")

@app.get("/favicon.ico")
async def serve_favicon():
    return FileResponse(STATIC_DIR / "favicon.ico")

@app.get("/api/config")
async def get_config():
    return {"site_key": config.CF_SITE_KEY}

class VerifyRequest(BaseModel):
    init_data: str
    turnstile_response: str


async def lift_restriction(user_id: int, chat_id: int, entry: dict):
    """Unmute a verified user, or put back the restriction an admin had set before they joined."""
    restore = entry.get("restore")
    until = int(entry.get("until") or 0)
    now = time.time()
    if restore and (until == 0 or until > now + 30):
        await bot.restrict_chat_member(
            chat_id=chat_id,
            user_id=user_id,
            permissions=ChatPermissions(**restore),
            until_date=until or None,
        )
        logger.info(f"Restored previous admin restriction for user {user_id} in group {chat_id}")
    else:
        await bot.restrict_chat_member(chat_id=chat_id, user_id=user_id, permissions=UNRESTRICTED_PERMISSIONS)
        logger.info(f"Unmuted user {user_id} in group {chat_id}")


@app.post("/api/verify")
async def verify_endpoint(req: VerifyRequest):
    # 1. Validate Telegram initData signature first (cheap, and does not burn the Turnstile token)
    if not req.init_data:
        return JSONResponse({"success": False, "message": "请在 Telegram 客户端内部打开 Mini App 进行身份验证"}, status_code=400)

    user_info = verify_telegram_init_data(req.init_data, config.BOT_TOKEN, config.INIT_DATA_MAX_AGE)
    if not user_info:
        return JSONResponse({"success": False, "message": "Telegram 身份校验失败或已过期，请关闭页面后重新打开"}, status_code=400)

    user_id = user_info.get("id")
    if not isinstance(user_id, int):
        return JSONResponse({"success": False, "message": "无法识别 Telegram 用户 ID"}, status_code=400)

    # 2. Validate Cloudflare Turnstile token
    cf_valid = await verify_cloud_flare_turnstile(req.turnstile_response)
    if not cf_valid:
        return JSONResponse({"success": False, "message": "Cloudflare 旋转门验证未通过或已失效"}, status_code=400)

    # 3. Lift restrictions in the groups this user is pending in
    pending = await store.chats(user_id)
    if not pending:
        return {
            "success": True,
            "message": "人机验证已通过，但当前没有需要解除限制的群组。",
            "user_id": user_id,
            "unmuted_chats": 0
        }

    if not bot:
        return JSONResponse({"success": False, "message": "服务端未配置 Bot，暂时无法解除限制"}, status_code=500)

    unmuted_count = 0
    failed = 0
    for chat_id, entry in pending.items():
        try:
            await lift_restriction(user_id, chat_id, entry)
        except Exception as e:
            failed += 1
            logger.error(f"Failed to unmute user {user_id} in group {chat_id}: {e}")
            continue  # keep the pending entry so the user can retry
        unmuted_count += 1
        await store.remove(user_id, chat_id)
        await delete_message_quietly(chat_id, entry.get("notice"))

    if unmuted_count == 0:
        return JSONResponse({
            "success": False,
            "message": "验证通过，但解除限制失败（机器人可能缺少管理员权限），请联系群管理员或稍后重试。",
        }, status_code=502)

    message = "验证成功！发言限制已成功解除。"
    if failed:
        message = f"验证成功！已在 {unmuted_count} 个群组解除限制，另有 {failed} 个群组解除失败，可稍后重试。"
    return {
        "success": True,
        "message": message,
        "user_id": user_id,
        "unmuted_chats": unmuted_count
    }

if __name__ == "__main__":
    import uvicorn
    # Pass the app object (not "main:app") so the module is not imported twice; no auto-reload in production
    uvicorn.run(app, host=config.HOST, port=config.PORT)
