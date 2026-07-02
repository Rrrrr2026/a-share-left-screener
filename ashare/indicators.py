#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
技术指标 (Technical indicators)
===============================
复用并扩展参考实现 a_share_left_screener.py 的指标函数:
  ema / macd / rsi / find_pivot_lows  —— 原样保留语义
新增:
  kdj / linreg_channel / reg_slope_norm / cumulative_return
所有函数对 NaN / 数据不足做安全降级, 不抛异常打断流程。
"""

from __future__ import annotations
import numpy as np
import pandas as pd


# ---------------------------------------------------------------------------
#  基础: 来自参考实现 (语义保持一致)
# ---------------------------------------------------------------------------
def ema(s: pd.Series, n: int) -> pd.Series:
    return s.ewm(span=n, adjust=False).mean()


def macd(close: pd.Series, fast: int = 12, slow: int = 26, signal: int = 9):
    """返回 (dif, dea, hist). hist 为通用 MACD 柱 = (dif-dea)*2。"""
    dif = ema(close, fast) - ema(close, slow)
    dea = ema(dif, signal)
    hist = (dif - dea) * 2.0
    return dif, dea, hist


def rsi(close: pd.Series, n: int = 14) -> pd.Series:
    diff = close.diff()
    up = diff.clip(lower=0).rolling(n).mean()
    dn = (-diff.clip(upper=0)).rolling(n).mean()
    rs = up / dn.replace(0, np.nan)
    return 100 - 100 / (1 + rs)


def find_pivot_lows(low: pd.Series, window: int):
    """返回摆动低点的 (索引, 价格) 列表: 左右 window 根内最低。"""
    lows = []
    vals = np.asarray(low.values, dtype=float)
    n = len(vals)
    for i in range(window, n - window):
        seg = vals[i - window:i + window + 1]
        if vals[i] == seg.min():
            lows.append((i, float(vals[i])))
    return lows


# ---------------------------------------------------------------------------
#  新增指标
# ---------------------------------------------------------------------------
def kdj(high: pd.Series, low: pd.Series, close: pd.Series,
        n: int = 9, k_period: int = 3, d_period: int = 3):
    """
    KDJ(9,3,3). 返回 (K, D, J) 三个 Series。
    RSV = (C - Ln) / (Hn - Ln) * 100; K=EMA(RSV), D=EMA(K), J=3K-2D。
    """
    low_n = low.rolling(n, min_periods=1).min()
    high_n = high.rolling(n, min_periods=1).max()
    rng = (high_n - low_n).replace(0, np.nan)
    rsv = (close - low_n) / rng * 100.0
    rsv = rsv.fillna(50.0)
    # 通达信常用: K = SMA(RSV, 3, 1) 的递推等价于 alpha=1/3 的 EMA
    k = rsv.ewm(alpha=1.0 / k_period, adjust=False).mean()
    d = k.ewm(alpha=1.0 / d_period, adjust=False).mean()
    j = 3.0 * k - 2.0 * d
    return k, d, j


def kdj_tag(k: float, d: float, j: float) -> str:
    """KDJ 金叉/死叉 + 超买/超卖 中文标签。"""
    if any(v is None or (isinstance(v, float) and np.isnan(v)) for v in (k, d, j)):
        return "—"
    parts = []
    if k > d:
        parts.append("金叉")
    elif k < d:
        parts.append("死叉")
    if j >= 90 or k >= 80:
        parts.append("超买")
    elif j <= 10 or k <= 20:
        parts.append("超卖")
    return "/".join(parts) if parts else "中性"


def linreg_channel(close: pd.Series, window: int, k: float):
    """
    对最近 window 根 close 做线性回归, 返回上升通道信息:
      dict(slope, intercept, line(np.array, 同window长度),
           lower_band(末根下轨值), lower_series(整窗口下轨), resid_std, uptrend)
    数据不足返回 None。
    """
    seg = close.tail(window).reset_index(drop=True)
    if len(seg) < max(20, window // 4):
        return None
    x = np.arange(len(seg))
    y = seg.values.astype(float)
    slope, intercept = np.polyfit(x, y, 1)
    pred = slope * x + intercept
    resid_std = float((y - pred).std())
    lower_series = pred - k * resid_std
    return {
        "slope": float(slope),
        "intercept": float(intercept),
        "line": pred,
        "lower_series": lower_series,
        "lower_band": float(lower_series[-1]),
        "resid_std": resid_std,
        "uptrend": slope > 0,
    }


def reg_slope_norm(series: pd.Series, window: int) -> float:
    """最近 window 根的线性回归斜率, 用均价归一 (≈每根的相对涨幅)。"""
    seg = series.tail(window).dropna()
    if len(seg) < max(5, window // 4):
        return np.nan
    x = np.arange(len(seg))
    y = seg.values.astype(float)
    slope = np.polyfit(x, y, 1)[0]
    denom = np.nanmean(y)
    return float(slope / denom) if denom else np.nan


def cumulative_return(close: pd.Series, bars: int) -> float:
    """最近 bars 根的累计涨跌幅 (%). 数据不足返回 NaN。"""
    s = close.dropna()
    if len(s) <= bars:
        return np.nan
    return float((s.iloc[-1] / s.iloc[-1 - bars] - 1.0) * 100.0)


def safe_last(series: pd.Series, default=np.nan):
    try:
        v = float(series.iloc[-1])
        return v if not np.isnan(v) else default
    except Exception:
        return default
