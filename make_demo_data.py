#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
离线演示数据 (Offline demo data)
================================
不联网! 用合成日线(刻意做出"上升通道回踩下轨/接近前低"的左侧形态)跑通真实的
模块2/3/4 打分逻辑, 写入 SQLite 并导出仪表盘, 便于在没有网络时先看效果, 也作为
离线集成自测。

    python make_demo_data.py
然后双击 dashboard/index.html。
"""
from __future__ import annotations
import sys
import datetime as dt
import numpy as np
import pandas as pd

for _s in (sys.stdout, sys.stderr):   # Windows 控制台中文/emoji 兼容
    try:
        _s.reconfigure(encoding="utf-8")
    except Exception:
        pass

from ashare import db
from ashare import indicators as ind
from ashare import module2_tech as m2
from ashare import module3_fundamentals as m3
from ashare import module4_crossscore as m4
from ashare import export_data as ex


DEMO_INDUSTRIES = [
    # (行业, 景气分, 趋势, 动量, 广度, 资金, 入选)
    ("半导体",     86.0, 90, 88, 80, 75, True),
    ("电力设备",   81.0, 84, 82, 78, 70, True),
    ("医疗器械",   76.0, 78, 74, 72, 66, True),
    ("软件开发",   72.0, 70, 75, 68, 60, True),
    ("通信设备",   69.0, 72, 66, 70, 58, True),
    ("汽车零部件", 65.0, 64, 68, 62, 55, True),
    ("有色金属",   61.0, 60, 63, 60, 52, True),
    ("食品饮料",   58.0, 56, 55, 60, 50, True),
    ("房地产",     32.0, 28, 30, 35, 40, False),
    ("煤炭",       29.0, 25, 27, 33, 38, False),
]

# (代码, 名称, 所属行业, 基价, ROE, PE, PE分位, 负债率, 净利同比)
DEMO_STOCKS = [
    ("600111", "演示半导A", "半导体",   45.0, 21.5, 28.0, 12, 35, 32.0),
    ("300222", "演示半导B", "半导体",   18.0, 16.2, 35.0, 22, 41, 18.5),
    ("002333", "演示电设A", "电力设备", 32.0, 14.8, 22.0, 30, 48, 12.0),
    ("601444", "演示电设B", "电力设备", 9.5,  9.2,  40.0, 55, 62,  3.0),
    ("300555", "演示医械A", "医疗器械", 55.0, 19.0, 31.0, 18, 30, 25.0),
    ("002666", "演示医械B", "医疗器械", 12.0, 7.5,  60.0, 70, 45, -8.0),
    ("300777", "演示软件A", "软件开发", 28.0, 11.0, 55.0, 40, 28, 15.0),
    ("688888", "演示软件B", "软件开发", 66.0, 4.5,  85.0, 88, 25, -25.0),
    ("002999", "演示通信A", "通信设备", 16.5, 13.3, 26.0, 25, 50,  9.0),
    ("600123", "演示汽零A", "汽车零部件", 21.0, 15.6, 19.0, 15, 44, 22.0),
    ("000456", "演示有色A", "有色金属", 13.0, 17.8, 14.0, 9,  52, 30.0),
    ("600789", "演示食饮A", "食品饮料", 88.0, 24.0, 24.0, 10, 33, 11.0),
    ("301010", "演示半导C", "半导体",   7.8,  -3.0, -1.0, 95, 71, -45.0),
    ("002120", "演示电设C", "电力设备", 24.0, 12.5, 30.0, 28, 49,  6.0),
]


def gen_ohlc(seed: int, base: float, n: int = 300, push: float = 0.0) -> pd.DataFrame:
    """生成"上升趋势 + 末段回踩到通道下轨/前低附近"的合成日线。
    在前段埋一个与现价同档的前低, 并把末价压到通道下轨附近, 形成左侧回踩。
    push 越大, 末段回踩越深 (用于把技术分校准到"强左侧"门槛之上)。"""
    rng = np.random.default_rng(seed)
    t = np.arange(n)
    drift = base * 0.0016
    trend = base + drift * t
    cycle = base * 0.09 * np.sin(2 * np.pi * t / 55.0)      # 制造若干摆动低点(前低)
    noise = rng.normal(0, base * 0.010, n).cumsum() * 0.22
    close = trend + cycle + noise
    close = np.maximum(close, base * 0.3)

    # 以"末段下压前"的序列拟合通道下轨, 作为回踩目标
    ch = ind.linreg_channel(pd.Series(close), 120, 2.0)
    lb = ch["lower_band"] if ch else close[-1] * 0.93
    target = lb * (1.012 - push)

    # 1) 前段埋一个"前期重要低点": 在 n-46 附近挖一个与 target 同档的低谷
    pk = n - 46
    width = 6
    for i in range(pk - width, pk + width + 1):
        if 0 <= i < n:
            w = 1.0 - abs(i - pk) / (width + 1.0)
            close[i] = min(close[i], target * (1.0 + 0.004) * (1 - 0.06 * w) + 0.06 * w * target)
    close[pk] = min(close[pk], target * 1.002)

    # 2) 末段 9 根平滑回踩到 target (贴近下轨 + 超跌)
    k = 9
    close[-k:] = np.linspace(close[-k - 1], target, k)
    close = np.maximum(close, base * 0.25)

    intraday = base * 0.012
    high = close + np.abs(rng.normal(0, intraday, n)) + intraday
    low = close - np.abs(rng.normal(0, intraday, n)) - intraday
    open_ = close + rng.normal(0, intraday, n)
    high = np.maximum.reduce([high, close, open_])
    low = np.minimum.reduce([low, close, open_])
    amount = base * 1e7 * (3 + np.abs(rng.normal(0, 1, n)))   # 远大于流动性门槛

    dates = pd.bdate_range(end=dt.date.today(), periods=n).strftime("%Y-%m-%d")
    return pd.DataFrame({
        "date": dates, "open": open_.round(2), "high": high.round(2),
        "low": low.round(2), "close": close.round(2),
        "volume": (amount / close).round(0), "amount": amount.round(0),
    })


def _demo_fund(roe, pe, pe_pct, debt, npy):
    f = {
        "pe_ttm": pe, "pe_pct": pe_pct, "pe_industry_median": round(pe * 1.1, 1),
        "pe_vs_industry": round(pe / (pe * 1.1), 2),
        "pb": round(max(0.4, pe / 12.0), 2), "pb_pct": min(99, pe_pct + 8),
        "dividend_yield": round(max(0, 3.5 - pe / 30.0), 2),
        "eps": round(max(-1.0, roe / 18.0), 2),
        "eps_yoy": npy, "roe": roe,
        "revenue_yoy": round(npy * 0.7, 1), "netprofit_yoy": npy,
        "gross_margin": round(28 + roe, 1), "debt_ratio": debt,
        "roe_trend": [{"date": f"{2021+i}-12-31",
                       "value": round(roe * (0.8 + 0.06 * i), 1)} for i in range(5)],
    }
    f["fund_flags"] = m3._flags(f)
    return f


def build_demo():
    run_date = dt.date.today().isoformat()
    started = dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    db.init_db()
    db.clear_run(run_date)   # 干净快照

    # 行业景气榜
    rows = []
    for name, score, tr, mo, br, ca, sel in DEMO_INDUSTRIES:
        rows.append({
            "industry": name, "prosperity_score": score,
            "trend": tr, "momentum": mo, "breadth": br, "capital": ca,
            "fundamental": None, "idx_close": 1000 + score * 5,
            "ma120": 1000 + score * 4, "above_ma120": sel,
            "eligible": sel, "selected": sel,
        })
    ind_df = pd.DataFrame(rows)
    db.save_industry_scores(run_date, ind_df)
    prosperity_map = dict(zip(ind_df["industry"], ind_df["prosperity_score"]))
    selected_inds = list(ind_df[ind_df["selected"]]["industry"])

    n_final = 0
    for i, (code, name, industry, base, roe, pe, pe_pct, debt, npy) in enumerate(DEMO_STOCKS):
        # 用真实打分器作"标尺", 逐步加深回踩直到技术分越过强左侧门槛(2.0), 让演示出现三种标签
        h, rec = None, None
        for attempt in range(16):
            h = gen_ohlc(seed=1000 + i, base=base, push=attempt * 0.004)
            rec, detail = m2.scan_one(code, name, h, None)
            if rec is not None and rec["tech_score"] >= 2.05:
                break
        if rec is None:
            continue
        spot_row = {
            "volume_ratio": round(0.8 + (i % 5) * 0.3, 2),
            "turnover": round(1.2 + (i % 7) * 0.6, 2),
            "amount": float(h["amount"].iloc[-1]),
            "pe_ttm": pe, "pb": round(pe / 12.0, 2),
        }
        rec, detail = m2.scan_one(code, name, h, spot_row)
        if rec is None:
            continue
        rec["industry"] = industry
        db.save_tech(run_date, [rec])
        db.save_detail(run_date, code, detail)
        f = _demo_fund(roe, pe, pe_pct, debt, npy)
        db.save_fundamental(run_date, code, f)
        fr = m4.cross_score(rec, f, prosperity_map.get(industry))
        db.save_final(run_date, [fr])
        n_final += 1

    finished = dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    db.log_run(run_date, started, finished, len(DEMO_STOCKS), n_final,
               selected_inds, "demo", "离线合成演示数据")
    ex.write_dashboard_js(run_date)
    ex.write_csv(run_date)
    print(f"✅ 演示数据已生成: 行业 {len(DEMO_INDUSTRIES)}, 候选 {n_final}。"
          f"请双击打开 dashboard/index.html")


if __name__ == "__main__":
    build_demo()
