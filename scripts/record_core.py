"""平台无关的直播录制循环。

把原 record_douyin.py 里和"具体平台"无关的部分抽出来：
- ffmpeg spawn / 进度解析 / FLV→MP4 转换
- record_room 主流程：调用 platform.resolve() + platform.fetch_live_info() 拿 stream_url，
  再走 ffmpeg 拉流落盘

CLI 入口在 record_douyin.py（兼容老命令行）。
"""
from __future__ import annotations

import datetime as dt
import re
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Callable, Optional

import platforms
from platforms.base import Platform


EventCallback = Callable[[str, dict], None]


def log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


def sanitize_filename(name: str) -> str:
    out = []
    for ch in name:
        if ch.isalnum() or ch in "_-.":
            out.append(ch)
        elif ch == " ":
            out.append("_")
    return "".join(out)[:60]


def humanize_size(num_bytes: int) -> str:
    n = float(num_bytes)
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.1f}{unit}"
        n /= 1024
    return f"{n:.1f}TB"


_FFMPEG_PROGRESS_RE = re.compile(
    r"size=\s*([\d.]+\s*\w+).*?time=([\d:.]+).*?bitrate=\s*([\d.]+\s*\w+/s)"
)


def _spawn_ffmpeg_record(
    stream_url: str,
    output_path: Path,
    *,
    user_agent: str,
    referer: str,
    container_format: str,
) -> subprocess.Popen:
    if shutil.which("ffmpeg") is None:
        raise RuntimeError("ffmpeg not found，请先安装 ffmpeg。")
    cmd = [
        "ffmpeg", "-y",
        "-user_agent", user_agent,
        "-headers", f"Referer: {referer}\r\n",
        "-reconnect", "1",
        "-reconnect_streamed", "1",
        "-reconnect_delay_max", "5",
        "-i", stream_url,
        "-c", "copy",
        "-f", container_format,
        str(output_path),
    ]
    return subprocess.Popen(
        cmd,
        stdin=subprocess.PIPE,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        bufsize=1,
        text=True,
    )


def _watch_ffmpeg(
    proc: subprocess.Popen,
    stop_event: Optional[threading.Event],
    on_event: Optional[EventCallback],
    cli_progress: bool,
) -> int:
    """读 ffmpeg stderr 推进度回调；stop_event 触发时给 ffmpeg 发 q 优雅收尾。"""
    last_progress = ""
    is_tty = sys.stderr.isatty()
    last_emit_at = 0.0
    EMIT_INTERVAL = 5.0

    stop_signaled = {"flag": False}

    def stop_watcher():
        if stop_event is None:
            return
        stop_event.wait()
        if stop_signaled["flag"]:
            return
        stop_signaled["flag"] = True
        try:
            if proc.stdin and not proc.stdin.closed:
                proc.stdin.write("q\n")
                proc.stdin.flush()
                proc.stdin.close()
        except Exception:
            pass

    if stop_event is not None:
        threading.Thread(target=stop_watcher, daemon=True).start()

    try:
        assert proc.stderr is not None
        for line in proc.stderr:
            line = line.rstrip("\r\n")
            m = _FFMPEG_PROGRESS_RE.search(line)
            if m:
                size, t, bitrate = m.group(1), m.group(2), m.group(3)
                if on_event:
                    on_event("progress", {"time": t, "size": size, "bitrate": bitrate})
                if cli_progress:
                    progress = f"  ⏺ 录制中  时长={t}  大小={size}  码率={bitrate}"
                    if is_tty:
                        sys.stderr.write("\r" + progress.ljust(len(last_progress)))
                        sys.stderr.flush()
                        last_progress = progress
                    else:
                        now = time.monotonic()
                        if now - last_emit_at >= EMIT_INTERVAL:
                            sys.stderr.write(progress + "\n")
                            sys.stderr.flush()
                            last_emit_at = now
            elif "error" in line.lower() or "failed" in line.lower():
                if on_event:
                    on_event("ffmpeg_log", {"line": line})
                if cli_progress:
                    sys.stderr.write(("\n" if is_tty and last_progress else "") + line + "\n")
                    sys.stderr.flush()
                    last_progress = ""
    finally:
        if cli_progress and last_progress:
            sys.stderr.write("\n")
            sys.stderr.flush()
        proc.wait()

    return proc.returncode


def _convert_to_mp4(src_path: Path, on_event: Optional[EventCallback]) -> Path:
    """把录到的 FLV/TS 转成 MP4 并删除源文件。"""
    if not src_path.exists() or src_path.stat().st_size == 0:
        raise RuntimeError(f"录制文件不存在或为空: {src_path}")

    mp4_path = src_path.with_suffix(".mp4")
    if on_event:
        on_event("converting", {"src": str(src_path), "mp4": str(mp4_path)})

    cmd = [
        "ffmpeg", "-y",
        "-i", str(src_path),
        "-c", "copy",
        "-bsf:a", "aac_adtstoasc",
        "-movflags", "+faststart",
        str(mp4_path),
    ]
    result = subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
    if result.returncode != 0 or not mp4_path.exists() or mp4_path.stat().st_size == 0:
        tail = (result.stderr or "")[-800:]
        raise RuntimeError(f"转换失败，源文件已保留: {src_path}\nffmpeg stderr 末尾:\n{tail}")
    src_path.unlink(missing_ok=True)
    return mp4_path


def record_room(
    target: str,
    *,
    platform: Optional[Platform] = None,
    output_dir: str | Path | None = None,
    quality: Optional[str] = None,
    cookie: Optional[str] = None,
    stop_event: Optional[threading.Event] = None,
    on_event: Optional[EventCallback] = None,
    cli_progress: bool = False,
) -> dict:
    """录制一场直播。

    target 是用户原始输入（链接 / id / sec_user_id ...）。
    platform 不传时按 target 自动嗅探。

    on_event(kind, payload) 钩子事件:
      "info":       {platform, room_id, nickname, title, quality, output_path, stream_url, qualities}
      "progress":   {time, size, bitrate}
      "ffmpeg_log": {line}
      "converting": {src, mp4}
      "done":       {mp4, size_bytes}
      "error":      {message, code}

    返回结果 dict（mp4_path / size_bytes / nickname / title / quality / platform）。
    """
    if platform is None:
        platform = platforms.detect_platform(target)
    if platform is None:
        raise ValueError(
            f"无法识别输入所属平台: {target!r}。"
            f"支持的平台: {[p.display_name for p in platforms.PLATFORMS]}"
        )

    cookie = cookie if cookie is not None else platform.default_cookie

    room_id, _profile = platform.resolve(target, cookie)
    if not room_id:
        msg = "无法解析出直播间 ID。如果是用户主页/sec_user_id 输入，请确认主播正在直播。"
        if on_event:
            on_event("error", {"message": msg, "code": "no_room_id"})
        raise RuntimeError(msg)

    info = platform.fetch_live_info(room_id, cookie)

    if not info.get("is_live"):
        msg = "该直播间当前未开播。"
        if info.get("nickname"):
            msg += f" 主播: {info['nickname']}"
        if info.get("title"):
            msg += f" / 标题: {info['title']}"
        if info.get("_xhs_error"):
            msg += f" / {info['_xhs_error']}"
        if on_event:
            on_event("error", {"message": msg, "code": "not_live"})
        raise RuntimeError(msg)

    stream_url = info.get("stream_url")
    chosen_quality = info.get("quality")
    if not stream_url:
        msg = "接口未返回拉流地址 (stream_url_missing)。建议浏览器登录后用开发者工具复制 cookie 重试。"
        if on_event:
            on_event("error", {"message": msg, "code": "stream_url_missing"})
        raise RuntimeError(msg)

    # 用户指定画质并存在 → 切换
    if quality and quality in info.get("stream_map", {}):
        stream_url = info["stream_map"][quality]
        chosen_quality = quality
    elif quality:
        if on_event:
            on_event("ffmpeg_log", {"line": f"指定画质 {quality} 不可用，回退到 {chosen_quality}"})

    output_dir = Path(output_dir).expanduser() if output_dir else Path.cwd()
    output_dir.mkdir(parents=True, exist_ok=True)

    title_for_file = info.get("title") or info.get("nickname") or f"{platform.id}_live"
    safe = sanitize_filename(title_for_file) or f"{platform.id}_live"
    ts = dt.datetime.now().strftime("%Y%m%d_%H%M%S")

    stream_format = info.get("stream_format") or "flv"
    # ffmpeg 输出容器：录 m3u8 时落 ts 太碎，统一录成 flv 容器（HLS 也能 stream copy 到 flv）
    container_format = "flv"
    out_ext = ".flv"
    intermediate_path = output_dir / f"{platform.id}_{safe}_{ts}{out_ext}"

    if on_event:
        on_event("info", {
            "platform": platform.id,
            "platform_display_name": platform.display_name,
            "room_id": room_id,
            "nickname": info.get("nickname"),
            "title": info.get("title"),
            "quality": chosen_quality,
            "qualities": info.get("qualities", []),
            "output_path": str(intermediate_path),
            "stream_url": stream_url,
            "stream_format": stream_format,
        })

    if cli_progress:
        log(f"\n▶ 开始录制 [{platform.display_name}]")
        log(f"   主播: {info.get('nickname') or '?'}")
        log(f"   标题: {info.get('title') or '?'}")
        log(f"   画质: {chosen_quality} ({stream_format})")
        log(f"   输出: {intermediate_path}")
        log(f"   流地址: {stream_url[:80]}{'...' if len(stream_url) > 80 else ''}\n")

    proc = _spawn_ffmpeg_record(
        stream_url, intermediate_path,
        user_agent=platform.ffmpeg_user_agent,
        referer=platform.ffmpeg_referer,
        container_format=container_format,
    )
    rc = _watch_ffmpeg(proc, stop_event, on_event, cli_progress)
    if cli_progress:
        log(f"\nffmpeg 已退出 (returncode={rc})。")

    if not intermediate_path.exists() or intermediate_path.stat().st_size == 0:
        msg = "录制文件为空或不存在，可能拉流失败或直播一启动就结束了。"
        if on_event:
            on_event("error", {"message": msg, "code": "empty_recording"})
        raise RuntimeError(msg)

    mp4_path = _convert_to_mp4(intermediate_path, on_event)
    size_bytes = mp4_path.stat().st_size
    if on_event:
        on_event("done", {"mp4": str(mp4_path), "size_bytes": size_bytes})
    if cli_progress:
        log(f"\n✅ 录制完成: {mp4_path}")
        log(f"   文件大小: {humanize_size(size_bytes)}")

    return {
        "mp4_path": str(mp4_path),
        "size_bytes": size_bytes,
        "nickname": info.get("nickname"),
        "title": info.get("title"),
        "quality": chosen_quality,
        "platform": platform.id,
        "platform_display_name": platform.display_name,
        "room_id": room_id,
    }
