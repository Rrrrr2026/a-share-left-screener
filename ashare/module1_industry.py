#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
模块1 — 行业景气度评分 (Industry Prosperity Score)
==================================================
对每个 (东财) 一级行业, 用五大支柱算景气总分, 选 Top N 作为模块2的候选池。

五大支柱 (各自先横截面归一, 再加权):
  A 趋势   weight 0.25  —— 指数 vs MA60/MA120 + 60日斜率
  B 动量   weight 0.25  —— 20日/60日涨幅 + 对沪深300的60日超额
  C 广度   weight 0.20  —— 成分股在MA60上方比例 / 20日正收益比例 / 近5日涨跌家数差
  D 资金   weight 0.15  —— 行业主力净流入(近5日); 拿不到则权重并入 A、B
  E 基本面 weight 0.15  —— 行业聚合净利/营收同比; 拿不到则权重并入其它

归一: 每个支柱的原始值先做横截面 zscore 合成, 再转成 0-100 横截面百分位。
总分 = 100 * Σ(归一权重_i * 百分位_i/100)。
趋势硬门槛: 行业指数需在 MA120 上方(或容差内) 才有资格入选。
"""
from __future__ import annotations
import logging
import numpy as np
import pandas as pd

from .config import CONFIG
from . import datasource as ds
from . import indicators as ind
from .statutil import zscore, cross_sectional_percentile, nanmean, safe_div

log = logging.getLogger("ashare.module1")


def _ret(close: pd.Series, bars: int) -> float:
    s = close.dropna()
    if len(s) <= bars:
        return np.nan
    return float(s.iloc[-1] / s.iloc[-1 - bars] - 1.0)


def _industry_index_features(hist: pd.DataFrame, bench_ret60: float) -> dict:
    """从行业指数日线算 趋势/动量 的原始分量。"""
    close = hist["close"].astype(float)
    px = close.iloc[-1]
    ma60 = close.rolling(60).mean().iloc[-1]
    ma120 = close.rolling(120).mean().iloc[-1]
    t1 = safe_div(px - ma60, ma60)
    t2 = safe_div(px - ma120, ma120)
    t3 = ind.reg_slope_norm(close, 60)
    m1 = _ret(close, 20)
    m2 = _ret(close, 60)
    m3 = (m2 - bench_ret60) if (not np.isnan(m2) and not np.isnan(bench_ret60)) else np.nan
    return {
        "idx_close": float(px),
        "ma120": float(ma120) if not np.isnan(ma120) else np.nan,
        "above_ma120": (not np.isnan(ma120)) and px >= ma120,
        "t1": t1, "t2": t2, "t3": t3,
        "m1": m1, "m2": m2, "m3": m3,
    }


def _breadth(cons: pd.DataFrame, sample: int) -> dict:
    """成分股广度: 抽样成分股, 算 MA60上方比例 / 20日正收益比例 / 近5日涨跌家数差。"""
    if cons is None or cons.empty:
        return {"b1": np.nan, "b2": np.nan, "b3": np.nan, "n": 0}
    codes = list(cons["code"])[:sample]
    above_ma60, pos20, adv, dec, n = 0, 0, 0, 0, 0
    for code in codes:
        h = ds.fetch_hist(code)
        if h is None or len(h) < 65:
            continue
        c = h["close"].astype(float)
        px = c.iloc[-1]
        ma60 = c.rolling(60).mean().iloc[-1]
        if not np.isnan(ma60):
            above_ma60 += 1 if px >= ma60 else 0
        r20 = _ret(c, 20)
        if not np.isnan(r20):
            pos20 += 1 if r20 > 0 else 0
        r5 = _ret(c, 5)
        if not np.isnan(r5):
            if r5 > 0:
                adv += 1
            elif r5 < 0:
                dec += 1
        n += 1
    if n == 0:
        return {"b1": np.nan, "b2": np.nan, "b3": np.nan, "n": 0}
    return {
        "b1": above_ma60 / n,
        "b2": pos20 / n,
        "b3": safe_div(adv - dec, n, 0.0),
        "n": n,
    }


def compute_industry_scores(progress_cb=None) -> pd.DataFrame:
    """
    返回 DataFrame, 一行一个行业, 列:
      industry, prosperity_score, trend, momentum, breadth, capital, fundamental,
      idx_close, ma120, above_ma120, eligible, selected
    sub-score 列 (trend/momentum/breadth/capital/fundamental) 为 0-100 横截面百分位;
    不可用的支柱该列为 NaN。
    """
    cfg = CONFIG["industry"]
    ind_list = ds.fetch_industry_list()
    if ind_list is None or ind_list.empty:
        log.warning("行业列表拉取失败, 模块1 返回空")
        return pd.DataFrame()

    industries = list(ind_list["industry"].dropna().unique())
    bench = ds.fetch_benchmark_close()
    bench_ret60 = np.nan
    if bench is not None and len(bench) > 61:
        bc = bench["close"].astype(float)
        bench_ret60 = float(bc.iloc[-1] / bc.iloc[-61] - 1.0)

    # 资金流 (可选)
    flow = ds.fetch_industry_fund_flow()
    flow_map = {}
    if flow is not None and not flow.empty:
        flow_map = dict(zip(flow["industry"], flow["net_inflow"]))

    def _one_industry(name):
        hist = ds.fetch_industry_hist(name)
        if hist is None or len(hist) < 130:
            log.debug("行业 %s 指数数据不足, 跳过", name)
            return None
        feat = _industry_index_features(hist, bench_ret60)
        cons = ds.fetch_industry_cons(name)
        br = _breadth(cons, cfg["breadth_sample"])
        return {
            "industry": name,
            **feat,
            "b1": br["b1"], "b2": br["b2"], "b3": br["b3"], "breadth_n": br["n"],
            "c1": flow_map.get(name, np.nan),
        }

    # 各行业相互独立, 并发拉取指数/成分以加速
    from concurrent.futures import ThreadPoolExecutor, as_completed
    import os as _os
    workers = CONFIG["fetch"].get("max_workers") or min(16, (_os.cpu_count() or 4) * 2)
    rows = []
    total = len(industries)
    done = 0
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futs = {pool.submit(_one_industry, n): n for n in industries}
        for fut in as_completed(futs):
            done += 1
            if progress_cb:
                progress_cb(done, total, futs[fut])
            try:
                r = fut.result()
            except Exception as e:
                log.debug("行业 %s 计算失败: %s", futs[fut], e)
                r = None
            if r is not None:
                rows.append(r)

    if not rows:
        return pd.DataFrame()

    df = pd.DataFrame(rows)

    # ---- 支柱A 趋势: zscore(t1,t2,t3) 横截面 -> 行均值 ----
    df["A_raw"] = pd.concat([zscore(df["t1"]), zscore(df["t2"]), zscore(df["t3"])],
                            axis=1).mean(axis=1, skipna=True)
    # ---- 支柱B 动量 ----
    df["B_raw"] = pd.concat([zscore(df["m1"]), zscore(df["m2"]), zscore(df["m3"])],
                            axis=1).mean(axis=1, skipna=True)
    # ---- 支柱C 广度: b1,b2 ∈[0,1]; b3=(涨-跌)/n ∈[-1,1], 先线性映射到[0,1]再取均值 ----
    b3_scaled = (df["b3"] + 1.0) / 2.0
    df["C_raw"] = pd.concat([df["b1"], df["b2"], b3_scaled], axis=1).mean(axis=1, skipna=True)
    # ---- 支柱D 资金 ----
    has_capital = df["c1"].notna().any()
    df["D_raw"] = zscore(df["c1"]) if has_capital else np.nan
    # ---- 支柱E 基本面: 默认不计算 (聚合财务成本高), 留接口, 权重按行并入其它 ----
    has_fundamental = False
    df["E_raw"] = np.nan

    # 每个支柱 -> 横截面百分位 0-100。fill=None 保留 NaN: 某行该支柱无数据时,
    # 既不会被给中位分, 其权重也会在 _score_row 里按行重新分配 (而非全局判断)。
    df["trend"] = cross_sectional_percentile(df["A_raw"], fill=None)
    df["momentum"] = cross_sectional_percentile(df["B_raw"], fill=None)
    df["breadth"] = cross_sectional_percentile(df["C_raw"], fill=None)
    df["capital"] = cross_sectional_percentile(df["D_raw"], fill=None) if has_capital else np.nan
    df["fundamental"] = cross_sectional_percentile(df["E_raw"], fill=None) if has_fundamental else np.nan

    log.info("景气支柱可用情况: 资金=%s 基本面=%s (缺数据的支柱按行重新分配权重)",
             has_capital, has_fundamental)

    weights = cfg["weights"]

    def _score_row(r):
        # 仅对"该行有数据"的支柱加权, 并在这些支柱上重新归一权重
        num, den = 0.0, 0.0
        for pillar, wt in weights.items():
            pct = r.get(pillar)
            if pct is not None and not (isinstance(pct, float) and np.isnan(pct)):
                num += wt * (pct / 100.0)
                den += wt
        return round(100.0 * num / den, 2) if den > 0 else np.nan

    df["prosperity_score"] = df.apply(_score_row, axis=1)

    # 趋势硬门槛
    if cfg["trend_gate_enabled"]:
        tol = cfg["trend_gate_tolerance_pct"] / 100.0
        df["eligible"] = df.apply(
            lambda r: (not np.isnan(r["ma120"])) and r["idx_close"] >= r["ma120"] * (1 - tol),
            axis=1)
    else:
        df["eligible"] = True

    df = df.sort_values("prosperity_score", ascending=False).reset_index(drop=True)

    # Top N 入选 (仅在合格者中取)
    if cfg["use_full_market"]:
        df["selected"] = True
    else:
        elig = df[df["eligible"]].head(cfg["top_n"])
        sel_names = set(elig["industry"])
        df["selected"] = df["industry"].isin(sel_names)

    cols = ["industry", "prosperity_score", "trend", "momentum", "breadth",
            "capital", "fundamental", "idx_close", "ma120", "above_ma120",
            "eligible", "selected", "breadth_n"]
    return df[[c for c in cols if c in df.columns]]
