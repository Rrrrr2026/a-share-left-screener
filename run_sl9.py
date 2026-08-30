# -*- coding: utf-8 -*-
"""M3: 强左侧策略类九年重放 (优质股回踩MA60支撑 · 出场网格 4/5/10% x 15/20bar)。
依赖: 已建好的 pricestore(9年) + quality_timeline9.json (M1 点时基本面缓存)。"""
import io
import logging
import os
import sys

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "vendor"))

from ashare import market as mkt              # noqa: F401,E402  set_market
from ashare.config import DATA_DIR            # noqa: E402
from ashare.coilscan import build_quality_at  # noqa: E402
from leftside_core import slscan              # noqa: E402

qa = build_quality_at(cache=os.path.join(DATA_DIR, "quality_timeline9.json"))
res = slscan.run(quality_at=qa)
print("SL9 DONE episodes=", res["n_episodes"], flush=True)
print("GRID", res["grid"], flush=True)
