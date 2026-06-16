"""小红书直播平台实现。

参考 DouyinLiveRecorder/src/spider.py::get_xhs_stream_url 的方案：
- iOS App UA + xy-common-params header 才能拿到 SSR 里完整的 pullConfig
- 直播流地址在 window.__INITIAL_STATE__.liveStream.roomData.roomInfo.pullConfig.h264[].master_url
- pullConfig 是 JSON 字符串，需要二次解析

不支持监听开播：小红书用户主页（https://www.xiaohongshu.com/user/profile/<host_id>）
SSR 里完全没有直播状态字段，公开接口又需要签名。所以 PLATFORM.supports_watcher = False。
"""
from __future__ import annotations

import json
import re
import ssl
import urllib.error
import urllib.parse
import urllib.request
from typing import Optional

from .base import Platform


# 注意：必须用 iOS App UA + xy-common-params 才会返回完整的 liveStream.roomData。
# 桌面 Chrome UA 拿到的 pullConfig 是 undefined。
USER_AGENT = "ios/7.830 (ios 17.0; ; iPhone 15 (A2846/A3089/A3090/A3092))"
XY_COMMON_PARAMS = "platform=iOS&sid=session.1722166379345546829388"
REFERER = "https://app.xhs.cn/"

# 匿名也能查（liveStream SSR 不需要登录态）。这里留空，调用层走 _http_get 时不带 Cookie。
DEFAULT_COOKIE = ""

# 画质：小红书 pullConfig 给的 quality_type 一般是 HD / SD / LD（实测多为 HD/原画）
QUALITY_ORDER = ["HD", "SD", "LD"]


def _http_get(url: str, cookie: str, timeout: float = 15.0) -> str:
    headers = {
        "User-Agent": USER_AGENT,
        "xy-common-params": XY_COMMON_PARAMS,
        "Referer": REFERER,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "zh-CN,zh;q=0.9",
    }
    if cookie:
        headers["Cookie"] = cookie
    req = urllib.request.Request(url, headers=headers)
    ctx = ssl._create_unverified_context()
    with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp:
        return resp.read().decode("utf-8", errors="replace")


_LIVESTREAM_URL = re.compile(r"xiaohongshu\.com/livestream/(\d+)")


def _extract_room_id(raw: str) -> Optional[str]:
    raw = raw.strip()
    m = _LIVESTREAM_URL.search(raw)
    if m:
        return m.group(1)
    return None


def _expand_short_url(short_url: str) -> str:
    """xhslink.com 短链 → 跟随重定向拿真实 URL。"""
    req = urllib.request.Request(
        short_url,
        headers={
            "User-Agent": USER_AGENT,
            "xy-common-params": XY_COMMON_PARAMS,
            "Referer": REFERER,
        },
    )
    ctx = ssl._create_unverified_context()
    try:
        with urllib.request.urlopen(req, timeout=10, context=ctx) as resp:
            return resp.geturl()
    except urllib.error.HTTPError as e:
        if e.code in (301, 302, 303, 307, 308):
            return e.headers.get("Location") or short_url
        raise


def _parse_pull_config(pull_config_str: str) -> tuple[Optional[str], Optional[str], dict]:
    """解析 pullConfig（JSON 字符串），返回 (preferred_url, preferred_quality, quality_map)。

    优先 flv > m3u8（ffmpeg 录 flv 比 m3u8 拼接稳，且能直接 stream copy）。
    quality_map: {"HD": "...flv", "HD_m3u8": "...m3u8", ...}
    """
    if not pull_config_str:
        return None, None, {}
    try:
        cfg = json.loads(pull_config_str)
    except (json.JSONDecodeError, TypeError):
        return None, None, {}

    streams = cfg.get("h264") or cfg.get("h265") or []
    quality_map: dict[str, str] = {}
    flv_streams: list[tuple[str, str]] = []
    m3u8_streams: list[tuple[str, str]] = []
    for s in streams:
        url = s.get("master_url")
        q = s.get("quality_type") or "HD"
        if not url:
            continue
        if url.endswith(".flv"):
            flv_streams.append((q, url))
            quality_map.setdefault(q, url)
        elif url.endswith(".m3u8"):
            m3u8_streams.append((q, url))
            quality_map.setdefault(f"{q}_m3u8", url)

    # 选优顺序：flv 原画 → flv 任一 → m3u8 原画 → m3u8 任一
    for q_pref in QUALITY_ORDER:
        for q, url in flv_streams:
            if q == q_pref:
                return url, q, quality_map
    if flv_streams:
        return flv_streams[0][1], flv_streams[0][0], quality_map
    for q_pref in QUALITY_ORDER:
        for q, url in m3u8_streams:
            if q == q_pref:
                return url, q, quality_map
    if m3u8_streams:
        return m3u8_streams[0][1], m3u8_streams[0][0], quality_map
    return None, None, quality_map


def _parse_initial_state(html: str) -> Optional[dict]:
    m = re.search(r"window\.__INITIAL_STATE__\s*=\s*", html)
    if not m:
        return None
    start = m.end()
    end = html.find("</script>", start)
    if end < 0:
        return None
    blob = html[start:end].rstrip().rstrip(";")
    blob = blob.replace("undefined", "null")
    try:
        return json.loads(blob)
    except json.JSONDecodeError:
        return None


def _fetch_room_data(room_id: str, cookie: str) -> dict:
    """抓直播间页面，解析出 {is_live, title, nickname, host_id, stream_url, ...}。"""
    url = f"https://www.xiaohongshu.com/livestream/{room_id}"
    html = _http_get(url, cookie=cookie)

    state = _parse_initial_state(html)
    if not state:
        raise RuntimeError(
            "未在小红书直播间页面找到 __INITIAL_STATE__，可能是 UA 被屏蔽或页面结构变更。"
        )

    live_stream = state.get("liveStream") or {}
    if live_stream.get("liveStatus") != "success":
        return {
            "is_live": False,
            "title": "",
            "nickname": "",
            "host_id": None,
            "stream_url": None,
            "stream_format": None,
            "quality": None,
            "qualities": [],
            "stream_map": {},
            "room_id": room_id,
            "live_status": live_stream.get("liveStatus"),
            "error_msg": live_stream.get("errorMessage", ""),
        }

    room_info = (live_stream.get("roomData") or {}).get("roomInfo") or {}
    title = room_info.get("roomTitle") or ""
    if title and "回放" in title:
        return {
            "is_live": False,
            "title": title,
            "nickname": "",
            "host_id": None,
            "stream_url": None,
            "stream_format": None,
            "quality": None,
            "qualities": [],
            "stream_map": {},
            "room_id": room_id,
            "live_status": "replay",
            "error_msg": "直播已结束（回放）",
        }

    pull_config = room_info.get("pullConfig")
    stream_url, chosen_quality, quality_map = _parse_pull_config(pull_config)

    # 从 deeplink 抠 host_nickname / host_id（pullConfig 不带这俩）
    deeplink = room_info.get("deeplink") or ""
    nickname = ""
    host_id = None
    if deeplink:
        params = urllib.parse.parse_qs(urllib.parse.urlparse(deeplink).query)
        nickname = (params.get("host_nickname") or [""])[0]
        host_id = (params.get("host_id") or [None])[0]

    fmt = None
    if stream_url:
        if stream_url.endswith(".flv"):
            fmt = "flv"
        elif stream_url.endswith(".m3u8"):
            fmt = "m3u8"

    return {
        "is_live": stream_url is not None,
        "title": title,
        "nickname": nickname,
        "host_id": host_id,
        "stream_url": stream_url,
        "stream_format": fmt,
        "quality": chosen_quality,
        "qualities": [k for k in quality_map.keys() if not k.endswith("_m3u8")],
        "stream_map": quality_map,
        "room_id": room_id,
    }


# ---------- Platform 接口实现 ----------

def matches(raw: str) -> bool:
    raw = raw.strip()
    if not raw:
        return False
    return ("xiaohongshu.com" in raw) or ("xhslink.com" in raw)


def resolve(raw: str, cookie: str) -> tuple[Optional[str], dict]:
    raw = raw.strip()
    cookie = cookie or DEFAULT_COOKIE

    if "xhslink.com" in raw:
        try:
            raw = _expand_short_url(raw)
        except Exception as e:
            raise RuntimeError(f"小红书短链解析失败: {e}")

    room_id = _extract_room_id(raw)
    if not room_id:
        raise ValueError(
            f"无法识别小红书直播间链接: {raw!r}\n"
            "支持的格式:\n"
            "  - https://www.xiaohongshu.com/livestream/<room_id>\n"
            "  - https://www.xiaohongshu.com/livestream/<room_id>?xsec_token=...\n"
            "  - xhslink.com 短链"
        )
    return room_id, {}


def fetch_live_info(room_id: str, cookie: str) -> dict:
    info = _fetch_room_data(room_id, cookie or DEFAULT_COOKIE)
    if not info["is_live"]:
        return {
            "is_live": False,
            "title": info.get("title", ""),
            "nickname": info.get("nickname", ""),
            "stream_url": None,
            "stream_format": None,
            "quality": None,
            "qualities": [],
            "stream_map": {},
            "room_id": room_id,
            "_xhs_status": info.get("live_status"),
            "_xhs_error": info.get("error_msg"),
        }
    return {
        "is_live": True,
        "title": info["title"],
        "nickname": info["nickname"],
        "stream_url": info["stream_url"],
        "stream_format": info["stream_format"],
        "quality": info["quality"],
        "qualities": info["qualities"],
        "stream_map": info["stream_map"],
        "room_id": room_id,
    }


PLATFORM = Platform(
    id="xiaohongshu",
    display_name="小红书",
    matches=matches,
    resolve=resolve,
    fetch_live_info=fetch_live_info,
    supports_watcher=False,
    fetch_user_profile=None,
    default_cookie=DEFAULT_COOKIE,
    input_examples=[
        "https://www.xiaohongshu.com/livestream/570321613446566013",
        "https://www.xiaohongshu.com/livestream/<room_id>?xsec_token=...",
        "xhslink.com 分享短链",
    ],
    ffmpeg_referer=REFERER,
    ffmpeg_user_agent=USER_AGENT,
)
