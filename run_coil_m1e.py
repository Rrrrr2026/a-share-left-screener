# -*- coding: utf-8 -*-
"""M1 fuyao版: 用同花顺金融数据API补齐剩余代码 (整段单请求, 无配额斗争),
达标后跑蓄势形态扫描。断点续传, 幂等。"""
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

mkt.MARKET.fetch_bars_bulk = mkt.fetch_bars_bulk_fuyao   # 主源: fuyao

TARGET = 5000

codes = mkt.universe_codes()
print(f"UNIVERSE {len(codes)}", flush=True)
assert len(codes) > 4000

for rnd in range(1, 8):
    r = ps.backfill(codes=codes)
    cov = ps.coverage()
    print(f"ROUND {rnd} fetched+{r['fetched']} COVERAGE {cov['codes']} codes, "
          f"idx {cov['index_bars']}", flush=True)
    if cov["codes"] >= TARGET and cov["index_bars"] and cov["index_bars"] > 1000:
        break
    if r["fetched"] == 0:
        print("WAIT source empty, sleep 600s", flush=True)
        time.sleep(600)
else:
    raise AssertionError(f"未达标: {ps.coverage()}")

print("COVERAGE FINAL", ps.coverage(), flush=True)

from ashare.coilscan import build_quality_at, run

qa = build_quality_at(cache=os.path.join(DATA_DIR, "quality_timeline.json"))
res = run(quality_at=qa)
print("M1 SCAN DONE episodes=", res["meta"]["n_episodes"], flush=True)
