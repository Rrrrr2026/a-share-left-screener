#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
模块9 — 👑 优质公司推荐 (Quality Compounders)
=============================================
与"左侧候选"不同, 这是对**全市场**做的质量筛选: 找"连续增长 + 行业龙头 +
高ROE + 深护城河 + 估值不贵"的公司, 每天给出打分排名前10 (硬性门槛全过的
标记 👑, 未全过的列出差在哪一条)。

数据: 东财业绩报表按报告期批量 (全市场归母净利/营收/加权ROE, 20期≈5年) +
全A快照 (PE-TTM/总市值) + 行业成分映射; 研发强度用同花顺年度利润表,
只对入围短名单逐只取 (py_mini_racer 不允许并发)。

硬性门槛 (全过 = 👑):
  Q4  近四个单季: 营收与归母净利的单季同比全部 > 0 (官方累计口径相邻期差分)
  Y4  近四个完整年度: 营收与净利同比全部 > 0
  ROE 最近年度加权ROE >= 15%
  PE  0 < PE-TTM < 31
  DOM 行业营收排名 <= 3 (同行业内按最近年度营收)
加分项 (进评分不进门槛):
  研发强度 (研发费用/营收, 金融行业豁免) / 增长加速(最近单季同比>=TTM同比) /
  ROE持续 (近4年每年>=12%) / 估值更低 / 龙头份额更大
诚实声明: A股没有干净的"一致预期"数据源, "超市场预期"用"增长仍在加速"近似,
美股版用财报日历的实际EPS超预期率。筛选只看已公布财报 —— 财报有滞后,
榜单是"客观条件筛选"而非投资建议, 买前仍需人工研究。
"""
from __future__ import annotations
import datetime as dt
import json
import logging
import os

import numpy as np

from .config import DASHBOARD_DIR, DATA_DIR

log = logging.getLogger("ashare.quality")

QL_JS = os.path.join(DASHBOARD_DIR, "quality_data.js")
QL_JSON = os.path.join(DATA_DIR, "quality_result.json")

N_PERIODS = 20           # 报告期深度 (~5年: 4个完整年度同比要第5个年报做基数)
PE_MAX = 31.0            # 用户指定: PE-TTM 上限
ROE_MIN = 15.0           # 最近年度加权ROE下限 (%)
DOM_RANK_MAX = 3         # 行业营收排名门槛
TOP_N = 10               # 每日榜单条数
SHORTLIST = 30           # 研发强度只对前 N 名逐只补数据 (THS 单线程)
RD_GOOD, RD_OK = 5.0, 3.0
FIN_INDUSTRIES = ("银行", "保险", "证券", "多元金融")   # 研发强度豁免

_PREV_Q = {"03": None, "06": "03", "09": "06", "12": "09"}


def _single_quarters(periods: list, cum: list) -> dict:
    """累计口径 -> 单季 {period: value}; 跨期缺失不硬拆 (相邻期差分)。"""
    out = {}
    by = dict(zip(periods, cum))
    for p, v in by.items():
        if v is None:
            continue
        m = p[5:7]
        prev_m = _PREV_Q.get(m)
        if prev_m is None:                       # Q1 即单季
            out[p] = v
            continue
        prev_p = f"{p[:5]}{prev_m}-{30 if prev_m in ('06', '09') else 31}"
        pv = by.get(prev_p)
        if pv is not None:
            out[p] = v - pv
    return out


def _yoy(cur: float | None, prev: float | None) -> float | None:
    if cur is None or prev is None or prev == 0:
        return None
    if prev < 0:
        return None                              # 基数为负, 同比无意义
    return (cur - prev) / abs(prev) * 100.0


def _last_q4_yoy(periods: list, cum: list) -> list | None:
    """最近4个单季的同比(%), 新→旧; 数据不足返回 None。"""
    sq = _single_quarters(periods, cum)
    ps = sorted(sq, reverse=True)
    out = []
    for p in ps[:6]:
        prev = f"{int(p[:4]) - 1}{p[4:]}"
        y = _yoy(sq.get(p), sq.get(prev))
        if y is not None:
            out.append((p, y))
        if len(out) == 4:
            break
    return out if len(out) == 4 else None


def _annual_yoy(periods: list, cum: list, n: int = 4) -> list | None:
    """最近 n 个完整年度的同比(%), 新→旧。"""
    ann = {p[:4]: v for p, v in zip(periods, cum) if p.endswith("12-31") and v is not None}
    ys = sorted(ann, reverse=True)
    out = []
    for y in ys:
        prev = str(int(y) - 1)
        g = _yoy(ann.get(y), ann.get(prev))
        if g is not None:
            out.append((y, g))
        if len(out) == n:
            break
    return out if len(out) == n else None


def _latest_annual(periods: list, vals: list):
    for p, v in sorted(zip(periods, vals), reverse=True):
        if p.endswith("12-31") and v is not None:
            return p[:4], v
    return None, None


def _rd_intensity(code: str) -> float | None:
    """年度研发费用/营业总收入 (%), 同花顺年度利润表; 拿不到返回 None。"""
    try:
        from . import datasource as ds
        import akshare as ak
        df = ds.call_with_retry(ak.stock_financial_benefit_ths, symbol=code,
                                indicator="按年度")
        if df is None or len(df) == 0:
            return None
        row = df.iloc[0]
        rd = ds._parse_cn_amount(row.get("研发费用"))
        rev = ds._parse_cn_amount(row.get("*营业总收入") or row.get("营业总收入"))
        if rd is None or not rev:
            return None
        return rd / rev * 100.0
    except Exception:
        return None

def _long_hist(code: str):
    from . import datasource as ds
    df = ds.fetch_long_hist(code, years=5)
    if df is None or len(df) < 120 or "high" not in df.columns:
        return None
    return (df["high"].to_numpy(float), df["close"].to_numpy(float))


def build_quality(top_n: int = TOP_N) -> dict | None:
    from . import datasource as ds
    reports = ds.fetch_profit_reports(N_PERIODS)
    if not reports:
        log.warning("业绩报表批量为空, 优质榜跳过")
        return None
    spot = ds.fetch_spot_snapshot()
    spot_map = {}
    if spot is not None and not spot.empty:
        for _, r in spot.iterrows():
            spot_map[str(r.get("code", "")).zfill(6)] = r.to_dict()

    # 行业映射 + 行业内最近年度营收排名
    ind_of, ind_rev = {}, {}
    try:
        inds = ds.fetch_industry_list()
        names = list(inds["industry"]) if inds is not None and "industry" in inds.columns \
            else (list(inds.iloc[:, 0]) if inds is not None else [])
        for ind in names:
            cons = ds.fetch_industry_cons(ind)
            if cons is None:
                continue
            for _, r in cons.iterrows():
                ind_of[str(r["code"]).zfill(6)] = ind
    except Exception as e:
        log.warning("行业映射构建失败(龙头判定降级): %s", e)
    for code, rep in reports.items():
        _, rev_a = _latest_annual(rep["periods"], rep["rev_cum"])
        ind = ind_of.get(code)
        if ind and rev_a:
            ind_rev.setdefault(ind, []).append((code, rev_a))
    dom_rank, dom_share = {}, {}
    for ind, arr in ind_rev.items():
        arr.sort(key=lambda x: -x[1])
        tot = sum(v for _, v in arr) or 1.0
        for i, (code, v) in enumerate(arr, 1):
            dom_rank[code] = i
            dom_share[code] = v / tot * 100.0

    rows = []
    for code, rep in reports.items():
        sp = spot_map.get(code, {})
        name = sp.get("name")
        pe = sp.get("pe_ttm")
        pe = float(pe) if isinstance(pe, (int, float)) and pe == pe else None
        ni_q4 = _last_q4_yoy(rep["periods"], rep["ni_cum"])
        rev_q4 = _last_q4_yoy(rep["periods"], rep["rev_cum"])
        ni_y4 = _annual_yoy(rep["periods"], rep["ni_cum"])
        rev_y4 = _annual_yoy(rep["periods"], rep["rev_cum"])
        roe_y, roe_a = _latest_annual(rep["periods"], rep.get("roe_cum") or [])
        if ni_q4 is None and ni_y4 is None:
            continue

        g_q4 = bool(ni_q4 and rev_q4
                    and all(v > 0 for _, v in ni_q4) and all(v > 0 for _, v in rev_q4))
        g_y4 = bool(ni_y4 and rev_y4
                    and all(v > 0 for _, v in ni_y4) and all(v > 0 for _, v in rev_y4))
        g_roe = bool(roe_a is not None and roe_a >= ROE_MIN)
        g_pe = bool(pe is not None and 0 < pe < PE_MAX)
        dr = dom_rank.get(code)
        g_dom = bool(dr is not None and dr <= DOM_RANK_MAX)
        gates = {"q4": g_q4, "y4": g_y4, "roe": g_roe, "pe": g_pe, "dom": g_dom}
        n_pass = sum(gates.values())
        if n_pass < 3:                            # 连3条都不过的不进排名池
            continue

        # 评分 (硬门槛之外的排序依据)
        score = 0.0
        if roe_a is not None:
            score += min(30.0, max(0.0, roe_a))
        if ni_y4:
            score += min(20.0, sum(1 for _, v in ni_y4 if v > 0) * 5.0)
        if dr is not None:
            score += 15.0 if dr == 1 else (10.0 if dr <= 3 else 0.0)
        if pe is not None and pe > 0:
            score += 10.0 if pe < 20 else (5.0 if pe < PE_MAX else 0.0)
        accel = None
        if ni_q4:
            ttm_yoy = ni_y4[0][1] if ni_y4 else None
            accel = bool(ttm_yoy is not None and ni_q4[0][1] >= ttm_yoy)
            if accel:
                score += 8.0
        rows.append({
            "code": code, "name": name, "industry": ind_of.get(code),
            "pe": round(pe, 1) if pe is not None else None,
            "roe": round(roe_a, 1) if roe_a is not None else None,
            "roe_year": roe_y,
            "dom_rank": dr,
            "dom_share": round(dom_share.get(code), 1) if code in dom_share else None,
            "ni_q4": [round(v, 1) for _, v in ni_q4][::-1] if ni_q4 else None,   # 旧→新
            "rev_q4": [round(v, 1) for _, v in rev_q4][::-1] if rev_q4 else None,
            "ni_y4": [round(v, 1) for _, v in ni_y4][::-1] if ni_y4 else None,
            "accel": accel,
            "gates": gates, "n_pass": n_pass, "score": round(score, 1),
        })

    rows.sort(key=lambda r: (-r["n_pass"], -(r["score"] or 0)))
    short = rows[:SHORTLIST]
    # 研发强度: 只对短名单逐只取 (THS 单线程, 金融行业豁免)
    for r in short:
        if r.get("industry") in FIN_INDUSTRIES:
            r["rd"] = None
            r["rd_exempt"] = 1
            continue
        rd = _rd_intensity(r["code"])
        r["rd"] = round(rd, 1) if rd is not None else None
        if rd is not None:
            r["score"] = round(r["score"] + (10.0 if rd >= RD_GOOD
                                             else (6.0 if rd >= RD_OK else 0.0)), 1)
    short.sort(key=lambda r: (-r["n_pass"], -(r["score"] or 0)))
    picks = short[:top_n]
    try:
        from . import prob20
        prob20.annotate(picks, _long_hist, conditional=False, key="p20")
    except Exception as e:
        log.warning("30日涨20%%概率计算失败: %s", e)
    n_crown = sum(1 for r in picks if r["n_pass"] == 5)
    result = {
        "meta": {"date": dt.date.today().isoformat(),
                 "generated": dt.datetime.now().strftime("%Y-%m-%d %H:%M"),
                 "n_screened": len(reports), "n_pool": len(rows),
                 "n_crown": n_crown, "pe_max": PE_MAX, "roe_min": ROE_MIN},
        "picks": picks,
    }
    json.dump(result, open(QL_JSON, "w", encoding="utf-8"), ensure_ascii=False)
    with open(QL_JS, "w", encoding="utf-8") as f:
        f.write("window.__QL__ = ")
        json.dump(result, f, ensure_ascii=False)
        f.write(";\n")
    log.info("优质榜: 全市场 %d, 入池 %d, 榜单 %d (👑全过 %d)",
             len(reports), len(rows), len(picks), n_crown)
    return result


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    build_quality()
