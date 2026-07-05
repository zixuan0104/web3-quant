"""
P0+P1 优化对比 — 移动止盈 + ADX 过滤 + 宽止损 vs 原始版本

P0: 移动止盈 (TRAILING_ACTIVATION=2.0 ATR, TRAILING_DISTANCE=1.5 ATR)
P1: ADX 震荡市过滤 (ADX < 20 不交易) + 止损从 ATR(2.0) 放宽到 ATR(3.0)

用法：
    python compare_p0p1.py
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

# ═══════════════════════════════
# 原始参数（优化前）
# ═══════════════════════════════
BEST_TREND = {'fast_period': 10, 'slow_period': 40, 'atr_stop': 2.0}
BEST_MOM = {'fast_momentum': 10, 'slow_momentum': 20, 'atr_stop': 2.0}


def run_original(strategy_class, params, df, label):
    """原始版本：无移动止盈，无ADX过滤"""
    s = strategy_class(**params)
    s.ENABLE_TRAILING_STOP = False
    s.ENABLE_ADX_FILTER = False
    s.precompute(df)
    engine = BacktestEngine(s, initial_capital=INITIAL_CAPITAL, split_ratio=SPLIT_RATIO)
    return engine.run(df)


def run_optimized(strategy_class, params, df, label):
    """P0+P1 优化版：移动止盈 + ADX过滤 + 宽止损"""
    p = params.copy()
    # P1: 放宽止损
    p['atr_stop'] = 3.0 if 'atr_stop' in p else 3.0
    s = strategy_class(**p)
    # P0 + P1 默认已启用（基类 ENABLE_TRAILING_STOP=True, ENABLE_ADX_FILTER=True）
    s.precompute(df)
    engine = BacktestEngine(s, initial_capital=INITIAL_CAPITAL, split_ratio=SPLIT_RATIO)
    return engine.run(df)


def print_side_by_side(orig, opt, label):
    """并排对比原始 vs 优化"""
    o = orig['full_sample']
    p = opt['full_sample']

    def delta(key):
        return p.get(key, 0) - o.get(key, 0)

    print(f"\n  {'─' * 80}")
    print(f"  {label}")
    print(f"  {'指标':<24} {'原始':>12} {'P0+P1':>12} {'变化':>12}")
    print(f"  {'─' * 65}")

    rows = [
        ('总收益率 (%)', 'total_return_pct', '{:.1f}%'),
        ('夏普比率', 'sharpe', '{:.3f}'),
        ('最大回撤 (%)', 'max_drawdown_pct', '{:.1f}%'),
        ('Calmar', 'calmar', '{:.3f}'),
        ('总交易笔数', 'total_trades', '{:.0f}'),
        ('胜率 (%)', 'win_rate', '{:.1f}%'),
        ('盈亏比', 'profit_loss_ratio', '{:.2f}'),
    ]

    for label_text, key, fmt in rows:
        orig_val = o.get(key, 0)
        opt_val = p.get(key, 0)
        d = opt_val - orig_val
        try:
            line = f"  {label_text:<24} {fmt.format(orig_val):>12} {fmt.format(opt_val):>12} "
        except:
            line = f"  {label_text:<24} {str(orig_val):>12} {str(opt_val):>12} "
        if d > 0:
            line += f"\033[32m{d:+.1f}\033[0m" if isinstance(d, float) else f"  {d}"
        elif d < 0:
            line += f"\033[31m{d:+.1f}\033[0m" if isinstance(d, float) else f"  {d}"
        else:
            line += "  0"
        print(line)

    # ── 出场原因对比 ──
    print(f"\n  🚪 出场原因分布:")
    for reason in ['trailing_stop', 'stop_loss', 'signal', 'take_profit']:
        orig_count = len(orig['trade_log'][orig['trade_log']['exit_reason'] == reason]) if len(orig['trade_log']) > 0 else 0
        opt_count = len(opt['trade_log'][opt['trade_log']['exit_reason'] == reason]) if len(opt['trade_log']) > 0 else 0
        if orig_count > 0 or opt_count > 0:
            print(f"    {reason:<16}: 原始 {orig_count:>3}笔 → P0+P1 {opt_count:>3}笔")

    # ── 曾盈利→亏损 改善 ──
    orig_losers = orig['trade_log'][orig['trade_log']['net_return_pct'] <= 0] if len(orig['trade_log']) > 0 else pd.DataFrame()
    opt_losers = opt['trade_log'][opt['trade_log']['net_return_pct'] <= 0] if len(opt['trade_log']) > 0 else pd.DataFrame()
    orig_was_profitable = (orig_losers['mfe_pct'] > 0).sum() if len(orig_losers) > 0 else 0
    opt_was_profitable = (opt_losers['mfe_pct'] > 0).sum() if len(opt_losers) > 0 else 0
    print(f"\n  📉 浮盈→亏损 改善:")
    print(f"    原始: {orig_was_profitable}/{len(orig_losers)} 笔亏损曾盈利")
    print(f"    P0+P1: {opt_was_profitable}/{len(opt_losers)} 笔亏损曾盈利")


def main():
    print("╔" + "═" * 78 + "╗")
    print("║  P0+P1 优化对比 — 移动止盈 + ADX过滤 + 宽止损 vs 原始".ljust(72) + "║")
    print("╚" + "═" * 78 + "╝")

    for symbol in ['BTC/USDT', 'ETH/USDT', 'SOL/USDT']:
        symbol_safe = symbol.replace('/', '')
        filepath = os.path.join(CLEAN_DIR, f'{symbol_safe}_1d.parquet')
        if not os.path.exists(filepath):
            continue

        df = pd.read_parquet(filepath)
        print(f"\n{'═' * 80}")
        print(f"  📂 {symbol}")
        print(f"{'═' * 80}")

        # ── 趋势跟踪 ──
        print(f"\n  ── 趋势跟踪 EMA({BEST_TREND['fast_period']}/{BEST_TREND['slow_period']}) ──")
        orig = run_original(TrendStrategy, BEST_TREND, df, '原始')
        opt = run_optimized(TrendStrategy, BEST_TREND, df, 'P0+P1')
        print_side_by_side(orig, opt, '趋势跟踪')

        # ── 动量策略 ──
        print(f"\n  ── 动量策略 MOM({BEST_MOM['fast_momentum']}/{BEST_MOM['slow_momentum']}) ──")
        orig_m = run_original(MomentumStrategy, BEST_MOM, df, '原始')
        opt_m = run_optimized(MomentumStrategy, BEST_MOM, df, 'P0+P1')
        print_side_by_side(orig_m, opt_m, '动量策略')

    print(f"\n✅ 对比完成")


if __name__ == '__main__':
    main()
