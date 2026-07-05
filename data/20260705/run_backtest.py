"""
Day 2 回测入口 — 对 BTC 1h 数据运行三种策略，生成对比报告

三种策略：
  1. 趋势跟踪 — EMA 20/50 交叉 + ATR 止损
  2. 动量策略 — 双层时间框架动量确认

用法：
    python run_backtest.py
"""

import pandas as pd
import numpy as np
import sys
import os

# Windows GBK 编码兼容
if sys.platform == 'win32':
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    sys.stderr.reconfigure(encoding='utf-8', errors='replace')

# 添加当前目录到 path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from backtest.engine import BacktestEngine
from backtest.strategies.trend import TrendStrategy
from backtest.strategies.momentum import MomentumStrategy

# ═══════════════════════════════
# 配置
# ═══════════════════════════════
DATA_DIR = os.path.dirname(os.path.abspath(__file__))
CLEAN_DIR = os.path.join(DATA_DIR, 'clean')

SYMBOL = 'BTC/USDT'
TIMEFRAME = '1h'
INITIAL_CAPITAL = 10000
SPLIT_RATIO = 0.7  # 70% 样本内 / 30% 样本外


def run_strategy(strategy, df, label):
    """运行一个策略并返回结果"""
    engine = BacktestEngine(strategy, initial_capital=INITIAL_CAPITAL, split_ratio=SPLIT_RATIO)
    try:
        results = engine.run(df)
    except Exception as e:
        print(f"  ❌ 策略 {label} 运行失败: {e}")
        import traceback
        traceback.print_exc()
        return None
    return results


def print_comparison(all_results):
    """打印三种策略对比表"""
    print("\n")
    print("╔" + "═" * 78 + "╗")
    print("║" + "  📊 三种策略对比报告".ljust(60) + "║")
    print("╠" + "═" * 78 + "╣")
    print(f"║  标的: {SYMBOL} {TIMEFRAME}  |  初始资金: {INITIAL_CAPITAL:,} USDT  |  样本内 {SPLIT_RATIO:.0%}".ljust(72) + "║")
    print("╚" + "═" * 78 + "╝")
    print()

    # ── 表头 ──
    header = f"{'指标':<24} {'趋势跟踪':>14} {'动量策略':>14}"
    print(header)
    print("─" * len(header))

    # ── 关键指标列表 ──
    metrics_to_show = [
        ('total_return_pct', '总收益率 (%)', '{:.2f}%'),
        ('annual_return_pct', '年化收益率 (%)', '{:.2f}%'),
        ('annual_vol_pct', '年化波动率 (%)', '{:.2f}%'),
        ('sharpe', '夏普比率', '{:.3f}'),
        ('max_drawdown_pct', '最大回撤 (%)', '{:.2f}%'),
        ('calmar', 'Calmar 比率', '{:.3f}'),
        ('total_trades', '总交易笔数', '{:.0f}'),
        ('win_rate', '胜率 (%)', '{:.1f}%'),
        ('profit_loss_ratio', '盈亏比', '{:.2f}'),
        ('profit_factor', '利润率因子', '{}'),
        ('avg_bars_held', '平均持仓 (K线)', '{:.1f}'),
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

    # ── 样本内外对比 ──
    print(f"\n{'─' * 70}")
    print("  🔍 样本内 vs 样本外 夏普对比")
    print(f"  {'策略':<20} {'样本内夏普':>12} {'样本外夏普':>12} {'OOS/IS':>10} {'判定':>10}")
    print(f"  {'─' * 60}")

    for name in ['趋势跟踪', '动量策略']:
        if name not in all_results or all_results[name] is None:
            continue
        r = all_results[name]
        is_sharpe = r['in_sample']['sharpe']
        oos_sharpe = r['out_of_sample']['sharpe']
        ratio = oos_sharpe / is_sharpe if is_sharpe > 0 else 0
        warning = '🔴 过拟合' if ratio < 0.5 else ('⚠️ 注意' if ratio < 0.7 else '✅ 正常')
        print(f"  {name:<20} {is_sharpe:>12.3f} {oos_sharpe:>12.3f} {ratio:>9.1%} {warning:>10}")

    # ── 异常 K 线影响 ──
    print(f"\n{'─' * 70}")
    print("  🚩 异常 K 线跳过统计")
    for name in ['趋势跟踪', '动量策略']:
        if name not in all_results or all_results[name] is None:
            continue
        r = all_results[name]
        skipped = r.get('anomaly_skipped', 0)
        total = r.get('total_bars', 0)
        print(f"  {name:<20} 跳过 {skipped:>5} / {total:>6} 根异常 K 线 ({skipped/total*100:.2f}%)")


def main():
    # ── 加载数据 ──
    filepath = os.path.join(CLEAN_DIR, f"BTCUSDT_1h.parquet")
    if not os.path.exists(filepath):
        print(f"❌ 数据文件不存在: {filepath}")
        print("   请先运行 clean_data.py")
        sys.exit(1)

    df = pd.read_parquet(filepath)
    print(f"📂 加载: {filepath} ({len(df):,} 行)")
    print(f"   时间范围: {df.index[0]} → {df.index[-1]}")
    print(f"   异常标记: {df['anomaly'].sum()} 根 K 线")

    # ── 创建策略实例 ──
    strategies = {
        '趋势跟踪': TrendStrategy(fast_period=20, slow_period=50, atr_stop=2.0),
        '动量策略': MomentumStrategy(fast_momentum=20, slow_momentum=50, atr_stop=2.5),
    }

    # ── 预计算指标（所有策略共享同一份数据）──
    for name, strat in strategies.items():
        print(f"\n🔧 预计算指标: {name}...")
        strat.precompute(df)

    # ── 逐个运行回测 ──
    all_results = {}
    for name, strat in strategies.items():
        print(f"\n{'═' * 60}")
        results = run_strategy(strat, df, name)
        all_results[name] = results

    # ── 输出对比报告 ──
    print_comparison(all_results)

    # ── 保存交易日志 ──
    logs_dir = os.path.join(DATA_DIR, 'logs')
    os.makedirs(logs_dir, exist_ok=True)
    for name in ['趋势跟踪', '动量策略']:
        if name not in all_results or all_results[name] is None:
            continue
        trade_log = all_results[name]['trade_log']
        if len(trade_log) > 0:
            filepath = os.path.join(logs_dir, f"trades_{name}.csv")
            trade_log.to_csv(filepath, index=False, encoding='utf-8-sig')
            print(f"\n📂 交易日志已保存: {filepath}")

    print(f"\n✅ Day 2 回测完成")


if __name__ == '__main__':
    main()
