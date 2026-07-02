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
                      spot_row: dict | None = None) -> dict:
    """返回基本面字典 (英文键)。任何字段拉取失败均降级为 None。"""
    res = {
        "pe_ttm": None, "pe_pct": None, "pe_industry_median": None, "pe_vs_industry": None,
        "pb": None, "pb_pct": None, "dividend_yield": None,
        "eps": None, "eps_yoy": None,
        "roe": None, "roe_trend": [],
        "revenue_yoy": None, "netprofit_yoy": None,
        "gross_margin": None, "debt_ratio": None,
        "fund_flags": [],
    }

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
            roe_s = pd.to_numeric(fin["roe"], errors="coerce")
            tail = fin.tail(8)
            res["roe_trend"] = [
                {"date": (str(d.date()) if hasattr(d, "date") else str(d)),
                 "value": (None if pd.isna(v) else round(float(v), 2))}
                for d, v in zip(tail.get("date", pd.Series([None] * len(tail))),
                                pd.to_numeric(tail["roe"], errors="coerce"))
            ]
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

    res["fund_flags"] = _flags(res)
    return res


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
