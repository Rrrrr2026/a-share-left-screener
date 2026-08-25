# -*- coding: utf-8 -*-
"""M1 morning runner: 夜间截断修复(全量重抓) -> 价格库回填 -> 蓄势形态扫描。"""
import io
import logging
import os
import sqlite3
import sys

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

from ashare import pricestore as ps
from ashare.config import DATA_DIR

db = os.path.join(DATA_DIR, "pricestore.db")
conn = sqlite3.connect(db)
conn.execute("DELETE FROM bars")          # 夜间抓的可能被腾讯静默截断, 全部重抓
conn.execute("DELETE FROM idx_bars")
conn.commit()
conn.close()
print("STORE RESET", flush=True)

r = ps.backfill()
print("BACKFILL", r, flush=True)
cov = ps.coverage()
print("COVERAGE", cov, flush=True)
assert cov["codes"] > 4500, f"覆盖不足, 数据源仍不健康: {cov}"
assert cov["index_bars"] > 1000, f"基准指数缺失: {cov}"

from ashare.coilscan import build_quality_at, run

qa = build_quality_at(cache=os.path.join(DATA_DIR, "quality_timeline.json"))
res = run(quality_at=qa)
print("M1 SCAN DONE episodes=", res["meta"]["n_episodes"], flush=True)
