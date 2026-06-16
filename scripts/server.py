#!/usr/bin/env python3
"""录制任务的本地 dashboard server（多平台版）。

提供：
- GET  /                          → dashboard.html
- GET  /api/tasks                 → 当前所有任务的状态
- GET  /api/watchers              → 监听器列表
- GET  /api/platforms             → 已注册的平台列表（id / display_name / supports_watcher）
- GET  /api/config                → 默认输出目录等
- GET  /api/events                → SSE 事件流
- POST /api/tasks                 → 提交录制 {target, platform?, quality?, output_dir?}
- POST /api/tasks/<id>/stop       → 停止某任务
- POST /api/tasks/<id>/bookmark   → 加书签 {note}
- POST /api/watchers              → 添加监听 {target, platform?, interval?, quality?, output_dir?}
- POST /api/watchers/<id>/check-now → 立即触发一次检查
- DELETE /api/watchers/<id>       → 删除监听

只用标准库，单进程多线程；同进程内 spawn record_room() 线程，状态保存在内存里。
"""
from __future__ import annotations

import http.server
import itertools
import json
import queue
import socketserver
import sys
import threading
import time
import uuid
import webbrowser
from pathlib import Path
from typing import Optional

# 让 record_core / platforms 可被 import
sys.path.insert(0, str(Path(__file__).parent))
import platforms  # noqa: E402
import record_core  # noqa: E402
from platforms.base import Platform  # noqa: E402

DASHBOARD_HTML = Path(__file__).parent / "dashboard.html"

DEFAULT_PORT = 8765
HOST = "127.0.0.1"

# 默认输出目录：跟随 server 启动时的 CWD（launcher 会通过 --output-dir 覆盖）
DEFAULT_OUTPUT_DIR = str(Path.cwd())


def _resolve_platform(target: str, platform_id: Optional[str]) -> Platform:
    """根据请求里的 platform 字段（可选）+ target 选定平台。

    platform_id 给定：必须是已注册的；自动嗅探作为 fallback。
    platform_id 为空：纯靠嗅探。
    """
    if platform_id:
        p = platforms.get_platform(platform_id)
        if p is None:
            raise ValueError(
                f"未知平台 {platform_id!r}，已支持: {[x.id for x in platforms.PLATFORMS]}"
            )
        return p
    p = platforms.detect_platform(target)
    if p is None:
        raise ValueError(
            f"无法识别输入所属平台: {target!r}。"
            f"支持的平台: {[x.display_name for x in platforms.PLATFORMS]}"
        )
    return p


class Task:
    """一个录制任务的状态。"""

    def __init__(self, task_id: str, target: str, platform: Platform,
                 quality: Optional[str], output_dir: str):
        self.id = task_id
        self.target = target
        self.platform = platform  # Platform 实例
        self.quality_pref = quality
        self.output_dir = output_dir

        self.status = "starting"  # starting / recording / converting / done / error / stopped
        self.created_at = time.time()
        self.started_at: Optional[float] = None
        self.ended_at: Optional[float] = None

        self.nickname: Optional[str] = None
        self.title: Optional[str] = None
        self.quality: Optional[str] = None
        self.room_id: Optional[str] = None
        self.output_path: Optional[str] = None
        self.mp4_path: Optional[str] = None

        self.progress: dict = {"time": "00:00:00", "size": "0KB", "bitrate": "0kbits/s"}
        self.bookmarks: list[dict] = []
        self.error: Optional[str] = None
        self.stop_event = threading.Event()

    def snapshot(self) -> dict:
        return {
            "id": self.id,
            "target": self.target,
            "platform": self.platform.id,
            "platform_display_name": self.platform.display_name,
            "status": self.status,
            "created_at": self.created_at,
            "started_at": self.started_at,
            "ended_at": self.ended_at,
            "nickname": self.nickname,
            "title": self.title,
            "quality": self.quality,
            "quality_pref": self.quality_pref,
            "room_id": self.room_id,
            # web_rid 历史字段，dashboard 旧 JS 还在用 → 别名
            "web_rid": self.room_id,
            "output_path": self.output_path,
            "mp4_path": self.mp4_path,
            "output_dir": self.output_dir,
            "progress": self.progress,
            "bookmarks": self.bookmarks,
            "error": self.error,
        }


class TaskManager:
    def __init__(self):
        self.tasks: dict[str, Task] = {}
        self.lock = threading.Lock()
        self.subscribers: list[queue.Queue] = []
        self.sub_lock = threading.Lock()

    # ---------- 订阅 ----------
    def subscribe(self) -> queue.Queue:
        q: queue.Queue = queue.Queue(maxsize=200)
        with self.sub_lock:
            self.subscribers.append(q)
        return q

    def unsubscribe(self, q: queue.Queue) -> None:
        with self.sub_lock:
            if q in self.subscribers:
                self.subscribers.remove(q)

    def broadcast(self, event: dict) -> None:
        with self.sub_lock:
            dead = []
            for q in self.subscribers:
                try:
                    q.put_nowait(event)
                except queue.Full:
                    dead.append(q)
            for q in dead:
                self.subscribers.remove(q)

    # ---------- 任务 ----------
    def create_task(self, target: str, platform: Platform,
                    quality: Optional[str], output_dir: str) -> Task:
        task_id = uuid.uuid4().hex[:8]
        task = Task(task_id, target, platform, quality, output_dir)
        with self.lock:
            self.tasks[task_id] = task
        threading.Thread(target=self._run_task, args=(task,), daemon=True).start()
        self.broadcast({"type": "task_created", "task": task.snapshot()})
        return task

    def list_tasks(self) -> list[dict]:
        with self.lock:
            tasks = list(self.tasks.values())
        return [t.snapshot() for t in sorted(tasks, key=lambda x: x.created_at, reverse=True)]

    def get_task(self, task_id: str) -> Optional[Task]:
        with self.lock:
            return self.tasks.get(task_id)

    def stop_task(self, task_id: str) -> bool:
        task = self.get_task(task_id)
        if not task:
            return False
        if task.status not in ("starting", "recording"):
            return False
        task.stop_event.set()
        return True

    def add_bookmark(self, task_id: str, note: str) -> Optional[dict]:
        task = self.get_task(task_id)
        if not task:
            return None
        bookmark = {
            "at": time.time(),
            "elapsed": (time.time() - task.started_at) if task.started_at else 0,
            "progress_time": task.progress.get("time"),
            "note": note,
        }
        task.bookmarks.append(bookmark)
        self.broadcast({
            "type": "bookmark_added",
            "task_id": task_id,
            "bookmark": bookmark,
        })
        return bookmark

    # ---------- worker ----------
    def _run_task(self, task: Task) -> None:
        def on_event(kind: str, payload: dict) -> None:
            if kind == "info":
                task.room_id = payload.get("room_id")
                task.nickname = payload.get("nickname")
                task.title = payload.get("title")
                task.quality = payload.get("quality")
                task.output_path = payload.get("output_path")
                task.status = "recording"
                task.started_at = time.time()
                self.broadcast({"type": "task_updated", "task": task.snapshot()})
            elif kind == "progress":
                task.progress = payload
                self.broadcast({
                    "type": "progress",
                    "task_id": task.id,
                    "progress": payload,
                })
            elif kind == "ffmpeg_log":
                self.broadcast({
                    "type": "log",
                    "task_id": task.id,
                    "line": payload.get("line", ""),
                })
            elif kind == "converting":
                task.status = "converting"
                self.broadcast({"type": "task_updated", "task": task.snapshot()})
            elif kind == "done":
                task.mp4_path = payload.get("mp4")
            elif kind == "error":
                self.broadcast({
                    "type": "log",
                    "task_id": task.id,
                    "line": f"error: {payload.get('message')}",
                })

        try:
            record_core.record_room(
                task.target,
                platform=task.platform,
                output_dir=task.output_dir,
                quality=task.quality_pref,
                stop_event=task.stop_event,
                on_event=on_event,
                cli_progress=False,
            )
            task.status = "stopped" if task.stop_event.is_set() else "done"
        except Exception as e:
            task.status = "error"
            task.error = str(e)
        finally:
            task.ended_at = time.time()
            self.broadcast({"type": "task_updated", "task": task.snapshot()})


MANAGER = TaskManager()


# ---------- Watcher：监听用户开播（仅 supports_watcher=True 的平台） ----------

DEFAULT_WATCH_INTERVAL = 30


class Watcher:
    """监听一个用户，开播自动启动录制，下播回到 idle 等下一场。"""

    def __init__(
        self,
        watcher_id: str,
        platform: Platform,
        user_id: str,
        nickname: str,
        avatar: str,
        interval: int,
        quality: Optional[str],
        output_dir: str,
    ):
        self.id = watcher_id
        self.platform = platform
        self.user_id = user_id  # 平台内的"用户 id"（抖音 = sec_user_id）
        self.nickname = nickname
        self.avatar = avatar
        self.interval = max(10, int(interval))
        self.quality = quality
        self.output_dir = output_dir

        self.created_at = time.time()
        self.status = "idle"  # idle / checking / live_recording / error
        self.last_checked_at: Optional[float] = None
        self.last_error: Optional[str] = None
        self.error_streak = 0
        self.current_task_id: Optional[str] = None
        self.records_started: int = 0
        self.kick_event = threading.Event()
        self.stop_event = threading.Event()

    def snapshot(self) -> dict:
        return {
            "id": self.id,
            "platform": self.platform.id,
            "platform_display_name": self.platform.display_name,
            "user_id": self.user_id,
            "sec_user_id": self.user_id,  # 历史别名给旧 dashboard JS
            "nickname": self.nickname,
            "avatar": self.avatar,
            "interval": self.interval,
            "quality": self.quality,
            "output_dir": self.output_dir,
            "created_at": self.created_at,
            "status": self.status,
            "last_checked_at": self.last_checked_at,
            "last_error": self.last_error,
            "current_task_id": self.current_task_id,
            "records_started": self.records_started,
        }


class WatcherManager:
    def __init__(self, task_manager: TaskManager):
        self.watchers: dict[str, Watcher] = {}
        self.lock = threading.Lock()
        self.task_manager = task_manager

    def list_watchers(self) -> list[dict]:
        with self.lock:
            ws = list(self.watchers.values())
        return [w.snapshot() for w in sorted(ws, key=lambda x: x.created_at, reverse=True)]

    def get(self, watcher_id: str) -> Optional[Watcher]:
        with self.lock:
            return self.watchers.get(watcher_id)

    def create(
        self,
        target: str,
        platform: Platform,
        interval: int = DEFAULT_WATCH_INTERVAL,
        quality: Optional[str] = None,
        output_dir: Optional[str] = None,
    ) -> Watcher:
        if not platform.supports_watcher or platform.fetch_user_profile is None:
            raise ValueError(
                f"平台 {platform.display_name!r} 不支持监听开播。"
                f"请在 \"立即录制\" tab 给一个正在播的直播间链接。"
            )

        # 先 resolve 出 user_id（resolve 既支持直播间也支持主页输入）
        # 监听场景下我们要的是 user_id；resolve 返回 (room_id_or_None, profile)
        # profile 里的 sec_user_id / user_id 才是关键
        room_id, profile = platform.resolve(target, platform.default_cookie)
        user_id = profile.get("sec_user_id") or profile.get("user_id")
        if not user_id:
            # 输入直接就是直播间链接 → 不能用作监听
            raise ValueError(
                f"监听任务需要一个'用户'输入，不是直播间链接。\n"
                f"对 {platform.display_name}，可用："
                + "; ".join(platform.input_examples[2:] or platform.input_examples)
            )

        # 已经从 resolve 里拿到的 profile 字段
        nickname = (profile.get("nickname") or "").strip() or user_id[:16] + "…"
        avatar = profile.get("avatar") or ""

        # 二次验证 + 拿最新 nickname/avatar
        try:
            fresh = platform.fetch_user_profile(user_id, platform.default_cookie)
            if fresh.get("nickname"):
                nickname = fresh["nickname"]
            if fresh.get("avatar"):
                avatar = fresh["avatar"]
        except Exception as e:
            raise RuntimeError(f"无法解析用户信息: {e}")

        watcher_id = uuid.uuid4().hex[:8]
        w = Watcher(
            watcher_id=watcher_id,
            platform=platform,
            user_id=user_id,
            nickname=nickname,
            avatar=avatar,
            interval=interval,
            quality=quality,
            output_dir=output_dir or DEFAULT_OUTPUT_DIR,
        )
        with self.lock:
            self.watchers[watcher_id] = w
        self.task_manager.broadcast({"type": "watcher_created", "watcher": w.snapshot()})
        threading.Thread(target=self._run, args=(w,), daemon=True).start()
        return w

    def delete(self, watcher_id: str) -> bool:
        with self.lock:
            w = self.watchers.pop(watcher_id, None)
        if not w:
            return False
        w.stop_event.set()
        w.kick_event.set()
        self.task_manager.broadcast({"type": "watcher_deleted", "watcher_id": watcher_id})
        return True

    def kick(self, watcher_id: str) -> bool:
        w = self.get(watcher_id)
        if not w:
            return False
        w.kick_event.set()
        return True

    def _broadcast_update(self, w: Watcher):
        self.task_manager.broadcast({"type": "watcher_updated", "watcher": w.snapshot()})

    def _run(self, w: Watcher):
        """watcher 主循环。逻辑跟原来抖音版一样，只不过 fetch_user_profile 走 platform 接口。"""
        while not w.stop_event.is_set():
            w.status = "checking" if w.current_task_id is None else w.status
            self._broadcast_update(w)

            try:
                profile = w.platform.fetch_user_profile(w.user_id, w.platform.default_cookie)
                w.last_checked_at = time.time()
                w.error_streak = 0
                w.last_error = None

                if profile.get("nickname"):
                    w.nickname = profile["nickname"]
                if profile.get("avatar"):
                    w.avatar = profile["avatar"]

                is_live = bool(profile.get("is_live"))
                # web_rid 是抖音字段；其他平台用 room_id 即可
                room_id = profile.get("web_rid") or profile.get("room_id")

                if w.current_task_id:
                    task = self.task_manager.get_task(w.current_task_id)
                    if task is None or task.status in ("done", "stopped", "error"):
                        w.current_task_id = None
                        if w.status != "error":
                            w.status = "idle"
                    else:
                        w.status = "live_recording"
                        self._broadcast_update(w)
                        self._sleep_or_kick(w)
                        continue

                if is_live and room_id:
                    try:
                        # 监听场景下提交录制：直接给 room_id 当 target，platform 嗅探会走 raw 数字分支
                        # 但更稳妥的做法是直接传 platform 实例 + target=room_id
                        task = self.task_manager.create_task(
                            room_id, w.platform, w.quality, w.output_dir
                        )
                        w.current_task_id = task.id
                        w.records_started += 1
                        w.status = "live_recording"
                    except Exception as e:
                        w.last_error = f"启动录制失败: {e}"
                        w.status = "error"
                else:
                    w.status = "idle"

            except Exception as e:
                w.error_streak += 1
                w.last_error = str(e)
                w.last_checked_at = time.time()
                if w.error_streak >= 3:
                    w.status = "error"

            self._broadcast_update(w)
            self._sleep_or_kick(w)

    def _sleep_or_kick(self, w: Watcher):
        w.kick_event.wait(timeout=w.interval)
        w.kick_event.clear()


WATCHERS = WatcherManager(MANAGER)


# ---------- HTTP ----------

def _read_json_body(handler: http.server.BaseHTTPRequestHandler) -> dict:
    length = int(handler.headers.get("Content-Length") or 0)
    if length == 0:
        return {}
    raw = handler.rfile.read(length)
    try:
        return json.loads(raw.decode("utf-8"))
    except Exception:
        return {}


def _send_json(handler: http.server.BaseHTTPRequestHandler, status: int, data) -> None:
    body = json.dumps(data, ensure_ascii=False).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(body)))
    handler.send_header("Cache-Control", "no-store")
    handler.end_headers()
    handler.wfile.write(body)


class Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        pass

    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if path == "/" or path == "/index.html":
            self._serve_dashboard()
        elif path == "/api/tasks":
            _send_json(self, 200, {"tasks": MANAGER.list_tasks()})
        elif path == "/api/watchers":
            _send_json(self, 200, {"watchers": WATCHERS.list_watchers()})
        elif path == "/api/platforms":
            _send_json(self, 200, {"platforms": platforms.list_platforms()})
        elif path == "/api/config":
            _send_json(self, 200, {
                "default_output_dir": DEFAULT_OUTPUT_DIR,
                "platforms": platforms.list_platforms(),
            })
        elif path == "/api/events":
            self._serve_sse()
        else:
            self.send_error(404)

    def do_POST(self):
        path = self.path.split("?", 1)[0]
        if path == "/api/tasks":
            body = _read_json_body(self)
            target = (body.get("target") or "").strip()
            if not target:
                _send_json(self, 400, {"error": "missing target"})
                return
            try:
                platform = _resolve_platform(target, body.get("platform"))
            except ValueError as e:
                _send_json(self, 400, {"error": str(e)})
                return
            quality = body.get("quality") or None
            output_dir = body.get("output_dir") or DEFAULT_OUTPUT_DIR
            try:
                task = MANAGER.create_task(target, platform, quality, output_dir)
            except Exception as e:
                _send_json(self, 500, {"error": str(e)})
                return
            _send_json(self, 201, {"task": task.snapshot()})
            return

        if path == "/api/watchers":
            body = _read_json_body(self)
            target = (body.get("target") or "").strip()
            if not target:
                _send_json(self, 400, {"error": "missing target"})
                return
            try:
                platform = _resolve_platform(target, body.get("platform"))
            except ValueError as e:
                _send_json(self, 400, {"error": str(e)})
                return
            try:
                interval = int(body.get("interval") or DEFAULT_WATCH_INTERVAL)
            except (TypeError, ValueError):
                interval = DEFAULT_WATCH_INTERVAL
            quality = body.get("quality") or None
            output_dir = body.get("output_dir") or DEFAULT_OUTPUT_DIR
            try:
                w = WATCHERS.create(target, platform, interval=interval,
                                    quality=quality, output_dir=output_dir)
            except (ValueError, RuntimeError) as e:
                _send_json(self, 400, {"error": str(e)})
                return
            except Exception as e:
                _send_json(self, 500, {"error": str(e)})
                return
            _send_json(self, 201, {"watcher": w.snapshot()})
            return

        m_stop = path.startswith("/api/tasks/") and path.endswith("/stop")
        m_bm = path.startswith("/api/tasks/") and path.endswith("/bookmark")
        m_w_kick = path.startswith("/api/watchers/") and path.endswith("/check-now")
        if m_stop:
            task_id = path[len("/api/tasks/"):-len("/stop")]
            if MANAGER.stop_task(task_id):
                _send_json(self, 200, {"ok": True})
            else:
                _send_json(self, 404, {"error": "task not found or not running"})
            return
        if m_bm:
            task_id = path[len("/api/tasks/"):-len("/bookmark")]
            body = _read_json_body(self)
            note = (body.get("note") or "").strip()
            bookmark = MANAGER.add_bookmark(task_id, note)
            if bookmark is None:
                _send_json(self, 404, {"error": "task not found"})
            else:
                _send_json(self, 201, {"bookmark": bookmark})
            return
        if m_w_kick:
            watcher_id = path[len("/api/watchers/"):-len("/check-now")]
            if WATCHERS.kick(watcher_id):
                _send_json(self, 200, {"ok": True})
            else:
                _send_json(self, 404, {"error": "watcher not found"})
            return

        self.send_error(404)

    def do_DELETE(self):
        path = self.path.split("?", 1)[0]
        if path.startswith("/api/watchers/"):
            watcher_id = path[len("/api/watchers/"):]
            if WATCHERS.delete(watcher_id):
                _send_json(self, 200, {"ok": True})
            else:
                _send_json(self, 404, {"error": "watcher not found"})
            return
        self.send_error(404)

    # ---------- helpers ----------
    def _serve_dashboard(self):
        try:
            html = DASHBOARD_HTML.read_bytes()
        except FileNotFoundError:
            self.send_error(500, "dashboard.html missing")
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(html)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(html)

    def _serve_sse(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "keep-alive")
        self.end_headers()

        try:
            self._sse_write({
                "type": "snapshot",
                "tasks": MANAGER.list_tasks(),
                "watchers": WATCHERS.list_watchers(),
                "platforms": platforms.list_platforms(),
            })
        except Exception:
            return

        q = MANAGER.subscribe()
        try:
            while True:
                try:
                    event = q.get(timeout=15)
                except queue.Empty:
                    try:
                        self.wfile.write(b": keepalive\n\n")
                        self.wfile.flush()
                    except Exception:
                        break
                    continue
                try:
                    self._sse_write(event)
                except Exception:
                    break
        finally:
            MANAGER.unsubscribe(q)

    def _sse_write(self, event: dict) -> None:
        data = json.dumps(event, ensure_ascii=False)
        self.wfile.write(f"data: {data}\n\n".encode("utf-8"))
        self.wfile.flush()


class ThreadingServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def find_free_port(preferred: int) -> int:
    import socket
    for port in itertools.islice(itertools.count(preferred), 20):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            try:
                s.bind((HOST, port))
                return port
            except OSError:
                continue
    raise RuntimeError(f"找不到可用端口（{preferred}-{preferred+19}）。")


def main():
    import argparse
    p = argparse.ArgumentParser(description="多平台直播录制 dashboard 服务")
    p.add_argument("--port", type=int, default=DEFAULT_PORT)
    p.add_argument("--no-browser", action="store_true", help="启动后不自动开浏览器")
    p.add_argument("--target", default=None, help="启动后立即提交一个录制任务")
    p.add_argument("--platform", default=None,
                   help=f"指定平台 id（{','.join(p.id for p in platforms.PLATFORMS)}），不指定按 target 嗅探")
    p.add_argument("--quality", default=None)
    p.add_argument("--output-dir", default=None,
                   help="默认输出目录（不指定用 server 进程的当前目录）")
    args = p.parse_args()

    global DEFAULT_OUTPUT_DIR
    if args.output_dir:
        DEFAULT_OUTPUT_DIR = str(Path(args.output_dir).expanduser().resolve())
    print(f"📁 默认输出目录: {DEFAULT_OUTPUT_DIR}", flush=True)
    print(f"🎬 已注册平台: {', '.join(p.display_name for p in platforms.PLATFORMS)}", flush=True)

    port = find_free_port(args.port)
    server = ThreadingServer((HOST, port), Handler)
    url = f"http://{HOST}:{port}/"
    print(f"📺 dashboard 已启动: {url}", flush=True)

    if args.target:
        try:
            platform = _resolve_platform(args.target, args.platform)
            MANAGER.create_task(args.target, platform, args.quality, DEFAULT_OUTPUT_DIR)
            print(f"   已提交录制任务 [{platform.display_name}]: {args.target}", flush=True)
        except Exception as e:
            print(f"   ❌ 启动时录制提交失败: {e}", flush=True)

    if not args.no_browser:
        try:
            webbrowser.open(url)
        except Exception:
            pass

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n关闭 dashboard...", flush=True)
        for t in list(MANAGER.tasks.values()):
            t.stop_event.set()
        deadline = time.time() + 8
        while time.time() < deadline and any(
            t.status in ("starting", "recording", "converting") for t in MANAGER.tasks.values()
        ):
            time.sleep(0.3)
        server.server_close()


if __name__ == "__main__":
    main()
