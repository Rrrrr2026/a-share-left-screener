#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
模块5b — 买卖点建议 (Trade Plan)
================================
对每只候选股, 基于其自身历史做"支撑位回踩"事件回测:
  事件 = 历史上某日贴近支撑位(前低/均线) + 处于回调中 + RSI偏弱
  结果 = 事件后 H 个交易日内, "先到目标涨幅 g" 还是 "先破止损"
得到各目标涨幅的历史胜率, 再与全体候选池的汇总先验做贝叶斯收缩
(单股样本少, 用池先验稳住估计), 输出:
  建议买入区 / 止损位 / 目标价梯子(含胜率, 如 "+10% 胜率72%") / 盈亏比 / 预计持有天数
⚠️ 胜率为历史回测频率, 不构成对未来的保证; 前端展示需带免责声明。
"""
from __future__ import annotations
import logging
import numpy as np
import pandas as pd

from . import indicators as ind

log = logging.getLogger("screener.tradeplan")

# 目标涨幅网格 (从事件日入场价起算)
GAIN_GRID = [0.03, 0.05, 0.08, 0.10, 0.15, 0.20, 0.30]
HORIZON = 60          # 事件后观察窗口 (交易日)
PIVOT_WINDOW = 10     # 摆动低点确认窗口 (与 module2 一致)
DEDUPE_BARS = 8       # 两次事件至少间隔的交易日
SHRINK_M = 12.0       # 贝叶斯收缩伪样本数 (单股样本向池先验收缩的强度)
MIN_STOP = 0.05       # 止损距离下限 5% (左侧建仓要给洗盘空间, 太紧全是噪音止损)
MAX_STOP = 0.15       # 止损距离上限 15%


def _stop_ret(atr_frac: float) -> float:
    """入场价到止损价的距离 (比例): 1.8×ATR, 夹在 [5%, 15%]。"""
    return float(np.clip(1.8 * atr_frac, MIN_STOP, MAX_STOP))


def compute_event_stats(df: pd.DataFrame) -> dict | None:
    """对单只股票的日线做支撑回踩事件回测。
    返回 {"n": 事件数, "hits": {g: 次数}, "stopped": 次数,
          "days_to": {g: [到达天数,...]}}; 数据不足返回 None。
    约定: 目标与止损同日命中按"先止损"保守处理; 窗口截断未出结果按"未达标"保守计。"""
    if df is None or len(df) < 140:
        return None
    close = df["close"].astype(float).to_numpy()
    low = df["low"].astype(float).to_numpy()
    high = df["high"].astype(float).to_numpy()
    n = len(close)

    close_s = pd.Series(close)
    rsi = ind.rsi(close_s).to_numpy()
    atr = ind.atr(pd.Series(high), pd.Series(low), close_s).to_numpy()
    ma60 = close_s.rolling(60).mean().to_numpy()
    ma120 = close_s.rolling(120).mean().to_numpy()
    ma250 = close_s.rolling(250).mean().to_numpy()
    roll_hi120 = pd.Series(high).rolling(120, min_periods=30).max().to_numpy()

    # 摆动低点 (确认于 i+PIVOT_WINDOW, 之后才可作为"已知支撑")
    pivots = ind.find_pivot_lows(pd.Series(low), PIVOT_WINDOW)   # [(i, price)]

    hits = {g: 0 for g in GAIN_GRID}
    days_to = {g: [] for g in GAIN_GRID}
    n_events, stopped = 0, 0
    last_t = -10**9

    # 事件至少要有20根前瞻bar, 否则截断样本会同时压低胜率与"历史止损率"两个方向的统计
    for t in range(70, n - 20):
        if t - last_t < DEDUPE_BARS:
            continue
        px = close[t]
        if not np.isfinite(px) or px <= 0:
            continue
        # 处于回调中 (距近120日高点回撤>=8%) 且 RSI 偏弱
        hi = roll_hi120[t]
        if not np.isfinite(hi) or hi <= 0 or (hi - px) / hi < 0.08:
            continue
        if np.isfinite(rsi[t]) and rsi[t] > 55.0:
            continue
        # 当日贴近某个"当时已知"的支撑位
        levels = []
        for ma in (ma60[t], ma120[t], ma250[t]):
            if np.isfinite(ma) and ma > 0:
                levels.append(float(ma))
        for (i, p) in pivots:
            if i + PIVOT_WINDOW <= t and p > 0 and abs(p / px - 1.0) <= 0.15:
                levels.append(float(p))
        lvl = None
        for L in levels:
            near = abs(low[t] / L - 1.0) <= 0.02 or (low[t] <= L <= close[t])
            if near and close[t] >= L * 0.97:
                lvl = L
                break
        if lvl is None:
            continue

        atr_frac = (atr[t] / px) if (np.isfinite(atr[t]) and px > 0) else 0.03
        sret = _stop_ret(atr_frac)
        stop_px = px * (1.0 - sret)

        n_events += 1
        last_t = t
        run_high = px
        got = {g: None for g in GAIN_GRID}
        was_stopped = False
        end = min(t + HORIZON, n - 1)
        for tau in range(t + 1, end + 1):
            if low[tau] <= stop_px:          # 同日双触发按先止损, 保守
                was_stopped = True
                break
            run_high = max(run_high, high[tau])
            for g in GAIN_GRID:
                if got[g] is None and run_high >= px * (1.0 + g):
                    got[g] = tau - t
        if was_stopped:
            stopped += 1
        for g in GAIN_GRID:
            if got[g] is not None:
                hits[g] += 1
                days_to[g].append(got[g])

    if n_events == 0:
        return None
    return {"n": n_events, "hits": hits, "stopped": stopped, "days_to": days_to}


def pool_prior(stats_list: list) -> dict:
    """全体候选的事件汇总 -> 各目标涨幅的池先验胜率。"""
    total = sum(s["n"] for s in stats_list if s)
    if total == 0:
        # 无任何事件时的兜底先验 (温和递减)
        return {"n": 0, "p": {g: max(0.05, 0.75 - 2.2 * g) for g in GAIN_GRID}}
    p = {}
    for g in GAIN_GRID:
        h = sum(s["hits"][g] for s in stats_list if s)
        p[g] = h / total
    return {"n": total, "p": p}


def _shrink(hits: int, n: int, p0: float, m: float = SHRINK_M) -> float:
    return (hits + m * p0) / (n + m) if (n + m) > 0 else p0


def build_trade_plan(rec: dict, stats: dict | None, prior: dict) -> dict | None:
    """结合当前支撑/ATR 与回测胜率, 生成买卖点建议。
    rec 需含: price, support_price, breakdown_price, atr_pct, fib_618。"""
    px = rec.get("price")
    if not px or px <= 0:
        return None
    support = rec.get("support_price")
    atr_pct = rec.get("atr_pct") or 3.0
    atr_frac = float(np.clip(atr_pct / 100.0, 0.008, 0.08))

    # 🚀 蓄势待发(coil)股是"突破型"交易, 与支撑回踩剧本相反:
    # 买点=放量突破箱体上沿, 止损=跌回箱体下沿, 目标=箱体高度量度目标 (不给回踩胜率 —
    # 事件回测统计的是支撑回踩, 描述不了突破交易, 强行标胜率是误导)。
    box_hi, box_lo = rec.get("box_hi"), rec.get("box_lo")
    if rec.get("coil") and box_hi and box_lo and 0 < box_lo < box_hi:
        entry_ref = float(box_hi)
        box_h = float(box_hi) - float(box_lo)
        stop_px = max(float(box_lo) * 0.995, entry_ref * (1.0 - MAX_STOP))
        stop_loss_pct = (entry_ref - stop_px) / entry_ref * 100.0

        def _mt(mult):
            g = box_h * mult / entry_ref
            return {"gain_pct": round(g * 100.0, 1),
                    "price": round(entry_ref * (1.0 + g), 2),
                    "prob_pct": None, "days_med": None}

        base = _mt(1.0)     # 经典箱体量度目标 = 上沿 + 1×箱体高度
        rr = (base["gain_pct"] / stop_loss_pct) if stop_loss_pct > 0 else None
        return {
            "entry_mode": "breakout",
            "entry_ref": round(entry_ref, 2),
            "entry_low": round(entry_ref, 2),
            "entry_high": round(entry_ref * (1.0 + 0.5 * atr_frac), 2),
            "stop_price": round(stop_px, 2),
            "stop_loss_pct": round(stop_loss_pct, 1),
            "targets": {"steady": _mt(0.5), "base": base, "stretch": _mt(2.0)},
            "ladder": [],
            "rr": round(rr, 1) if rr else None,
            "horizon_days": HORIZON,
            "n_events": 0, "pool_events": prior.get("n", 0),
            "stopped_rate_pct": None,
            "box_hi": round(float(box_hi), 2), "box_lo": round(float(box_lo), 2),
        }

    # 入场参考: 有支撑位则以支撑为锚 (等回踩); 已跌破支撑则以现价为锚;
    # 根本没识别出支撑的(深跌/纯趋势回调股)单独标注, 不能谎称"已失守支撑"
    if support and support > 0 and px >= support * 0.985:
        entry_ref = float(support)
        entry_mode = "support"       # 等回踩到支撑区再买
    elif support and support > 0:
        entry_ref = float(px)
        entry_mode = "market"        # 已跌破支撑: 以现价分批
    else:
        entry_ref = float(px)
        entry_mode = "none"          # 无明确支撑位: 现价分批, 更需谨慎
    entry_low = entry_ref * (1.0 - 0.4 * atr_frac)
    entry_high = entry_ref * (1.0 + 0.5 * atr_frac)

    # 止损 = 结构失效位: ATR止损 与 破位位略下方 取更深者 (给足洗盘空间),
    # 但最深不超过入场价 -15% (风险上限)
    sret = _stop_ret(atr_frac)
    stop_px = entry_ref * (1.0 - sret)
    bp = rec.get("breakdown_price")
    if bp and 0 < bp < entry_ref:
        stop_px = min(stop_px, float(bp) * 0.995)
    stop_px = max(stop_px, entry_ref * (1.0 - MAX_STOP))
    stop_loss_pct = (entry_ref - stop_px) / entry_ref * 100.0

    # 各目标涨幅的收缩后胜率
    p0 = prior.get("p", {})
    n_s = stats["n"] if stats else 0
    probs = {}
    for g in GAIN_GRID:
        h = stats["hits"][g] if stats else 0
        probs[g] = _shrink(h, n_s, p0.get(g, 0.3))

    def _pick(threshold, default_idx=0):
        cand = [g for g in GAIN_GRID if probs[g] >= threshold]
        return max(cand) if cand else GAIN_GRID[default_idx]

    g_steady = _pick(0.75)          # 稳健目标: 胜率≥75%
    g_base = max(_pick(0.50), g_steady)   # 基准目标: 胜率≥50%
    g_stretch = max(_pick(0.30), g_base)  # 进取目标: 胜率≥30%

    def _days(g):
        if stats and stats["days_to"].get(g):
            return int(np.median(stats["days_to"][g]))
        return None

    def _tgt(g):
        return {
            "gain_pct": round(g * 100.0, 1),
            "price": round(entry_ref * (1.0 + g), 2),
            "prob_pct": round(probs[g] * 100.0, 0),
            "days_med": _days(g),
        }

    rr = (g_base * 100.0) / stop_loss_pct if stop_loss_pct > 0 else None
    return {
        "entry_mode": entry_mode,
        "entry_ref": round(entry_ref, 2),
        "entry_low": round(entry_low, 2),
        "entry_high": round(entry_high, 2),
        "stop_price": round(stop_px, 2),
        "stop_loss_pct": round(stop_loss_pct, 1),
        "targets": {"steady": _tgt(g_steady), "base": _tgt(g_base), "stretch": _tgt(g_stretch)},
        "ladder": [_tgt(g) for g in GAIN_GRID],
        "rr": round(rr, 1) if rr else None,
        "horizon_days": HORIZON,
        "n_events": n_s,
        "pool_events": prior.get("n", 0),
        "stopped_rate_pct": (round(stats["stopped"] / stats["n"] * 100.0, 0)
                             if stats and stats["n"] > 0 else None),
    }
