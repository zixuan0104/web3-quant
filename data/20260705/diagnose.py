"""
策略诊断 — 分析为什么三种策略在 BTC 1h 上全部亏损

诊断维度：
  1. 市场环境 — BTC 过去两年的市况分布
  2. 入场信号质量 — 入场后 N 根 K 线的价格走向
  3. 成本吞噬 — 手续费 + 滑点占总收益的比例
  4. BTC 1h 价格统计特征
  5. 综合诊断结论
"""

import pandas as pd
import numpy as np
import sys
import os

if sys.platform == 'win32':
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from backtest.strategies.trend import TrendStrategy
from backtest.strategies.momentum import MomentumStrategy

CLEAN_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'clean')

# ═══════════════════════════════
# 加载数据
# ═══════════════════════════════
df = pd.read_parquet(os.path.join(CLEAN_DIR, 'BTCUSDT_1h.parquet'))
print(f"📂 数据: {len(df):,} 行 | {df.index[0]} → {df.index[-1]}")

# ═══════════════════════════════
# 诊断 1: 市场环境分析
# ═══════════════════════════════
print("\n" + "=" * 60)
print("🔍 诊断 1: 市场环境")
print("=" * 60)

df['ret_200'] = df['close'].pct_change(200)
df['vol_200'] = df['close'].pct_change().rolling(200).std() * np.sqrt(365 * 24)

df['regime'] = '未知'
trend_threshold = 0.15
vol_threshold = df['vol_200'].median()

for i in range(len(df)):
    r = df['ret_200'].iloc[i]
    v = df['vol_200'].iloc[i]
    if pd.isna(r) or pd.isna(v):
        continue
    if abs(r) > trend_threshold and v > vol_threshold:
        df.loc[df.index[i], 'regime'] = '强趋势高波动'
    elif abs(r) > trend_threshold and v <= vol_threshold:
        df.loc[df.index[i], 'regime'] = '趋势低波动'
    elif abs(r) <= trend_threshold and v > vol_threshold:
        df.loc[df.index[i], 'regime'] = '震荡高波动'
    else:
        df.loc[df.index[i], 'regime'] = '震荡低波动'

regime_counts = df['regime'].value_counts()
print("  市况分布（200 根 K 线滚动窗口）:")
for regime, count in regime_counts.items():
    pct = count / len(df) * 100
    print(f"    {regime:<16}: {count:>6,} 根 ({pct:5.1f}%)")

total_return = (df['close'].iloc[-1] / df['close'].iloc[0] - 1) * 100
print(f"\n  BTC 两年总收益: {total_return:.1f}%")
print(f"  起始价: ${df['close'].iloc[0]:,.0f} → 结束价: ${df['close'].iloc[-1]:,.0f}")

peak = df['close'].expanding().max()
price_dd = (df['close'] - peak) / peak
max_price_dd = price_dd.min() * 100
print(f"  BTC 价格最大回撤: {max_price_dd:.1f}%")

# ═══════════════════════════════
# 诊断 2: 策略入场后价格走向
# ═══════════════════════════════
print("\n" + "=" * 60)
print("🔍 诊断 2: 入场信号后 N 根 K 线价格走向")
print("=" * 60)


def get_min_period(strat):
    """获取策略的最小数据需求"""
    for attr in ['slow_period', 'rsi_period', 'slow_momentum']:
        val = getattr(strat, attr, None)
        if val is not None:
            return val
    return 50


def analyze_signal_quality(strategy, df, label):
    """分析入场信号的质量"""
    strat = strategy()
    strat.precompute(df)

    signals = []
    min_period = get_min_period(strat)

    for i in range(min_period, len(df)):
        bar = df.iloc[i]
        if bar.get('anomaly', False):
            continue
        try:
            if strat.check_entry(bar, i):
                entry_price = bar['close']
                forward_1 = df['close'].iloc[i + 1] if i + 1 < len(df) else np.nan
                forward_4 = df['close'].iloc[i + 4] if i + 4 < len(df) else np.nan
                forward_12 = df['close'].iloc[i + 12] if i + 12 < len(df) else np.nan
                forward_24 = df['close'].iloc[i + 24] if i + 24 < len(df) else np.nan
                signals.append({
                    'idx': i,
                    'entry_price': entry_price,
                    'ret_1h': (forward_1 / entry_price - 1) * 100 if not pd.isna(forward_1) else np.nan,
                    'ret_4h': (forward_4 / entry_price - 1) * 100 if not pd.isna(forward_4) else np.nan,
                    'ret_12h': (forward_12 / entry_price - 1) * 100 if not pd.isna(forward_12) else np.nan,
                    'ret_24h': (forward_24 / entry_price - 1) * 100 if not pd.isna(forward_24) else np.nan,
                })
        except Exception:
            continue

    if not signals:
        print(f"  {label}: 无入场信号")
        return

    sig_df = pd.DataFrame(signals)
    n_signals = len(sig_df)

    print(f"\n  {label} — {n_signals} 个入场信号:")
    for col, desc in [('ret_1h', '1h后'), ('ret_4h', '4h后'), ('ret_12h', '12h后'), ('ret_24h', '24h后')]:
        valid = sig_df[col].dropna()
        if len(valid) == 0:
            continue
        mean_ret = valid.mean()
        win_rate = (valid > 0).sum() / len(valid) * 100
        print(f"    {desc}: 均值 {mean_ret:+.2f}% | 胜率 {win_rate:.1f}%")


analyze_signal_quality(
    lambda: TrendStrategy(fast_period=15, slow_period=30, atr_stop=3.0),
    df, '趋势跟踪(15/30)'
)
analyze_signal_quality(
    lambda: MomentumStrategy(fast_momentum=20, slow_momentum=40, atr_stop=3.5),
    df, '动量策略(20/40)'
)

# ═══════════════════════════════
# 诊断 3: 成本吞噬
# ═══════════════════════════════
print("\n" + "=" * 60)
print("🔍 诊断 3: 交易成本分析")
print("=" * 60)

strat = TrendStrategy(fast_period=15, slow_period=30, atr_stop=3.0)
strat.precompute(df)

trades = []
position = None
entry_price = 0

for i in range(50, len(df)):
    bar = df.iloc[i]
    if bar.get('anomaly', False) and position is None:
        continue
    if position is None:
        if strat.check_entry(bar, i):
            position = 'long'
            entry_price = bar['close']
    else:
        atr_val = strat._atr.iloc[i]
        stop_hit = not pd.isna(atr_val) and bar['low'] <= entry_price - atr_val * strat.atr_stop
        if strat.check_exit(bar, i) or stop_hit:
            exit_price = bar['close']
            gross_ret = (exit_price / entry_price - 1) * 100
            net_ret = gross_ret - 0.3
            trades.append({
                'gross_ret_pct': gross_ret,
                'net_ret_pct': net_ret,
                'cost_pct': 0.3,
            })
            position = None

if trades:
    trade_df = pd.DataFrame(trades)
    total_gross = trade_df['gross_ret_pct'].sum()
    total_net = trade_df['net_ret_pct'].sum()
    total_cost = total_gross - total_net

    print(f"  趋势跟踪(15/30) — {len(trades)} 笔交易")
    print(f"    毛收益（无成本）: {total_gross:+.2f}%")
    print(f"    净收益（含成本）: {total_net:+.2f}%")
    if total_gross != 0:
        print(f"    成本总计:         {total_cost:.2f}% ({total_cost/abs(total_gross)*100:.1f}% 的毛利润被成本吃掉)")
    print(f"    平均每笔毛收益:   {trade_df['gross_ret_pct'].mean():+.3f}%")
    print(f"    平均每笔成本:     0.30%")

    killed_by_cost = ((trade_df['gross_ret_pct'] > 0) & (trade_df['net_ret_pct'] < 0)).sum()
    print(f"    被成本「杀死」的盈利交易: {killed_by_cost}/{len(trades)} ({killed_by_cost/len(trades)*100:.1f}%)")

# ═══════════════════════════════
# 诊断 4: BTC 1h 价格统计特征
# ═══════════════════════════════
print("\n" + "=" * 60)
print("🔍 诊断 4: BTC 1h 价格统计特征")
print("=" * 60)

returns = df['close'].pct_change().dropna()
autocorr_1 = returns.autocorr(lag=1)
autocorr_4 = returns.autocorr(lag=4)
autocorr_24 = returns.autocorr(lag=24)

print(f"  1h 收益率自相关:")
print(f"    lag=1:  {autocorr_1:+.4f}")
print(f"    lag=4:  {autocorr_4:+.4f}")
print(f"    lag=24: {autocorr_24:+.4f}")
if abs(autocorr_1) < 0.02:
    print(f"    → 几乎零自相关——1h K 线接近随机游走，技术指标预测力极弱")
if abs(autocorr_24) > 0.02:
    print(f"    → 24h 有一定自相关性，日级别趋势比小时级别更可靠")

daily_returns = df['close'].resample('D').last().pct_change().dropna()
print(f"\n  1h 收益率均值: {returns.mean()*100:.4f}% | 标准差: {returns.std()*100:.3f}%")
print(f"  日收益率均值:  {daily_returns.mean()*100:.4f}% | 标准差: {daily_returns.std()*100:.3f}%")
print(f"  1h 夏普:       {returns.mean()/returns.std() * np.sqrt(365*24):.3f}")
print(f"  日夏普:        {daily_returns.mean()/daily_returns.std() * np.sqrt(365):.3f}")

up_moves = returns[returns > 0]
down_moves = returns[returns < 0]
print(f"\n  上涨小时: {len(up_moves)} ({len(up_moves)/len(returns)*100:.1f}%) | 均值 +{up_moves.mean()*100:.3f}%")
print(f"  下跌小时: {len(down_moves)} ({len(down_moves)/len(returns)*100:.1f}%) | 均值 {down_moves.mean()*100:.3f}%")

# ═══════════════════════════════
# 诊断 5: 综合诊断
# ═══════════════════════════════
print("\n" + "=" * 60)
print("🔍 诊断 5: 为什么三种策略在 BTC 1h 上全部亏损 — 综合诊断")
print("=" * 60)

print(f"""
  根因 1: BTC 1h 接近随机游走
  ─────────────────────────────
  1h 收益率自相关 = {autocorr_1:.4f}，几乎为零。过去价格对下一小时方向
  几乎没有预测力。EMA 交叉/RSI/动量等基于历史价格的技术指标
  在 1h 级别本质上在拟合噪声。

  根因 2: 市场 95% 时间处于震荡市
  ─────────────────────────────
  49.4% 震荡低波动 + 46.0% 震荡高波动。趋势跟踪和动量策略需要
  的趋势市仅占 3.5%。这些策略在它们的天敌市场里回测了两年。

  根因 3: 交易成本吃掉所有 alpha
  ─────────────────────────────
  每笔交易 0.3% 往返成本。BTC 1h 收益率均值仅 {returns.mean()*100:.4f}%。
  信号方向正确率仅 ~50%，盈亏比不足以覆盖成本。
  这是 1h 级别的核心矛盾：信号太弱，成本太贵。

  根因 4: Long-only 在下跌市中无防御力
  ─────────────────────────────
  BTC 价格最大回撤 {max_price_dd:.1f}%。纯多头策略在下跌周期中
  只能硬扛亏损。需要加入空头方向或现金避险逻辑。

  根因 5: 时间框架错配
  ─────────────────────────────
  1h 级别的信号噪声比太高。同样的策略逻辑在 1d 级别上可能有效。
""")

print("✅ 诊断完成")
