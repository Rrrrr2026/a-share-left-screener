#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""coilscan — 共用核心 leftside_core.coilscan 的本仓库入口。
A股版注入"点时基本面档位": 业绩报表按法定披露截止日滞后, 无前视。"""
from bisect import bisect_right

from . import market as _market          # noqa: F401
import leftside_core.coilscan as _core

globals().update({k: v for k, v in vars(_core).items() if not k.startswith("__")})

N_PERIODS = 28               # ~7年报告期: 5年窗口内的早期episode也够算同比


def _avail(period: str) -> str:
    """报告期 -> 法定披露截止日 (保守: 实际多数更早, 宁可晚不可早)。"""
    y, m = int(period[:4]), period[5:7]
    return {"03": f"{y}-04-30", "06": f"{y}-08-31",
            "09": f"{y}-10-31", "12": f"{y + 1}-04-30"}[m]


def build_quality_at(cache: str | None = None):
    """-> quality_at(code, date) -> 0-3 档位 (增长4季/年度增长/ROE>=15) 或 None。
    cache: json 路径; 存在则直接加载 (夜间预抓 -> 白天扫描免拉东财)。"""
    import json
    import os
    if cache and os.path.exists(cache):
        raw = json.load(open(cache, encoding="utf-8"))
        timeline = {c: (v[0], v[1]) for c, v in raw.items()}
        return _lookup_fn(timeline)
    from . import datasource as ds
    from .quality import _single_quarters, _yoy, _annual_yoy, _latest_annual
    reports = ds.fetch_profit_reports(N_PERIODS)
    timeline = {}                         # code -> ([avail_dates], [qcounts])
    for code, rep in reports.items():
        periods, ni, rev, roe = (rep["periods"], rep["ni_cum"],
                                 rep["rev_cum"], rep.get("roe_cum") or [])
        order = sorted(range(len(periods)), key=lambda i: periods[i])
        pts = []
        for k in range(1, len(order) + 1):
            idx = order[:k]
            p_k = [periods[i] for i in idx]
            ni_k = [ni[i] for i in idx]
            rev_k = [rev[i] for i in idx]
            roe_k = [roe[i] for i in idx] if roe else []
            # 近4单季净利同比 + 最新单季营收同比
            sq_ni = _single_quarters(p_k, ni_k)
            sq_rev = _single_quarters(p_k, rev_k)
            ys = sorted(sq_ni, reverse=True)
            ni_yoys = []
            for p in ys[:6]:
                prev = f"{int(p[:4]) - 1}{p[4:]}"
                g = _yoy(sq_ni.get(p), sq_ni.get(prev))
                if g is not None:
                    ni_yoys.append(g)
                if len(ni_yoys) == 4:
                    break
            rev_g = None
            for p in sorted(sq_rev, reverse=True)[:1]:
                prev = f"{int(p[:4]) - 1}{p[4:]}"
                rev_g = _yoy(sq_rev.get(p), sq_rev.get(prev))
            g_q4 = bool(len(ni_yoys) == 4 and all(v > 0 for v in ni_yoys)
                        and rev_g is not None and rev_g > 0)
            ni_y = _annual_yoy(p_k, ni_k, 1)
            rev_y = _annual_yoy(p_k, rev_k, 1)
            g_year = bool(ni_y and rev_y and ni_y[0][1] > 0 and rev_y[0][1] > 0)
            _, roe_a = _latest_annual(p_k, roe_k) if roe_k else (None, None)
            g_roe = bool(roe_a is not None and roe_a >= 15.0)
            pts.append((_avail(periods[order[k - 1]]), int(g_q4) + int(g_year) + int(g_roe)))
        pts.sort()
        timeline[code] = ([a for a, _ in pts], [q for _, q in pts])
    if cache:
        json.dump({c: [v[0], v[1]] for c, v in timeline.items()},
                  open(cache, "w", encoding="utf-8"))
    return _lookup_fn(timeline)


def _lookup_fn(timeline: dict):
    def quality_at(code: str, date: str):
        tl = timeline.get(code)
        if not tl:
            return None
        i = bisect_right(tl[0], date) - 1
        return tl[1][i] if i >= 0 else None
    return quality_at


if __name__ == "__main__":
    import logging
    import os
    from .config import DATA_DIR
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    qa = build_quality_at(cache=os.path.join(DATA_DIR, "quality_timeline.json"))
    run(quality_at=qa)
