#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
"为什么跌"的证据: 错杀候选的近期新闻/公告标题关键词标记
====================================================
错杀卡片说"无可见基本面恶化, 请人工查原因" —— 这里把证据摆到分数旁边:
拉每只错杀候选最近的新闻标题 (东财个股新闻), 命中风险关键词 (减持/立案/处罚/
商誉减值/预亏/下修/问询/质押/停牌/高管变动...) 就打 🚩。
不做判断, 只把线索放出来, 让"人工查原因"从十分钟变成十秒。
"""
from __future__ import annotations
import datetime as dt
import logging

log = logging.getLogger("ashare.newsflag")

KEYWORDS = [
    ("减持", "减持"), ("立案", "立案/调查"), ("调查", "立案/调查"), ("处罚", "处罚"), ("被罚", "处罚"),
    ("警示函", "监管措施"), ("问询", "问询函"), ("关注函", "问询函"), ("商誉", "商誉减值"),
    ("减值", "减值"), ("预亏", "预亏"), ("亏损", "亏损"), ("下修", "下修"), ("业绩下滑", "业绩下滑"),
    ("诉讼", "诉讼"), ("仲裁", "诉讼"), ("质押", "质押"), ("违规", "违规"), ("退市", "退市风险"),
    ("辞职", "高管变动"), ("离职", "高管变动"), ("停牌", "停牌"), ("终止", "终止事项"),
    ("解禁", "解禁"), ("定增", "再融资"), ("配股", "再融资"), ("可转债", "再融资"),
]
MAX_ITEMS = 8
WINDOW_DAYS = 30


def _titles(code: str) -> list:
    """[(date 'YYYY-MM-DD', title, url)] 最新在前; 失败返回 []。"""
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
        out.sort(key=lambda x: x[0], reverse=True)
        return out
    except Exception as e:
        log.debug("news %s 失败: %s", code, e)
        return []


def flags_in(title: str) -> list:
    f = []
    for kw, lab in KEYWORDS:
        if kw in title and lab not in f:
            f.append(lab)
    return f


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
            news.append({"d": d, "t": t[:80], "u": u, "f": f})
            for x in f:
                if x not in labels:
                    labels.append(x)
        c["news"] = news
        c["news_flags"] = labels
        if labels:
            n_flag += 1
    return n_flag
