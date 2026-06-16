# record-live

多平台直播间录制 Claude Code skill。给一条直播间链接，自动拉流、转 MP4、本地 dashboard 实时显示进度。

> 这是 Claude Code 的 [agent skill](https://docs.anthropic.com/en/docs/claude-code/agent-skills)。`SKILL.md` 是给 AI 看的指令；`README.md` 这份是给人看的。

## 支持的平台

| 平台 | 立即录制 | 监听开播（蹲守） |
|---|---|---|
| 抖音 | ✅ 直播间链接 / web_rid | ✅ 主页 / sec_user_id / 抖音号 |
| 小红书 | ✅ `xiaohongshu.com/livestream/<id>` 或 xhslink 短链 | ❌ [见下](#为什么小红书不支持监听) |

## 快速使用

### 通过 Claude Code（推荐）

直接对 Claude 说：

```
录这个抖音直播：https://live.douyin.com/123456789
```
```
录这个小红书：https://www.xiaohongshu.com/livestream/570321613446566013
```
```
监听 Yseparate 开播
```

Claude 会自动调起 launcher，开浏览器，dashboard 实时显示进度。

### 不经过 Claude，直接命令行

skill 是个独立 Python 脚本（无第三方依赖），手动跑也完全 OK。

```bash
# 默认 dashboard 模式（开浏览器）
python3 ~/.claude/skills/record-live/scripts/launcher.py \
  "https://www.xiaohongshu.com/livestream/570321613446566013"

# 监听抖音主播开播
python3 ~/.claude/skills/record-live/scripts/launcher.py \
  --watch "https://www.douyin.com/user/MS4wLjABAAAA..."

# 纯 CLI 模式（不开网页，仅立即录制）
python3 ~/.claude/skills/record-live/scripts/record_douyin.py \
  "https://live.douyin.com/123456789"
```

#### 常用参数

| 参数 | 说明 |
|---|---|
| `--platform <id>` | 强制指定平台（`douyin` / `xiaohongshu`），不传按 URL 嗅探 |
| `--quality <tag>` | 抖音：`ORIGIN`/`FULL_HD1`/`UHD`/`HD1`/`HD`/`SD1`/`SD`/`SD2`/`LD`；小红书：`HD`/`SD`/`LD` |
| `--port <num>` | dashboard 端口，默认 8765（被占自动 +1） |
| `--no-browser` | 不自动打开浏览器 |
| `--interval <sec>` | 监听场景下检查频率，默认 30s，最小 10s |
| `--output-dir <path>` | 输出目录，不传用当前 `cwd` |

> ⚠ **server 的输出目录是首次启动时锁定的**。dashboard 表单提交的任务总是落到那个目录。要换默认目录，先 `pkill -f scripts/server.py` 再用新 cwd 重启。

## 依赖

- **Python 3.8+**（标准库就够，不需要装 pip 包）
- **ffmpeg**（macOS `brew install ffmpeg` / Linux `apt install ffmpeg` / Windows `choco install ffmpeg`）

## 架构

```
record-live/
├── SKILL.md                       # 给 Claude 的指令
├── README.md                      # 这份（给人看的）
└── scripts/
    ├── launcher.py                # 入口：拉起 server + 提交任务
    ├── server.py                  # 本地 HTTP server，提供 dashboard + REST API + SSE
    ├── dashboard.html             # 浏览器端 UI（纯静态）
    ├── record_core.py             # 平台无关的录制循环（ffmpeg spawn / 进度 / 转 MP4）
    ├── record_douyin.py           # 兼容老命令行入口的 CLI shim
    └── platforms/
        ├── __init__.py            # 平台注册表 + URL 嗅探
        ├── base.py                # Platform dataclass（抽象接口）
        ├── douyin.py              # 抖音实现
        └── xiaohongshu.py         # 小红书实现
```

**数据流**（立即录制）：

```
用户输入
  → launcher.py POST /api/tasks
  → server.TaskManager.create_task()
  → 起线程跑 record_core.record_room()
      → platform.resolve(target)        # 解析输入 → room_id
      → platform.fetch_live_info(...)   # 拿 stream_url + 元信息
      → ffmpeg -i stream_url → out.flv
      → ffmpeg -i out.flv → out.mp4（结束/停止后转换，删 flv）
  → SSE /api/events 实时推进度给 dashboard
```

**数据流**（监听开播，仅抖音）：

```
launcher.py POST /api/watchers
  → WatcherManager.create()
  → 起线程跑 _run() 循环：
      每 interval 秒 → platform.fetch_user_profile()
        ├─ 在播 + 当前没在录 → 嫁接一条 task 进 TaskManager
        ├─ 在播 + 已在录 → noop
        └─ 没在播 → 等下一轮
```

## 怎么加新平台

平台插件式设计，加新平台不用动核心代码。3 步：

### 1. 写 module

`scripts/platforms/yourplatform.py`：

```python
from .base import Platform

def matches(raw: str) -> bool:
    """根据用户输入判断是不是你这平台。"""
    return "yourplatform.com" in raw

def resolve(raw: str, cookie: str) -> tuple[str | None, dict]:
    """把任意输入解析成 (room_id, profile)。"""
    return room_id, {}

def fetch_live_info(room_id: str, cookie: str) -> dict:
    """抓直播间信息，必须返回这个 dict 结构。"""
    return {
        "is_live": True,
        "title": "直播标题",
        "nickname": "主播昵称",
        "stream_url": "http://.../live.flv",  # ffmpeg 拉流地址
        "stream_format": "flv",                # "flv" | "m3u8"
        "quality": "HD",
        "qualities": ["HD", "SD"],
        "stream_map": {"HD": "...", "SD": "..."},
        "room_id": room_id,
    }

# 可选：如果支持监听开播
def fetch_user_profile(user_id: str, cookie: str) -> dict:
    """返回 {is_live, room_id, nickname, avatar, ...}。"""
    ...

PLATFORM = Platform(
    id="yourplatform",
    display_name="某平台",
    matches=matches,
    resolve=resolve,
    fetch_live_info=fetch_live_info,
    supports_watcher=False,           # 实现了 fetch_user_profile 才设 True
    fetch_user_profile=None,
    default_cookie="",
    input_examples=["https://yourplatform.com/live/123"],
    ffmpeg_referer="https://yourplatform.com/",
    ffmpeg_user_agent="...",          # ffmpeg 拉流用的 UA
)
```

### 2. 注册

`scripts/platforms/__init__.py` 的 `PLATFORMS` 列表加一条：

```python
from . import douyin, xiaohongshu, yourplatform

PLATFORMS: list[Platform] = [
    douyin.PLATFORM,
    xiaohongshu.PLATFORM,
    yourplatform.PLATFORM,   # ← 加这行
]
```

### 3. 完事

dashboard 重启后会自动通过 `/api/platforms` 拉到新平台，前端选择器会自动出现。不用改 server.py、launcher.py、dashboard.html。

平台徽章想要自定义颜色，在 `dashboard.html` 的 CSS 里加一条：

```css
.badge.platform-yourplatform { background: ...; color: ...; }
```

## 为什么小红书不支持监听

监听开播 = 给一个用户 ID，不停轮询查"他现在在播吗"。

**抖音**：用户主页接口 `aweme/v1/web/user/profile/other/` 的响应里 `room_data.status == 2` 直接告诉你在播 + `web_rid` 让你立刻去拉流。匿名 cookie 就够。

**小红书**：用户主页（`xiaohongshu.com/user/profile/<id>`）的 SSR HTML 里完全没有任何直播状态字段（试过 `liveLink`/`liveStatus`/`isLive`/`is_live`/`live_link`/`liveInfo` 都 0 命中）。公开的 `check_live_status` / `host_status` 接口要么返回 `create invoker failed` 要么 404，需要登录态 + X-S/X-T 签名才能调。

要做监听就得逆向签名算法，工程量不在这个 skill 的合理范围内。妥协是：**主播开播后你直接给直播间链接，立即录制就行**。

## 故障排查

| 现象 | 处理 |
|---|---|
| `room not live` | 直播间没开播。如果是抖音，改用监听模式 `--watch` |
| `stream_url_missing` | 接口没返回拉流地址。浏览器登录后用 DevTools 复制 cookie 走 `--cookie` 重试 |
| `ffmpeg not found` | 装 ffmpeg：macOS `brew install ffmpeg`，Linux `apt install ffmpeg` |
| `[X] launcher.py 需要 Python 3.8+` | Python 太老，装个新的 |
| `无法识别输入所属平台` | URL 格式不对，或这平台没注册 |
| `通过抖音号解析失败` | 八成是机构号反爬，改用主页 URL（含 `MS4wLjABAAAA…`）重试 |
| `未在小红书直播间页面找到 __INITIAL_STATE__` | 小红书改了页面结构或 UA 被屏蔽，需要更新 `platforms/xiaohongshu.py` |
| 监听一直 idle 但主播明明在播 | cookie 失效。在 dashboard 删了重建，或换一份新 cookie 重启 server |
| dashboard 一直显示老数据 | 可能旧 server 还在跑：`pkill -f scripts/server.py` 后重启 |
| 同一台机有多个 server | launcher 默认 8765，被占自动 +1。多 server 互不干扰，但 dashboard URL 不同 |

server 日志：`~/.cache/record-live/server.log`

## 文件命名

录制输出文件名为：

```
<platform>_<sanitized_title>_<YYYYMMDD_HHMMSS>.mp4
```

比如：
```
douyin_某主播开播啦_20260616_113722.mp4
xiaohongshu_全国汽车托运3秒查价_20260616_113722.mp4
```

## 致谢

小红书的拉流地址解析路径（iOS App UA + `xy-common-params` header + 解析 SSR 里的 `liveStream.roomData.roomInfo.pullConfig`）参考自 [ihmily/DouyinLiveRecorder](https://github.com/ihmily/DouyinLiveRecorder/blob/main/src/spider.py) 的 `get_xhs_stream_url`。

## License

跟着你的 skills 仓库走。
