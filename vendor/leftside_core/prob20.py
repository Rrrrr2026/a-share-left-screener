#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
模块10 — "30日内涨20%"的历史概率
================================
给错杀候选 / 优质公司加一列: 按该股自身多年日线统计, 从"与今天状态相似"的
日子出发, 随后 30 个交易日内最高价曾达到 +20% 的频率。
  * 错杀候选: 条件 = 当日距近250日高点回撤 >= 18% (与错杀门槛一致) —— 回答
    "这只股以前跌成这样时, 多常在30天内反弹20%";
  * 优质公司: 无条件 (任意时点起算) —— 优质白马波动小, 这个数天然偏低, 如实展示。
  * 窗口每隔 5 个交易日取一次, 减少重叠窗口的伪样本;
  * 向同组汇总频率收缩 (伪样本 20 个窗口), 小样本不许吹大数;
  * 这是历史频率, 不是预测; 样本不足 (n<8) 时不出数。
"""
from __future__ import annotations
import logging
import numpy as np

log = logging.getLogger("leftside_core.prob20")

HORIZON = 30
GAIN = 0.20
STRIDE = 5
SHRINK_K = 20
MIN_N = 8
DD_LOOKBACK = 250
DD_MIN = 0.18


def _event_counts(high: np.ndarray, close: np.ndarray, mask) -> tuple[int, int]:
    n = len(close)
    hits = cnt = 0
    t = 0
    while t < n - HORIZON:
        if mask is None or mask[t]:
            fut = float(np.max(high[t + 1:t + 1 + HORIZON]))
            hits += int(fut >= close[t] * (1.0 + GAIN))
            cnt += 1
            t += STRIDE
        else:
            t += 1
    return hits, cnt


def _dd_mask(high: np.ndarray, close: np.ndarray) -> np.ndarray:
    n = len(close)
    mask = np.zeros(n, dtype=bool)
    run = np.full(n, np.nan)
    for i in range(n):
        lo = max(0, i - DD_LOOKBACK + 1)
        if i - lo + 1 >= 60:
            run[i] = float(np.max(high[lo:i + 1]))
    with np.errstate(invalid="ignore"):
        dd = close / run - 1.0
    mask[np.isfinite(dd) & (dd <= -DD_MIN)] = True
    return mask


def annotate(items: list[dict], get_hist, conditional: bool, key: str = "p20") -> int:
    """items: dict 列表 (需含 code); get_hist(code) -> (high ndarray, close ndarray) 或 None。
    就地写入 item[key] (百分比, 收缩后) 与 item[key+'_n'] (窗口数); 返回写入条数。"""
    raw = []
    for it in items:
        try:
            h = get_hist(it.get("code"))
        except Exception as e:
            log.debug("hist %s 失败: %s", it.get("code"), e)
            h = None
        if h is None:
            raw.append(None)
            continue
        high, close = (np.asarray(h[0], dtype=float), np.asarray(h[1], dtype=float))
        if len(close) < HORIZON + 60:
            raw.append(None)
            continue
        mask = _dd_mask(high, close) if conditional else None
        raw.append(_event_counts(high, close, mask))
    tot_h = sum(r[0] for r in raw if r)
    tot_n = sum(r[1] for r in raw if r)
    prior = (tot_h / tot_n) if tot_n else None
    n_out = 0
    for it, r in zip(items, raw):
        if not r or r[1] < MIN_N or prior is None:
            continue
        hits, n = r
        p = (hits + SHRINK_K * prior) / (n + SHRINK_K)
        it[key] = round(p * 100.0, 1)
        it[key + "_n"] = int(n)
        n_out += 1
    return n_out
