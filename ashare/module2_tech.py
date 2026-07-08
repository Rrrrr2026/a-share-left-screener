#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
模块2 — 技术左侧扫描 (Technical Left-Side Scan)
===============================================
复用参考实现 a_share_left_screener.py 的指标与打分思想, 并扩展:
  * 输出"关键支撑位 / 距支撑% / 破位参考位"
  * 输出详情页 K线 + MA + 通道下轨 + 前低 + MACD/KDJ/RSI 所需的逐日序列

对每只股票 (约250根前复权日线) 命中以下信号给分 (越接近支撑分越高):
  1 上升通道下轨   2 前期重要低点   3 关键均线支撑
  4 超跌+MACD底背离   5 左侧前提(回撤够深)
基础过滤: 剔除ST / 次新 / 流动性差 / 低价股 (在 datasource.build_universe + 此处)。
"""
from __future__ import annotations
import logging
import numpy as np
import pandas as pd

from .config import CONFIG
from . import indicators as ind

log = logging.getLogger("ashare.module2")


def _nz(x):
    """NaN/inf -> None (便于 JSON 序列化与 — 显示)。"""
    if x is None:
        return None
    try:
        xf = float(x)
        return None if (np.isnan(xf) or np.isinf(xf)) else round(xf, 4)
    except Exception:
        return None


def _series_to_list(s, bars):
    s = s.tail(bars)
    return [None if (v is None or (isinstance(v, float) and (np.isnan(v) or np.isinf(v)))) else round(float(v), 4)
            for v in s.values]


def scan_one(code: str, name: str, df: pd.DataFrame, spot_row: dict | None = None,
             bench_close: pd.Series | None = None):
    """
    df: 个股日线 (date,open,high,low,close[,volume,amount])。
    返回 (record:dict, detail:dict) 或 (None, None)。
    record 为打分与关键位; detail 为图表逐日序列。
    """
    c = CONFIG["tech"]
    # 至少要够算最长均线(MA250 的最后一个值需要 250 根); +5 留一点余量
    if df is None or len(df) < max(c["channel_window"], max(c["ma_list"])) + 5:
        return None, None

    df = df.sort_values("date").reset_index(drop=True)
    close = df["close"].astype(float)
    low = df["low"].astype(float)
    high = df["high"].astype(float)
    px = float(close.iloc[-1])
    if px < c["min_price"]:
        return None, None

    # 流动性: 近20日日均成交额(亿)
    amt_yi = np.nan
    if "amount" in df.columns:
        amt_yi = float(df["amount"].astype(float).tail(20).mean()) / 1e8
        if not np.isnan(amt_yi) and amt_yi < c["min_amount_yi"]:
            return None, None

    w = c["weights"]
    score = 0.0
    signals = {}
    support_cands = []   # (label, price)

    # --- 1) 上升通道下轨 ---
    ch = ind.linreg_channel(close, c["channel_window"], c["channel_band_k"])
    dist_lower = None
    if ch is not None:
        lower_band = ch["lower_band"]
        dist_lower = (px - lower_band) / px * 100.0
        hit_channel = ch["uptrend"] and (-1.0 <= dist_lower <= c["near_lower_pct"])
        if hit_channel:
            prox = max(0.0, 1 - abs(dist_lower) / c["near_lower_pct"])
            score += w["channel"] * (0.5 + 0.5 * prox)
            support_cands.append(("通道下轨", lower_band))
        signals["channel"] = "✓" if hit_channel else ""
    else:
        signals["channel"] = ""

    # --- 2) 前期重要低点 ---
    piv = ind.find_pivot_lows(low, c["pivot_window"])
    pivot_levels = []
    dist_pivot, near_pivot = None, False
    if piv:
        pivot_levels = sorted({round(p, 2) for (i, p) in piv
                               if i < len(df) - 5 and abs(p - px) / px <= 0.25})
        cands = [p for (i, p) in piv if i < len(df) - 5 and abs(p - px) / px <= 0.15]
        if cands:
            nearest = min(cands, key=lambda p: abs(p - px))
            dist_pivot = (px - nearest) / px * 100.0
            near_pivot = abs(dist_pivot) <= c["near_pivot_pct"]
            if near_pivot:
                prox = max(0.0, 1 - abs(dist_pivot) / c["near_pivot_pct"])
                score += w["pivot"] * (0.5 + 0.5 * prox)
                support_cands.append(("前低", nearest))
    signals["pivot"] = "✓" if near_pivot else ""

    # --- 3) 关键均线支撑 ---
    hit_ma, best_ma_dist, best_ma, best_ma_price = False, None, None, None
    ma_vals = {}
    for n in c["ma_list"]:
        ma = close.rolling(n).mean().iloc[-1]
        ma_vals[n] = ma
        if np.isnan(ma):
            continue
        d = (px - ma) / px * 100.0
        if -1.0 <= d <= c["near_ma_pct"]:
            hit_ma = True
            if best_ma_dist is None or abs(d) < abs(best_ma_dist):
                best_ma_dist, best_ma, best_ma_price = d, n, float(ma)
    if hit_ma:
        prox = max(0.0, 1 - abs(best_ma_dist) / c["near_ma_pct"])
        score += w["ma"] * (0.5 + 0.5 * prox)
        support_cands.append((f"MA{best_ma}", best_ma_price))
    signals["ma"] = f"MA{best_ma}" if hit_ma else ""

    # --- 4) 超跌 + MACD底背离 / 绿柱缩短 + RSI超卖 ---
    dif, dea, hist = ind.macd(close)
    r = ind.rsi(close)
    rsi_now = float(r.iloc[-1]) if not np.isnan(r.iloc[-1]) else np.nan
    hist_now, hist_prev = float(hist.iloc[-1]), float(hist.iloc[-2])
    green_shrink = (hist_now < 0) and (hist_now > hist_prev)
    bull_div = False
    look = 60
    if len(close) > look:
        c_seg = close.tail(look).reset_index(drop=True)
        d_seg = dif.tail(look).reset_index(drop=True)
        if c_seg.idxmin() >= look - 15:
            half = look // 2
            if d_seg.iloc[half:].min() > d_seg.iloc[:half].min():
                bull_div = True
    oversold = (not np.isnan(rsi_now)) and rsi_now <= c["rsi_oversold"]
    hit_osc = oversold or green_shrink or bull_div
    if hit_osc:
        sub = (0.5 if oversold else 0.0) + (0.25 if green_shrink else 0.0) + (0.5 if bull_div else 0.0)
        score += w["oversold_div"] * min(1.0, sub)
    signals["osc"] = "".join(["超卖" if oversold else "",
                              "缩柱" if green_shrink else "",
                              "底背离" if bull_div else ""])

    # --- 5) 回撤幅度 (左侧前提) ---
    hi = float(high.tail(c["channel_window"]).max())
    drawdown = (hi - px) / hi if hi else np.nan
    if not np.isnan(drawdown) and drawdown >= c["drawdown_min"]:
        score += w["drawdown"] * min(1.0, drawdown / 0.5)

    # --- 6) 布林带下轨(额外支撑参考) + 量能确认 ---
    boll_low = ind.bollinger_lower(close, c.get("boll_n", 20), c.get("boll_k", 2.0))
    boll_low_val = ind.safe_last(boll_low)
    if not np.isnan(boll_low_val) and boll_low_val < px:
        d_boll = (px - boll_low_val) / px * 100.0
        if 0 <= d_boll <= c["near_lower_pct"]:
            support_cands.append(("布林下轨", boll_low_val))
    vol_ratio_calc, vol_confirm_txt = None, ""
    if "volume" in df.columns:
        vol = df["volume"].astype(float)
        avg20v = vol.tail(20).mean()
        if avg20v and not np.isnan(avg20v) and avg20v > 0:
            vol_ratio_calc = round(float(vol.iloc[-1] / avg20v), 2)
            shrink = vol_ratio_calc < c.get("vol_shrink_ratio", 0.85)
            spike_up = vol_ratio_calc > 1.5 and float(close.iloc[-1]) > float(close.iloc[-2])
            if support_cands and (shrink or spike_up):
                score += w.get("vol_confirm", 0.0) * (0.7 if shrink else 0.5)
                vol_confirm_txt = "缩量企稳" if shrink else "放量"
    signals["vol"] = vol_confirm_txt

    n_hit = (sum(1 for k in ("channel", "pivot", "ma") if signals[k])
             + (1 if hit_osc else 0) + (1 if vol_confirm_txt else 0))

    # ---- 关键位: 主支撑(离现价最近且<=现价附近) / 距支撑% / 破位参考位 ----
    support_label, support_price, dist_support = None, None, None
    if support_cands:
        # 取离现价最近的作为"主支撑"
        support_label, support_price = min(support_cands, key=lambda kv: abs(px - kv[1]))
        dist_support = (px - support_price) / px * 100.0
    breakdown_price = None
    all_support_prices = [p for (_, p) in support_cands] + pivot_levels
    all_support_prices = [p for p in all_support_prices if p and p <= px * 1.02]
    if all_support_prices:
        breakdown_price = min(all_support_prices) * 0.97   # 破位 = 最低支撑下方3%

    # ---- 52周高低 / 位置 / 近半年涨跌 ----
    win52 = min(250, len(df))
    high_52w = float(high.tail(win52).max())
    low_52w = float(low.tail(win52).min())
    pos_52w = (px - low_52w) / (high_52w - low_52w) * 100.0 if high_52w > low_52w else np.nan
    ret_half = ind.cumulative_return(close, 120)
    ret_1m = ind.cumulative_return(close, 21)     # 近一月涨幅 (≈21个交易日)

    # ---- KDJ ----
    k, d_, j = ind.kdj(high, low, close)
    kk, dd, jj = ind.safe_last(k), ind.safe_last(d_), ind.safe_last(j)
    kdj_tag = ind.kdj_tag(kk, dd, jj)

    # ---- 风控指标 + 行内sparkline + 斐波那契回撤 ----
    atrp = ind.atr_pct(high, low, close)
    maxdd = ind.max_drawdown(close, 250)
    if bench_close is not None:
        # 用日期做索引, beta() 内部按日期交集对齐 (避免停牌/日历差导致的错位配对)
        close_dated = pd.Series(close.values, index=df["date"].astype(str).values)
        beta_v = ind.beta(close_dated, bench_close, 120)
    else:
        beta_v = np.nan
    spark = ind.downsample(close.tail(60), 40)   # 近60日收盘降采样, 行内走势
    fib = ind.fib_levels(high_52w, low_52w)

    # ---- 量比/换手 (优先用快照, 否则留空) ----
    vol_ratio = turnover = amount_today = None
    if spot_row:
        vol_ratio = _nz(spot_row.get("volume_ratio"))
        turnover = _nz(spot_row.get("turnover"))
        amount_today = _nz(spot_row.get("amount"))

    # ---- 独立"深跌超卖抄底"桶 (与支撑型 tech_score 完全解耦, 不改动 score) ----
    # 硬门槛: 深跌(回撤>=阈值) + 超卖(RSI<=阈值) + 逼近52周低点. 三者全中才进桶。
    dcfg = c.get("dip") or {}
    dip_ok, dip_score = False, 0.0
    dip_confirm = ""
    _dd = drawdown if not np.isnan(drawdown) else None
    _rsi = rsi_now if not np.isnan(rsi_now) else None
    _pos = pos_52w if not np.isnan(pos_52w) else None
    if dcfg and _dd is not None and _rsi is not None and _pos is not None:
        deep = _dd >= dcfg["drawdown_min"]
        oversold_d = _rsi <= dcfg["rsi_max"]
        nearlow = _pos <= dcfg["pos_52w_max"]
        if deep and oversold_d and nearlow:
            dip_ok = True
            confirms = []
            if bull_div: confirms.append("底背离")
            if green_shrink: confirms.append("缩柱")
            if kdj_tag and "金叉" in kdj_tag: confirms.append("金叉")   # kdj_tag 可能是"金叉/超卖", 用子串匹配
            if vol_ratio_calc is not None and vol_ratio_calc >= dcfg.get("vol_spike", 1.8):
                confirms.append("放量")
            dip_confirm = "".join(confirms)
            dw = dcfg["weights"]
            f_depth = min(1.0, _dd / 0.60)
            f_os = max(0.0, min(1.0, (40.0 - _rsi) / 40.0))
            f_near = max(0.0, min(1.0, 1.0 - _pos / max(1e-6, dcfg["pos_52w_max"])))
            f_conf = len(confirms) / 4.0
            dip_score = round(dw["depth"] * f_depth + dw["oversold"] * f_os
                              + dw["nearlow"] * f_near + dw["confirm"] * f_conf, 3)

    record = {
        "code": code, "name": name,
        "price": round(px, 2),
        "tech_score": round(float(score), 3),
        "n_hit": int(n_hit),
        "sig_channel": signals["channel"],
        "sig_pivot": signals["pivot"],
        "sig_ma": signals["ma"],
        "sig_osc": signals["osc"],
        "dist_lower": _nz(dist_lower),
        "dist_pivot": _nz(dist_pivot),
        "dist_ma": _nz(best_ma_dist),
        "drawdown_pct": _nz(drawdown * 100 if not np.isnan(drawdown) else None),
        "rsi": _nz(rsi_now),
        "support_label": support_label,
        "support_price": _nz(support_price),
        "dist_support_pct": _nz(dist_support),
        "breakdown_price": _nz(breakdown_price),
        "high_52w": round(high_52w, 2),
        "low_52w": round(low_52w, 2),
        "pos_52w_pct": _nz(pos_52w),
        "ret_half_year_pct": _nz(ret_half),
        "ret_1m_pct": _nz(ret_1m),
        "turnover": turnover,
        "volume_ratio": vol_ratio if vol_ratio is not None else vol_ratio_calc,
        "amount_today": amount_today,
        "avg_amt20_yi": _nz(amt_yi),
        "kdj_k": _nz(kk), "kdj_d": _nz(dd), "kdj_j": _nz(jj), "kdj_tag": kdj_tag,
        # 新增: sparkline / 风控 / 量能 / 斐波那契
        "spark": spark,
        "atr_pct": _nz(atrp),
        "max_dd_pct": _nz(maxdd),
        "beta": _nz(beta_v),
        "vol_ratio_calc": vol_ratio_calc,
        "sig_vol": signals.get("vol", ""),
        "boll_low": _nz(boll_low_val),
        "fib_382": fib["f382"], "fib_500": fib["f500"], "fib_618": fib["f618"],
        # 深跌抄底桶 (独立于 tech_score)
        "dip": bool(dip_ok),
        "dip_score": float(dip_score),
        "dip_confirm": dip_confirm,
    }

    # ---- 详情图表逐日序列 ----
    bars = c["detail_bars"]
    dates = list(df["date"].tail(bars))
    o = df["open"].astype(float).tail(bars).values
    cl = close.tail(bars).values
    lo = low.tail(bars).values
    hg = high.tail(bars).values
    ohlc = [[round(float(o[i]), 2), round(float(cl[i]), 2),
             round(float(lo[i]), 2), round(float(hg[i]), 2)] for i in range(len(dates))]
    # 通道下轨序列 (仅最近 channel_window 根有值, 左侧补 None 对齐 bars)
    lb_full = [None] * len(dates)
    if ch is not None:
        lser = ch["lower_series"]
        cw = len(lser)
        for i in range(cw):
            idx = len(dates) - cw + i
            if 0 <= idx < len(dates):
                lb_full[idx] = round(float(lser[i]), 3)

    detail = {
        "code": code, "name": name,
        "dates": dates,
        "ohlc": ohlc,
        "ma60": _series_to_list(close.rolling(60).mean(), bars),
        "ma120": _series_to_list(close.rolling(120).mean(), bars),
        "ma250": _series_to_list(close.rolling(250).mean(), bars),
        "lower_band": lb_full,
        "pivot_lows": pivot_levels,
        "macd_dif": _series_to_list(dif, bars),
        "macd_dea": _series_to_list(dea, bars),
        "macd_hist": _series_to_list(hist, bars),
        "kdj_k": _series_to_list(k, bars),
        "kdj_d": _series_to_list(d_, bars),
        "kdj_j": _series_to_list(j, bars),
        "rsi": _series_to_list(r, bars),
        "boll_lower": _series_to_list(boll_low, bars),
        "fib": {"f382": fib["f382"], "f500": fib["f500"], "f618": fib["f618"]},
    }
    return record, detail
