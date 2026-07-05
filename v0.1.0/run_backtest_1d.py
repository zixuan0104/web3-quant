"""
Day 2 回测 — 日线级别 + 多空双向

对比维度：
  1. 1d vs 1h — 时间框架切换后的表现变化
  2. long-only vs long+short — 做空能力对策略的提升
  3. 三种策略在日线上的相对强弱

用法：
    python run_backtest_1d.py
"""

import pandas as pd
import numpy as np
import sys
import os

if sys.platform == 'win32':
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from backtest.engine import BacktestEngine
from backtest.strategies.trend import TrendStrategy
from backtest.strategies.momentum import MomentumStrategy

DATA_DIR = os.path.dirname(os.path.abspath(__file__))
CLEAN_DIR = os.path.join(DATA_DIR, 'clean')
INITIAL_CAPITAL = 10000
SPLIT_RATIO = 0.7


def run_strategy(strategy, df, label):
    """运行策略并返回结果"""
    engine = BacktestEngine(strategy, initial_capital=INITIAL_CAPITAL, split_ratio=SPLIT_RATIO)
    try:
        results = engine.run(df)
    except Exception as e:
        print(f"  ❌ {label} 失败: {e}")
        import traceback
        traceback.print_exc()
        return None
    return results


def print_direction_breakdown(trade_df, label):
    """打印多空交易拆解"""
    if trade_df is None or len(trade_df) == 0:
        return

    long_trades = trade_df[trade_df['side'] == 'long']
    short_trades = trade_df[trade_df['side'] == 'short']

    print(f"\n  {label} 多空拆解:")
    for side_name, side_df in [('做多', long_trades), ('做空', short_trades)]:
        if len(side_df) == 0:
            print(f"    {side_name}: 无交易")
            continue
        n = len(side_df)
        win_rate = (side_df['net_return_pct'] > 0).sum() / n * 100
        total_ret = side_df['net_return_pct'].sum()
        avg_ret = side_df['net_return_pct'].mean()
        print(f"    {side_name}: {n}笔 | 胜率 {win_rate:.1f}% | 累计 {total_ret:+.2f}% | 均值 {avg_ret:+.3f}%")


def main():
    # ── 加载 1d 数据 ──
    filepath = os.path.join(CLEAN_DIR, 'BTCUSDT_1d.parquet')
    if not os.path.exists(filepath):
        print(f"❌ 数据不存在: {filepath}")
        sys.exit(1)

    df = pd.read_parquet(filepath)
    print(f"📂 日线数据: {len(df):,} 行 | {df.index[0].strftime('%Y-%m-%d')} → {df.index[-1].strftime('%Y-%m-%d')}")
    print(f"   异常标记: {df['anomaly'].sum()} 天")

    # ═══════════════════════════════
    # 日线参数（与 1h 不同——日线趋势更慢，需要更短周期）
    # ═══════════════════════════════
    strategies = {
        '趋势跟踪': TrendStrategy(fast_period=5, slow_period=20, atr_stop=2.0),
        '动量策略': MomentumStrategy(fast_momentum=10, slow_momentum=30, atr_stop=2.5),
    }

    # ── 预计算 ──
    for name, strat in strategies.items():
        print(f"🔧 预计算: {name}...")
        strat.precompute(df)

    # ── 运行 ──
    all_results = {}
    for name, strat in strategies.items():
        print(f"\n{'═' * 60}")
        results = run_strategy(strat, df, name)
        all_results[name] = results

    # ═══════════════════════════════
    # 对比报告
    # ═══════════════════════════════
    print("\n")
    print("╔" + "═" * 78 + "╗")
    print("║" + "  📊 BTC 日线级别 + 多空双向 — 策略对比".ljust(64) + "║")
    print("╚" + "═" * 78 + "╝")

    header = f"{'指标':<24} {'趋势跟踪':>14} {'动量策略':>14}"
    print(header)
    print("─" * len(header))

    metrics_to_show = [
        ('total_return_pct', '总收益率 (%)', '{:.2f}%'),
        ('annual_return_pct', '年化收益率 (%)', '{:.2f}%'),
        ('sharpe', '夏普比率', '{:.3f}'),
        ('max_drawdown_pct', '最大回撤 (%)', '{:.2f}%'),
        ('calmar', 'Calmar 比率', '{:.3f}'),
        ('total_trades', '总交易笔数', '{:.0f}'),
        ('win_rate', '胜率 (%)', '{:.1f}%'),
        ('profit_loss_ratio', '盈亏比', '{:.2f}'),
        ('profit_factor', '利润率因子', '{}'),
        ('avg_bars_held', '平均持仓 (天)', '{:.1f}'),
        ('max_consecutive_losses', '最大连续亏损', '{:.0f}'),
    ]

    for key, label, fmt in metrics_to_show:
        row = f"  {label:<22}"
        for name in ['趋势跟踪', '动量策略']:
            if name in all_results and all_results[name] is not None:
                val = all_results[name]['full_sample'].get(key, 'N/A')
                try:
                    row += f" {fmt.format(val):>14}"
                except (ValueError, TypeError):
                    row += f" {str(val):>14}"
            else:
                row += f" {'—':>14}"
        print(row)

    print("─" * len(header))

    # ── 多空拆解 ──
    for name in ['趋势跟踪', '动量策略']:
        if name in all_results and all_results[name] is not None:
            print_direction_breakdown(all_results[name]['trade_log'], name)

    # ── 样本内外对比 ──
    print(f"\n{'─' * 70}")
    print("  🔍 样本内 vs 样本外 夏普对比")
    print(f"  {'策略':<20} {'样本内夏普':>12} {'样本外夏普':>12} {'OOS/IS':>10} {'判定':>10}")
    print(f"  {'─' * 60}")

    for name in ['趋势跟踪', '动量策略']:
        if name not in all_results or all_results[name] is None:
            continue
        r = all_results[name]
        is_s = r['in_sample']['sharpe']
        oos_s = r['out_of_sample']['sharpe']
        ratio = oos_s / is_s if is_s > 0 else float('inf')
        if is_s <= 0:
            warning = '⚠️ IS≤0'
        elif ratio < 0.5:
            warning = '🔴 过拟合'
        elif ratio < 0.7:
            warning = '🟡 可疑'
        else:
            warning = '✅ 稳定'
        print(f"  {name:<20} {is_s:>12.3f} {oos_s:>12.3f} {ratio:>9.1%} {warning:>10}")

    # ── 1d vs 1h 对比（使用之前 1h long-only 的结果）──
    print(f"\n{'─' * 70}")
    print("  📈 1d(多空双向) vs 1h(long-only) — 夏普对比")
    print(f"  {'策略':<16} {'1h long-only':>14} {'1d 多空双向':>14} {'提升':>10}")
    print(f"  {'─' * 52}")

    # 1h 结果（来自之前的 run_backtest.py）
    h1_results = {
        '趋势跟踪': -0.706,
        '动量策略': -1.313,
    }

    for name in ['趋势跟踪', '动量策略']:
        h1_sharpe = h1_results[name]
        if name in all_results and all_results[name] is not None:
            d1_sharpe = all_results[name]['full_sample']['sharpe']
            improvement = d1_sharpe - h1_sharpe
            print(f"  {name:<16} {h1_sharpe:>14.3f} {d1_sharpe:>14.3f} {improvement:>+9.3f}")

    print(f"\n✅ 日线回测完成")


if __name__ == '__main__':
    main()
