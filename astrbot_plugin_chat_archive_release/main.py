import asyncio
import datetime
import json
import re
import sqlite3
import time
import hashlib
import ipaddress
import socket
import os
from pathlib import Path
from urllib.parse import urlparse
import httpx
from contextlib import contextmanager
from typing import Any

try:
    from .db_config import get_db_connection, init_db, DatabaseManager
except ImportError:
    try:
        from db_config import get_db_connection, init_db, DatabaseManager
    except Exception:
        raise RuntimeError("无法加载 db_config 模块，请确保插件包完整。")

from astrbot.api.event import filter
from astrbot.api.star import Context, Star, register
from astrbot.core import AstrBotConfig
from astrbot.core.agent.run_context import ContextWrapper
from astrbot.core.agent.tool import FunctionTool, ToolExecResult
from astrbot.core.astr_agent_context import AstrAgentContext
from astrbot.core.platform.astr_message_event import AstrMessageEvent
from astrbot.core.star.filter.event_message_type import EventMessageType
from pydantic import Field
from pydantic.dataclasses import dataclass

# 尝试导入 web server
try:
    from .web.server import AdminServer
except ImportError:
    AdminServer = None

from astrbot.api import logger

try:
    from astrbot.api.star import StarTools
    _env_data_dir = os.environ.get("ARCHIVE_DATA_DIR", "").strip()
    if _env_data_dir:
        _data_path = Path(os.path.expandvars(_env_data_dir)).expanduser()
        DATA_DIR = (_data_path if _data_path.is_absolute() else Path(__file__).resolve().parent / _data_path).resolve()
    else:
        DATA_DIR = Path(StarTools.get_data_dir()).expanduser().resolve()
except Exception:
    # Fallback for standalone decoupling execution or tests
    _env_data_dir = os.environ.get("ARCHIVE_DATA_DIR", "").strip()
    if _env_data_dir:
        _data_path = Path(os.path.expandvars(_env_data_dir)).expanduser()
        DATA_DIR = (_data_path if _data_path.is_absolute() else Path(__file__).resolve().parent / _data_path).resolve()
    else:
        DATA_DIR = (Path(__file__).resolve().parent / "data").resolve()

STATIC_CACHE_DIR = DATA_DIR / "web_cache"

DEFAULT_ALLOWED_MEDIA_DOMAINS = {
    "multimedia.nt.qq.com.cn",
    "gchat.qpic.cn",
    "q.qlogo.cn",
    "p.qlogo.cn",
    "q1.qlogo.cn",
    "gxh.vip.qq.com",
}
DEFAULT_MAX_MEDIA_BYTES = 50 * 1024 * 1024  # 50 MiB


def _archive_json_result(data: Any) -> str:
    """Serialize archive query results for LLM tool calls."""
    return json.dumps(data, ensure_ascii=False, default=str)


def _archive_tool_error(message: str) -> str:
    return _archive_json_result({"error": message})


@dataclass
class ArchiveGetHistoryTool(FunctionTool[AstrAgentContext]):
    """LLM tool: query archived chat history."""

    name: str = "archive_get_history"
    description: str = (
        "查询聊天存档历史记录。可按用户、会话、关键词、时间范围分页查询，"
        "返回消息列表，适合查找某段对话、某人发言或包含特定关键词的消息。"
    )
    parameters: dict = Field(
        default_factory=lambda: {
            "type": "object",
            "properties": {
                "user_id": {
                    "type": "string",
                    "description": "可选，用户 ID；传入后仅查询该用户的消息。",
                },
                "session_id": {
                    "type": "string",
                    "description": "可选，会话 ID/群聊 ID；传入后仅查询该会话的消息。",
                },
                "keyword": {
                    "type": "string",
                    "description": "可选，消息关键词；用于模糊搜索消息正文。",
                },
                "since_ts": {
                    "type": "integer",
                    "description": "可选，起始 Unix 时间戳（秒），只返回此时间之后的消息。",
                },
                "until_ts": {
                    "type": "integer",
                    "description": "可选，结束 Unix 时间戳（秒），只返回此时间之前的消息。",
                },
                "limit": {
                    "type": "integer",
                    "description": "可选，最多返回条数，默认 50。",
                    "default": 50,
                },
                "offset": {
                    "type": "integer",
                    "description": "可选，分页偏移量，默认 0。",
                    "default": 0,
                },
                "asc": {
                    "type": "boolean",
                    "description": "可选，是否按时间升序返回；false 表示最新消息优先，默认 true。",
                    "default": True,
                },
                "exclude_recalled": {
                    "type": "boolean",
                    "description": "可选，是否排除已撤回消息，默认 true。",
                    "default": True,
                },
            },
        }
    )
    plugin: Any = None

    async def call(
        self, context: ContextWrapper[AstrAgentContext], **kwargs
    ) -> ToolExecResult:
        allowed, scoped_kwargs = self.plugin._prepare_archive_tool_query(
            context, kwargs, require_session=True
        )
        if not allowed:
            return _archive_tool_error(scoped_kwargs["error"])
        return _archive_json_result(
            await asyncio.to_thread(self.plugin.get_history, **scoped_kwargs)
        )


@dataclass
class ArchiveGetSessionsTool(FunctionTool[AstrAgentContext]):
    """LLM tool: list archived sessions."""

    name: str = "archive_get_sessions"
    description: str = (
        "获取所有存在聊天存档的会话列表。返回每个会话的 session_id、消息类型、"
        "消息数量和最后消息时间，可用于先定位要查询的群聊/私聊会话。"
    )
    parameters: dict = Field(
        default_factory=lambda: {
            "type": "object",
            "properties": {},
        }
    )
    plugin: Any = None

    async def call(
        self, context: ContextWrapper[AstrAgentContext], **kwargs
    ) -> ToolExecResult:
        if not self.plugin._is_admin_tool_context(context):
            return _archive_tool_error("权限不足：只有管理员可以列出所有归档会话。")
        return _archive_json_result(await asyncio.to_thread(self.plugin.get_sessions))


@dataclass
class ArchiveGetMemberRankTool(FunctionTool[AstrAgentContext]):
    """LLM tool: rank active members."""

    name: str = "archive_get_member_rank"
    description: str = (
        "查询指定会话内成员活跃排行，按发言数量从高到低返回。"
        "适合回答谁最活跃、某段时间内群内发言排行等问题。"
    )
    parameters: dict = Field(
        default_factory=lambda: {
            "type": "object",
            "properties": {
                "session_id": {
                    "type": "string",
                    "description": "必填，会话 ID/群聊 ID。",
                },
                "limit": {
                    "type": "integer",
                    "description": "可选，返回排行人数，默认 10。",
                    "default": 10,
                },
                "since_ts": {
                    "type": "integer",
                    "description": "可选，起始 Unix 时间戳（秒）。",
                },
                "until_ts": {
                    "type": "integer",
                    "description": "可选，结束 Unix 时间戳（秒）。",
                },
            },
            "required": ["session_id"],
        }
    )
    plugin: Any = None

    async def call(
        self, context: ContextWrapper[AstrAgentContext], **kwargs
    ) -> ToolExecResult:
        allowed, scoped_kwargs = self.plugin._prepare_archive_tool_query(
            context, kwargs, require_session=True
        )
        if not allowed:
            return _archive_tool_error(scoped_kwargs["error"])
        return _archive_json_result(
            await asyncio.to_thread(self.plugin.get_member_rank, **scoped_kwargs)
        )


@dataclass
class ArchiveGetUserSummaryTool(FunctionTool[AstrAgentContext]):
    """LLM tool: summarize one user."""

    name: str = "archive_get_user_summary"
    description: str = (
        "查询指定用户的存档统计概览，包括总消息数、首次出现时间、最后发言时间、"
        "最近昵称等；可选限定在某个会话内统计。"
    )
    parameters: dict = Field(
        default_factory=lambda: {
            "type": "object",
            "properties": {
                "user_id": {
                    "type": "string",
                    "description": "必填，用户 ID。",
                },
                "session_id": {
                    "type": "string",
                    "description": "可选，会话 ID/群聊 ID；传入后只统计该会话内的数据。",
                },
            },
            "required": ["user_id"],
        }
    )
    plugin: Any = None

    async def call(
        self, context: ContextWrapper[AstrAgentContext], **kwargs
    ) -> ToolExecResult:
        allowed, scoped_kwargs = self.plugin._prepare_archive_tool_query(
            context, kwargs, require_session=True
        )
        if not allowed:
            return _archive_tool_error(scoped_kwargs["error"])
        return _archive_json_result(
            await asyncio.to_thread(self.plugin.get_user_summary, **scoped_kwargs)
        )


@dataclass
class ArchiveGetMessageCountTool(FunctionTool[AstrAgentContext]):
    """LLM tool: count messages."""

    name: str = "archive_get_message_count"
    description: str = (
        "轻量统计聊天存档消息数量。可按用户、会话、时间范围筛选，"
        "适合回答总共有多少条消息、某人/某群某段时间发了多少条等问题。"
    )
    parameters: dict = Field(
        default_factory=lambda: {
            "type": "object",
            "properties": {
                "user_id": {
                    "type": "string",
                    "description": "可选，用户 ID；传入后只统计该用户消息。",
                },
                "session_id": {
                    "type": "string",
                    "description": "可选，会话 ID/群聊 ID；传入后只统计该会话消息。",
                },
                "since_ts": {
                    "type": "integer",
                    "description": "可选，起始 Unix 时间戳（秒）。",
                },
                "until_ts": {
                    "type": "integer",
                    "description": "可选，结束 Unix 时间戳（秒）。",
                },
                "exclude_recalled": {
                    "type": "boolean",
                    "description": "可选，是否排除已撤回消息，默认 true。",
                    "default": True,
                },
            },
        }
    )
    plugin: Any = None

    async def call(
        self, context: ContextWrapper[AstrAgentContext], **kwargs
    ) -> ToolExecResult:
        allowed, scoped_kwargs = self.plugin._prepare_archive_tool_query(
            context, kwargs, require_session=True
        )
        if not allowed:
            return _archive_tool_error(scoped_kwargs["error"])
        return _archive_json_result(
            await asyncio.to_thread(self.plugin.get_message_count, **scoped_kwargs)
        )


@dataclass
class ArchiveGetContextMessagesTool(FunctionTool[AstrAgentContext]):
    """LLM tool: get formatted context messages."""

    name: str = "archive_get_context_messages"
    description: str = (
        "获取适合 LLM 阅读的上下文消息列表。返回格式为 "
        "[时间字符串, 发送者昵称, 消息内容]，适合在需要回顾某个会话近期上下文时调用。"
    )
    parameters: dict = Field(
        default_factory=lambda: {
            "type": "object",
            "properties": {
                "session_id": {
                    "type": "string",
                    "description": "必填，会话 ID/群聊 ID。",
                },
                "user_id": {
                    "type": "string",
                    "description": "可选，用户 ID；传入后只返回该用户的上下文消息。",
                },
                "limit": {
                    "type": "integer",
                    "description": "可选，返回消息条数，默认 50。",
                    "default": 50,
                },
                "exclude_recalled": {
                    "type": "boolean",
                    "description": "可选，是否排除已撤回消息，默认 true。",
                    "default": True,
                },
            },
            "required": ["session_id"],
        }
    )
    plugin: Any = None

    async def call(
        self, context: ContextWrapper[AstrAgentContext], **kwargs
    ) -> ToolExecResult:
        allowed, scoped_kwargs = self.plugin._prepare_archive_tool_query(
            context, kwargs, require_session=True
        )
        if not allowed:
            return _archive_tool_error(scoped_kwargs["error"])
        return _archive_json_result(
            await asyncio.to_thread(self.plugin.get_context_messages, **scoped_kwargs)
        )


@register("astrbot_plugin_chat_archive", "yukino42", "高性能聊天记录存档插件", "1.2")
class ChatArchivePlugin(Star):
    # Batch writer configuration
    _BATCH_SIZE = 50
    _FLUSH_INTERVAL = 2.0  # seconds

    def __init__(self, context: Context, config: AstrBotConfig = None):
        super().__init__(context)
        self.conf = config

        self._shutting_down = False
        self._download_locks: dict[str, asyncio.Lock] = {}

        # Track active background tasks to avoid garbage collection and 'Task was destroyed but it is pending' errors
        self._background_tasks = set()

        # Write buffer: queue + background writer
        self._write_queue: asyncio.Queue = asyncio.Queue(maxsize=5000)
        self._writer_task: asyncio.Task | None = None
        self._writer_lock = asyncio.Lock()

        # 插件启动时，显式调用建表逻辑，确保数据库表结构就绪
        try:
            init_db()
            logger.info("Chat Archive: 数据库与表结构初始化成功。")
        except Exception as e:
            logger.error(f"Chat Archive: 初始化数据库失败: {e}")

        self._register_llm_tools()

        # Cache blacklist as set for O(1) lookup
        basic_conf = self.conf.get("basic", {}) if self.conf else {}
        self._ignored_users: set[str] = {
            str(u) for u in basic_conf.get("ignored_users", [])
        }

        self.web_server = None
        if AdminServer:
            web_conf = self.conf.get("web_server", {}) if self.conf else {}
            if web_conf.get("enable", True):
                host = os.environ.get("ARCHIVE_HOST", "").strip() or web_conf.get("host", "127.0.0.1")
                try:
                    port = int(os.environ.get("ARCHIVE_PORT", "").strip() or web_conf.get("port", 8090))
                except (TypeError, ValueError):
                    port = 8090
                api_key = os.environ.get("ARCHIVE_API_KEY", "").strip() or web_conf.get("api_key", "")

                if not api_key:
                    import secrets
                    api_key = secrets.token_urlsafe(16)
                    logger.warning("\n" + "=" * 60 +
                                   f"\n[Chat Archive 安全警告] 您未在配置中指定访问验证 Key (api_key)！"
                                   f"\n为了保障您的聊天记录隐私，系统已自动生成一个强随机密码："
                                   f"\n👉👉 {api_key} 👈👈"
                                   f"\n请使用上述密码登录 Web 仪表盘。您随时可以在插件的配置选项中设置自定义的 api_key。"
                                   "\n" + "=" * 60 + "\n")

                self.web_server = AdminServer(
                    plugin_instance=self, host=host, port=port, api_key=api_key, cache_dir=STATIC_CACHE_DIR
                )
                self.web_server.run_in_thread()
            else:
                logger.info("Chat Archive: 内置 Web 面板已禁用（已通过外部或 systemd 解耦运行）。")

        # Start clean-up background task
        self._clean_task = None
        try:
            loop = asyncio.get_running_loop()
            self._clean_task = loop.create_task(self._periodic_clean_loop())
        except RuntimeError:
            pass

    def _register_llm_tools(self):
        """注册聊天存档查询 LLM 工具到 AstrBot Context。"""
        try:
            self.context.add_llm_tools(
                ArchiveGetHistoryTool(plugin=self),
                ArchiveGetSessionsTool(plugin=self),
                ArchiveGetMemberRankTool(plugin=self),
                ArchiveGetUserSummaryTool(plugin=self),
                ArchiveGetMessageCountTool(plugin=self),
                ArchiveGetContextMessagesTool(plugin=self),
            )
            logger.info("Chat Archive: 已注册 6 个 LLM 查询工具。")
        except Exception as e:
            logger.warning(f"Chat Archive: 注册 LLM 查询工具失败: {e}")

    @staticmethod
    def _extract_tool_event(context):
        """Best-effort extraction of the AstrBot event from an LLM tool context."""
        queue = [context]
        seen = set()
        event_attrs = ("event", "message_event", "astr_message_event")
        nested_attrs = ("context", "ctx", "data", "value", "payload", "run_context")

        while queue:
            obj = queue.pop(0)
            if obj is None:
                continue
            obj_id = id(obj)
            if obj_id in seen:
                continue
            seen.add(obj_id)

            if hasattr(obj, "unified_msg_origin") or hasattr(obj, "get_sender_id"):
                return obj

            for attr in event_attrs:
                try:
                    candidate = getattr(obj, attr, None)
                except Exception:
                    candidate = None
                if candidate is not None and (
                    hasattr(candidate, "unified_msg_origin")
                    or hasattr(candidate, "get_sender_id")
                ):
                    return candidate
                if candidate is not None and len(queue) < 32:
                    queue.append(candidate)

            for attr in nested_attrs:
                try:
                    candidate = getattr(obj, attr, None)
                except Exception:
                    candidate = None
                if candidate is not None and len(queue) < 32:
                    queue.append(candidate)

        return None

    @staticmethod
    def _event_session_id(event) -> str:
        if not event:
            return ""
        for attr in ("unified_msg_origin", "session_id"):
            value = getattr(event, attr, "")
            if value:
                return str(value)
        return ""

    @staticmethod
    def _event_sender_id(event) -> str:
        if not event:
            return ""
        try:
            sender_id = event.get_sender_id()
            if sender_id:
                return str(sender_id)
        except Exception:
            pass
        return str(getattr(event, "sender_id", "") or "")

    @staticmethod
    def _load_admin_ids() -> set[str]:
        admin_ids = set()
        try:
            config_dir = DATA_DIR.parent.parent / "config"
            if not config_dir.exists():
                config_dir = Path(__file__).resolve().parent.parent / "config"
            if config_dir.exists():
                for f in config_dir.glob("abconf_*.json"):
                    with open(f, "r", encoding="utf-8-sig") as fh:
                        data = json.load(fh)
                    for admin in data.get("admins_id", []):
                        admin_str = str(admin).strip()
                        if not admin_str:
                            continue
                        admin_ids.add(admin_str)
                        if admin_str.startswith("UID: "):
                            admin_ids.add(admin_str.replace("UID: ", "").strip())
        except Exception as e:
            logger.debug(f"Chat Archive: 加载管理员 ID 失败: {e}")
        return admin_ids

    def _is_admin_tool_context(self, context) -> bool:
        event = self._extract_tool_event(context)
        if not event:
            return False

        try:
            is_admin = getattr(event, "is_admin", None)
            if callable(is_admin) and is_admin():
                return True
        except Exception:
            pass

        sender_id = self._event_sender_id(event)
        return bool(sender_id and sender_id in self._load_admin_ids())

    def _prepare_archive_tool_query(
        self, context, kwargs: dict, require_session: bool = True
    ) -> tuple[bool, dict]:
        scoped_kwargs = dict(kwargs)
        event = self._extract_tool_event(context)
        current_session_id = self._event_session_id(event)
        is_admin = self._is_admin_tool_context(context)
        requested_session_id = str(scoped_kwargs.get("session_id") or "").strip()

        if requested_session_id:
            if current_session_id and requested_session_id == current_session_id:
                return True, scoped_kwargs
            if is_admin:
                return True, scoped_kwargs
            return False, {
                "error": "权限不足：只能查询当前会话的归档，跨会话查询需要管理员权限。"
            }

        if current_session_id:
            scoped_kwargs["session_id"] = current_session_id
            return True, scoped_kwargs

        if is_admin and not require_session:
            return True, scoped_kwargs

        return False, {"error": "无法确认当前会话，已拒绝归档查询以保护聊天隐私。"}

    async def terminate(self):
        self._shutting_down = True
        # Cancel the clean-up task
        if self._clean_task and not self._clean_task.done():
            self._clean_task.cancel()
            try:
                await self._clean_task
            except asyncio.CancelledError:
                pass

        # Wait for all background tasks to finish
        if self._background_tasks:
            pending_tasks = [t for t in self._background_tasks if not t.done()]
            if pending_tasks:
                logger.info(f"Chat Archive: 等待 {len(pending_tasks)} 个后台任务执行完毕...")
                await asyncio.gather(*pending_tasks, return_exceptions=True)
            self._background_tasks.clear()

        # Cancel the batch writer and flush remaining messages
        if self._writer_task and not self._writer_task.done():
            self._writer_task.cancel()
            try:
                await self._writer_task
            except asyncio.CancelledError:
                pass

        # Final flush of any remaining queued messages
        remaining = []
        while not self._write_queue.empty():
            try:
                remaining.append(self._write_queue.get_nowait())
            except asyncio.QueueEmpty:
                break
        if remaining:
            self._flush_batch_sync(remaining)

        if self.web_server:
            await self.web_server.stop()

    async def _ensure_writer_started(self):
        """Lazily start the background batch writer task."""
        async with self._writer_lock:
            if not self._shutting_down and (self._writer_task is None or self._writer_task.done()):
                self._writer_task = asyncio.create_task(self._batch_writer())

    async def _enqueue_record(self, record: tuple):
        """Enqueue a record with backpressure to avoid unbounded memory growth."""
        if self._shutting_down:
            return
        try:
            await asyncio.wait_for(self._write_queue.put(record), timeout=1.0)
        except asyncio.TimeoutError:
            logger.error("Chat Archive: 写入队列已满，丢弃一条归档记录以保护主进程内存。")

    async def _batch_writer(self):
        """Background coroutine: drain queue and batch-insert to DB."""
        buffer: list[tuple] = []
        while True:
            try:
                # Wait for first item or timeout
                try:
                    item = await asyncio.wait_for(
                        self._write_queue.get(), timeout=self._FLUSH_INTERVAL
                    )
                    buffer.append(item)
                except asyncio.TimeoutError:
                    pass

                # Drain remaining items from queue (non-blocking)
                while not self._write_queue.empty() and len(buffer) < self._BATCH_SIZE:
                    try:
                        buffer.append(self._write_queue.get_nowait())
                    except asyncio.QueueEmpty:
                        break

                # Flush if buffer has data
                if buffer:
                    batch = buffer.copy()
                    buffer.clear()
                    await asyncio.to_thread(self._flush_batch_sync, batch)
            except asyncio.CancelledError:
                # Flush remaining on shutdown
                while not self._write_queue.empty():
                    try:
                        buffer.append(self._write_queue.get_nowait())
                    except asyncio.QueueEmpty:
                        break
                if buffer:
                    try:
                        self._flush_batch_sync(buffer)
                    except Exception as e:
                        logger.error(f"Chat Archive: final flush error: {e}")
                break
            except Exception as e:
                logger.error(
                    f"Chat Archive: batch writer error, discarding {len(buffer)} messages: {e}"
                )
                buffer.clear()

    def _write_failed_batch(self, batch: list[tuple], reason: str):
        """Persist failed DB writes for later manual recovery instead of silently losing them."""
        try:
            DATA_DIR.mkdir(parents=True, exist_ok=True)
            failed_path = DATA_DIR / "chat_archive_failed_writes.jsonl"
            with open(failed_path, "a", encoding="utf-8") as f:
                for item in batch:
                    f.write(json.dumps({"reason": reason, "record": item, "failed_at": int(time.time())}, ensure_ascii=False) + "\n")
            logger.error(f"Chat Archive: {len(batch)} 条写入失败记录已保存到 {failed_path}")
        except Exception as e:
            logger.error(f"Chat Archive: 保存失败写入记录也失败了: {e}")

    def _flush_batch_sync(self, batch: list[tuple]):
        """Flush a batch of messages to the database (runs in thread)."""
        if not batch:
            return
        conn = None
        retries = 3
        delays = [0.5, 1.0, 2.0]
        
        for attempt in range(retries):
            try:
                conn = get_db_connection()
                # session_name length matching the tuple
                conn.executemany(
                    "INSERT INTO chat_history (user_id, sender_name, message, timestamp, session_id, message_type, session_name, msg_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    batch,
                )
                conn.commit()
                break
            except sqlite3.OperationalError as e:
                if conn:
                    try:
                        conn.rollback()
                    except Exception as rollback_error:
                        logger.error(f"Chat Archive: DB rollback failed: {rollback_error}")
                if attempt < retries - 1:
                    logger.warning(f"Chat Archive: DB busy, retrying flush in {delays[attempt]}s: {e}")
                    time.sleep(delays[attempt])
                else:
                    logger.error(f"Chat Archive: Failed to flush batch after retries ({len(batch)} msgs): {e}")
                    self._write_failed_batch(batch, str(e))
            except Exception as e:
                if conn:
                    try:
                        conn.rollback()
                    except Exception as rollback_error:
                        logger.error(f"Chat Archive: DB rollback failed: {rollback_error}")
                logger.error(
                    f"Chat Archive: Failed to flush batch ({len(batch)} msgs): {e}"
                )
                self._write_failed_batch(batch, str(e))
                break
            finally:
                if conn:
                    conn.close()
                    conn = None

    @staticmethod
    def _serialize_message_chain(chain) -> str:
        """将 AstrBot 消息链序列化为包含 CQ 码的字符串，保留媒体 URL。"""
        parts = []
        for comp in chain:
            cls_name = comp.__class__.__name__
            try:
                if cls_name == "Plain":
                    parts.append(getattr(comp, "text", ""))
                elif cls_name == "Image":
                    url = getattr(comp, "url", "") or getattr(comp, "file", "")
                    if url:
                        width = ChatArchivePlugin._positive_int(getattr(comp, "width", 0))
                        height = ChatArchivePlugin._positive_int(getattr(comp, "height", 0))
                        dim_str = f",width={width},height={height}" if width and height else ""
                        parts.append(f"[CQ:image,url={url}{dim_str}]")
                    else:
                        parts.append("[CQ:image]")
                elif cls_name == "Video":
                    url = getattr(comp, "file", "") or getattr(comp, "url", "")
                    if url:
                        parts.append(f"[CQ:video,url={url}]")
                    else:
                        parts.append("[CQ:video]")
                elif cls_name == "Record":
                    url = getattr(comp, "url", "") or getattr(comp, "file", "")
                    if url:
                        parts.append(f"[CQ:record,url={url}]")
                    else:
                        parts.append("[语音]")
                elif cls_name == "Face":
                    face_id = getattr(comp, "id", "")
                    parts.append(f"[CQ:face,id={face_id}]")
                elif cls_name == "At":
                    qq = getattr(comp, "qq", "")
                    parts.append(f"[CQ:at,qq={qq}]")
                elif cls_name == "Reply":
                    rid = getattr(comp, "id", "")
                    parts.append(f"[CQ:reply,id={rid}]")
                elif cls_name in ("Forward", "Node", "Nodes"):
                    parts.append("[合并转发]")
                elif cls_name == "File":
                    name = getattr(comp, "name", "文件")
                    parts.append(f"[文件: {name}]")
                elif cls_name == "Poke":
                    parts.append("[戳一戳]")
                else:
                    text = getattr(comp, "text", None)
                    if text:
                        parts.append(str(text))
            except Exception as e:
                logger.debug(f"Chat Archive: failed to serialize {cls_name}: {e}")
        return "".join(parts)

    @staticmethod
    def _positive_int(value) -> int:
        try:
            number = int(value)
        except (TypeError, ValueError):
            return 0
        return number if number > 0 else 0

    @staticmethod
    def _valid_image_dimensions(width: int, height: int) -> bool:
        return 0 < width <= 100000 and 0 < height <= 100000

    @staticmethod
    def _read_image_dimensions(filepath: str) -> tuple[int, int] | None:
        """Read PNG/JPEG/GIF/WebP dimensions from headers without external deps."""
        import struct

        try:
            with open(filepath, "rb") as f:
                header = f.read(30)
                if len(header) < 10:
                    return None

                if header[:8] == b"\x89PNG\r\n\x1a\n" and len(header) >= 24:
                    width, height = struct.unpack(">II", header[16:24])
                    return (width, height) if ChatArchivePlugin._valid_image_dimensions(width, height) else None

                if header[:3] == b"GIF":
                    width, height = struct.unpack("<HH", header[6:10])
                    return (width, height) if ChatArchivePlugin._valid_image_dimensions(width, height) else None

                if header[:2] == b"\xff\xd8":
                    f.seek(2)
                    sof_markers = set(range(0xC0, 0xC4)) | set(range(0xC5, 0xC8)) | set(range(0xC9, 0xCC)) | set(range(0xCD, 0xD0))
                    while True:
                        marker_prefix = f.read(1)
                        if not marker_prefix:
                            break
                        if marker_prefix != b"\xff":
                            continue
                        marker_byte = f.read(1)
                        if not marker_byte:
                            break
                        marker = marker_byte[0]
                        while marker == 0xFF:
                            marker_byte = f.read(1)
                            if not marker_byte:
                                return None
                            marker = marker_byte[0]
                        if marker == 0xD9:
                            break
                        if 0xD0 <= marker <= 0xD7 or marker == 0x01:
                            continue
                        length_bytes = f.read(2)
                        if len(length_bytes) < 2:
                            break
                        length = struct.unpack(">H", length_bytes)[0]
                        if length < 2:
                            break
                        if marker in sof_markers:
                            data = f.read(5)
                            if len(data) < 5:
                                break
                            height, width = struct.unpack(">HH", data[1:5])
                            return (width, height) if ChatArchivePlugin._valid_image_dimensions(width, height) else None
                        f.seek(length - 2, 1)
                    return None

                if header[:4] == b"RIFF" and header[8:12] == b"WEBP":
                    chunk = header[12:16]
                    if chunk == b"VP8 " and len(header) >= 30:
                        width = struct.unpack("<H", header[26:28])[0] & 0x3FFF
                        height = struct.unpack("<H", header[28:30])[0] & 0x3FFF
                        return (width, height) if ChatArchivePlugin._valid_image_dimensions(width, height) else None
                    if chunk == b"VP8L" and len(header) >= 25:
                        bits = struct.unpack("<I", header[21:25])[0]
                        width = (bits & 0x3FFF) + 1
                        height = ((bits >> 14) & 0x3FFF) + 1
                        return (width, height) if ChatArchivePlugin._valid_image_dimensions(width, height) else None
                    if chunk == b"VP8X":
                        f.seek(24)
                        canvas = f.read(6)
                        if len(canvas) == 6:
                            width = (canvas[0] | (canvas[1] << 8) | (canvas[2] << 16)) + 1
                            height = (canvas[3] | (canvas[4] << 8) | (canvas[5] << 16)) + 1
                            return (width, height) if ChatArchivePlugin._valid_image_dimensions(width, height) else None
        except Exception:
            return None
        return None

    @staticmethod
    def _cq_param_exists(inner: str, key: str) -> bool:
        return re.search(rf"(?:^|,){re.escape(key)}=", inner) is not None

    @staticmethod
    @contextmanager
    def _db_conn():
        """Context manager for database connections."""
        conn = None
        try:
            conn = get_db_connection()
            yield conn
        finally:
            if conn:
                conn.close()

    def _get_allowed_media_domains(self) -> set[str]:
        """Return configured media-cache domain allowlist."""
        basic_conf = self.conf.get("basic", {}) if self.conf else {}
        configured = basic_conf.get("allowed_media_domains", [])
        domains = configured or list(DEFAULT_ALLOWED_MEDIA_DOMAINS)
        return {str(d).strip().lower().rstrip(".") for d in domains if str(d).strip()}

    def _get_max_media_bytes(self) -> int:
        basic_conf = self.conf.get("basic", {}) if self.conf else {}
        try:
            mb = int(basic_conf.get("media_max_mb", 50))
        except (TypeError, ValueError):
            mb = 50
        mb = max(1, min(mb, 200))
        return mb * 1024 * 1024

    @staticmethod
    def _hostname_matches_allowlist(hostname: str, domains: set[str]) -> bool:
        host = (hostname or "").lower().rstrip(".")
        return any(host == d or host.endswith("." + d) for d in domains)

    @staticmethod
    def _ip_is_public(ip: ipaddress._BaseAddress) -> bool:
        return not (
            ip.is_private
            or ip.is_loopback
            or ip.is_link_local
            or ip.is_multicast
            or ip.is_reserved
            or ip.is_unspecified
        )

    async def _hostname_resolves_to_public_ips(self, hostname: str) -> bool:
        """Block localhost/private/link-local destinations before media caching."""
        try:
            try:
                ip = ipaddress.ip_address(hostname)
                return self._ip_is_public(ip)
            except ValueError:
                pass

            infos = await asyncio.to_thread(
                socket.getaddrinfo, hostname, None, type=socket.SOCK_STREAM
            )
            if not infos:
                return False
            for info in infos:
                ip = ipaddress.ip_address(info[4][0])
                if not self._ip_is_public(ip):
                    logger.warning(f"Chat Archive: 拒绝缓存解析到非公网地址的媒体域名 {hostname} -> {ip}")
                    return False
            return True
        except Exception as e:
            logger.warning(f"Chat Archive: 媒体域名解析校验失败 {hostname}: {e}")
            return False

    async def _download_media_to_cache(self, url: str) -> str:
        """下载媒体文件到本地缓存，并返回其相对 Web 路径 '/static/cache/...'。
        如果下载失败，返回原始 URL。
        """
        if not url:
            return ""
        
        # Check if url is already cached
        if url.startswith("/static/cache/"):
            return url
            
        # Security: only cache explicitly allowed public HTTP(S) media origins.
        parsed_url = urlparse(url)
        if parsed_url.scheme not in ("http", "https"):
            logger.warning(f"Chat Archive: 拒绝下载危险的 URL 协议 {url}")
            return url
        hostname = parsed_url.hostname
        if not hostname:
            logger.warning(f"Chat Archive: 拒绝下载无效媒体 URL {url}")
            return url
        allowed_domains = self._get_allowed_media_domains()
        if not self._hostname_matches_allowlist(hostname, allowed_domains):
            logger.warning(f"Chat Archive: 拒绝缓存非白名单媒体域名 {hostname}")
            return url
        if not await self._hostname_resolves_to_public_ips(hostname):
            return url
        max_media_bytes = self._get_max_media_bytes()

        try:
            STATIC_CACHE_DIR.mkdir(parents=True, exist_ok=True)
        except Exception as e:
            logger.error(f"Chat Archive: 创建缓存目录失败: {e}")
            return url

        # Generate filename
        # Compute MD5 hash of URL
        url_hash = hashlib.md5(url.encode("utf-8")).hexdigest()
        
        if url_hash not in self._download_locks:
            self._download_locks[url_hash] = asyncio.Lock()
            
        async with self._download_locks[url_hash]:
            # Try to guess extension from URL
            ext = ""
            url_lower = url.lower()
            if ".png" in url_lower:
                ext = ".png"
            elif ".gif" in url_lower:
                ext = ".gif"
            elif ".webp" in url_lower:
                ext = ".webp"
            elif ".jpg" in url_lower or ".jpeg" in url_lower:
                ext = ".jpg"
            elif ".mp4" in url_lower:
                ext = ".mp4"
            elif ".webm" in url_lower:
                ext = ".webm"
            elif ".avi" in url_lower:
                ext = ".avi"
            elif ".mkv" in url_lower:
                ext = ".mkv"
            
            # Default fallback if extension not found
            if not ext:
                if "video" in url_lower:
                    ext = ".mp4"
                else:
                    ext = ".jpg"
                    
            filename = f"{url_hash}{ext}"
            dest_path = STATIC_CACHE_DIR / filename
            relative_url = f"/static/cache/{filename}"

            # If already exists, return local path directly
            if dest_path.exists():
                return relative_url

            headers = {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/110.0.0.0 Safari/537.36",
                "Referer": "https://q.qq.com/",
            }

            try:
                # Do not follow redirects automatically; a redirect could jump to a non-allowlisted/internal host.
                async with httpx.AsyncClient(timeout=30.0, follow_redirects=False) as client:
                    async with client.stream("GET", url, headers=headers) as response:
                        if response.status_code != 200:
                            logger.warning(f"Chat Archive: 下载媒体失败 {url}, 状态码 {response.status_code}")
                            return url

                        content_type = response.headers.get("content-type", "").split(";", 1)[0].strip().lower()
                        if not (content_type.startswith("image/") or content_type.startswith("video/")):
                            logger.warning(f"Chat Archive: 拒绝缓存非图片/视频响应 {url}, content-type={content_type or 'unknown'}")
                            return url

                        content_length = response.headers.get("content-length")
                        if content_length:
                            try:
                                if int(content_length) > max_media_bytes:
                                    logger.warning(f"Chat Archive: 拒绝缓存超大媒体 {url}, size={content_length}")
                                    return url
                            except ValueError:
                                pass

                        # Refine extension based on content-type.
                        new_ext = ""
                        if content_type == "image/png":
                            new_ext = ".png"
                        elif content_type == "image/gif":
                            new_ext = ".gif"
                        elif content_type == "image/webp":
                            new_ext = ".webp"
                        elif content_type in ("image/jpeg", "image/jpg"):
                            new_ext = ".jpg"
                        elif content_type == "video/mp4":
                            new_ext = ".mp4"
                        elif content_type == "video/webm":
                            new_ext = ".webm"

                        if new_ext:
                            filename = f"{url_hash}{new_ext}"
                            dest_path = STATIC_CACHE_DIR / filename
                            relative_url = f"/static/cache/{filename}"
                            if dest_path.exists():
                                return relative_url

                        # Write to temp file first to avoid corrupted/partial file cache on interrupted downloads.
                        temp_path = dest_path.with_suffix(".tmp")
                        downloaded = 0
                        try:
                            with open(temp_path, "wb") as f:
                                async for chunk in response.aiter_bytes():
                                    downloaded += len(chunk)
                                    if downloaded > max_media_bytes:
                                        raise ValueError(f"media too large: {downloaded} bytes")
                                    f.write(chunk)
                            temp_path.rename(dest_path)
                        except Exception:
                            try:
                                temp_path.unlink(missing_ok=True)
                            except Exception:
                                pass
                            raise
                        logger.info(f"Chat Archive: 成功缓存媒体到 {dest_path}")
                        return relative_url
            except Exception as e:
                logger.error(f"Chat Archive: 下载媒体异常 {url}: {e}")
                
            return url

    async def _replace_cq_media_url(self, match) -> str:
        cq_type = match.group(1)  # 'image' or 'video'
        inner = match.group(2)
        # Find url=...
        url_match = re.search(r"url=(https?://[^,\]]+)", inner)
        if url_match:
            original_url = url_match.group(1)
            # Decode potential XML/HTML entities in URL
            url = original_url.replace("&amp;", "&").replace("&#44;", ",")
            cached_url = await self._download_media_to_cache(url)
            # Replace the original url with cached url in the CQ code
            new_inner = inner.replace(original_url, cached_url)
            if (
                cq_type == "image"
                and not self._cq_param_exists(new_inner, "width")
                and not self._cq_param_exists(new_inner, "height")
                and cached_url.startswith("/static/cache/")
            ):
                cached_path = STATIC_CACHE_DIR / cached_url.rsplit("/", 1)[-1]
                dims = self._read_image_dimensions(str(cached_path))
                if dims:
                    new_inner += f",width={dims[0]},height={dims[1]}"
            return f"[CQ:{cq_type},{new_inner}]"
        return match.group(0)

    async def _process_and_cache_media_in_string(self, text: str) -> str:
        """解析字符串中的 CQ 码，并下载其中的图片和视频媒体，替换为本地缓存路径。"""
        if not text:
            return text

        pattern = r"\[CQ:(image|video),([^\]]+)\]"
        matches = list(re.finditer(pattern, text))
        if not matches:
            return text

        # Process matches from right to left to avoid index shifting
        for match in reversed(matches):
            replaced = await self._replace_cq_media_url(match)
            start, end = match.span()
            text = text[:start] + replaced + text[end:]

        return text

    @staticmethod
    def _get_field(obj, key, default=None):
        """兼容 dict-like 对象的字段提取。"""
        if not obj:
            return default
        if hasattr(obj, "get") and callable(obj.get):
            res = obj.get(key)
            if res is not None:
                return res
        if hasattr(obj, "__getitem__"):
            try:
                res = obj[key]
                if res is not None:
                    return res
            except (KeyError, TypeError):
                pass
        return getattr(obj, key, default)

    @staticmethod
    def _extract_from_dirty_str(s, key):
        """从脏字符串中提取指定 key 的值。"""
        if not isinstance(s, str):
            return None
        patterns = [
            rf"['\"]{key}['\"]\s*:\s*['\"]([^'\"]*)['\"]",
            rf"['\"]{key}['\"]\s*:\s*(\d+)",
        ]
        for p in patterns:
            m = re.search(p, s)
            if m:
                return m.group(1)
        return None

    @filter.event_message_type(EventMessageType.ALL)
    async def record_message(self, event: AstrMessageEvent):
        """
        拦截所有的消息事件，并存档至数据库。
        """
        # 读取配置
        basic_conf = self.conf.get("basic", {}) if self.conf else {}
        if not basic_conf.get("enable_archive", True):
            return

        cache_media = basic_conf.get("cache_media", False)

        user_id = str(event.get_sender_id())

        # O(1) blacklist lookup via cached set
        if user_id in self._ignored_users:
            return

        # 1. 优先使用 AstrBot 消息链序列化（含已解析的媒体 URL）
        raw_message = ""
        try:
            chain = event.get_messages()
            if chain:
                raw_message = self._serialize_message_chain(chain)
        except Exception:
            pass

        # 2. 回退：尝试从平台原始事件对象中提取 raw_message (CQ 码字符串)
        platform_raw = getattr(event.message_obj, "raw_message", None)
        if not raw_message:
            raw_message = self._get_field(platform_raw, "raw_message", "")

            if isinstance(raw_message, str) and (
                raw_message.startswith("<Event") or raw_message.startswith("{")
            ):
                extracted = self._extract_from_dirty_str(raw_message, "raw_message")
                if extracted:
                    raw_message = extracted

        # 3. 提取基础信息
        nickname = event.get_sender_name()

        # 清理脏字符串形式的 user_id
        if isinstance(user_id, str) and (
            user_id.startswith("<Event") or user_id.startswith("{")
        ):
            extracted = self._extract_from_dirty_str(user_id, "user_id")
            if extracted:
                user_id = extracted

        timestamp = self._get_field(platform_raw, "time", int(time.time()))
        if isinstance(timestamp, str) and len(str(timestamp)) > 10:
            extracted = self._extract_from_dirty_str(str(timestamp), "time")
            if extracted:
                timestamp = int(extracted)

        # 4. 提取会话信息
        session_id = event.unified_msg_origin
        try:
            message_type = event.get_message_type().value  # group or friend
        except Exception:
            message_type = str(event.get_message_type())

        session_name = ""
        try:
            if message_type == "group":
                if hasattr(event.message_obj, "group") and event.message_obj.group:
                    session_name = event.message_obj.group.group_name or ""
        except Exception:
            pass

        # 5. 终极兜底：如果还不是 clean 字符串，用 event.message_str
        if (
            not isinstance(raw_message, str)
            or not raw_message
            or raw_message.startswith("<Event")
        ):
            raw_message = event.message_str
            if isinstance(raw_message, str) and raw_message.startswith("<Event"):
                extracted = self._extract_from_dirty_str(raw_message, "raw_message")
                raw_message = extracted if extracted else "[无法解析的消息]"

        # 6. 处理消息回撤事件 (Notice Event in OneBot 11)
        try:
            notice_type = self._get_field(platform_raw, "notice_type", "")
            if notice_type in ["group_recall", "friend_recall"]:
                recalled_msg_id = str(self._get_field(platform_raw, "message_id", ""))
                if recalled_msg_id:
                    # 将对应的原始消息标记为已撤回
                    def mark_recalled(m_id, sess_id):
                        for attempt in range(5):
                            c = None
                            try:
                                c = get_db_connection()
                                c.execute(
                                    "UPDATE chat_history SET is_recalled = 1 "
                                    "WHERE msg_id = ? AND session_id = ? AND user_id != '0'",
                                    (m_id, str(sess_id)),
                                )
                                c.commit()
                                row = c.execute(
                                    "SELECT id FROM chat_history "
                                    "WHERE msg_id = ? AND session_id = ? AND user_id != '0' "
                                    "AND is_recalled = 1 LIMIT 1",
                                    (m_id, str(sess_id)),
                                ).fetchone()
                                if row:
                                    return
                            except Exception as e:
                                logger.error(
                                    f"Chat Archive: 标记撤回失败 {m_id}: {e}"
                                )
                            finally:
                                if c:
                                    c.close()
                            time.sleep(0.5 * (attempt + 1))
                        logger.warning(f"Chat Archive: 未找到可标记撤回的消息 {m_id}")

                    if not self._shutting_down:
                        task = asyncio.create_task(
                            asyncio.to_thread(mark_recalled, recalled_msg_id, session_id)
                        )
                        self._background_tasks.add(task)
                        task.add_done_callback(self._background_tasks.discard)

                    # 同时记录一条系统消息
                    raw_message = f"🛡️ [撤回了一条消息 (ID: {recalled_msg_id})]"
                    # 让系统消息的记录也进入队列，但标记为系统类型
                    user_id = "0"
                    nickname = "系统通知"
        except Exception as e:
            logger.error(f"Chat Archive: 撤回检测异常: {e}")

        # 6.5. 缓存媒体原文件 (如果配置开启)
        if cache_media and raw_message:
            try:
                raw_message = await self._process_and_cache_media_in_string(raw_message)
            except Exception as e:
                logger.error(f"Chat Archive: 缓存媒体文件失败: {e}")

        # 7. 提取平台消息 ID
        msg_id = str(getattr(event.message_obj, "message_id", ""))
        if not msg_id:
            msg_id = str(self._get_field(platform_raw, "message_id", ""))

        if not user_id:
            return

        # Enqueue for batch write
        await self._ensure_writer_started()
        await self._enqueue_record(
            (
                str(user_id),
                str(nickname),
                str(raw_message),
                int(timestamp),
                str(session_id),
                str(message_type),
                str(session_name),
                str(msg_id),
            )
        )

    @filter.after_message_sent()
    async def handle_bot_reply(self, event: AstrMessageEvent):
        """
        在机器人成功发送消息后，捕获并存档机器人自己的回复。
        """
        # 读取配置，检查是否开启存档
        basic_conf = self.conf.get("basic", {}) if self.conf else {}
        if not basic_conf.get("enable_archive", True):
            return

        result = event.get_result()
        if not result or not result.chain:
            return

        try:
            # 1. 序列化消息链
            raw_message = self._serialize_message_chain(result.chain)
            if not raw_message:
                return

            # 2. 检查是否开启缓存媒体文件并执行缓存
            cache_media = basic_conf.get("cache_media", False)
            if cache_media:
                try:
                    raw_message = await self._process_and_cache_media_in_string(raw_message)
                except Exception as e:
                    logger.error(f"Chat Archive: 缓存机器人媒体回复失败: {e}")

            # 3. 构造机器人自己的基本信息
            user_id = str(event.get_self_id() or "bot")
            
            # 获取机器人自身的昵称，如果获取不到则使用配置或默认名称
            nickname = "Bot"
            try:
                if self.context and hasattr(self.context, "get_self_nickname"):
                    nickname = self.context.get_self_nickname() or nickname
                elif self.context and hasattr(self.context, "get_bot_name"):
                    nickname = self.context.get_bot_name() or nickname
            except Exception:
                pass

            timestamp = int(time.time())
            session_id = str(event.unified_msg_origin or event.session_id)
            
            try:
                message_type = event.get_message_type().value
            except Exception:
                try:
                    message_type = str(event.get_message_type())
                except Exception:
                    message_type = "group"
            
            # 获取会话名称
            session_name = ""
            try:
                session_name = event.get_group_name() or event.get_sender_name() or ""
            except Exception:
                pass

            # 消息 ID，对于机器人回复，尽量从 platform 侧结果获取，没有则生成临时 ID
            msg_id = f"bot_{timestamp}_{int(time.time() * 1000) % 1000}"

            # 4. Enqueue for batch write
            await self._ensure_writer_started()
            await self._enqueue_record(
                (
                    str(user_id),
                    str(nickname),
                    str(raw_message),
                    int(timestamp),
                    str(session_id),
                    str(message_type),
                    str(session_name),
                    str(msg_id),
                )
            )
            
        except Exception as e:
            logger.error(f"Chat Archive: 记录机器人回复消息异常: {e}", exc_info=True)

    # ==========================
    # Third-party Plugin API (Python API)
    # Access via: self.context.get_registered_star("astrbot_plugin_chat_archive")
    # ==========================

    @classmethod
    def get_history(cls, *args, **kwargs) -> list[dict]:
        """Advanced mixed query interface for chat history."""
        return DatabaseManager.get_history(*args, **kwargs)

    @classmethod
    def get_sessions(cls) -> list[dict]:
        """Get all sessions that have archived chat records."""
        return DatabaseManager.get_sessions()

    @classmethod
    def get_member_rank(cls, *args, **kwargs) -> list[dict]:
        """Get the top active members in a session by message count."""
        return DatabaseManager.get_member_rank(*args, **kwargs)

    @classmethod
    def get_user_summary(cls, *args, **kwargs) -> dict:
        """Get a statistical overview of a specific user."""
        return DatabaseManager.get_user_summary(*args, **kwargs)

    @classmethod
    def get_message_count(cls, *args, **kwargs) -> int:
        """Lightweight count query without fetching message data."""
        return DatabaseManager.get_message_count(*args, **kwargs)

    @classmethod
    def get_context_messages(cls, *args, **kwargs) -> list[tuple[str, str, str]]:
        """Convenience method for LLM context: returns formatted messages."""
        return DatabaseManager.get_context_messages(*args, **kwargs)

    @classmethod
    def _execute_query(cls, *args, **kwargs) -> dict:
        """Execute the actual database query (runs in thread)."""
        return DatabaseManager.execute_query(*args, **kwargs)

    async def _periodic_clean_loop(self):
        """定期清理过期缓存文件的循环。每一天运行一次。"""
        # Wait a small duration initially to let startup finish
        await asyncio.sleep(5)
        while True:
            try:
                basic_conf = self.conf.get("basic", {}) if self.conf else {}
                enable_clean = basic_conf.get("enable_clean", False)
                clean_days = basic_conf.get("clean_days", 30)

                if enable_clean and clean_days > 0:
                    await self._clean_expired_cache(clean_days)
            except Exception as e:
                logger.error(f"Chat Archive: 定期清理执行异常: {e}")
            
            # Sleep for 24 hours (86400 seconds)
            await asyncio.sleep(86400)

    async def _clean_expired_cache(self, days: int):
        """物理清理几天前的缓存文件"""
        if not STATIC_CACHE_DIR.exists():
            return

        now = time.time()
        threshold = now - (days * 86400)

        try:
            def _scan_and_delete():
                """在线程池中执行文件扫描与删除，避免阻塞事件循环。"""
                cleaned_count = 0
                cleaned_bytes = 0
                for p in STATIC_CACHE_DIR.glob("*"):
                    if p.is_file() and p.suffix != ".tmp":
                        try:
                            st = p.stat()
                            if st.st_mtime < threshold:
                                cleaned_bytes += st.st_size
                                p.unlink()
                                cleaned_count += 1
                        except Exception as e:
                            logger.debug(f"Chat Archive: 无法删除文件 {p}: {e}")
                return cleaned_count, cleaned_bytes

            cleaned_count, cleaned_bytes = await asyncio.to_thread(_scan_and_delete)

            if cleaned_count > 0:
                logger.info(f"Chat Archive: 清理了 {cleaned_count} 个过期媒体文件，释放空间 {cleaned_bytes / (1024 * 1024):.2f} MB。")
        except Exception as e:
            logger.error(f"Chat Archive: 清理过期缓存文件失败: {e}")
