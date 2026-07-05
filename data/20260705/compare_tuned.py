"""
P0+P1 调参版对比 — 根据上一轮结果针对性调整

趋势跟踪: ADX过滤 ON, 初始止损 ATR(3.0), 移动止盈 ACTIVATION=3.0, DISTANCE=2.0
         （更宽的激活阈值和距离，让趋势跑得更远再开始保护利润）

动量策略: ADX过滤 OFF, 初始止损 ATR(2.0) 保持原值, 移动止盈 ACTIVATION=3.0, DISTANCE=2.0
         （不破坏信号出场逻辑，只在盈利足够大时才启用移动止盈保护）
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

BEST_TREND = {'fast_period': 10, 'slow_period': 40}
BEST_MOM = {'fast_momentum': 10, 'slow_momentum': 20}


def run_version(strategy_class, params, df, config):
    """运行指定配置的策略"""
    s = strategy_class(**params)
    s.ENABLE_TRAILING_STOP = config.get('trailing', True)
    s.ENABLE_ADX_FILTER = config.get('adx', True)
    s.TRAILING_ACTIVATION_ATR = config.get('trail_act', 2.0)
    s.TRAILING_DISTANCE_ATR = config.get('trail_dist', 1.5)
    s.precompute(df)
    engine = BacktestEngine(s, initial_capital=INITIAL_CAPITAL, split_ratio=SPLIT_RATIO)
    return engine.run(df)


def main():
    print("╔" + "═" * 78 + "╗")
    print("║  P0+P1 调参版 — 趋势放宽止盈 / 动量保留信号出场".ljust(72) + "║")
    print("╚" + "═" * 78 + "╝")

    # 三版配置
    configs = {
        '原始': {
            'trailing': False, 'adx': False, 'trail_act': 2.0, 'trail_dist': 1.5,
        },
        'P0+P1(激进)': {
            'trailing': True, 'adx': True, 'trail_act': 2.0, 'trail_dist': 1.5,
        },
        'P0+P1(调参)': {
            'trailing': True, 'adx': True, 'trail_act': 3.0, 'trail_dist': 2.0,
        },
    }

    # 动量策略用不同配置（ADX OFF）
    mom_configs = {
        '原始': {
            'trailing': False, 'adx': False, 'trail_act': 2.0, 'trail_dist': 1.5,
        },
        'P0+P1(激进)': {
            'trailing': True, 'adx': True, 'trail_act': 2.0, 'trail_dist': 1.5,
        },
        'P0+P1(调参)': {
            'trailing': True, 'adx': False, 'trail_act': 3.0, 'trail_dist': 2.0,
        },
    }

    for symbol in ['BTC/USDT', 'ETH/USDT', 'SOL/USDT']:
        symbol_safe = symbol.replace('/', '')
        filepath = os.path.join(CLEAN_DIR, f'{symbol_safe}_1d.parquet')
        if not os.path.exists(filepath):
            continue
        df = pd.read_parquet(filepath)

        print(f"\n{'═' * 80}")
        print(f"  📂 {symbol}")
        print(f"{'═' * 80}")

        # ── 趋势跟踪三版对比 ──
        print(f"\n  ── 趋势跟踪 EMA(10/40) ──")
        print(f"  {'版本':<20} {'ATR止损':>8} {'ADX':>5} {'移动止盈':>8} {'激活':>6} {'距离':>6} {'收益':>10} {'夏普':>8} {'回撤':>10} {'胜率':>7} {'交易':>5}")
        print(f"  {'─' * 95}")

        trend_params = {**BEST_TREND, 'atr_stop': 3.0}
        for label, cfg in configs.items():
            r = run_version(TrendStrategy, trend_params, df, cfg)
            full = r['full_sample']
            atr_val = 3.0 if cfg['trailing'] else 2.0
            print(f"  {label:<20} {atr_val:>8.1f} {'✅' if cfg['adx'] else '❌':>5} {'✅' if cfg['trailing'] else '❌':>8} "
                  f"{cfg['trail_act']:>5.1f} {cfg['trail_dist']:>5.1f} "
                  f"{full['total_return_pct']:>+9.1f}% {full['sharpe']:>8.3f} {full['max_drawdown_pct']:>+9.1f}% "
                  f"{full['win_rate']:>6.1f}% {full['total_trades']:>5.0f}")

            # 记录调参版详细数据用于动量部分对比
            if label == 'P0+P1(调参)':
                trend_tuned = r

        # ── 动量策略三版对比 ──
        print(f"\n  ── 动量策略 MOM(10/20) ──")
        print(f"  {'版本':<20} {'ATR止损':>8} {'ADX':>5} {'移动止盈':>8} {'激活':>6} {'距离':>6} {'收益':>10} {'夏普':>8} {'回撤':>10} {'胜率':>7} {'交易':>5}")
        print(f"  {'─' * 95}")

        for label, cfg in mom_configs.items():
            atr_val = 3.0 if cfg['trailing'] else 2.0
            mom_params = {**BEST_MOM, 'atr_stop': atr_val}
            r = run_version(MomentumStrategy, mom_params, df, cfg)
            full = r['full_sample']
            print(f"  {label:<20} {atr_val:>8.1f} {'✅' if cfg['adx'] else '❌':>5} {'✅' if cfg['trailing'] else '❌':>8} "
                  f"{cfg['trail_act']:>5.1f} {cfg['trail_dist']:>5.1f} "
                  f"{full['total_return_pct']:>+9.1f}% {full['sharpe']:>8.3f} {full['max_drawdown_pct']:>+9.1f}% "
                  f"{full['win_rate']:>6.1f}% {full['total_trades']:>5.0f}")

    print(f"\n✅ 调参对比完成")


if __name__ == '__main__':
    main()
