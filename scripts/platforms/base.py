"""平台抽象接口。

每个平台实现一个 module，导出 PLATFORM = Platform(...) 实例供 registry 注册。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Optional


# fetch_live_info 的返回 schema（dict 字段名约定）
#   is_live:    bool
#   title:      str
#   nickname:   str
#   stream_url: str   ← 用于 ffmpeg 的 -i（flv/m3u8 都行）
#   stream_format: "flv" | "m3u8"
#   quality:    str   ← 实际选中的画质标签
#   qualities:  list[str]
#   stream_map: dict[str, str]   ← 画质 → URL，用于切换
#   room_id:    str   ← 平台内的房间唯一 id（落盘命名用）
#
# fetch_user_profile 返回（仅用户主页/sec_user_id 输入或监听场景）：
#   sec_user_id / user_id: str
#   nickname:   str
#   avatar:     str
#   is_live:    bool
#   web_rid / room_id: str | None  ← 在播时填，否则 None


@dataclass
class Platform:
    """一个平台的能力描述。"""

    # 平台唯一 id："douyin" / "xiaohongshu"
    id: str
    # 中文展示名："抖音" / "小红书"
    display_name: str

    # URL / 输入嗅探：是否能识别这条 raw 输入。返回 True 表示该平台接手
    matches: Callable[[str], bool]

    # 输入归一化：把任意支持的输入解析成 (room_id_or_None, profile_dict)
    # - 如果输入直接是直播间链接/id：返回 (room_id, {})
    # - 如果输入是用户主页/user_id：查 profile，在播时返回 (room_id, profile)，
    #   不在播返回 (None, profile)
    # 实现可抛 ValueError（输入不识别）/ RuntimeError（接口失败）
    resolve: Callable[[str, str], tuple[Optional[str], dict]]

    # 拉直播间的元信息（含拉流地址）。room_id 必须是 resolve() 返回的值
    fetch_live_info: Callable[[str, str], dict]

    # 是否支持监听开播（dashboard 的 watcher tab）
    supports_watcher: bool = False

    # 给定 user_id（主页 id 或 sec_user_id），返回其当前直播状态。仅 supports_watcher=True 时实现
    fetch_user_profile: Optional[Callable[[str, str], dict]] = None

    # 默认 cookie（匿名兜底）。每个平台不同
    default_cookie: str = ""

    # 默认输入示例，给 SKILL.md / 错误提示使用
    input_examples: list[str] = field(default_factory=list)

    # ffmpeg 拉流时附加的请求头：Referer + UA。按平台区分
    ffmpeg_referer: str = ""
    ffmpeg_user_agent: str = ""
