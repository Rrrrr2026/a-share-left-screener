#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""biweekly — 共用核心 leftside_core.biweekly 的本仓库入口。"""
from . import market as _market          # noqa: F401
import leftside_core.biweekly as _core

globals().update({k: v for k, v in vars(_core).items() if not k.startswith("__")})


if __name__ == "__main__":
    import logging
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    update()
