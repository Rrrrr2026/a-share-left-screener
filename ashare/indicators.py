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


# ---------------------------------------------------------------------------
#  风控 / 波动 / 支撑增强
# ---------------------------------------------------------------------------
def atr(high: pd.Series, low: pd.Series, close: pd.Series, n: int = 14) -> pd.Series:
    """真实波幅均值 ATR。"""
    h, l, c = high.astype(float), low.astype(float), close.astype(float)
    pc = c.shift(1)
    tr = pd.concat([(h - l), (h - pc).abs(), (l - pc).abs()], axis=1).max(axis=1)
    return tr.rolling(n).mean()


def atr_pct(high: pd.Series, low: pd.Series, close: pd.Series, n: int = 14) -> float:
    """ATR 占现价百分比 (波动率代理)。"""
    a = atr(high, low, close, n)
    av = safe_last(a)
    px = float(close.iloc[-1])
    return float(av / px * 100.0) if (px and not np.isnan(av)) else np.nan


def max_drawdown(close: pd.Series, bars: int = 250) -> float:
    """最近 bars 根的最大回撤 (%, 负值)。"""
    s = close.tail(bars).astype(float).dropna()
    if len(s) < 5:
        return np.nan
    dd = (s / s.cummax() - 1.0)
    return float(dd.min() * 100.0)


def beta(stock_close: pd.Series, bench_close: pd.Series, bars: int = 120) -> float:
    """相对基准的 Beta。
    两序列若带日期索引(如 'YYYY-MM-DD'), 先按日期求交集严格对齐(个股停牌/两源日历不一致
    时按位置对齐会把不同交易日的收益配对, 算出错误Beta); 整数索引则退回末端位置对齐。
    协方差与方差统一用 ddof=1, 避免 n/(n-1) 的系统性偏差。"""
    sr = stock_close.astype(float).pct_change().dropna()
    br = bench_close.astype(float).pct_change().dropna()
    date_like = not (sr.index.dtype.kind in "iu" and br.index.dtype.kind in "iu")
    if date_like:
        common = sr.index.intersection(br.index)
        if len(common) < 20:
            return np.nan
        sr, br = sr.loc[common], br.loc[common]
    n = min(len(sr), len(br), bars)
    if n < 20:
        return np.nan
    sr, br = sr.tail(n).values, br.tail(n).values
    var = float(np.var(br, ddof=1))
    if var == 0:
        return np.nan
    return float(np.cov(sr, br)[0, 1] / var)


def bollinger_lower(close: pd.Series, n: int = 20, k: float = 2.0) -> pd.Series:
    ma = close.rolling(n).mean()
    sd = close.rolling(n).std()
    return ma - k * sd


def fib_levels(hi: float, lo: float) -> dict:
    """从区间高->低的斐波那契回撤价 (支撑参考)。"""
    rng = float(hi) - float(lo)
    return {"f382": round(hi - 0.382 * rng, 3),
            "f500": round(hi - 0.5 * rng, 3),
            "f618": round(hi - 0.618 * rng, 3)}


def downsample(series: pd.Series, points: int = 40) -> list:
    """把序列均匀降采样到 points 个点 (行内 sparkline 用)。"""
    s = series.dropna().astype(float)
    if len(s) == 0:
        return []
    if len(s) <= points:
        return [round(float(x), 3) for x in s.values]
    idx = np.linspace(0, len(s) - 1, points).astype(int)
    return [round(float(s.values[i]), 3) for i in idx]
