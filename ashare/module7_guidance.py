#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
模块7 — 盈利指引 (Profit Guidance by Historical Drawdown Analogs)
==================================================================
问题: "按这个价位买进去, 赚 50% / 100% 的概率有多大?"

做法 —— **不预测, 只统计**:
  在该股自己的历史里, 找出所有"和现在处境相似"的时刻(同样是大回撤之后、
  同样跌到区间低位/超卖), 把每一次之后 *实际* 走出来的结果统计成频率:
      · 之后一年内最高涨过 +100% 的有几次 -> 就是 +100% 的历史频率
      · 之后一年内最多还跌了多少 (MAE)   -> 就是"接刀"的代价
  输出的是 **历史同类形态的经验频率**, 不是未来概率预测。

为什么不用"机器学习":
  单只股票 10 年里这种大回撤形态通常只有 3~10 次(且彼此重叠)。样本量这么小,
  任何模型(哪怕梯度提升/神经网)都只会过拟合噪声, 给出好看但没有意义的数字。
  条件频率统计是这个样本量下唯一诚实的做法, 且每个数字都可追溯到具体日期。

统计上的几个硬约束(避免自欺):
  · 样本必须**互不重叠**: 两次入场至少间隔 min_gap 个交易日, 否则同一波行情
    会被重复计数, 把概率算高;
  · 样本必须有**完整的前向窗口**: 距今不足 horizon 天的形态不计入(否则只统计
    到一半就下结论, 系统性高估或低估);
  · 样本数 < min_samples 时**不给概率**, 直接标"样本不足" —— 3 次里中 1 次
    不等于 33% 的概率;
  · 用 high/low 算最大涨幅与最大回撤(真实可触达), 而不是收盘价。
"""
from __future__ import annotations
import logging

import numpy as np
import pandas as pd

from .config import CONFIG

log = logging.getLogger("ashare.module7")

DEFAULTS = {
    "horizon": 250,          # 前向观察窗口(交易日) ≈ 1年
    "horizon_short": 120,    # 次级窗口 ≈ 半年
    "lookback_high": 250,    # 回撤基准: 过去N日最高
    "dd_min": 30.0,          # 多深才算"大回撤"(%)
    "near_low_pct": 12.0,    # 距近60日最低 <=X% 视为"跌到低位"
    "rsi_max": 40.0,         # 或 RSI <= X (超卖) —— 二者满足其一
    "min_gap": 120,          # 两次样本最小间隔(交易日), 保证不重叠
    "min_samples": 3,        # 少于这个数不给概率
    "targets": [100, 50, 30, 20],   # 统计哪些盈利档位(%)
}


def _cfg() -> dict:
    d = dict(DEFAULTS)
    d.update(CONFIG.get("guidance") or {})
    return d


def _rsi(close: pd.Series, n: int = 14) -> pd.Series:
    d = close.diff()
    up = d.clip(lower=0).ewm(alpha=1 / n, adjust=False).mean()
    dn = (-d.clip(upper=0)).ewm(alpha=1 / n, adjust=False).mean()
    rs = up / dn.replace(0, np.nan)
    return (100 - 100 / (1 + rs)).fillna(50)


def find_analogs(df: pd.DataFrame, cfg: dict) -> list[int]:
    """返回历史上"与当前处境相似"的入场位置(整数下标)。"""
    close = df["close"].astype(float)
    low = df["low"].astype(float) if "low" in df.columns else close
    n = len(df)
    roll_high = close.rolling(cfg["lookback_high"], min_periods=60).max()
    dd = (roll_high - close) / roll_high * 100.0          # 回撤%
    low60 = low.rolling(60, min_periods=20).min()
    dist_low = (close - low60) / low60 * 100.0            # 距60日低点%
    rsi = _rsi(close)

    horizon = cfg["horizon"]
    cand = []
    for i in range(60, n - horizon):        # 必须留够完整前向窗口
        if not (dd.iloc[i] >= cfg["dd_min"]):
            continue
        if not (dist_low.iloc[i] <= cfg["near_low_pct"] or rsi.iloc[i] <= cfg["rsi_max"]):
            continue
        cand.append(i)

    # 去重叠: 相邻样本至少间隔 min_gap, 同一波行情只取第一次(最贴近"刚形成")
    picked = []
    for i in cand:
        if not picked or (i - picked[-1]) >= cfg["min_gap"]:
            picked.append(i)
    return picked


def analyze(df: pd.DataFrame, support_price: float | None = None,
            price_now: float | None = None) -> dict:
    """对一只股票的长历史做同类形态统计。df 需含 date/close(+high/low 更准)。"""
    cfg = _cfg()
    out = {
        "guid_n": 0, "guid_probs": [], "guid_note": None,
        "guid_med_gain": None, "guid_med_mae": None, "guid_win_rate": None,
        "guid_buy_low": None, "guid_buy_high": None,
        "guid_samples": [], "guid_hist_years": None,
    }
    if df is None or len(df) < 300 or "close" not in df.columns:
        out["guid_note"] = "历史数据不足, 无法统计"
        return out

    close = df["close"].astype(float)
    high = df["high"].astype(float) if "high" in df.columns else close
    low = df["low"].astype(float) if "low" in df.columns else close
    out["guid_hist_years"] = round(len(df) / 244.0, 1)

    idxs = find_analogs(df, cfg)
    H, HS = cfg["horizon"], cfg["horizon_short"]
    recs = []
    for i in idxs:
        entry = float(close.iloc[i])
        if entry <= 0:
            continue
        fwd_h = high.iloc[i + 1: i + 1 + H]
        fwd_l = low.iloc[i + 1: i + 1 + H]
        fwd_c = close.iloc[i + 1: i + 1 + H]
        if len(fwd_h) < H * 0.9:
            continue
        max_gain = float(fwd_h.max()) / entry - 1.0
        mae = float(fwd_l.min()) / entry - 1.0
        ret_h = float(fwd_c.iloc[-1]) / entry - 1.0
        short_c = close.iloc[i + 1: i + 1 + HS]
        ret_s = (float(short_c.iloc[-1]) / entry - 1.0) if len(short_c) else None
        recs.append({
            "date": str(df["date"].iloc[i]),
            "entry": round(entry, 2),
            "max_gain_pct": round(max_gain * 100, 1),
            "mae_pct": round(mae * 100, 1),
            "ret_1y_pct": round(ret_h * 100, 1),
            "ret_6m_pct": (round(ret_s * 100, 1) if ret_s is not None else None),
        })

    out["guid_n"] = len(recs)
    out["guid_samples"] = recs
    if len(recs) < cfg["min_samples"]:
        out["guid_note"] = (f"历史同类形态仅 {len(recs)} 次, 样本不足以给出频率"
                            f"(需≥{cfg['min_samples']}次)")
        return out

    gains = [r["max_gain_pct"] for r in recs]
    maes = [r["mae_pct"] for r in recs]
    rets = [r["ret_1y_pct"] for r in recs]
    out["guid_probs"] = [
        {"target": t, "prob": round(sum(1 for g in gains if g >= t) / len(gains), 3),
         "hits": sum(1 for g in gains if g >= t)}
        for t in cfg["targets"]
    ]
    out["guid_med_gain"] = round(float(np.median(gains)), 1)
    out["guid_med_mae"] = round(float(np.median(maes)), 1)
    out["guid_win_rate"] = round(sum(1 for r in rets if r > 0) / len(rets), 3)

    # 买入区间: 上沿=关键支撑(没有就现价), 下沿=按历史中位 MAE 再让一档
    base = support_price if (support_price and support_price > 0) else price_now
    if base and base > 0:
        out["guid_buy_high"] = round(float(base), 2)
        out["guid_buy_low"] = round(float(base) * (1 + min(0.0, out["guid_med_mae"]) / 100.0 * 0.5), 2)

    top = out["guid_probs"][0] if out["guid_probs"] else None
    parts = [f"历史{out['guid_hist_years']}年内同类形态 {len(recs)} 次"]
    for p in out["guid_probs"]:
        if p["prob"] > 0:
            parts.append(f"涨{p['target']}%+ 出现{p['hits']}/{len(recs)}次({p['prob']*100:.0f}%)")
    parts.append(f"期间最大回撤中位 {out['guid_med_mae']:.0f}%")
    out["guid_note"] = "；".join(parts)
    return out
