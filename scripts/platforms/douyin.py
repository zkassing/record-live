"""抖音平台实现。

把原 record_douyin.py 里的 URL/sec_user_id/web_rid 解析逻辑搬过来，符合 Platform 接口。
"""
from __future__ import annotations

import json
import re
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Optional

from .base import Platform


DOUYIN_MAIN = "https://live.douyin.com"
DEFAULT_TTWID = (
    "ttwid=1%7C2iDIYVmjzMcpZ20fcaFde0VghXAA3NaNXE_SLR68IyE%7C1761045455%7C"
    "ab35197d5cfb21df6cbb2fa7ef1c9262206b062c315b9d04da746d0b37dfbc7d"
)
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)
QUALITY_ORDER = ["ORIGIN", "FULL_HD1", "UHD", "HD1", "HD", "SD1", "SD", "SD2", "LD"]


def _http_get(url: str, cookie: str, referer: str, timeout: float = 15.0) -> str:
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "zh-CN,zh;q=0.9",
            "Cookie": cookie,
            "Referer": referer,
        },
    )
    ctx = ssl._create_unverified_context()
    with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp:
        return resp.read().decode("utf-8", errors="replace")


def _extract_web_rid(raw: str) -> str:
    """从用户输入里抠 web_rid。仅识别直播间链接和纯数字。"""
    raw = raw.strip()
    m = re.search(r"live\.douyin\.com/(\d+)", raw)
    if m:
        return m.group(1)
    if raw.isdigit():
        return raw
    raise ValueError(
        f"无法从输入中识别 web_rid: {raw!r}。请提供形如 https://live.douyin.com/123456789 的链接，或纯数字 web_rid。"
    )


def _extract_sec_user_id(raw: str) -> Optional[str]:
    raw = raw.strip()
    m = re.search(r"douyin\.com/(?:user|share/user)/([A-Za-z0-9_-]+)", raw)
    if m:
        return m.group(1)
    if raw.startswith("MS4wLjABAAAA") and len(raw) >= 24:
        return raw
    return None


_UNIQUE_ID_RE = re.compile(r"^[A-Za-z0-9._-]{2,30}$")


def _looks_like_unique_id(raw: str) -> bool:
    raw = raw.strip()
    if not _UNIQUE_ID_RE.match(raw):
        return False
    if raw.startswith("MS4wLjABAAAA"):
        return False
    return True


def _unique_id_to_profile(unique_id: str, cookie: str) -> dict:
    """通过抖音号抓 live.douyin.com/<unique_id> 页面，从 RSC chunk 解析出 sec_uid/nickname/web_rid/is_live。"""
    url = f"{DOUYIN_MAIN}/{urllib.parse.quote(unique_id)}"
    req = urllib.request.Request(url, headers={
        "User-Agent": USER_AGENT,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
        "Accept-Language": "zh-CN,zh;q=0.9",
        "Cookie": cookie,
        "Referer": "https://live.douyin.com/",
    })
    ctx = ssl._create_unverified_context()
    try:
        with urllib.request.urlopen(req, timeout=10, context=ctx) as resp:
            html = resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"抖音号 {unique_id!r} 主页请求失败: HTTP {e.code}")

    sec_uid_re = re.compile(r'(?:\\\")?sec_uid(?:\\\")?\s*:\s*(?:\\\")?(MS4wLj[A-Za-z0-9_-]+)')
    sec_uid_matches = list(set(sec_uid_re.findall(html)))
    if not sec_uid_matches:
        raise RuntimeError(
            f"抖音号 {unique_id!r} 的主页未返回 sec_user_id。可能是机构号/官方号反爬，"
            f"或者抖音号写错了。建议直接给主播主页 URL（含 MS4wLjABAAAA…）。"
        )
    sec_uid = sec_uid_matches[0]
    if len(sec_uid_matches) > 1:
        sec_uid = min(sec_uid_matches, key=lambda s: html.find(s))

    nick_re = re.compile(r'(?:\\\")?nickname(?:\\\")?\s*:\s*(?:\\\")?([^"\\,}]+)')
    nicks = [n for n in nick_re.findall(html) if n and n != "$undefined"]
    nickname = nicks[0] if nicks else unique_id

    web_rid_re = re.compile(r'(?:\\\")?web_rid(?:\\\")?\s*:\s*(?:\\\")?([^"\\,}]+)')
    rids = [r for r in web_rid_re.findall(html) if r != unique_id and r.isdigit()]
    web_rid = rids[0] if rids else None
    is_live = web_rid is not None

    return {
        "sec_user_id": sec_uid,
        "nickname": nickname.strip(),
        "web_rid": web_rid,
        "is_live": is_live,
        "unique_id": unique_id,
    }


def _fetch_user_profile_impl(sec_user_id: str, cookie: str) -> dict:
    """调 aweme profile/other 接口，返回 nickname/avatar/web_rid/is_live。"""
    params = {
        "device_platform": "webapp",
        "aid": "6383",
        "sec_user_id": sec_user_id,
    }
    url = f"https://www.douyin.com/aweme/v1/web/user/profile/other/?{urllib.parse.urlencode(params)}"
    body = _http_get(url, cookie=cookie, referer="https://www.douyin.com/")
    data = json.loads(body)

    user = data.get("user") or {}
    nickname = (user.get("nickname") or "").strip()
    avatar = ""
    avatar_thumb = user.get("avatar_thumb") or {}
    urls = avatar_thumb.get("url_list") or []
    if urls:
        avatar = urls[0]

    room_data_raw = user.get("room_data")
    room_id = None
    web_rid = None
    is_live = False
    if isinstance(room_data_raw, str) and room_data_raw.strip():
        try:
            rd = json.loads(room_data_raw)
            web_rid = rd.get("owner", {}).get("web_rid") or rd.get("web_rid")
            room_id = rd.get("id_str") or rd.get("id")
            status = rd.get("status")
            is_live = status == 2
        except Exception:
            pass

    if not web_rid:
        web_rid = user.get("web_rid_str") or user.get("web_rid")

    return {
        "sec_user_id": sec_user_id,
        "nickname": nickname,
        "avatar": avatar,
        "room_id": str(room_id) if room_id else None,
        "web_rid": str(web_rid) if web_rid else None,
        "is_live": bool(is_live),
    }


# ---------- Platform 接口实现 ----------

_DOUYIN_LIVE_URL = re.compile(r"live\.douyin\.com")
_DOUYIN_USER_URL = re.compile(r"douyin\.com/(?:user|share/user)/")


def matches(raw: str) -> bool:
    raw = raw.strip()
    if not raw:
        return False
    if _DOUYIN_LIVE_URL.search(raw) or _DOUYIN_USER_URL.search(raw):
        return True
    if raw.isdigit() and 8 <= len(raw) <= 14:
        return True
    if raw.startswith("MS4wLjABAAAA"):
        return True
    if _looks_like_unique_id(raw):
        return True
    return False


def resolve(raw: str, cookie: str) -> tuple[Optional[str], dict]:
    raw = raw.strip()
    # 直播间链接 / 纯数字 → 直接拿 web_rid
    try:
        return _extract_web_rid(raw), {}
    except ValueError:
        pass

    sec_user_id = _extract_sec_user_id(raw)
    if sec_user_id:
        profile = _fetch_user_profile_impl(sec_user_id, cookie)
        return profile.get("web_rid"), profile

    if _looks_like_unique_id(raw):
        info = _unique_id_to_profile(raw, cookie)
        sec_user_id = info["sec_user_id"]
        # 用 sec_user_id 再走一次 profile 接口，统一返回结构（拿 avatar 等）
        try:
            profile = _fetch_user_profile_impl(sec_user_id, cookie)
        except Exception:
            profile = info
        # 把 unique_id 解析出来的兜底字段补进去
        if not profile.get("nickname"):
            profile["nickname"] = info.get("nickname", "")
        profile.setdefault("unique_id", info.get("unique_id"))
        return profile.get("web_rid") or info.get("web_rid"), profile

    raise ValueError(
        f"无法识别输入: {raw!r}。\n"
        "支持的格式:\n"
        "  - https://live.douyin.com/123456789\n"
        "  - 123456789\n"
        "  - https://www.douyin.com/user/MS4wLjABAAAA...\n"
        "  - MS4wLjABAAAA...\n"
        "  - 抖音号（如 Yseparate）"
    )


def fetch_live_info(web_rid: str, cookie: str) -> dict:
    """调 webcast enter 接口，返回 stream_url + 元信息。"""
    params = {
        "aid": "6383", "app_name": "douyin_web", "live_id": "1",
        "device_platform": "web", "language": "zh-CN", "browser_language": "zh-CN",
        "browser_platform": "Win32", "browser_name": "Chrome", "browser_version": "116.0.0.0",
        "web_rid": web_rid, "room_id": web_rid, "enter_from": "web_live",
        "cookie_enabled": "true", "screen_width": "1920", "screen_height": "1080",
    }
    url = f"{DOUYIN_MAIN}/webcast/room/web/enter/?{urllib.parse.urlencode(params)}"
    referer = f"{DOUYIN_MAIN}/{web_rid}"

    last_err: Optional[Exception] = None
    for attempt in range(3):
        try:
            body = _http_get(url, cookie=cookie, referer=referer)
            data = json.loads(body)
            break
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as e:
            last_err = e
            time.sleep(1.5)
    else:
        raise RuntimeError(f"接口请求三次都失败: {last_err}")

    rooms = data.get("data", {}).get("data") or []
    if not rooms:
        raise RuntimeError("接口返回空房间列表，可能 web_rid 错误或直播间不存在。")
    room = rooms[0]

    is_live = room.get("status") == 2
    title = (room.get("title") or "").strip()
    owner = (data.get("data", {}).get("user") or {}) or room.get("owner") or {}
    nickname = (owner.get("nickname") or "").strip()

    stream_url = room.get("stream_url") or {}
    flv_map = stream_url.get("flv_pull_url") or {}
    available = [q for q in QUALITY_ORDER if isinstance(flv_map.get(q), str) and flv_map[q].startswith("http")]

    chosen_url = flv_map.get(available[0]) if available else None
    chosen_quality = available[0] if available else None

    return {
        "is_live": is_live,
        "title": title,
        "nickname": nickname,
        "stream_url": chosen_url,
        "stream_format": "flv",
        "quality": chosen_quality,
        "qualities": available,
        "stream_map": flv_map,
        "room_id": web_rid,
    }


def fetch_user_profile(user_id: str, cookie: str) -> dict:
    """sec_user_id → 是否在播 + web_rid + 元信息。watcher 用。"""
    return _fetch_user_profile_impl(user_id, cookie)


PLATFORM = Platform(
    id="douyin",
    display_name="抖音",
    matches=matches,
    resolve=resolve,
    fetch_live_info=fetch_live_info,
    supports_watcher=True,
    fetch_user_profile=fetch_user_profile,
    default_cookie=DEFAULT_TTWID,
    input_examples=[
        "https://live.douyin.com/123456789  (直播间链接)",
        "123456789  (web_rid 纯数字)",
        "https://www.douyin.com/user/MS4wLjABAAAA...  (用户主页)",
        "MS4wLjABAAAA...  (sec_user_id)",
        "Yseparate  (抖音号 unique_id)",
    ],
    ffmpeg_referer="https://live.douyin.com/",
    ffmpeg_user_agent=USER_AGENT,
)
