#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
财报预约披露日 (东财数据中心 RPT_PUBLIC_BS_APPOIN)
==================================================
左侧策略最常见的可避免亏损: 财报前三天建仓, 一份差财报把剧本打穿。
这里一次拉全市场的"预约披露日期", 给每只候选标 earn_date / earn_days,
前端在 7 天内亮出 📅, 错杀卡片提示"财报前不建仓"。
akshare 的 stock_yysj_em 在本版本解析列数不匹配 -> 直连数据中心接口, 按天缓存。
"""
from __future__ import annotations
import datetime as dt
import logging
import time

log = logging.getLogger("ashare.earnings_cal")
URL = "https://datacenter-web.eastmoney.com/api/data/v1/get"


def _current_periods(today: dt.date) -> list[str]:
    """正在/即将披露的报告期。年报与一季报同在 1-4 月; 半年报 7-8 月; 三季报 10 月。"""
    y, m = today.year, today.month
    if m <= 4:
        return [f"{y - 1}-12-31", f"{y}-03-31"]
    if m <= 8:
        return [f"{y}-06-30"]
    if m <= 10:
        return [f"{y}-09-30"]
    return [f"{y}-12-31"]


def fetch_appoint_map() -> dict:
    """code -> 'YYYY-MM-DD' (尚未实际披露的最近预约日); 已披露的期不给日期。按天缓存。"""
    from . import datasource as ds
    key = ds._cache_key("appoint", dt.date.today().isoformat())
    c = ds._cache_load(key)
    if isinstance(c, dict):
        return c
    out: dict = {}
    n_fail = 0
    for rd in _current_periods(dt.date.today()):
        page = 1
        while True:
            p = {"reportName": "RPT_PUBLIC_BS_APPOIN",
                 "columns": "SECURITY_CODE,APPOINT_PUBLISH_DATE,ACTUAL_PUBLISH_DATE,REPORT_DATE",
                 "filter": f"(REPORT_DATE='{rd}')", "pageNumber": page, "pageSize": 500,
                 "sortColumns": "SECURITY_CODE", "sortTypes": 1, "source": "WEB", "client": "WEB"}
            try:
                r = ds._http().get(URL, params=p, timeout=20,
                                   headers={"User-Agent": ds._BROWSER_UA,
                                            "Referer": "https://data.eastmoney.com/"})
                res = (r.json() or {}).get("result") or {}
            except Exception as e:
                log.warning("预约披露 %s 第%d页失败: %s", rd, page, e)
                n_fail += 1
                break
            for row in res.get("data") or []:
                code = str(row.get("SECURITY_CODE") or "").zfill(6)
                ap = (row.get("APPOINT_PUBLISH_DATE") or "")[:10]
                act = (row.get("ACTUAL_PUBLISH_DATE") or "")[:10]
                if not code or not ap or act:
                    continue                        # 已实际披露的不算"即将"
                prev = out.get(code)
                if prev is None or ap < prev:
                    out[code] = ap
            if page >= int(res.get("pages") or 1):
                break
            page += 1
            time.sleep(0.2)
    log.info("预约披露日: %d 只待披露", len(out))
    if out and n_fail == 0:
        ds._cache_save(key, out)
    return out


def annotate(cands: list[dict], as_of: str | None = None) -> int:
    """给候选写 earn_date / earn_days (距数据日的自然日数); 返回标注数。"""
    try:
        amap = fetch_appoint_map()
    except Exception as e:
        log.warning("预约披露日获取失败: %s", e)
        return 0
    base = dt.date.fromisoformat(as_of) if as_of else dt.date.today()
    n = 0
    for c in cands:
        d = amap.get(str(c.get("code") or "").zfill(6))
        if not d:
            continue
        try:
            days = (dt.date.fromisoformat(d) - base).days
        except ValueError:
            continue
        if days < 0:
            continue
        c["earn_date"] = d
        c["earn_days"] = days
        n += 1
    return n
