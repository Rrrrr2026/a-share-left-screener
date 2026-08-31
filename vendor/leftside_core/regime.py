#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
市场状态标定 (Regime) — R0 度量底座
=====================================
从 pricestore.idx_bars (沪深300 / SPY) 逐日标定:
  趋势三态  bull / bear / range
  波动率三态 low / mid / high
每个交易日的标签只用"截至当日收盘"的数据 (无前视), 规则全部事前定死、
参数透明 —— 这是右侧/波段策略回测"分 regime 报告"的统一口径, 也是将来
市场状态路由器的度量地基。

三个候选趋势规则族 (R0 标定报告横向对比后定稿一个生产口径):
  ma50slope: 收盘 vs MA50, 且 MA50 的 20bar 斜率 ±0.5% 定牛熊, 其余震荡
  dualma:    收盘 vs MA200 + MA50 vs MA200 同向定牛熊, 分歧即震荡
  hilo:      距 250bar 收盘高点回撤 <=10% 牛 / >=20% 熊 / 之间震荡

选型原则 (事前声明): 简单 + 稳定 (少抽鞭、状态持续时间合理、状态间已实现
波动率区分清晰), **不以任何策略的回测表现选型** —— 否则 regime 门就成了
拟合出来的参数。标定报告里的前向收益仅作评估展示, 不进任何交易规则。

旧口径不受影响: coilscan/slscan 的 by_regime (收盘>=MA50 两态) 各自内置,
继续可复现历史报告。
"""
from __future__ import annotations

import numpy as np

from . import pricestore as ps

TREND_STATES = ("bull", "range", "bear")
VOL_STATES = ("low", "mid", "high")
NA = "na"

# 各规则族默认参数 (标定报告会对每个数值参数 ±20% 做稳定性检验)
P_MA50SLOPE = {"ma_n": 50, "slope_w": 20, "slope_thr": 0.005}
P_DUALMA = {"fast_n": 50, "slow_n": 200}
P_HILO = {"win": 250, "bull_dd": 0.10, "bear_dd": 0.20}
P_VOL = {"win": 20, "lookback": 504, "min_hist": 250}


def _ma(x: np.ndarray, n: int) -> np.ndarray:
    out = np.full(len(x), np.nan)
    if n >= 1 and len(x) >= n:
        c = np.cumsum(np.insert(x.astype(float), 0, 0.0))
        out[n - 1:] = (c[n:] - c[:-n]) / n
    return out


def _roll_max(x: np.ndarray, n: int) -> np.ndarray:
    out = np.full(len(x), np.nan)
    if len(x) >= n:
        out[n - 1:] = np.lib.stride_tricks.sliding_window_view(x, n).max(axis=1)
    return out


def _labels_ma50slope(close: np.ndarray, ma_n: int = 50, slope_w: int = 20,
                      slope_thr: float = 0.005) -> np.ndarray:
    ma = _ma(close, ma_n)
    slope = np.full(len(close), np.nan)
    slope[slope_w:] = ma[slope_w:] / ma[:-slope_w] - 1.0
    out = np.full(len(close), NA, dtype=object)
    ok = ~np.isnan(ma) & ~np.isnan(slope)
    out[ok] = "range"
    out[ok & (close >= ma) & (slope > slope_thr)] = "bull"
    out[ok & (close < ma) & (slope < -slope_thr)] = "bear"
    return out


def _labels_dualma(close: np.ndarray, fast_n: int = 50, slow_n: int = 200) -> np.ndarray:
    fast, slow = _ma(close, fast_n), _ma(close, slow_n)
    out = np.full(len(close), NA, dtype=object)
    ok = ~np.isnan(fast) & ~np.isnan(slow)
    out[ok] = "range"
    out[ok & (close >= slow) & (fast >= slow)] = "bull"
    out[ok & (close < slow) & (fast < slow)] = "bear"
    return out


def _labels_hilo(close: np.ndarray, win: int = 250, bull_dd: float = 0.10,
                 bear_dd: float = 0.20) -> np.ndarray:
    hi = _roll_max(close, win)          # 含当日, 只看过去
    dd = close / hi - 1.0
    out = np.full(len(close), NA, dtype=object)
    ok = ~np.isnan(hi)
    out[ok] = "range"
    out[ok & (dd >= -bull_dd)] = "bull"
    out[ok & (dd <= -bear_dd)] = "bear"
    return out


FAMILIES = {
    "ma50slope": (_labels_ma50slope, P_MA50SLOPE),
    "dualma": (_labels_dualma, P_DUALMA),
    "hilo": (_labels_hilo, P_HILO),
}


def trend_labels(close: np.ndarray, family: str, **params) -> np.ndarray:
    """-> object ndarray, 每日 'bull'|'range'|'bear'|'na' (预热期 na)。"""
    fn, defaults = FAMILIES[family]
    kw = {**defaults, **params}
    return fn(np.asarray(close, dtype=float), **kw)


def vol_labels(close: np.ndarray, win: int = 20, lookback: int = 504,
               min_hist: int = 250) -> np.ndarray:
    """20bar 已实现波动率在"截至当日的过去 lookback bar"里的分位 -> 三分位标签。
    分位窗口只含过去+当日 (无前视); 有效历史不足 min_hist 记 na。"""
    close = np.asarray(close, dtype=float)
    n = len(close)
    r = np.full(n, np.nan)
    r[1:] = np.log(close[1:] / close[:-1])
    vol = np.full(n, np.nan)
    if n >= win + 1:
        sw = np.lib.stride_tricks.sliding_window_view(r[1:], win)   # 行 t 对应日 win..n-1
        vol[win:] = sw.std(axis=1) * np.sqrt(252.0)
    out = np.full(n, NA, dtype=object)
    for t in range(n):
        if np.isnan(vol[t]):
            continue
        hist = vol[max(0, t - lookback + 1): t + 1]
        hist = hist[~np.isnan(hist)]
        if len(hist) < min_hist:
            continue
        srt = np.sort(hist)
        lo = np.searchsorted(srt, vol[t], side="left")
        hi = np.searchsorted(srt, vol[t], side="right")
        pct = (lo + hi) / 2 / len(hist)   # 并列取中位名次: 零波动/大量并列不会被推成 high
        out[t] = "low" if pct <= 1 / 3 else ("high" if pct > 2 / 3 else "mid")
    return out


def debounce(labels: np.ndarray, k: int = 5) -> np.ndarray:
    """确认去抖: 生效状态只在"新原始状态连续出现 k 天"后才切换 (只看过去,
    无前视; 代价是每次转折晚 k 天确认)。首个非na原始标签直接生效。"""
    out = np.full(len(labels), NA, dtype=object)
    cur, cand, run = None, None, 0
    for i, lab in enumerate(labels):
        s = str(lab)
        if s == NA:
            out[i] = cur or NA
            cand, run = None, 0
            continue
        if cur is None:
            cur = s
        elif s == cur:
            cand, run = None, 0
        else:
            if s == cand:
                run += 1
            else:
                cand, run = s, 1
            if run >= k:
                cur, cand, run = s, None, 0
        out[i] = cur
    return out


def series(family: str, trend_params: dict | None = None,
           vol_params: dict | None = None, confirm_k: int = 5) -> dict | None:
    """从 pricestore 基准指数出全序列 -> {dates, close, trend, vol};
    trend 默认带 confirm_k 天确认去抖 (confirm_k=0 取原始标签); 无指数返回 None。"""
    idx = ps.load_index()
    if not idx:
        return None
    close = idx["ohlcv"][:, 3]
    trend = trend_labels(close, family, **(trend_params or {}))
    if confirm_k:
        trend = debounce(trend, confirm_k)
    return {
        "dates": idx["dates"],
        "close": close,
        "trend": trend,
        "vol": vol_labels(close, **(vol_params or {**P_VOL})),
    }


def label_at(dates: list, labels: np.ndarray, day: str) -> str:
    """任意日历日 -> 最近一个 <= day 的交易日标签 (回测按信号日查询用)。"""
    i = int(np.searchsorted(np.array(dates), day, side="right")) - 1
    return str(labels[i]) if 0 <= i < len(labels) else NA


def segments(dates: list, labels: np.ndarray) -> list:
    """连续同状态区段 -> [{state, start, end, n}], 含 na 段 (调用方自行过滤)。"""
    assert len(dates) == len(labels), f"dates({len(dates)}) != labels({len(labels)})"
    out = []
    for i, lab in enumerate(labels):
        s = str(lab)
        if out and out[-1]["state"] == s:
            out[-1]["end"] = dates[i]
            out[-1]["n"] += 1
        else:
            out.append({"state": s, "start": dates[i], "end": dates[i], "n": 1})
    return out
