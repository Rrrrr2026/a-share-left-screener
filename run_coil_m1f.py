# -*- coding: utf-8 -*-
"""M1 最终版: 全库单源重建 (fuyao, 9年, 成交量单位统一为股) -> 9年基本面时间线
-> 蓄势形态扫描。修复腾讯(手)/fuyao(股) 成交量混单位导致的流动性门失真。"""
import io
import logging
import os
import sqlite3
import sys

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

from ashare import market as mkt
from ashare import pricestore as ps
from ashare.config import DATA_DIR

mkt.MARKET.fetch_bars_bulk = mkt.fetch_bars_bulk_fuyao
mkt.MARKET.fetch_index_bars = mkt.fetch_index_bars_em    # 东财挂则新浪

conn = sqlite3.connect(os.path.join(DATA_DIR, "pricestore.db"), timeout=60)
conn.execute("DELETE FROM bars")
conn.execute("DELETE FROM idx_bars")
conn.commit()
conn.close()
print("STORE RESET (single-source rebuild)", flush=True)

codes = mkt.universe_codes()
print(f"UNIVERSE {len(codes)}", flush=True)
assert len(codes) > 4000

r = ps.backfill(codes=codes, years=9)
cov = ps.coverage()
print("COVERAGE", cov, flush=True)
assert cov["codes"] >= 4800, f"覆盖不足: {cov}"
assert cov["index_bars"] and cov["index_bars"] > 1800, f"指数长度不足: {cov}"

# 成交量单位自检: 茅台日成交量应为百万股级 (股), 而非万级 (手)
ser = ps.load(["600519"]).get("600519")
assert ser is not None and ser["ohlcv"][-1][4] > 3e5, f"量纲异常: {ser['ohlcv'][-1]}"
print("VOLUME UNIT OK", flush=True)

from ashare.coilscan import build_quality_at, run

qa = build_quality_at(cache=os.path.join(DATA_DIR, "quality_timeline9.json"))
res = run(quality_at=qa)
print("M1 SCAN DONE episodes=", res["meta"]["n_episodes"], flush=True)
