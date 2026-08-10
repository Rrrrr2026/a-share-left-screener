#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
模块3 — 基本面抓取 (Fundamentals Pull)
======================================
对模块2命中的每只股票, 拉取并整理:
  估值: 市盈率TTM(+历史分位+行业中位对比), 市净率PB(+历史分位), 股息率
  盈利: EPS(最新+同比), ROE(最新+多年趋势), 营收/净利同比, 毛利率, 资产负债率
缺失值 -> None (前端显示 —), 绝不抛异常。
所有估值优先给"历史分位" (例如 PE 处于近X年12%分位 = 偏低)。
"""
from __future__ import annotations
import logging
import numpy as np
import pandas as pd

from . import datasource as ds
from .statutil import hist_percentile

log = logging.getLogger("ashare.module3")


def _last(series):
    s = pd.to_numeric(series, errors="coerce").dropna()
    return float(s.iloc[-1]) if len(s) else None


def compute_industry_pe_median(spot: pd.DataFrame, ind_to_codes: dict) -> dict:
    """给定快照(含每只 pe_ttm)和 行业->成分代码 映射, 算各行业 PE 中位数。"""
    out = {}
    if spot is None or spot.empty:
        return out
    pe_map = dict(zip(spot["code"], spot.get("pe_ttm", pd.Series(dtype=float))))
    for ind_name, codes in ind_to_codes.items():
        vals = [pe_map.get(c) for c in codes]
        vals = [v for v in vals if v is not None and not (isinstance(v, float) and np.isnan(v)) and v > 0]
        if vals:
            out[ind_name] = float(np.median(vals))
    return out


def pull_fundamentals(code: str, industry: str | None = None,
                      industry_pe_median: float | None = None,
                      spot_row: dict | None = None,
                      prosperity: float | None = None) -> dict:
    """返回基本面字典 (英文键)。任何字段拉取失败均降级为 None。"""
    res = {
        "pe_ttm": None, "pe_pct": None, "pe_industry_median": None, "pe_vs_industry": None,
        "pb": None, "pb_pct": None, "dividend_yield": None,
        "eps": None, "eps_yoy": None,
        "roe": None, "roe_trend": [],
        "revenue_yoy": None, "netprofit_yoy": None,
        "gross_margin": None, "debt_ratio": None,
        "growth_quality": None, "growth_quality_score": None,
        "growth_quality_note": None,
        # v2.1: 近四季 归母/营收 单季同比×4 (全市场业绩表批量) + 市场地位(编排层填)
        "ni_ttm_yoy": None, "ni_basis": None, "ni_qoq": [], "ni_q_labels": [],
        "rev_ttm_yoy": None, "rev_basis": None, "rev_qoq": [],
        "dominance_disp": None, "dom_rank": None, "dom_n": None, "dom_share": None,
        "fund_flags": [],
    }

    # ---- 近四季 归母净利/营收 单季同比×4 (来自全市场业绩表批量缓存, 带缺季防护) ----
    try:
        qser = ds.get_quarterly_series(code)
    except Exception:
        qser = {}
    if qser:
        res["ni_qoq"], res["ni_q_labels"], res["ni_ttm_yoy"], res["ni_basis"] = \
            _yoy4(qser.get("periods"), qser.get("ni_q"), qser.get("ni_cum"))
        res["rev_qoq"], _, res["rev_ttm_yoy"], res["rev_basis"] = \
            _yoy4(qser.get("periods"), qser.get("rev_q"), qser.get("rev_cum"))

    # ---- 估值历史: PE/PB 分位 + 股息 ----
    val = ds.fetch_valuation_hist(code)
    if val is not None and not val.empty:
        if "pe_ttm" in val.columns:
            res["pe_ttm"] = _last(val["pe_ttm"])
            res["pe_pct"] = _round(hist_percentile(val["pe_ttm"].tolist(), res["pe_ttm"]))
        elif "pe" in val.columns:
            res["pe_ttm"] = _last(val["pe"])
            res["pe_pct"] = _round(hist_percentile(val["pe"].tolist(), res["pe_ttm"]))
        if "pb" in val.columns:
            res["pb"] = _last(val["pb"])
            res["pb_pct"] = _round(hist_percentile(val["pb"].tolist(), res["pb"]))
        if "dv_ttm" in val.columns:
            res["dividend_yield"] = _last(val["dv_ttm"])

    # 快照兜底 PE/PB
    if res["pe_ttm"] is None and spot_row:
        res["pe_ttm"] = _clean(spot_row.get("pe_ttm"))
    if res["pb"] is None and spot_row:
        res["pb"] = _clean(spot_row.get("pb"))

    # 行业 PE 中位对比
    if industry_pe_median is not None and res["pe_ttm"] is not None and industry_pe_median > 0:
        res["pe_industry_median"] = round(float(industry_pe_median), 2)
        res["pe_vs_industry"] = round(res["pe_ttm"] / industry_pe_median, 2)

    # ---- 财务指标: ROE/EPS/增长/毛利/负债 ----
    fin = ds.fetch_financial_indicator(code)
    if fin is not None and not fin.empty:
        if "roe" in fin.columns:
            res["roe"] = _last(fin["roe"])

            def _roe_list(df):
                return [
                    {"date": (str(d.date()) if hasattr(d, "date") else str(d)),
                     "value": (None if pd.isna(v) else round(float(v), 2))}
                    for d, v in zip(df.get("date", pd.Series([None] * len(df))),
                                    pd.to_numeric(df["roe"], errors="coerce"))
                ]
            # 季度(近8季, 原口径) + 年度(取年报12-31行, 近6年) —— 供 ROE 图 年度/季度 切换
            res["roe_trend_q"] = _roe_list(fin.tail(8))
            try:
                _dts = pd.to_datetime(fin.get("date"), errors="coerce")
                _ann = fin[(_dts.dt.month == 12) & (_dts.dt.day == 31)]
                res["roe_trend"] = _roe_list(_ann.tail(6)) if not _ann.empty else _roe_list(fin.tail(8))
            except Exception:
                res["roe_trend"] = _roe_list(fin.tail(8))
        if "eps" in fin.columns:
            res["eps"] = _last(fin["eps"])
            eps_s = pd.to_numeric(fin["eps"], errors="coerce").dropna()
            if len(eps_s) >= 5 and eps_s.iloc[-5] not in (0, None):
                try:
                    res["eps_yoy"] = _round((eps_s.iloc[-1] / abs(eps_s.iloc[-5]) - 1.0) * 100.0)
                except Exception:
                    pass
        if "revenue_yoy" in fin.columns:
            res["revenue_yoy"] = _last(fin["revenue_yoy"])
        if "netprofit_yoy" in fin.columns:
            res["netprofit_yoy"] = _last(fin["netprofit_yoy"])
            if res["eps_yoy"] is None:
                res["eps_yoy"] = res["netprofit_yoy"]
        if "gross_margin" in fin.columns:
            res["gross_margin"] = _last(fin["gross_margin"])
        if "debt_ratio" in fin.columns:
            res["debt_ratio"] = _last(fin["debt_ratio"])
        # 增速质量: 最新增速是一次性还是可持续 (用整条序列判断, 不额外打接口)
        try:
            res.update(analyze_growth_quality(fin, prosperity))
        except Exception as e:  # noqa: BLE001
            log.debug("增速质量分析失败 %s: %s", code, e)

    res["fund_flags"] = _flags(res)
    return res


def _series(fin, col, n=8):
    if fin is None or col not in fin.columns:
        return []
    s = pd.to_numeric(fin[col], errors="coerce").dropna()
    return [float(x) for x in s.tail(n)]


def analyze_growth_quality(fin, prosperity: float | None = None) -> dict:
    """判断最新增速是「一次性」还是「可持续」。

    核心问题: 利润的增长有没有 *收入* 和 *毛利* 撑着?
      · 净利暴增而营收几乎不动  -> 多半来自卖资产/补助/投资收益/汇兑, 或去年低基数,
        这类增长明年大概率不复现 (one-time);
      · 营收与净利同向增长 + 毛利率不塌 + 连续多期 -> 经营驱动, 可持续性高。

    只用已有的财务指标序列(不额外打接口), 输出 0-100 分 + 标签 + 中文理由。
    数据不足(少于2期)时返回 None 标签, 前端显示 '—', 不猜。
    """
    rev = _series(fin, "revenue_yoy")
    npf = _series(fin, "netprofit_yoy")
    gm = _series(fin, "gross_margin")
    if len(rev) < 2 and len(npf) < 2:
        return {"growth_quality": None, "growth_quality_score": None,
                "growth_quality_note": None}

    r0 = rev[-1] if rev else None          # 最新营收同比
    n0 = npf[-1] if npf else None          # 最新归母净利同比
    score = 50.0
    pros, cons = [], []                     # 正面/负面理由

    # ---- 1) 收入与利润的匹配度 (最关键) ----
    if n0 is not None and r0 is not None:
        if n0 > 50 and r0 < 10:
            score -= 28
            cons.append(f"净利+{n0:.0f}%但营收仅{r0:+.0f}%(疑非经营性)")
        elif n0 > 0 and r0 <= 0:
            score -= 12
            cons.append("利润增长但营收未增(靠降本/非经常)")
        elif n0 > 0 and r0 > 0:
            score += 16
            pros.append("营收与净利同向增长")
            if 0.5 <= (n0 / r0 if r0 else 9) <= 3.0:
                score += 6
                pros.append("利润增速与收入匹配")
        elif n0 < 0 and r0 > 0:
            score -= 8
            cons.append("增收不增利(成本/费用侵蚀)")

    # ---- 2) 连续性: 近4期有几期正增长 ----
    def _pos_ratio(xs):
        w = xs[-4:]
        return (sum(1 for x in w if x > 0) / len(w)) if w else None
    pr, pn = _pos_ratio(rev), _pos_ratio(npf)
    if pr is not None and len(rev) >= 3:
        if pr >= 0.75:
            score += 12; pros.append("营收连续多期正增长")
        elif pr <= 0.25:
            score -= 10; cons.append("营收多期负增长")
    if pn is not None and len(npf) >= 3 and pn <= 0.25 and (n0 or 0) > 50:
        score -= 12
        cons.append("此前多期利润下滑, 本期突然暴增(基数效应)")

    # ---- 3) 稳定性: 营收增速波动越小越可信 ----
    if len(rev) >= 4:
        w = rev[-6:]
        sd = float(np.std(w))
        if sd < 15:
            score += 8; pros.append("增速平稳")
        elif sd > 60:
            score -= 8; cons.append("增速大起大落")

    # ---- 4) 毛利率趋势: 利润暴增却毛利下滑 = 更可疑 ----
    if len(gm) >= 3:
        base = float(np.mean(gm[-4:-1])) if len(gm) >= 4 else float(np.mean(gm[:-1]))
        d = gm[-1] - base
        if d >= 1.5:
            score += 8; pros.append(f"毛利率走高({d:+.1f}pct)")
        elif d <= -3.0:
            score -= 10; cons.append(f"毛利率下滑({d:+.1f}pct)")
            if (n0 or 0) > 50:
                score -= 6; cons.append("毛利下滑却利润暴增(存疑)")

    # ---- 5) 行业景气加成 (口径能对上时才有; 对不上不惩罚) ----
    if prosperity is not None:
        if prosperity >= 70:
            score += 6; pros.append("所处行业景气居前")
        elif prosperity <= 30:
            score -= 6; cons.append("所处行业景气靠后")

    score = float(max(0.0, min(100.0, score)))
    if score >= 66:
        tag = "🟢 可持续"
    elif score >= 40:
        tag = "🟡 待观察"
    else:
        tag = "🔴 一次性"
    note = "；".join((pros[:3] + cons[:3])) or "信号不明显"
    return {"growth_quality": tag,
            "growth_quality_score": round(score, 1),
            "growth_quality_note": note}


def _flags(r: dict) -> list:
    """生成中文基本面亮点/瑕疵标签 (供结论与详情展示)。"""
    flags = []
    if r.get("roe") is not None:
        if r["roe"] >= 18:
            flags.append("高ROE")
        elif r["roe"] < 0:
            flags.append("⚠️亏损/负ROE")
    if r.get("pe_pct") is not None and r["pe_pct"] <= 30:
        flags.append("估值偏低分位")
    if r.get("pe_ttm") is not None and (r["pe_ttm"] <= 0):
        flags.append("⚠️PE为负(亏损)")
    if r.get("netprofit_yoy") is not None and r["netprofit_yoy"] > 0:
        flags.append("净利正增长")
    elif r.get("netprofit_yoy") is not None and r["netprofit_yoy"] < -20:
        flags.append("⚠️净利下滑")
    if r.get("debt_ratio") is not None and r["debt_ratio"] >= 70:
        flags.append("⚠️高负债")
    return flags


def _round(x, n=2):
    if x is None:
        return None
    try:
        xf = float(x)
        return None if (np.isnan(xf) or np.isinf(xf)) else round(xf, n)
    except Exception:
        return None


def _clean(x):
    if x is None:
        return None
    try:
        xf = float(x)
        return None if (np.isnan(xf) or np.isinf(xf)) else xf
    except Exception:
        return None


def _pct_chg(now, prev):
    """同比/环比 (%): 负基数用 |prev| 作分母 (亏转盈得正增速), 基数近零不计。"""
    if now is None or prev is None or abs(prev) < 1e-6:
        return None
    return round((now - prev) / abs(prev) * 100.0, 1)


def _q_label(p: str) -> str:
    """'2025-09-30' -> '25Q3'。"""
    try:
        return f"{p[2:4]}Q{(int(p[5:7]) - 1) // 3 + 1}"
    except Exception:
        return p


def _consecutive_quarters(ps: list) -> bool:
    """检查报告期序列是否为连续自然季 (缺期时 TTM 的位置求和会错位跨年)。"""
    _next = {3: "-06-30", 6: "-09-30", 9: "-12-31", 12: "-03-31"}
    for a, b in zip(ps, ps[1:]):
        try:
            y, m = int(a[:4]), int(a[5:7])
        except Exception:
            return False
        if b != f"{y + (1 if m == 12 else 0)}{_next[m]}":
            return False
    return True


def _yoy4(periods, singles, cums, k: int = 4):
    """A股财报口径的增速套件:
    返回 (单季同比×最近k个[旧→新], 季度标签, 头条增速, 口径标签)。
    头条优先 真TTM同比(近4单季合计 vs 前4单季合计); 退化用 最新报告期累计同比。"""
    periods = periods or []
    singles = singles or []
    cums = cums or []
    by_p = {p: v for p, v in zip(periods, singles)}
    cum_by_p = {p: v for p, v in zip(periods, cums)}
    yoys, labels = [], []
    for p in periods[-k:]:
        prev_p = f"{int(p[:4]) - 1}{p[4:]}"
        yoys.append(_pct_chg(by_p.get(p), by_p.get(prev_p)))
        labels.append(_q_label(p))
    headline, basis = None, None
    if len(periods) >= 8 and _consecutive_quarters(periods[-8:]):
        last8 = [by_p.get(p) for p in periods[-8:]]
        if all(v is not None for v in last8):
            headline = _pct_chg(sum(last8[4:]), sum(last8[:4]))
            if headline is not None:
                basis = "TTM"
    if headline is None and periods:
        p = periods[-1]
        prev_p = f"{int(p[:4]) - 1}{p[4:]}"
        headline = _pct_chg(cum_by_p.get(p), cum_by_p.get(prev_p))
        if headline is not None:
            basis = "累计"
    return yoys, labels, headline, basis


