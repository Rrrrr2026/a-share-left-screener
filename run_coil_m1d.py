# -*- coding: utf-8 -*-
"""M1 腾讯限额窗口版: 每窗口只抓 ~700 只 (≈1400 请求, 主动留在 WAF 阈值之下),
冷却 40 分钟再抓下一窗; 13:10-15:40 本地时间让路给生产流水线。达标后跑形态扫描。"""
import datetime as dt
import io
import logging
import os
import sys
import time

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

from ashare import market as mkt
from ashare import pricestore as ps
from ashare.config import DATA_DIR

TARGET = 5000
WINDOW_CODES = 700            # ×2页 ≈ 1400 请求 < ~1900 封禁阈值
COOLDOWN_S = 2400             # 窗口间冷却 40 分钟
PROD_START, PROD_END = (13, 10), (15, 40)   # 本地时间生产窗口, 让路


def in_prod_window() -> bool:
    now = dt.datetime.now()
    t = (now.hour, now.minute)
    return PROD_START <= t <= PROD_END


codes = mkt.universe_codes()
print(f"UNIVERSE {len(codes)}", flush=True)
assert len(codes) > 4000

for rnd in range(1, 30):
    while in_prod_window():
        print("YIELD production window, sleep 10min", flush=True)
        time.sleep(600)
    have = set(ps.last_dates())
    todo = [c for c in codes if c not in have]
    if not todo:
        break
    chunk = todo[:WINDOW_CODES]
    r = ps.backfill(codes=chunk)
    cov = ps.coverage()
    print(f"ROUND {rnd} fetched+{r['fetched']}/{len(chunk)} COVERAGE {cov['codes']} codes, "
          f"idx {cov['index_bars']}", flush=True)
    if cov["codes"] >= TARGET and cov["index_bars"] and cov["index_bars"] > 1000:
        break
    if r["fetched"] == 0:
        print("WAIT source unhealthy, sleep 40min", flush=True)
    time.sleep(COOLDOWN_S)
else:
    raise AssertionError(f"30轮仍未达标: {ps.coverage()}")

print("COVERAGE FINAL", ps.coverage(), flush=True)

from ashare.coilscan import build_quality_at, run

qa = build_quality_at(cache=os.path.join(DATA_DIR, "quality_timeline.json"))
res = run(quality_at=qa)
print("M1 SCAN DONE episodes=", res["meta"]["n_episodes"], flush=True)
