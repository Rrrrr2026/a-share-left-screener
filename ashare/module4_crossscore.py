#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
模块4 — 技术 × 基本面 交叉打分 (Cross-Score)
=============================================
综合分 = 技术分(归一0-100) * w_tech + 基本面分(0-100) * w_fund + 景气分(0-100) * w_prosperity
并产出:
  结论标签: ✅强左侧 / ⚠️技术好但基本面弱 / 🔎观察
  关键位中文描述: 建议关注支撑价 / 距现价空间% / 破位参考位
  一句话结论 + 基本面亮点/瑕疵
"""
from __future__ import annotations
import numpy as np

from .config import CONFIG
from .statutil import clamp

# 技术分理论上限 = 各信号权重之和 (满分命中)
_TECH_MAX = sum(CONFIG["tech"]["weights"].values())


def _fund_score(f: dict) -> float:
    """基本面 0-100 评分。"""
    cc = CONFIG["cross"]
    s = 50.0
    roe = f.get("roe")
    if roe is not None:
        if roe >= cc["roe_excellent"]:
            s += 20
        elif roe >= cc["roe_good"]:
            s += 12
        elif roe < 0:
            s -= 20
    pe = f.get("pe_ttm")
    pe_pct = f.get("pe_pct")
    pe_positive = (pe is None) or (pe > 0)
    if pe_pct is not None:
        if pe_pct <= cc["pe_low_percentile"] and pe_positive:
            s += 12   # 仅当 PE 为正才算"估值偏低"; 负PE(亏损)不给便宜加分
        elif pe_pct >= cc["pe_high_percentile"]:
            s -= 10
    if pe is not None and pe <= 0:
        s -= 10       # 亏损/负PE 扣分
    npy = f.get("netprofit_yoy")
    if npy is not None:
        if npy > cc["netprofit_yoy_good"]:
            s += 8
        elif npy < -20:
            s -= 10
    debt = f.get("debt_ratio")
    if debt is not None and debt >= cc["debt_ratio_warn"]:
        s -= 8
    gm = f.get("gross_margin")
    if gm is not None and gm >= 40:
        s += 5
    return round(clamp(s, 0.0, 100.0), 1)


def _tag(tech_score: float, fund_score: float, prosperity: float) -> str:
    cc = CONFIG["cross"]
    # 景气未知(全市场回退, prosperity=None)时, 无法验证"高景气", 只按 技术+基本面 判定;
    # 此时展示的景气分为 "—"(见 cross_score), 不会伪称已通过 60 分位门槛。
    if (tech_score >= cc["strong_left_tech"] and fund_score >= cc["strong_left_fund"]
            and (prosperity is None or prosperity >= cc["strong_left_prosperity"])):
        return "✅ 强左侧"
    if tech_score >= cc["strong_left_tech"] and fund_score < cc["fund_weak_threshold"]:
        return "⚠️ 技术好但基本面弱"
    return "🔎 观察"


_TAG_EN = {"✅ 强左侧": "✅ Strong Left",
           "⚠️ 技术好但基本面弱": "⚠️ Tech-strong, Weak Fundamentals",
           "🔎 观察": "🔎 Watch",
           "🪸 深跌抄底": "🪸 Deep-Dip Bottom-Fish"}
_DIP_CONFIRM_EN = {"底背离": "bullish divergence", "缩柱": "shrinking MACD histogram",
                   "金叉": "KDJ golden cross", "放量": "volume spike"}
_OSC_EN = {"超卖": "oversold", "缩柱": "shrinking MACD histogram", "底背离": "bullish divergence"}
_SUPP_EN = {"通道下轨": "channel lower band", "前低": "prior low", "布林下轨": "Bollinger lower band"}
_FLAG_EN = {"高ROE": "high ROE", "⚠️亏损/负ROE": "⚠️ loss / negative ROE",
            "估值偏低分位": "low valuation percentile", "⚠️PE为负(亏损)": "⚠️ negative P/E (loss-making)",
            "净利正增长": "positive net-profit growth", "⚠️净利下滑": "⚠️ net-profit decline",
            "⚠️高负债": "⚠️ high debt",
            # 兼容美股口径
            "盈利正增长": "positive earnings growth", "⚠️盈利下滑": "⚠️ earnings decline",
            "高毛利": "high gross margin", "⚠️高杠杆": "⚠️ high leverage"}


def _osc_en(s):
    parts = [v for k, v in _OSC_EN.items() if k in (s or "")]
    return ", ".join(parts) if parts else (s or "")


def _supp_en(label):
    for k, v in _SUPP_EN.items():
        if label and label.startswith(k):
            return v
    return label or "support"


def _dip_conclusion(tech_rec: dict, f: dict, tag: str) -> str:
    """深跌抄底桶的中文一句话结论: 深跌幅度 + 超卖 + 位置 + 见底确认 + 破位参考。"""
    parts = [tag + "："]
    seg = []
    if tech_rec.get("drawdown_pct") is not None:
        seg.append(f"自高点回撤{tech_rec['drawdown_pct']:.0f}%")
    if tech_rec.get("rsi") is not None:
        seg.append(f"RSI {tech_rec['rsi']:.0f}(超卖)")
    if tech_rec.get("pos_52w_pct") is not None:
        seg.append(f"处52周区间底部{tech_rec['pos_52w_pct']:.0f}%")
    parts[0] += "、".join(seg) if seg else "深跌超卖"
    conf = tech_rec.get("dip_confirm")
    parts.append("见底确认：" + conf if conf else "尚无见底确认(接刀需谨慎)")
    if tech_rec.get("breakdown_price") is not None:
        parts.append(f"破位参考{tech_rec['breakdown_price']}(跌破继续走弱)")
    flags = f.get("fund_flags") or []
    if flags:
        parts.append("基本面：" + "、".join(flags))
    return "；".join(parts) + "。"


def _dip_conclusion_en(tech_rec: dict, f: dict, tag: str) -> str:
    parts = [_TAG_EN.get(tag, tag) + ": "]
    seg = []
    if tech_rec.get("drawdown_pct") is not None:
        seg.append(f"{tech_rec['drawdown_pct']:.0f}% off the high")
    if tech_rec.get("rsi") is not None:
        seg.append(f"RSI {tech_rec['rsi']:.0f} (oversold)")
    if tech_rec.get("pos_52w_pct") is not None:
        seg.append(f"bottom {tech_rec['pos_52w_pct']:.0f}% of 52w range")
    parts[0] += ", ".join(seg) if seg else "deeply oversold"
    conf = tech_rec.get("dip_confirm") or ""
    conf_en = ", ".join(_DIP_CONFIRM_EN.get(k, k) for k in ("底背离", "缩柱", "金叉", "放量") if k in conf)
    parts.append("bottoming signals: " + conf_en if conf_en else "no bottoming signal yet (catching a falling knife — caution)")
    if tech_rec.get("breakdown_price") is not None:
        parts.append(f"breakdown ref {tech_rec['breakdown_price']} (a break lower = further weakness)")
    flags = f.get("fund_flags") or []
    if flags:
        parts.append("fundamentals: " + ", ".join(_FLAG_EN.get(x, x) for x in flags))
    return "; ".join(parts) + "."


def _conclusion_text_en(tech_rec: dict, f: dict, tag: str) -> str:
    """英文一句话结论 (与中文版结构对应)。"""
    if tag.startswith("🪸"):
        return _dip_conclusion_en(tech_rec, f, tag)
    sigs = []
    if tech_rec.get("sig_channel"):
        sigs.append("near the rising-channel lower band")
    if tech_rec.get("sig_pivot"):
        sigs.append("near a prior low")
    if tech_rec.get("sig_ma"):
        sigs.append(f"pullback to {tech_rec['sig_ma']}")
    if tech_rec.get("sig_osc"):
        sigs.append(_osc_en(tech_rec["sig_osc"]))
    sig_txt = ", ".join(sigs) if sigs else "no strong support signal"

    parts = [f"{_TAG_EN.get(tag, tag)}: technically {sig_txt}"]
    if tech_rec.get("support_price") is not None:
        parts.append(f"watch support ≈ {tech_rec['support_price']} ({_supp_en(tech_rec.get('support_label'))})")
    if tech_rec.get("dist_support_pct") is not None:
        parts.append(f"~{tech_rec['dist_support_pct']}% from support")
    if tech_rec.get("breakdown_price") is not None:
        parts.append(f"breakdown ref {tech_rec['breakdown_price']} (a break below = failed pattern / stop)")
    flags = f.get("fund_flags") or []
    if flags:
        parts.append("fundamentals: " + ", ".join(_FLAG_EN.get(x, x) for x in flags))
    return "; ".join(parts) + "."


def _conclusion_text(tech_rec: dict, f: dict, tag: str) -> str:
    """一句话中文结论: 哪些信号命中 + 关键支撑 + 破位参考 + 基本面亮点/瑕疵。"""
    if tag.startswith("🪸"):
        return _dip_conclusion(tech_rec, f, tag)
    sigs = []
    if tech_rec.get("sig_channel"):
        sigs.append("贴近上升通道下轨")
    if tech_rec.get("sig_pivot"):
        sigs.append("接近前期低点")
    if tech_rec.get("sig_ma"):
        sigs.append(f"回踩{tech_rec['sig_ma']}")
    if tech_rec.get("sig_osc"):
        sigs.append(tech_rec["sig_osc"])
    sig_txt = "、".join(sigs) if sigs else "暂无强支撑信号"

    parts = [f"{tag}：技术面{sig_txt}"]
    if tech_rec.get("support_price") is not None:
        sp = tech_rec.get("support_label") or "支撑"
        parts.append(f"建议关注支撑价≈{tech_rec['support_price']}({sp})")
    if tech_rec.get("dist_support_pct") is not None:
        parts.append(f"距支撑约{tech_rec['dist_support_pct']}%")
    if tech_rec.get("breakdown_price") is not None:
        parts.append(f"破位参考{tech_rec['breakdown_price']}(跌破即形态失败止损)")
    flags = f.get("fund_flags") or []
    if flags:
        parts.append("基本面：" + "、".join(flags))
    return "；".join(parts) + "。"


def cross_score(tech_rec: dict, fund: dict, prosperity_score: float | None) -> dict:
    """合并技术记录 + 基本面 + 景气, 返回最终 final_rank 记录 (英文键)。"""
    cc = CONFIG["cross"]
    tech_score = float(tech_rec.get("tech_score") or 0.0)
    tech_norm = clamp(tech_score / _TECH_MAX * 100.0, 0.0, 100.0) if _TECH_MAX else 0.0
    fund_score = _fund_score(fund)
    # 景气未知时, 仅用 50 作为综合分的中性占位(排序用); 但标签与展示仍以真实值(None)为准
    prosp_for_score = prosperity_score if prosperity_score is not None else 50.0

    final = (cc["w_tech"] * tech_norm
             + cc["w_fund"] * fund_score
             + cc["w_prosperity"] * prosp_for_score)
    final = round(final, 2)

    tag = _tag(tech_score, fund_score, prosperity_score)
    # 深跌抄底: 仅当支撑模型只把它当"观察"(没有真支撑信号)时才改挂 🪸 标签,
    # 已是 ✅强左侧 / ⚠️技术好但基本面弱 的(确有支撑结构)保留原标签, 不抢标。
    if tech_rec.get("dip") and tag == "🔎 观察":
        tag = "🪸 深跌抄底"
    text = _conclusion_text(tech_rec, fund, tag)
    text_en = _conclusion_text_en(tech_rec, fund, tag)

    return {
        "code": tech_rec["code"],
        "name": tech_rec["name"],
        "industry": tech_rec.get("industry"),
        "tag": tag,
        "final_score": final,
        "dip": bool(tech_rec.get("dip")),
        "dip_score": round(float(tech_rec.get("dip_score") or 0.0), 3),
        "dip_confirm": tech_rec.get("dip_confirm") or "",
        "tech_score": round(tech_score, 3),
        "tech_norm": round(tech_norm, 1),
        "fund_score": fund_score,
        # 展示真实景气分: 未知则为 None -> 前端显示 "—" (不再伪造 50)
        "prosperity_score": (round(prosperity_score, 2) if prosperity_score is not None else None),
        "conclusion": text,
        "conclusion_en": text_en,
    }
