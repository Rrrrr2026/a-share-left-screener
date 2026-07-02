#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
导出层 (Export)
===============
把某个 run_date 的库表汇成仪表盘数据对象, 写成:
  dashboard/dashboard_data.js  ->  window.__ASHARE__ = {...};
  data/candidates_<date>.csv   ->  主表中文表头, 一键导出 (utf-8-sig, Excel可读)
仪表盘 index.html 用 <script src="dashboard_data.js"> 直接读取, 双击即可打开。
"""
from __future__ import annotations
import os
import csv
import json
import datetime as dt
import logging

from . import db
from .config import DASHBOARD_DATA_JS, DATA_DIR, CONFIG

log = logging.getLogger("ashare.export")

DISCLAIMER = ("本系统仅做技术/基本面数据的自动化整理与形态筛选, 不构成任何投资建议。"
              "“左侧买入”是在下跌中、支撑确认前进场, 风险天然更高(可能继续下跌或破位)。"
              "所有标的需人工复核, 使用者自负盈亏与风控。")

# 主表中文表头 (顺序即 PRD §8.3)
MAIN_COLUMNS = [
    ("code", "代码"), ("name", "名称"), ("industry", "所属行业"),
    ("tag", "结论标签"), ("final_score", "综合分"), ("tech_score", "技术分"),
    ("fund_score", "基本面分"), ("price", "现价"), ("dist_support_pct", "距支撑%"),
    ("support_disp", "关键支撑位"), ("breakdown_price", "破位位"),
    ("pos_52w_pct", "52周位置%"), ("ret_half_year_pct", "近半年涨跌%"),
    ("turnover", "换手率"), ("volume_ratio", "量比"), ("kdj_tag", "KDJ"),
    ("pe_disp", "市盈率TTM(分位)"), ("pb", "市净率"), ("eps", "EPS"), ("roe", "ROE"),
]


def _index_by_code(rows):
    return {r["code"]: r for r in rows}


def build_payload(run_date: str | None = None) -> dict:
    if run_date is None:
        run_date = db.latest_run_date()
    if run_date is None:
        return {"meta": {"run_date": None, "candidates": []}, "industries": [],
                "candidates": [], "details": {}}

    runlog = db.fetch_run_log(run_date) or {}
    industries = db.fetch_table("industry_score", run_date)
    tech = _index_by_code(db.fetch_table("tech_scan", run_date))
    fund = _index_by_code(db.fetch_table("fundamental", run_date))
    finals = db.fetch_table("final_rank", run_date)
    details_rows = db.fetch_table("stock_detail", run_date)

    # 行业榜 (按景气分降序)
    industries_sorted = sorted(industries, key=lambda r: (r.get("prosperity_score") or -1),
                               reverse=True)
    selected_inds = [r["industry"] for r in industries_sorted if r.get("selected")]

    candidates = []
    for fr in finals:
        code = fr["code"]
        t = tech.get(code, {})
        f = fund.get(code, {})
        support_disp = None
        if t.get("support_price") is not None:
            support_disp = f"{t.get('support_label') or '支撑'} {round(float(t['support_price']), 2)}"
        pe_disp = None
        if f.get("pe_ttm") is not None:
            pe_disp = f"{round(f['pe_ttm'],1)}"
            if f.get("pe_pct") is not None:
                pe_disp += f" ({round(f['pe_pct'])}%分位)"
        row = {
            **fr,
            # 技术/行情字段
            "price": t.get("price"),
            "dist_support_pct": t.get("dist_support_pct"),
            "support_label": t.get("support_label"),
            "support_price": t.get("support_price"),
            "support_disp": support_disp,
            "breakdown_price": t.get("breakdown_price"),
            "pos_52w_pct": t.get("pos_52w_pct"),
            "high_52w": t.get("high_52w"), "low_52w": t.get("low_52w"),
            "ret_half_year_pct": t.get("ret_half_year_pct"),
            "turnover": t.get("turnover"), "volume_ratio": t.get("volume_ratio"),
            "amount_today": t.get("amount_today"), "avg_amt20_yi": t.get("avg_amt20_yi"),
            "kdj_tag": t.get("kdj_tag"),
            "kdj_k": t.get("kdj_k"), "kdj_d": t.get("kdj_d"), "kdj_j": t.get("kdj_j"),
            "rsi": t.get("rsi"),
            "sig_channel": t.get("sig_channel"), "sig_pivot": t.get("sig_pivot"),
            "sig_ma": t.get("sig_ma"), "sig_osc": t.get("sig_osc"),
            "n_hit": t.get("n_hit"),
            # 基本面字段
            "pe_ttm": f.get("pe_ttm"), "pe_pct": f.get("pe_pct"),
            "pe_industry_median": f.get("pe_industry_median"),
            "pe_vs_industry": f.get("pe_vs_industry"), "pe_disp": pe_disp,
            "pb": f.get("pb"), "pb_pct": f.get("pb_pct"),
            "dividend_yield": f.get("dividend_yield"),
            "eps": f.get("eps"), "eps_yoy": f.get("eps_yoy"), "roe": f.get("roe"),
            "revenue_yoy": f.get("revenue_yoy"), "netprofit_yoy": f.get("netprofit_yoy"),
            "gross_margin": f.get("gross_margin"), "debt_ratio": f.get("debt_ratio"),
            "roe_trend": _loads(f.get("roe_trend_json")),
            "fund_flags": _loads(f.get("fund_flags_json")),
        }
        candidates.append(row)

    candidates.sort(key=lambda r: (-(r.get("final_score") or -1), r.get("code") or ""))
    candidates = candidates[:CONFIG["output"]["final_top_n"]]   # 展示上限

    details = {}
    for dr in details_rows:
        details[dr["code"]] = _loads(dr["detail_json"], default={})

    payload = {
        "meta": {
            "run_date": run_date,
            "updated_at": runlog.get("finished_at") or run_date,
            "n_scanned": runlog.get("n_scanned"),
            "n_hit": len(candidates),   # 与主表展示条数一致
            "selected_industries": selected_inds,
            "disclaimer": DISCLAIMER,
        },
        "industries": industries_sorted,
        "candidates": candidates,
        "details": details,
        "columns": [{"key": k, "label": lab} for k, lab in MAIN_COLUMNS],
    }
    return payload


def write_dashboard_js(run_date: str | None = None) -> str:
    payload = build_payload(run_date)
    os.makedirs(os.path.dirname(DASHBOARD_DATA_JS), exist_ok=True)
    js = "window.__ASHARE__ = " + json.dumps(payload, ensure_ascii=False) + ";\n"
    with open(DASHBOARD_DATA_JS, "w", encoding="utf-8") as f:
        f.write(js)
    log.info("仪表盘数据已写出: %s (%d 候选)", DASHBOARD_DATA_JS, len(payload["candidates"]))
    return DASHBOARD_DATA_JS


def write_csv(run_date: str | None = None) -> str:
    payload = build_payload(run_date)
    rd = payload["meta"]["run_date"] or dt.date.today().isoformat()
    path = os.path.join(DATA_DIR, f"candidates_{rd}.csv")
    headers = [lab for _, lab in MAIN_COLUMNS]
    keys = [k for k, _ in MAIN_COLUMNS]
    with open(path, "w", encoding="utf-8-sig", newline="") as f:
        wtr = csv.writer(f)
        wtr.writerow(headers)
        for r in payload["candidates"]:
            wtr.writerow(["—" if r.get(k) in (None, "") else r.get(k) for k in keys])
    log.info("CSV 已导出: %s", path)
    return path


def _loads(s, default=None):
    if not s:
        return default if default is not None else []
    try:
        return json.loads(s)
    except Exception:
        return default if default is not None else []
