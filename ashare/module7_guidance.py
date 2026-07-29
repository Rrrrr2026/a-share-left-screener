#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
模块7 — 盈利指引 v2 (跨股票近邻回归)
=====================================
问题: "按这个价位买进去, 赚 50% / 100% 的概率有多大?"

v1 为什么不够用
---------------
v1 只在**这一只股票自己**的历史里找相似时刻。可单只股票十年也就 3~15 次大回撤,
绝大多数股票凑不够 3 次 -> 页面上全是"样本不足"。样本天然不够, 不是阈值调松就能解决的。

v2 的做法: 把样本池从"这一只"扩展到"全市场", 但必须解决可比性
------------------------------------------------------------
直接把银行股和小盘半导体混在一起统计是错的 —— 统计上叫**样本不可交换**。
三层处理:

1) **把"谁在关注它"变成特征**: 市值、换手率、波动率、流动性 —— 这些是"资金结构"
   的可观测代理。找相似案例时不只要求"形态像", 还要求"这类股票像"。

2) **按自身波动率归一化目标(最关键)**: 银行股涨50%是几年一遇, 小盘成长股可能是
   家常便饭。所以不预测原始涨幅, 而是预测**"涨了几个自身波动单位"**;
   回到目标股票时, 再用**它自己的波动率**换算回来。这样不同品种的历史才真正可比。
   —— 判断 P(涨≥50%) 时, 把 50% 先换算成目标股票的波动单位, 再去数近邻。

3) **分层匹配 + 透明标注**: 优先同行业同规模的案例; 不够再放宽。页面明确标注
   这次用的是哪一层、多少个案例、其中多少来自本行业 —— 而不是假装都一样。

为什么用近邻回归而不是梯度提升/神经网
--------------------------------------
本机只有 numpy(无 sklearn), 但更重要的是: 近邻法在这里**不是妥协, 是更合适**。
它直接就是用户的原始思路("找历史上类似的情况, 看后来怎么走")的严格化版本,
每个概率都能摊开成具体案例(哪只股票、哪一天、后来涨跌多少)供人工复核 ——
黑箱模型给出的 37.2% 无法验证, 而这里的 37% 是"148个可查案例里有55个达到了"。
"""
from __future__ import annotations
import datetime as dt
import logging
import os

import numpy as np
import pandas as pd

from .config import CONFIG, DATA_DIR

log = logging.getLogger("ashare.module7")

MODEL_PATH = os.path.join(DATA_DIR, "guidance_model.npz")

# 参与"形态相似度"的特征 (身份特征另作分层, 不进距离)
FEATS = ["dd", "rsi", "pos52", "dlow60", "ma60d", "ma120d", "ret20", "ret60", "volr"]

DEFAULTS = {
    "horizon": 250,          # 前向观察窗口(交易日) ≈1年
    "lookback_high": 250,    # 回撤基准
    "sample_every": 10,      # 训练时每隔N个交易日取一个观测点(降低自相关)
    "k": 160,                # 近邻数
    "min_pool": 300,         # 某一层候选池至少要有这么多观测, 否则放宽到下一层
    "min_neighbors": 40,     # 少于这么多近邻则不出结论
    "targets": [100, 50, 30, 20],
    "model_max_age_days": 7, # 模型缓存超过这么久就重建
    "train_stocks": 800,     # 训练池股票数(分层抽样)
}


def _cfg(overrides: dict | None = None) -> dict:
    """DEFAULTS <- CONFIG.guidance <- 显式传入。**必须走这里合并**:
    外部(如管线)常只传 CONFIG['guidance'](v1遗留键), 直接当 cfg 用会 KeyError。"""
    d = dict(DEFAULTS)
    d.update(CONFIG.get("guidance") or {})
    if overrides:
        d.update({k: v for k, v in overrides.items() if v is not None})
    return d


# --------------------------------------------------------------------------- #
#  指标
# --------------------------------------------------------------------------- #
def _rsi(close: pd.Series, n: int = 14) -> pd.Series:
    d = close.diff()
    up = d.clip(lower=0).ewm(alpha=1 / n, adjust=False).mean()
    dn = (-d.clip(upper=0)).ewm(alpha=1 / n, adjust=False).mean()
    rs = up / dn.replace(0, np.nan)
    return (100 - 100 / (1 + rs)).fillna(50)


def _vol_ann(close: pd.Series, n: int = 60) -> pd.Series:
    """年化波动率(%) —— 归一化的尺子。低波动股和高波动股的'涨50%'难度天差地别。"""
    r = close.pct_change()
    return r.rolling(n).std() * np.sqrt(244) * 100.0


def compute_features(df: pd.DataFrame) -> pd.DataFrame:
    """逐日算出形态特征 + 波动率。df 需含 close/high/low(+volume)。"""
    c = df["close"].astype(float)
    h = df["high"].astype(float) if "high" in df.columns else c
    l = df["low"].astype(float) if "low" in df.columns else c
    v = pd.to_numeric(df["volume"], errors="coerce") if "volume" in df.columns else pd.Series(np.nan, index=c.index)

    out = pd.DataFrame(index=df.index)
    roll_high = c.rolling(250, min_periods=60).max()
    out["dd"] = (roll_high - c) / roll_high * 100.0          # 回撤%
    out["rsi"] = _rsi(c)
    hi52, lo52 = c.rolling(244, min_periods=60).max(), c.rolling(244, min_periods=60).min()
    out["pos52"] = (c - lo52) / (hi52 - lo52).replace(0, np.nan) * 100.0
    low60 = l.rolling(60, min_periods=20).min()
    out["dlow60"] = (c - low60) / low60 * 100.0              # 距60日低点%
    out["ma60d"] = (c / c.rolling(60).mean() - 1) * 100.0
    out["ma120d"] = (c / c.rolling(120).mean() - 1) * 100.0
    out["ret20"] = (c / c.shift(20) - 1) * 100.0
    out["ret60"] = (c / c.shift(60) - 1) * 100.0
    out["volr"] = v.rolling(20).mean() / v.rolling(60).mean() # 量能比
    out["vol_ann"] = _vol_ann(c)
    return out


def _forward(df: pd.DataFrame, i: int, H: int) -> tuple | None:
    """从 i 买入, 之后H个交易日内: 最高涨幅% / 最深跌幅% / 期末收益%。"""
    c = df["close"].astype(float)
    h = df["high"].astype(float) if "high" in df.columns else c
    l = df["low"].astype(float) if "low" in df.columns else c
    entry = float(c.iloc[i])
    if entry <= 0 or i + H >= len(df):
        return None
    fh, fl, fc = h.iloc[i + 1:i + 1 + H], l.iloc[i + 1:i + 1 + H], c.iloc[i + 1:i + 1 + H]
    if len(fh) < H * 0.9:
        return None
    return (float(fh.max()) / entry - 1) * 100.0, \
           (float(fl.min()) / entry - 1) * 100.0, \
           (float(fc.iloc[-1]) / entry - 1) * 100.0


# --------------------------------------------------------------------------- #
#  训练集
# --------------------------------------------------------------------------- #
def observations_for(code: str, df: pd.DataFrame, meta: dict, cfg: dict) -> list:
    """把一只股票的历史切成若干观测点。只保留"左侧"情形(已有明显回撤), 与用途一致。"""
    if df is None or len(df) < 400:
        return []
    F = compute_features(df)
    H, step = cfg["horizon"], cfg["sample_every"]
    rows = []
    start = 260
    for i in range(start, len(df) - H, step):
        f = F.iloc[i]
        if not np.isfinite(f["dd"]) or f["dd"] < 15:        # 只学"跌下来之后"的样本
            continue
        va = f["vol_ann"]
        if not np.isfinite(va) or va <= 1:
            continue
        fw = _forward(df, i, H)
        if fw is None:
            continue
        vals = [f[k] for k in FEATS]
        if not all(np.isfinite(x) for x in vals):
            continue
        gain, mae, ret = fw
        rows.append(vals + [va, gain / va, mae / va, ret / va,      # 归一化目标
                            gain, mae, ret,
                            meta.get("mcap", np.nan), meta.get("turnover", np.nan)])
    return rows


def build_model(codes: list, spot_map: dict, hist_getter, cfg: dict = None,
                progress=None) -> dict | None:
    """扫一批股票的长历史, 汇成跨股票样本池。返回 dict 并落盘。"""
    cfg = _cfg(cfg)
    X, Y, IND, CODES, NAMES = [], [], [], [], []
    n_ok = 0
    for j, code in enumerate(codes):
        if progress:
            progress(j + 1, len(codes), code)
        try:
            df = hist_getter(code)
        except Exception:
            df = None
        if df is None or len(df) < 400:
            continue
        sp = spot_map.get(code) or {}
        meta = {"mcap": sp.get("total_mv"), "turnover": sp.get("turnover")}
        rows = observations_for(code, df, meta, cfg)
        if not rows:
            continue
        n_ok += 1
        ind = sp.get("industry") or "—"
        nm = sp.get("name") or code
        for r in rows:
            X.append(r[:len(FEATS)])
            Y.append(r[len(FEATS):len(FEATS) + 7])   # vol_ann, g/mae/ret(归一), g/mae/ret(原始)
            IND.append(ind)
            CODES.append(code)
            NAMES.append(nm)
    if len(X) < 2000:
        log.warning("盈利指引训练样本仅 %d 条(<2000), 放弃建模", len(X))
        return None
    X = np.asarray(X, dtype=np.float32)
    Y = np.asarray(Y, dtype=np.float32)
    mu, sd = X.mean(axis=0), X.std(axis=0)
    sd[sd < 1e-6] = 1.0
    model = {"X": X, "Y": Y, "IND": np.asarray(IND), "CODES": np.asarray(CODES),
             "NAMES": np.asarray(NAMES),
             "mu": mu, "sd": sd, "built": dt.date.today().isoformat(),
             "n_stocks": n_ok}
    log.info("盈利指引模型: %d 只股票 -> %d 条观测, %d 个行业",
             n_ok, len(X), len(set(IND)))
    try:
        np.savez_compressed(MODEL_PATH, **{k: v for k, v in model.items()})
    except Exception as e:
        log.debug("模型落盘失败: %s", e)
    return model


def load_model(cfg: dict = None) -> dict | None:
    cfg = _cfg(cfg)
    if not os.path.exists(MODEL_PATH):
        return None
    try:
        z = np.load(MODEL_PATH, allow_pickle=False)
        built = str(z["built"])
        age = (dt.date.today() - dt.date.fromisoformat(built)).days
        if age > cfg["model_max_age_days"]:
            log.info("盈利指引模型已过期(%d天), 将重建", age)
            return None
        return {k: z[k] for k in z.files}
    except Exception as e:
        log.debug("模型读取失败: %s", e)
        return None


# --------------------------------------------------------------------------- #
#  预测
# --------------------------------------------------------------------------- #
def _weighted_quantile(vals, w, q):
    idx = np.argsort(vals)
    v, ww = np.asarray(vals)[idx], np.asarray(w)[idx]
    cw = np.cumsum(ww)
    if cw[-1] <= 0:
        return float(np.median(v))
    return float(np.interp(q * cw[-1], cw, v))


def predict(code: str, df: pd.DataFrame, model: dict, spot_row: dict | None = None,
            support_price: float | None = None, cfg: dict = None) -> dict:
    """对一只股票给出盈利指引。df 为其日线(至少 ~300 根)。"""
    cfg = _cfg(cfg)
    out = {"guid_n": 0, "guid_probs": [], "guid_note": None, "guid_tier": None,
           "guid_med_gain": None, "guid_med_mae": None, "guid_win_rate": None,
           "guid_buy_low": None, "guid_buy_high": None, "guid_samples": [],
           "guid_vol_ann": None, "guid_same_ind": 0}
    if model is None or df is None or len(df) < 300:
        out["guid_note"] = "数据不足, 无法给出指引"
        return out

    F = compute_features(df)
    f = F.iloc[-1]
    vals = [f[k] for k in FEATS]
    va = float(f["vol_ann"])
    if not all(np.isfinite(x) for x in vals) or not np.isfinite(va) or va <= 1:
        out["guid_note"] = "指标计算异常, 不给指引"
        return out
    out["guid_vol_ann"] = round(va, 1)

    X, Y, IND = model["X"], model["Y"], model["IND"]
    mu, sd = model["mu"], model["sd"]
    ind_now = (spot_row or {}).get("industry") or "—"

    # ---- 分层: 同行业 -> 同波动档 -> 全市场 (每层都要够大才用) ----
    vol_all = Y[:, 0]
    same_ind = (IND == ind_now)
    band = (vol_all >= va * 0.6) & (vol_all <= va * 1.6)     # 同波动档
    tiers = [("同行业", same_ind & band), ("同行业", same_ind),
             ("同波动档", band), ("全市场", np.ones(len(X), dtype=bool))]
    mask, tier = None, None
    for name, m in tiers:
        if int(m.sum()) >= cfg["min_pool"]:
            mask, tier = m, name
            break
    if mask is None:
        mask, tier = np.ones(len(X), dtype=bool), "全市场"
    out["guid_tier"] = tier

    # ---- 近邻: 标准化后的欧氏距离 ----
    q = (np.asarray(vals, dtype=np.float32) - mu) / sd
    Xs = (X[mask] - mu) / sd
    d = np.sqrt(((Xs - q) ** 2).sum(axis=1))
    k = min(cfg["k"], len(d))
    if k < cfg["min_neighbors"]:
        out["guid_note"] = f"可比案例仅 {k} 个, 不足以给出频率"
        return out
    nn = np.argpartition(d, k - 1)[:k]
    dn = d[nn]
    w = 1.0 / (dn + 1e-6)                    # 距离加权: 越像的案例权重越大
    w = w / w.sum()

    Ysub = Y[mask][nn]
    g_norm, mae_norm, ret_norm = Ysub[:, 1], Ysub[:, 2], Ysub[:, 3]
    out["guid_n"] = int(k)
    out["guid_same_ind"] = int((IND[mask][nn] == ind_now).sum())

    # ---- 概率: 把目标涨幅换算成"目标股票自己的波动单位"再去数近邻 ----
    probs = []
    for t in cfg["targets"]:
        thr = t / va                                   # 关键: 用本股波动率换算
        p = float(w[(g_norm >= thr)].sum())
        probs.append({"target": t, "prob": round(p, 3),
                      "hits": int((g_norm >= thr).sum())})
    out["guid_probs"] = probs
    out["guid_med_gain"] = round(_weighted_quantile(g_norm, w, 0.5) * va, 1)
    out["guid_med_mae"] = round(_weighted_quantile(mae_norm, w, 0.5) * va, 1)
    out["guid_win_rate"] = round(float(w[(ret_norm > 0)].sum()), 3)

    # ---- 买入区间 ----
    base = support_price if (support_price and support_price > 0) else float(df["close"].iloc[-1])
    out["guid_buy_high"] = round(float(base), 2)
    out["guid_buy_low"] = round(float(base) * (1 + min(0.0, out["guid_med_mae"]) / 100.0 * 0.5), 2)

    # ---- 最像的若干真实案例(可人工复核) ----
    order = np.argsort(dn)[:8]
    Csub = model["CODES"][mask][nn]
    Isub = IND[mask][nn]
    Nsub = model["NAMES"][mask][nn] if "NAMES" in model else Csub
    for oi in order:
        out["guid_samples"].append({
            "code": str(Csub[oi]), "name": str(Nsub[oi]), "industry": str(Isub[oi]),
            "max_gain_pct": round(float(Ysub[oi, 4]), 1),
            "mae_pct": round(float(Ysub[oi, 5]), 1),
            "ret_1y_pct": round(float(Ysub[oi, 6]), 1),
            "vol_ann": round(float(Ysub[oi, 0]), 1),
            "similarity": round(float(1.0 / (1.0 + dn[oi])), 3),
        })

    p50 = next((p for p in probs if p["target"] == 50), None)
    out["guid_note"] = (
        f"匹配层级：{tier}；可比案例 {k} 个"
        + (f"(其中同行业 {out['guid_same_ind']} 个)" if out["guid_same_ind"] else "")
        + f"；本股年化波动 {va:.0f}%"
        + (f"；涨50%+ 的历史频率 {p50['prob']*100:.0f}%" if p50 else "")
        + f"；期间最深回撤中位 {out['guid_med_mae']:.0f}%"
    )
    return out
