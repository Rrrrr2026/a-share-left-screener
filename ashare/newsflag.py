#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""newsflag — 共用核心 leftside_core.newsflag 的本仓库入口 (市场差异见 market.py)。"""
from . import market as _market          # noqa: F401  注入本市场的 Market 适配器
import leftside_core.newsflag as _core

globals().update({k: v for k, v in vars(_core).items() if not k.startswith("__")})
