#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""pricestore — 共用核心 leftside_core.pricestore 的本仓库入口。"""
from . import market as _market          # noqa: F401
import leftside_core.pricestore as _core

globals().update({k: v for k, v in vars(_core).items() if not k.startswith("__")})


if __name__ == "__main__":
    import logging
    import sys
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    cmd = sys.argv[1] if len(sys.argv) > 1 else "backfill"
    print(backfill() if cmd == "backfill" else update_daily())
    print(coverage())
