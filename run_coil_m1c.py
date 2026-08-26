# -*- coding: utf-8 -*-
"""M1 东财备源版: 腾讯WAF封禁期间改用东财补齐价格库 (断点续传), 然后跑形态扫描。"""
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

# 换用东财取数钩子 (腾讯被WAF拦, 一个请求都别发)
mkt.MARKET.fetch_bars_bulk = mkt.fetch_bars_bulk_em
mkt.MARKET.fetch_index_bars = mkt.fetch_index_bars_em

TARGET = 5000
WAIT_BLOCKED = 900            # 东财限流后等 15 分钟
WAIT_OK = 60

codes = mkt.universe_codes()
print(f"UNIVERSE {len(codes)}", flush=True)
assert len(codes) > 4000

for rnd in range(1, 30):
    r = ps.backfill(codes=codes)
    cov = ps.coverage()
    print(f"ROUND {rnd} fetched+{r['fetched']} COVERAGE {cov['codes']} codes, "
          f"idx {cov['index_bars']}", flush=True)
    if cov["codes"] >= TARGET and cov["index_bars"] and cov["index_bars"] > 1000:
        break
    if r["fetched"] == 0 or r.get("aborted"):
        print(f"WAIT em-throttled, sleep {WAIT_BLOCKED}s", flush=True)
        time.sleep(WAIT_BLOCKED)
    else:
        time.sleep(WAIT_OK)
else:
    raise AssertionError(f"30轮仍未达标: {ps.coverage()}")

print("COVERAGE FINAL", ps.coverage(), flush=True)

from ashare.coilscan import build_quality_at, run

qa = build_quality_at(cache=os.path.join(DATA_DIR, "quality_timeline.json"))
res = run(quality_at=qa)
print("M1 SCAN DONE episodes=", res["meta"]["n_episodes"], flush=True)
