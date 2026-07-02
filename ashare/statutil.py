#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
统计小工具 (Stats utilities)
============================
横截面百分位 / zscore / nan 安全均值。
模块1的五大支柱归一化、模块4的估值历史分位都用到。
"""
from __future__ import annotations
import numpy as np
import pandas as pd


def nanmean(values) -> float:
    arr = np.array([v for v in values if v is not None], dtype=float)
    arr = arr[~np.isnan(arr)]
    return float(arr.mean()) if len(arr) else np.nan


def zscore(series: pd.Series) -> pd.Series:
    s = pd.to_numeric(series, errors="coerce")
    mu = s.mean(skipna=True)
    sd = s.std(skipna=True)
    if not sd or np.isnan(sd):
        return pd.Series(np.zeros(len(s)), index=s.index)
    return (s - mu) / sd


def cross_sectional_percentile(series: pd.Series, fill=50.0) -> pd.Series:
    """把一列横截面数值转成 0-100 百分位 (越大越高)。对异常值稳健。
    fill: 缺失值填充。默认 50(中位); 传 None 则保留 NaN ——
    用于"某支柱无数据的行不应被给中位分、其权重应被重新分配"的场景。"""
    s = pd.to_numeric(series, errors="coerce")
    # rank: 平均名次法, 然后映射到 0-100
    pct = s.rank(method="average", pct=True) * 100.0
    if fill is None:
        return pct
    return pct.fillna(fill)


def hist_percentile(history, current) -> float:
    """当前值在历史序列中的分位 (0-100). 用于 "PE 处于近3年 12% 分位"。
    分位越低代表当前越便宜 (历史上更低的值占比)。"""
    arr = np.array([v for v in history if v is not None], dtype=float)
    arr = arr[~np.isnan(arr)]
    if len(arr) < 5 or current is None or (isinstance(current, float) and np.isnan(current)):
        return np.nan
    return float((arr <= current).mean() * 100.0)


def clamp(x, lo, hi):
    return max(lo, min(hi, x))


def safe_div(a, b, default=np.nan):
    try:
        if b == 0 or b is None:
            return default
        return a / b
    except Exception:
        return default
