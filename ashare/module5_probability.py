#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
模块5 — 历史类比·概率化目标位 (Historical-Analog Probability Targets)
=====================================================================
回答的问题: **这只股票以前每次跌到现在这么深之后, 后面都涨了多少?**

做法 (纯经验统计, 不做任何模型预测):
  1. 取该股尽量长的历史日线(默认10年, 前复权);
  2. 逐日算「距过去250日最高价的回撤」, 找出历史上回撤 >= 当前回撤(打8折放宽)
     的所有日子 —— 这些就是"和现在一样惨"的历史时点;
  3. 连续的下跌日会挤在一起, 用冷却期(默认120交易日)把它们归并成互不重叠的
     **独立事件**, 避免同一轮下跌被重复统计成几十个样本;
  4. 对每个事件, 看它之后 H 个交易日内(默认250日≈1年):
       · 最大涨幅 max_gain  = 期间最高价 / 事件当日价 - 1   (决定"能涨到哪")
       · 期末收益 ret_end   = 第H日收盘 / 事件当日价 - 1     (决定"拿满一年什么结果")
     只统计前向窗口**完整**的事件(不足H日的丢弃), 避免最近的事件因数据没走完
     而系统性低估涨幅;
  5. 把这些事件的 max_gain 排成经验分布, 得到「多大概率能涨到 +20%/+50%/+100%/+200%」,
     并按当前价换算成对应的卖出参考位。

⚠ 必须诚实的局限 (都会写进输出, 前端一并展示):
  · 样本量很小 —— 一只股票10年里"跌这么深"通常只有 2~8 次, 概率只是**历史频率**,
    不是未来概率; 样本 < 3 时标注"样本不足", 不给概率分层;
  · 幸存者/制度偏差: 过去的大跌发生在不同的市场环境、不同的公司基本面下;
  · 前复权序列会随分红送转重算, 长期历史的绝对价位仅供比例参考。
  这是"历史上同类情形的统计", **不构成任何预测或投资建议**。
"""
from __future__ import annotations
import logging

import numpy as np
import pandas as pd

log = logging.getLogger("ashare.module5")

# 概率分层: 想赚到这些幅度的历史频率各是多少
DEFAULT_TIERS = (0.20, 0.50, 1.00, 2.00)


def _episodes(dd: pd.Series, thresh: float, cooldown: int) -> list[int]:
    """把满足回撤阈值的日子归并成互不重叠的独立事件, 返回事件的位置下标。"""
    idx = np.where(dd.values >= thresh)[0]
    out, last = [], -10**9
    for i in idx:
        if i - last >= cooldown:
            out.append(int(i))
            last = i
    return out


def probability_profile(df: pd.DataFrame,
                        horizon: int = 250,
                        # 冷却期 = 统计窗口: 保证各事件的前向窗口**互不重叠**(样本独立)。
                        # 若取更短(如120), 同一轮熊市会被反复采样, 样本相关且系统性
                        # 低估反弹幅度(实测翔丰华: 重叠采样中位涨28% vs 独立采样49%)。
                        cooldown: int = 250,
                        min_drawdown: float = 0.25,
                        high_window: int = 250,
                        tiers=DEFAULT_TIERS) -> dict:
    """df: 长历史日线(date, close[, high])。返回概率分层与目标位统计。"""
    empty = {"prob_n": 0, "prob_tiers": [], "prob_median_max_gain": None,
             "prob_median_ret": None, "prob_hist_years": None,
             "prob_dd_thresh_pct": None, "prob_note": None}
    if df is None or len(df) < high_window + horizon + 20:
        return empty
    d = df.sort_values("date").reset_index(drop=True)
    close = pd.to_numeric(d["close"], errors="coerce")
    high = pd.to_numeric(d["high"], errors="coerce") if "high" in d.columns else close
    if close.isna().all():
        return empty

    roll_max = close.rolling(high_window, min_periods=high_window // 2).max()
    dd = (roll_max - close) / roll_max            # 距250日高点的回撤(0~1)
    dd_now = float(dd.iloc[-1]) if not np.isnan(dd.iloc[-1]) else 0.0
    # 阈值: 至少和现在一样惨(打8折放宽以凑样本), 但不低于 min_drawdown
    thresh = max(min_drawdown, dd_now * 0.8)

    # 只在"前向窗口能走完"的区间里找事件 (末尾 horizon 天不参与)
    usable = dd.iloc[:len(d) - horizon]
    ev = _episodes(usable, thresh, cooldown)
    if not ev:
        return {**empty, "prob_dd_thresh_pct": round(thresh * 100, 1),
                "prob_hist_years": round(len(d) / 244.0, 1),
                "prob_note": "历史上没有出现过与当前同等深度的回撤(或数据不够长)"}

    max_gains, end_rets = [], []
    for i in ev:
        p0 = float(close.iloc[i])
        if not np.isfinite(p0) or p0 <= 0:
            continue
        fwd_hi = high.iloc[i + 1:i + 1 + horizon]
        fwd_cl = close.iloc[i + 1:i + 1 + horizon]
        if len(fwd_cl) < horizon:
            continue
        max_gains.append(float(fwd_hi.max()) / p0 - 1.0)
        end_rets.append(float(fwd_cl.iloc[-1]) / p0 - 1.0)

    n = len(max_gains)
    if n == 0:
        return {**empty, "prob_dd_thresh_pct": round(thresh * 100, 1),
                "prob_hist_years": round(len(d) / 244.0, 1)}

    mg = np.array(max_gains)
    tier_rows = []
    for t in tiers:
        p = float((mg >= t).mean())
        tier_rows.append({"gain_pct": round(t * 100), "prob": round(p, 3)})
    return {
        "prob_n": n,
        "prob_tiers": tier_rows,
        "prob_median_max_gain": round(float(np.median(mg)) * 100, 1),
        "prob_median_ret": round(float(np.median(end_rets)) * 100, 1),
        "prob_hist_years": round(len(d) / 244.0, 1),
        "prob_dd_thresh_pct": round(thresh * 100, 1),
        "prob_note": (f"基于该股近{round(len(d)/244.0,1)}年内 {n} 次"
                      f"回撤≥{round(thresh*100)}% 的历史情形, 统计其后{horizon}"
                      f"个交易日的表现(历史频率, 非预测)"),
    }


def attach_targets(prob: dict, price: float | None,
                   support_price: float | None = None,
                   breakdown_price: float | None = None) -> dict:
    """把概率分层换算成具体的卖出参考位, 并带上买入参考/失效位。"""
    out = dict(prob)
    if price and prob.get("prob_tiers"):
        for row in out["prob_tiers"]:
            row["target"] = round(price * (1 + row["gain_pct"] / 100.0), 2)
    out["buy_ref"] = round(float(support_price), 2) if support_price else None
    out["stop_ref"] = round(float(breakdown_price), 2) if breakdown_price else None
    return out


def summary_text(prob: dict) -> str | None:
    """一句话中文摘要 (主表悬浮/详情用)。样本不足时说清楚。"""
    n = prob.get("prob_n") or 0
    if n < 3:
        return f"历史同类深跌样本仅{n}次, 不足以统计概率" if n else None
    parts = []
    for row in prob.get("prob_tiers") or []:
        if row["prob"] > 0:
            tgt = f"→{row['target']}" if row.get("target") else ""
            parts.append(f"{row['prob']*100:.0f}%概率涨{row['gain_pct']}%{tgt}")
    if not parts:
        return f"{n}次历史样本中, 均未出现明显反弹"
    return "；".join(parts) + f"（{n}次样本中位最大涨幅{prob.get('prob_median_max_gain')}%）"
