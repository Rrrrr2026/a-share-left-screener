#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
"为什么跌"的证据: 错杀候选的近期新闻标题关键词标记  [leftside_core 共用版]
=======================================================================
拉每只错杀候选最近的新闻标题, 命中风险关键词就打 🚩。不做判断, 只把线索
放到分数旁边, 让"人工查原因"从十分钟变成十秒。
市场差异: 标题取数函数与关键词表, 来自 Market.news_titles / Market.news_keywords。
"""
from __future__ import annotations
import datetime as dt
import logging

from .market import current

log = logging.getLogger("leftside_core.newsflag")

MAX_ITEMS = 8
WINDOW_DAYS = 30


def flags_in(title: str) -> list:
    low = title.lower()
    f = []
    for kw, lab in current().news_keywords:
        if (kw.lower() in low) and lab not in f:
            f.append(lab)
    return f


def _titles(code: str) -> list:
    fn = current().news_titles
    if fn is None:
        return []
    try:
        out = list(fn(code) or [])
        out.sort(key=lambda x: x[0], reverse=True)
        return out
    except Exception as e:
        log.debug("news %s 失败: %s", code, e)
        return []


def annotate(cands: list[dict], as_of: str | None = None) -> int:
    """只对错杀候选: c['news'] = [{d,t,u,f}], c['news_flags'] = [labels]; 返回有🚩的数量。"""
    base = dt.date.fromisoformat(as_of) if as_of else dt.date.today()
    cutoff = (base - dt.timedelta(days=WINDOW_DAYS)).isoformat()
    n_flag = 0
    for c in cands:
        if not c.get("cuosha_score"):
            continue
        items = [(d, t, u) for (d, t, u) in _titles(c["code"]) if d >= cutoff][:MAX_ITEMS]
        if not items:
            continue
        news, labels = [], []
        for d, t, u in items:
            f = flags_in(t)
            news.append({"d": d, "t": t[:90], "u": u, "f": f})
            for x in f:
                if x not in labels:
                    labels.append(x)
        c["news"] = news
        c["news_flags"] = labels
        if labels:
            n_flag += 1
    return n_flag
