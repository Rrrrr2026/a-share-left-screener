# -*- coding: utf-8 -*-
"""M3.5: 快弹信号九年验证 (A股)。"""
import io, logging, os, sys
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "vendor"))
from ashare import market as mkt              # noqa: F401,E402
from leftside_core import fastscan            # noqa: E402
res = fastscan.run()
print("FAST9 DONE", res["pool"], flush=True)
