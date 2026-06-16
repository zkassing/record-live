---
name: record-live
description: 多平台直播间录制。支持抖音、小红书。两种模式：(1) 直播间链接 → 立即录制；(2) 用户主页（仅抖音）→ 监听开播，开播自动录制、下播自动结束并继续监听。带本地 dashboard 网页（实时进度、书签、停止）。触发词："录制直播 / 录这个直播间 / 录抖音 / 录小红书 / 监听开播 / 蹲守开播 / record douyin live / record xiaohongshu"。
---

# record-live

多平台直播间录制 skill，全流程自动化。提供两种使用场景 × 两种 UI 模式：

**支持平台**：
- **抖音**：直播间链接、用户主页都能用，支持监听开播
- **小红书**：仅支持立即录制（`xiaohongshu.com/livestream/<room_id>` 链接），**不支持监听**——小红书用户主页 SSR 里没有直播状态字段，公开接口要签名，所以无法蹲守

**场景**：
- **立即录制**：现在就有人在播，给链接 → 直接拉流
- **监听开播**（仅抖音）：主播现在没播但你想录他下一场，给用户主页/sec_user_id → 后台轮询，开播自动录、下播自动结束并继续监听下一场

**UI**：
- **dashboard 模式**（默认）：launcher 拉起本地 HTTP server，浏览器里管理。两个 tab 分别管录制和监听
- **CLI 模式**（后备）：纯命令行，仅支持立即录制

平台逻辑都收敛在 `scripts/platforms/` 下的 module 里，新增平台只要实现 `Platform` 接口（参见 `platforms/base.py`）。

## 何时触发

用户说出下列任意意图：

**立即录制（抖音）**：
- "录制 X 这个直播间" / "录这个直播" / "帮我把这个抖音直播录下来"
- 给出 `https://live.douyin.com/<web_rid>` 链接或纯 web_rid
- "拉这个直播的流" / "record douyin live"

**立即录制（小红书）**：
- "录这个小红书直播" / "录红书直播" / "把这条小红书直播录下来"
- 给出 `https://www.xiaohongshu.com/livestream/<room_id>` 链接
- xhslink.com 短链
- "record xiaohongshu live"

**监听开播（仅抖音）**：
- "监听 X 开播" / "蹲守 X" / "等 X 开播自动录"
- 给出抖音用户主页链接（`https://www.douyin.com/user/MS4wLjABAAAA...`）或 sec_user_id 字符串
- 用户提到"小红书监听" → 直接告诉用户**小红书不支持监听**，让用户在主播开播后给直播间链接

如果用户只是想"看直播间数据 / 监控开播状态"且不要录制，那不在本 skill 范围内。

## 用户需要给什么

按平台和场景对应：

| 平台 | 场景 | 期望输入 |
|---|---|---|
| 抖音 | 立即录制 | `https://live.douyin.com/123456789` 或纯 web_rid |
| 抖音 | 监听 | 抖音号 / 主页链接 / sec_user_id（任一） |
| 小红书 | 立即录制 | `https://www.xiaohongshu.com/livestream/<room_id>` 或 xhslink 短链 |
| 小红书 | 监听 | **不支持** |

抖音监听细节：
- **抖音号**（unique_id，如 `Yseparate` / `insta360_china`）：个人主播账号通常可用，机构/官方号常因反爬抓不到
- **用户主页链接**：任何账号都可用，最稳
- **sec_user_id**（以 `MS4wLjABAAAA` 开头）：直接给也行
- 三种输入对应同一个用户，skill 内部会自动转换。**抖音号失败时回退到主页链接**

可选（所有场景都适用）：
- 输出目录（默认是**用户当前工作目录**——`launcher.py` 调用时所在的 shell `cwd`）
- 录制画质偏好（抖音默认 `ORIGIN`，小红书默认 `HD`）
- 监听检查频率（仅监听场景，默认 30s）

> **关于输出目录**：launcher 把 spawn 时刻的 `cwd` 锁进 server，浏览器表单里"开始录制"按钮提交的任务也会落到那个目录。**想换目录？两种办法**：
> 1. 当次录制：launcher 加 `--output-dir /path/to/somewhere`
> 2. 永久换：先杀 server（`pkill -f scripts/server.py`），`cd` 到新目录，再跑 launcher

## 执行步骤

按下面的顺序执行，**不要跳步**：

### 1. 确认输入并判断平台 + 模式

如果用户没给链接，问一句，不要瞎猜。

**平台嗅探**（看 URL 即可，skill 内部也做同样判断）：
- 含 `live.douyin.com` / `douyin.com/user/` / `MS4wLjABAAAA` / 抖音号格式 → **抖音**
- 含 `xiaohongshu.com` / `xhslink.com` → **小红书**

**模式判断**（仅抖音相关）：
- 输入是 `live.douyin.com/<digits>` 或纯数字 → **立即录制**
- 输入是 `douyin.com/user/MS4wLjABAAAA...` 或 `MS4wLjABAAAA...` 字符串 → **监听开播**
- 输入是抖音号（unique_id）→ 优先按监听处理；用户原话说"录这个直播间"且当前在播也可以走立即录制
- 用户原话明确说"监听" / "蹲守" / "开播再录" → 走监听模式
- **抖音号解析失败时**（机构号/官方号常见，会报 "未返回 sec_user_id"）→ 让用户去 ta 主页 URL 复制 `MS4wLjABAAAA…` 那段重试

**小红书**：
- 只有立即录制一种模式
- 用户问"能不能监听小红书的 X"→ 直接说不行，让 ta 在主播开播后给直播间链接

### 2. 检查环境（Python + ffmpeg）

启动 skill 前先一次性确认两个依赖：

```bash
# Python 3.8+
python3 -c 'import sys; print("python", sys.version.split()[0]); sys.exit(0 if sys.version_info >= (3, 8) else 1)' 2>&1

# ffmpeg
if command -v ffmpeg &> /dev/null; then
    ffmpeg -version | head -n 1
else
    echo "FFmpeg 未安装"
fi
```

**两条都通过** → 跳到第 3 步。否则按下面的指引装：

#### Python 3.8+

skill 用 Python 标准库 + ffmpeg 二进制实现，**不依赖任何第三方 pip 包**，所以不用建虚拟环境。要的就只是一个 ≥3.8 的解释器。

| 平台 | 检查 / 安装 |
|---|---|
| **macOS** | 系统自带的 `/usr/bin/python3` 通常 ≥3.9（Big Sur 起）。版本不够时：有 brew 直接代跑 `brew install python`；没 brew 让用户先按 ffmpeg 那段装 brew |
| **Linux** | Debian/Ubuntu: `sudo apt install -y python3`（一般已装）；CentOS/RHEL: `sudo yum install -y python3`。让用户自己跑（涉及 sudo） |
| **Windows** | 让用户去 https://www.python.org/downloads/ 下安装包，**勾选 "Add python.exe to PATH"** 那个选项，装完开新终端验证 |

如果 launcher.py 自己跑起来，发现版本太低，会直接报：
```
[X] launcher.py 需要 Python 3.8+
   当前: 3.7.x ...
```
跟着提示走就行。

#### ffmpeg

参考自 [ffmpeg-install](https://www.skills.sh/chunpu/agent-skills/ffmpeg-install)。

> **总原则**：所有需要 sudo / 管理员权限 / 交互确认的命令，**不要在 skill 里直接跑**——非交互 shell 拿不到密码，会卡住或失败。把命令贴出来让用户自己执行；不需要权限的（比如已有 brew 的 macOS）才直接代跑。

##### macOS

先看 brew 在不在：

```bash
command -v brew
```

- **brew 已安装** → 直接代跑：
  ```bash
  brew install ffmpeg
  ```
- **brew 未安装** → 让用户自己装 Homebrew（涉及 sudo，**skill 不要代跑**）：
  ```bash
  /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
  ```

##### Linux

让用户自己跑（涉及 sudo）：

| 发行版 | 命令 |
|---|---|
| Ubuntu/Debian | `sudo apt update && sudo apt install -y ffmpeg` |
| CentOS/RHEL | `sudo yum install -y epel-release && sudo yum install -y ffmpeg ffmpeg-devel` |
| Fedora | `sudo dnf install -y ffmpeg` |
| Arch | `sudo pacman -S ffmpeg` |

##### Windows

给用户三条路，**skill 都不代跑**：

1. **Chocolatey**：管理员 PowerShell：`choco install ffmpeg`
2. **Scoop**（无需管理员）：`scoop bucket add extras && scoop install ffmpeg`
3. **手动**：从 https://www.gyan.dev/ffmpeg/builds/ 下 release 包，解压后把 `bin` 目录加到系统 PATH，**重启终端**让 PATH 生效

### 3. 启动 dashboard 并下发任务

**默认走 dashboard 模式**——浏览器里管理远比终端友好。

直接调 launcher（前台运行，几秒就返回；不要用 `run_in_background`）：

#### 立即录制（任意平台）

```bash
python3 ~/.claude/skills/record-live/scripts/launcher.py "<链接>"
```

抖音示例：
```bash
python3 ~/.claude/skills/record-live/scripts/launcher.py "https://live.douyin.com/123456789"
```

小红书示例：
```bash
python3 ~/.claude/skills/record-live/scripts/launcher.py "https://www.xiaohongshu.com/livestream/570321613446566013"
```

#### 监听开播（仅抖音）

加 `--watch` 标志：
```bash
python3 ~/.claude/skills/record-live/scripts/launcher.py --watch "<用户主页链接或 sec_user_id>"
```

#### 可选参数

- `--platform <douyin|xiaohongshu>` 强制指定平台。不传时按 URL 嗅探（一般不用传）
- `--quality <画质标签>` 抖音用 `ORIGIN/FULL_HD1/UHD/HD1/HD/SD1/SD/SD2/LD`；小红书用 `HD/SD/LD`
- `--port <端口号>` 默认 8765；被占了会自动 +1 找空闲端口
- `--no-browser` 不自动开浏览器
- `--interval <秒>` 仅 `--watch` 用，默认 30，最小 10
- `--output-dir <路径>` 自定义输出目录；不传时**默认用户当前工作目录**

⚠ **server 的输出目录是首次启动时锁定的**。如果 server 已经在跑（dashboard 复用），launcher 会提示你"server 默认目录是 X，本次提交会落到 Y"。dashboard 表单里点"开始录制"按钮的任务**总是落到 X**。要让浏览器表单也用新目录，得先 `pkill -f scripts/server.py` 再重启。

启动完成后告诉用户：
- dashboard URL（终端会打印）
- 浏览器里两个 tab：「立即录制」「监听开播」
- 立即录制 tab 顶部能选平台（默认自动识别），输入框接受任何已注册平台的链接
- 监听 tab 仅对抖音可用（小红书不支持时下拉为空 + 提交按钮禁用）
- 关掉浏览器不会停录制/监听；server 后台一直跑，可随时回来打开 URL

### 4. 终端 / 后备模式（仅当用户明确不要 dashboard）

如果用户说"不要网页，就在终端跑"，回退到纯 CLI 模式（**仅支持立即录制**）：

```bash
python3 ~/.claude/skills/record-live/scripts/record_douyin.py "<target>" &
```

> 文件名仍叫 `record_douyin.py` 是为了向后兼容，内部已经改用平台抽象层，所以传小红书链接也能跑。

区分场景：

| 场景 | 选谁 |
|---|---|
| 用户没特别说 | **dashboard**（默认） |
| 用户明说"不开网页" / "终端就行" | CLI（仅立即录制） |
| 监听开播 | **必须** dashboard |
| 在没有图形界面的远程服务器上跑（SSH） | CLI |

### 5. 等待并汇报

dashboard 模式下，剩下的事情都在浏览器里：
- 用户问"录多久了" / "录到了吗" → 让用户看浏览器，或调 `GET http://127.0.0.1:<port>/api/tasks` / `GET /api/watchers` 拿状态读给用户
- 用户说"停止录制" → 让用户点页面里"停止录制"按钮，或 `POST /api/tasks/<id>/stop`
- 用户说"加个书签" → 让用户点页面里"添加书签"按钮
- 用户说"取消监听 X" → 让用户点页面里"删除监听"按钮，或 `DELETE /api/watchers/<id>`

server 持续运行，agent 退出不会杀它。下次再触发本 skill 会自动复用同一个 server。

## 注意事项

- 不要在录制中途主动 kill 脚本，让它自然结束或让用户主动停。
- 如果用户家里抖音 cookie 失效（极少见），脚本会报 `stream_url_missing`，让用户去浏览器里复制一份 cookie 走 `--cookie` 传进来。
- 小红书匿名访问已能拿到拉流地址（用 iOS App UA + xy-common-params 头），通常不需要 cookie。
- 默认输出目录如果不存在脚本会自动创建。
- 同一个直播间重复录制会生成不同时间戳的文件，不会覆盖。文件名带平台前缀（`douyin_xxx_<时间>.mp4` / `xiaohongshu_xxx_<时间>.mp4`）。

## 错误处理

| 报错 | 处理 |
|---|---|
| `room not live` / `该直播间当前未开播` | 直播间没开播。如果用户其实想"等开播再录"，建议改用监听模式（仅抖音） |
| `stream_url_missing` / `接口未返回拉流地址` | 接口没返回拉流地址，建议用户在浏览器打开直播间确认能播放，或换 cookie |
| `ffmpeg not found` | 走第 2 步给用户装好（macOS/Linux 自动装，Windows 让用户手动） |
| `[X] launcher.py 需要 Python 3.8+` | 用户 Python 太老。走第 2 步装一个 ≥3.8 的 |
| `python3: command not found` | 用户根本没装 Python。走第 2 步 |
| `无法识别输入所属平台` | 链接格式不对或新平台没注册。让用户检查输入 |
| `平台 'xiaohongshu' 不支持监听开播` | 小红书没法蹲守，让用户开播后给直播间链接 |
| `监听任务需要一个'用户'输入` | 用户给的不是用户主页/id 而是直播间链接。让用户检查输入 |
| `通过抖音号解析失败` | 八成是机构号/官方号反爬，让用户改用主页 URL（含 MS4wLjABAAAA…）重试 |
| `无法解析用户信息` | sec_user_id 错了或被抖音抹了。让用户重新复制 |
| `未在小红书直播间页面找到 __INITIAL_STATE__` | 小红书改了页面结构或 UA 被屏蔽，需要更新 `platforms/xiaohongshu.py` |
| 监听一直在 idle 但主播明明在播 | 可能 cookie 失效了。让用户在 dashboard 删了重建，或登录抖音网页拿一份新 cookie |
| 网络超时 | 脚本会自动重试 3 次，仍失败就退出 |

## 给开发者：怎么加新平台

1. 在 `scripts/platforms/` 下加一个 module（参考 `xiaohongshu.py`）
2. 实现 `matches(raw)` / `resolve(raw, cookie)` / `fetch_live_info(room_id, cookie)`，可选 `fetch_user_profile`
3. 在 module 末尾导出 `PLATFORM = Platform(...)`
4. 在 `platforms/__init__.py` 的 `PLATFORMS` 列表里加一条
5. dashboard 会自动通过 `/api/platforms` 拉到新平台并填充选择器

平台间共用的 ffmpeg 录制循环 / FLV→MP4 转换都在 `record_core.py`，新平台只要返回正确的 `stream_url + stream_format` 即可。
