#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
中央配置 (Central CONFIG)
=========================
所有阈值 / 权重 / 行业数量 / 股票池开关 / token 都集中在这里。
改这里就能改变全流程的结果 (满足验收标准 #4)。

英文注释/中文注释皆可;但仪表盘与导出的 *用户可见文字* 必须为简体中文。
"""

from __future__ import annotations
import os

# 项目根目录 (this file is .../a-share-left-screener/ashare/config.py)
PKG_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT_DIR = os.path.dirname(PKG_DIR)
DATA_DIR = os.path.join(ROOT_DIR, "data")
DASHBOARD_DIR = os.path.join(ROOT_DIR, "dashboard")

os.makedirs(DATA_DIR, exist_ok=True)

DB_PATH = os.path.join(DATA_DIR, "ashare.db")
# 仪表盘读取的数据文件 (导出为 JS, 直接 <script> 引入, 双击 HTML 即可打开, 无需服务器)
DASHBOARD_DATA_JS = os.path.join(DASHBOARD_DIR, "dashboard_data.js")


CONFIG = {
    # =====================================================================
    #  数据源 (Data sources)
    # =====================================================================
    "source": {
        "primary": "akshare",          # akshare (免费, 全市场)
        "tushare_token": os.environ.get("TUSHARE_TOKEN", ""),  # 可选, 留空则只用 akshare
        "industry_classification": "东财",   # 行业分类口径: 东财(EastMoney). akshare 的 board_industry_* 即东财一级行业
        "benchmark_index": "sh000300",  # 沪深300, 用于超额收益基准
        "cache_dir": os.path.join(DATA_DIR, "cache"),
        "cache_ttl_hours": 12,          # 行情/财务数据本地缓存有效期 (小时)
        "use_cache": True,
    },

    # =====================================================================
    #  抓取行为 (Fetch behaviour) —— 限频 + 重试 + 跳过失败
    # =====================================================================
    "fetch": {
        "lookback_days": 500,           # 拉取最近多少日历日的日线 (≈330 交易日, 需 >MA250)
        "adjust": "qfq",                # 前复权
        "sleep_sec": 0.05,              # 每次接口调用之间 sleep, 防限频(并发下调小)
        "max_retries": 2,               # 单次接口失败重试次数 (akshare 内部已自带重试)
        "retry_backoff_sec": 1.0,       # 重试退避基数 (秒), 指数退避
        "timeout_sec": 30,
        # 逐只扫描的并发线程数 (网络IO密集, 提高它能成倍加速; 过高可能被限频)
        # 默认取 min(16, CPU*2); 设为具体数字可覆盖。
        "max_workers": 0,
    },

    # =====================================================================
    #  模块1 — 行业景气度 (Industry prosperity)
    # =====================================================================
    "industry": {
        "top_n": 8,                     # 入选行业数 (Top N 作为模块2的候选池)
        "use_full_market": False,       # True = 跳过行业筛选, 扫描全市场 (退路开关)
        "trend_gate_enabled": True,     # 趋势硬门槛: 行业指数需在 MA120 上方(或2%内)
        "trend_gate_tolerance_pct": 2.0,
        "ma_short": 60,
        "ma_long": 120,
        # 五大支柱权重 (sum 不必为1, 最终按加权百分位归一)
        "weights": {
            "trend":       0.25,        # 趋势
            "momentum":    0.25,        # 动量
            "breadth":     0.20,        # 广度
            "capital":     0.15,        # 资金 (无数据时权重并入趋势+动量)
            "fundamental": 0.15,        # 基本面景气
        },
        "breadth_sample": 60,           # 计算广度时, 每个行业最多抽样多少只成分股 (控制耗时)
        "momentum_excess_weight_boost": 1.0,  # m3(对沪深300超额) 的额外权重
    },

    # =====================================================================
    #  模块2 — 技术左侧扫描 (Technical left-side scan)
    #  阈值/权重沿用并扩展参考实现 a_share_left_screener.py
    # =====================================================================
    "tech": {
        # ---- 股票池过滤 ----
        "exclude_st": True,
        "exclude_new_days": 180,        # 上市交易日不足则视为次新, 剔除
        "min_amount_yi": 0.5,           # 近20日日均成交额下限(亿元)
        "min_price": 2.0,
        "exclude_bj": True,             # 剔除北交所(8/4/920 开头)
        # ---- 信号阈值 ----
        "channel_window": 120,          # 拟合上升通道窗口(交易日)
        "channel_band_k": 2.0,          # 下轨 = 回归线 - k*残差std
        "near_lower_pct": 4.0,          # 距下轨 <=4% 视为贴近
        "pivot_window": 10,             # 摆动低点识别窗口(左右各N根)
        "near_pivot_pct": 4.0,          # 距前低 <=4% 视为接近
        "ma_list": [60, 120, 250],
        "near_ma_pct": 3.0,             # 距均线 <=3% 视为均线支撑
        "rsi_oversold": 38.0,
        "drawdown_min": 0.18,           # 左侧前提: 距区间高至少回撤18%
        # ---- 各信号权重 ----
        "weights": {
            "channel": 1.0,
            "pivot":   1.0,
            "ma":      0.8,
            "oversold_div": 1.2,
            "drawdown": 0.6,
            "vol_confirm": 0.5,         # 支撑处量能确认(缩量企稳/放量企稳)
        },
        "boll_n": 20, "boll_k": 2.0,    # 布林带下轨(额外支撑参考)
        "vol_shrink_ratio": 0.85,       # 支撑处近量/20日均量 < 此值 = 缩量企稳
        "min_tech_score": 1.0,          # 技术分低于此值不进入候选 (后续仍做基本面)
        "detail_bars": 250,             # 详情页 K 线保留多少根
        # ---- 独立"深跌超卖抄底"桶 (与支撑型左侧互不干扰) ----
        # 刻画结构已破的深度价值/抄底标的: 深跌 + 超卖 + 逼近52周低点 (与主模型要求上升通道/贴均线/前低企稳不同)。
        "dip": {
            "drawdown_min": 0.35,       # 从近channel_window高点回撤 >= 35%
            "rsi_max": 32.0,            # RSI(14) <= 32 (真超卖)
            "pos_52w_max": 20.0,        # 处于52周区间底部 20% 以内
            "vol_spike": 1.8,           # 近量/20日均量 >= 此值 = 放量(见底确认之一)
            "weights": {"depth": 1.2, "oversold": 1.0, "nearlow": 1.0, "confirm": 0.8},
        },
    },

    # =====================================================================
    #  模块4 — 技术 × 基本面 交叉打分
    # =====================================================================
    "cross": {
        # 综合分 = 技术分(标准化) * w_tech + 基本面分 * w_fund + 景气加成 * w_prosperity
        "w_tech": 0.50,
        "w_fund": 0.30,
        "w_prosperity": 0.20,
        # 基本面打分阈值 (用于 0-100 评分)
        "roe_good": 12.0,               # ROE(%) 高于此为加分
        "roe_excellent": 18.0,
        "pe_low_percentile": 30.0,      # PE 历史分位 低于此为"偏低"加分
        "pe_high_percentile": 80.0,     # 高于此为"偏高"减分
        "debt_ratio_warn": 70.0,        # 资产负债率(%) 高于此预警
        "netprofit_yoy_good": 0.0,      # 净利同比 > 0 加分
        # 结论标签阈值
        "strong_left_tech": 2.0,        # 强左侧: 技术分门槛
        "strong_left_fund": 60.0,       # 强左侧: 基本面分门槛
        "strong_left_prosperity": 60.0, # 强左侧: 所属行业景气分门槛
        "fund_weak_threshold": 40.0,    # 基本面分低于此 -> "技术好但基本面弱"
    },

    # =====================================================================
    #  输出 (Output)
    # =====================================================================
    "output": {
        "final_top_n": 200,             # 仪表盘候选清单最多展示多少只
        "fund_top_n": 300,              # 仅对"技术分最高的前N只"拉基本面(限制耗时/接口压力)
        "dashboard_detail_top_n": 150,  # 详情(K线)数据为前多少只生成 (控制 JS 体积)
        "dip_top_n": 40,                # 深跌抄底桶最多并入/展示的只数 (上限, 防止灌进一堆刀)
    },
}


def deep_get(d: dict, path: str, default=None):
    """按 'a.b.c' 路径取嵌套配置, 安全降级。"""
    cur = d
    for key in path.split("."):
        if not isinstance(cur, dict) or key not in cur:
            return default
        cur = cur[key]
    return cur
