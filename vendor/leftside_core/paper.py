#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
自动模拟组合 (Auto paper portfolio)
====================================
每次数据更新时, 把四类榜单信号的"首次出现"自动按买卖点建议模拟买入固定预算
(美股 $10,000 / A股 ¥70,000), 之后每天用最新行情更新收益:

  quality  👑 优质公司      (无买卖点计划 -> 信号日收盘为参考价, 市价单次日成交)
  cuosha   💎 错杀候选      (榜单出现即注册, 用当日存档的买卖点计划)
  coil     🎯 蓄势待发      (突破单: 站上箱体上沿才成交, 不追高)
  dip      🕳 深跌抄底      (回踩限价单)

成交与离场规则**完全复用回测引擎** (reconstruct_plan + find_anchor + simulate):
突破/回踩/市价三种成交方式、开盘破止损不接飞刀、A股T+1与一字板、同日双触发
先止损、+10% 目标 / 止损 / 20个交易日观察窗到期离场、含交易成本。

设计: 状态文件只存"信号注册表" (哪天、哪一类、哪只票、当时的快照与计划),
每次更新对未了结信号用最新行情**重新模拟** —— 幂等, 无增量状态可错; 已了结
的结果缓存 (新bar不会改变已完结事件), 日常成本只有活跃持仓的行情。
同类信号同一只票 30 天冷却, 与回测口径一致。
"""
from __future__ import annotations
import datetime as dt
import glob
import json
import logging
import math
import os

import numpy as np

from .market import current
from . import backtest as bt

log = logging.getLogger("leftside_core.paper")

COOLDOWN_DAYS = 30          # 同一 (类别, 代码) 两次注册之间的最短间隔 (自然日)
FETCH_PAD_DAYS = 40         # 行情起点 = 最早未了结信号日 - 缓冲
CAND_KEYS = ("code", "name", "tag", "price", "atr_pct", "plan", "coil",
             "box_hi", "box_lo", "support_price", "breakdown_price", "cuosha_score")
FINAL_STATUSES = ("won", "stopped", "expired", "no_fill", "box_broke",
                  "gap_invalid", "too_expensive", "bad_anchor")
CATS = ("quality", "cuosha", "coil", "dip")


def _budget_and_lot() -> tuple[float, int, str]:
    if current().name == "ashare":
        return 70000.0, 100, "¥"       # ≈ $10,000, A股一手=100股
    return 10000.0, 1, "$"


def _paths() -> tuple[str, str, str]:
    m = current()
    return (os.path.join(m.dashboard_dir, "history"),
            os.path.join(m.data_dir, "paper_portfolio.json"),
            os.path.join(m.dashboard_dir, "paper_data.js"))


def _load_state(path: str) -> dict:
    try:
        with open(path, encoding="utf-8") as f:
            st = json.load(f)
        if isinstance(st.get("signals"), list):
            return st
    except Exception:
        pass
    return {"signals": [], "daily": []}


def _latest_signals() -> tuple[str | None, list[dict]]:
    """最新快照 + 最新优质榜 -> [(cat, sig_date, cand)] 候选注册项。
    sig_date 一律用快照的 data_date (真实行情日): 周末/盘前跑的流水线扫描日
    可能比数据日晚, 用扫描日会把成交时点错推一个bar。"""
    out = []
    as_of = None
    snaps = bt.load_snapshots()
    if snaps:
        snap = snaps[-1]
        as_of = snap["as_of"]
        for c in snap["cands"]:
            if not c.get("code"):
                continue
            tag = (c.get("tag") or "").strip()
            slim = {k: c.get(k) for k in CAND_KEYS if c.get(k) is not None}
            if c.get("cuosha_score"):
                out.append(("cuosha", as_of, slim))
            if "蓄势待发" in tag:                    # 标签带emoji前缀 ("🚀 蓄势待发")
                out.append(("coil", as_of, slim))
            elif "深跌抄底" in tag:
                out.append(("dip", as_of, slim))
    qs = sorted(glob.glob(os.path.join(_paths()[0], "quality_*.json")))
    if qs:
        try:
            j = json.load(open(qs[-1], encoding="utf-8"))
            qd = j.get("date") or os.path.basename(qs[-1])[8:18]
            if as_of and as_of <= qd:
                qd = as_of                 # 优质榜与快照同一次流水线 -> 对齐数据日
            for p in j.get("picks") or []:
                if p.get("code"):
                    out.append(("quality", qd,
                                {"code": p["code"], "name": p.get("name")}))
        except Exception as e:
            log.warning("读取优质榜历史失败: %s", e)
    return as_of, out


def _register(state: dict, new_sigs: list[tuple]) -> int:
    """冷却期外的新信号进注册表; 返回新增条数。"""
    last_by_key: dict[tuple, str] = {}
    for s in state["signals"]:
        k = (s["cat"], s["code"])
        if k not in last_by_key or s["sig_date"] > last_by_key[k]:
            last_by_key[k] = s["sig_date"]
    n = 0
    for cat, sig_date, cand in new_sigs:
        code = cand["code"]
        prev = last_by_key.get((cat, code))
        if prev:
            try:
                gap = (dt.date.fromisoformat(sig_date) - dt.date.fromisoformat(prev)).days
            except Exception:
                gap = 0
            if gap < COOLDOWN_DAYS:
                continue
        state["signals"].append({
            "id": f"{cat}:{code}:{sig_date}", "cat": cat, "code": code,
            "name": cand.get("name"), "sig_date": sig_date, "cand": cand,
        })
        last_by_key[(cat, code)] = sig_date
        n += 1
    return n


def _simulate_signal(sig: dict, ser: dict, budget: float, lot: int) -> dict:
    """一条注册信号 -> 用最新行情重新模拟; 返回展示/统计用结果 dict。"""
    dates, ohlc = ser["dates"], np.asarray(ser["ohlc"], dtype=float)
    res = {"status": "pending"}
    idx0 = int(np.searchsorted(np.array(dates), sig["sig_date"], side="right")) - 1
    if idx0 < 0 or idx0 + 1 >= len(dates):
        return res                                   # 信号日之后还没有新bar
    cand = dict(sig["cand"])
    if sig["cat"] == "quality":                      # 优质榜无快照价: 以锚bar收盘为参考, 序列即基准 (scale=1)
        anchor, scale = idx0, 1.0
        cand["price"] = float(ohlc[idx0][3])
        cand.pop("plan", None)
    else:
        snap_px = cand.get("price")
        if not snap_px or snap_px <= 0:
            return {"status": "bad_anchor"}
        anchor = bt.find_anchor(ohlc[:, 3], idx0, float(snap_px))
        if anchor is None or anchor + 1 >= len(dates) or ohlc[anchor][3] <= 0:
            return res
        scale = float(ohlc[anchor][3]) / float(snap_px)
        if not (0.2 < scale < 5.0):
            return {"status": "bad_anchor"}
    plan = bt.reconstruct_plan(cand)
    if plan is None:
        return {"status": "bad_anchor"}
    r = bt.simulate(plan, dates, ohlc, anchor + 1, scale)
    status = r.get("status")
    if status in ("pending", "no_fill", "box_broke", "gap_invalid"):
        return {"status": status,
                "end_date": dates[r["end_i"]] if r.get("end_i") is not None else None}

    fill_px = float(r["fill_px"])
    disp_fill = fill_px / scale                       # 换回信号时名义价格展示
    shares = math.floor(budget / (fill_px / scale * lot)) * lot if lot > 1 \
        else math.floor(budget / (fill_px / scale))
    if shares <= 0:
        return {"status": "too_expensive"}
    used = shares * disp_fill
    ret = float(r["ret"])                             # simulate 已含成本 (持仓中含买入侧)
    return {
        "status": status, "fill_date": r["fill_date"], "fill_px": round(disp_fill, 3),
        "exit_date": r["exit_date"], "exit_px": round(float(r["exit_px"]) / scale, 3),
        "ret": round(ret, 5), "days": int(r["days"]), "complete": bool(r["complete"]),
        "shares": int(shares), "used": round(used, 2), "pnl": round(used * ret, 2),
        "max_gain": round(float(r["max_gain"]), 4), "max_dd": round(float(r["max_dd"]), 4),
    }


def update_portfolio() -> dict | None:
    m = current()
    hist_dir, state_path, js_path = _paths()
    budget, lot, cur_sym = _budget_and_lot()
    state = _load_state(state_path)
    as_of, sigs = _latest_signals()
    n_new = _register(state, sigs)
    log.info("模拟组合: 注册表 %d 条 (+%d 新), 快照 %s",
             len(state["signals"]), n_new, as_of)
    if not state["signals"]:
        return None

    # 未了结信号 -> 拉行情重新模拟; 已了结用缓存
    active = [s for s in state["signals"] if not s.get("final")]
    prices = {}
    if active:
        start = min(s["sig_date"] for s in active)
        start = (dt.date.fromisoformat(start) - dt.timedelta(days=FETCH_PAD_DAYS)).isoformat()
        prices = bt.fetch_price_series(sorted({s["code"] for s in active}), start)

    rows = []
    for s in state["signals"]:
        if s.get("final"):
            rows.append({**s["final"], "id": s["id"], "cat": s["cat"], "code": s["code"],
                         "name": s.get("name"), "sig_date": s["sig_date"]})
            continue
        ser = prices.get(s["code"])
        res = {"status": "no_data"} if ser is None else _simulate_signal(s, ser, budget, lot)
        if res["status"] in FINAL_STATUSES and (res.get("complete") or "fill_date" not in res):
            s["final"] = res                          # 完整窗口走完 (或从未成交) 才缓存
        rows.append({**res, "id": s["id"], "cat": s["cat"], "code": s["code"],
                     "name": s.get("name"), "sig_date": s["sig_date"]})

    filled = [r for r in rows if r.get("shares")]
    open_rows = [r for r in filled if r["status"] == "open"]
    resolved = [r for r in filled if r["status"] in ("won", "stopped", "expired")]

    def _agg(rs: list[dict]) -> dict:
        res_ = [r for r in rs if r["status"] in ("won", "stopped", "expired")]
        opn = [r for r in rs if r["status"] == "open"]
        wins = [r for r in res_ if r["ret"] > 0]
        return {
            "n_signals": len(rs), "n_filled": len([r for r in rs if r.get("shares")]),
            "n_open": len(opn), "n_resolved": len(res_), "n_won": len(wins),
            "win_rate": round(len(wins) / len(res_) * 100, 1) if res_ else None,
            "realized": round(sum(r["pnl"] for r in res_), 2),
            "unrealized": round(sum(r["pnl"] for r in opn), 2),
            "avg_ret": round(float(np.mean([r["ret"] for r in res_])) * 100, 2) if res_ else None,
        }

    by_cat = {c: _agg([r for r in rows if r["cat"] == c]) for c in CATS}
    total = _agg(rows)

    # 逐日净值点 (以最新bar日期为准, 同日重跑覆盖)
    last_dates = [r.get("exit_date") for r in open_rows if r.get("exit_date")]
    mark_date = max(last_dates) if last_dates else (as_of or dt.date.today().isoformat())
    point = {"d": mark_date, "realized": total["realized"],
             "unrealized": total["unrealized"], "n_open": total["n_open"]}
    state["daily"] = [p for p in state.get("daily", []) if p.get("d") != mark_date] + [point]
    state["daily"] = state["daily"][-260:]

    payload = {
        "meta": {"updated": dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                 "budget": budget, "currency": cur_sym, "cooldown_days": COOLDOWN_DAYS,
                 "market": m.name, "as_of": as_of},
        "total": total, "by_cat": by_cat,
        "open": sorted(open_rows, key=lambda r: r["fill_date"], reverse=True),
        "recent": sorted(resolved, key=lambda r: r["exit_date"] or "", reverse=True)[:15],
        "daily": state["daily"],
    }
    os.makedirs(os.path.dirname(state_path), exist_ok=True)
    with open(state_path, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False)
    with open(js_path, "w", encoding="utf-8") as f:
        f.write("window.__PP__ = " + json.dumps(payload, ensure_ascii=False) + ";\n")
    log.info("模拟组合: 持仓 %d / 已了结 %d / 胜率 %s%% / 已实现 %s%.0f -> %s",
             total["n_open"], total["n_resolved"], total["win_rate"],
             cur_sym, total["realized"], os.path.basename(js_path))
    return payload
