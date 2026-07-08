#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
一键运行 (One-command pipeline)
===============================
    python run_pipeline.py              # 完整跑: 行业景气 -> 技术扫描 -> 基本面 -> 交叉打分 -> 入库 -> 导出仪表盘
    python run_pipeline.py --full-market  # 跳过行业筛选, 扫描全市场
    python run_pipeline.py --demo       # 不联网: 用合成数据填库 + 导出, 便于先看仪表盘
    python run_pipeline.py --no-cache   # 不使用本地缓存
跑完后双击打开 dashboard/index.html。
"""
from __future__ import annotations
import os
import sys
import time
import socket
import argparse
import logging
import datetime as dt
from concurrent.futures import ThreadPoolExecutor, as_completed

# 防卡死: 给所有网络请求设默认超时。akshare/requests 若不显式传 timeout, 单个卡住的
# 连接会让阶段C(单线程)无限期挂起(历史上曾卡在 12/200)。30s 足够正常返回, 卡住则抛错被 _safe 捕获。
socket.setdefaulttimeout(30)

from ashare.config import CONFIG
from ashare import db
from ashare import datasource as ds
from ashare import module1_industry as m1
from ashare import module2_tech as m2
from ashare import module3_fundamentals as m3
from ashare import module4_crossscore as m4
from ashare import module6_profile as m6
from ashare import export_data as ex

# Windows 控制台默认 GBK, 输出中文/emoji 会报 UnicodeEncodeError; 统一切到 UTF-8
for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8")
    except Exception:
        pass

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
                    datefmt="%H:%M:%S")
log = logging.getLogger("ashare.run")


def _tqdm():
    try:
        from tqdm import tqdm
        return tqdm
    except Exception:
        def _f(x, **k):
            return x
        return _f


def run(full_market: bool, use_cache: bool):
    tqdm = _tqdm()
    run_date = dt.date.today().isoformat()
    started = dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    CONFIG["source"]["use_cache"] = use_cache
    if full_market:
        CONFIG["industry"]["use_full_market"] = True

    db.init_db()
    db.clear_run(run_date)   # 干净快照: 清掉今天的旧结果(含演示数据)

    # ---------------- 模块1: 行业景气 ----------------
    log.info("模块1: 计算行业景气度 ...")
    ind_df = m1.compute_industry_scores(
        progress_cb=lambda i, n, name: (i % 5 == 0) and log.info("  行业 %d/%d %s", i, n, name))
    if ind_df is not None and not ind_df.empty:
        db.save_industry_scores(run_date, ind_df)
    prosperity_map = {}
    ind_to_codes = {}
    selected_inds = []
    if ind_df is not None and not ind_df.empty:
        prosperity_map = dict(zip(ind_df["industry"], ind_df["prosperity_score"]))
        selected_inds = list(ind_df[ind_df["selected"]]["industry"])
    log.info("模块1: 入选行业 %s", selected_inds)

    # ---------------- 候选股票池 ----------------
    spot = ds.fetch_spot_snapshot()
    spot_map = {}
    if spot is not None and not spot.empty:
        spot_map = {r["code"]: r.to_dict() for _, r in spot.iterrows()}

    def _full_market_universe():
        uni = ds.build_universe(spot)
        rows = []
        if uni is not None:
            thr = CONFIG["tech"]["min_amount_yi"] * 1e8
            minp = CONFIG["tech"]["min_price"]
            for _, r in uni.iterrows():
                code, name = r["code"], r["name"]
                sp = spot_map.get(code, {})
                price = sp.get("price")
                if price is not None and price == price and price < minp:
                    continue   # 低价股预筛, 避免无谓拉取日线
                amt = sp.get("amount")
                if amt is not None and amt == amt and 0 < amt < thr * 0.3:
                    continue   # 明显流动性不足预筛
                rows.append((code, name, None))
        log.info("候选池: 全市场(预筛后) %d 只", len(rows))
        return rows

    universe = []   # list of (code, name, industry)
    if CONFIG["industry"]["use_full_market"] or not selected_inds:
        universe = _full_market_universe()
    else:
        seen = set()
        for ind_name in selected_inds:
            cons = ds.fetch_industry_cons(ind_name)
            if cons is None:
                continue
            ind_to_codes[ind_name] = list(cons["code"])
            for _, r in cons.iterrows():
                code = r["code"]
                if code in seen:
                    continue
                # 基础过滤: ST / 北交所
                name = r.get("name") or (spot_map.get(code, {}).get("name"))
                if CONFIG["tech"]["exclude_st"] and name and "ST" in str(name).upper():
                    continue
                if CONFIG["tech"]["exclude_bj"] and str(code).startswith(("8", "4", "920")):
                    continue
                seen.add(code)
                universe.append((code, name, ind_name))
        log.info("候选池: 入选行业成分股 %d 只", len(universe))
        # 成分股全部获取失败(东财实时端点被重置)时, 回退到全市场扫描, 保证流程不空跑
        if len(universe) == 0:
            log.warning("行业成分股获取失败(东财push2被限, 无可用备用成分接口), 回退到全市场扫描。"
                        "行业景气榜仍展示; 但个股缺行业归属, '所属行业/景气加成/行业PE对比'将显示 '—'。")
            universe = _full_market_universe()

    # 行业 PE 中位 (用于基本面对比)
    industry_pe_median = m3.compute_industry_pe_median(spot, ind_to_codes) if ind_to_codes else {}

    # ---------------- 模块2: 技术扫描 (并发, 阶段A) ----------------
    # 网络IO密集 -> 线程池并发; 只做技术打分, 便宜且快。
    workers = CONFIG["fetch"]["max_workers"] or min(16, (os.cpu_count() or 4) * 2)

    _bench = ds.fetch_benchmark_close()
    if _bench is not None and not _bench.empty:
        # 日期作索引 -> beta() 按日期交集对齐
        bench_close = _bench.set_index(_bench["date"].astype(str))["close"]
    else:
        bench_close = None

    def _scan_stock(code, name, industry):
        h = ds.fetch_hist(code)
        if h is None:
            return None
        rec, detail = m2.scan_one(code, name, h, spot_map.get(code), bench_close=bench_close)
        if rec is None:
            return None
        # 支撑分达标 OR 深跌抄底桶达标, 二者其一即保留 (dip 桶专捞结构已破的深跌超卖股)
        if rec["tech_score"] < CONFIG["tech"]["min_tech_score"] and not rec.get("dip"):
            return None
        rec["industry"] = industry
        return (rec, detail)

    log.info("阶段A 技术扫描: %d 只, 并发 %d 线程 ...", len(universe), workers)
    hits = []
    n_scanned = 0
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [pool.submit(_scan_stock, c, n, i) for (c, n, i) in universe]
        for fut in tqdm(as_completed(futures), total=len(futures)):
            n_scanned += 1
            try:
                r = fut.result()
            except Exception as e:
                log.debug("扫描失败: %s", e)
                continue
            if r:
                hits.append(r)
    log.info("技术命中 %d 只", len(hits))

    # ---------------- 模块3-4: 仅对技术分最高的前N只拉基本面 (阶段B) ----------------
    # 技术分降序; 同分时按代码升序, 保证跨次运行结果确定(否则受线程完成顺序影响)
    hits.sort(key=lambda rd: (-rd[0]["tech_score"], rd[0]["code"]))
    top_hits = hits[:CONFIG["output"]["fund_top_n"]]
    # 并入"深跌抄底"桶: 支撑分排不进 top_hits、但深跌达标的, 按 dip_score 取前 dip_top_n 只补进来。
    # 先剔除已在 top_hits 的再切片(与 export 过滤顺序一致), 让深跌超卖股也能进 final_rank(带 🪸 标签)。
    _seen = {rd[0]["code"] for rd in top_hits}
    dip_pool = sorted([rd for rd in hits if rd[0].get("dip")],
                      key=lambda rd: -rd[0].get("dip_score", 0.0))
    dip_new = [rd for rd in dip_pool if rd[0]["code"] not in _seen][:CONFIG["output"].get("dip_top_n", 40)]
    for rd in dip_new:
        top_hits.append(rd)
        _seen.add(rd[0]["code"])
    log.info("深跌抄底桶: 命中 %d 只, 并入候选 %d 只", len(dip_pool), len(dip_new))
    log.info("阶段B 基本面+交叉打分: 取技术分最高的 %d 只(含深跌抄底) ...", len(top_hits))

    def _fund_stock(rd):
        rec, detail = rd
        industry = rec.get("industry")
        f = m3.pull_fundamentals(
            rec["code"], industry=industry,
            industry_pe_median=industry_pe_median.get(industry) if industry else None,
            spot_row=spot_map.get(rec["code"]))
        fr = m4.cross_score(rec, f, prosperity_map.get(industry) if industry else None)
        return (rec, detail, f, fr)

    results = []
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [pool.submit(_fund_stock, rd) for rd in top_hits]
        for fut in tqdm(as_completed(futures), total=len(futures)):
            try:
                results.append(fut.result())
            except Exception as e:
                log.debug("基本面失败: %s", e)
                continue

    # 按综合分排序后落库(同分按代码升序, 结果确定); 详情(K线)只存前 N 只以控制 JS 体积
    results.sort(key=lambda x: (-(x[3].get("final_score") or -1), x[0]["code"]))
    detail_n = CONFIG["output"]["dashboard_detail_top_n"]
    final_records = []
    for idx, (rec, detail, f, fr) in enumerate(results):
        db.save_tech(run_date, [rec])
        db.save_fundamental(run_date, rec["code"], f)
        db.save_final(run_date, [fr])
        final_records.append(fr)
        # 深跌抄底股 final_score 低会沉到 detail_n 之后, 但会在主表浮现; 不存 detail 点进去K线是空的 -> dip 股无论排名都存。
        if (idx < detail_n or fr.get("dip")) and detail:
            db.save_detail(run_date, rec["code"], detail)

    # ---------------- 模块6: 个股深度档案 (阶段C) ----------------
    # 仅对最终展示的候选生成: 简介/主营构成/营收增速/现金流+漏洞/风险/新闻/两融/龙虎榜/大宗
    # 注: akshare 部分东财接口用 py_mini_racer(V8) 解密, 多线程会崩 -> 单线程串行。
    show_n = CONFIG["output"].get("final_top_n") or len(final_records)
    # 同理: export 会把排名 >show_n 的 dip 股(上限 dip_top_n)补进主表, 这里也给它们做深度档案, 否则点开 🪸 股档案是空的。
    dip_tail = [fr for fr in final_records[show_n:] if fr.get("dip")][:CONFIG["output"].get("dip_top_n", 40)]
    prof_targets = final_records[:show_n] + dip_tail
    log.info("阶段C 深度档案: %d 只 (主营/现金流/新闻/两融/大宗) 单线程 ...", len(prof_targets))
    for fr in tqdm(prof_targets):
        try:
            p = m6.pull_profile(fr["code"], sector=fr.get("industry"))
            db.save_profile(run_date, fr["code"], p)
        except Exception as e:
            log.debug("深度档案失败 %s: %s", fr["code"], e)

    finished = dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    db.log_run(run_date, started, finished, n_scanned, len(final_records),
               selected_inds, "ok")
    log.info("扫描完成: 扫描 %d, 命中 %d", n_scanned, len(final_records))

    # ---------------- 导出仪表盘 ----------------
    ex.write_dashboard_js(run_date)
    ex.write_csv(run_date)
    log.info("✅ 全部完成。请双击打开 dashboard/index.html")


def main():
    ap = argparse.ArgumentParser(description="A股左侧支撑位筛选 + 监控")
    ap.add_argument("--full-market", action="store_true", help="跳过行业筛选, 扫描全市场")
    ap.add_argument("--demo", action="store_true", help="离线合成数据演示 (不联网)")
    ap.add_argument("--no-cache", action="store_true", help="禁用本地缓存")
    args = ap.parse_args()

    if args.demo:
        from make_demo_data import build_demo
        build_demo()
        return

    t0 = time.time()
    try:
        run(full_market=args.full_market, use_cache=not args.no_cache)
    except KeyboardInterrupt:
        log.warning("用户中断")
        sys.exit(1)
    log.info("耗时 %.1f 秒", time.time() - t0)


if __name__ == "__main__":
    main()
