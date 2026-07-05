#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
模块6 — 个股深度档案 (Company Profile, A股版)
=============================================
仅对最终候选执行 (阶段C), 组装公司分析栏目所需的全部数据:
  1. 公司简介 (机构简介 / 主营业务 / 法人 / 官网 / 地址) + 管理层近期增减持
  2. 各项业务营收占比 (主营构成饼图, 收入比例/毛利率, 增速异常标注)
  3. 营收增速 (总体年度 + 单季) 与归母净利润
  4. 现金流分析 (经营/投资/筹资/资本开支/收购/分红) + 自动"漏洞/钱去哪了"要点
  5. 风险分析 (资产负债率/流动比率/毛利率/ROE/审计意见)
  6. 政策分析 —— 前端按行业静态梳理 (此处仅提供 sector)
  7. 利好vs利空新闻 + 融资融券多空博弈 (期权博弈的 A股代理)
  8. 暗池代理 —— 大宗交易 (折溢率/机构专用席位/成交额占流通市值)
  9. 企业相关重要新闻 (东财个股新闻)

数据源: akshare(东财/同花顺/新浪/巨潮)。全部字段缺失安全:
取不到 -> None/[] , 前端显示"暂无"。单个子项失败不影响其它子项。
"""
from __future__ import annotations
import logging
import datetime as dt
import threading

import numpy as np
import pandas as pd

from . import datasource as ds

log = logging.getLogger("ashare.module6")

YI = 1e8       # 1 亿
WAN = 1e4      # 1 万

# 全市场数据集(大宗/龙虎榜/两融)对所有候选相同 —— 进程内 memo, 一轮只下载一次。
# 持锁下载: 首个线程下载时其余线程等待, 避免 N 只股票各下一遍全市场文件。
_MEMO: dict = {}
_MEMO_LOCK = threading.Lock()


def _memo(key, loader):
    with _MEMO_LOCK:
        if key in _MEMO:
            return _MEMO[key]
        _MEMO[key] = loader()
        return _MEMO[key]


# --------------------------------------------------------------------------
#  基础工具
# --------------------------------------------------------------------------
def _num(x):
    if x is None:
        return None
    try:
        v = float(x)
        return None if (np.isnan(v) or np.isinf(v)) else v
    except Exception:
        return None


def _r(v, nd=2):
    """四舍五入且清洗 NaN/inf -> None"""
    v = _num(v)
    return None if v is None else round(v, nd)


def _parse_cn_num(s):
    """解析同花顺中文数字串: '277.27亿' / '8.05万亿' / '3521万' / '48.28%' / '-33.48%'。
    百分号 -> 原数值(不除100); 亿/万亿/万 -> 换算为'亿元'口径的浮点。返回 float 或 None。"""
    if s is None:
        return None
    t = str(s).strip()
    if t in ("", "False", "None", "--", "-", "—"):
        return None
    try:
        if t.endswith("%"):
            return float(t[:-1])
        neg = t.startswith("-")
        t = t.lstrip("+-")
        if t.endswith("万亿"):
            v = float(t[:-2]) * 1e4          # 万亿 -> 亿
        elif t.endswith("亿"):
            v = float(t[:-1])
        elif t.endswith("万"):
            v = float(t[:-1]) / 1e4          # 万 -> 亿
        else:
            v = float(t)
        return -v if neg else v
    except Exception:
        return None


def _last(lst):
    if not lst:
        return None, None
    for i in range(len(lst) - 1, -1, -1):
        if lst[i] is not None:
            return lst[i], i
    return None, None


def _safe(fn, *a, **k):
    """带重试地调用 akshare, 任意异常 -> None (缺失安全)。"""
    try:
        return ds.call_with_retry(fn, *a, **k)
    except Exception as e:
        log.debug("profile fetch failed %s: %s", getattr(fn, "__name__", fn), e)
        return None


def _mkt_upper(code: str) -> str:
    """'002594' -> 'SZ002594' (东财主营构成/现金流EM 需大写市场前缀)。"""
    return ds._sina_symbol(code).upper()


# --------------------------------------------------------------------------
#  1) 简介 / 管理层
# --------------------------------------------------------------------------
def _profile_basic(code: str) -> dict:
    out = {"summary": None, "business": None, "sector": None, "website": None,
           "hq": None, "legal_rep": None, "list_date": None, "found_date": None,
           "reg_capital": None}
    ak = ds._ak()
    df = _safe(ak.stock_profile_cninfo, symbol=code)
    if isinstance(df, pd.DataFrame) and not df.empty:
        row = df.iloc[0]
        g = lambda k: (str(row[k]).strip() if k in df.columns and pd.notna(row[k]) else None)
        out["summary"] = g("机构简介")
        out["business"] = g("主营业务")
        out["sector"] = g("所属行业")
        out["website"] = g("官方网站")
        out["hq"] = g("办公地址") or g("注册地址")
        out["legal_rep"] = g("法人代表")
        out["list_date"] = g("上市日期")
        out["found_date"] = g("成立日期")
        rc = g("注册资金")
        out["reg_capital"] = (rc + "万元") if (rc and not str(rc).endswith("元")) else rc
    # 主营业务补充 (同花顺, 更精炼)
    if not out["business"]:
        d2 = _safe(ak.stock_zyjs_ths, symbol=code)
        if isinstance(d2, pd.DataFrame) and not d2.empty and "主营业务" in d2.columns:
            out["business"] = str(d2.iloc[0]["主营业务"]).strip() or None
    return out


def _mgmt_changes(code: str, top: int = 8) -> list:
    """管理层/关联人近期增减持 —— A股可得的'管理层动向'免费信号。"""
    ak = ds._ak()
    df = _safe(ak.stock_management_change_ths, symbol=code)
    if not isinstance(df, pd.DataFrame) or df.empty:
        return []
    dcol = ds.pick_col(df, ["变动日期"], contains=True)
    if dcol:
        df = df.sort_values(dcol)
    out = []
    for _, r in df.tail(top).iloc[::-1].iterrows():
        g = lambda k: (str(r[k]).strip() if k in df.columns and pd.notna(r[k]) else None)
        out.append({"date": g("变动日期"), "person": g("变动人"),
                    "relation": g("与公司高管关系"), "change": g("变动数量"),
                    "price": _num(r["交易均价"]) if "交易均价" in df.columns else None,
                    "way": g("股份变动途径")})
    return out


# --------------------------------------------------------------------------
#  2) 主营构成 (营收拆解饼图 + 分部增速)
# --------------------------------------------------------------------------
def _segments(code: str) -> dict | None:
    ak = ds._ak()
    df = _safe(ak.stock_zygc_em, symbol=_mkt_upper(code))
    if not isinstance(df, pd.DataFrame) or df.empty:
        return None
    for c in ("报告日期", "分类类型", "主营构成", "主营收入", "收入比例", "毛利率"):
        if c not in df.columns:
            return None
    df = df.copy()
    df["报告日期"] = df["报告日期"].astype(str)
    latest = sorted(df["报告日期"].unique())[-1]
    # 优先"按产品分类", 其次行业, 再次地区
    order = ["按产品分类", "按行业分类", "按地区分类"]
    cats = list(df["分类类型"].unique())
    by = next((o for o in order if o in cats), (cats[0] if cats else None))
    if by is None:
        return None
    cur = df[(df["报告日期"] == latest) & (df["分类类型"] == by)]
    if cur.empty:
        return None
    # 去年同期(同分类)用于分部增速
    prev_date = None
    try:
        y, m, d = latest.split("-")
        prev_date = f"{int(y)-1}-{m}-{d}"
    except Exception:
        pass
    prev = df[(df["报告日期"] == prev_date) & (df["分类类型"] == by)] if prev_date else pd.DataFrame()
    prev_map = {}
    if not prev.empty:
        for _, r in prev.iterrows():
            prev_map[str(r["主营构成"])] = _num(r["主营收入"])

    items, total = [], 0.0
    for _, r in cur.iterrows():
        name = str(r["主营构成"]).strip()
        rev = _num(r["主营收入"])
        if rev is None:
            continue
        pv = prev_map.get(name)
        yoy = _r((rev / pv - 1) * 100, 1) if (pv and pv > 0) else None
        margin = _num(r["毛利率"])
        margin = _r(margin * 100, 1) if (margin is not None and abs(margin) <= 1.5) else _r(margin, 1)
        pct = _num(r["收入比例"])
        pct = _r(pct * 100, 1) if (pct is not None and abs(pct) <= 1.5) else _r(pct, 1)
        items.append({"name": name, "value": _r(rev / YI, 2), "pct": pct,
                      "margin": margin, "yoy": yoy})
        if rev > 0:
            total += rev
    if not items:
        return None
    items.sort(key=lambda x: (x["value"] is not None, x["value"] or 0), reverse=True)
    return {"date": latest, "by": by, "total": _r(total / YI, 2), "items": items}


# --------------------------------------------------------------------------
#  3) 营收/净利趋势 (年度 + 单季)
# --------------------------------------------------------------------------
def _revenue_trend(code: str) -> dict | None:
    ak = ds._ak()
    out = {"years": [], "revenue": [], "rev_yoy": [], "net": [], "net_yoy": [],
           "quarters": [], "q_revenue": []}

    # ---- 年度: 同花顺关键指标(按年度) 直接带同比 ----
    yr = _safe(ak.stock_financial_abstract_ths, symbol=code, indicator="按年度")
    if isinstance(yr, pd.DataFrame) and not yr.empty and "报告期" in yr.columns:
        yr = yr.copy()
        yr["报告期"] = yr["报告期"].astype(str)
        yr = yr.sort_values("报告期").tail(6)
        for _, r in yr.iterrows():
            out["years"].append(str(r["报告期"]))
            out["revenue"].append(_parse_cn_num(r.get("营业总收入")))
            out["rev_yoy"].append(_parse_cn_num(r.get("营业总收入同比增长率")))
            out["net"].append(_parse_cn_num(r.get("净利润")))
            out["net_yoy"].append(_parse_cn_num(r.get("净利润同比增长率")))

    # ---- 单季营收: 东财关键指标(宽表, 累计口径 -> 差分成单季) ----
    ab = _safe(ak.stock_financial_abstract, symbol=code)
    if isinstance(ab, pd.DataFrame) and not ab.empty and "指标" in ab.columns:
        rev_row = ab[ab["指标"] == "营业总收入"]
        if not rev_row.empty:
            date_cols = [c for c in ab.columns if str(c).isdigit() and len(str(c)) == 8]
            date_cols = sorted(date_cols)          # 升序
            cum = {}
            for c in date_cols:
                cum[c] = _num(rev_row.iloc[0][c])
            singles = []
            for c in date_cols:
                y, md = c[:4], c[4:]
                v = cum.get(c)
                if v is None:
                    singles.append((c, None)); continue
                if md == "0331":
                    singles.append((c, v))
                else:
                    prev = {"0630": "0331", "0930": "0630", "1231": "0930"}.get(md)
                    pc = (y + prev) if prev else None
                    pv = cum.get(pc)
                    singles.append((c, (v - pv) if (pv is not None) else None))
            singles = singles[-8:]
            qmap = {"0331": "Q1", "0630": "Q2", "0930": "Q3", "1231": "Q4"}
            for c, v in singles:
                out["quarters"].append(c[2:4] + qmap.get(c[4:], c[4:]))
                out["q_revenue"].append(_r(v / YI, 1) if v is not None else None)

    if not out["years"] and not out["quarters"]:
        return None
    return out


# --------------------------------------------------------------------------
#  4) 现金流 + 自动要点
# --------------------------------------------------------------------------
def _cashflow(code: str, net_by_year: dict | None = None) -> dict | None:
    ak = ds._ak()
    df = _safe(ak.stock_financial_report_sina, stock=ds._sina_symbol(code), symbol="现金流量表")
    if not isinstance(df, pd.DataFrame) or df.empty or "报告日" not in df.columns:
        return None
    df = df.copy()
    df["报告日"] = df["报告日"].astype(str)
    ann = df[df["报告日"].str.endswith("1231")].copy()
    if ann.empty:
        return None
    ann = ann.sort_values("报告日").tail(5)

    def col(cands):
        c = ds.pick_col(ann, cands, contains=True)
        return c

    c_ocf = col(["经营活动产生的现金流量净额"])
    c_icf = col(["投资活动产生的现金流量净额"])
    c_fin = col(["筹资活动产生的现金流量净额"])
    c_capex = col(["购建固定资产、无形资产和其他长期资产所支付的现金"])
    c_acq = col(["取得子公司及其他营业单位支付的现金净额"])
    c_div = col(["分配股利、利润或偿付利息所支付的现金"])
    c_diss = col(["取得借款收到的现金"])
    c_drep = col(["偿还债务支付的现金"])
    c_audit = col(["是否审计"])

    out = {"years": [], "ocf": [], "icf": [], "fcf": [], "capex": [], "acq": [],
           "dividend": [], "debt_iss": [], "debt_rep": [], "net_income": [], "audit": None}

    def g(row, c):
        return _num(row[c]) if (c and c in ann.columns and pd.notna(row[c])) else None

    for _, r in ann.iterrows():
        yr = r["报告日"][:4]
        out["years"].append(yr)
        ocf = g(r, c_ocf)
        capex_pos = g(r, c_capex)                        # 正=支付
        acq_pos = g(r, c_acq)
        div_pos = g(r, c_div)
        diss = g(r, c_diss)
        drep_pos = g(r, c_drep)
        toY = lambda v: _r(v / YI, 2) if v is not None else None
        out["ocf"].append(toY(ocf))
        out["icf"].append(toY(g(r, c_icf)))
        out["capex"].append(toY(-capex_pos) if capex_pos is not None else None)   # 流出为负
        out["acq"].append(toY(-acq_pos) if acq_pos is not None else None)
        out["dividend"].append(toY(-div_pos) if div_pos is not None else None)
        out["debt_iss"].append(toY(diss))
        out["debt_rep"].append(toY(-drep_pos) if drep_pos is not None else None)
        fcf = (ocf - capex_pos) if (ocf is not None and capex_pos is not None) else None
        out["fcf"].append(toY(fcf))
        ni = (net_by_year or {}).get(yr)
        out["net_income"].append(_r(ni, 2) if ni is not None else None)
    if c_audit:
        av = ann.iloc[-1][c_audit]
        out["audit"] = str(av).strip() if pd.notna(av) else None

    # ---- 季度现金流(近8季): sina累计口径 -> 差分成单季, 供图表"季度"细化 ----
    try:
        d2 = df.copy()
        d2["_dt"] = pd.to_datetime(d2["报告日"], errors="coerce")
        d2 = d2.dropna(subset=["_dt"]).sort_values("_dt")

        def _cum(c):
            m = {}
            if c and c in d2.columns:
                for _, r in d2.iterrows():
                    v = _num(r[c])
                    if v is not None:
                        m[pd.Timestamp(r["_dt"])] = v
            return m
        maps = {"ocf": _cum(c_ocf), "capex": _cum(c_capex),
                "acq": _cum(c_acq), "dividend": _cum(c_div)}

        def _single(m, ts):
            cur = m.get(ts)
            if cur is None:
                return None
            if ts.month == 3:
                return cur                       # Q1 累计=单季
            prevm = {6: 3, 9: 6, 12: 9}.get(ts.month)
            for x in m:
                if x.year == ts.year and x.month == prevm:
                    return cur - m[x]            # 本期累计 - 同年上一季累计
            return None
        qdts = sorted({pd.Timestamp(x) for x in d2["_dt"]})[-8:]
        toY = lambda v: _r(v / YI, 2) if v is not None else None
        if qdts:
            out["q_years"] = [t.strftime("%y") + "Q" + str((t.month - 1) // 3 + 1) for t in qdts]
            so = [_single(maps["ocf"], t) for t in qdts]
            sc = [_single(maps["capex"], t) for t in qdts]
            out["q_ocf"] = [toY(v) for v in so]
            out["q_capex"] = [toY(-v) if v is not None else None for v in sc]
            out["q_acq"] = [toY(-_single(maps["acq"], t)) if _single(maps["acq"], t) is not None else None for t in qdts]
            out["q_dividend"] = [toY(-_single(maps["dividend"], t)) if _single(maps["dividend"], t) is not None else None for t in qdts]
            out["q_fcf"] = [toY(so[i] - sc[i]) if (so[i] is not None and sc[i] is not None) else None for i in range(len(qdts))]
            out["q_net_income"] = [None] * len(qdts)   # 单季归母净利无免费源
    except Exception as e:
        log.debug("cashflow quarterly %s: %s", code, e)
    return out


def _cash_insights(cf: dict) -> list:
    """规则化生成"钱流向/漏洞"中文+英文要点。单位: 亿元。level: good/warn/info"""
    notes = []
    if not cf or not cf.get("years"):
        return notes
    years = cf["years"]

    def v(k, i):
        arr = cf.get(k) or []
        return arr[i] if (i is not None and i < len(arr)) else None

    ocf, i = _last(cf.get("ocf") or [])
    if i is None:
        return notes
    yr = years[i]
    ni = v("net_income", i)
    fcf = v("fcf", i)
    capex = v("capex", i)
    acq = v("acq", i)
    dividend = v("dividend", i)
    diss = v("debt_iss", i)
    drep = v("debt_rep", i)
    debt_net = None
    if diss is not None or drep is not None:
        debt_net = (diss or 0) + (drep or 0)     # drep 已为负

    def add(level, zh, en):
        notes.append({"level": level, "text": zh, "text_en": en})

    # 1) 盈利质量: 经营现金流 vs 归母净利润
    if ocf is not None and ni is not None and ni > 0:
        r = ocf / ni
        if r < 0.7:
            add("warn",
                f"{yr}年经营现金流仅为归母净利润的{r*100:.0f}% —— 利润未充分转化为现金(应收/存货占款?), 盈利质量需警惕",
                f"FY{yr} operating cash flow is only {r*100:.0f}% of net profit — profit isn't converting to cash (receivables/inventory tie-up?); earnings quality is a concern")
        elif r > 1.2:
            add("good",
                f"{yr}年经营现金流为归母净利润的{r*100:.0f}% —— 利润含金量高, 现金流扎实",
                f"FY{yr} operating cash flow is {r*100:.0f}% of net profit — high-quality, cash-backed earnings")
    if ni is not None and ni < 0 and ocf is not None and ocf > 0:
        add("info",
            f"{yr}年账面亏损但经营现金流为正({ocf:.1f}亿) —— 亏损或主要来自非现金项目(摊销/减值)",
            f"FY{yr} shows a book loss but positive operating cash flow ({ocf:.1f}00M CNY) — the loss likely stems mainly from non-cash items")

    # 2) 自由现金流
    if fcf is not None and fcf < 0:
        add("warn",
            f"{yr}年自由现金流为负({fcf:.1f}亿) —— 经营造血不足以覆盖资本开支, 需外部融资",
            f"FY{yr} free cash flow is negative ({fcf:.1f}00M CNY) — operations don't cover capex; external financing needed")

    # 3) 大额收购 (现金去向: 并购)
    if acq is not None and acq < 0 and ocf and ocf > 0 and abs(acq) >= 0.3 * ocf:
        add("warn",
            f"{yr}年收购/并表支出{abs(acq):.1f}亿, 相当于经营现金流的{abs(acq)/ocf*100:.0f}% —— 关注商誉与整合风险",
            f"FY{yr} spent {abs(acq):.1f}00M CNY on acquisitions, ~{abs(acq)/ocf*100:.0f}% of operating cash flow — watch goodwill and integration risk")

    # 4) 分红 vs 造血能力
    if dividend is not None and abs(dividend) > 0 and fcf is not None:
        ret = abs(dividend)
        if fcf > 0 and ret > fcf and (debt_net or 0) > 0:
            add("warn",
                f"{yr}年分红({ret:.1f}亿)超过自由现金流({fcf:.1f}亿)且当年净举债 —— 借钱分红, 持续性存疑",
                f"FY{yr} dividends ({ret:.1f}00M CNY) exceed free cash flow ({fcf:.1f}00M) with net new borrowing — funding payouts with debt, sustainability in doubt")
        elif fcf > 0 and ret > 0.9 * fcf:
            add("info",
                f"{yr}年分红({ret:.1f}亿)几乎用尽自由现金流 —— 留给扩张/还债的余地小",
                f"FY{yr} dividends ({ret:.1f}00M CNY) nearly exhaust free cash flow — little left for expansion/debt paydown")
        elif fcf > 0:
            add("good",
                f"{yr}年分红{ret:.1f}亿, 自由现金流({fcf:.1f}亿)覆盖充分",
                f"FY{yr} paid {ret:.1f}00M CNY dividends, well covered by free cash flow ({fcf:.1f}00M)")

    # 5) 资本开支强度
    if capex is not None and ocf and ocf > 0 and abs(capex) > 0.8 * ocf:
        add("info",
            f"{yr}年资本开支{abs(capex):.1f}亿, 占经营现金流的{abs(capex)/ocf*100:.0f}% —— 重资产扩张期, 关注投产回报",
            f"FY{yr} capex {abs(capex):.1f}00M CNY is {abs(capex)/ocf*100:.0f}% of operating cash flow — capital-intensive expansion; watch returns")
    return notes


# --------------------------------------------------------------------------
#  5) 风险 (偿债/毛利/ROE/审计)
# --------------------------------------------------------------------------
def _risk(code: str, audit: str | None) -> dict | None:
    ak = ds._ak()
    out = {"debt_ratio": None, "current_ratio": None, "quick_ratio": None,
           "gross_margin": None, "roe": None, "audit": audit}
    df = _safe(ak.stock_financial_abstract_ths, symbol=code, indicator="按年度")
    if isinstance(df, pd.DataFrame) and not df.empty and "报告期" in df.columns:
        df = df.copy().sort_values("报告期")
        r = df.iloc[-1]
        out["debt_ratio"] = _parse_cn_num(r.get("资产负债率"))
        out["current_ratio"] = _parse_cn_num(r.get("流动比率"))
        out["quick_ratio"] = _parse_cn_num(r.get("速动比率"))
        out["gross_margin"] = _parse_cn_num(r.get("销售毛利率"))
        out["roe"] = _parse_cn_num(r.get("净资产收益率"))
    if all(out[k] is None for k in ("debt_ratio", "current_ratio", "gross_margin", "roe")) and not audit:
        return None
    return out


# --------------------------------------------------------------------------
#  7) 新闻 (利好/利空粗分类)
# --------------------------------------------------------------------------
_POS_KW = ["中标", "签约", "订单", "增长", "涨价", "获批", "过会", "回购", "增持", "创新高",
           "超预期", "扭亏", "盈利", "分红", "合作", "突破", "量产", "投产", "扩产", "新高",
           "利好", "提价", "供不应求", "满产", "净利润增", "营收增", "龙头", "受益"]
_NEG_KW = ["亏损", "下滑", "减持", "质押", "违规", "处罚", "罚款", "立案", "问询", "退市",
           "商誉减值", "减值", "诉讼", "被查", "预亏", "下调", "解禁", "爆雷", "停产",
           "利空", "风险警示", "*ST", "债务", "逾期", "跌停", "警示函", "监管"]


def _news(code: str, top: int = 12) -> list:
    ak = ds._ak()
    df = _safe(ak.stock_news_em, symbol=code)
    if not isinstance(df, pd.DataFrame) or df.empty:
        return []
    out = []
    for _, r in df.head(top).iterrows():
        g = lambda k: (str(r[k]).strip() if k in df.columns and pd.notna(r[k]) else "")
        title = g("新闻标题")
        if not title:
            continue
        text = title + " " + g("新闻内容")
        pos = sum(1 for k in _POS_KW if k in text)
        neg = sum(1 for k in _NEG_KW if k in text)
        tone = "利好" if pos > neg else ("利空" if neg > pos else "中性")
        out.append({"tone": tone, "title": title, "url": g("新闻链接"),
                    "publisher": g("文章来源"), "time": g("发布时间")})
    return out


# --------------------------------------------------------------------------
#  7b) 融资融券 (多空博弈 —— A股"期权博弈"代理)
# --------------------------------------------------------------------------
def _margin(code: str) -> dict | None:
    """取最近一个可得交易日的两融快照。SH 用 sse, SZ 用 szse; 北交所无两融。"""
    ak = ds._ak()
    sym = ds._sina_symbol(code)
    if sym.startswith("bj"):
        return None
    is_sh = sym.startswith("sh")
    today = dt.date.today()
    mkt = "sse" if is_sh else "szse"
    for back in range(0, 8):                      # 回溯至多8天找已发布数据
        d = (today - dt.timedelta(days=back)).strftime("%Y%m%d")
        fn = ak.stock_margin_detail_sse if is_sh else ak.stock_margin_detail_szse
        df = _memo(f"margin_{mkt}_{d}", lambda: _safe(fn, date=d))  # 全市场文件, 每(市场,日)只下一次
        if not isinstance(df, pd.DataFrame) or df.empty:
            continue
        ccol = ds.pick_col(df, ["证券代码", "标的证券代码"], contains=True)
        if not ccol:
            continue
        df["_c"] = df[ccol].astype(str).str.zfill(6)
        row = df[df["_c"] == str(code).zfill(6)]
        if row.empty:
            continue
        r = row.iloc[0]
        def gv(cands, unit=YI):
            c = ds.pick_col(df, cands, contains=True)
            v = _num(r[c]) if (c and pd.notna(r[c])) else None
            return _r(v / unit, 2) if v is not None else None
        return {"date": d,
                "fin_bal": gv(["融资余额"]),
                "fin_buy": gv(["融资买入额"]),
                "short_bal": gv(["融券余额"]),
                "short_vol": gv(["融券余量"], unit=WAN),
                "total_bal": gv(["融资融券余额"])}
    return None


# --------------------------------------------------------------------------
#  7c) 龙虎榜 (游资/机构席位)
# --------------------------------------------------------------------------
def _lhb(code: str, days: int = 60) -> dict | None:
    ak = ds._ak()
    today = dt.date.today()
    start = (today - dt.timedelta(days=days)).strftime("%Y%m%d")
    end = today.strftime("%Y%m%d")
    df = _memo(f"lhb_{start}_{end}",
               lambda: _safe(ak.stock_lhb_detail_em, start_date=start, end_date=end))
    if not isinstance(df, pd.DataFrame) or df.empty:
        return None
    ccol = ds.pick_col(df, ["代码", "证券代码"], contains=True)
    if not ccol:
        return None
    df = df.copy()
    df["_c"] = df[ccol].astype(str).str.zfill(6)
    sub = df[df["_c"] == str(code).zfill(6)]
    if sub.empty:
        return {"count": 0, "last_date": None, "net_buy": None, "reason": None}
    dcol = ds.pick_col(sub, ["上榜日", "交易日期", "日期"], contains=True)
    if dcol:
        sub = sub.sort_values(dcol)
    last = sub.iloc[-1]
    g = lambda k: (str(last[k]).strip() if k in sub.columns and pd.notna(last[k]) else None)
    nbcol = ds.pick_col(sub, ["龙虎榜净买额", "净买额"], contains=True)
    nb = _num(last[nbcol]) if nbcol and pd.notna(last[nbcol]) else None
    return {"count": int(len(sub)),
            "last_date": g(dcol) if dcol else None,
            "net_buy": _r(nb / YI, 2) if nb is not None else None,
            "reason": g("上榜原因") or g("解读")}


# --------------------------------------------------------------------------
#  8) 大宗交易 (暗池代理)
# --------------------------------------------------------------------------
def _block(code: str) -> dict | None:
    ak = ds._ak()
    out = {"count": 0, "premium_rate": None, "amt_total": None, "amt_over_float": None,
           "inst_buy": 0, "recent": []}
    # 汇总: 活跃个股统计(近三月) —— 全市场, memo
    hy = _memo("dzjy_hygtj_近三月", lambda: _safe(ak.stock_dzjy_hygtj, symbol="近三月"))
    if isinstance(hy, pd.DataFrame) and not hy.empty:
        ccol = ds.pick_col(hy, ["证券代码"], contains=True)
        if ccol:
            hy = hy.copy(); hy["_c"] = hy[ccol].astype(str).str.zfill(6)
            row = hy[hy["_c"] == str(code).zfill(6)]
            if not row.empty:
                r = row.iloc[0]
                gv = lambda cands: (_num(r[ds.pick_col(hy, cands, contains=True)])
                                    if ds.pick_col(hy, cands, contains=True) else None)
                out["count"] = int(gv(["上榜次数-总计"]) or 0)
                pr = gv(["折溢率"])
                out["premium_rate"] = _r(pr * 100, 2) if (pr is not None and abs(pr) <= 1.5) else _r(pr, 2)
                at = gv(["总成交额"])
                out["amt_total"] = _r(at / WAN, 2) if at is not None else None   # 总成交额单位万元 -> 亿元
                ov = gv(["成交总额/流通市值"])
                out["amt_over_float"] = _r(ov * 100, 2) if (ov is not None and abs(ov) <= 5) else _r(ov, 2)
    # 明细: 近一个月每日明细, 过滤本股
    today = dt.date.today()
    start = (today - dt.timedelta(days=35)).strftime("%Y%m%d")
    end = today.strftime("%Y%m%d")
    mx = _memo(f"dzjy_mrmx_{start}_{end}",
               lambda: _safe(ak.stock_dzjy_mrmx, symbol="A股", start_date=start, end_date=end))
    if isinstance(mx, pd.DataFrame) and not mx.empty:
        ccol = ds.pick_col(mx, ["证券代码"], contains=True)
        if ccol:
            mx = mx.copy(); mx["_c"] = mx[ccol].astype(str).str.zfill(6)
            sub = mx[mx["_c"] == str(code).zfill(6)]
            dcol = ds.pick_col(sub, ["交易日期"], contains=True)
            if dcol is not None and not sub.empty:
                sub = sub.sort_values(dcol).tail(8).iloc[::-1]
            for _, r in sub.iterrows():
                g = lambda k: (str(r[k]).strip() if k in sub.columns and pd.notna(r[k]) else None)
                prem = _num(r["折溢率"]) if "折溢率" in sub.columns else None
                amt = _num(r["成交额"]) if "成交额" in sub.columns else None
                buyer = g("买方营业部")
                if buyer and "机构专用" in buyer:
                    out["inst_buy"] += 1
                out["recent"].append({
                    "date": g("交易日期"), "price": _num(r["成交价"]) if "成交价" in sub.columns else None,
                    "premium": _r(prem * 100, 2) if (prem is not None and abs(prem) <= 1.5) else _r(prem, 2),
                    "amt": _r(amt / WAN, 1) if amt is not None else None,   # 万元
                    "buyer": buyer, "seller": g("卖方营业部")})
    if out["count"] == 0 and not out["recent"]:
        return None
    return out


# --------------------------------------------------------------------------
#  组装
# --------------------------------------------------------------------------
def pull_profile(code: str, sector: str | None = None) -> dict:
    p = {"summary": None, "business": None, "sector": sector, "website": None,
         "hq": None, "legal_rep": None, "list_date": None, "found_date": None,
         "reg_capital": None, "mgmt_changes": [], "segments": None, "revenue": None,
         "cashflow": None, "cash_notes": [], "risk": None, "news": [],
         "margin": None, "lhb": None, "block": None}

    basic = _profile_basic(code)
    p.update({k: basic.get(k) for k in basic})
    if not p.get("sector"):
        p["sector"] = sector
    elif sector:                       # 优先流水线传入的行业(与主表一致)
        p["sector"] = sector or p["sector"]

    p["mgmt_changes"] = _mgmt_changes(code)
    p["segments"] = _segments(code)
    p["revenue"] = _revenue_trend(code)

    net_by_year = {}
    if p["revenue"] and p["revenue"].get("years"):
        for y, nv in zip(p["revenue"]["years"], p["revenue"].get("net") or []):
            if nv is not None:
                net_by_year[str(y)] = nv
    cf = _cashflow(code, net_by_year)
    p["cashflow"] = cf
    p["cash_notes"] = _cash_insights(cf or {})
    p["risk"] = _risk(code, (cf or {}).get("audit"))
    p["news"] = _news(code)
    p["margin"] = _margin(code)
    p["lhb"] = _lhb(code)
    p["block"] = _block(code)
    return p
