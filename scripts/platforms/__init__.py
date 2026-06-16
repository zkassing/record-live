"""平台注册表。

新增平台只需在这里 import + 加进 PLATFORMS。
"""
from __future__ import annotations

from typing import Optional

from . import douyin, xiaohongshu
from .base import Platform


# 顺序就是嗅探优先级（一般无重叠，但若某天有就先到先得）
PLATFORMS: list[Platform] = [
    douyin.PLATFORM,
    xiaohongshu.PLATFORM,
]

PLATFORMS_BY_ID: dict[str, Platform] = {p.id: p for p in PLATFORMS}


def detect_platform(raw: str) -> Optional[Platform]:
    """根据用户输入嗅探平台。识别不出返回 None。"""
    raw = (raw or "").strip()
    if not raw:
        return None
    for p in PLATFORMS:
        try:
            if p.matches(raw):
                return p
        except Exception:
            continue
    return None


def get_platform(platform_id: str) -> Optional[Platform]:
    return PLATFORMS_BY_ID.get(platform_id)


def list_platforms() -> list[dict]:
    """给 dashboard 用：[{id, display_name, supports_watcher, examples}]。"""
    return [
        {
            "id": p.id,
            "display_name": p.display_name,
            "supports_watcher": p.supports_watcher,
            "input_examples": list(p.input_examples),
        }
        for p in PLATFORMS
    ]
