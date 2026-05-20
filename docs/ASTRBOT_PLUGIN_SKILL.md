---
name: astrbot-plugin-development
description: 当需要编写、维护、审查或重构 AstrBot 插件时使用。涵盖 release 目录下 astrbot_plugin_* 插件总结出的注册、配置、命令、事件、LLM 工具、后台任务、Web API、数据目录、网络安全与发布检查模式。
metadata:
  short-description: AstrBot 插件开发、审查与加固指南
---

# AstrBot 插件开发 Skill

本 Skill 基于 `/home/yukino42/release` 下多个 `astrbot_plugin_*` 插件的实际代码模式总结，适用于：

- 新建 AstrBot 插件
- 修改已有插件的 `main.py` / `_conf_schema.json` / `metadata.yaml`
- 注册命令、事件监听、LLM 工具、Web API
- 增加定时任务、缓存、下载、外部 API 调用
- 做代码审查、安全加固、发布前检查


## 覆盖的 release 插件目录

本 Skill 生成时扫描了以下目录（含单文件插件与多模块插件）：

- `astrbot_plugin_apis`
- `astrbot_plugin_at_check`
- `astrbot_plugin_at_ignore`
- `astrbot_plugin_chat_archive_release`
- `astrbot_plugin_cloudrank`
- `astrbot_plugin_daily_news`
- `astrbot_plugin_debounce`
- `astrbot_plugin_error_notice`
- `astrbot_plugin_gemini_image_generation`
- `astrbot_plugin_gifcaijian`
- `astrbot_plugin_github_cards`
- `astrbot_plugin_group_chat_plus`
- `astrbot_plugin_groupmemberquery`
- `astrbot_plugin_hapi_connector`
- `astrbot_plugin_help`
- `astrbot_plugin_livingmemory`
- `astrbot_plugin_magnet_preview`
- `astrbot_plugin_meme_manager`
- `astrbot_plugin_memelite`
- `astrbot_plugin_mnemosyne`
- `astrbot_plugin_music`
- `astrbot_plugin_netease_download`
- `astrbot_plugin_parser`
- `astrbot_plugin_persona`
- `astrbot_plugin_persona_vote`
- `astrbot_plugin_pixiv`
- `astrbot_plugin_proactive_chat`
- `astrbot_plugin_qbittorrent_bridge`
- `astrbot_plugin_quote_collocter`
- `astrbot_plugin_rate_limit`
- `astrbot_plugin_reread`
- `astrbot_plugin_rocom`
- `astrbot_plugin_schedule`
- `astrbot_plugin_self_learning`
- `astrbot_plugin_splitter`
- `astrbot_plugin_sticker_bank`
- `astrbot_plugin_telegram_forwarder`
- `astrbot_plugin_telegram_verify`
- `astrbot_plugin_xiaoxuenai`

## 一、最小插件骨架

典型插件由 `main.py`、`metadata.yaml`、可选 `_conf_schema.json`、`requirements.txt`、资源目录组成。

```python
from astrbot.api import logger
from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star, register

@register("astrbot_plugin_example", "author", "插件描述", "1.0.0")
class ExamplePlugin(Star):
    def __init__(self, context: Context, config: dict | None = None):
        super().__init__(context)
        self.config = config or {}
        logger.info("Example 插件已加载")

    @filter.command("hello")
    async def hello(self, event: AstrMessageEvent):
        yield event.plain_result("hello")

    async def terminate(self):
        logger.info("Example 插件已卸载")
```

实际例子：`astrbot_plugin_at_ignore/main.py` 使用 `@register(...)` + `class IgnoreAtPlugin(Star)`，并在事件处理器中调用 `event.stop_event()` 拦截后续处理。

## 二、插件元数据与配置

### metadata.yaml

推荐提供：

```yaml
name: astrbot_plugin_gemini_image_generation
desc: Gemini图像生成插件，支持生图和改图，支持自动获取头像作为参考
display_name: Gemini 图像生成
version: v1.10.6
author: piexian
repo: https://github.com/piexian/astrbot_plugin_gemini_image_generation
astrbot_version: ">=4.10.4"
support_platforms:
  - aiocqhttp
```

### _conf_schema.json

配置项应提供 `description`、`hint`、`type`、`default`。例如 `astrbot_plugin_pixiv/_conf_schema.json`：

```json
{
  "push_time": {
    "description": "每日推送时间 (格式 HH:MM)",
    "type": "string",
    "default": "08:00"
  },
  "manual_admin_only": {
    "description": "手动 /pixiv 命令是否仅允许管理员触发",
    "hint": "默认开启，防止普通用户滥用推送。",
    "type": "bool",
    "default": true
  }
}
```

### 配置读取必须校验

不要直接信任用户配置。`astrbot_plugin_pixiv/main.py` 中的模式可复用：

```python
_DEFAULT_PUSH_TIME = "08:00"
_DEFAULT_IMAGE_COUNT = 3
_MAX_IMAGE_COUNT = 30

@staticmethod
def _validate_push_time(value) -> str:
    raw = str(value or _DEFAULT_PUSH_TIME).strip()
    try:
        hour_str, minute_str = raw.split(":", 1)
        hour = int(hour_str)
        minute = int(minute_str)
        if not (0 <= hour <= 23 and 0 <= minute <= 59):
            raise ValueError("hour/minute out of range")
        return f"{hour:02d}:{minute:02d}"
    except (ValueError, TypeError):
        logger.warning(f"push_time 配置非法: {raw!r}，已回退")
        return _DEFAULT_PUSH_TIME

@staticmethod
def _validate_image_count(value) -> int:
    try:
        count = int(value)
        if not (1 <= count <= _MAX_IMAGE_COUNT):
            raise ValueError("image_count out of range")
        return count
    except (ValueError, TypeError):
        return _DEFAULT_IMAGE_COUNT
```

布尔值如果可能来自字符串，建议不要 `bool("false")`，而是做显式解析：

```python
def _config_bool(value, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        v = value.strip().lower()
        if v in {"1", "true", "yes", "on", "启用", "开启"}:
            return True
        if v in {"0", "false", "no", "off", "禁用", "关闭"}:
            return False
    return default
```

## 三、命令开发模式

### 基础命令

命令处理器通常是异步生成器，通过 `yield event.plain_result(...)` 返回文本。

```python
@filter.command("pixiv_id")
async def get_group_id(self, event: AstrMessageEvent):
    uid = event.unified_msg_origin
    yield event.plain_result(f"当前群组ID:\n{uid}")
```

### 命令别名

`astrbot_plugin_parser/main.py`、`astrbot_plugin_github_cards/main.py` 中常见：

```python
@filter.command("登录B站", alias={"blogin", "登录b站"})
async def login_bilibili(self, event: AstrMessageEvent):
    ...
```

### 命令组

`astrbot_plugin_meme_manager/main.py` 使用命令组：

```python
@filter.command_group("表情管理")
def meme_manager(self):
    pass

@meme_manager.command("开启管理后台")
async def start_webui(self, event: AstrMessageEvent):
    yield event.plain_result("管理后台启动中")
```

### 权限控制

高风险命令必须加权限控制。推荐优先使用官方过滤器：

```python
@filter.permission_type(filter.PermissionType.ADMIN)
@filter.command("关闭解析")
async def close_parser(self, event: AstrMessageEvent):
    yield event.plain_result("当前会话的解析已关闭")
```

也可以做配置化权限，例如 `/pixiv` 手动推送：

```python
@filter.command("pixiv")
async def pixiv_manual(self, event: AstrMessageEvent):
    if self.manual_admin_only and not event.is_admin():
        yield event.plain_result("权限不足：只有管理员可以手动触发 Pixiv 推送。")
        event.stop_event()
        return
    yield event.plain_result("正在校验")
    event.stop_event()
```

### 私聊/群聊限制

涉及后台、Token、敏感链接的命令建议限制私聊。`astrbot_plugin_meme_manager/main.py` 的模式：

```python
if event.get_message_type() != PlatformMessageType.FRIEND_MESSAGE:
    yield event.plain_result("该指令仅限私聊使用。")
    return
```

## 四、事件监听与拦截

### 监听所有消息

`astrbot_plugin_parser/main.py`：

```python
@filter.event_message_type(filter.EventMessageType.ALL)
async def on_message(self, event: AstrMessageEvent):
    umo = event.unified_msg_origin
    if self.cfg.whitelist and umo not in self.cfg.whitelist:
        return
    if self.cfg.blacklist and umo in self.cfg.blacklist:
        return
    chain = event.get_messages()
    if not chain:
        return
```

### 高优先级拦截

`astrbot_plugin_at_ignore/main.py` 使用最大优先级在其他处理前拦截：

```python
from sys import maxsize
import astrbot.api.message_components as Comp

@filter.event_message_type(filter.EventMessageType.ALL, priority=maxsize)
async def ignore_at_messages(self, event: AstrMessageEvent):
    bot_id = str(event.get_self_id())
    for message in event.get_messages():
        if isinstance(message, Comp.At) and str(message.qq) == bot_id:
            event.stop_event()
            return
```

使用 `event.stop_event()` 时要谨慎：它会阻止后续 LLM 和插件继续处理。

## 五、消息发送与消息组件

### 简单响应

```python
yield event.plain_result("操作完成")
```

### 主动发送到会话

```python
from astrbot.core.message.message_event_result import MessageChain

await self.context.send_message(session_id, MessageChain().message("推送内容"))
```

### 图片发送

```python
from astrbot.api.message_components import Image

yield event.image_result(image_path)
# 或
chain = MessageChain([Image.fromFileSystem(image_path)])
await self.context.send_message(session_id, chain)
```

### 合并转发节点

`astrbot_plugin_pixiv/main.py` 使用 `Node` / `Nodes`：

```python
from astrbot.api.message_components import Image, Node, Nodes, Plain

content = [Plain("标题\n链接")]
content.append(Image.fromFileSystem(local_path))
node = Node(uin=sender_uin, name=sender_name, content=content)
merge_msg = MessageChain()
merge_msg.chain = [Nodes(nodes=[node])]
await self.context.send_message(group_id, merge_msg)
```

## 六、LLM 工具开发

AstrBot 插件中有两种常见 LLM 工具注册方式。

### 方式 A：`@filter.llm_tool`

适合简单工具。`astrbot_plugin_groupmemberquery/main.py`：

```python
@filter.llm_tool(name="get_group_members_info")
async def get_group_members(self, event: AstrMessageEvent) -> str:
    group_id = event.get_group_id()
    if not group_id:
        return json.dumps({"error": "这不是群聊"}, ensure_ascii=False)
    members_info = await self._get_group_members_internal(event)
    return json.dumps({"group_id": group_id, "members": members_info}, ensure_ascii=False)
```

工具文档字符串应说明：何时调用、参数含义、返回格式、平台限制。

`astrbot_plugin_hapi_connector/main.py` 的工具文档包含 Args，便于模型理解：

```python
@filter.llm_tool(name="hapi_coding_list_sessions")
async def tool_list_sessions(self, event, window: str = "", path: str = "", agent: str = ""):
    '''列出 HAPI 的可交互 session 列表。

    Args:
        window(string): 窗口过滤，空=当前窗口，all=所有窗口
        path(string): 路径搜索关键词
        agent(string): 代理类型，claude/codex/gemini/opencode
    '''
    ...
```

### 方式 B：`FunctionTool` + `context.add_llm_tools`

适合复杂工具、动态参数 schema、多模态返回。`astrbot_plugin_gemini_image_generation/tl/llm_tools.py` 的模式：

```python
from astrbot.core.agent.run_context import ContextWrapper
from astrbot.core.agent.tool import FunctionTool, ToolExecResult
from astrbot.core.astr_agent_context import AstrAgentContext
from pydantic import Field
from pydantic.dataclasses import dataclass

@dataclass
class MyTool(FunctionTool[AstrAgentContext]):
    name: str = "my_tool"
    description: str = "中文说明：何时调用此工具。"
    parameters: dict = Field(default_factory=lambda: {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "查询内容"}
        },
        "required": ["query"],
    })
    plugin: object = None

    async def call(self, context: ContextWrapper[AstrAgentContext], **kwargs) -> ToolExecResult:
        return "结果文本"
```

在插件 `__init__` 中注册：

```python
def _register_llm_tools(self):
    try:
        tool = MyTool(plugin=self)
        self.context.add_llm_tools(tool)
        logger.debug("已注册 MyTool 到 LLM 工具列表")
    except Exception as e:
        logger.warning(f"注册 LLM 工具失败: {e}")
```

### LLM 工具安全建议

- 不要暴露任意文件读写、任意 URL 请求、任意 shell 执行，除非有严格权限与审批。
- 参数必须有限制：枚举、长度、数量、路径范围。
- 返回结构使用 JSON 字符串时要 `ensure_ascii=False`。
- 平台专属 API 先判断事件类型，例如 `AiocqhttpMessageEvent`。
- 高危工具可在 `@filter.on_llm_request()` 中动态控制可见性；`astrbot_plugin_hapi_connector` 使用了该钩子控制工具可见性。

## 七、后台任务、定时任务与生命周期

### 启动后台任务

`astrbot_plugin_daily_news/main.py`、`astrbot_plugin_github_cards/main.py` 都在初始化时启动定时任务：

```python
self._daily_task = asyncio.create_task(self.daily_task())
```

更稳健的写法：

```python
self._daily_task = asyncio.create_task(self._daily_loop())
self._daily_task.add_done_callback(self._on_task_done)

@staticmethod
def _on_task_done(task: asyncio.Task):
    if task.cancelled():
        return
    exc = task.exception()
    if exc:
        logger.error(f"后台任务异常退出: {exc}")
```

### terminate 必须 await 被取消任务

`astrbot_plugin_github_cards/main.py` 是好例子：

```python
async def terminate(self):
    if self.task:
        self.task.cancel()
        try:
            await self.task
        except asyncio.CancelledError:
            pass
```

如果任务里有 HTTP server、webhook、子进程，也要停止：

```python
if self.webhook_server:
    await self.webhook_server.stop()
```

### 防止并发 pipeline

手动命令和定时任务可能同时执行。使用锁：

```python
self._pipeline_lock = asyncio.Lock()

async def _core_pipeline(self, target_groups: list[str]):
    async with self._pipeline_lock:
        ...
```

## 八、数据目录、缓存与文件持久化

### 数据目录

运行时数据不要写进插件代码目录。优先使用插件数据目录：

```python
from astrbot.api.star import StarTools
from pathlib import Path

def _resolve_cache_dir(self) -> Path:
    try:
        data_dir = self.context.get_data_dir()
    except Exception:
        data_dir = StarTools.get_data_dir("astrbot_plugin_name")
    return Path(data_dir).expanduser().resolve() / "cache"
```

缓存目录启动时创建：

```python
self.cache_dir.mkdir(parents=True, exist_ok=True)
```

### JSON 状态文件

常见于订阅、默认仓库、用户设置：

```python
def _load_json(path: str, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        logger.error(f"加载数据失败: {e}")
        return default
```

写入建议使用临时文件 + 原子替换，避免进程中断导致 JSON 损坏。

## 九、网络请求与下载安全

任何访问外部 API 的插件都应设置超时、重试、大小限制、内容类型检查。

### HTTP 请求

```python
async with aiohttp.ClientSession(headers=headers) as session:
    async with session.get(url, timeout=aiohttp.ClientTimeout(total=15)) as resp:
        resp.raise_for_status()
        data = await resp.json(content_type=None)
```

### 下载文件必须流式写入

不要：

```python
dest.write_bytes(await resp.read())  # 可能一次性读爆内存
```

推荐：

```python
_MAX_IMAGE_BYTES = 20 * 1024 * 1024
_ALLOWED_IMAGE_CONTENT_TYPES = {"image/jpeg", "image/png", "image/webp", "image/gif"}

content_type = resp.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
if content_type not in _ALLOWED_IMAGE_CONTENT_TYPES:
    return False

total = 0
with tmp_path.open("wb") as f:
    async for chunk in resp.content.iter_chunked(64 * 1024):
        total += len(chunk)
        if total > _MAX_IMAGE_BYTES:
            return False
        f.write(chunk)
tmp_path.replace(dest)
```

### URL 白名单

```python
from urllib.parse import urlparse

_ALLOWED_IMAGE_HOSTS = {"i.pximg.net"}

def _is_allowed_image_url(url: str) -> bool:
    parsed = urlparse(url)
    return parsed.scheme == "https" and parsed.hostname in _ALLOWED_IMAGE_HOSTS
```

### 解析外部 JSON 用 `.get()`

不要直接 `data["contents"]`、`raw["url"]`。推荐：

```python
contents = data.get("contents")
if not isinstance(contents, list):
    logger.error("数据格式异常")
    return None

for raw in contents:
    if not isinstance(raw, dict):
        continue
    title = str(raw.get("title") or "Untitled")
    image_url = raw.get("url")
```

## 十、Web API / 插件页面

AstrBot 新版支持插件注册 Web API。`astrbot_plugin_livingmemory/core/page_api.py` 的模式：

```python
class PluginPageApi:
    def __init__(self, plugin) -> None:
        self.plugin = plugin

    def register_routes(self) -> None:
        register = self.plugin.context.register_web_api
        register(
            "/astrbot_plugin_livingmemory/page/stats",
            self.get_stats,
            ["GET"],
            "LivingMemory Page stats",
        )
```

主插件中要兼容旧版 AstrBot：

```python
def _register_official_page_api_if_available(self) -> None:
    if not hasattr(self.context, "register_web_api"):
        return
    try:
        self.page_api = PluginPageApi(self)
        self.page_api.register_routes()
    except Exception as exc:
        logger.warning(f"官方插件页面 API 注册失败: {exc}", exc_info=True)
```

Web API 安全要点：

- 管理 API 必须鉴权。
- 写操作只接受 `POST`。
- 校验输入类型、长度、路径范围。
- 不要把内部异常堆栈返回给用户。
- 如果启动独立 Web server，`terminate()` 中必须 stop。

## 十一、平台适配

某些能力只支持特定平台，例如 QQ 群成员 API 依赖 aiocqhttp：

```python
from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event import AiocqhttpMessageEvent

if not isinstance(event, AiocqhttpMessageEvent):
    return json.dumps({"error": f"此功能仅支持 aiocqhttp，当前平台为 {event.get_platform_name()}"}, ensure_ascii=False)
```

使用平台私有 API 前先判断：

```python
group_id = event.get_group_id()
if not group_id:
    yield event.plain_result("此指令仅在群聊中可用")
    return
```

## 十二、内容安全与合规

涉及图片、榜单、外链、下载的插件应默认保守：

- 成人内容默认跳过，配置项如 `allow_r18` 默认 `false`。
- 不确定是否安全的图片降级为链接或隐藏。
- 沙盒验证不是安全过滤的替代品；它仍会真实发送内容。
- 用户可触发的广播、群发、下载类命令必须加管理员权限或白名单。

例：R-18 默认跳过：

```python
is_r18 = bool(_R18_TAGS & {str(tag) for tag in tags})
if is_r18 and not self.allow_r18:
    logger.info(f"跳过第 {rank} 名 R-18 作品。")
    continue
```

## 十三、错误处理与日志

推荐模式：

```python
try:
    ...
except asyncio.CancelledError:
    raise
except Exception as e:
    logger.error(f"操作失败: {e}", exc_info=True)
    yield event.plain_result("操作失败，请查看日志")
```

注意：

- 后台循环中捕获异常后应 `await asyncio.sleep(...)`，避免快速重试刷日志。
- 关键路径使用 `exc_info=True`。
- 用户消息不要暴露 Token、Cookie、文件系统绝对路径、完整异常栈。

## 十四、发布前检查清单

### 功能检查

- [ ] `main.py` 可 `python3 -m py_compile`。
- [ ] `@register` 名称与目录名、`metadata.yaml` 一致。
- [ ] 命令名无冲突，别名合理。
- [ ] `_conf_schema.json` 包含新增配置项。
- [ ] README/帮助命令描述与实际命令一致。

### 生命周期检查

- [ ] 所有 `asyncio.create_task` 都能在 `terminate()` 中 cancel + await。
- [ ] 后台任务异常有 done callback 或日志。
- [ ] 子进程、web server、webhook、ClientSession 都能关闭。
- [ ] 手动触发和定时触发不会并发冲突，必要时用 `asyncio.Lock`。

### 安全检查

- [ ] 管理、广播、下载、外部操作命令有权限控制。
- [ ] 外部 URL 有域名白名单或严格校验。
- [ ] 下载有超时、大小限制、Content-Type 检查、流式写入。
- [ ] 用户输入路径不能逃逸插件数据目录。
- [ ] Token/Cookie/API Key 不写日志、不回显。
- [ ] 外部 JSON 使用 `.get()` 和类型检查。

### 数据检查

- [ ] 运行时数据写入插件数据目录，而不是代码目录。
- [ ] 缓存有清理策略。
- [ ] JSON/SQLite 写入有异常处理，必要时原子写。

## 十五、常见反模式

避免以下写法：

```python
# 1. 不校验配置
hour, minute = map(int, self.push_time.split(":"))

# 2. terminate 只 cancel 不 await
async def terminate(self):
    self._task.cancel()

# 3. 下载无限制，一次性读入内存
dest.write_bytes(await resp.read())

# 4. 直接信任外部 JSON
contents = data["contents"]
url = raw["url"]

# 5. 普通用户可触发群发/广播/下载
@filter.command("push_all")
async def push_all(self, event):
    await self._broadcast(...)

# 6. 运行时缓存写入插件源码目录
self.image_dir = Path(__file__).parent / "images"
```

对应修复：配置校验、cancel 后 await、流式下载、`.get()` + 类型检查、权限控制、使用插件数据目录。

## 十六、推荐开发流程

1. 先读目标插件的 `main.py`、`_conf_schema.json`、`metadata.yaml`。
2. 确认 AstrBot API 版本与已有插件导入路径。
3. 小改优先只动 `main.py`；配置新增必须同步 `_conf_schema.json`。
4. 涉及外部网络/文件/群发/后台任务时同时补安全边界。
5. 修改后运行：

```bash
python3 -m py_compile path/to/plugin/main.py
```

6. 如果 release 目录只是发布副本，记得同步到实际插件目录，例如：

```bash
cp astrbot_plugin_xxx/main.py ~/AstrBot/data/plugins/astrbot_plugin_xxx/main.py
cp astrbot_plugin_xxx/_conf_schema.json ~/AstrBot/data/plugins/astrbot_plugin_xxx/_conf_schema.json
```

7. 最后用 `git diff` 或 `diff -u` 检查实际改动是否只包含预期内容。
