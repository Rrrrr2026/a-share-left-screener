# -*- coding: utf-8 -*-
"""二档最高优先: 出场三参数联合网格九年重放 (A股)。"""
import io, logging, os, sys
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "vendor"))
from ashare import market as mkt              # noqa: F401,E402
from leftside_core import exitgrid            # noqa: E402
res = exitgrid.run()
print("EXITGRID DONE dip=", res["dip"]["n_episodes"], "pb=", res["pb"]["n_episodes"], flush=True)
