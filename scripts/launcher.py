#!/usr/bin/env python3
"""skill 入口：拉起 server，自动开浏览器，可选直接下发一个录制 / 监听任务。

支持平台由 platforms 注册表决定。target 平台默认按 URL 嗅探，
也可用 --platform 显式指定。

用法：
  launcher.py                                       # 只开 dashboard
  launcher.py "https://live.douyin.com/123"          # 抖音立即录制
  launcher.py "https://www.xiaohongshu.com/livestream/570321613446566013"  # 小红书立即录制
  launcher.py --watch "https://www.douyin.com/user/MS4wLj..."   # 抖音监听开播
"""
import sys

# 在跑任何业务代码前先验环境
if sys.version_info < (3, 8):
    sys.stderr.write(
        "[X] launcher.py 需要 Python 3.8+\n"
        "   当前: %s (%s)\n"
        "   macOS 装新版: brew install python\n"
        "   或用 conda/pyenv 装一个 >=3.8 的环境后重试。\n"
        % (sys.version.split()[0], sys.executable)
    )
    sys.exit(1)

import argparse
import json
import socket
import subprocess
import time
import urllib.error
import urllib.request
import webbrowser
from pathlib import Path
from typing import Optional

DEFAULT_PORT = 8765
HOST = "127.0.0.1"

SCRIPTS_DIR = Path(__file__).parent
SERVER_SCRIPT = SCRIPTS_DIR / "server.py"

LOG_DIR = Path.home() / ".cache" / "record-live"
LOG_DIR.mkdir(parents=True, exist_ok=True)
SERVER_LOG = LOG_DIR / "server.log"
PID_FILE = LOG_DIR / "server.pid"


def is_our_server(port: int) -> bool:
    """检查端口上跑的是不是我们这个 server。"""
    try:
        with urllib.request.urlopen(f"http://{HOST}:{port}/api/tasks", timeout=1.5) as resp:
            if resp.status != 200:
                return False
            data = json.loads(resp.read().decode("utf-8"))
            return isinstance(data, dict) and "tasks" in data
    except (urllib.error.URLError, TimeoutError, ConnectionError, ValueError):
        return False


def port_in_use(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        try:
            s.bind((HOST, port))
            return False
        except OSError:
            return True


def pick_port(preferred: int) -> int:
    if is_our_server(preferred):
        return preferred
    if not port_in_use(preferred):
        return preferred
    for offset in range(1, 20):
        candidate = preferred + offset
        if is_our_server(candidate):
            return candidate
        if not port_in_use(candidate):
            return candidate
    raise RuntimeError(f"找不到可用端口（{preferred}-{preferred+19}）。")


def spawn_server(port: int, output_dir: str) -> int:
    log_fd = open(SERVER_LOG, "ab", buffering=0)
    proc = subprocess.Popen(
        [sys.executable, str(SERVER_SCRIPT),
         "--port", str(port), "--no-browser",
         "--output-dir", output_dir],
        stdin=subprocess.DEVNULL,
        stdout=log_fd,
        stderr=log_fd,
        start_new_session=True,
    )
    PID_FILE.write_text(str(proc.pid))
    return proc.pid


def wait_until_ready(port: int, timeout: float = 8.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if is_our_server(port):
            return True
        time.sleep(0.2)
    return False


def fetch_platforms(port: int) -> list:
    try:
        with urllib.request.urlopen(f"http://{HOST}:{port}/api/platforms", timeout=2) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            return data.get("platforms") or []
    except Exception:
        return []


def submit_task(port: int, target: str, platform_id: Optional[str],
                quality: Optional[str], output_dir: Optional[str] = None) -> dict:
    body = {"target": target}
    if platform_id:
        body["platform"] = platform_id
    if quality:
        body["quality"] = quality
    if output_dir:
        body["output_dir"] = output_dir
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(
        f"http://{HOST}:{port}/api/tasks",
        data=data,
        method="POST",
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.loads(resp.read().decode("utf-8"))


def submit_watcher(port: int, target: str, platform_id: Optional[str],
                   interval: int, quality: Optional[str],
                   output_dir: Optional[str] = None) -> dict:
    body = {"target": target, "interval": interval}
    if platform_id:
        body["platform"] = platform_id
    if quality:
        body["quality"] = quality
    if output_dir:
        body["output_dir"] = output_dir
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(
        f"http://{HOST}:{port}/api/watchers",
        data=data,
        method="POST",
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.loads(resp.read().decode("utf-8"))


def main() -> int:
    p = argparse.ArgumentParser(description="启动多平台直播录制 dashboard，并可选立即下发任务")
    p.add_argument("target", nargs="?", default=None,
                   help="直播间链接 / 用户主页链接 / 平台 id 字符串")
    p.add_argument("--platform", default=None,
                   help="指定平台 id（douyin / xiaohongshu），不指定时按 target 嗅探")
    p.add_argument("--watch", action="store_true",
                   help="监听用户开播（仅支持的平台），而不是当场录制")
    p.add_argument("--interval", type=int, default=30,
                   help="--watch 模式的检查间隔秒数（默认 30s）")
    p.add_argument("--quality", default=None)
    p.add_argument("--port", type=int, default=DEFAULT_PORT)
    p.add_argument("--no-browser", action="store_true")
    p.add_argument("--output-dir", default=None,
                   help="录制输出目录，默认是当前工作目录")
    args = p.parse_args()

    import os as _os
    output_dir = str(Path(args.output_dir).expanduser().resolve()) if args.output_dir else _os.getcwd()

    port = pick_port(args.port)
    url = f"http://{HOST}:{port}/"

    if is_our_server(port):
        print(f"✓ dashboard 已经在跑: {url}")
        try:
            with urllib.request.urlopen(f"http://{HOST}:{port}/api/config", timeout=2) as resp:
                cfg = json.loads(resp.read().decode("utf-8"))
            server_dir = cfg.get("default_output_dir", "?")
            if server_dir != output_dir:
                print(f"  ⚠ server 默认目录是 {server_dir}（不是当前目录 {output_dir}）。")
                print(f"     本次提交会落到 {output_dir}；浏览器表单提交仍落到 server 默认目录。")
                print(f"     要换默认目录请先 kill server：pkill -f scripts/server.py")
        except Exception:
            pass
    else:
        pid = spawn_server(port, output_dir)
        print(f"⏳ 启动 dashboard server (pid={pid}, port={port}, log={SERVER_LOG})...")
        if not wait_until_ready(port):
            print(f"❌ server 启动超时。检查日志: {SERVER_LOG}")
            return 1
        print(f"✓ dashboard 已就绪: {url}")
        print(f"📁 默认输出目录: {output_dir}")
        plats = fetch_platforms(port)
        if plats:
            print(f"🎬 支持平台: {', '.join(p['display_name'] for p in plats)}")

    if args.target:
        try:
            if args.watch:
                data = submit_watcher(port, args.target, args.platform,
                                      args.interval, args.quality, output_dir)
                w = data.get("watcher") or {}
                nick = w.get("nickname") or "(待解析)"
                pname = w.get("platform_display_name") or w.get("platform") or "?"
                print(f"✓ 已添加监听 [{pname}] (id={w.get('id', '?')}): {nick}，"
                      f"每 {w.get('interval', '?')}s 检查一次")
            else:
                data = submit_task(port, args.target, args.platform, args.quality, output_dir)
                task = data.get("task") or {}
                pname = task.get("platform_display_name") or task.get("platform") or "?"
                print(f"✓ 已提交录制任务 [{pname}] (id={task.get('id', '?')}): {args.target}")
        except urllib.error.HTTPError as e:
            try:
                err = json.loads(e.read().decode("utf-8"))
                print(f"❌ 提交失败: {err.get('error', e)}")
            except Exception:
                print(f"❌ 提交失败: HTTP {e.code}")
            return 1
        except Exception as e:
            print(f"❌ 提交失败: {e}")
            return 1

    if not args.no_browser:
        try:
            webbrowser.open(url)
        except Exception:
            pass

    if args.watch:
        print(f"\n👉 打开 {url} 管理监听（'监听开播' tab）")
    else:
        print(f"\n👉 打开 {url} 管理录制任务（添加书签、停止录制、查看进度）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
