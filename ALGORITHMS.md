# 核心算法说明（左侧支撑位筛选系统）

> 这份文档把系统里**所有核心算法**用公式 + 带注释的代码写出来，目的有两个：
> 1. **对外解释**——任何人读完能明白"这个筛选器到底在算什么、凭什么把一只股票排在前面"；
> 2. **邀请高手改进**——每个算法都标了它的*直觉、公式、参数、已知局限*，方便更专业的人直接动手优化。
>
> 美股版（`screener/`）与 A 股版（`ashare/`）的**核心打分算法完全一致**（模块 1~4），只是数据源不同（yfinance vs akshare）。下面的代码以 A 股版为准（价格单位 ¥，数据源 akshare）。
>
> ⚠️ **注意**：第 5 节"现金流漏洞引擎"及其新闻/期权/暗池等**深度档案（模块6）目前仅美股版实现**，A 股版尚未移植——保留在此文档中是为了完整说明系统设计，A 股接入需换用 akshare 的财报/资金流接口。

---

## 0. 整体流水线（先看这张图）

```
             ┌─────────────────────────────────────────────────────┐
 股票池      │  全A股(剔除ST/次新) 或指定板块                          │
 (universe)  └───────────────────────┬─────────────────────────────┘
                                     │
         ┌───────────────────────────┼───────────────────────────┐
         ▼                           ▼                           ▼
 ┌───────────────┐        ┌────────────────────┐       ┌──────────────────┐
 │ 模块1 板块景气 │        │ 模块2 技术左侧扫描  │       │ 模块3 基本面      │
 │ 5支柱打分     │        │ (全部股票)          │       │ (仅技术分Top-N)   │
 │ → 景气分0-100 │        │ → 技术分 tech_score │       │ → 基本面分0-100   │
 └───────┬───────┘        └─────────┬──────────┘       └────────┬─────────┘
         │                          │                           │
         └──────────────┬───────────┴───────────────┬───────────┘
                        ▼                            ▼
              ┌────────────────────┐      ┌────────────────────────┐
              │ 模块4 交叉打分      │      │ 模块6 深度档案(Top-200) │
              │ 综合分 = 技术×景气×  │      │ 现金流/营收/新闻/期权/  │
              │ 基本面 → 结论标签   │      │ 暗池 + 规则化"漏洞"要点 │
              └─────────┬──────────┘      └────────────────────────┘
                        ▼
              ┌────────────────────┐
              │ 模块5 SQLite 入库   │ → 导出 dashboard_data.js → 网页看板
              └────────────────────┘
```

**为什么这样分层**：技术扫描要跑全市场（几千只），很快；基本面/深度档案要抓财报和期权，慢且有反爬限制，所以**只对技术分最高的那批股票**去抓，用"两阶段漏斗"把算力花在刀刃上。

---

## 1. 核心算法一：技术左侧打分

> 文件：[`ashare/module2_tech.py`](ashare/module2_tech.py) 的 `scan_one()`
> 一句话：**给每只股票在"是否正回踩到一个可靠支撑位"上打分，越贴近支撑、信号越多，分越高。**

"左侧"= 在下跌途中、支撑*确认之前*就进场（对应"右侧"= 等反转确认后才追）。所以我们找的是**正在接近支撑、但还没跌破**的形态。

### 1.1 打分总公式

$$
\text{tech\_score} = \sum_{\text{命中的信号 } i} w_i \times \text{strength}_i
$$

一共 6 类信号，每类有自己的权重 $w_i$ 和"强度" $\text{strength}_i \in [0,1]$（越贴近支撑强度越高）。权重在 `CONFIG["tech"]["weights"]`：

| 信号 | 含义 | 权重 $w$ | 强度怎么算 |
|---|---|---|---|
| `channel` | 贴近**上升通道下轨** | 1.0 | $0.5 + 0.5\cdot\text{prox}$ |
| `pivot` | 贴近**前期重要低点** | 1.0 | $0.5 + 0.5\cdot\text{prox}$ |
| `ma` | 回踩**关键均线**(MA60/120/250) | 0.8 | $0.5 + 0.5\cdot\text{prox}$ |
| `oversold_div` | **超跌** + MACD 底背离/绿柱缩短 + RSI 超卖 | 1.2 | 三个子条件加权，封顶 1 |
| `drawdown` | **回撤够深**（左侧的前提） | 0.6 | $\min(1,\ \text{回撤}/0.5)$ |
| `vol_confirm` | 支撑处**量能确认**（缩量企稳/放量） | 0.5 | 缩量 0.7 / 放量 0.5 |

其中"接近度" prox 的定义（以通道为例）：

$$
\text{prox} = \max\!\Big(0,\ 1 - \frac{|\text{dist}|}{\text{near\_lower\_pct}}\Big)
$$

`dist` = 现价距支撑的百分比。**正好踩在支撑上 → dist=0 → prox=1 → 强度=1.0（满分）**；离得越远强度线性衰减到 0.5，超出阈值(4%)就不算命中。这个 `0.5 + 0.5·prox` 的设计保证"只要命中至少拿一半分，越准拿越多"。

### 1.2 六类信号逐个讲

**① 上升通道下轨** — 对最近 120 根收盘价做**线性回归**得到趋势线，再往下平移 2 倍残差标准差作为"下轨"。只有*上升趋势*（斜率>0）且现价落在下轨上方 4% 以内才算命中。

```python
# 对最近 window 根 close 做 y = slope·x + intercept 的最小二乘拟合
ch = ind.linreg_channel(close, c["channel_window"], c["channel_band_k"])
dist_lower = (px - ch["lower_band"]) / px * 100.0        # 现价距下轨%
hit_channel = ch["uptrend"] and (-1.0 <= dist_lower <= c["near_lower_pct"])
if hit_channel:
    prox = max(0.0, 1 - abs(dist_lower) / c["near_lower_pct"])
    score += w["channel"] * (0.5 + 0.5 * prox)
    support_cands.append(("通道下轨", ch["lower_band"]))   # 记为候选支撑位
```

**② 前期重要低点** — 找"摆动低点"（左右各 window 根内的最低点），保留离现价 15% 以内的，取最近的一个。现价距它 4% 内算命中。

```python
piv = ind.find_pivot_lows(low, c["pivot_window"])         # [(下标,价格),...]
cands = [p for (i, p) in piv if i < len(df)-5 and abs(p-px)/px <= 0.15]
if cands:
    nearest = min(cands, key=lambda p: abs(p - px))        # 离现价最近的前低
    dist_pivot = (px - nearest) / px * 100.0
    if abs(dist_pivot) <= c["near_pivot_pct"]:
        prox = max(0.0, 1 - abs(dist_pivot) / c["near_pivot_pct"])
        score += w["pivot"] * (0.5 + 0.5 * prox)
```

**③ 关键均线支撑** — 遍历 MA60/120/250，现价落在某条均线上方 3% 内算命中，取最贴近的那条。

```python
for n in c["ma_list"]:                                    # [60, 120, 250]
    ma = close.rolling(n).mean().iloc[-1]
    d = (px - ma) / px * 100.0
    if -1.0 <= d <= c["near_ma_pct"]:                     # 贴在均线上方3%内
        hit_ma = True
        if best_ma_dist is None or abs(d) < abs(best_ma_dist):
            best_ma_dist, best_ma, best_ma_price = d, n, float(ma)
```

**④ 超跌 + 底背离**（这是"左侧"最关键的择时信号）— 三个子条件命中任一即算，但强度叠加：
- **RSI 超卖**：RSI ≤ 38（子分 0.5）
- **MACD 绿柱缩短**：柱子还在 0 下方但比上一根高，说明下跌动能衰竭（子分 0.25）
- **MACD 底背离**：价格创新低（最低点在最近 15 根内）但 DIF 没有创新低（后半段最小值 > 前半段最小值）—— 典型的"价跌指标不跌"反转前兆（子分 0.5）

```python
dif, dea, hist = ind.macd(close)
r = ind.rsi(close)
green_shrink = (hist.iloc[-1] < 0) and (hist.iloc[-1] > hist.iloc[-2])
bull_div = False
if len(close) > 60:
    c_seg, d_seg = close.tail(60).reset_index(drop=True), dif.tail(60).reset_index(drop=True)
    if c_seg.idxmin() >= 60 - 15:                          # 价格最低点在最近15根内(创新低)
        if d_seg.iloc[30:].min() > d_seg.iloc[:30].min():  # 但DIF后半段没破前低 → 背离
            bull_div = True
oversold = rsi_now <= c["rsi_oversold"]
if oversold or green_shrink or bull_div:
    sub = (0.5 if oversold else 0) + (0.25 if green_shrink else 0) + (0.5 if bull_div else 0)
    score += w["oversold_div"] * min(1.0, sub)             # 子分封顶1.0
```

**⑤ 回撤幅度**（左侧前提）— 从最近 120 根的最高点算起，跌幅 ≥ 18% 才认为"跌得够深、有左侧价值"。跌 50% 给满分。

```python
hi = float(high.tail(c["channel_window"]).max())
drawdown = (hi - px) / hi                                  # 距区间高点的回撤
if drawdown >= c["drawdown_min"]:                          # ≥18%
    score += w["drawdown"] * min(1.0, drawdown / 0.5)      # 跌50%给满分
```

**⑥ 量能确认** — 只有在已经命中某个支撑的前提下，量能才加分：**缩量**（近量 < 20 日均量的 85%，"卖压枯竭"）给 0.7，**放量上涨**给 0.5。

```python
vol_ratio_calc = vol.iloc[-1] / avg20v                     # 当日量 / 20日均量
shrink   = vol_ratio_calc < c["vol_shrink_ratio"]          # <0.85 缩量企稳
spike_up = vol_ratio_calc > 1.5 and close.iloc[-1] > close.iloc[-2]
if support_cands and (shrink or spike_up):
    score += w["vol_confirm"] * (0.7 if shrink else 0.5)
```

### 1.3 从信号导出"可操作的关键位"

打完分后，把所有候选支撑（通道下轨/前低/均线/布林下轨）汇总，选**离现价最近的**作为"主支撑"，并算出破位止损参考位：

```python
# 主支撑 = 所有候选里离现价最近的
support_label, support_price = min(support_cands, key=lambda kv: abs(px - kv[1]))
dist_support = (px - support_price) / px * 100.0           # 距支撑%（网页高亮用）
# 破位参考 = 最低支撑再下方3%（跌破即形态失败，止损位）
breakdown_price = min(all_support_prices) * 0.97
```

> **给高手的话**：这套打分是"专家规则加权"，没有做参数寻优/回测。可改进方向：(a) 用历史数据回测每个信号的胜率来定权重，而非拍脑袋；(b) 通道用稳健回归（Theil–Sen）替代最小二乘，抗异常值；(c) 背离判断目前是"两段极值比较"的粗糙版，可换成基于摆动点配对的严格背离检测。

---

## 2. 核心算法二：技术指标库

> 文件：[`ashare/indicators.py`](ashare/indicators.py)
> 所有指标对 NaN/数据不足都做**安全降级**（返回 NaN 而不抛异常），保证扫描不会因个别脏数据中断。

### 2.1 MACD

$$
\text{DIF} = \text{EMA}_{12}(C) - \text{EMA}_{26}(C),\quad
\text{DEA} = \text{EMA}_9(\text{DIF}),\quad
\text{HIST} = (\text{DIF}-\text{DEA})\times 2
$$

```python
def macd(close, fast=12, slow=26, signal=9):
    dif = ema(close, fast) - ema(close, slow)   # 快慢EMA之差
    dea = ema(dif, signal)                       # DIF的EMA
    hist = (dif - dea) * 2.0                      # 柱状图(能量)
    return dif, dea, hist
```

### 2.2 RSI（相对强弱，0–100）

$$
\text{RSI} = 100 - \frac{100}{1 + \dfrac{\text{平均涨幅}_n}{\text{平均跌幅}_n}}
$$

```python
def rsi(close, n=14):
    diff = close.diff()
    up = diff.clip(lower=0).rolling(n).mean()        # n日平均上涨
    dn = (-diff.clip(upper=0)).rolling(n).mean()     # n日平均下跌
    rs = up / dn.replace(0, np.nan)
    return 100 - 100 / (1 + rs)
```

### 2.3 KDJ（随机指标）

$$
\text{RSV}=\frac{C-L_n}{H_n-L_n}\times100,\quad
K=\text{EMA}_{1/3}(\text{RSV}),\quad D=\text{EMA}_{1/3}(K),\quad J=3K-2D
$$

```python
def kdj(high, low, close, n=9, k_period=3, d_period=3):
    low_n, high_n = low.rolling(n,1).min(), high.rolling(n,1).max()
    rsv = (close - low_n) / (high_n - low_n).replace(0, np.nan) * 100.0
    rsv = rsv.fillna(50.0)
    k = rsv.ewm(alpha=1/k_period, adjust=False).mean()   # 通达信SMA(RSV,3,1)的等价EMA
    d = k.ewm(alpha=1/d_period, adjust=False).mean()
    j = 3.0*k - 2.0*d
    return k, d, j
```

### 2.4 线性回归通道（技术信号①的核心）

对最近 window 根价格拟合直线 $y=\text{slope}\cdot x+\text{intercept}$，下轨 = 拟合线 − k×残差标准差：

```python
def linreg_channel(close, window, k):
    seg = close.tail(window).reset_index(drop=True)
    x, y = np.arange(len(seg)), seg.values
    slope, intercept = np.polyfit(x, y, 1)               # 最小二乘拟合直线
    pred = slope * x + intercept
    resid_std = (y - pred).std()                          # 残差标准差(通道宽度)
    lower_series = pred - k * resid_std                   # 下轨 = 中线 - k·σ
    return {"slope": slope, "lower_band": lower_series[-1],
            "lower_series": lower_series, "uptrend": slope > 0}
```

### 2.5 Beta（相对大盘的系统性风险）★ 有个容易踩的坑

$$
\beta = \frac{\text{Cov}(r_{\text{stock}},\ r_{\text{bench}})}{\text{Var}(r_{\text{bench}})}
$$

**坑**：个股和大盘的日线可能因停牌/数据源日历不一致而**日期没对齐**。如果直接按"最后 N 根"位置配对，会把不同交易日的收益配在一起，算出完全错误的 Beta（实测能把真实 1.5 算成 −0.02）。所以必须**先按日期取交集再算**：

```python
def beta(stock_close, bench_close, bars=120):
    sr = stock_close.pct_change().dropna()               # 个股日收益
    br = bench_close.pct_change().dropna()               # 基准日收益
    if 带日期索引:
        common = sr.index.intersection(br.index)         # ★按日期交集对齐(关键)
        sr, br = sr.loc[common], br.loc[common]
    var = np.var(br, ddof=1)                              # ddof与cov统一,避免n/(n-1)偏差
    return np.cov(sr, br)[0,1] / var
```

### 2.6 其余指标（公式一览）

| 指标 | 公式 / 说明 |
|---|---|
| **ATR%** | $\text{ATR}=\text{MA}_n(\max(H-L,\|H-C_{-1}\|,\|L-C_{-1}\|))$，再 $/现价$ 得波动率代理 |
| **最大回撤** | $\min\big(\frac{C_t}{\max_{s\le t}C_s}-1\big)$，最近 250 根，负值 |
| **布林下轨** | $\text{MA}_{20}(C) - 2\cdot\text{STD}_{20}(C)$ |
| **斐波那契回撤** | 区间高低差 $R=H-L$，支撑价 $= H-\{0.382,0.5,0.618\}\times R$ |
| **摆动低点** | 左右各 window 根内的最低点即为一个 pivot low |
| **累计涨跌** | $\frac{C_t}{C_{t-\text{bars}}}-1$（近一月用 bars=21，近半年 bars=120） |
| **降采样** | 用 `np.linspace` 均匀取 40 个点，用于表格里的行内迷你走势图(sparkline) |

---

## 3. 核心算法三：板块景气度（5 支柱）

> 文件：[`ashare/module1_industry.py`](ashare/module1_industry.py)
> 一句话：**给每个板块打 0–100 的"景气分"，用板块 ETF 的趋势/动量 + 成分股的广度**。高景气板块里的左侧机会更值得关注。

美股用 11 个 GICS 板块，各自对应一只 SPDR 行业 ETF（科技=XLK、金融=XLF……）作为"板块指数"。

### 3.1 三大支柱的原始特征

**A. 趋势** — 板块 ETF 是否在均线上方、回归斜率是否向上：

$$
t_1=\frac{P-\text{MA}_{60}}{\text{MA}_{60}},\quad
t_2=\frac{P-\text{MA}_{120}}{\text{MA}_{120}},\quad
t_3=\text{归一化回归斜率}_{60}
$$

**B. 动量** — 近 20 日、60 日涨幅，以及相对大盘的超额：

$$
m_1=\text{ret}_{20},\quad m_2=\text{ret}_{60},\quad m_3=m_2-\text{大盘}_{60}
$$

**C. 广度** — 板块成分股里"健康个股"的比例（这是景气度的核心，衡量"是不是普涨"）：

```python
b1 = 站上MA60的成分股比例          # 多少只在中期均线之上
b2 = 近20日上涨的成分股比例        # 多少只在涨
b3 = (上涨数 - 下跌数) / 总数       # 涨跌广度(ADL思想), 范围[-1,1]
```

### 3.2 从特征到分数：横截面 z-score → 百分位 → 加权

关键思想：**同一支柱的原始值先做横截面标准化**（在所有板块间比较，而不是看绝对值），再转成 0–100 百分位：

```python
# 每个支柱 = 其子特征的横截面z-score取均值
df["A_raw"] = mean(zscore(t1), zscore(t2), zscore(t3))       # 趋势
df["B_raw"] = mean(zscore(m1), zscore(m2), zscore(m3))       # 动量
df["C_raw"] = mean(b1, b2, (b3+1)/2)                         # 广度(b3从[-1,1]映到[0,1])

df["trend"]    = cross_sectional_percentile(df["A_raw"])     # → 0-100
df["momentum"] = cross_sectional_percentile(df["B_raw"])
df["breadth"]  = cross_sectional_percentile(df["C_raw"])
```

最终景气分 = 各支柱百分位的**加权平均**，权重来自 `CONFIG["sector"]["weights"]`。A 股版用**全部 5 支柱**（趋势 0.25 / 动量 0.25 / 广度 0.20 / 资金流 0.15 / 基本面 0.15）——akshare 能提供北向/主力资金流，所以比美股多了"资金"这一维：

$$
\text{prosperity} = 100\times\frac{\sum_{\text{可用支柱}} w_p \cdot (\text{pct}_p/100)}{\sum_{\text{可用支柱}} w_p}
$$

```python
def _score_row(r):
    num, den = 0.0, 0.0
    for pillar, wt in weights.items():
        pct = r.get(pillar)
        if wt > 0 and pct is not None and not np.isnan(pct):
            num += wt * (pct / 100.0)
            den += wt                          # ★只累加"有数据"的支柱权重
    return round(100.0 * num / den, 2)         # 除以den → 权重自动重新归一
```

> **注意这个"权重重归一"细节**：如果某支柱缺数据（如基本面/资金流为 NaN），它不会被当成 0 分拉低总分，而是把它的权重从分母里剔除——保证"缺数据 ≠ 差"。这是很多打分系统会犯的错。

---

## 4. 核心算法四：技术 × 基本面交叉打分

> 文件：[`ashare/module4_crossscore.py`](ashare/module4_crossscore.py)
> 一句话：**把技术分、基本面分、景气分揉成一个 0–100 的综合分，并贴一个人能看懂的结论标签。**

### 4.1 综合分公式

$$
\text{综合分} = w_{\text{tech}}\cdot\text{tech\_norm} + w_{\text{fund}}\cdot\text{fund\_score} + w_{\text{prosperity}}\cdot\text{景气分}
$$

权重 `CONFIG["cross"]` = 技术 0.50 / 基本面 0.30 / 景气 0.20。其中技术分要先**归一到 0–100**（除以理论满分再×100）：

```python
_TECH_MAX = sum(CONFIG["tech"]["weights"].values())          # 满分=各信号权重之和≈5.1
tech_norm = clamp(tech_score / _TECH_MAX * 100.0, 0, 100)     # 技术分归一到0-100
final = (cc["w_tech"] * tech_norm
       + cc["w_fund"] * fund_score
       + cc["w_prosperity"] * prosp_for_score)               # 景气未知时用50中性占位
```

### 4.2 基本面分（0–100，从 50 中性起步加减）

```python
def _fund_score(f):
    s = 50.0                                          # 中性起点
    if roe >= 18:  s += 20      elif roe >= 12: s += 12    elif roe < 0: s -= 20   # 盈利能力
    if pe便宜(分位<30%) and pe>0: s += 12   elif pe贵(分位>80%): s -= 10           # 估值
    if pe <= 0:    s -= 10                                                          # 亏损扣分
    if 净利同比>0:  s += 8       elif 净利同比<-20: s -= 10                          # 成长
    if 负债率>=70: s -= 8                                                           # 杠杆
    if 毛利率>=40: s += 5                                                           # 质地
    return clamp(s, 0, 100)
```

### 4.3 结论标签（三档）

```python
def _tag(tech, fund, prosperity):
    if tech>=2.0 and fund>=60 and (prosperity is None or prosperity>=60):
        return "✅ 强左侧"                     # 技术好+基本面好+景气高
    if tech>=2.0 and fund<40:
        return "⚠️ 技术好但基本面弱"           # 形态漂亮但基本面差,谨慎
    return "🔎 观察"
```

> **一个诚实的设计**：当板块景气未知时（全市场回退模式），综合分排序用 50 占位，但**展示的景气分是"—"而不是假的 50**，标签判定也不会伪称"已通过景气门槛"。不拿占位值骗自己。

---

## 5. 核心算法五：现金流"漏洞"规则引擎

> 文件：[`ashare/module6_profile.py`](ashare/module6_profile.py) 的 `_cash_insights()`
> 一句话：**用财务常识写成的 if-then 规则，自动指出"这家公司的钱流向哪了、有没有猫腻"。**

这不是评分，而是**把年报现金流量表翻译成人话预警**。核心是几条经典的财务分析规则：

```python
# 规则1: 盈利质量 —— 经营现金流 vs 净利润
if ocf / ni < 0.7:   → ⚠"利润没转化成现金(应收/存货占款?),盈利质量存疑"
if ocf / ni > 1.1:   → ✓"利润含金量高"

# 规则2: 自由现金流为负
if fcf < 0:          → ⚠"造血不足以覆盖资本开支,需外部融资"

# 规则3: 大额收购(找并购/商誉风险)
if |收购支出| >= 30% 经营现金流:  → ⚠"大额收购,关注商誉与整合风险"

# 规则4: 借钱回馈股东(不可持续信号)
if 回购+分红 > 自由现金流 且 当年净举债>0:  → ⚠"借钱回购分红,不可持续"

# 规则5: 重资产扩张
if 资本开支 > 80% 经营现金流:  → ℹ"重资产扩张期,关注回报率"
```

**为什么这条最能"找漏洞"**：会计利润可以被应计项目粉饰，但现金流量表很难造假。经营现金流长期低于净利润、或"利润为正但自由现金流为负还在大手笔回购"——这些都是财报排雷的经典信号。规则引擎把它们自动跑一遍，省去人工翻报表。

> **给高手的话**：这些阈值（0.7、30%、80%）是行业经验值，不同板块该不一样（重资产的公用事业天然资本开支高）。可改进：按板块设不同阈值，或引入同行分位而非绝对阈值。

---

## 6. 全部可调参数（`CONFIG`）

所有旋钮集中在 [`ashare/config.py`](ashare/config.py)，改这里就能调系统行为，不用碰算法代码：

```python
"tech": {
    "min_price": 3.0,              # 股价下限(剔除仙股)
    "min_amount_usd": 5e6,         # 近20日日均成交额下限(剔除流动性差)
    "channel_window": 120,         # 通道/回撤回看窗口
    "channel_band_k": 2.0,         # 通道下轨 = 中线 - k×残差std
    "near_lower_pct": 4.0,         # "贴近通道下轨"的阈值%
    "near_pivot_pct": 4.0,         # "贴近前低"的阈值%
    "near_ma_pct": 3.0,            # "回踩均线"的阈值%
    "ma_list": [60, 120, 250],     # 参与判断的均线
    "rsi_oversold": 38.0,          # RSI超卖线
    "drawdown_min": 0.18,          # 左侧前提:最小回撤18%
    "weights": {                   # ★六类信号的权重(打分的核心旋钮)
        "channel": 1.0, "pivot": 1.0, "ma": 0.8,
        "oversold_div": 1.2, "drawdown": 0.6, "vol_confirm": 0.5},
},
"sector": {
    "weights": {"trend":0.25, "momentum":0.25, "breadth":0.20,
                "capital":0.15, "fundamental":0.15},  # 景气5支柱权重(A股含资金流)
},
"cross": {
    "w_tech":0.50, "w_fund":0.30, "w_prosperity":0.20,  # ★综合分三大权重
    "roe_good":12.0, "roe_excellent":18.0,              # 基本面加分线
    "strong_left_tech":2.0, "strong_left_fund":60.0,    # "强左侧"标签门槛
},
```

---

## 7. 已知局限（诚实清单 / 给改进者的邀请函）

| # | 局限 | 可能的改进方向 |
|---|---|---|
| 1 | **权重靠经验拍定，没做回测寻优** | 用历史数据回测各信号胜率来标定权重；或用逻辑回归/梯度提升学习权重 |
| 2 | **无未来收益标签，不是预测模型** | 系统只做"形态筛选"，不预测涨跌。可加"信号出现后 N 日收益"的回测统计 |
| 3 | 通道用最小二乘，**对异常值敏感** | 换稳健回归(Theil–Sen / Huber) |
| 4 | 背离检测是**粗糙的两段极值比较** | 用严格的摆动点配对做背离 |
| 5 | 现金流规则**阈值一刀切**，不分行业 | 按板块设阈值，或用同行分位 |
| 6 | 新闻情绪用**关键词粗分**，会有误判 | 换成小型情感模型/LLM 打分 |
| 7 | 暗池用 **FINRA 场外空头占比代理**，非逐笔暗池 | 逐笔暗池数据需付费源(如 IEX/Cheddar Flow) |
| 8 | 基本面分部营收**无免费源**，用成本结构拆解替代 | 接入付费财报分部数据(如 Daloopa) |

---

### 免责声明

本系统仅做技术/基本面数据的自动化整理与形态筛选，**不构成任何投资建议**。"左侧买入"是在下跌中、支撑确认前进场，风险天然更高（可能继续下跌或破位）。所有标的需人工复核，使用者自负盈亏。
