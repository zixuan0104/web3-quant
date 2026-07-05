"""
参数扫描 — 网格搜索三种策略最优参数

对每种策略定义参数网格，穷举所有组合，按样本外夏普排序输出 Top-N。
同时检查过拟合：标记 OOS 夏普 < IS 夏普 50% 的组合。

用法：
    python parameter_scan.py
"""

import pandas as pd
import numpy as np
import sys
import os
import warnings
import itertools
from datetime import datetime

warnings.filterwarnings('ignore')

if sys.platform == 'win32':
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from backtest.engine import BacktestEngine
from backtest.strategies.trend import TrendStrategy
from backtest.strategies.momentum import MomentumStrategy

# ═══════════════════════════════
# 配置
# ═══════════════════════════════
CLEAN_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'clean')
SYMBOL = 'BTC/USDT'
TIMEFRAME = '1h'
INITIAL_CAPITAL = 10000
SPLIT_RATIO = 0.7
TOP_N = 10  # 每种策略输出 Top-10 参数组合

# ═══════════════════════════════
# 参数网格
# ═══════════════════════════════

TREND_GRID = {
    'fast_period': [10, 15, 20, 30, 40],
    'slow_period': [30, 40, 50, 60, 80],
    'atr_stop': [1.5, 2.0, 2.5, 3.0],
}
# 约束：fast < slow，否则无意义
TREND_GRID_FILTERED = [
    (f, s, a) for f, s, a in itertools.product(
        TREND_GRID['fast_period'],
        TREND_GRID['slow_period'],
        TREND_GRID['atr_stop']
    ) if f < s
]
# 5×5×4 = 100 → 过滤后约 50

MOMENTUM_GRID = {
    'fast_momentum': [10, 15, 20, 30],
    'slow_momentum': [30, 40, 50, 60],
    'atr_stop': [2.0, 2.5, 3.0, 3.5],
}
MOMENTUM_GRID_FILTERED = [
    (f, s, a) for f, s, a in itertools.product(
        MOMENTUM_GRID['fast_momentum'],
        MOMENTUM_GRID['slow_momentum'],
        MOMENTUM_GRID['atr_stop']
    ) if f < s
]
# 4×4×4 = 64 → 过滤后约 32


def run_scan(df, strategy_factory, param_grid, label):
    """
    参数网格扫描

    strategy_factory: 函数，接收参数 dict 返回策略实例
    param_grid: [(p1, p2, p3), ...] 参数组合列表
    """
    results = []
    total = len(param_grid)
    start_time = datetime.now()

    print(f"\n{'═' * 60}")
    print(f"🔍 {label} — 参数扫描 ({total} 种组合)")
    print(f"{'═' * 60}")

    for idx, params in enumerate(param_grid):
        strat = strategy_factory(*params)
        strat.precompute(df)

        engine = BacktestEngine(strat, initial_capital=INITIAL_CAPITAL, split_ratio=SPLIT_RATIO)
        result = engine.run(df)

        is_metrics = result['in_sample']
        oos_metrics = result['out_of_sample']
        full_metrics = result['full_sample']

        results.append({
            'params': params,
            'param_str': str(params),
            'is_sharpe': is_metrics['sharpe'],
            'oos_sharpe': oos_metrics['sharpe'],
            'full_sharpe': full_metrics['sharpe'],
            'is_return': is_metrics['total_return_pct'],
            'oos_return': oos_metrics['total_return_pct'],
            'full_return': full_metrics['total_return_pct'],
            'is_maxdd': is_metrics['max_drawdown_pct'],
            'oos_maxdd': oos_metrics['max_drawdown_pct'],
            'win_rate': full_metrics['win_rate'],
            'total_trades': full_metrics['total_trades'],
            'profit_factor': full_metrics['profit_factor'],
            'oos_is_ratio': oos_metrics['sharpe'] / is_metrics['sharpe'] if is_metrics['sharpe'] > 0 else float('inf'),
        })

        # 进度条
        pct = (idx + 1) / total * 100
        bar_len = 30
        filled = int(bar_len * (idx + 1) / total)
        bar = '█' * filled + '░' * (bar_len - filled)
        elapsed = (datetime.now() - start_time).total_seconds()
        eta = elapsed / (idx + 1) * (total - idx - 1)
        print(f"\r  [{bar}] {pct:5.1f}% | {idx+1}/{total} | ETA: {eta:.0f}s", end='', flush=True)

    print()  # newline after progress bar

    # ── 按样本外夏普排序 ──
    results.sort(key=lambda x: x['oos_sharpe'], reverse=True)

    return results


def print_scan_results(results, param_names, label):
    """打印扫描结果 Top-N"""
    print(f"\n{'─' * 80}")
    print(f"  🏆 {label} — Top-{TOP_N} (按样本外夏普排序)")
    print(f"  {'Rank':<5} {'参数':<35} {'IS夏普':>8} {'OOS夏普':>8} {'OOS/IS':>8} {'全收益%':>9} {'胜率%':>8} {'交易':>6} {'判定':>10}")
    print(f"  {'─' * 75}")

    for rank, r in enumerate(results[:TOP_N], 1):
        oos_is = r['oos_is_ratio']
        if oos_is == float('inf') or np.isinf(oos_is):
            verdict = '⚠️ IS≤0'
        elif oos_is < 0.5:
            verdict = '🔴 过拟合'
        elif oos_is < 0.7:
            verdict = '🟡 可疑'
        else:
            verdict = '✅ 稳定'

        # 格式化参数
        param_parts = []
        for i, name in enumerate(param_names):
            param_parts.append(f"{name}={r['params'][i]}")
        param_str = ', '.join(param_parts)

        print(f"  {rank:<5} {param_str:<35} {r['is_sharpe']:>8.3f} {r['oos_sharpe']:>8.3f} {oos_is:>7.1%} {r['full_return']:>8.2f}% {r['win_rate']:>7.1f}% {r['total_trades']:>6.0f} {verdict:>10}")

    # ── 统计 ──
    positive_oos = sum(1 for r in results if r['oos_sharpe'] > 0)
    stable = sum(1 for r in results if r['oos_is_ratio'] >= 0.7 and r['oos_sharpe'] > 0)

    print(f"\n  📊 统计: {len(results)} 组合 | OOS夏普>0: {positive_oos} 个 | 样本内外一致: {stable} 个")
    print(f"  📊 OOS夏普 均值: {np.mean([r['oos_sharpe'] for r in results]):.3f} | "
          f"中位数: {np.median([r['oos_sharpe'] for r in results]):.3f} | "
          f"最大: {max(r['oos_sharpe'] for r in results):.3f}")


def main():
    # ── 加载数据 ──
    filepath = os.path.join(CLEAN_DIR, f"BTCUSDT_1h.parquet")
    if not os.path.exists(filepath):
        print(f"❌ 数据不存在: {filepath}")
        sys.exit(1)

    df = pd.read_parquet(filepath)
    print(f"📂 数据: {len(df):,} 行 | {df.index[0]} → {df.index[-1]}")

    # ═══════════════════════
    # 趋势跟踪
    # ═══════════════════════
    trend_strategy_factory = lambda f, s, a: TrendStrategy(
        fast_period=f, slow_period=s, atr_stop=a
    )
    trend_results = run_scan(df, trend_strategy_factory, TREND_GRID_FILTERED, '趋势跟踪 (EMA交叉)')
    print_scan_results(trend_results, ['fast', 'slow', 'atr_stop'], '趋势跟踪')

    # ═══════════════════════
    # 动量
    # ═══════════════════════
    mom_strategy_factory = lambda f, s, a: MomentumStrategy(
        fast_momentum=f, slow_momentum=s, atr_stop=a
    )
    mom_results = run_scan(df, mom_strategy_factory, MOMENTUM_GRID_FILTERED, '动量策略 (双层动量)')
    print_scan_results(mom_results, ['fast_mom', 'slow_mom', 'atr_stop'], '动量策略')

    # ── 综合排名 ──
    print(f"\n{'═' * 80}")
    print(f"  🏆🏆 跨策略 Top-{TOP_N} (按 OOS 夏普)")
    print(f"  {'策略':<16} {'参数':<40} {'OOS夏普':>8} {'全收益%':>9} {'胜率%':>8}")

    all_top = []
    for r in trend_results[:TOP_N]:
        all_top.append(('趋势跟踪', r))
    for r in mom_results[:TOP_N]:
        all_top.append(('动量策略', r))

    all_top.sort(key=lambda x: x[1]['oos_sharpe'], reverse=True)

    for rank, (name, r) in enumerate(all_top[:TOP_N], 1):
        print(f"  {rank:<2} {name:<14} {r['param_str']:<40} {r['oos_sharpe']:>8.3f} {r['full_return']:>8.2f}% {r['win_rate']:>7.1f}%")

    print(f"\n✅ 参数扫描完成")


if __name__ == '__main__':
    main()
