#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
模块7 — 信号回测 (Signal Backtest)
==================================
把历史每日快照里"当天发出的买卖点建议"与其后真实行情对照, 统计各类信号
(标签 × 成长质量 × 入场方式 × 市场温度)的真实触发率/胜率/期望收益, 并据此
给今天的候选打"历史同类信号胜率"标注, 生成回测优选榜。

原则 (多智能体对抗评审后收敛的口径):
  * 只用信号日当天快照里已有的字段重建交易计划 (无未来函数);
  * 锚定bar靠"快照价==该bar收盘价"匹配, 不信任快照标注的日期 —— 美股快照
    在北京时间早晨生成, 标注日与真实数据日差一个交易日, 直接按日期锚定会把
    "第二天的涨跌"泄漏进所有价位 (评审抓出的关键前视偏差);
  * 胜率只统计"观察窗完整走完"的信号 (fill后满 HORIZON 根bar)。已了结但窗口
    未满的样本一并剔除, 否则"快出结果的交易"会被优先计入, 胜率被截尾偏差推高;
  * 成交日回落破止损按止损位成交 (挂着的止损单), 不许按成交价记零损失;
    成交日不记目标达成 (bar内先后次序不可知, 保守);
  * 开盘已破止损 -> 计划失效不进场 (不接飞刀); 突破追高不超过计划买入带上限;
  * 同日目标/止损双触发按"先止损"保守处理, 与产品自身的事件回测口径一致;
  * 同一股票同一时间只允许一个信号事件, 结束+冷却后才能再开;
  * 已知局限 (记录在案, 未建模): 信号按日聚集、彼此相关, 分段胜率的有效样本
    小于名义 n; 拿不到价格序列的退市/停牌股 (约2%) 未计入; bar内先后次序按
    保守约定近似。样本仅覆盖近几周的单一市场环境。
"""
from __future__ import annotations
import datetime as dt
import glob
import json
import logging
import os
import sqlite3

import numpy as np

from .config import DASHBOARD_DIR, DATA_DIR, DB_PATH

log = logging.getLogger("ashare.backtest")

HISTORY_DIR = os.path.join(DASHBOARD_DIR, "history")
BT_JS = os.path.join(DASHBOARD_DIR, "backtest_data.js")
BT_JSON = os.path.join(DATA_DIR, "backtest_result.json")

# ---- 市场规则开关 -----------------------------------------------------------
T_PLUS_ONE = True          # A股 T+1: 成交当日不可卖出
LIMIT_BOARDS = True        # A股涨跌停: 一字涨停买不进 / 一字跌停卖不出

# ---- 交易规则参数 -----------------------------------------------------------
ENTRY_VALID_BARS = 10      # 回踩买入的等待窗口 (交易日); 超时未触及 -> 未成交
BREAKOUT_VALID_BARS = 15   # 突破买入的等待窗口
HORIZON_BARS = 20          # 回测判定窗口: fill后20个交易日 (产品计划的60日窗
                           # 在当前几周历史下必然截尾, 20日窗才能有"完整样本")
HEADLINE_GAIN = 0.10       # 主胜率口径: 止损前先到 +10%
SOFT_GAIN = 0.05           # 辅助口径: 最大浮盈曾达 +5%
COST_RT = 0.003            # 往返成本 (佣金双边+卖出印花税+滑点), A股约 0.3%
COOLDOWN_DAYS = 5          # 事件结束后同一股票再开新事件的冷却 (自然日)
MIN_STOP, MAX_STOP = 0.05, 0.15   # 与 tradeplan 一致
SHRINK_K = 12.0            # 分段胜率向全池先验收缩的伪样本数
MIN_SEG_N = 12             # 段"完整窗口"样本数达标才有资格进推荐
FETCH_START_PAD_DAYS = 10  # 价格序列起点 = 最早快照日 - 该天数
ANCHOR_TOL_EXACT = 0.0025  # 锚定: 收盘价与快照价偏差 <=0.25% 视为同一天
ANCHOR_TOL_NEAR = 0.02     # 锚定: 兜底容差

# 成长质量 -> 归一化档位 (A股用三色标签)
GROWTH_TIER = {
    "🟢 可持续": "G",
    "🟡 待观察": "M",
    "🔴 一次性": "W",
}
TIER_LABEL = {"G": "🟢 可持续", "M": "🟡 待观察", "W": "🔴 一次性", "NA": "⚪ 无数据"}


# ===========================================================================
#  快照加载
# ===========================================================================
def load_snapshots() -> list[dict]:
    out = []
    for p in sorted(glob.glob(os.path.join(HISTORY_DIR, "day_*.json"))):
        try:
            j = json.load(open(p, encoding="utf-8"))
        except Exception as e:
            log.warning("快照 %s 读取失败: %s", p, e)
            continue
        meta = j.get("meta") or {}
        cands = j.get("candidates") or []
        if not cands:
            continue
        out.append({
            "run_date": meta.get("run_date") or os.path.basename(p)[4:14],
            "as_of": meta.get("data_date") or meta.get("run_date") or os.path.basename(p)[4:14],
            "opp_score": ((meta.get("opp") or {}).get("score")),
            "cands": cands,
        })
    return out


# ===========================================================================
#  价格序列: 主源 yfinance, 兜底 stock_detail 里存过的K线
# ===========================================================================
def _series_from_stock_detail(codes: set[str]) -> dict:
    """从 stock_detail 取每只股票最近一次存档的K线 (echarts [o,c,l,h]) 作兜底。"""
    out = {}
    try:
        conn = sqlite3.connect(DB_PATH)
        rows = conn.execute(
            "SELECT code, detail_json FROM stock_detail WHERE (code, run_date) IN "
            "(SELECT code, MAX(run_date) FROM stock_detail GROUP BY code)").fetchall()
        conn.close()
    except Exception as e:
        log.warning("stock_detail 兜底读取失败: %s", e)
        return out
    for code, dj in rows:
        if code not in codes:
            continue
        try:
            d = json.loads(dj)
            dates = d.get("dates") or []
            ohlc = d.get("ohlc") or []
            if len(dates) < 30 or len(dates) != len(ohlc):
                continue
            arr = np.array([[o, h, l, c] for (o, c, l, h) in ohlc], dtype=float)
            out[code] = {"dates": [str(x) for x in dates], "ohlc": arr}
        except Exception:
            continue
    return out


def _drop_partial_today() -> str | None:
    """若北京时间尚未收盘(15:05前), 返回今天的日期串 -> 丢弃当日未走完的bar。"""
    bj = dt.datetime.now(dt.timezone(dt.timedelta(hours=8)))
    if bj.hour < 15 or (bj.hour == 15 and bj.minute < 5):
        return bj.date().isoformat()
    return None


def fetch_price_series(codes: list[str], start: str) -> dict:
    """code -> {"dates":[...], "ohlc": ndarray[N,4] (o,h,l,c)}; 腾讯前复权日线。
    窗口只有几个月 (<640根), 单请求即可拿全, 不存在分段复权基准错位问题。
    盘中运行时丢弃今天未走完的bar, 避免半根K线参与判定。"""
    from concurrent.futures import ThreadPoolExecutor
    from . import datasource as ds
    today = dt.date.today().isoformat()
    skip_day = _drop_partial_today()
    res = {}

    def one(code):
        try:
            kl = ds.call_with_retry(ds._tencent_chunk, ds._tencent_symbol(code),
                                    start, today)
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
    missing = set(codes) - set(res)
    if missing:
        fb = _series_from_stock_detail(missing)
        # 兜底序列同样丢弃未收盘的当日bar
        if skip_day:
            for code, ser in list(fb.items()):
                keep = [k for k, d0 in enumerate(ser["dates"]) if d0 < skip_day]
                if len(keep) < 5:
                    fb.pop(code)
                else:
                    fb[code] = {"dates": [ser["dates"][k] for k in keep],
                                "ohlc": ser["ohlc"][keep]}
        res.update(fb)
        log.info("stock_detail 兜底补了 %d 只 (仍缺 %d)", len(fb), len(missing) - len(fb))
    return res


# ===========================================================================
#  计划重建 (与 tradeplan.build_trade_plan 同公式; 有存档 plan 优先用存档)
# ===========================================================================
def reconstruct_plan(c: dict) -> dict | None:
    px = c.get("price")
    if not px or px <= 0:
        return None
    stored = c.get("plan") if isinstance(c.get("plan"), dict) else None
    atr_pct = c.get("atr_pct") or 3.0
    a = float(np.clip(atr_pct / 100.0, 0.008, 0.08))

    box_hi, box_lo = c.get("box_hi"), c.get("box_lo")
    if c.get("coil") and box_hi and box_lo and 0 < box_lo < box_hi:
        ref = float(box_hi)
        stop = max(float(box_lo) * 0.995, ref * (1.0 - MAX_STOP))
        if stored and stored.get("entry_mode") == "breakout":
            ref = float(stored.get("entry_ref") or ref)
            stop = float(stored.get("stop_price") or stop)
        return {"kind": "breakout", "entry_ref": ref, "entry_high": ref * (1.0 + 0.5 * a),
                "stop": stop, "box_lo": float(box_lo)}

    support = c.get("support_price")
    if support and support > 0 and px >= support * 0.985:
        ref, mode = float(support), "support"
    elif support and support > 0:
        ref, mode = float(px), "market"
    else:
        ref, mode = float(px), "none"
    entry_low, entry_high = ref * (1.0 - 0.4 * a), ref * (1.0 + 0.5 * a)
    sret = float(np.clip(1.8 * a, MIN_STOP, MAX_STOP))
    stop = ref * (1.0 - sret)
    bp = c.get("breakdown_price")
    if bp and 0 < bp < ref:
        stop = min(stop, float(bp) * 0.995)
    stop = max(stop, ref * (1.0 - MAX_STOP))
    if stored and stored.get("entry_mode") in ("support", "market", "none"):
        mode = stored["entry_mode"]
        entry_low = float(stored.get("entry_low") or entry_low)
        entry_high = float(stored.get("entry_high") or entry_high)
        stop = float(stored.get("stop_price") or stop)
        ref = float(stored.get("entry_ref") or ref)
    return {"kind": "pullback", "mode": mode, "entry_ref": ref,
            "entry_low": entry_low, "entry_high": entry_high, "stop": stop}


# ===========================================================================
#  涨跌停判定: 全天几乎无振幅 + 涨跌幅接近主板停板 -> 视作一字封死
#  (20cm板的10%一字理论可交易, 但按封死保守处理; 只拦真封死情形)
# ===========================================================================
def _limit_up_oneline(o, h, l, c, prev_c):
    if prev_c is None or prev_c <= 0 or c <= 0:
        return False
    return (h - l) < 0.002 * c and c >= prev_c * 1.085


def _limit_down_oneline(o, h, l, c, prev_c):
    if prev_c is None or prev_c <= 0 or c <= 0:
        return False
    return (h - l) < 0.002 * c and c <= prev_c * 0.915


# ===========================================================================
#  锚定: 用"快照价 == 某bar收盘价"找信号真实数据日 (防错标日期泄漏未来)
# ===========================================================================
def find_anchor(closes: np.ndarray, idx0: int, snap_px: float) -> int | None:
    """从 idx0 (最后一根日期<=标注as_of的bar) 往前找收盘价与快照价吻合的bar。
    优先取容差0.25%内最近的; 否则2%内最接近的; 都没有 -> 用 idx0 兜底。"""
    lo = max(0, idx0 - 6)
    best, best_d = None, 1e9
    for i in range(idx0, lo - 1, -1):
        cv = closes[i]
        if cv <= 0:
            continue
        d = abs(cv / snap_px - 1.0)
        if d <= ANCHOR_TOL_EXACT:
            return i
        if d < best_d:
            best, best_d = i, d
    if best is not None and best_d <= ANCHOR_TOL_NEAR:
        return best
    return idx0


# ===========================================================================
#  单事件模拟
# ===========================================================================
def simulate(plan: dict, dates: list[str], ohlc: np.ndarray, start_idx: int,
             scale: float) -> dict:
    """从 start_idx (信号数据日后第一根bar) 起模拟。价位按 scale 缩放对齐复权序列。
    同日双触发按先止损; 窗口完整走完才有资格进胜率统计 (complete 标志)。"""
    ref = plan["entry_ref"] * scale
    stop = plan["stop"] * scale
    ehigh = plan["entry_high"] * scale
    kind = plan["kind"]
    n = len(dates)
    valid = BREAKOUT_VALID_BARS if kind == "breakout" else ENTRY_VALID_BARS

    fill_i, fill_px = None, None
    i = start_idx
    while i < n and i < start_idx + valid:
        o, h, l, c = ohlc[i]
        prev_c = ohlc[i - 1][3] if i > 0 else o
        sealed_up = LIMIT_BOARDS and _limit_up_oneline(o, h, l, c, prev_c)
        if kind == "breakout":
            px = max(o, ref)
            if h >= ref and not sealed_up and px <= ehigh:
                fill_i, fill_px = i, px           # 突破且未超计划追高带
                break
            if l <= plan["box_lo"] * scale:       # (未成交前提下)破位 -> 剧本失效
                return {"status": "box_broke", "end_i": i}
        else:
            if o <= stop:                          # 开盘已破止损: 不接飞刀
                return {"status": "gap_invalid", "end_i": i}
            if plan.get("mode") in ("market", "none"):
                if not sealed_up:
                    fill_i, fill_px = i, o
                    break
            elif l <= ehigh:                       # 回踩进入买入区 (限价单口径)
                fill_i, fill_px = i, min(o, ehigh)
                break
        i += 1
    if fill_i is None:
        return {"status": "no_fill" if i >= start_idx + valid else "pending",
                "end_i": min(i, n - 1)}

    tgt = fill_px * (1.0 + HEADLINE_GAIN)
    end = min(fill_i + HORIZON_BARS, n - 1)
    complete = (fill_i + HORIZON_BARS) <= (n - 1)
    status = exit_i = exit_px = None

    # 成交当日: 只判止损 (止损单挂在stop位), 不判目标 (bar内次序不可知);
    # A股 T+1 当日不可卖, 连止损也顺延到次日起判。
    if not T_PLUS_ONE:
        o, h, l, c = ohlc[fill_i]
        if l <= stop:
            status, exit_i = "stopped", fill_i
            exit_px = stop if fill_px > stop else c
    if status is None:
        j = fill_i + 1
        while j <= end:
            o, h, l, c = ohlc[j]
            prev_c = ohlc[j - 1][3]
            if l <= stop:
                if (LIMIT_BOARDS and _limit_down_oneline(o, h, l, c, prev_c)
                        and j < n - 1):
                    # 一字跌停卖不出 -> 次日开盘才能离场
                    status, exit_i, exit_px = "stopped", j + 1, ohlc[j + 1][0]
                else:
                    status, exit_i, exit_px = "stopped", j, min(o, stop)
                break
            if h >= tgt:
                status, exit_i, exit_px = "won", j, (o if o > tgt else tgt)
                break
            j += 1
        else:
            if complete:
                status, exit_i, exit_px = "expired", end, ohlc[end][3]
            else:
                status, exit_i, exit_px = "open", n - 1, ohlc[n - 1][3]

    # 浮盈/回撤只算持仓期内 (成交日与止损离场日的极值可能发生在持仓之外, 剔除)
    seg = ohlc[fill_i + 1:exit_i]
    max_h = float(np.max(seg[:, 1])) if len(seg) else fill_px
    min_l = float(np.min(seg[:, 2])) if len(seg) else fill_px
    if status in ("won", "expired", "open"):
        max_h = max(max_h, ohlc[exit_i][1])
        min_l = min(min_l, ohlc[exit_i][2])
    max_h = max(max_h, fill_px)
    min_l = min(min_l, min(fill_px, exit_px))

    ret = exit_px / fill_px - 1.0
    ret -= COST_RT if status != "open" else COST_RT / 2.0   # 持仓中也已付了买入侧成本
    return {"status": status, "fill_i": fill_i, "fill_px": fill_px,
            "fill_date": dates[fill_i], "exit_i": exit_i, "exit_px": exit_px,
            "exit_date": dates[exit_i], "ret": ret,
            "days": exit_i - fill_i, "complete": bool(complete),
            "max_gain": max_h / fill_px - 1.0, "max_dd": min_l / fill_px - 1.0,
            "end_i": exit_i}


# ===========================================================================
#  事件流构建 + 汇总
# ===========================================================================
def _opp_bucket(s):
    if s is None:
        return "na"
    return "cold" if s < 40 else ("hot" if s >= 60 else "mid")


def build_and_run(snaps: list[dict], prices: dict) -> list[dict]:
    episodes = []
    busy_until = {}          # code -> date str, 该日期(含冷却)前不开新事件
    for snap in snaps:
        as_of = snap["as_of"]
        for c in snap["cands"]:
            code = c.get("code")
            if not code or code not in prices:
                continue
            if code in busy_until and as_of <= busy_until[code]:
                continue
            plan = reconstruct_plan(c)
            if plan is None:
                continue
            ser = prices[code]
            dates, ohlc = ser["dates"], ser["ohlc"]
            idx0 = int(np.searchsorted(np.array(dates), as_of, side="right")) - 1
            if idx0 < 0:
                continue
            snap_px = c.get("price")
            if not snap_px or snap_px <= 0:
                continue
            # 锚定bar = 快照价真正来自的那根bar (防标注日错位泄漏次日行情)
            anchor = find_anchor(ohlc[:, 3], idx0, float(snap_px))
            if anchor is None or anchor + 1 >= len(dates):
                continue
            if ohlc[anchor][3] <= 0:
                continue
            scale = float(ohlc[anchor][3]) / float(snap_px)
            if not (0.2 < scale < 5.0):
                continue
            r = simulate(plan, dates, ohlc, anchor + 1, scale)
            gt = GROWTH_TIER.get(c.get("growth_quality"), "NA")
            ep = {
                "code": code, "name": c.get("name"), "sig_date": as_of,
                "tag": (c.get("tag") or "").strip(), "growth": gt,
                "kind": plan["kind"], "mode": plan.get("mode", "breakout"),
                "final_score": c.get("final_score"), "fund_score": c.get("fund_score"),
                "opp": _opp_bucket(snap.get("opp_score")),
                "industry": c.get("industry"),
                "cuosha": ("cs" if c.get("cuosha_score")
                           else ("elig" if c.get("cuosha_eligible") else "other")),
                **{k: r.get(k) for k in ("status", "fill_date", "fill_px", "exit_date",
                                          "exit_px", "ret", "days", "complete",
                                          "max_gain", "max_dd")},
            }
            episodes.append(ep)
            busy_end = r.get("end_i")
            if busy_end is not None:
                end_date = prices[code]["dates"][busy_end]
                cd = (dt.date.fromisoformat(end_date) + dt.timedelta(days=COOLDOWN_DAYS))
                busy_until[code] = cd.isoformat()
            else:
                busy_until[code] = "9999-12-31"
    return episodes


RESOLVED = ("won", "stopped", "expired")
UNFILLED = ("no_fill", "box_broke", "gap_invalid")


def _stats_pool(eps: list[dict]) -> list[dict]:
    """进胜率统计的样本 = 已了结 且 观察窗完整 (剔除截尾偏差)。"""
    return [e for e in eps if e["status"] in RESOLVED and e.get("complete")]


def _seg_stats(eps: list[dict], p0: float) -> dict:
    fills = [e for e in eps if e["status"] in RESOLVED + ("open",)]
    res = _stats_pool(eps)
    n_sig = sum(1 for e in eps if e["status"] != "pending")
    won = sum(1 for e in res if e["status"] == "won")
    soft = sum(1 for e in res if (e.get("max_gain") or 0) >= SOFT_GAIN)
    rets = [e["ret"] for e in res if e.get("ret") is not None]
    days = [e["days"] for e in res if e.get("days") is not None]
    win = won / len(res) if res else None
    return {
        "n_signals": n_sig, "n_filled": len(fills), "n_resolved": len(res),
        "n_open": len(fills) - len(res),      # 持仓中 + 窗口未满不计入统计的
        "fill_rate": round(len(fills) / n_sig, 3) if n_sig else None,
        "win10": round(win, 3) if win is not None else None,
        "win10_post": round((won + SHRINK_K * p0) / (len(res) + SHRINK_K), 3) if res else None,
        "reach5": round(soft / len(res), 3) if res else None,
        "avg_ret": round(float(np.mean(rets)), 4) if rets else None,
        "med_days": int(np.median(days)) if days else None,
    }


def aggregate(episodes: list[dict]) -> dict:
    res_all = _stats_pool(episodes)
    p0 = (sum(1 for e in res_all if e["status"] == "won") / len(res_all)) if res_all else 0.4
    out = {"pool": _seg_stats(episodes, p0), "p0": round(p0, 3),
           "by_tag": {}, "by_growth": {}, "by_combo": {}, "by_mode": {}, "by_opp": {},
           "by_cuosha": {}}
    def _group(keyf):
        g = {}
        for e in episodes:
            g.setdefault(keyf(e), []).append(e)
        return g
    for k, eps in _group(lambda e: e["tag"] or "?").items():
        out["by_tag"][k] = _seg_stats(eps, p0)
    for k, eps in _group(lambda e: e["growth"]).items():
        out["by_growth"][k] = _seg_stats(eps, p0)
    for k, eps in _group(lambda e: f'{e["tag"]}|{e["growth"]}').items():
        s = _seg_stats(eps, p0)
        if s["n_signals"] >= 6:
            out["by_combo"][k] = s
    for k, eps in _group(lambda e: e["mode"]).items():
        out["by_mode"][k] = _seg_stats(eps, p0)
    for k, eps in _group(lambda e: e["opp"]).items():
        out["by_opp"][k] = _seg_stats(eps, p0)
    # 三段: 达标(cs) / 过门槛未达标(elig) / 其它 —— elig 组固定了准入门槛的
    # 选择效应(深回撤+基本面前40%), cs vs elig 才是对打分本身的检验
    for k, eps in _group(lambda e: e.get("cuosha") or "other").items():
        out["by_cuosha"][k] = _seg_stats(eps, p0)
    return out


def recommend(latest_cands: list[dict], agg: dict) -> tuple[list[dict], dict]:
    """给今天的候选贴历史同类段位战绩; 段位达标(完整样本够+收缩后胜率优于全池+
    期望为正)的入围, 再按(段位胜率, 今日综合分)取前20进推荐榜。
    返回 (推荐榜, code->段位战绩 映射, 供前端行内展示)。"""
    p0 = agg.get("p0") or 0.4
    recos, today_map = [], {}
    for c in latest_cands:
        gt = GROWTH_TIER.get(c.get("growth_quality"), "NA")
        combo = agg["by_combo"].get(f'{(c.get("tag") or "").strip()}|{gt}')
        # 细分段样本不足时退回标签级 (细分段小样本不许贴大胜率)
        if combo and combo["n_resolved"] >= MIN_SEG_N:
            seg, seg_kind = combo, "combo"
        else:
            seg, seg_kind = agg["by_tag"].get((c.get("tag") or "").strip()), "tag"
        if not seg:
            continue
        item = {"code": c["code"], "name": c.get("name"), "tag": c.get("tag"),
                "growth": gt, "seg_kind": seg_kind,
                "seg_n": seg["n_resolved"], "seg_win": seg["win10"],
                "seg_win_post": seg["win10_post"], "seg_ret": seg["avg_ret"],
                "seg_days": seg["med_days"]}
        today_map[c["code"]] = item
        ok = (seg["n_resolved"] >= MIN_SEG_N and seg["win10_post"] is not None
              and seg["win10_post"] >= max(0.55, p0 + 0.03)
              and (seg["avg_ret"] or 0) > 0)
        if ok:
            item = dict(item, fs=c.get("final_score") or 0)
            recos.append(item)
    # 段位达标只是入围; 榜单按 (段位收缩胜率, 今日综合分) 取前20, 避免"全场都是优选"
    recos.sort(key=lambda x: (-(x["seg_win_post"] or 0), -(x["fs"] or 0)))
    recos = recos[:20]
    for item in recos:
        item["reco"] = 1
        today_map[item["code"]] = item
    return recos, today_map


# ===========================================================================
#  入口
# ===========================================================================
def run_backtest(write_js: bool = True) -> dict | None:
    snaps = load_snapshots()
    if len(snaps) < 3:
        log.info("快照不足 3 天, 跳过回测")
        return None
    # 给每天的快照候选重算错杀分 (输入字段快照里都有), 供 by_cuosha 分段验证
    try:
        from . import cuosha
        for s in snaps:
            cuosha.annotate(s["cands"])
    except Exception as e:
        log.warning("错杀标注失败(回测继续): %s", e)
    codes = sorted({c["code"] for s in snaps for c in s["cands"] if c.get("code")})
    start = (dt.date.fromisoformat(snaps[0]["as_of"])
             - dt.timedelta(days=FETCH_START_PAD_DAYS)).isoformat()
    log.info("回测: %d 天快照, %d 只股票, 价格起点 %s", len(snaps), len(codes), start)
    prices = fetch_price_series(codes, start)
    log.info("价格覆盖 %d/%d", len(prices), len(codes))
    episodes = build_and_run(snaps, prices)
    agg = aggregate(episodes)
    latest = snaps[-1]["cands"]
    recos, today_map = recommend(latest, agg)

    by_code = {}
    for e in episodes:
        if e["status"] in RESOLVED:
            by_code.setdefault(e["code"], []).append(
                {"d": e["sig_date"], "s": e["status"], "ret": round((e["ret"] or 0) * 100, 1),
                 "days": e["days"]})
    recent = sorted([e for e in episodes if e["status"] in RESOLVED],
                    key=lambda x: x["exit_date"] or "", reverse=True)[:100]
    opens = [e for e in episodes if e["status"] == "open"]
    result = {
        "meta": {"generated": dt.datetime.now().strftime("%Y-%m-%d %H:%M"),
                 "n_days": len(snaps), "first_day": snaps[0]["run_date"],
                 "last_day": snaps[-1]["run_date"],
                 "n_codes": len(codes), "px_cover": len(prices),
                 "n_episodes": len(episodes),
                 "horizon": HORIZON_BARS, "headline_gain": HEADLINE_GAIN,
                 "cost_rt": COST_RT},
        "agg": agg, "recos": recos[:40], "today": today_map,
        "recent": [{k: e.get(k) for k in ("code", "name", "tag", "sig_date", "fill_date",
                                           "exit_date", "status", "ret", "days", "growth")}
                   for e in recent],
        "open": [{k: e.get(k) for k in ("code", "name", "tag", "sig_date", "fill_date",
                                         "status", "ret", "growth")} for e in opens][:80],
        "by_code": {k: v[-3:] for k, v in by_code.items()},
    }
    json.dump(result, open(BT_JSON, "w", encoding="utf-8"), ensure_ascii=False)
    if write_js:
        with open(BT_JS, "w", encoding="utf-8") as f:
            f.write("window.__BT__ = ")
            json.dump(result, f, ensure_ascii=False)
            f.write(";\n")
        log.info("回测导出: %s (episodes=%d, 完整窗口已了结=%d, 推荐=%d)", BT_JS,
                 len(episodes), agg["pool"]["n_resolved"], len(recos))
    return result


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    run_backtest()
