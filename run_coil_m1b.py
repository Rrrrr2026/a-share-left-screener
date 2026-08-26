# -*- coding: utf-8 -*-
"""M1 配额续传版: 腾讯K线有IP配额(~1600-1900请求/窗口), 每轮抓到被掐断为止,
等窗口恢复继续, 直到覆盖达标 -> 基准指数 -> 蓄势形态扫描。断点续传, 可反复运行。"""
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

TARGET = 5000                 # 5,553 全A里到达即视为覆盖达标 (新股/停牌拿不到属正常)
WAIT_BLOCKED = 1500           # 被配额掐断后等 25 分钟
WAIT_OK = 90                  # 正常轮之间的小停顿

codes = mkt.universe_codes()  # 只取一次, 避免反复打东财快照
print(f"UNIVERSE {len(codes)}", flush=True)
assert len(codes) > 4000

for rnd in range(1, 40):
    r = ps.backfill(codes=codes)
    cov = ps.coverage()
    print(f"ROUND {rnd} fetched+{r['fetched']} COVERAGE {cov['codes']} codes, "
          f"idx {cov['index_bars']}", flush=True)
    if cov["codes"] >= TARGET and cov["index_bars"] and cov["index_bars"] > 1000:
        break
    if r["fetched"] == 0 or r.get("aborted"):
        print(f"WAIT quota-blocked, sleep {WAIT_BLOCKED}s", flush=True)
        time.sleep(WAIT_BLOCKED)
    else:
        time.sleep(WAIT_OK)
else:
    raise AssertionError(f"40轮仍未达标: {ps.coverage()}")

cov = ps.coverage()
print("COVERAGE FINAL", cov, flush=True)

from ashare.coilscan import build_quality_at, run

qa = build_quality_at(cache=os.path.join(DATA_DIR, "quality_timeline.json"))
res = run(quality_at=qa)
print("M1 SCAN DONE episodes=", res["meta"]["n_episodes"], flush=True)
