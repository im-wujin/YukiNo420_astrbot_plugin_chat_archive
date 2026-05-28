from __future__ import annotations

import asyncio
import hashlib
import inspect
import time
from contextlib import suppress
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from astrbot.api import logger

try:
    from .event_extractor import ArchiveEventExtractor
    from .serializer import escape_cq_param
except ImportError:
    from event_extractor import ArchiveEventExtractor
    from serializer import escape_cq_param


class TelegramChannelCapture:
    """Capture Telegram channel_post updates from the running PTB application."""

    _HANDLER_GROUP = 90
    _POLL_INTERVAL = 30

    def __init__(self, plugin):
        self.plugin = plugin
        self._registrations: dict[int, tuple[Any, Any, int]] = {}
        self._register_lock = asyncio.Lock()
        self._avatar_cache: dict[str, str] = {}

    async def run(self) -> None:
        while not getattr(self.plugin, "_shutting_down", False):
            await self.ensure_registered()
            await asyncio.sleep(self._POLL_INTERVAL)

    async def ensure_registered(self) -> int:
        try:
            from telegram import Update
            from telegram.ext import TypeHandler
        except Exception as e:
            logger.debug(f"Chat Archive: Telegram channel capture unavailable: {e}")
            return 0

        async with self._register_lock:
            count = 0
            for adapter in self._iter_telegram_adapters():
                application = getattr(adapter, "application", None)
                if application is None:
                    continue

                key = id(adapter)
                registered = self._registrations.get(key)
                if registered and registered[0] is application:
                    count += 1
                    continue
                if registered:
                    self._remove_handler(*registered)

                async def channel_post_handler(update, context, adapter=adapter):
                    await self.handle_update(update, context, adapter)

                handler = TypeHandler(Update, channel_post_handler)
                application.add_handler(handler, group=self._HANDLER_GROUP)
                self._registrations[key] = (
                    application,
                    handler,
                    self._HANDLER_GROUP,
                )
                count += 1

                platform_id, _ = self._adapter_platform(adapter)
                logger.info(
                    f"Chat Archive: 已为 Telegram 平台 {platform_id} 注册频道消息捕获器。"
                )

            return count

    async def stop(self) -> None:
        for registration in list(self._registrations.values()):
            self._remove_handler(*registration)
        self._registrations.clear()

    async def handle_update(self, update, context, adapter) -> None:
        if getattr(self.plugin, "_shutting_down", False):
            return

        message = getattr(update, "channel_post", None)
        if not message:
            return

        basic_conf = self.plugin.conf.get("basic", {}) if self.plugin.conf else {}
        if not basic_conf.get("enable_archive", True):
            return

        try:
            raw_message = await self._message_to_archive_text(message)
            platform_id, platform_name = self._adapter_platform(adapter)
            record = self._record_from_channel_message(
                message,
                platform_id=platform_id,
                platform_name=platform_name,
                raw_message=raw_message,
            )
            avatar_url = await self.resolve_message_avatar(
                message,
                bot=getattr(context, "bot", None) or getattr(adapter, "client", None),
                platform_id=platform_id,
            )
            if avatar_url:
                record["avatar_url"] = avatar_url
                record["guild_avatar_url"] = avatar_url

            if record["user_id"] in getattr(self.plugin, "_ignored_users", set()):
                return

            if basic_conf.get("cache_media", False) and record["message"]:
                record["message"] = await self.plugin._process_and_cache_media_in_string(
                    record["message"]
                )

            await self.plugin._ensure_writer_started()
            await self.plugin._enqueue_record(self.plugin._archive_record_tuple(record))
        except Exception as e:
            logger.error(f"Chat Archive: 记录 Telegram 频道消息异常: {e}", exc_info=True)

    def _iter_telegram_adapters(self):
        context = getattr(self.plugin, "context", None)
        platform_manager = getattr(context, "platform_manager", None)
        if platform_manager is None:
            return []

        get_insts = getattr(platform_manager, "get_insts", None)
        if callable(get_insts):
            with suppress(Exception):
                adapters = list(get_insts())
                return [
                    adapter
                    for adapter in adapters
                    if self._adapter_platform(adapter)[1].lower() == "telegram"
                ]

        adapters = getattr(platform_manager, "platform_insts", []) or []
        return [
            adapter
            for adapter in adapters
            if self._adapter_platform(adapter)[1].lower() == "telegram"
        ]

    @staticmethod
    def _adapter_platform(adapter) -> tuple[str, str]:
        platform_id = ""
        platform_name = ""
        try:
            meta = adapter.meta()
            platform_id = str(getattr(meta, "id", "") or "")
            platform_name = str(getattr(meta, "name", "") or "")
        except Exception:
            pass

        config = getattr(adapter, "config", {}) or {}
        if not platform_id and hasattr(config, "get"):
            platform_id = str(config.get("id") or "")
        if not platform_name and hasattr(config, "get"):
            platform_name = str(config.get("type") or "")
        return platform_id or "telegram", platform_name or "telegram"

    @classmethod
    def _record_from_channel_message(
        cls,
        message,
        *,
        platform_id: str,
        platform_name: str,
        raw_message: str,
    ) -> dict[str, Any]:
        chat = getattr(message, "chat", None)
        chat_id = str(getattr(chat, "id", "") or "")
        thread_id = str(getattr(message, "message_thread_id", "") or "")
        session_key = chat_id
        if getattr(message, "is_topic_message", False) and thread_id:
            session_key = f"{chat_id}#{thread_id}"

        sender_chat = getattr(message, "sender_chat", None) or chat
        user_id = str(getattr(sender_chat, "id", "") or chat_id)
        sender_name = (
            getattr(sender_chat, "title", "")
            or getattr(sender_chat, "username", "")
            or getattr(chat, "title", "")
            or "Telegram Channel"
        )
        session_name = (
            getattr(chat, "title", "")
            or getattr(chat, "full_name", "")
            or getattr(chat, "username", "")
            or chat_id
        )
        timestamp = ArchiveEventExtractor.coerce_timestamp(
            getattr(message, "date", None),
            int(time.time()),
        )
        msg_id = str(getattr(message, "message_id", "") or "")

        return {
            "user_id": user_id,
            "sender_name": str(sender_name or ""),
            "message": str(raw_message or "[Telegram 频道消息]"),
            "timestamp": int(timestamp),
            "session_id": (
                f"{platform_id}:ChannelMessage:{session_key}"
                if platform_id and session_key
                else session_key
            ),
            "message_type": "ChannelMessage",
            "session_name": str(session_name or ""),
            "msg_id": msg_id or f"{platform_id}:{user_id}:{timestamp}",
            "platform_id": str(platform_id or ""),
            "platform_name": str(platform_name or "telegram"),
            "avatar_url": "",
        }

    async def resolve_event_avatar(self, event) -> str:
        platform_id = ""
        try:
            platform_id = str(event.get_platform_id() or "")
        except Exception:
            platform_id = ""

        platform_raw = getattr(getattr(event, "message_obj", None), "raw_message", None)
        message = (
            getattr(platform_raw, "effective_message", None)
            or getattr(platform_raw, "message", None)
            or getattr(platform_raw, "channel_post", None)
            or getattr(platform_raw, "edited_channel_post", None)
            or platform_raw
        )
        if message is None:
            return ""

        user = getattr(platform_raw, "effective_user", None)
        chat = getattr(platform_raw, "effective_chat", None)
        bot = self._bot_for_platform(platform_id)
        return await self.resolve_message_avatar(
            message,
            bot=bot,
            platform_id=platform_id or "telegram",
            user=user,
            chat=chat,
        )

    async def resolve_message_avatar(
        self,
        message,
        *,
        bot=None,
        platform_id: str = "telegram",
        user=None,
        chat=None,
    ) -> str:
        from_user = user or getattr(message, "from_user", None)
        user_id = getattr(from_user, "id", None)
        if user_id:
            cache_key = f"{platform_id}:user:{user_id}"
            if cache_key not in self._avatar_cache:
                avatar_url = await self._fetch_user_avatar(
                    bot,
                    str(user_id),
                    cache_key,
                )
                if not avatar_url:
                    avatar_url = await self._fetch_chat_avatar(
                        bot,
                        str(user_id),
                        chat or getattr(message, "chat", None),
                        cache_key,
                    )
                self._avatar_cache[cache_key] = avatar_url
            return self._avatar_cache.get(cache_key, "")

        chat = chat or getattr(message, "sender_chat", None) or getattr(message, "chat", None)
        chat_id = getattr(chat, "id", None)
        if chat_id:
            cache_key = f"{platform_id}:chat:{chat_id}"
            if cache_key not in self._avatar_cache:
                self._avatar_cache[cache_key] = await self._fetch_chat_avatar(
                    bot,
                    str(chat_id),
                    chat,
                    cache_key,
                )
            return self._avatar_cache.get(cache_key, "")

        return ""

    async def resolve_chat_avatar(self, event) -> str:
        platform_id = ""
        try:
            platform_id = str(event.get_platform_id() or "")
        except Exception:
            platform_id = ""

        platform_raw = getattr(getattr(event, "message_obj", None), "raw_message", None)
        message = (
            getattr(platform_raw, "effective_message", None)
            or getattr(platform_raw, "message", None)
            or getattr(platform_raw, "channel_post", None)
            or getattr(platform_raw, "edited_channel_post", None)
            or platform_raw
        )
        chat = getattr(platform_raw, "effective_chat", None) or (getattr(message, "chat", None) if message else None)
        if chat is None:
            return ""

        chat_id = getattr(chat, "id", None)
        if not chat_id:
            return ""

        bot = self._bot_for_platform(platform_id)
        cache_key = f"{platform_id}:chat:{chat_id}"
        if cache_key not in self._avatar_cache:
            self._avatar_cache[cache_key] = await self._fetch_chat_avatar(
                bot,
                str(chat_id),
                chat,
                cache_key,
            )
        return self._avatar_cache.get(cache_key, "")

    async def resolve_bot_avatar(self, event) -> str:
        platform_id = ""
        try:
            platform_id = str(event.get_platform_id() or "")
        except Exception:
            platform_id = ""
        platform_id = platform_id or "telegram"

        bot = self._bot_for_platform(platform_id)
        if bot is None:
            return ""

        bot_id = None
        try:
            bot_id = getattr(bot, "id", None)
        except Exception:
            bot_id = None
        if not bot_id:
            token = str(getattr(bot, "token", "") or "")
            token_id = token.split(":", 1)[0]
            if token_id.isdigit():
                bot_id = token_id
        if not bot_id:
            get_me = getattr(bot, "get_me", None)
            if callable(get_me):
                try:
                    me = get_me()
                    me = await me if inspect.isawaitable(me) else me
                    bot_id = getattr(me, "id", None)
                except Exception as e:
                    logger.debug(f"Chat Archive: 获取 Telegram bot 信息失败: {e}")
        if not bot_id:
            return ""

        cache_key = f"{platform_id}:bot:{bot_id}"
        if cache_key not in self._avatar_cache:
            avatar_url = await self._fetch_user_avatar(bot, str(bot_id), cache_key)
            if not avatar_url:
                avatar_url = await self._fetch_chat_avatar(bot, str(bot_id), None, cache_key)
            self._avatar_cache[cache_key] = avatar_url
        return self._avatar_cache.get(cache_key, "")

    def _bot_for_platform(self, platform_id: str):
        fallback_bot = None
        for adapter in self._iter_telegram_adapters():
            adapter_platform_id, _ = self._adapter_platform(adapter)
            bot = getattr(adapter, "client", None)
            if fallback_bot is None and bot is not None:
                fallback_bot = bot
            if platform_id and adapter_platform_id != platform_id:
                continue
            if bot is not None:
                return bot
        return fallback_bot

    async def _fetch_user_avatar(self, bot, user_id: str, cache_key: str) -> str:
        if bot is None:
            return ""
        try:
            photos = await bot.get_user_profile_photos(user_id=int(user_id), limit=1)
            photo_rows = getattr(photos, "photos", None) or []
            if not photo_rows or not photo_rows[0]:
                return ""
            photo = photo_rows[0][-1]
            file_obj = await self._photo_to_file(photo, bot)
            return await self._cache_telegram_file(file_obj, cache_key)
        except Exception as e:
            logger.debug(f"Chat Archive: 获取 Telegram 用户头像失败 {user_id}: {e}")
            return ""

    async def _fetch_chat_avatar(self, bot, chat_id: str, chat, cache_key: str) -> str:
        if bot is None:
            return ""
        try:
            chat_obj = chat
            get_chat = getattr(bot, "get_chat", None)
            if callable(get_chat):
                maybe_chat = get_chat(chat_id=int(chat_id))
                chat_obj = await maybe_chat if inspect.isawaitable(maybe_chat) else maybe_chat
            photo = getattr(chat_obj, "photo", None) or getattr(chat, "photo", None)
            file_id = (
                getattr(photo, "big_file_id", "")
                or getattr(photo, "small_file_id", "")
                or getattr(photo, "file_id", "")
            )
            if not file_id:
                return ""
            get_file = getattr(bot, "get_file", None)
            if not callable(get_file):
                return ""
            file_obj = get_file(file_id)
            if inspect.isawaitable(file_obj):
                file_obj = await file_obj
            return await self._cache_telegram_file(file_obj, cache_key)
        except Exception as e:
            logger.debug(f"Chat Archive: 获取 Telegram 聊天头像失败 {chat_id}: {e}")
            return ""

    @staticmethod
    async def _photo_to_file(photo, bot):
        get_file = getattr(photo, "get_file", None)
        if callable(get_file):
            file_obj = get_file()
            return await file_obj if inspect.isawaitable(file_obj) else file_obj

        file_id = getattr(photo, "file_id", "")
        get_file = getattr(bot, "get_file", None)
        if file_id and callable(get_file):
            file_obj = get_file(file_id)
            return await file_obj if inspect.isawaitable(file_obj) else file_obj
        return None

    async def _cache_telegram_file(self, file_obj, cache_key: str) -> str:
        if file_obj is None:
            return ""
        cache_dir = getattr(getattr(self.plugin, "_media_cache", None), "cache_dir", None)
        if cache_dir is None:
            return ""

        file_path = str(getattr(file_obj, "file_path", "") or "")
        suffix = Path(urlparse(file_path).path).suffix.lower()
        if suffix not in {".jpg", ".jpeg", ".png", ".webp", ".gif"}:
            suffix = ".jpg"
        if suffix == ".jpeg":
            suffix = ".jpg"

        digest = hashlib.md5(cache_key.encode("utf-8")).hexdigest()
        cache_dir.mkdir(parents=True, exist_ok=True)
        dest_path = cache_dir / f"telegram_avatar_{digest}{suffix}"
        relative_url = f"/static/cache/{dest_path.name}"
        if dest_path.exists():
            return relative_url

        tmp_path = dest_path.with_suffix(dest_path.suffix + ".tmp")
        try:
            download = getattr(file_obj, "download_to_drive", None)
            if callable(download):
                result = download(custom_path=str(tmp_path))
                if inspect.isawaitable(result):
                    await result
            else:
                download_bytes = getattr(file_obj, "download_as_bytearray", None)
                if not callable(download_bytes):
                    return ""
                data = download_bytes()
                if inspect.isawaitable(data):
                    data = await data
                with open(tmp_path, "wb") as f:
                    f.write(bytes(data))
            tmp_path.replace(dest_path)
            return relative_url
        except Exception as e:
            with suppress(Exception):
                tmp_path.unlink(missing_ok=True)
            logger.debug(f"Chat Archive: 缓存 Telegram 头像失败: {e}")
            return ""

    async def _message_to_archive_text(self, message) -> str:
        parts = []
        text = getattr(message, "text", None) or getattr(message, "caption", None)
        if text:
            parts.append(str(text))

        await self._append_media_parts(message, parts)
        return "".join(parts) or "[Telegram 频道消息]"

    async def _append_media_parts(self, message, parts: list[str]) -> None:
        photos = getattr(message, "photo", None)
        if photos:
            photo = photos[-1]
            await self._append_media_cq(parts, "image", photo, "[CQ:image]")

        video = getattr(message, "video", None) or getattr(message, "animation", None)
        if video:
            await self._append_media_cq(parts, "video", video, "[CQ:video]")

        voice = getattr(message, "voice", None) or getattr(message, "audio", None)
        if voice:
            await self._append_media_cq(parts, "record", voice, "[语音]")

        document = getattr(message, "document", None)
        if document:
            name = getattr(document, "file_name", "") or "文件"
            await self._append_file_cq(parts, document, name)

        sticker = getattr(message, "sticker", None)
        if sticker:
            await self._append_media_cq(parts, "image", sticker, "[CQ:image]")
            emoji = getattr(sticker, "emoji", "")
            if emoji:
                parts.append(f"Sticker: {emoji}")

    async def _append_media_cq(
        self,
        parts: list[str],
        cq_type: str,
        media,
        fallback: str,
    ) -> None:
        url = await self._media_file_url(media)
        if url:
            parts.append(f"[CQ:{cq_type},url={escape_cq_param(url)}]")
        else:
            parts.append(fallback)

    async def _append_file_cq(self, parts: list[str], document, name: str) -> None:
        url = await self._media_file_url(document)
        if url:
            parts.append(
                f"[CQ:file,name={escape_cq_param(name)},url={escape_cq_param(url)}]"
            )
        else:
            parts.append(f"[文件: {name}]")

    @staticmethod
    async def _media_file_url(media) -> str:
        get_file = getattr(media, "get_file", None)
        if callable(get_file):
            try:
                file_obj = get_file()
                if inspect.isawaitable(file_obj):
                    file_obj = await file_obj
                file_path = getattr(file_obj, "file_path", "") or ""
                if file_path:
                    return str(file_path)
            except Exception as e:
                logger.debug(f"Chat Archive: 获取 Telegram 频道媒体文件失败: {e}")

        return str(getattr(media, "file_id", "") or "")

    @staticmethod
    def _remove_handler(application, handler, group: int) -> None:
        remove_handler = getattr(application, "remove_handler", None)
        if callable(remove_handler):
            with suppress(Exception):
                remove_handler(handler, group=group)
