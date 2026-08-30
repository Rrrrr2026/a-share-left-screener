#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
同花顺金融数据 API (fuyao.aicubes.cn) 客户端
===========================================
为什么接它: 本项目原本靠 东财/腾讯/新浪/百度 拼凑, 每家都有坑 ——
  · 东财 push2his(日线历史) 在本机不可达, akshare 还写死了被墙的主机;
  · 腾讯 fqkline 单次只给~640根, 分段拼接会因每段复权基准不同而**断裂**
    (实测茅台 47 处跳变, 据此算出过"历史涨过+2142%"的假数据);
  · akshare 的新浪日线/同花顺板块用 py_mini_racer(V8), 多线程直接把进程打崩;
  · akshare 财务接口经常改字段/报错。
这个 API 一次请求就能给 **10年前复权日线**, 带鉴权、结构稳定、无 V8 依赖,
把上面四类问题一次性解决。

鉴权: API Key 只从环境变量 FUYAO_API_KEY 读, 或本地 data/.fuyao_key 文件
      (已在 .gitignore 内)。**不写进代码/配置**, 避免误提交到公开仓库。
降级: 未配置 key 或接口失败时返回 None, 上层自动回退原有数据源, 不影响运行。
"""
from __future__ import annotations

import datetime as dt
import json
import logging
import os
import time
import urllib.parse
import urllib.request

import pandas as pd

from .config import DATA_DIR

log = logging.getLogger("ashare.fuyao")

BASE = "https://fuyao.aicubes.cn/api"
KEY_FILE = os.path.join(DATA_DIR, ".fuyao_key")
_MAX_YEARS = 9.8            # 接口上限10年, 留余量(超了返回 code=1003)

_key_cache = None
_unavailable = False        # 一轮内确认不可用就不再重试, 避免每只股票都白等


def api_key() -> str:
    global _key_cache
    if _key_cache is not None:
        return _key_cache
    k = os.environ.get("FUYAO_API_KEY", "").strip()
    if not k and os.path.exists(KEY_FILE):
        try:
            with open(KEY_FILE, encoding="utf-8") as f:
                k = f.read().strip()
        except OSError:
            k = ""
    _key_cache = k
    return k


def available() -> bool:
    return bool(api_key()) and not _unavailable


def _get(path: str, params: dict, timeout: int = 30):
    """GET 一个端点。失败返回 None (上层回退), 不抛异常。"""
    global _unavailable
    k = api_key()
    if not k:
        return None
    url = f"{BASE}{path}?{urllib.parse.urlencode(params)}"
    req = urllib.request.Request(url, headers={"X-api-key": k})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            j = json.load(r)
    except Exception as e:
        log.debug("fuyao %s 失败: %s", path, str(e)[:80])
        return None
    if not isinstance(j, dict):
        return None
    code = j.get("code")
    if code == 401 or code == 403:
        _unavailable = True
        log.warning("同花顺API 鉴权失败(code=%s), 本轮改用原有数据源", code)
        return None
    if code != 0:
        log.debug("fuyao %s 返回 code=%s msg=%s", path, code, j.get("message"))
        return None
    return j.get("data") or {}


def to_thscode(code: str) -> str:
    """6位代码 -> 带交易所后缀。北交所(8/4/920)用 .BJ。"""
    c = str(code).zfill(6)
    if c.startswith("920") or c.startswith(("8", "4")):
        return c + ".BJ"
    if c.startswith(("6", "9")):
        return c + ".SH"
    return c + ".SZ"


# --------------------------------------------------------------------------- #
#  日线历史 (核心收益: 一次拿到10年前复权, 无需分段拼接)
# --------------------------------------------------------------------------- #
def hist(code: str, years: float = 9.8, adjust: str = "forward") -> pd.DataFrame | None:
    """返回 date/open/high/low/close/volume(+amount)。失败返回 None。"""
    years = min(float(years), _MAX_YEARS)
    end = int(time.time() * 1000)
    start = end - int(years * 365 * 86400 * 1000)
    d = _get("/a-share/prices/historical",
             {"thscode": to_thscode(code), "interval": "1d",
              "start": start, "end": end, "adjust": adjust})
    if not d:
        return None
    items = d.get("item") or []
    if len(items) < 60:
        return None
    df = pd.DataFrame(items)
    need = {"date_ms", "open_price", "high_price", "low_price", "close_price"}
    if not need.issubset(df.columns):
        return None
    out = pd.DataFrame({
        # date_ms 是北京时间零点的epoch毫秒 — 必须按东八区转日期, 裸UTC会整体早一天
        # (2026-08-31 事故: 九年价格库全库日期-1天, 详见 stock-core CHRONICLE R0.5)
        "date": pd.to_datetime(df["date_ms"], unit="ms", utc=True)
                  .dt.tz_convert("Asia/Shanghai").dt.strftime("%Y-%m-%d"),
        "open": pd.to_numeric(df["open_price"], errors="coerce"),
        "high": pd.to_numeric(df["high_price"], errors="coerce"),
        "low": pd.to_numeric(df["low_price"], errors="coerce"),
        "close": pd.to_numeric(df["close_price"], errors="coerce"),
    })
    if "volume" in df.columns:
        out["volume"] = pd.to_numeric(df["volume"], errors="coerce")
    if "turnover" in df.columns:
        out["amount"] = pd.to_numeric(df["turnover"], errors="coerce")
    out = out.dropna(subset=["date", "close"]).sort_values("date").reset_index(drop=True)
    return out if len(out) >= 60 else None


# --------------------------------------------------------------------------- #
#  估值 (批量当前值; 该 API 无历史序列, PE分位仍走原数据源)
# --------------------------------------------------------------------------- #
def valuations(codes: list) -> dict:
    """批量取 PE-TTM/PB 等。返回 {6位代码: {...}}。"""
    out = {}
    codes = list(codes)
    for i in range(0, len(codes), 80):          # 分批, 避免URL过长
        batch = codes[i:i + 80]
        d = _get("/a-share/valuations/snapshot",
                 {"thscodes": ",".join(to_thscode(c) for c in batch)})
        for it in (d or {}).get("item") or []:
            t = str(it.get("ticker") or "").zfill(6)
            if t:
                out[t] = it
    return out


# --------------------------------------------------------------------------- #
#  财务报表 (替代经常改字段/报错的 akshare 财务接口)
# --------------------------------------------------------------------------- #
def income_statements(code: str, period: str = "quarterly") -> pd.DataFrame | None:
    d = _get("/a-share/financials/income-statements",
             {"thscode": to_thscode(code), "period": period})
    items = (d or {}).get("item") or []
    if not items:
        return None
    df = pd.DataFrame(items)
    if "period_end_ms" in df.columns:
        df["date"] = (pd.to_datetime(df["period_end_ms"], unit="ms", utc=True)
                      .dt.tz_convert("Asia/Shanghai").dt.tz_localize(None))
        df = df.sort_values("date").reset_index(drop=True)
    return df
