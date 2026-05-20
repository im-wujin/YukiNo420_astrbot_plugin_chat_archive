import asyncio
import datetime
import json
import secrets
import os
import threading
from functools import lru_cache
from urllib.parse import urlparse
from pathlib import Path

import httpx
import uvicorn
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

try:
    from ..db_config import get_db_connection
except ImportError:
    try:
        import sys
        sys.path.append(str(Path(__file__).resolve().parent.parent))
        from db_config import get_db_connection
    except Exception:
        raise RuntimeError("无法加载 db_config 模块。如果是独立解耦运行，请使用 'python -m astrbot_plugin_chat_archive.web.server' 从 AstrBot 的 plugins 目录执行。")

from astrbot.api import logger

def get_data_dir() -> Path:
    try:
        from astrbot.api.star import StarTools
        return StarTools.get_data_dir()
    except Exception:
        # Fallback for standalone decoupling execution
        return Path(__file__).resolve().parent.parent / "data"

def _load_api_key() -> str:
    # Prioritize loading from plugin config file to have a single source of truth
    data_dir = get_data_dir()
    config_dir = data_dir.parent.parent / "config"
    if not config_dir.exists():
        config_dir = Path(__file__).resolve().parent.parent.parent.parent / "config"
    config_path = config_dir / "astrbot_plugin_chat_archive_config.json"
    if config_path.exists():
        try:
            with open(config_path, "r", encoding="utf-8-sig") as f:
                config_data = json.load(f)
                key = config_data.get("web_server", {}).get("api_key", "")
                if key:
                    return key
        except Exception as e:
            logger.error(f"从配置文件加载 API Key 失败: {e}")
            
    # Fallback to environment variable
    return os.environ.get("ARCHIVE_API_KEY", "")

API_KEY = _load_api_key()

@lru_cache(maxsize=1)
def load_right_align_ids():
    """加载管理员 ID 列表用于 WebUI 中的消息右对齐显示。
    
    注意: 使用 @lru_cache 缓存，管理员列表变更后需要重启服务才能生效。
    """
    right_align_ids = {"astrbot", "bot", "99999"}
    try:
        data_dir = get_data_dir()
        config_dir = data_dir.parent.parent / "config"
        if not config_dir.exists():
            config_dir = Path(__file__).resolve().parent.parent.parent.parent / "config"
        if config_dir.exists():
            for f in config_dir.glob("abconf_*.json"):
                with open(f, "r", encoding="utf-8-sig") as fh:
                    data = json.load(fh)
                    if "admins_id" in data:
                        for admin in data["admins_id"]:
                            admin_str = str(admin).strip()
                            right_align_ids.add(admin_str)
                            if admin_str.startswith("UID: "):
                                right_align_ids.add(admin_str.replace("UID: ", ""))
    except Exception as e:
        logger.error(f"加载管理员列表失败: {e}")
    return frozenset(right_align_ids)

app = FastAPI(title="Chat Archive Admin Panel")

current_dir = Path(__file__).resolve().parent
static_dir = current_dir / "static"
templates_dir = current_dir / "templates"

cors_origins_env = os.environ.get("CORS_ORIGINS", "http://localhost:8090")
cors_origins = [origin.strip() for origin in cors_origins_env.split(",") if origin.strip()]

app.add_middleware(
    CORSMiddleware,
    allow_origins=cors_origins,
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.middleware("http")
async def auth_middleware(request: Request, call_next):
    if not API_KEY:
        # 如果 API_KEY 为空，仅允许访问根路径和验证路径，防止 /static 中的缓存被遍历
        allowed_when_no_key = ["/", "/api/auth/verify"]
        if request.url.path not in allowed_when_no_key:
            return JSONResponse(
                status_code=401,
                content={"error": "Unauthorized", "message": "API Key is not configured. Access is disabled for security reasons."}
            )
        return await call_next(request)

    public_paths = ["/", "/static", "/api/auth/verify"]
    is_public = any(
        request.url.path == p or request.url.path.startswith(p + "/")
        for p in public_paths
    )

    if is_public:
        return await call_next(request)

    # Support API Key via header or query param (only query param for image proxy)
    req_key = request.headers.get("X-API-Key", "")
    if not req_key and request.url.path.startswith("/api/proxy/image"):
        req_key = request.query_params.get("key", "")
    
    if not req_key or not secrets.compare_digest(req_key, API_KEY):
        return JSONResponse(
            status_code=401,
            content={"error": "Unauthorized", "message": "Invalid API Key"},
        )
    return await call_next(request)

app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")
templates = Jinja2Templates(directory=str(templates_dir))

@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    try:
        return templates.TemplateResponse(
            request=request, name="index.html", context={"request": request}
        )
    except TypeError:
        return templates.TemplateResponse("index.html", {"request": request})

@app.post("/api/auth/verify")
async def verify_auth(request: Request):
    data = await request.json()
    provided_key = data.get("api_key", "").strip()
    if not API_KEY:
        return JSONResponse(content={"success": True, "message": "No auth required"})
    if provided_key and secrets.compare_digest(provided_key, API_KEY):
        return JSONResponse(content={"success": True})
    return JSONResponse(status_code=401, content={"success": False, "message": "Invalid API Key"})

@app.get("/api/history")
def get_history(
    keyword: str = "",
    user_id: str = "",
    session_id: str = "",
    time_start: int = 0,
    time_end: int = 0,
    page: int = 1,
    limit: int = 50,
    cursor: int = 0,
):
    db = None
    try:
        db = get_db_connection()
        
        conditions = ["1=1"]
        params = []

        if keyword:
            conditions.append("message LIKE ? ESCAPE '\\'")
            safe_keyword = (
                keyword.replace("\\", "\\\\")
                .replace("%", "\\%")
                .replace("_", "\\_")
            )
            params.append(f"%{safe_keyword}%")
        if user_id:
            conditions.append("user_id = ?")
            params.append(str(user_id))
        if session_id == "legacy:archive":
            conditions.append(
                "(session_id IS NULL OR session_id = '' OR session_id = 'legacy:archive')"
            )
        elif session_id:
            conditions.append("session_id = ?")
            params.append(str(session_id))
        if time_start:
            conditions.append("timestamp >= ?")
            params.append(time_start)
        if time_end:
            conditions.append("timestamp <= ?")
            params.append(time_end)

        where_cl = " WHERE " + " AND ".join(conditions)

        query_conditions = list(conditions)
        query_params = list(params)

        if cursor > 0:
            query_conditions.append("id < ?")
            query_params.append(cursor)
            where_cl_cursor = " WHERE " + " AND ".join(query_conditions)
            query = f"SELECT * FROM chat_history {where_cl_cursor} ORDER BY id DESC LIMIT ?"
            query_params.append(limit)
        else:
            # Subquery pagination optimization to avoid performance degradation on deep offsets
            query = f"SELECT * FROM chat_history WHERE id IN (SELECT id FROM chat_history {where_cl} ORDER BY id DESC LIMIT ? OFFSET ?)"
            offset = (page - 1) * limit
            query_params.extend([limit, offset])

        records = db.execute(query, query_params).fetchall()

        if records:
            next_cursor = records[-1]["id"]
            has_more = len(records) == limit
        else:
            next_cursor = 0
            has_more = False

        # Fetch total count
        count_query = f"SELECT COUNT(*) as total FROM chat_history {where_cl}"
        total = db.execute(count_query, params).fetchone()["total"]

        right_ids = load_right_align_ids()
        processed_records = []
        for r in records:
            item = dict(r)
            uid = str(item.get("user_id", "")).strip()
            sname = str(item.get("sender_name", "")).strip()
            msg_type = str(item.get("message_type", "")).strip().lower()
            
            is_right = False
            is_bot = sname.lower() == "bot" or "bot" in uid.lower() or uid == "99999"
            is_admin = uid in right_ids
            
            if is_bot:
                is_right = True
            elif is_admin and "group" in msg_type:
                is_right = True
                
            item["is_right"] = is_right
            processed_records.append(item)

        return JSONResponse(
            content={
                "success": True,
                "data": processed_records,
                "total": total,
                "page": page,
                "limit": limit,
                "next_cursor": next_cursor,
                "has_more": has_more,
            }
        )
    except Exception as e:
        logger.error(f"WebUI get_history error: {e}")
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        if db:
            db.close()

@app.get("/api/proxy/image")
async def proxy_image(url: str = Query(...)):
    """
    代理媒体请求，解决 NTQQ 域名 (multimedia.nt.qq.com.cn) 的跨域与 Referer 限制。
    支持图片和视频的代理流式传输。
    已修复：域名提取 SSRF 漏洞 与 &amp; 实体字符容错。
    """
    # 容错：替换转义的 &amp;
    url = url.replace("&amp;", "&")

    # 安全校验：仅允许代理指定的域名（精准匹配 hostname 规避 SSRF）
    try:
        parsed_url = urlparse(url)
        hostname = parsed_url.hostname
        if not hostname:
            raise HTTPException(status_code=400, detail="Invalid URL")
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid URL")

    allowed_domains = ["multimedia.nt.qq.com.cn", "gchat.qpic.cn", "q.qlogo.cn", "p.qlogo.cn", "q1.qlogo.cn"]
    if hostname not in allowed_domains:
        raise HTTPException(status_code=403, detail="Forbidden domain")

    async def stream_media():
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/110.0.0.0 Safari/537.36",
            "Referer": "https://q.qq.com/",
        }
        # Removed verify=False to comply with production security standards (MITM prevention)
        async with httpx.AsyncClient() as client:
            try:
                async with client.stream(
                    "GET", url, headers=headers, timeout=30.0
                ) as response:
                    if response.status_code != 200:
                        yield b""
                        return

                    async for chunk in response.aiter_bytes():
                        yield chunk
            except Exception as e:
                logger.error(f"Media proxy error for {url}: {e}")
                yield b""

    # 自动检测内容类型：支持图片和视频
    url_lower = url.lower()
    content_type = "application/octet-stream"
    # 视频类型
    if ".mp4" in url_lower or "video" in url_lower:
        content_type = "video/mp4"
    elif ".webm" in url_lower:
        content_type = "video/webm"
    elif ".avi" in url_lower:
        content_type = "video/x-msvideo"
    elif ".mkv" in url_lower:
        content_type = "video/x-matroska"
    # 音频类型
    elif ".mp3" in url_lower:
        content_type = "audio/mpeg"
    elif ".amr" in url_lower:
        content_type = "audio/amr"
    elif ".silk" in url_lower or ".slk" in url_lower:
        content_type = "audio/silk"
    elif ".ogg" in url_lower:
        content_type = "audio/ogg"
    elif ".wav" in url_lower:
        content_type = "audio/wav"
    # 图片类型
    elif ".png" in url_lower:
        content_type = "image/png"
    elif ".gif" in url_lower:
        content_type = "image/gif"
    elif ".webp" in url_lower:
        content_type = "image/webp"
    else:
        content_type = "image/jpeg"

    return StreamingResponse(stream_media(), media_type=content_type)


@app.get("/api/sessions")
def get_sessions():
    db = None
    try:
        db = get_db_connection()
        query = """
            SELECT COALESCE(session_id, 'legacy:archive') as session_id,
                   COALESCE(message_type, 'legacy') as message_type,
                   timestamp as last_time,
                   message as last_msg,
                   sender_name,
                   session_name
            FROM chat_history
            WHERE id IN (
                SELECT MAX(id)
                FROM chat_history
                GROUP BY COALESCE(session_id, 'legacy:archive')
            )
            ORDER BY last_time DESC
        """
        sessions = db.execute(query).fetchall()

        for s in sessions:
            s_id = s["session_id"]
            if s_id == "legacy:archive":
                s["name"] = "📦 历史记录 (未分类)"
                s["avatar"] = ""
                continue

            name = s_id
            avatar = ""
            if s["message_type"] in ["group", "GroupMessage"]:
                group_id = s_id.split(":")[-1] if ":" in s_id else s_id
                avatar = f"https://p.qlogo.cn/gh/{group_id}/{group_id}/100/"
                
                db_name = s.get("session_name")
                if db_name and db_name.strip():
                    name = db_name.strip()
                else:
                    name = f"群聊: {group_id}"

            elif s["message_type"] in ["friend", "FriendMessage"]:
                user_id = s_id.split(":")[-1] if ":" in s_id else s_id
                avatar = f"https://q1.qlogo.cn/g?b=qq&nk={user_id}&s=100"
                if s["sender_name"] and s["sender_name"].strip():
                    name = f"👤 私聊: {s['sender_name']}"
                else:
                    name = f"👤 私聊: {user_id}"

            s["name"] = name
            s["avatar"] = avatar

        return JSONResponse(content={"success": True, "data": sessions})
    except Exception as e:
        logger.error(f"WebUI get_sessions error: {e}")
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        if db:
            db.close()

@app.get("/api/stats")
def get_stats(
    session_id: str = "",
    user_id: str = "",
    time_start: int = 0,
    time_end: int = 0,
    is_private: int = 0,
):
    db = None
    try:
        db = get_db_connection()
        
        conditions = ["1=1"]
        params = []
        if session_id:
            conditions.append("session_id = ?")
            params.append(session_id)
        if user_id:
            conditions.append("user_id = ?")
            params.append(user_id)
        if time_start:
            conditions.append("timestamp >= ?")
            params.append(time_start)
        if time_end:
            conditions.append("timestamp <= ?")
            params.append(time_end)

        where_cl = " WHERE " + " AND ".join(conditions)

        # Total count
        total = db.execute(f"SELECT COUNT(*) as c FROM chat_history {where_cl}", params).fetchone()["c"]

        # Today count (using local timezone dynamically, starting from today's midnight)
        local_now = datetime.datetime.now().astimezone()
        local_today_midnight = local_now.replace(hour=0, minute=0, second=0, microsecond=0)
        today_start = int(local_today_midnight.timestamp())
        today_total = db.execute(f"SELECT COUNT(*) as c FROM chat_history {where_cl} AND timestamp >= ?", [*params, today_start]).fetchone()["c"]

        # Time distribution (24h divided into 12 slots of 2h)
        # Determine the host system's timezone offset in seconds dynamically
        local_offset = int(local_now.utcoffset().total_seconds())
        dist_sql = f"""
            SELECT CAST(((timestamp + {local_offset}) / 7200) % 12 AS INTEGER) as slot, COUNT(*) as cnt
            FROM chat_history {where_cl}
            GROUP BY slot ORDER BY slot
        """
        dist_rows = db.execute(dist_sql, params).fetchall()
        distribution = [0] * 12
        for r in dist_rows:
            distribution[r["slot"]] = r["cnt"]

        # Top users
        top_sql = f"""
            SELECT user_id, sender_name, COUNT(*) as cnt
            FROM chat_history {where_cl}
            GROUP BY user_id ORDER BY cnt DESC LIMIT 15
        """
        top_rows = db.execute(top_sql, params).fetchall()
        top_users = []
        for r in top_rows:
            top_users.append({
                "user_id": r["user_id"],
                "sender_name": r["sender_name"],
                "count": r["cnt"]
            })

        # Active days (if filtering by user_id)
        active_days = 0
        avg_text_len = 0
        message_types = []
        if user_id:
            days_sql = f"""
                SELECT COUNT(DISTINCT CAST(timestamp / 86400 AS INTEGER)) as days
                FROM chat_history {where_cl}
            """
            active_days = db.execute(days_sql, params).fetchone()["days"]

            len_sql = f"""
                SELECT AVG(LENGTH(message)) as avg_len
                FROM chat_history {where_cl}
                AND message NOT LIKE '[CQ:%' AND message NOT LIKE '<Event%'
            """
            avg_len_row = db.execute(len_sql, params).fetchone()
            avg_text_len = round(avg_len_row["avg_len"]) if avg_len_row["avg_len"] else 0

            image_count = db.execute(f"SELECT COUNT(*) as c FROM chat_history {where_cl} AND message LIKE '%[CQ:image%'", params).fetchone()["c"]
            text_count = total - image_count
            message_types = [
                {"name": "文本消息", "value": text_count},
                {"name": "图片消息", "value": image_count},
            ]

        return JSONResponse(
            content={
                "success": True,
                "data": {
                    "total_messages": total,
                    "today_messages": today_total,
                    "active_days": active_days,
                    "avg_text_length": avg_text_len,
                    "time_distribution": distribution,
                    "top_users": top_users,
                    "message_types": message_types if message_types else None,
                }
            }
        )
    except Exception as e:
        logger.error(f"WebUI get_stats error: {e}")
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        if db:
            db.close()
def _load_custom_apis():
    try:
        import importlib.util
        import stat
        data_dir = get_data_dir()
        ext_dir = data_dir.parent.parent / "chat_archive_ext"
        if not ext_dir.exists():
            ext_dir = Path(__file__).resolve().parent.parent.parent.parent / "chat_archive_ext"
        
        if ext_dir.exists():
            # Security check: directory must not be world-writable
            try:
                dir_stat = ext_dir.stat()
                if dir_stat.st_mode & stat.S_IWOTH:
                    logger.error(f"Chat Archive Ext: 拒绝加载自定义 API 扩展。原因：扩展目录 {ext_dir} 对其他用户可写，存在严重安全风险，请修改其权限 (如 chmod 700)。")
                    return
            except Exception as e:
                logger.error(f"Chat Archive Ext: 读取扩展目录属性失败: {e}")
                return

            for f in ext_dir.glob("*.py"):
                if f.name.startswith("_"):
                    continue
                try:
                    # Security check 1: prevent path traversal via symlinks
                    resolved_f = f.resolve()
                    if not str(resolved_f).startswith(str(ext_dir.resolve())):
                        logger.error(f"Chat Archive Ext: 拒绝加载自定义 API {f.name}。原因：试图跨越目录的路径探测攻击。")
                        continue

                    # Security check 2: file must not be world-writable
                    file_stat = f.stat()
                    if file_stat.st_mode & stat.S_IWOTH:
                        logger.error(f"Chat Archive Ext: 拒绝加载自定义 API {f.name}。原因：文件对其他用户可写，存在潜在代码注入风险，请修改权限 (如 chmod 600)。")
                        continue

                    spec = importlib.util.spec_from_file_location(f.stem, str(f))
                    if spec and spec.loader:
                        module = importlib.util.module_from_spec(spec)
                        spec.loader.exec_module(module)
                        if hasattr(module, "register"):
                            module.register(app, get_db_connection)
                            logger.info(f"Chat Archive Ext: 成功加载自定义 API [{f.name}]")
                except Exception as ex:
                    logger.error(f"Chat Archive Ext: 加载自定义 API {f.name} 失败: {ex}")
    except Exception as e:
        logger.error(f"Chat Archive Ext: 扫描自定义 API 失败: {e}")

# NOTE: Custom APIs are loaded in AdminServer.run_in_thread() after API_KEY is configured.
# For standalone usage (`python server.py`), see __main__ block below.
_custom_apis_loaded = False


class AdminServer:
    def __init__(self, plugin_instance, host: str = "0.0.0.0", port: int = 8090, api_key: str = "", cache_dir: Path = None):
        self.plugin = plugin_instance
        self.host = host
        self.port = port
        
        global API_KEY
        if api_key:
            API_KEY = api_key
        elif not API_KEY:
            API_KEY = os.environ.get("ARCHIVE_API_KEY", "")
            
        self.config = uvicorn.Config(app, host=self.host, port=self.port, log_level="warning")
        self.server = uvicorn.Server(self.config)
        self.thread = None

    def run_in_thread(self):
        """在守护线程中启动 Uvicorn，避免阻塞 AstrBot 主进程事件循环"""
        if self.thread and self.thread.is_alive():
            return
            
        def _run():
            asyncio.run(self.server.serve())

        self.thread = threading.Thread(target=_run, daemon=True)
        self.thread.start()
        
        # Load custom extension APIs after server starts, when API_KEY is set
        global _custom_apis_loaded
        if not _custom_apis_loaded:
            _load_custom_apis()
            _custom_apis_loaded = True
        
        logger.info(f"Chat Archive WebUI started on http://{self.host}:{self.port}")

    async def stop(self):
        """插件卸载时优雅关闭后台服务"""
        self.server.should_exit = True
        if self.thread and self.thread.is_alive():
            await asyncio.to_thread(self.thread.join, timeout=5.0)
            logger.info("Chat Archive WebUI stopped.")


if __name__ == "__main__":
    # In standalone execution, try loading API_KEY from env or config file
    if not API_KEY:
        API_KEY = os.environ.get("ARCHIVE_API_KEY", "")
    if not API_KEY:
        # Try loading from JSON config file
        try:
            data_dir = get_data_dir()
            config_path = data_dir.parent.parent / "config" / "astrbot_plugin_chat_archive_config.json"
            if config_path.exists():
                with open(config_path, "r", encoding="utf-8-sig") as fh:
                    cfg_data = json.load(fh)
                    API_KEY = cfg_data.get("web_server", {}).get("api_key", "")
        except Exception:
            pass
    if not API_KEY:
        # If API_KEY is still not set, raise RuntimeError to prevent unauthenticated public internet exposure!
        raise RuntimeError("CRITICAL SECURITY ERROR: api_key is not configured in environment (ARCHIVE_API_KEY) or config file! Standalone web server cannot start in unauthenticated mode.")

    _load_custom_apis()
    uvicorn.run(app, host="0.0.0.0", port=8090)
