"""把信号写成中文解读。有 API key 时由 Claude 写（数字只能来自信号数据），否则用模板。

同一份信号数据只调用一次（按内容哈希缓存在 meta 表里）。
"""
from __future__ import annotations

import hashlib
import json
from typing import Callable

from .config import CLAUDE_EFFORT, CLAUDE_MODEL, now
from . import llm
from .db import DB

SYSTEM = """你是期货量化研究员，把一个品种的统计信号写成给少数同好看的中文解读。

规则：
- 所有数字（概率、胜率、AUC、涨跌幅、价格）都必须来自给你的数据，不要自己估算或编造。
- 价格口径和行情软件一致：close 是 date 当天日盘收盘（不含当晚夜盘），change_pts / change_settle 是比昨结算的涨跌；
  change_5d / change_20d 是收盘对收盘、换月已复权的涨跌，提到时要说明是"5 日/20 日涨跌"。
- 先用一句话分别说 5 日、10 日、20 日的信号方向和可信度（如"10 日信号偏空，可信度低"）。
- 再说主要依据：贡献最大的 3~4 个因子是什么、当前数值在历史上的位置；量价仓情况；外盘或产业链（如有）；消息面（如有）。
- 必须讲清可靠性：样本外年内 AUC 和标准误、胜率与"一直押多数方向"的对比；可信度低时直接说明历史上没有可靠优势。
- 降级条件（换月、临近重大事件、波动剧烈、数据不全等）要逐条转述。
- 措辞用"信号偏多/偏空""模型倾向"，绝不写"建议做多/做空/买入/卖出/止损"之类的交易指令，不保证收益。
- 篇幅 250~400 字，可以用短列表，不用标题。最后一句固定为：以上为统计信号，仅供研究，不构成投资建议。"""


def _compact(sig: dict) -> dict:
    """只把写解读需要的字段交给模型，省 token。"""
    hz = {}
    for h, v in sig.get("horizons", {}).items():
        hz[h] = {k: v.get(k) for k in ("direction", "confidence", "prob", "score", "agreement", "reasons", "downgrades",
                                      "until", "events", "skill", "analogs")}
        hz[h]["top_factors"] = [{k: c.get(k) for k in ("label", "group", "z", "contribution", "hit_rate", "n")}
                                for c in v.get("contributions", [])[:6]]
    # 价格口径和网页一致：date 当天日盘收盘（不含夜盘），涨跌比昨结算；5/20 日是收盘对收盘、换月已复权
    keep = {k: sig.get(k) for k in ("name", "product", "date", "contract", "close", "settle", "pre_settle",
                                   "change_pts", "change_settle", "change_5d", "change_20d", "quadrant",
                                   "oi_change_5d", "foreign", "risk", "near_roll")}
    keep["horizons"] = hz
    keep["factors"] = [{k: f.get(k) for k in ("label", "raw", "z", "pct")} for f in sig.get("factors", [])
                       if f.get("group") in ("产业链", "库存", "期限与基差", "内外盘")]
    if sig.get("news"):
        keep["news"] = sig["news"]
    return keep


def template(sig: dict) -> str:
    parts = []
    for h, v in sig.get("horizons", {}).items():
        prob = f"，上涨概率 {v['prob']:.0%}" if v.get("prob") is not None else ""
        parts.append(f"{h} 日信号{v['direction']}{prob}，可信度{v['confidence']}。")
    lines = ["".join(parts)]
    h10 = sig.get("horizons", {}).get("10") or next(iter(sig.get("horizons", {}).values()), None)
    if h10:
        top = [c for c in h10.get("contributions", [])[:4] if c.get("contribution")]
        if top:
            lines.append("主要依据：" + "；".join(
                f"{c['label']}（z={c['z']:+.1f}，{'偏多' if c['contribution'] > 0 else '偏空'}）" for c in top) + "。")
        lines += [f"- {r}" for r in h10.get("reasons", [])]
        lines += [f"- 降级：{d}" for d in h10.get("downgrades", [])]
    lines.append(f"量价仓：{sig.get('quadrant')}。")
    lines.append("以上为统计信号，仅供研究，不构成投资建议。")
    return "\n".join(lines)


def explain_all(db: DB, signals: dict, log: Callable = print, use_ai: bool = True) -> dict:
    client = llm.make_client() if use_ai and llm.has_key() else None
    out = {}
    for code, sig in signals.items():
        data = _compact(sig)
        key = "explain:" + hashlib.sha1(json.dumps(data, ensure_ascii=False, sort_keys=True, default=str)
                                        .encode()).hexdigest()
        cached = db.get_meta(key)
        if cached:
            out[code] = json.loads(cached)
            continue
        if client is None:
            out[code] = {"text": template(sig), "source": "template", "created_at": now().isoformat(timespec="seconds")}
            continue
        try:
            resp = client.beta.messages.create(
                model=CLAUDE_MODEL, max_tokens=4000, system=SYSTEM,
                messages=[{"role": "user", "content": json.dumps(data, ensure_ascii=False, default=str)}],
                output_config={"effort": CLAUDE_EFFORT}, **llm.FALLBACK)
            if resp.stop_reason == "refusal":
                raise RuntimeError("请求被拒绝")
            text = "".join(b.text for b in resp.content if b.type == "text").strip()
            out[code] = {"text": text, "source": "claude", "model": CLAUDE_MODEL,
                         "created_at": now().isoformat(timespec="seconds")}
            db.set_meta(key, json.dumps(out[code], ensure_ascii=False))
        except Exception as e:  # noqa: BLE001
            log(f"  ! {code} 解读生成失败，改用模板：{e}")
            out[code] = {"text": template(sig), "source": "template", "created_at": now().isoformat(timespec="seconds")}
    log(f"解读：{sum(1 for v in out.values() if v['source'] == 'claude')} 个由 Claude 生成，"
        f"{sum(1 for v in out.values() if v['source'] == 'template')} 个用模板")
    return out
