#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
A股 Market 适配器 — leftside_core 共用核心的全部市场差异都在这里
==================================================================
回测交易规则 (T+1 / 一字涨跌停 / 0.3% 往返成本)、成长质量标签、价格序列
(腾讯前复权, 盘中丢弃未收盘bar)、基准指数 (沪深300)、个股新闻标题与风险关键词。
"""
from __future__ import annotations
import datetime as dt
import logging

import numpy as np

from .config import DASHBOARD_DIR, DATA_DIR, DB_PATH
from leftside_core.market import Market, set_market

log = logging.getLogger("ashare.market")

GROWTH_TIER = {"🟢 可持续": "G", "🟡 待观察": "M", "🔴 一次性": "W"}
TIER_LABEL = {"G": "🟢 可持续", "M": "🟡 待观察", "W": "🔴 一次性", "NA": "⚪ 无数据"}

NEWS_KEYWORDS = [
    ("减持", "减持"), ("立案", "立案/调查"), ("调查", "立案/调查"), ("处罚", "处罚"), ("被罚", "处罚"),
    ("警示函", "监管措施"), ("问询", "问询函"), ("关注函", "问询函"), ("商誉", "商誉减值"),
    ("减值", "减值"), ("预亏", "预亏"), ("亏损", "亏损"), ("下修", "下修"), ("业绩下滑", "业绩下滑"),
    ("诉讼", "诉讼"), ("仲裁", "诉讼"), ("质押", "质押"), ("违规", "违规"), ("退市", "退市风险"),
    ("辞职", "高管变动"), ("离职", "高管变动"), ("停牌", "停牌"), ("终止", "终止事项"),
    ("解禁", "解禁"), ("定增", "再融资"), ("配股", "再融资"), ("可转债", "再融资"),
]


def _drop_partial_today() -> str | None:
    """若北京时间尚未收盘(15:05前), 返回今天的日期串 -> 丢弃当日未走完的bar。"""
    bj = dt.datetime.now(dt.timezone(dt.timedelta(hours=8)))
    if bj.hour < 15 or (bj.hour == 15 and bj.minute < 5):
        return bj.date().isoformat()
    return None


def fetch_price_series(codes: list, start: str) -> dict:
    """code -> {"dates":[...], "ohlc": ndarray[N,4] (o,h,l,c)}; 腾讯前复权日线。
    窗口只有几个月 (<640根), 单请求即可拿全; 盘中运行时丢弃今天未走完的bar。
    (stock_detail 兜底由核心统一处理, 这里只负责网络取数。)"""
    from concurrent.futures import ThreadPoolExecutor
    from . import datasource as ds
    today = dt.date.today().isoformat()
    skip_day = _drop_partial_today()
    res = {}

    def one(code):
        try:
            kl = ds.call_with_retry(ds._tencent_chunk, ds._tencent_symbol(code), start, today)
        except Exception:
            return code, None
        rows = []
        for k in (kl or []):
            if not k or len(k) < 5:
                continue
            try:
                o, c, h, l = float(k[1]), float(k[2]), float(k[3]), float(k[4])
            except (TypeError, ValueError):
                continue
            if h < l or min(o, c, h, l) <= 0:
                continue
            d0 = str(k[0])
            if skip_day and d0 >= skip_day:
                continue
            rows.append((d0, o, h, l, c))
        if len(rows) < 5:
            return code, None
        return code, {"dates": [r[0] for r in rows],
                      "ohlc": np.array([r[1:] for r in rows], dtype=float)}

    with ThreadPoolExecutor(max_workers=6) as exe:
        for i, (code, ser) in enumerate(exe.map(one, codes), 1):
            if ser:
                res[code] = ser
            if i % 300 == 0 or i == len(codes):
                log.info("价格进度 %d/%d (拿到 %d)", i, len(codes), len(res))
    return res


def fetch_benchmark():
    from . import datasource as ds
    return ds.fetch_benchmark_close()


def limit_up_oneline(o, h, l, c, prev_c):
    """一字/准一字涨停买不进: 全天几乎无振幅且涨幅接近主板涨停。"""
    if prev_c is None or prev_c <= 0 or c <= 0:
        return False
    return (h - l) < 0.002 * c and c >= prev_c * 1.085


def limit_down_oneline(o, h, l, c, prev_c):
    if prev_c is None or prev_c <= 0 or c <= 0:
        return False
    return (h - l) < 0.002 * c and c <= prev_c * 0.915


def news_titles(code: str) -> list:
    """[(date 'YYYY-MM-DD', title, url)] 东财个股新闻; 失败返回 []。"""
    try:
        from . import datasource as ds
        import akshare as ak
        df = ds.call_with_retry(ak.stock_news_em, symbol=code)
        if df is None or len(df) == 0:
            return []
        out = []
        for _, r in df.iterrows():
            t = str(r.get("新闻标题") or "").strip()
            d = str(r.get("发布时间") or "")[:10]
            u = str(r.get("新闻链接") or "")
            if t and d:
                out.append((d, t, u))
        return out
    except Exception as e:
        log.debug("news %s 失败: %s", code, e)
        return []


MARKET = set_market(Market(
    name="ashare",
    dashboard_dir=DASHBOARD_DIR, data_dir=DATA_DIR, db_path=DB_PATH,
    t_plus_one=True, limit_boards=True, cost_rt=0.003,
    growth_tier=GROWTH_TIER, tier_label=TIER_LABEL,
    fetch_price_series=fetch_price_series, fetch_benchmark=fetch_benchmark,
    limit_up_oneline=limit_up_oneline, limit_down_oneline=limit_down_oneline,
    news_titles=news_titles, news_keywords=NEWS_KEYWORDS,
    log_prefix="ashare",
))
