"""消息汇总和解读的离线测试：用 httpx2 MockTransport 伪造 Claude API 的响应，不需要 API key、不花钱。

    .venv\\Scripts\\python.exe -m pytest tests -q
"""
from __future__ import annotations

import json

import anthropic
import httpx2
import pytest
from anthropic import DefaultHttpxClient

from futures_analyst import explain, llm, news
from futures_analyst.db import DB

CALLS = []


def fake(request: httpx2.Request) -> httpx2.Response:
    body = json.loads(request.content)
    CALLS.append(body)
    if any(t.get("type", "").startswith("web_search") for t in body.get("tools", [])):
        content = [
            {"type": "server_tool_use", "id": "srvtoolu_1", "name": "web_search", "input": {"query": "沪铜"}},
            {"type": "web_search_tool_result", "tool_use_id": "srvtoolu_1", "content": [
                {"type": "web_search_result", "url": "https://example.com/cu", "title": "铜库存下降",
                 "encrypted_content": "x", "page_age": "1 day ago"}]},
            {"type": "text", "text": "- 上期所铜库存连续下降，偏利多。"},
        ]
    elif body.get("output_config", {}).get("format"):
        content = [{"type": "text", "text": json.dumps({
            "summary": "库存下降，消息面偏多。", "sentiment": 1.7,
            "items": [{"title": "铜库存下降", "url": "https://example.com/cu", "source": "example.com",
                       "published": "2026-10-27", "stance": "利多", "impact": 5, "reason": "库存下降"}]},
            ensure_ascii=False)}]
    else:
        content = [{"type": "text", "text": "10 日信号偏多，可信度低。以上为统计信号，仅供研究，不构成投资建议。"}]
    return httpx2.Response(200, json={"id": "msg_x", "type": "message", "role": "assistant", "model": "claude-opus-5-5",
                                      "content": content, "stop_reason": "end_turn", "stop_sequence": None,
                                      "usage": {"input_tokens": 1, "output_tokens": 1}})


@pytest.fixture()
def db(tmp_path, monkeypatch):
    CALLS.clear()
    monkeypatch.setattr(llm, "has_key", lambda: True)
    monkeypatch.setattr(llm, "make_client", lambda: anthropic.Anthropic(
        api_key="test", http_client=DefaultHttpxClient(transport=httpx2.MockTransport(fake))))
    return DB(tmp_path / "t.db")


def test_news_digest(db):
    signals = {"CU": {"signal_day": "2026-10-27"}}
    assert news.digest_all(db, signals, log=lambda *a: None)
    d = db.query("SELECT * FROM news_digest").iloc[0]
    assert d["sentiment"] == 1.0                       # 超出范围的情绪分被截断到 [-1, 1]
    assert d["bullish"] == 1 and "example.com/cu" in d["sources"]
    item = db.query("SELECT * FROM news_items").iloc[0]
    assert item["stance"] == "利多" and item["impact"] == 3   # 影响程度截断到 1~3
    search, extract = CALLS[0], CALLS[1]
    assert search["tools"][0]["type"] == "web_search_20260209" and search["model"] == "claude-opus-5-5"
    assert extract["output_config"]["format"]["type"] == "json_schema"
    assert "fallbacks" in search                       # 默认开启服务端兜底
    # 同一天不重复调用
    n = len(CALLS)
    assert not news.digest_all(db, signals, log=lambda *a: None) and len(CALLS) == n


def test_explain_uses_claude_and_caches(db):
    sig = {"name": "沪铜", "product": "CU", "quadrant": "价跌仓增（空头主动）",
           "horizons": {"10": {"direction": "偏多", "confidence": "低", "prob": 0.56, "reasons": ["r"], "downgrades": [],
                               "contributions": []}}}
    out = explain.explain_all(db, {"CU": sig}, log=lambda *a: None)
    assert out["CU"]["source"] == "claude" and "不构成投资建议" in out["CU"]["text"]
    n = len(CALLS)
    explain.explain_all(db, {"CU": sig}, log=lambda *a: None)
    assert len(CALLS) == n                             # 相同信号命中缓存


def test_explain_template_without_key(db, monkeypatch):
    monkeypatch.setattr(llm, "has_key", lambda: False)
    sig = {"quadrant": "—", "horizons": {"5": {"direction": "中性", "confidence": "低", "prob": 0.5, "reasons": [],
                                               "downgrades": [], "contributions": []}}}
    out = explain.explain_all(db, {"CU": sig}, log=lambda *a: None)
    assert out["CU"]["source"] == "template" and "不构成投资建议" in out["CU"]["text"]
