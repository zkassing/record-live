#!/usr/bin/env python3
"""旧版 CLI 入口的兼容 shim。

为了兼容 SKILL.md 里"用户明说不要 dashboard"的纯命令行用法，保留这个文件，
内部转发到 record_core.record_room()。

新代码请直接 import record_core / platforms。
"""
from __future__ import annotations

import argparse
import os
import signal
import sys
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import platforms  # noqa: E402
import record_core  # noqa: E402

# 老代码可能 import 这两个常量，保留导出
from platforms.douyin import DEFAULT_TTWID, USER_AGENT, QUALITY_ORDER  # noqa: F401, E402


def log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


def main() -> int:
    p = argparse.ArgumentParser(description="录制直播到 MP4（多平台）")
    p.add_argument("target", help="直播间链接 / web_rid / 用户主页 / sec_user_id 等")
    p.add_argument("--platform", default=None,
                   help=f"指定平台 id（{','.join(p.id for p in platforms.PLATFORMS)}），不指定按 target 嗅探")
    p.add_argument("--output-dir", "-o", default=os.getcwd())
    p.add_argument("--quality", "-q", default=None)
    p.add_argument("--cookie", "-c",
                   default=os.environ.get("LIVEBUDDY_DOUYIN_COOKIE")
                   or os.environ.get("DOUYIN_COOKIE")
                   or None)
    args = p.parse_args()

    platform = None
    if args.platform:
        platform = platforms.get_platform(args.platform)
        if platform is None:
            log(f"❌ 未知平台 {args.platform!r}，已支持: {[x.id for x in platforms.PLATFORMS]}")
            return 1
    else:
        platform = platforms.detect_platform(args.target)
        if platform is None:
            log(f"❌ 无法识别输入所属平台: {args.target!r}")
            return 1

    stop_event = threading.Event()

    def handle_signal(_sig, _frame):
        if stop_event.is_set():
            return
        log("\n收到中断信号，向 ffmpeg 发 'q' 优雅收尾...")
        stop_event.set()

    signal.signal(signal.SIGINT, handle_signal)
    signal.signal(signal.SIGTERM, handle_signal)

    try:
        record_core.record_room(
            args.target,
            platform=platform,
            output_dir=args.output_dir,
            quality=args.quality,
            cookie=args.cookie,
            stop_event=stop_event,
            cli_progress=True,
        )
        return 0
    except RuntimeError as e:
        log(f"❌ {e}")
        return 1
    except ValueError as e:
        log(f"❌ {e}")
        return 1
    except KeyboardInterrupt:
        log("\n中断退出。")
        return 130


if __name__ == "__main__":
    sys.exit(main())
