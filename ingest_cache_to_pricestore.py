#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把当日流水线 hist 缓存灌进共享价格库 (零网络请求)。

背景 (2026-09-01): 价格库唯一的日更来源是 lab 18:30 的 update_daily — 每天 ~5177 个
fuyao 请求, 与 14:00 流水线的 ~5200 次共享同一配额, 晚间必然中途被限流 (实测止步
~2000 只), lab 的 50% 新鲜度门槛永远过不去。而流水线缓存里已有全部当日数据, 复用即可。
防单位混库守卫: 逐码与库内重叠日成交量比对, 比例偏离 [0.5, 2] 的代码跳过
(腾讯"手" vs fuyao"股"事故教训 — 价格库必须单源口径, 见 CHRONICLE 数据源之战)。
用法: 流水线跑完后执行 (run_a.sh 已接线); 幂等, 可重复跑。
"""
import datetime as dt
import os
import sqlite3
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from ashare.config import CONFIG                 # noqa: E402
from ashare import datasource as ds              # noqa: E402

DB = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "pricestore.db")
KEEP_DAYS = 10


def main():
    if not os.path.exists(DB):
        print("pricestore.db 不存在, 跳过")
        return
    conn = sqlite3.connect(DB)
    codes = [r[0] for r in conn.execute("SELECT DISTINCT code FROM bars")]
    f = CONFIG["fetch"]
    today = dt.date.today().isoformat()
    cutoff = (dt.date.today() - dt.timedelta(days=KEEP_DAYS)).isoformat()
    n_ok = n_miss = n_unit = 0
    for code in codes:
        key = ds._cache_key("hist", code, f["adjust"], f["lookback_days"], today)
        df = ds._cache_load(key)
        if df is None or len(df) == 0 or "volume" not in df.columns:
            n_miss += 1
            continue
        sub = df[df["date"].astype(str) >= cutoff]
        if len(sub) == 0:
            n_miss += 1
            continue
        have = dict(conn.execute(
            "SELECT d, v FROM bars WHERE code=? AND d>=?", (code, cutoff)))
        ratio_ok = True
        for _, r in sub.iterrows():
            v0 = have.get(str(r["date"]))
            if v0 and v0 > 0 and r.get("volume") and float(r["volume"]) > 0:
                q = float(r["volume"]) / float(v0)
                ratio_ok = 0.5 <= q <= 2.0
                break
        if not ratio_ok:
            n_unit += 1
            continue
        rows = [(code, str(r["date"]), float(r["open"]), float(r["high"]),
                 float(r["low"]), float(r["close"]), float(r.get("volume") or 0))
                for _, r in sub.iterrows()]
        conn.executemany(
            "INSERT OR REPLACE INTO bars(code,d,o,h,l,c,v) VALUES(?,?,?,?,?,?,?)", rows)
        n_ok += 1
    conn.commit()
    n_today = conn.execute(
        "SELECT COUNT(DISTINCT code) FROM bars WHERE d=?", (today,)).fetchone()[0]
    print(f"缓存灌库: 成功 {n_ok} / 无缓存 {n_miss} / 单位存疑跳过 {n_unit}; "
          f"今日({today})bar覆盖 {n_today}/{len(codes)}")
    conn.close()


if __name__ == "__main__":
    main()
