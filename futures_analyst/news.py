"""消息汇总：每个品种每天两步。

1. Claude + 联网搜索：找最近几天影响该品种价格的消息，写一段带来源的中文摘要
2. 结构化抽取（output_config.format）：把摘要里的消息和上海金属网快讯整理成条目，标利多/利空/中性和影响程度，
   并给一个整体情绪分（-1~1）。citations 不能和 output_config.format 同用，所以分两次调用。

情绪分没有历史可回测，只展示、不进打分。
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta
from typing import Callable
from urllib.parse import urlparse

import pandas as pd

from .config import CLAUDE_EFFORT, CLAUDE_MODEL, NEWS_ENABLED, now
from . import llm
from .db import DB
from .llm import FALLBACK
from .universe import PRODUCTS

SEARCH_SYSTEM = """你是期货研究助理，负责每天整理影响某个国内期货品种价格的消息。

要求：
- 只找最近 3 天内发布的消息（今天是 {today}，北京时间）。超过 3 天的不要用，除非是仍在发酵的重大事件，并注明原始日期。
- 关注供需、库存、产量和开工、进出口、政策（限产、收储、关税、环保）、宏观（美联储、美元、国内经济数据）、海外市场、突发事件。
- 优先用权威来源：交易所和政府公告、行业机构（如上海有色网、我的钢铁网、中国钢铁工业协会）、主流财经媒体和通讯社。
- 用中文写 5~8 条要点，每条一句话，说明消息内容和它可能对价格的影响方向（偏利多 / 偏利空 / 影响不明）。
- 只陈述消息和常见的影响逻辑，不预测具体价格，不给任何买卖或开仓建议。
- 找不到可靠的近期消息就直说，不要编造。"""

EXTRACT_SYSTEM = """你把期货消息整理成结构化条目。只使用给你的材料，不添加材料里没有的事实。
stance 只能是 利多 / 利空 / 中性，指对该品种国内期货价格的常见影响方向；impact 取 1（小）~3（大）。
sentiment 是整体情绪，-1（明显利空）到 1（明显利多），没有明确倾向就给 0 附近。
summary 用中文 2~3 句话概括今天的消息面，不给买卖建议。"""

EXTRACT_SCHEMA = {
    "type": "object",
    "properties": {
        "summary": {"type": "string"},
        "sentiment": {"type": "number"},
        "items": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "title": {"type": "string"},
                    "url": {"type": "string"},
                    "source": {"type": "string"},
                    "published": {"type": "string"},
                    "stance": {"type": "string", "enum": ["利多", "利空", "中性"]},
                    "impact": {"type": "integer"},
                    "reason": {"type": "string"},
                },
                "required": ["title", "url", "source", "published", "stance", "impact", "reason"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["summary", "sentiment", "items"],
    "additionalProperties": False,
}



def _search(client, code: str, today: str) -> tuple[str, list[dict]]:
    p = PRODUCTS[code]
    messages = [{"role": "user", "content": f"品种：{p.name}（{p.exchange} {code}）。搜索关键词参考：{p.news_query}。"
                                            f"请整理最近 3 天的重要消息。"}]
    text, sources = [], {}
    for _ in range(4):   # 联网搜索可能分几段返回（pause_turn），续上即可
        resp = client.beta.messages.create(
            model=CLAUDE_MODEL, max_tokens=16000, system=SEARCH_SYSTEM.format(today=today), messages=messages,
            tools=[{"type": "web_search_20260209", "name": "web_search", "max_uses": 5,
                    "user_location": {"type": "approximate", "country": "CN", "timezone": "Asia/Shanghai"}}],
            output_config={"effort": CLAUDE_EFFORT}, **FALLBACK)
        if resp.stop_reason == "refusal":
            raise RuntimeError("请求被拒绝")
        for b in resp.content:
            if b.type == "text":
                text.append(b.text)
                for c in getattr(b, "citations", None) or []:
                    url = getattr(c, "url", None)
                    if url:
                        sources.setdefault(url, {"url": url, "title": getattr(c, "title", "") or url})
            elif b.type == "web_search_tool_result" and isinstance(b.content, list):
                for r in b.content:
                    if getattr(r, "url", None):
                        sources.setdefault(r.url, {"url": r.url, "title": r.title, "page_age": getattr(r, "page_age", None)})
        if resp.stop_reason != "pause_turn":
            break
        messages = messages[:1] + [{"role": "assistant", "content": resp.content}]
    return "".join(text).strip(), list(sources.values())


def _extract(client, code: str, summary: str, sources: list[dict], flashes: list[str]) -> dict:
    material = [f"品种：{PRODUCTS[code].name}", "【联网搜索摘要】", summary or "（无）", "【搜索到的来源】",
                json.dumps(sources[:20], ensure_ascii=False)]
    if flashes:
        material += ["【上海金属网快讯（最近两天）】", *flashes[:30]]
    resp = client.beta.messages.create(
        model=CLAUDE_MODEL, max_tokens=8000, system=EXTRACT_SYSTEM,
        messages=[{"role": "user", "content": "\n".join(material)}],
        output_config={"effort": "low", "format": {"type": "json_schema", "schema": EXTRACT_SCHEMA}}, **FALLBACK)
    if resp.stop_reason == "refusal":
        raise RuntimeError("请求被拒绝")
    return json.loads(next(b.text for b in resp.content if b.type == "text"))


def digest_product(db: DB, client, code: str, day: str, log: Callable = print) -> bool:
    flashes = db.query("SELECT published_at, content FROM news_items WHERE product = ? AND source = '上海金属网' "
                       "AND published_at >= ? ORDER BY published_at DESC LIMIT 30",
                       (code, (now() - timedelta(days=2)).isoformat()))
    flash_text = [f"{r.published_at[:16]} {r.content[:200]}" for r in flashes.itertuples()]
    summary, sources = _search(client, code, now().date().isoformat())
    data = _extract(client, code, summary, sources, flash_text)
    items = data.get("items", [])
    sentiment = max(-1.0, min(1.0, float(data.get("sentiment", 0))))
    counts = {k: sum(1 for i in items if i["stance"] == k) for k in ("利多", "利空", "中性")}
    db.upsert("news_digest", ["product", "date", "summary", "sentiment", "bullish", "bearish", "neutral", "sources",
                              "model", "created_at"],
              [(code, day, data.get("summary", ""), sentiment, counts["利多"], counts["利空"], counts["中性"],
                json.dumps(sources[:20], ensure_ascii=False), CLAUDE_MODEL, now().isoformat(timespec="seconds"))])
    rows = []
    for it in items:
        url = it.get("url") or ""
        nid = "ai-" + hashlib.sha1(f"{code}{url}{it['title']}".encode()).hexdigest()[:16]
        source = it.get("source") or (urlparse(url).netloc if url else "")
        rows.append((nid, code, it.get("published") or now().isoformat(timespec="minutes"), source, it["title"],
                     url or None, None, it["stance"], max(1, min(3, int(it.get("impact", 1)))), it.get("reason"), day))
    db.upsert("news_items", ["id", "product", "published_at", "source", "title", "url", "content", "stance", "impact",
                             "reason", "digest_date"], rows)
    log(f"  {code}：{len(items)} 条（利多 {counts['利多']} / 利空 {counts['利空']}），情绪 {sentiment:+.2f}")
    return True


def digest_all(db: DB, signals: dict, log: Callable = print) -> bool:
    """返回是否有新的消息汇总。没有 API key 或关闭了 NEWS_ENABLED 时跳过。"""
    if not NEWS_ENABLED or not llm.has_key():
        log("消息汇总：没有配置 ANTHROPIC_API_KEY，跳过")
        return False
    from .sources import fetch

    client = llm.make_client()
    changed = False
    log("消息汇总（Claude 联网搜索）…")
    for code, sig in signals.items():
        day = sig["signal_day"]
        if db.execute("SELECT 1 FROM news_digest WHERE product = ? AND date = ?", (code, day)):
            continue
        try:
            fetch(db, "news_ai", f"{code}:{day}", lambda: digest_product(db, client, code, day, log), retries=2,
                  describe=lambda r: (1, day))
            changed = True
        except Exception as e:  # noqa: BLE001 —— 单个品种失败不影响其他品种和整体流程
            log(f"  ! {code} 消息汇总失败：{e}")
    return changed
