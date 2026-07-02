#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
数据源访问层 (Data access layer)
================================
封装 akshare 接口, 统一做:
  * 字段映射 (mapping layer): 接口列名是中文且偶有改名, 这里用"候选名匹配"归一为
    稳定的英文列名, 单个字段改名不会让流水线崩溃 (PRD §2 要求)。
  * 限频 + 重试 + 超时: 每次调用之间 sleep, 失败指数退避重试。
  * 本地缓存: 同一交易日内重复运行直接读缓存, 避免重复打接口。
  * 失败只跳过并记录, 绝不打断整轮 (PRD §1)。

每个 fetch_* 函数返回"规范化"后的 DataFrame (英文列名) 或 None。
"""

from __future__ import annotations
import os
import time
import pickle
import hashlib
import threading
import datetime as dt
import logging

import numpy as np
import pandas as pd

from .config import CONFIG, DATA_DIR

log = logging.getLogger("ashare.datasource")

_CACHE_DIR = CONFIG["source"]["cache_dir"]
os.makedirs(_CACHE_DIR, exist_ok=True)


# ===========================================================================
#  给所有 requests.Session 注入浏览器 UA
#  (akshare 默认不带 UA, 部分东财端点会因此重置连接 RemoteDisconnected)
# ===========================================================================
_BROWSER_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
               "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")


def _install_ua_patch():
    try:
        import requests
        orig = requests.sessions.Session.__init__

        if getattr(requests.sessions.Session, "_ashare_ua_patched", False):
            return

        def patched(self, *a, **k):
            orig(self, *a, **k)
            try:
                self.headers.update({"User-Agent": _BROWSER_UA})
            except Exception:
                pass

        requests.sessions.Session.__init__ = patched
        requests.sessions.Session._ashare_ua_patched = True
    except Exception as e:  # 没装 requests 也不影响离线测试
        log.debug("UA patch skipped: %s", e)


_install_ua_patch()


# 东财 push2 实时端点一旦被重置, 置位此标志, 后续实时类请求直接走备用源(新浪/同花顺),
# 避免对每个行业都重试东财而拖慢整轮。多线程下用锁保证只翻转一次、只告警一次。
_em_realtime_down = False
_em_hist_down = False
_flag_lock = threading.Lock()


def _mark_em_down(reason: str = ""):
    global _em_realtime_down
    with _flag_lock:
        if _em_realtime_down:
            return
        _em_realtime_down = True
    log.warning("东财实时端点疑似不可用, 后续改用备用源(新浪/同花顺)。原因: %s", str(reason)[:80])


def _mark_em_hist_down(reason: str = ""):
    global _em_hist_down
    with _flag_lock:
        if _em_hist_down:
            return
        _em_hist_down = True
    log.warning("东财日线端点疑似被限频, 后续个股日线改用新浪。原因: %s", str(reason)[:80])


def _is_conn_error(e) -> bool:
    s = type(e).__name__ + " " + str(e)
    return any(k in s for k in ("ConnectionError", "RemoteDisconnected",
                                "ConnectionReset", "ConnectTimeout", "ReadTimeout"))


# ===========================================================================
#  akshare 延迟导入 (离线测试时不强依赖)
# ===========================================================================
def _ak():
    import akshare as ak
    return ak


# ===========================================================================
#  缓存
# ===========================================================================
def _cache_key(name: str, *args) -> str:
    raw = name + "|" + "|".join(str(a) for a in args)
    h = hashlib.md5(raw.encode("utf-8")).hexdigest()[:16]
    return f"{name}_{h}"


def _cache_load(key: str):
    if not CONFIG["source"]["use_cache"]:
        return None
    path = os.path.join(_CACHE_DIR, key + ".pkl")
    if not os.path.exists(path):
        return None
    age_h = (time.time() - os.path.getmtime(path)) / 3600.0
    if age_h > CONFIG["source"]["cache_ttl_hours"]:
        return None
    try:
        with open(path, "rb") as f:
            return pickle.load(f)
    except Exception:
        return None


def _cache_save(key: str, obj) -> None:
    if not CONFIG["source"]["use_cache"]:
        return
    path = os.path.join(_CACHE_DIR, key + ".pkl")
    # 先写临时文件再原子改名, 避免并发写同一键 / Ctrl-C 中断产生半截损坏的 .pkl
    tmp = f"{path}.{os.getpid()}.{threading.get_ident()}.tmp"
    try:
        with open(tmp, "wb") as f:
            pickle.dump(obj, f)
        os.replace(tmp, path)
    except Exception as e:  # 缓存失败不影响主流程
        log.debug("cache save failed %s: %s", key, e)
        try:
            if os.path.exists(tmp):
                os.remove(tmp)
        except Exception:
            pass


# ===========================================================================
#  通用: 带重试的调用 + 字段映射
# ===========================================================================
def call_with_retry(fn, *args, **kwargs):
    """对一个 akshare 调用做 限频sleep + 重试 + 超时容错。失败抛出最后一次异常。"""
    f = CONFIG["fetch"]
    last_exc = None
    for attempt in range(f["max_retries"]):
        try:
            time.sleep(f["sleep_sec"])
            return fn(*args, **kwargs)
        except Exception as e:  # noqa
            last_exc = e
            wait = f["retry_backoff_sec"] * (2 ** attempt)
            log.debug("retry %d/%d after error: %s (sleep %.1fs)",
                      attempt + 1, f["max_retries"], e, wait)
            time.sleep(wait)
    raise last_exc


def pick_col(df: pd.DataFrame, candidates, contains: bool = False):
    """在 df 中找到第一个匹配的列名。candidates 为候选中文/英文名列表。
    contains=True 时做子串匹配。找不到返回 None。"""
    cols = list(df.columns)
    # 1) 精确匹配
    for cand in candidates:
        if cand in cols:
            return cand
    # 2) 子串匹配
    if contains:
        for cand in candidates:
            for col in cols:
                if cand in str(col):
                    return col
    return None


def rename_normalize(df: pd.DataFrame, mapping: dict) -> pd.DataFrame:
    """mapping: {规范英文名: [候选原列名, ...]} -> 返回只含命中列、且已改名的副本。
    缺失字段不报错 (后续以 NaN/— 降级)。"""
    out = {}
    for std_name, cands in mapping.items():
        col = pick_col(df, cands, contains=True)
        if col is not None:
            out[std_name] = df[col]
    res = pd.DataFrame(out)
    return res


def _to_num(s):
    return pd.to_numeric(s, errors="coerce")


# ===========================================================================
#  1) 全A实时快照 (universe + 估值/换手/量比)  ——  stock_zh_a_spot_em
# ===========================================================================
def fetch_spot_snapshot(force: bool = False) -> pd.DataFrame | None:
    """全A快照 (股票池 + 估值/换手/量比)。优先东财, 失败退回新浪
    (东财 push2 实时端点在部分网络会被重置)。"""
    key = _cache_key("spot", dt.date.today().isoformat())
    if not force:
        c = _cache_load(key)
        if c is not None:
            return c
    df = _spot_from_em()
    if df is None or df.empty:
        log.info("东财快照不可用, 尝试新浪快照 ...")
        df = _spot_from_sina()
    if df is None or df.empty:
        return None
    df["code"] = df["code"].astype(str).str.zfill(6)
    _cache_save(key, df)
    return df


def _spot_from_em() -> pd.DataFrame | None:
    if _em_realtime_down:
        return None
    try:
        raw = call_with_retry(_ak().stock_zh_a_spot_em)
    except Exception as e:
        log.warning("东财快照 stock_zh_a_spot_em 失败: %s", e)
        if _is_conn_error(e):
            _mark_em_down(e)
        return None
    df = rename_normalize(raw, {
        "code":         ["代码"],
        "name":         ["名称"],
        "price":        ["最新价"],
        "pct_chg":      ["涨跌幅"],
        "volume":       ["成交量"],
        "amount":       ["成交额"],
        "high":         ["最高"],
        "low":          ["最低"],
        "volume_ratio": ["量比"],
        "turnover":     ["换手率"],
        "pe_ttm":       ["市盈率-动态", "市盈率"],
        "pb":           ["市净率"],
        "total_mv":     ["总市值"],
        "float_mv":     ["流通市值"],
    })
    if "code" not in df.columns:
        return None
    for col in df.columns:
        if col not in ("code", "name"):
            df[col] = _to_num(df[col])
    return df


def _spot_from_sina() -> pd.DataFrame | None:
    """新浪全A快照 (较慢但端点稳定)。无量比/PE等字段时优雅缺省。"""
    try:
        raw = call_with_retry(_ak().stock_zh_a_spot)
    except Exception as e:
        log.warning("新浪快照 stock_zh_a_spot 失败: %s", e)
        return None
    df = rename_normalize(raw, {
        "code":     ["代码"],
        "name":     ["名称"],
        "price":    ["最新价"],
        "pct_chg":  ["涨跌幅"],
        "volume":   ["成交量"],
        "amount":   ["成交额"],
        "high":     ["最高"],
        "low":      ["最低"],
        "turnover": ["换手率"],
        "pe_ttm":   ["市盈率"],
        "pb":       ["市净率"],
    })
    if "code" not in df.columns:
        return None
    # 新浪代码常带 sh/sz/bj 前缀
    df["code"] = df["code"].astype(str).str.replace(r"^(sh|sz|bj)", "", regex=True)
    for col in df.columns:
        if col not in ("code", "name"):
            df[col] = _to_num(df[col])
    return df


def build_universe(spot: pd.DataFrame | None = None) -> pd.DataFrame | None:
    """从快照构造股票池 (剔除ST / 北交所), 返回 code,name。"""
    if spot is None:
        spot = fetch_spot_snapshot()
    if spot is None or spot.empty:
        return None
    df = spot[["code", "name"]].copy()
    t = CONFIG["tech"]
    if t["exclude_st"]:
        df = df[~df["name"].astype(str).str.contains("ST", case=False, na=False)]
    if t["exclude_bj"]:
        # 北交所: 8xx / 4xx, 以及 2024 年新增的 920xxx 段
        df = df[~df["code"].str.startswith(("8", "4", "920"))]
    df = df.drop_duplicates(subset=["code"])   # 去重, 避免重复代码导致命中数虚高
    return df.reset_index(drop=True)


# ===========================================================================
#  2) 个股日线 (前复权)  ——  stock_zh_a_hist
# ===========================================================================
def _sina_symbol(code: str) -> str:
    code = str(code).zfill(6)
    # 北交所(920/8/4) 必须放在 6/9 之前判断: 920 以 '9' 开头, 否则会被误判成沪市
    if code.startswith("920") or code.startswith(("8", "4")):
        return "bj" + code
    if code.startswith(("6", "9")):           # 沪市 60/68, 沪B 900
        return "sh" + code
    if code.startswith(("0", "3", "2")):      # 深市 00/30, 深B 200
        return "sz" + code
    return "sh" + code


def _finalize_hist(df: pd.DataFrame) -> pd.DataFrame | None:
    need = {"date", "open", "high", "low", "close"}
    if df is None or not need.issubset(df.columns):
        return None
    for col in ("open", "high", "low", "close", "volume", "amount", "pct_chg"):
        if col in df.columns:
            df[col] = _to_num(df[col])
    df["date"] = pd.to_datetime(df["date"], errors="coerce").dt.strftime("%Y-%m-%d")
    return df.dropna(subset=["date"]).sort_values("date").reset_index(drop=True)


def fetch_hist(code: str) -> pd.DataFrame | None:
    """个股日线(前复权)。优先东财, 被限/失败时退回新浪
    (东财被限频时新浪 stock_zh_a_daily 仍可用, 保证扫描不空跑)。"""
    f = CONFIG["fetch"]
    # 缓存键含 lookback_days: 改了回看天数(bar数)会自动失效旧缓存, 避免用到过短的历史
    key = _cache_key("hist", code, f["adjust"], f["lookback_days"], dt.date.today().isoformat())
    c = _cache_load(key)
    if c is not None:
        return c
    df = None
    if not _em_hist_down:
        df = _hist_from_em(code)
    if df is None or df.empty:
        df = _hist_from_sina(code)
    if df is None or df.empty:
        return None
    _cache_save(key, df)
    return df


def _hist_from_em(code: str) -> pd.DataFrame | None:
    f = CONFIG["fetch"]
    end = dt.date.today()
    start = end - dt.timedelta(days=f["lookback_days"])
    try:
        raw = call_with_retry(
            _ak().stock_zh_a_hist,
            symbol=code, period="daily",
            start_date=start.strftime("%Y%m%d"),
            end_date=end.strftime("%Y%m%d"),
            adjust=f["adjust"],
        )
    except Exception as e:
        log.debug("东财日线 %s 失败: %s", code, e)
        if _is_conn_error(e):
            _mark_em_hist_down(e)
        return None
    if raw is None or len(raw) == 0:
        return None
    return _finalize_hist(rename_normalize(raw, {
        "date": ["日期"], "open": ["开盘"], "high": ["最高"], "low": ["最低"],
        "close": ["收盘"], "volume": ["成交量"], "amount": ["成交额"], "pct_chg": ["涨跌幅"],
    }))


def _hist_from_sina(code: str) -> pd.DataFrame | None:
    f = CONFIG["fetch"]
    end = dt.date.today()
    start = end - dt.timedelta(days=f["lookback_days"])
    try:
        raw = call_with_retry(
            _ak().stock_zh_a_daily,
            symbol=_sina_symbol(code), adjust=f["adjust"],
            start_date=start.strftime("%Y%m%d"), end_date=end.strftime("%Y%m%d"),
        )
    except Exception as e:
        log.debug("新浪日线 %s 失败: %s", code, e)
        return None
    if raw is None or len(raw) == 0:
        return None
    return _finalize_hist(rename_normalize(raw, {
        "date": ["date", "日期"], "open": ["open", "开盘"], "high": ["high", "最高"],
        "low": ["low", "最低"], "close": ["close", "收盘"],
        "volume": ["volume", "成交量"], "amount": ["amount", "成交额"],
    }))


# ===========================================================================
#  3) 行业列表 / 成分 / 指数历史  ——  stock_board_industry_*_em (东财一级)
# ===========================================================================
def fetch_industry_list() -> pd.DataFrame | None:
    """行业列表。优先东财, 失败退回同花顺。"""
    key = _cache_key("ind_list", dt.date.today().isoformat())
    c = _cache_load(key)
    if c is not None:
        return c
    df = _industry_list_em()
    if df is None or df.empty:
        log.info("东财行业列表不可用, 尝试同花顺 ...")
        df = _industry_list_ths()
    if df is None or df.empty:
        return None
    df = df.drop_duplicates(subset=["industry"]).reset_index(drop=True)
    _cache_save(key, df)
    return df


def _industry_list_em() -> pd.DataFrame | None:
    if _em_realtime_down:
        return None
    try:
        raw = call_with_retry(_ak().stock_board_industry_name_em)
    except Exception as e:
        log.warning("东财行业列表失败: %s", e)
        if _is_conn_error(e):
            _mark_em_down(e)
        return None
    df = rename_normalize(raw, {
        "industry":  ["板块名称", "行业名称", "名称"],
        "board_code": ["板块代码", "代码"],
        "pct_chg":   ["涨跌幅"],
    })
    df.attrs["source"] = "em"
    return df if "industry" in df.columns else None


def _industry_list_ths() -> pd.DataFrame | None:
    try:
        raw = call_with_retry(_ak().stock_board_industry_name_ths)
    except Exception as e:
        log.warning("同花顺行业列表失败: %s", e)
        return None
    df = rename_normalize(raw, {
        "industry":  ["name", "板块名称", "名称"],
        "board_code": ["code", "板块代码", "代码"],
    })
    df.attrs["source"] = "ths"
    return df if "industry" in df.columns else None


def fetch_industry_cons(industry: str) -> pd.DataFrame | None:
    key = _cache_key("ind_cons", industry, dt.date.today().isoformat())
    c = _cache_load(key)
    if c is not None:
        return c
    if _em_realtime_down:    # 东财成分股(push2)无备用源, 直接放弃 -> 上层回退全市场
        return None
    try:
        raw = call_with_retry(_ak().stock_board_industry_cons_em, symbol=industry)
    except Exception as e:
        log.debug("fetch_industry_cons %s failed: %s", industry, e)
        if _is_conn_error(e):
            _mark_em_down(e)
        return None
    df = rename_normalize(raw, {
        "code": ["代码"],
        "name": ["名称"],
    })
    if "code" not in df.columns:
        return None
    df["code"] = df["code"].astype(str).str.zfill(6)
    _cache_save(key, df)
    return df


def fetch_industry_hist(industry: str) -> pd.DataFrame | None:
    """行业指数日线。优先东财, 失败退回同花顺行业指数。"""
    key = _cache_key("ind_hist", industry, CONFIG["fetch"]["lookback_days"],
                     dt.date.today().isoformat())
    c = _cache_load(key)
    if c is not None:
        return c
    df = _industry_hist_em(industry)
    if df is None or df.empty:
        df = _industry_hist_ths(industry)
    if df is None or df.empty:
        return None
    _cache_save(key, df)
    return df


def _normalize_idx_hist(df: pd.DataFrame) -> pd.DataFrame | None:
    if "close" not in df.columns or "date" not in df.columns:
        return None
    for col in ("open", "high", "low", "close", "amount"):
        if col in df.columns:
            df[col] = _to_num(df[col])
    df["date"] = pd.to_datetime(df["date"], errors="coerce").dt.strftime("%Y-%m-%d")
    return df.dropna(subset=["date"]).sort_values("date").reset_index(drop=True)


def _industry_hist_em(industry: str) -> pd.DataFrame | None:
    if _em_realtime_down:
        return None
    end = dt.date.today()
    start = end - dt.timedelta(days=CONFIG["fetch"]["lookback_days"])
    try:
        raw = call_with_retry(
            _ak().stock_board_industry_hist_em,
            symbol=industry,
            start_date=start.strftime("%Y%m%d"),
            end_date=end.strftime("%Y%m%d"),
            period="日k", adjust="",
        )
    except Exception as e:
        log.debug("东财行业指数 %s 失败: %s", industry, e)
        if _is_conn_error(e):
            _mark_em_down(e)
        return None
    if raw is None or len(raw) == 0:
        return None
    return _normalize_idx_hist(rename_normalize(raw, {
        "date": ["日期"], "open": ["开盘"], "high": ["最高"],
        "low": ["最低"], "close": ["收盘"], "amount": ["成交额"],
    }))


def _industry_hist_ths(industry: str) -> pd.DataFrame | None:
    end = dt.date.today()
    start = end - dt.timedelta(days=CONFIG["fetch"]["lookback_days"])
    try:
        raw = call_with_retry(
            _ak().stock_board_industry_index_ths,
            symbol=industry,
            start_date=start.strftime("%Y%m%d"),
            end_date=end.strftime("%Y%m%d"),
        )
    except Exception as e:
        log.debug("同花顺行业指数 %s 失败: %s", industry, e)
        return None
    if raw is None or len(raw) == 0:
        return None
    return _normalize_idx_hist(rename_normalize(raw, {
        "date": ["日期"], "open": ["开盘价", "开盘"], "high": ["最高价", "最高"],
        "low": ["最低价", "最低"], "close": ["收盘价", "收盘"], "amount": ["成交额"],
    }))


# ===========================================================================
#  4) 基准指数 (沪深300) 历史收盘  ——  stock_zh_index_daily_em
# ===========================================================================
def fetch_benchmark_close() -> pd.DataFrame | None:
    sym = CONFIG["source"]["benchmark_index"]
    key = _cache_key("bench", sym, dt.date.today().isoformat())
    c = _cache_load(key)
    if c is not None:
        return c
    raw = None
    for fn, kw in (
        (lambda: _ak().stock_zh_index_daily_em(symbol=sym), {}),
        (lambda: _ak().stock_zh_index_daily(symbol=sym), {}),
    ):
        try:
            raw = call_with_retry(fn)
            if raw is not None and len(raw):
                break
        except Exception as e:
            log.debug("benchmark fetch attempt failed: %s", e)
            raw = None
    if raw is None or len(raw) == 0:
        return None
    df = rename_normalize(raw, {
        "date":  ["date", "日期"],
        "close": ["close", "收盘"],
    })
    if "close" not in df.columns:
        return None
    df["close"] = _to_num(df["close"])
    df["date"] = pd.to_datetime(df["date"]).dt.strftime("%Y-%m-%d")
    df = df.sort_values("date").reset_index(drop=True)
    _cache_save(key, df)
    return df


# ===========================================================================
#  5) 个股估值历史 (PE/PB 分位)
#     注意: akshare 1.12+ 已移除 stock_a_indicator_lg, 改用东财 stock_value_em
#     (一次返回 PE-TTM/PB/PE静/市销/总市值 的逐日历史); 失败再退回百度股市通。
# ===========================================================================
def fetch_valuation_hist(code: str) -> pd.DataFrame | None:
    key = _cache_key("val", code, dt.date.today().isoformat())
    c = _cache_load(key)
    if c is not None:
        return c
    df = _valuation_from_value_em(code)
    if df is None or df.empty:
        df = _valuation_from_baidu(code)
    if df is None or df.empty:
        return None
    _cache_save(key, df)
    return df


def _valuation_from_value_em(code: str) -> pd.DataFrame | None:
    """东财估值分析: 一次拿到 PE-TTM/PB 等的逐日序列。"""
    try:
        raw = call_with_retry(_ak().stock_value_em, symbol=code)
    except Exception as e:
        log.debug("stock_value_em %s failed: %s", code, e)
        return None
    if raw is None or len(raw) == 0:
        return None
    df = rename_normalize(raw, {
        "date":     ["数据日期", "trade_date", "日期"],
        "pe_ttm":   ["PE(TTM)", "市盈率(TTM)"],
        "pe":       ["PE(静)", "市盈率(静)"],
        "pb":       ["市净率"],
        "ps_ttm":   ["市销率"],
        "total_mv": ["总市值"],
    })
    if "date" not in df.columns:
        return None
    for col in df.columns:
        if col != "date":
            df[col] = _to_num(df[col])
    df["date"] = pd.to_datetime(df["date"], errors="coerce")
    return df.sort_values("date").reset_index(drop=True)


def _valuation_from_baidu(code: str) -> pd.DataFrame | None:
    """百度股市通: 每个指标一次调用, 取近五年, 按日期合并。"""
    out = None
    for indicator, std in (("市盈率(TTM)", "pe_ttm"), ("市净率", "pb")):
        try:
            raw = call_with_retry(_ak().stock_zh_valuation_baidu,
                                  symbol=code, indicator=indicator, period="近五年")
        except Exception as e:
            log.debug("baidu valuation %s %s failed: %s", code, indicator, e)
            continue
        if raw is None or len(raw) == 0:
            continue
        d = rename_normalize(raw, {"date": ["date", "日期"], std: ["value"]})
        if "date" not in d.columns or std not in d.columns:
            continue
        d["date"] = pd.to_datetime(d["date"], errors="coerce")
        d[std] = _to_num(d[std])
        out = d if out is None else out.merge(d, on="date", how="outer")
    if out is None:
        return None
    return out.sort_values("date").reset_index(drop=True)


# ===========================================================================
#  6) 财务指标 (ROE/EPS/增长/负债)  ——  stock_financial_analysis_indicator
# ===========================================================================
def fetch_financial_indicator(code: str) -> pd.DataFrame | None:
    key = _cache_key("fin", code, dt.date.today().isoformat())
    c = _cache_load(key)
    if c is not None:
        return c
    start_year = str(dt.date.today().year - 5)
    raw = None
    for kwargs in ({"symbol": code, "start_year": start_year},
                   {"symbol": code}):
        try:
            raw = call_with_retry(_ak().stock_financial_analysis_indicator, **kwargs)
            if raw is not None and len(raw):
                break
        except Exception as e:
            log.debug("fetch_financial_indicator %s failed (%s): %s", code, kwargs, e)
            raw = None
    if raw is None or len(raw) == 0:
        return None
    df = rename_normalize(raw, {
        "date":         ["日期"],
        "roe":          ["净资产收益率(%)", "净资产收益率"],
        "eps":          ["摊薄每股收益(元)", "加权每股收益(元)", "每股收益"],
        "revenue_yoy":  ["主营业务收入增长率(%)", "营业收入增长率", "主营业务收入增长率"],
        "netprofit_yoy": ["净利润增长率(%)", "净利润增长率"],
        "gross_margin": ["销售毛利率(%)", "销售毛利率"],
        "debt_ratio":   ["资产负债率(%)", "资产负债率"],
    })
    for col in df.columns:
        if col != "date":
            df[col] = _to_num(df[col])
    if "date" in df.columns:
        df["date"] = pd.to_datetime(df["date"], errors="coerce")
        df = df.sort_values("date").reset_index(drop=True)
    _cache_save(key, df)
    return df


# ===========================================================================
#  7) 个股基础信息 (所属行业/上市时间/市值)  ——  stock_individual_info_em
# ===========================================================================
def fetch_basic_info(code: str) -> dict | None:
    key = _cache_key("info", code, dt.date.today().isoformat())
    c = _cache_load(key)
    if c is not None:
        return c
    try:
        raw = call_with_retry(_ak().stock_individual_info_em, symbol=code)
    except Exception as e:
        log.debug("fetch_basic_info %s failed: %s", code, e)
        return None
    if raw is None or len(raw) == 0:
        return None
    # 该接口返回两列: item / value
    try:
        d = dict(zip(raw.iloc[:, 0].astype(str), raw.iloc[:, 1]))
    except Exception:
        return None
    info = {
        "industry": d.get("行业"),
        "name": d.get("股票简称"),
        "list_date": d.get("上市时间"),
        "total_mv": d.get("总市值"),
        "float_mv": d.get("流通市值"),
    }
    _cache_save(key, info)
    return info


# ===========================================================================
#  8) 行业资金流 (主力净流入, 近5日)  ——  stock_sector_fund_flow_rank
#     (可选数据源; 拿不到则上层把"资金"支柱权重并入趋势+动量)
# ===========================================================================
def fetch_industry_fund_flow() -> pd.DataFrame | None:
    """行业资金净流入。优先东财资金流排名, 失败退回同花顺行业摘要(净流入)。"""
    key = _cache_key("ind_flow", dt.date.today().isoformat())
    c = _cache_load(key)
    if c is not None:
        return c
    df = _fund_flow_em()
    if df is None or df.empty:
        df = _fund_flow_ths()
    if df is None or df.empty:
        return None
    _cache_save(key, df)
    return df


def _fund_flow_em() -> pd.DataFrame | None:
    if _em_realtime_down:
        return None
    raw = None
    for kwargs in ({"indicator": "5日", "sector_type": "行业资金流"},
                   {"indicator": "今日", "sector_type": "行业资金流"}):
        try:
            raw = call_with_retry(_ak().stock_sector_fund_flow_rank, **kwargs)
            if raw is not None and len(raw):
                break
        except Exception as e:
            log.debug("东财行业资金流失败 (%s): %s", kwargs, e)
            if _is_conn_error(e):
                _mark_em_down(e)
            raw = None
    if raw is None or len(raw) == 0:
        return None
    df = rename_normalize(raw, {
        "industry":  ["名称", "板块名称"],
        "net_inflow": ["5日主力净流入-净额", "今日主力净流入-净额",
                       "主力净流入-净额", "主力净流入"],
    })
    if "industry" not in df.columns or "net_inflow" not in df.columns:
        return None
    df["net_inflow"] = _to_num(df["net_inflow"])
    return df


def _fund_flow_ths() -> pd.DataFrame | None:
    try:
        raw = call_with_retry(_ak().stock_board_industry_summary_ths)
    except Exception as e:
        log.debug("同花顺行业摘要失败: %s", e)
        return None
    if raw is None or len(raw) == 0:
        return None
    df = rename_normalize(raw, {
        "industry": ["板块", "名称", "板块名称"],
        "net_inflow": ["净流入", "净额", "主力净流入"],
    })
    if "industry" not in df.columns or "net_inflow" not in df.columns:
        return None
    df["net_inflow"] = _to_num(df["net_inflow"])
    return df
