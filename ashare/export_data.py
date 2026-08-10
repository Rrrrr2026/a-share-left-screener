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
    ("dominance_disp", "市场地位"),
    ("tag", "结论标签"), ("streak", "连续上榜"), ("final_score", "综合分"), ("tech_score", "技术分"),
    ("fund_score", "基本面分"), ("price", "现价"), ("spark", "近期走势"), ("dist_support_pct", "距支撑%"),
    ("support_disp", "关键支撑位"), ("breakdown_price", "破位位"),
    ("pos_52w_pct", "52周位置%"), ("ret_1m_pct", "近一月涨%"), ("ret_half_year_pct", "近半年涨跌%"),
    ("turnover", "换手率"), ("volume_ratio", "量比"), ("kdj_tag", "KDJ"),
    ("pe_disp", "市盈率TTM(分位)"), ("pb", "市净率"), ("eps", "EPS"), ("roe", "ROE"),
    ("revenue_yoy", "营收同比%"), ("netprofit_yoy", "归母净利同比%"),
    ("growth_quality", "增速质量"),
    # 注: 盈利指引(买入位/概率)不进主表 —— 它需要图表与方法说明才能被正确解读,
    #     放在详情抽屉的「盈利指引」页签里(第5个)。摘要字段仍随 candidates 下发。
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

    appear = db.recent_appearance_counts(db.recent_run_dates(5))   # 连续上榜次数
    # 🚀 跨日追踪: 蓄势是持续状态, 昨天/前天命中的今天多半还在同一位置。
    # 只看当天快照会因分数在阈值上下浮动而漏掉, 故并入近10轮的命中记录。
    brk_recent = db.recent_breakouts(db.recent_run_dates_any(10))
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
            "ret_1m_pct": t.get("ret_1m_pct"),
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
            "consol": bool(t.get("consol")),
            "consol_score": t.get("consol_score"),
            "consol_note": t.get("consol_note"),
            "growth_quality": f.get("growth_quality"),
            "growth_quality_score": f.get("growth_quality_score"),
            "growth_quality_note": f.get("growth_quality_note"),
            "gross_margin": f.get("gross_margin"), "debt_ratio": f.get("debt_ratio"),
            "roe_trend": _loads(f.get("roe_trend_json")),
            "roe_trend_q": _loads(f.get("roe_trend_q_json")),
            "fund_flags": _loads(f.get("fund_flags_json")),
            # 新增: sparkline / 风控 / 量能 / 斐波那契 / 分析师(A股多为空) / 连续上榜
            "breakout": bool(fr.get("breakout")),
            "breakout_score": fr.get("breakout_score"),
            "breakout_note": fr.get("breakout_note") or "",
            "spark": _loads(t.get("spark_json"), default=[]),
            "atr_pct": t.get("atr_pct"), "max_dd_pct": t.get("max_dd_pct"),
            "beta": t.get("beta"), "vol_ratio_calc": t.get("vol_ratio_calc"),
            "sig_vol": t.get("sig_vol"), "boll_low": t.get("boll_low"),
            "fib_382": t.get("fib_382"), "fib_500": t.get("fib_500"), "fib_618": t.get("fib_618"),
            "target_price": f.get("target_price"), "analyst_rating": f.get("analyst_rating"),
            "analyst_count": f.get("analyst_count"), "upside_pct": f.get("upside_pct"),
            "streak": appear.get(code, 1),
            # 近N轮命中🚀的记录(即使今天没达标也保留, 供筛选与展示)
            "brk_recent_days": (brk_recent.get(code) or {}).get("days", 0),
            "brk_last_date": (brk_recent.get(code) or {}).get("last"),
            # v2.1: 市场地位 + 近四季 归母/营收 单季同比×4 (增速列的四数展示)
            "dominance_disp": f.get("dominance_disp"), "dom_rank": f.get("dom_rank"),
            "dom_n": f.get("dom_n"), "dom_share": f.get("dom_share"),
            "ni_ttm_yoy": f.get("ni_ttm_yoy"), "ni_basis": f.get("ni_basis"),
            "rev_ttm_yoy": f.get("rev_ttm_yoy"), "rev_basis": f.get("rev_basis"),
            "ni_qoq": _loads(f.get("ni_qoq_json"), default=[]),
            "rev_qoq": _loads(f.get("rev_qoq_json"), default=[]),
            "ni_q_labels": _loads(f.get("ni_q_labels_json"), default=[]),
        }
        candidates.append(row)

    # 🚀 蓄势待发 置顶(用户每天首要关注), 其余按综合分降序
    candidates.sort(key=lambda r: (0 if str(r.get("tag", "")).startswith("🚀") else 1,
                                   -(r.get("final_score") or -1), r.get("code") or ""))
    top_n = CONFIG["output"]["final_top_n"]
    head = candidates[:top_n]                                   # 支撑型主榜(展示上限)
    # 深跌抄底桶: 支撑分低会被 final_top_n 截掉, 这里把落榜的 dip 候选按 dip_score 补回来
    # (上限 dip_top_n), 保证深跌超卖股作为独立标签组浮现, 不挤占支撑型名额。
    seen = {r.get("code") for r in head}
    dip_extra = sorted((r for r in candidates[top_n:] if r.get("dip")),
                       key=lambda r: -(r.get("dip_score") or 0.0))[:CONFIG["output"].get("dip_top_n", 40)]
    candidates = head + [r for r in dip_extra if r.get("code") not in seen]

    details = {}
    for dr in details_rows:
        details[dr["code"]] = _loads(dr["detail_json"], default={})

    profiles = {}
    for pr in db.fetch_table("profile", run_date):
        profiles[pr["code"]] = _loads(pr["profile_json"], default={})

    # 盈利指引(模块7): 完整对象放 guidance{}, 主表只挂摘要字段
    guidance = {}
    for gr in db.fetch_table("guidance", run_date):
        guidance[gr["code"]] = _loads(gr["guidance_json"], default={})
    for row in candidates:
        g = guidance.get(row.get("code")) or {}
        row["guid_n"] = g.get("guid_n") or 0
        row["guid_buy_low"] = g.get("guid_buy_low")
        row["guid_buy_high"] = g.get("guid_buy_high")
        row["guid_med_mae"] = g.get("guid_med_mae")
        row["guid_tier"] = g.get("guid_tier")            # 匹配层级(同行业/同波动档/全市场)
        row["guid_vol_ann"] = g.get("guid_vol_ann")      # 本股年化波动率(概率的换算尺子)
        row["guid_same_ind"] = g.get("guid_same_ind")    # 近邻里有多少来自同行业
        row["guid_med_gain"] = g.get("guid_med_gain")
        row["guid_win_rate"] = g.get("guid_win_rate")
        probs = {p["target"]: p["prob"] for p in (g.get("guid_probs") or [])}
        row["guid_p100"] = probs.get(100)
        row["guid_p50"] = probs.get(50)
        row["guid_p30"] = probs.get(30)
        if row["guid_n"] and row["guid_p50"] is not None:
            row["guid_disp"] = (f"+50%:{row['guid_p50']*100:.0f}%／"
                                f"+100%:{(row['guid_p100'] or 0)*100:.0f}%"
                                f"({row['guid_n']}次)")
        else:
            row["guid_disp"] = None

    payload = {
        "meta": {
            "run_date": run_date,
            # 管线正常收尾才有 run_log; 崩溃/中断时没有 -> 退回"本文件生成时刻",
            # 至少让用户知道数据是什么时候产出的, 而不是只剩一个日期。
            "updated_at": (runlog.get("finished_at")
                           or dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")),
            "n_scanned": runlog.get("n_scanned"),
            # 没有 run_log = 那一轮没正常跑完(扫描数等统计缺失)。明确标出来,
            # 免得用户看到空白以为是页面坏了。
            "run_status": ("ok" if runlog.get("finished_at") else "partial"),
            "run_status_note": ("" if runlog.get("finished_at") else
                                "该轮管线未正常收尾(中途报错或被中断), 扫描总数缺失; "
                                "候选股与各项指标仍可用。建议重跑一次取完整数据。"),
            "n_hit": len(candidates),   # 与主表展示条数一致
            "selected_industries": selected_inds,
            "disclaimer": DISCLAIMER,
        },
        "industries": industries_sorted,
        "candidates": candidates,
        "details": details,
        "profiles": profiles,
        "guidance": guidance,
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


HISTORY_DIR = os.path.join(os.path.dirname(DASHBOARD_DATA_JS), "history")


def write_history_snapshot(run_date: str | None = None) -> str | None:
    """把某个 run_date 的候选榜写成"瘦身版"历史快照 (无K线明细/深度档案,
    体积 ~1MB), 供前端的日期切换器回看历史扫描结果:
      dashboard/history/day_<date>.json  +  dashboard/history/index.json (可用日期清单)
    auto_update.bat 会把 history/ 整目录同步到 docs/ 发布。"""
    payload = build_payload(run_date)
    rd = payload["meta"].get("run_date")
    if not rd:
        return None
    slim = {"meta": payload["meta"], "industries": payload["industries"],
            "candidates": payload["candidates"], "columns": payload["columns"]}
    os.makedirs(HISTORY_DIR, exist_ok=True)
    path = os.path.join(HISTORY_DIR, f"day_{rd}.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(slim, f, ensure_ascii=False)
    # 更新清单 (按日期倒序, 只保留 history_days 天)
    keep = CONFIG["output"].get("history_days", 90)
    dates = sorted({fn[4:14] for fn in os.listdir(HISTORY_DIR)
                    if fn.startswith("day_") and fn.endswith(".json")}, reverse=True)
    for stale in dates[keep:]:
        try:
            os.remove(os.path.join(HISTORY_DIR, f"day_{stale}.json"))
        except OSError:
            pass
    dates = dates[:keep]
    with open(os.path.join(HISTORY_DIR, "index.json"), "w", encoding="utf-8") as f:
        json.dump({"dates": dates}, f)
    log.info("历史快照已写出: %s (%d 天可回看)", path, len(dates))
    return path


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
            wtr.writerow(["" if k == "spark" else ("—" if r.get(k) in (None, "") else r.get(k)) for k in keys])
    log.info("CSV 已导出: %s", path)
    return path


def _loads(s, default=None):
    if not s:
        return default if default is not None else []
    try:
        return json.loads(s)
    except Exception:
        return default if default is not None else []
