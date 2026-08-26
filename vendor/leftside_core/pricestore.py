#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
本地价格库 (Local price store)
================================
量化研究的地基: 全市场 5 年日线 (含成交量) 抓一次存本地 SQLite, 之后每天增量
更新。所有回测/形态扫描只读本地库 —— 数据源抖动最多耽误一次更新, 不再影响研究。

  bars(code, d, o, h, l, c, v)   前复权日线
  idx_bars(d, o, h, l, c, v)     基准指数 (沪深300 / SPY)

取数走 Market 钩子 (fetch_bars_bulk / fetch_index_bars / universe_codes),
市场差异 (腾讯分页 vs yfinance 批量、盘中丢当日bar) 全部在各仓库 market.py 里。
"""
from __future__ import annotations
import datetime as dt
import logging
import os
import sqlite3

import numpy as np

from .market import current

log = logging.getLogger("leftside_core.pricestore")

YEARS = 5
STALE_DAYS_FULL = 3650      # backfill: 完全没有该股才算缺
BATCH = 400                 # 每批写库/打日志的代码数


def _db_path() -> str:
    return os.path.join(current().data_dir, "pricestore.db")


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(_db_path(), timeout=60)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("CREATE TABLE IF NOT EXISTS bars("
                 "code TEXT, d TEXT, o REAL, h REAL, l REAL, c REAL, v REAL, "
                 "PRIMARY KEY(code, d)) WITHOUT ROWID")
    conn.execute("CREATE TABLE IF NOT EXISTS idx_bars("
                 "d TEXT PRIMARY KEY, o REAL, h REAL, l REAL, c REAL, v REAL) WITHOUT ROWID")
    return conn


def _upsert(conn: sqlite3.Connection, code: str, rows: list) -> None:
    conn.executemany(
        "INSERT OR REPLACE INTO bars(code,d,o,h,l,c,v) VALUES(?,?,?,?,?,?,?)",
        [(code, r[0], r[1], r[2], r[3], r[4], r[5]) for r in rows])


def last_dates(conn: sqlite3.Connection | None = None) -> dict:
    own = conn is None
    conn = conn or _conn()
    out = {r[0]: r[1] for r in conn.execute("SELECT code, MAX(d) FROM bars GROUP BY code")}
    if own:
        conn.close()
    return out


def backfill(codes: list | None = None, years: int = YEARS) -> dict:
    """缺哪补哪: 库里没有的代码抓全量 5 年; 已有的跳过 (增量交给 update_daily)。"""
    m = current()
    if codes is None:
        codes = (m.universe_codes or (lambda: []))()
    start = (dt.date.today() - dt.timedelta(days=365 * years + 30)).isoformat()
    conn = _conn()
    have = set(last_dates(conn))
    todo = [c for c in codes if c not in have]
    log.info("价格库回填: 目标 %d, 已有 %d, 待抓 %d (起点 %s)",
             len(codes), len(have), len(todo), start)
    n_ok = 0
    aborted = False
    for i in range(0, len(todo), BATCH):
        chunk = todo[i:i + BATCH]
        got = m.fetch_bars_bulk(chunk, start)
        for code, rows in got.items():
            _upsert(conn, code, rows)
            n_ok += 1
        conn.commit()
        log.info("回填批 %d/%d: +%d (累计 %d)",
                 i // BATCH + 1, (len(todo) + BATCH - 1) // BATCH, len(got), n_ok)
        if not got:                       # 数据源配额已尽: 立即停, 剩下的留给下一轮续传
            log.warning("回填批全空 -> 判定配额/封禁, 提前结束本轮 (已 %d)", n_ok)
            aborted = True
            break
    # 基准指数 (被配额掐断时跳过, 留给下一轮)
    if not aborted:
        idx = (m.fetch_index_bars or (lambda s: []))(start)
        if idx:
            conn.executemany("INSERT OR REPLACE INTO idx_bars(d,o,h,l,c,v) VALUES(?,?,?,?,?,?)", idx)
            conn.commit()
            log.info("基准指数: %d 根", len(idx))
    conn.close()
    return {"target": len(codes), "fetched": n_ok, "skipped": len(have), "aborted": aborted}


def update_daily(codes: list | None = None, lookback_days: int = 10) -> int:
    """增量: 对库里已有代码抓最近 lookback_days 补上新bar (前复权价可能因除权
    整体平移 —— 增量只适合日常; 检测到大偏差的代码应重新全量, 这里先记日志)。"""
    m = current()
    conn = _conn()
    have = last_dates(conn)
    if codes is None:
        codes = sorted(have)
    start = (dt.date.today() - dt.timedelta(days=lookback_days)).isoformat()
    n = 0
    for i in range(0, len(codes), BATCH):
        chunk = [c for c in codes[i:i + BATCH] if c in have]
        got = m.fetch_bars_bulk(chunk, start)
        for code, rows in got.items():
            _upsert(conn, code, rows)
            n += 1
        conn.commit()
    idx = (m.fetch_index_bars or (lambda s: []))(start)
    if idx:
        conn.executemany("INSERT OR REPLACE INTO idx_bars(d,o,h,l,c,v) VALUES(?,?,?,?,?,?)", idx)
        conn.commit()
    conn.close()
    log.info("价格库增量: %d 只已更新", n)
    return n


def load(codes: list) -> dict:
    """-> {code: {"dates": [...], "ohlcv": ndarray[N,5]}} (o,h,l,c,v) 升序。"""
    conn = _conn()
    out = {}
    for code in codes:
        rows = conn.execute(
            "SELECT d,o,h,l,c,v FROM bars WHERE code=? ORDER BY d", (code,)).fetchall()
        if len(rows) >= 60:
            out[code] = {"dates": [r[0] for r in rows],
                         "ohlcv": np.array([r[1:] for r in rows], dtype=float)}
    conn.close()
    return out


def load_index() -> dict | None:
    conn = _conn()
    rows = conn.execute("SELECT d,o,h,l,c,v FROM idx_bars ORDER BY d").fetchall()
    conn.close()
    if len(rows) < 60:
        return None
    return {"dates": [r[0] for r in rows],
            "ohlcv": np.array([r[1:] for r in rows], dtype=float)}


def coverage() -> dict:
    conn = _conn()
    n_codes, n_bars = conn.execute("SELECT COUNT(DISTINCT code), COUNT(*) FROM bars").fetchone()
    dmin, dmax = conn.execute("SELECT MIN(d), MAX(d) FROM bars").fetchone()
    n_idx = conn.execute("SELECT COUNT(*) FROM idx_bars").fetchone()[0]
    conn.close()
    return {"codes": n_codes, "bars": n_bars, "from": dmin, "to": dmax, "index_bars": n_idx}


if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    cmd = sys.argv[1] if len(sys.argv) > 1 else "backfill"
    if cmd == "backfill":
        print(backfill())
    elif cmd == "update":
        print(update_daily())
    print(coverage())
