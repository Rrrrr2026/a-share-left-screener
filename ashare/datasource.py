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
    # 顺序: 东财直连(多主机, 最稳) -> akshare东财 -> 新浪。
    # 直连放第一位是因为 akshare 写死的主机在本机被挡, 而其余东财主机可达。
    df = _spot_from_em_direct()
    if df is None or df.empty:
        log.info("东财直连快照不可用, 尝试 akshare 东财 ...")
        df = _spot_from_em()
    if df is None or df.empty:
        log.info("东财快照不可用, 尝试新浪快照 ...")
        df = _spot_from_sina()
    if df is None or df.empty:
        log.error("全部快照源均失败 -> 股票池为空, 本轮无法扫描。"
                  "请检查网络/代理是否放行 push2.eastmoney.com")
        return None
    df["code"] = df["code"].astype(str).str.zfill(6)
    _cache_save(key, df)
    return df


# 东财行情主机池。akshare 写死用 17.push2, 而本机代理恰好挡住这一台 ——
# 其余主机(push2 / 1.push2 / push2delay / 82.push2)实测均可达。
# 2026-07-27 事故: akshare 东财快照失败 + 新浪快照 akshare 解析报错(list index out of
# range) -> 股票池为空 -> 扫描数 0。故自己直连 + 多主机故障转移, 不再受制于 akshare。
_EM_HOSTS = ("push2.eastmoney.com", "1.push2.eastmoney.com",
             "push2delay.eastmoney.com", "82.push2.eastmoney.com",
             "2.push2.eastmoney.com")
_em_host_ok = None          # 记住第一个成功的主机, 后续优先用它
_em_host_fails = {}         # 主机 -> 连续失败次数; 连挂多次就本轮拉黑, 不再浪费超时


def _em_get(path: str, params: dict, timeout: int = 8):
    """对东财接口做多主机轮询 + JSON 容错。全部失败返回 None。

    分页要打几十次, 若每次都从头撞坏主机, 光超时就拖垮整轮(实测 298s):
    故 ① 记住上次成功的主机并排最前; ② 连续失败 3 次的主机本轮拉黑跳过。
    """
    import requests
    global _em_host_ok
    live = [h for h in _EM_HOSTS if _em_host_fails.get(h, 0) < 3]
    if not live:                                   # 全被拉黑 -> 清零重来(网络可能恢复了)
        _em_host_fails.clear()
        live = list(_EM_HOSTS)
    if _em_host_ok in live:                        # 上次成功的主机排最前
        live.remove(_em_host_ok); live.insert(0, _em_host_ok)
    headers = {"User-Agent": _BROWSER_UA, "Referer": "https://quote.eastmoney.com/"}
    for h in live:
        try:
            r = requests.get(f"https://{h}{path}", params=params,
                             headers=headers, timeout=timeout)
            if r.status_code != 200 or not r.text.strip().startswith("{"):
                raise ValueError(f"bad body {r.status_code}")
            j = r.json()
            if not isinstance(j, dict) or j.get("data") is None:
                raise ValueError("no data")
            _em_host_ok = h
            _em_host_fails[h] = 0
            return j["data"]
        except Exception as e:
            _em_host_fails[h] = _em_host_fails.get(h, 0) + 1
            if _em_host_fails[h] == 3:
                log.info("东财主机 %s 连续失败3次, 本轮跳过", h)
            log.debug("东财主机 %s 失败: %s", h, str(e)[:60])
            continue
    return None


def _spot_from_em_direct() -> pd.DataFrame | None:
    """全A快照: 直连东财 clist 分页拉取(不经 akshare), 带主机故障转移。
    一次拿到 代码/名称/价格/量额/换手/量比/PE/PB/市值, 即股票池 + 估值字段。"""
    fields = "f12,f14,f2,f3,f5,f6,f8,f9,f10,f15,f16,f20,f21,f23,f100"  # f100=所属行业
    fs = "m:0 t:6,m:0 t:80,m:1 t:2,m:1 t:23,m:0 t:81 s:2048"   # 沪深主板/创业/科创/北交
    rows, pn, pz = [], 1, 200
    total = None
    while True:
        # 单页重试: 中途一次抖动就 break 会**静默截断股票池**(漏掉的股票整轮扫不到),
        # 这比慢几秒严重得多 -> 每页最多重试3次(轮换主机), 仍失败才放弃并明确告警。
        d = None
        for _try in range(3):
            d = _em_get("/api/qt/clist/get",
                        {"pn": pn, "pz": pz, "po": 1, "np": 1, "fltt": 2, "invt": 2,
                         "fid": "f3", "fs": fs, "fields": fields})
            if d:
                break
            time.sleep(0.6 * (_try + 1))
        if not d:
            log.warning("东财快照第 %d 页三次重试仍失败, 股票池可能不完整", pn)
            break
        if total is None:
            total = d.get("total") or 0
        diff = d.get("diff") or []
        if isinstance(diff, dict):          # 个别返回是 {"0":{...}} 形式
            diff = list(diff.values())
        if not diff:
            break
        rows.extend(diff)
        if total and len(rows) >= total:
            break
        pn += 1
        if pn > 60:                          # 安全上限 (60*200=12000 只)
            break
    if not rows:
        return None
    if total and len(rows) < total * 0.95:
        log.warning("东财快照只取到 %d/%d 只(缺 %.0f%%), 本轮扫描范围偏小",
                    len(rows), total, (1 - len(rows) / total) * 100)
    df = pd.DataFrame(rows).rename(columns={
        "f12": "code", "f14": "name", "f2": "price", "f3": "pct_chg",
        "f5": "volume", "f6": "amount", "f8": "turnover", "f9": "pe_ttm",
        "f10": "volume_ratio", "f15": "high", "f16": "low",
        "f20": "total_mv", "f21": "float_mv", "f23": "pb", "f100": "industry"})
    keep = [c for c in ("code", "name", "price", "pct_chg", "volume", "amount",
                        "turnover", "pe_ttm", "volume_ratio", "high", "low",
                        "total_mv", "float_mv", "pb", "industry") if c in df.columns]
    df = df[keep].copy()
    for col in df.columns:
        if col not in ("code", "name", "industry"):
            df[col] = _to_num(df[col])       # 东财用 "-" 表示缺失 -> NaN
    df["code"] = df["code"].astype(str).str.zfill(6)
    if "industry" in df.columns:              # "-" / "" 视为无行业
        df["industry"] = df["industry"].astype(str).str.strip()
        df.loc[df["industry"].isin(["-", "", "nan", "None"]), "industry"] = None
    log.info("东财快照(直连 %s): %d 只", _em_host_ok, len(df))
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
#  2b) 长历史日线 (盈利指引回测用, ~10年前复权)
#      东财 push2his / 新浪 / 网易 在本机均不可达 -> 用腾讯 fqkline。
#      腾讯单次最多返回 ~640 根, 但支持按日期区间查询 -> 分段翻页拼接。
# ===========================================================================
_TENCENT_KLINE = "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get"


def _tencent_symbol(code: str) -> str:
    code = str(code).zfill(6)
    if code.startswith("920") or code.startswith(("8", "4")):
        return "bj" + code
    if code.startswith(("6", "9")):
        return "sh" + code
    return "sz" + code


def _tencent_chunk(sym: str, start: str, end: str) -> list:
    """取一段日线(前复权)。返回 [[date,open,close,high,low,volume], ...]。"""
    import requests
    p = {"param": f"{sym},day,{start},{end},640,qfq"}
    r = requests.get(_TENCENT_KLINE, params=p,
                     headers={"User-Agent": _BROWSER_UA}, timeout=25)
    j = r.json()
    if j.get("msg") == "param error":
        return []
    d = j.get("data") or {}
    if not isinstance(d, dict):
        return []
    sub = d.get(sym) or {}
    if not isinstance(sub, dict):
        return []
    return sub.get("qfqday") or sub.get("day") or []


def _long_hist_is_sane(df: pd.DataFrame, code: str = "") -> bool:
    """长历史数据体检。回测最怕"脏数据算出漂亮结论"(实测某些票会返回错乱行:
    单日 high/low 振幅 60%+、隔日跳变 40%+, 据此算出的 '历史涨过2142%' 是假的)。
    任何一项超阈值直接判脏 -> 上层拒绝出统计, 而不是给出可信度不明的数字。"""
    need = {"open", "high", "low", "close"}
    if df is None or len(df) < 250 or not need.issubset(df.columns):
        return False
    o, h, l, c = (df[k].astype(float) for k in ("open", "high", "low", "close"))
    n = len(df)
    bad = 0
    bad += int((h < l).sum())                                   # 最高<最低
    bad += int((h < o - 1e-9).sum()) + int((h < c - 1e-9).sum())  # 最高不封顶
    bad += int((l > o + 1e-9).sum()) + int((l > c + 1e-9).sum())  # 最低不兜底
    if bad > n * 0.01:
        log.debug("长历史 %s 脏: OHLC 关系违规 %d/%d", code, bad, n)
        return False
    # 日内振幅: A股有涨跌停, 正常 <=22%; 超 35% 的行视为错乱
    rng = ((h - l) / c.replace(0, np.nan)).abs()
    if int((rng > 0.35).sum()) > n * 0.01:
        log.debug("长历史 %s 脏: 日内振幅异常 %d/%d", code, int((rng > 0.35).sum()), n)
        return False
    # 隔日跳变: 除权/停牌复牌会有大跳, 但占比应极低; 超 25% 的跳变过多 = 拼接错位
    jump = (c / c.shift(1) - 1).abs()
    if int((jump > 0.25).sum()) > max(3, n * 0.005):
        log.debug("长历史 %s 脏: 隔日跳变异常 %d/%d", code, int((jump > 0.25).sum()), n)
        return False
    return True


def fetch_long_hist(code: str, years: int = 10) -> pd.DataFrame | None:
    """~N年前复权日线 (date/open/high/low/close/volume)。分段翻页 + 缓存。
    盈利指引的回测样本靠它; 常规扫描仍用 fetch_hist(500天) 不受影响。"""
    key = _cache_key("longhist", code, years, dt.date.today().isoformat())
    c = _cache_load(key)
    if isinstance(c, str) and c == "BAD":     # 当轮已判定脏, 不再重拉
        return None
    if c is not None:
        return c
    sym = _tencent_symbol(code)
    today = dt.date.today()
    rows, seen = [], set()
    # 每段约 2.5 年(<640根), 从最早往今天翻
    seg = 900                                  # 天/段
    cur = today - dt.timedelta(days=365 * years)
    while cur < today:
        nxt = min(cur + dt.timedelta(days=seg), today)
        try:
            kl = call_with_retry(_tencent_chunk, sym,
                                 cur.isoformat(), nxt.isoformat())
        except Exception as e:
            log.debug("腾讯长历史 %s [%s~%s] 失败: %s", code, cur, nxt, e)
            kl = []
        for k in kl:
            if not k or len(k) < 5:
                continue
            d0 = str(k[0])
            if d0 in seen:
                continue
            seen.add(d0)
            rows.append(k[:6])
        cur = nxt + dt.timedelta(days=1)
    if len(rows) < 250:                        # 不足1年 -> 视为拿不到
        return None
    df = pd.DataFrame(rows, columns=["date", "open", "close", "high", "low", "volume"][:len(rows[0])])
    for col in ("open", "close", "high", "low", "volume"):
        if col in df.columns:
            df[col] = _to_num(df[col])
    df["date"] = pd.to_datetime(df["date"], errors="coerce").dt.strftime("%Y-%m-%d")
    df = df.dropna(subset=["date", "close"]).sort_values("date").reset_index(drop=True)
    if not _long_hist_is_sane(df, code):
        log.info("长历史 %s 未通过数据体检, 放弃(该股不出盈利指引)", code)
        _cache_save(key, "BAD")          # 缓存坏结果, 当轮不反复重拉
        return None
    _cache_save(key, df)
    return df


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


def _industry_list_em_direct() -> pd.DataFrame | None:
    """东财一级行业板块列表 —— 直连 clist(fs=m:90 t:2), 走主机池。
    拿到板块代码后, 成分股可用 fs=b:<board_code> 取到 -> 恢复'按行业扫描'的正常路径。"""
    d = _em_get("/api/qt/clist/get",
                {"pn": 1, "pz": 500, "po": 1, "np": 1, "fltt": 2, "invt": 2,
                 "fid": "f3", "fs": "m:90 t:2 f:!50", "fields": "f12,f14,f3"})
    if not d:
        return None
    diff = d.get("diff") or []
    if isinstance(diff, dict):
        diff = list(diff.values())
    if not diff:
        return None
    df = pd.DataFrame(diff).rename(columns={"f12": "board_code", "f14": "industry",
                                            "f3": "pct_chg"})
    if "industry" not in df.columns:
        return None
    df.attrs["source"] = "em"
    log.info("东财行业列表(直连): %d 个", len(df))
    return df[[c for c in ("industry", "board_code", "pct_chg") if c in df.columns]]


def _industry_cons_em_direct(board_code: str) -> pd.DataFrame | None:
    """某板块成分股 —— 直连 clist(fs=b:<board_code>)。"""
    rows, pn = [], 1
    while True:
        d = _em_get("/api/qt/clist/get",
                    {"pn": pn, "pz": 200, "po": 1, "np": 1, "fltt": 2, "invt": 2,
                     "fid": "f3", "fs": f"b:{board_code} f:!50", "fields": "f12,f14"})
        if not d:
            break
        diff = d.get("diff") or []
        if isinstance(diff, dict):
            diff = list(diff.values())
        if not diff:
            break
        rows.extend(diff)
        total = d.get("total") or 0
        if total and len(rows) >= total:
            break
        pn += 1
        if pn > 20:
            break
    if not rows:
        return None
    df = pd.DataFrame(rows).rename(columns={"f12": "code", "f14": "name"})
    if "code" not in df.columns:
        return None
    df["code"] = df["code"].astype(str).str.zfill(6)
    return df[["code", "name"]]


def _industry_list_em() -> pd.DataFrame | None:
    # 注: 这里**故意不用** _industry_list_em_direct()。东财行业指数K线(push2his)在本机
    # 不可达, 而模块1 需要每个行业的K线; 同花顺又不认东财独有的行业名(实测"文字媒体"
    # "零食"均 FAIL) -> 若把列表换成东财口径, 景气榜会掉一大批行业。
    # 行业列表保持"同花顺列表+同花顺K线"的一致口径; 个股行业归属另由快照 f100 提供。
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


def _board_code_of(industry: str) -> str | None:
    """行业名 -> 东财板块代码(BKxxxx), 取自行业列表(已缓存)。"""
    try:
        lst = fetch_industry_list()
        if lst is None or lst.empty or "board_code" not in lst.columns:
            return None
        hit = lst[lst["industry"].astype(str) == str(industry)]
        return str(hit.iloc[0]["board_code"]) if len(hit) else None
    except Exception:
        return None


def fetch_industry_cons(industry: str) -> pd.DataFrame | None:
    key = _cache_key("ind_cons", industry, dt.date.today().isoformat())
    c = _cache_load(key)
    if c is not None:
        return c
    # 先走直连(akshare 默认主机 17.push2 在本机被挡, 其余主机可达)
    bc = _board_code_of(industry)
    if bc:
        df = _industry_cons_em_direct(bc)
        if df is not None and not df.empty:
            _cache_save(key, df)
            return df
    if _em_realtime_down:    # 直连也失败 -> 上层回退全市场
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
#  7b) 个股所属行业 (全市场回退时补全 '所属行业' 列)
#      东财个股信息(stock_individual_info_em)在本机被墙 -> 用雪球个股基础信息,
#      其 affiliate_industry.ind_name 走 HTTPS(443) 可达; 东财兜底(多不可用)。
# ===========================================================================
def _xq_symbol(code: str) -> str:
    code = str(code).zfill(6)
    if code.startswith("920") or code.startswith(("8", "4")):   # 北交所
        return "BJ" + code
    if code.startswith(("6", "9")):                             # 沪市
        return "SH" + code
    return "SZ" + code                                          # 深市 00/30/2


# akshare 内置的 xq_a_token 是硬编码的, 会过期 (过期后接口返回的 JSON 里没有 'data',
# 表现为每只都查不到行业)。这里进程内自取一枚新鲜 token (访问雪球首页拿 cookie), 失败
# 则回落到 akshare 内置值。只取一次, 多线程共用。
_xq_token = None
_xq_token_lock = threading.Lock()


def _get_xq_token() -> str | None:
    global _xq_token
    if _xq_token is not None:
        return _xq_token or None
    with _xq_token_lock:
        if _xq_token is not None:
            return _xq_token or None
        tok = ""
        try:
            import requests
            s = requests.Session()
            s.headers.update({"User-Agent": _BROWSER_UA})
            s.get("https://xueqiu.com/", timeout=CONFIG["fetch"]["timeout_sec"])
            tok = s.cookies.get("xq_a_token") or ""
            if tok:
                log.info("雪球 token 已自动刷新")
        except Exception as e:
            log.debug("雪球 token 获取失败, 用 akshare 内置值: %s", e)
        _xq_token = tok
        return tok or None


def _stock_industry_from_xq(code: str) -> str | None:
    try:
        raw = call_with_retry(_ak().stock_individual_basic_info_xq,
                              symbol=_xq_symbol(code), token=_get_xq_token())
    except Exception as e:
        log.debug("雪球个股信息 %s 失败: %s", code, e)
        return None
    if raw is None or len(raw) == 0:
        return None
    try:
        d = dict(zip(raw.iloc[:, 0].astype(str), raw.iloc[:, 1]))
    except Exception:
        return None
    ind = d.get("affiliate_industry")
    if isinstance(ind, str):
        import ast
        try:
            ind = ast.literal_eval(ind)
        except Exception:
            ind = None
    if isinstance(ind, dict):
        name = ind.get("ind_name")
        return str(name) if name else None
    return None


def fetch_hist_long(code: str, years: int = 10) -> pd.DataFrame | None:
    """个股**长历史**日线(前复权), 供模块5做"历次深跌后表现"的历史类比统计。

    与 fetch_hist 分开: 主扫描只需 500 天, 这里要 ~10 年才够找到多次深跌事件。
    走新浪 stock_zh_a_daily(本机可达且线程安全; 东财日线在本机被限)。按日缓存。
    """
    key = _cache_key("hist_long", code, years, dt.date.today().isoformat())
    c = _cache_load(key)
    if c is not None:
        return c
    end = dt.date.today()
    start = end - dt.timedelta(days=int(years * 365.25))
    try:
        raw = call_with_retry(
            _ak().stock_zh_a_daily, symbol=_sina_symbol(code), adjust="qfq",
            start_date=start.strftime("%Y%m%d"), end_date=end.strftime("%Y%m%d"))
    except Exception as e:
        log.debug("长历史日线 %s 失败: %s", code, e)
        return None
    if raw is None or len(raw) == 0:
        return None
    df = _finalize_hist(rename_normalize(raw, {
        "date": ["date", "日期"], "open": ["open", "开盘"], "high": ["high", "最高"],
        "low": ["low", "最低"], "close": ["close", "收盘"], "volume": ["volume", "成交量"],
    }))
    if df is None or df.empty:
        return None
    _cache_save(key, df)
    return df


_ind_map = None
_ind_map_lock = threading.Lock()


def fetch_industry_map() -> dict:
    """全市场 {code: 所属行业} —— 一次批量请求拿完 (东财口径, 如 '半导体'/'银行Ⅱ')。

    关键: 走 **push2delay** 主机。本机 push2/push2his 被墙(见项目笔记), 只有 delay 可达;
    它的 clist 接口带 f100(所属行业) 字段, 5500+ 只一次返回, ~2s, 纯 JSON 线程安全
    —— 远优于逐只查(雪球有WAF; 巨潮 stock_profile_cninfo 用 py_mini_racer, 多线程会
    让进程硬崩)。拿不到则返回空 dict, 上层再逐只降级。
    """
    global _ind_map
    if _ind_map is not None:
        return _ind_map
    with _ind_map_lock:
        if _ind_map is not None:
            return _ind_map
        key = _cache_key("ind_map", dt.date.today().isoformat())
        c = _cache_load(key)
        if c:
            _ind_map = c
            return _ind_map
        m = {}
        used_host = None
        # 主机故障转移: 本机对各 push2 主机的可达性会变(代理时好时坏), 逐个试。
        # 2026-07-27 实测 push2.eastmoney.com 可达而 push2delay SSL 报错 —— 反过来的
        # 情况以前也出现过, 所以这里不写死, 谁通用谁。
        HOSTS = ("push2.eastmoney.com", "82.push2.eastmoney.com",
                 "push2delay.eastmoney.com", "push2his.eastmoney.com")
        for host in HOSTS:
            m = {}
            try:
                import requests
                sess = requests.Session()
                sess.headers.update({"User-Agent": _BROWSER_UA,
                                     "Referer": "https://quote.eastmoney.com/"})
                url = f"https://{host}/api/qt/clist/get"
                page, total, PZ = 1, None, 100      # 服务端每页上限 100, 必须分页
                while page <= 80:                   # 上限兜底, 正常 ~59 页
                    r = sess.get(url, params={
                        "pn": page, "pz": PZ, "po": 0, "np": 1, "fltt": 2, "invt": 2,
                        "fid": "f12", "fs": "m:0 t:6,m:0 t:80,m:1 t:2,m:1 t:23",
                        "fields": "f12,f14,f100"},
                        timeout=CONFIG["fetch"]["timeout_sec"])
                    d = ((r.json() or {}).get("data") or {})
                    rows = d.get("diff") or []
                    if isinstance(rows, dict):      # 某些主机返回 {"0":{...}} 形式
                        rows = list(rows.values())
                    if total is None:
                        total = d.get("total") or 0
                    if not rows:
                        break
                    for it in rows:
                        code = str(it.get("f12") or "").zfill(6)
                        ind = str(it.get("f100") or "").strip()
                        if code and ind and ind not in ("-", "—"):
                            m[code] = ind
                    if total and page * PZ >= total:
                        break
                    page += 1
            except Exception as e:  # noqa: BLE001
                log.debug("批量行业映射失败(%s): %s", host, e)
                continue
            if m:
                used_host = host
                break
        if m:
            log.info("批量行业映射: %d 只 (东财 %s)", len(m), used_host)
            _cache_save(key, m)
        else:
            log.warning("批量行业映射获取失败(所有东财主机不可达), 将逐只降级")
        _ind_map = m
        return _ind_map


# 巨潮 stock_profile_cninfo 内部走 py_mini_racer(V8) 解密, **多线程并发会让进程直接
# abort**(libmini_racer address_pool_manager Check failed) —— 与阶段C同源的坑。
# 用全局锁把它串行化; 它只作为批量映射之外的兜底, 调用量很小。
_cninfo_lock = threading.Lock()


def _stock_industry_from_cninfo(code: str) -> str | None:
    """巨潮个股概况的『所属行业』(证监会口径, 如 '酒、饮料和精制茶制造业')。
    全A覆盖、在本机可达, 作为雪球(有WAF)与东财(被墙)之外的主力兜底。"""
    try:
        with _cninfo_lock:                     # 串行, 防 V8 并发崩进程
            raw = call_with_retry(_ak().stock_profile_cninfo, symbol=str(code).zfill(6))
    except Exception as e:
        log.debug("巨潮个股概况 %s 失败: %s", code, e)
        return None
    if raw is None or len(raw) == 0 or "所属行业" not in raw.columns:
        return None
    try:
        v = raw.iloc[0]["所属行业"]
    except Exception:
        return None
    v = None if v is None else str(v).strip()
    return v or None


_stk_ind_miss = set()          # 本轮已查且没结果的代码 (只在进程内, 不落盘)
_stk_ind_miss_lock = threading.Lock()


def fetch_stock_industry(code: str) -> str | None:
    """个股所属行业名。优先雪球(走443可达), 东财兜底。

    只把"查到的结果"写进磁盘缓存。查不到的仅记在进程内集合里(同轮不重复空查),
    **不落盘** —— 否则数据源临时挂掉(如雪球 token 过期)会把空结果缓存一整天,
    当天即便修好了也全是 '—'(2026-07-27 踩过这个坑)。
    """
    code = str(code).zfill(6)
    m = fetch_industry_map()                      # 0) 批量映射: 一次请求覆盖全市场, 最优先
    if m.get(code):
        return m[code]
    key = _cache_key("stk_ind", code, dt.date.today().isoformat())
    c = _cache_load(key)
    if c:                        # 只认非空的缓存
        return c
    with _stk_ind_miss_lock:
        if code in _stk_ind_miss:
            return None
    name = _stock_industry_from_xq(code)          # 1) 雪球(名称贴近THS口径, 但有WAF)
    if not name:
        name = _stock_industry_from_cninfo(code)  # 2) 巨潮(证监会口径, 串行)
    if not name:
        info = fetch_basic_info(code)             # 3) 东财兜底(本机多不可用)
        name = (info or {}).get("industry")
    if name:
        _cache_save(key, name)
    else:
        with _stk_ind_miss_lock:
            _stk_ind_miss.add(code)
    return name or None


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
