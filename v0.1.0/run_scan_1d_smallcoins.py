"""
1d 参数扫描 + 小币种回测

三大任务：
  1. BTC 1d 趋势跟踪参数网格扫描（聚焦趋势——OOS 夏普最高的策略）
  2. WIF / PEPE 1d 双向策略回测 + 与 BTC 对比
  3. 小币种策略适配建议

用法：
    python run_scan_1d_smallcoins.py
"""

import pandas as pd
import numpy as np
import sys
import os
import itertools
from datetime import datetime

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

# ===============================
# Part 1: BTC 1d 参数扫描
# ===============================

# 聚焦趋势跟踪（1h→1d 后表现最好的策略），但三种都扫
TREND_GRID_1D = {
    'fast_period': [3, 5, 8, 10, 13],
    'slow_period': [15, 20, 30, 40, 50],
    'atr_stop': [1.5, 2.0, 2.5, 3.0, 3.5],
}

MOM_GRID_1D = {
    'fast_momentum': [5, 7, 10, 15],
    'slow_momentum': [20, 30, 40, 50],
    'atr_stop': [2.0, 2.5, 3.0, 3.5],
}


def run_scan_1d(df, strategy_factory, param_grid, label, param_names):
    """1d 参数扫描（数据量小，速度快）"""
    results = []
    total = len(param_grid)
    start_time = datetime.now()

    print(f"\n{'─' * 65}")
    print(f"  [SCAN] {label} — {total} 种组合")
    print(f"  {'─' * 65}")

    for idx, params in enumerate(param_grid):
        strat = strategy_factory(*params)
        strat.precompute(df)
        engine = BacktestEngine(strat, initial_capital=INITIAL_CAPITAL, split_ratio=SPLIT_RATIO)
        result = engine.run(df)

        is_m = result['in_sample']
        oos_m = result['out_of_sample']
        full_m = result['full_sample']

        results.append({
            'params': params,
            'param_str': ', '.join(f'{n}={v}' for n, v in zip(param_names, params)),
            'is_sharpe': is_m['sharpe'],
            'oos_sharpe': oos_m['sharpe'],
            'full_sharpe': full_m['sharpe'],
            'oos_return': oos_m['total_return_pct'],
            'full_return': full_m['total_return_pct'],
            'max_dd': full_m['max_drawdown_pct'],
            'win_rate': full_m['win_rate'],
            'total_trades': full_m['total_trades'],
            'calmar': full_m['calmar'],
        })

        pct = (idx + 1) / total * 100
        bar_len = 25
        filled = int(bar_len * (idx + 1) / total)
        bar = '#' * filled + '.' * (bar_len - filled)
        print(f"\r    [{bar}] {pct:5.1f}% | {idx+1}/{total}", end='', flush=True)

    print()
    results.sort(key=lambda x: x['oos_sharpe'], reverse=True)
    return results


def print_top(results, label, top_n=10):
    """打印 Top-N 参数组合"""
    if not results:
        return

    positive = sum(1 for r in results if r['oos_sharpe'] > 0)
    print(f"\n  [BEST] {label} — Top-{top_n}")
    print(f"  {'Rank':<5} {'参数':<45} {'OOS夏普':>8} {'OOS收益%':>9} {'全夏普':>8} {'交易':>6} {'胜率%':>7}")
    print(f"  {'─' * 90}")

    for rank, r in enumerate(results[:top_n], 1):
        print(f"  {rank:<5} {r['param_str']:<45} {r['oos_sharpe']:>8.3f} {r['oos_return']:>8.2f}% {r['full_sharpe']:>8.3f} {r['total_trades']:>6.0f} {r['win_rate']:>6.1f}%")

    print(f"\n  [STAT] OOS夏普>0: {positive}/{len(results)} | "
          f"均值: {np.mean([r['oos_sharpe'] for r in results]):.3f} | "
          f"中位数: {np.median([r['oos_sharpe'] for r in results]):.3f} | "
          f"最大: {max(r['oos_sharpe'] for r in results):.3f}")


# ===============================
# Part 2: 小币种回测
# ===============================

def run_smallcoin_backtest(df, symbol, strategies):
    """对小币种运行三种策略，返回对比结果"""
    results = {}
    for name, strat_factory in strategies.items():
        strat = strat_factory()
        strat.precompute(df)
        engine = BacktestEngine(strat, initial_capital=INITIAL_CAPITAL, split_ratio=SPLIT_RATIO)
        result = engine.run(df)
        results[name] = result
    return results


def print_smallcoin_comparison(btc_results, wif_results, pepe_results, label):
    """打印 BTC vs WIF vs PEPE 三方对比"""
    print(f"\n  {'─' * 90}")
    print(f"  [STAT] {label}")
    print(f"  {'指标':<20} {'BTC':>14} {'WIF':>14} {'PEPE':>14}")
    print(f"  {'─' * 68}")

    for key, desc, fmt in [
        ('full_sharpe', '夏普', '{:.3f}'),
        ('total_return_pct', '总收益%', '{:.1f}%'),
        ('max_drawdown_pct', '最大回撤%', '{:.1f}%'),
        ('win_rate', '胜率%', '{:.1f}%'),
        ('total_trades', '交易笔数', '{:.0f}'),
    ]:
        row = f"  {desc:<20}"
        for results in [btc_results, wif_results, pepe_results]:
            if results:
                val = results.get(key, 'N/A') if isinstance(results, dict) else results['full_sample'].get(key, 'N/A')
                try:
                    row += f" {fmt.format(val):>14}"
                except:
                    row += f" {str(val):>14}"
            else:
                row += f" {'—':>14}"
        print(row)


def main():
    # ── 加载 BTC 1d ──
    btc_1d = pd.read_parquet(os.path.join(CLEAN_DIR, 'BTCUSDT_1d.parquet'))
    print(f"[LOAD] BTC 1d: {len(btc_1d):,} 行")

    # ===============================
    # Part 1: 参数扫描
    # ===============================
    print("\n" + "=" * 80)
    print("  Part 1: BTC 1d 参数扫描（趋势跟踪 + 动量）")
    print("=" * 80)

    # 趋势跟踪
    trend_grid = [
        (f, s, a) for f, s, a in itertools.product(
            TREND_GRID_1D['fast_period'],
            TREND_GRID_1D['slow_period'],
            TREND_GRID_1D['atr_stop']
        ) if f < s
    ]
    print(f"\n  趋势跟踪: {len(trend_grid)} 组合 (fast<slow 过滤)")
    trend_results = run_scan_1d(btc_1d, lambda f, s, a: TrendStrategy(f, s, a), trend_grid,
                                '趋势跟踪', ['fast', 'slow', 'atr'])
    print_top(trend_results, '趋势跟踪')

    # 动量
    mom_grid = [
        (f, s, a) for f, s, a in itertools.product(
            MOM_GRID_1D['fast_momentum'],
            MOM_GRID_1D['slow_momentum'],
            MOM_GRID_1D['atr_stop']
        ) if f < s
    ]
    print(f"\n  动量策略: {len(mom_grid)} 组合")
    mom_results = run_scan_1d(btc_1d, lambda f, s, a: MomentumStrategy(f, s, a), mom_grid,
                              '动量策略', ['fast_mom', 'slow_mom', 'atr'])
    print_top(mom_results, '动量策略')

    # ── 综合 Top-5 ──
    print(f"\n{'=' * 80}")
    print(f"  [BEST][BEST] 跨策略 Top-5（按 OOS 夏普）")
    all_top = []
    for r in trend_results[:5]:
        all_top.append(('趋势跟踪', r))
    for r in mom_results[:5]:
        all_top.append(('动量策略', r))
    all_top.sort(key=lambda x: x[1]['oos_sharpe'], reverse=True)

    for rank, (name, r) in enumerate(all_top[:5], 1):
        print(f"  {rank}. {name:<12} {r['param_str']:<45} OOS夏普={r['oos_sharpe']:.3f}  收益={r['oos_return']:.1f}%")

    # ===============================
    # Part 2: 小币种回测
    # ===============================
    print("\n" + "=" * 80)
    print("  Part 2: 小币种 (WIF / PEPE) 1d 双向策略回测")
    print("=" * 80)

    # 使用 BTC 最优参数
    best_trend = trend_results[0]['params'] if trend_results else (5, 20, 2.0)
    best_mom = mom_results[0]['params'] if mom_results else (10, 30, 2.5)

    print(f"\n  使用 BTC 最优参数:")
    print(f"    趋势跟踪: fast={best_trend[0]}, slow={best_trend[1]}, atr={best_trend[2]}")
    print(f"    动量策略: fast={best_mom[0]}, slow={best_mom[1]}, atr={best_mom[2]}")

    # 定义策略工厂
    def trend_factory():
        return TrendStrategy(*best_trend)

    def mom_factory():
        return MomentumStrategy(*best_mom)

    strategies = {
        '趋势跟踪': trend_factory,
        '动量策略': mom_factory,
    }

    # BTC 基准（用最优参数重跑）
    print(f"\n{'─' * 40}")
    print("  [STAT] BTC 基准")
    btc_optimal = {}
    for name, factory in strategies.items():
        strat = factory()
        strat.precompute(btc_1d)
        engine = BacktestEngine(strat, initial_capital=INITIAL_CAPITAL, split_ratio=SPLIT_RATIO)
        btc_optimal[name] = engine.run(btc_1d)['full_sample']

    # WIF
    wif_1d = pd.read_parquet(os.path.join(CLEAN_DIR, 'WIFUSDT_1d.parquet'))
    wif_1d_cols = wif_1d.columns.tolist()
    print(f"\n{'─' * 40}")
    print(f"  [STAT] WIF/USDT 1d — {len(wif_1d)} 行 | {wif_1d.index[0]} → {wif_1d.index[-1]}")
    print(f"    起始价: ${wif_1d['close'].iloc[0]:.4f} → 结束价: ${wif_1d['close'].iloc[-1]:.4f}")
    print(f"    异常标记: {wif_1d['anomaly'].sum() if 'anomaly' in wif_1d.columns else 'N/A'} 天")
    wif_results = {}
    for name, factory in strategies.items():
        try:
            strat = factory()
            strat.precompute(wif_1d)
            engine = BacktestEngine(strat, initial_capital=INITIAL_CAPITAL, split_ratio=SPLIT_RATIO)
            r = engine.run(wif_1d)
            wif_results[name] = r['full_sample']
        except Exception as e:
            print(f"  [X] WIF {name}: {e}")
            wif_results[name] = None

    # PEPE
    pepe_1d = pd.read_parquet(os.path.join(CLEAN_DIR, 'PEPEUSDT_1d.parquet'))
    print(f"\n{'─' * 40}")
    print(f"  [STAT] PEPE/USDT 1d — {len(pepe_1d)} 行 | {pepe_1d.index[0]} → {pepe_1d.index[-1]}")
    print(f"    起始价: ${pepe_1d['close'].iloc[0]:.8f} → 结束价: ${pepe_1d['close'].iloc[-1]:.8f}")
    print(f"    异常标记: {pepe_1d['anomaly'].sum() if 'anomaly' in pepe_1d.columns else 'N/A'} 天")
    pepe_results = {}
    for name, factory in strategies.items():
        try:
            strat = factory()
            strat.precompute(pepe_1d)
            engine = BacktestEngine(strat, initial_capital=INITIAL_CAPITAL, split_ratio=SPLIT_RATIO)
            r = engine.run(pepe_1d)
            pepe_results[name] = r['full_sample']
        except Exception as e:
            print(f"  [X] PEPE {name}: {e}")
            pepe_results[name] = None

    # ── 三方对比 ──
    for name in ['趋势跟踪', '动量策略']:
        print_smallcoin_comparison(
            btc_optimal.get(name),
            wif_results.get(name),
            pepe_results.get(name),
            f'{name} — BTC vs WIF vs PEPE'
        )

    # ── 价格变动背景 ──
    print(f"\n{'=' * 80}")
    print(f"  [UP] 两年价格变动背景")
    print(f"  {'币种':<12} {'起始价':>16} {'结束价':>16} {'涨幅':>12} {'波动率':>12}")
    print(f"  {'─' * 70}")
    for name, df_coin in [('BTC', btc_1d), ('WIF', wif_1d), ('PEPE', pepe_1d)]:
        start_p = df_coin['close'].iloc[0]
        end_p = df_coin['close'].iloc[-1]
        chg = (end_p / start_p - 1) * 100
        vol = df_coin['close'].pct_change().std() * np.sqrt(365)
        print(f"  {name:<12} ${start_p:>15.6f} ${end_p:>15.6f} {chg:>+11.1f}% {vol:>11.1f}%")

    print(f"\n[OK] 扫描 + 小币种回测完成")


if __name__ == '__main__':
    main()
