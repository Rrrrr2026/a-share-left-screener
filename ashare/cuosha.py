#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
模块8 — 错杀检测 (Unfair-Selloff Detector)
==========================================
回答筛选器一直没回答的问题: 这只股票**为什么**跌?
高质量公司在情绪性抛售中被"错杀"时, 价格与基本面出现背离 —— 股价大跌而
官方单季利润/营收仍在增长。本模块给每只候选打 0-100 的"错杀分":

  背离度   40分  跌得越深(距52周高点)、基本面越硬(单季同比/TTM同比), 分越高
  板块归因 25分  跌幅有多大比例能用同行业候选的中位跌幅解释 —— 随板块一起跌
                的是情绪/资金面(可能错杀); 远超板块的跌大概率有个股原因(低分)
  质量护城河 25分  成长质量档位 / ROE / 行业营收排名 / 自身PE历史分位处低位
  企稳迹象 10分  贴近支撑、RSI脱离极弱区、多次踩支撑

  修复空间 = 只回补"超出板块的那部分跌幅"的涨幅 (回到板块合理水位), 是对
             "回归合理"的保守估计, 不预测板块本身修复。

准入门槛(不达标不打分): 距52周高点回撤>=18%; 基本面得分居当日候选前40%;
近两季/TTM无明显盈利恶化(任一 < -10% 即出局); 成长质量非🔴; 非"基本面弱"标签。

所有输入字段在每天的历史快照里都有, 因此回测可对过去每天重算错杀分,
立刻得到"错杀候选 vs 其它信号"的真实胜率对比 (by_cuosha 分段) —— 论点可被证伪。
⚠️ 已知盲区: 财报滞后 —— 下跌可能反映尚未公布的恶化。结论只是"无可见基本面
恶化", 不是"确认错杀", 重仓前仍需人工查下跌原因(公告/舆情/政策)。
"""
from __future__ import annotations

MIN_DD = 0.18            # 距52周高点最小回撤 (比例)
DD_FULL = 0.50           # 回撤到该深度, 背离度的深度因子拉满
SCORE_MIN = 60           # 达标线: >= 该分才标记为错杀候选
IND_MIN_MEMBERS = 4      # 留一后同行候选少于该数 -> 不做板块归因 (不许拿全体冒充行业)
FUND_PCTL = 0.60         # 基本面得分须 >= 当日候选的60分位 (前40%)
DETERIORATE = -10.0      # 近两季均值/TTM同比 任一低于该值 = 可见恶化, 出局

# 成长质量 -> 档位 (A股三色标签)
GROWTH_TIER = {"🟢 可持续": "G", "🟡 待观察": "M", "🔴 一次性": "W"}
WEAK_TAG = "技术好但基本面弱"


def _num(x):
    return x if isinstance(x, (int, float)) and x == x else None


def _dd_now(c) -> float | None:
    px, hi = _num(c.get("price")), _num(c.get("high_52w"))
    if not px or not hi or hi <= 0 or px <= 0:
        return None
    return px / hi - 1.0          # 负数 (比例)


def industry_dd_lists(cands) -> dict:
    """行业 -> [各候选回撤(比例,负数)...]。归因时留一法(剔除自身)取中位:
    ① 防止小行业里深跌股把中位拖向自己、"自己解释自己" (内生性);
    ② 同行候选不足 4 只时不归因 (expl=0), 绝不用全体候选中位冒充行业 ——
       否则恰恰是个股暴雷高发的冷门行业会被伪造出"板块解释"。
    已知偏差(如实承认): 候选池本身是被筛出来的弱势股, 其行业中位跌幅系统性
    深于真实行业中位 -> expl 整体偏高。该偏差对所有日期一致, 且回测的
    三段对比(达标/过门槛未达标/其它)控制了门槛效应, 用于验证打分本身。"""
    by = {}
    for c in cands:
        d = _dd_now(c)
        if d is None:
            continue
        by.setdefault(c.get("industry") or "?", []).append(d)
    return by


def _loo_median(vals: list, own: float) -> float | None:
    """留一中位: 剔除一次自身值; 剩余样本 < IND_MIN_MEMBERS 则判为无法归因。"""
    rest = list(vals)
    try:
        rest.remove(own)
    except ValueError:
        pass
    if len(rest) < IND_MIN_MEMBERS:
        return None
    rest.sort()
    return rest[len(rest) // 2]


def _fund_threshold(cands) -> float | None:
    fs = sorted(f for f in (_num(c.get("fund_score")) for c in cands) if f is not None)
    if not fs:
        return None
    return fs[min(len(fs) - 1, int(len(fs) * FUND_PCTL))]


def score_one(c: dict, ind_dd: float | None, fund_thr: float | None) -> dict | None:
    dd = _dd_now(c)
    if dd is None or dd > -MIN_DD:
        return None
    if WEAK_TAG in (c.get("tag") or ""):
        return None
    if GROWTH_TIER.get(c.get("growth_quality"), "NA") == "W":
        return None
    fs = _num(c.get("fund_score"))
    if fund_thr is not None and (fs is None or fs < fund_thr):
        return None

    # 基本面趋势: 官方单季同比近两季均值 + TTM同比; 任一明显为负 = 可见恶化
    qoq = [v for v in (c.get("ni_qoq") or []) if _num(v) is not None]
    recent = sum(qoq[-2:]) / len(qoq[-2:]) if qoq else None
    ttm = _num(c.get("ni_ttm_yoy"))
    if recent is None and ttm is None:
        return None                      # 无基本面数据, 谈不上"错杀"
    if (recent is not None and recent < DETERIORATE) or (ttm is not None and ttm < DETERIORATE):
        return None
    g = max(v for v in (recent, ttm) if v is not None)          # 增速代表值(%)
    f01 = min(1.0, 0.35 + max(0.0, g) / 50.0)                   # 增速50%+拉满

    # 1) 背离度 40
    d01 = min(1.0, max(0.0, (-dd - MIN_DD) / (DD_FULL - MIN_DD)))
    s_div = 40.0 * (0.35 + 0.65 * d01) * f01

    # 2) 板块归因 25: 跌幅被同行(留一)中位解释的比例; 无法归因时为 0
    expl = 0.0
    attributable = ind_dd is not None
    if attributable and ind_dd < 0:
        expl = min(1.0, max(0.0, ind_dd / dd))
    s_ind = 25.0 * expl

    # 3) 质量护城河 25
    tier = GROWTH_TIER.get(c.get("growth_quality"), "NA")
    pts = {"G": 9.0, "M": 4.0}.get(tier, 0.0)
    roe = _num(c.get("roe"))
    if roe is not None:
        pts += 7.0 if roe >= 15 else (4.0 if roe >= 8 else 0.0)
    dr = _num(c.get("dom_rank"))
    if dr is not None:
        pts += 5.0 if dr <= 3 else (3.0 if dr <= 5 else 0.0)
    pep = _num(c.get("pe_pct"))                                 # 自身PE历史分位
    if pep is not None:
        pts += 4.0 if pep <= 25 else (2.0 if pep <= 40 else 0.0)
    s_moat = min(25.0, pts)

    # 4) 企稳迹象 10
    st = 0.0
    dsp = _num(c.get("dist_support_pct"))                       # 距支撑 (%)
    if dsp is not None and abs(dsp) <= 3.0:
        st += 4.0
    rsi = _num(c.get("rsi"))
    if rsi is not None and 30.0 <= rsi <= 55.0:
        st += 3.0
    tou = _num(c.get("supp_touches"))
    if tou is not None and tou >= 3:
        st += 3.0
    s_stab = min(10.0, st)

    score = round(s_div + s_ind + s_moat + s_stab)

    # 修复空间: 只回补超出板块的那部分跌幅
    upside = 0.0
    if ind_dd is not None and ind_dd > dd:
        upside = min(0.80, (1.0 + ind_dd) / (1.0 + dd) - 1.0)

    bits = [f"距52周高点-{-dd * 100:.0f}%"]
    if not attributable:
        bits.append("同行样本不足, 无法做板块归因")
    else:
        bits.append(f"板块解释{expl * 100:.0f}%" if expl > 0 else "跌幅几乎全为个股因素")
    if recent is not None:
        bits.append(f"近两季单季同比均值{recent:+.0f}%")
    if ttm is not None:
        bits.append(f"TTM同比{ttm:+.0f}%")
    if roe is not None:
        bits.append(f"ROE {roe:.0f}")
    if dr is not None and dr <= 5:
        bits.append(f"行业营收第{int(dr)}名")
    if pep is not None:
        bits.append(f"PE处自身历史{pep:.0f}%分位")
    note = " · ".join(bits) + " · 财报有滞后, 重仓前请人工查下跌原因"

    return {"score": score, "note": note, "upside": round(upside * 100.0, 1),
            "expl_pct": round(expl * 100.0) if attributable else None,
            "dd_pct": round(dd * 100.0, 1)}


_FIELDS = ("cuosha_score", "cuosha_note", "cuosha_upside", "cuosha_expl",
           "cuosha_dd", "cuosha_eligible")


def annotate(cands: list[dict]) -> int:
    """就地标注; 返回达标数。先清掉旧字段再重算 —— 历史快照里存过老版本打分,
    不清场的话回测会把新老两套规则的标注混在一个分段里, 无法比较版本。
    cuosha_eligible=1 标记"过了准入门槛"(不论分数): 回测按 达标/过门槛未达标/
    其它 三段对比, 过门槛组固定了门槛效应, 才能分离出打分本身的贡献。"""
    for c in cands:
        for k in _FIELDS:
            c.pop(k, None)
    ind_lists = industry_dd_lists(cands)
    thr = _fund_threshold(cands)
    n = 0
    for c in cands:
        own = _dd_now(c)
        peers = ind_lists.get(c.get("industry") or "?", [])
        idd = _loo_median(peers, own) if own is not None else None
        r = score_one(c, idd, thr)
        if r is None:
            continue
        c["cuosha_eligible"] = 1
        if r["score"] >= SCORE_MIN:
            c["cuosha_score"] = r["score"]
            c["cuosha_note"] = r["note"]
            c["cuosha_upside"] = r["upside"]
            c["cuosha_expl"] = r["expl_pct"]
            c["cuosha_dd"] = r["dd_pct"]
            n += 1
    return n
