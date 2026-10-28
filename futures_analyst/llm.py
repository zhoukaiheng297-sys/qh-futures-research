"""Claude 客户端。集中在一处创建，测试里替换成伪造响应的客户端。"""
from __future__ import annotations

import os

# 用 claude-opus-5-5 时默认开启服务端兜底：被安全分类器拒绝时，API 自动换一个模型重试
FALLBACK = {"betas": ["server-side-fallback-2026-07-01"], "fallbacks": "default"}


def has_key() -> bool:
    return bool(os.getenv("ANTHROPIC_API_KEY") or os.getenv("ANTHROPIC_AUTH_TOKEN"))


def make_client():
    import anthropic

    return anthropic.Anthropic()
