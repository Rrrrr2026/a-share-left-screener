#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""statutil — 共用核心 leftside_core.statutil 的本仓库入口 (市场差异见 market.py)。"""
from . import market as _market          # noqa: F401  注入本市场的 Market 适配器
import leftside_core.statutil as _core

globals().update({k: v for k, v in vars(_core).items() if not k.startswith("__")})
