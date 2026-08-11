#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
机会温度计 (Opportunity Thermometer)
====================================
回答"今天该不该重仓"这一件事: 把当天榜单的**整体机会质量**压缩成一个 0-100 的温度,
并给出相对自身历史的分位 —— 优质左侧机会在时间上是扎堆出现的(恐慌/洗盘时集中冒出),
温度处于历史高位时才值得下重手, 平庸日子留好子弹, 避免"开超市"式撒胡椒面。

组件 (每项都取"相对自身历史的百分位", 再加权平均):
  n_strong        当日 ✅强左侧 数量           (机会广度)
  n_coil          当日 🚀蓄势待发 数量          (临突破广度)
  avg_rr          交易计划平均盈亏比            (机会质量)
  avg_win         交易计划基准目标平均胜率       (机会质量)
  near_support    贴支撑(±2%内)候选占比         (成熟度: 位置到了没)
  idx_dd          基准指数距250日高回撤%        (市场恐慌度: 跌得深, 便宜货多)

⚠️ 诚实声明: 温度是"当下机会相对自己历史的排位", 不是对未来的预测;
历史样本越长越可信 (前端会标注样本天数)。
"""
from __future__ import annotations
import os
import json
import glob
import logging

log = logging.getLogger("ashare.opportunity")

# 组件权重 (和为1)
WEIGHTS = {
    "n_strong": 0.18,
    "n_coil": 0.10,
    "avg_rr": 0.18,
    "avg_win": 0.17,
    "near_support": 0.12,
    "idx_dd": 0.25,
}
MIN_HISTORY = 8      # 历史样本少于此仅作参考 (前端标注)


def compute_components(candidates: list, bench_close=None) -> dict:
    """从当日候选行 + 基准收盘序列 计算原始组件值。candidates 为 export 的行 dict。"""
    n = len(candidates) or 1
    n_strong = sum(1 for c in candidates if "强左侧" in (c.get("tag") or ""))
    n_coil = sum(1 for c in candidates if "蓄势待发" in (c.get("tag") or ""))
    rrs, wins = [], []
    for c in candidates:
        p = c.get("plan") or {}
        if p.get("entry_mode") == "breakout":
            continue                      # 突破型不带胜率, 不进质量均值
        if p.get("rr") is not None:
            rrs.append(float(p["rr"]))
        base = (p.get("targets") or {}).get("base") or {}
        if base.get("prob_pct") is not None:
            wins.append(float(base["prob_pct"]))
    near = sum(1 for c in candidates
               if c.get("dist_support_pct") is not None
               and abs(c["dist_support_pct"]) <= 2.0)
    idx_dd = None
    try:
        if bench_close is not None and len(bench_close) >= 60:
            s = [float(x) for x in list(bench_close)[-250:]]
            idx_dd = round((max(s) - s[-1]) / max(s) * 100.0, 2)
    except Exception:
        idx_dd = None
    return {
        "n_strong": n_strong,
        "n_coil": n_coil,
        "avg_rr": round(sum(rrs) / len(rrs), 2) if rrs else None,
        "avg_win": round(sum(wins) / len(wins), 1) if wins else None,
        "near_support": round(near / n * 100.0, 1),
        "idx_dd": idx_dd,
    }


def load_history_components(history_dir: str, exclude_date: str | None = None) -> list:
    """从 history/day_*.json 读取"该日之前"的组件 —— 严格只用过去 (含当日重算历史
    快照的场景也不能偷看未来, 否则回看视图里的温度带前视偏差)。"""
    out = []
    for fp in sorted(glob.glob(os.path.join(history_dir, "day_*.json"))):
        d = os.path.basename(fp)[4:14]
        if exclude_date and d >= exclude_date:
            continue
        try:
            with open(fp, encoding="utf-8") as f:
                meta = (json.load(f).get("meta") or {})
            comp = (meta.get("opp") or {}).get("components")
            if comp:
                out.append(comp)
        except Exception:
            continue
    return out


def _pctile(value, history_vals) -> float | None:
    vals = [v for v in history_vals if v is not None]
    if value is None or not vals:
        return None
    lt = sum(1 for v in vals if v < value)
    eq = sum(1 for v in vals if v == value)
    return (lt + 0.5 * eq) / len(vals) * 100.0


def temperature(components: dict, history: list) -> dict:
    """当日组件 vs 历史 → 温度(0-100) + 判定。history 为往日组件 dict 列表。
    每个组件按自身有效样本数把关: 样本不足的组件不给分位、不进加权
    (N=1-2 时的 P100/P0 是噪音, 不能顶着全量样本天数的名义误导使用者)。"""
    pctiles, n_by, wsum, acc = {}, {}, 0.0, 0.0
    for k, w in WEIGHTS.items():
        vals = [h.get(k) for h in history]
        n_k = sum(1 for v in vals if v is not None)
        n_by[k] = n_k
        p = _pctile(components.get(k), vals) if n_k >= MIN_HISTORY else None
        pctiles[k] = round(p, 0) if p is not None else None
        if p is not None:
            acc += w * p
            wsum += w
    score = round(acc / wsum) if wsum > 0 else None
    n_hist = len(history)
    if score is None:
        verdict = "insufficient"
    elif score >= 80:
        verdict = "deploy"          # 重仓分批
    elif score >= 60:
        verdict = "lean_in"         # 加大试仓
    elif score >= 40:
        verdict = "normal"          # 正常试仓
    else:
        verdict = "hold_fire"       # 观望留子弹
    return {
        "score": score,
        "verdict": verdict,
        "components": components,
        "pctiles": pctiles,
        "n_by_component": n_by,
        "n_history": n_hist,
        "reliable": n_hist >= MIN_HISTORY,
    }
