"""
Day 3 — 双趋势策略回测对比（EMA 交叉 + Donchian 突破）
按路线图要求实现 1-2 个趋势策略，跑不同币种/周期回测

赚钱逻辑：
  EMA 交叉：「快线穿越慢线确认趋势方向，顺势跟进」
  通道突破：「价格突破 N 日高低点 = 市场选择方向，顺势跟进」

用法：
    python day3_dual_trend.py
"""

import pandas as pd
import numpy as np
import sys
import os
from datetime import datetime

if sys.platform == 'win32':
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from backtest.engine import BacktestEngine
from backtest.strategies.trend import TrendStrategy
from backtest.strategies.breakout import BreakoutStrategy

DATA_DIR = os.path.dirname(os.path.abspath(__file__))
CLEAN_DIR = os.path.join(DATA_DIR, 'clean')
LOGS_DIR = os.path.join(DATA_DIR, 'logs')
INITIAL_CAPITAL = 10000
SPLIT_RATIO = 0.7

os.makedirs(LOGS_DIR, exist_ok=True)

# ═══════════════════════════════
# 参数 — 经参数扫描确认
# ═══════════════════════════════
TREND_PARAMS = {'fast_period': 10, 'slow_period': 40, 'atr_stop': 2.0}
BREAKOUT_PARAMS = {'channel_period': 20, 'atr_stop': 2.0, 'atr_filter': 0.5}

SYMBOLS = ['BTC/USDT', 'ETH/USDT', 'SOL/USDT']
TIMEFRAMES = ['1d', '1h']


def run_one(strategy_class, params, df, label):
    """运行单个策略"""
    s = strategy_class(**params)
    s.ENABLE_TRAILING_STOP = False
    s.ENABLE_ADX_FILTER = False
    s.precompute(df)
    engine = BacktestEngine(s, initial_capital=INITIAL_CAPITAL, split_ratio=SPLIT_RATIO)
    return engine.run(df)


def print_dual_trend_report(all_results):
    """双趋势策略报告"""
    now = datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')

    print("\n")
    print("╔" + "═" * 78 + "╗")
    print("║" + "  📊 Day 3 — 双趋势策略回测对比（EMA 交叉 + 通道突破）  ".ljust(70) + "║")
    print("╠" + "═" * 78 + "╣")
    print(f"║  报告时间：{now}".ljust(72) + "║")
    print("╚" + "═" * 78 + "╝")

    # ═══════════════════════
    # 一、1d 日线核心指标
    # ═══════════════════════
    print("\n📊 一、日线 (1d) — 核心指标")

    header = f"  {'币种':<10} {'策略':<18} {'总收益%':>9} {'夏普':>8} {'最大回撤%':>10} {'胜率%':>8} {'盈亏比':>8} {'交易笔数':>8}"
    print(header)
    print("  " + "─" * 81)

    for symbol in SYMBOLS:
        for strategy_name in ['EMA交叉', '通道突破']:
            key = (symbol, '1d', strategy_name)
            if key not in all_results or all_results[key] is None:
                continue
            r = all_results[key]['full_sample']
            print(f"  {symbol:<10} {strategy_name:<18} {r['total_return_pct']:>+8.1f}% {r['sharpe']:>8.3f} {r['max_drawdown_pct']:>+9.1f}% {r['win_rate']:>7.1f}% {r['profit_loss_ratio']:>8.2f} {r['total_trades']:>8.0f}")

    print("  " + "─" * 81)

    # ═══════════════════════
    # 二、样本内外对比
    # ═══════════════════════
    print(f"\n📈 二、样本内外夏普对比 (1d)")
    print(f"  {'币种':<10} {'策略':<18} {'IS夏普':>8} {'OOS夏普':>8} {'OOS/IS':>8} {'判定':>10}")
    print(f"  {'─' * 58}")

    for symbol in SYMBOLS:
        for strategy_name in ['EMA交叉', '通道突破']:
            key = (symbol, '1d', strategy_name)
            if key not in all_results or all_results[key] is None:
                continue
            r = all_results[key]
            is_s = r['in_sample']['sharpe']
            oos_s = r['out_of_sample']['sharpe']
            ratio = oos_s / is_s if is_s > 0 else float('inf')
            if is_s <= 0:
                verdict = '(IS≤0特殊)'
            elif ratio >= 0.7:
                verdict = '✅ 稳定'
            elif ratio >= 0.5:
                verdict = '🟡 可疑'
            else:
                verdict = '🔴 过拟合'
            print(f"  {symbol:<10} {strategy_name:<18} {is_s:>8.3f} {oos_s:>8.3f} {ratio:>7.1%} {verdict:>10}")

    # ═══════════════════════
    # 三、多空拆解 (BTC)
    # ═══════════════════════
    print(f"\n📊 三、BTC 多空方向拆解 (1d)")
    for strategy_name in ['EMA交叉', '通道突破']:
        key = ('BTC/USDT', '1d', strategy_name)
        if key not in all_results or all_results[key] is None:
            continue
        trade_df = all_results[key]['trade_log']
        if len(trade_df) == 0:
            continue

        print(f"\n  ── {strategy_name} ──")
        for side, side_label in [('long', '做多'), ('short', '做空')]:
            side_df = trade_df[trade_df['side'] == side]
            if len(side_df) == 0:
                print(f"    {side_label}: 无交易")
                continue
            n = len(side_df)
            wr = (side_df['net_return_pct'] > 0).sum() / n * 100
            total = side_df['net_return_pct'].sum()
            avg = side_df['net_return_pct'].mean()
            print(f"    {side_label}: {n}笔 | 胜率 {wr:.0f}% | 累计 {total:+.1f}% | 均值 {avg:+.2f}%")

    # ═══════════════════════
    # 四、1h vs 1d 时间框架对比
    # ═══════════════════════
    print(f"\n📐 四、时间框架对比 — 1h vs 1d (BTC)")
    print(f"  {'策略':<18} {'1h夏普':>8} {'1d夏普':>8} {'1h收益%':>9} {'1d收益%':>9} {'1h回撤%':>10} {'1d回撤%':>10}")
    print(f"  {'─' * 68}")

    for strategy_name in ['EMA交叉', '通道突破']:
        key_1d = ('BTC/USDT', '1d', strategy_name)
        key_1h = ('BTC/USDT', '1h', strategy_name)

        h1_s = all_results[key_1h]['full_sample']['sharpe'] if key_1h in all_results and all_results[key_1h] else float('nan')
        d1_s = all_results[key_1d]['full_sample']['sharpe'] if key_1d in all_results and all_results[key_1d] else float('nan')
        h1_r = all_results[key_1h]['full_sample']['total_return_pct'] if key_1h in all_results and all_results[key_1h] else float('nan')
        d1_r = all_results[key_1d]['full_sample']['total_return_pct'] if key_1d in all_results and all_results[key_1d] else float('nan')
        h1_d = all_results[key_1h]['full_sample']['max_drawdown_pct'] if key_1h in all_results and all_results[key_1h] else float('nan')
        d1_d = all_results[key_1d]['full_sample']['max_drawdown_pct'] if key_1d in all_results and all_results[key_1d] else float('nan')

        print(f"  {strategy_name:<18} {h1_s:>8.3f} {d1_s:>8.3f} {h1_r:>+8.1f}% {d1_r:>+8.1f}% {h1_d:>+9.1f}% {d1_d:>+9.1f}%")

    # ═══════════════════════
    # 五、双策略相关性
    # ═══════════════════════
    print(f"\n🔄 五、双策略交易相关性分析 (BTC 1d)")
    ema_trades = None
    bo_trades = None
    if ('BTC/USDT', '1d', 'EMA交叉') in all_results:
        ema_trades = all_results[('BTC/USDT', '1d', 'EMA交叉')]['trade_log']
    if ('BTC/USDT', '1d', '通道突破') in all_results:
        bo_trades = all_results[('BTC/USDT', '1d', '通道突破')]['trade_log']

    if ema_trades is not None and bo_trades is not None and len(ema_trades) > 0 and len(bo_trades) > 0:
        ema_dates = set(str(t)[:10] for t in ema_trades['entry_time'])
        bo_dates = set(str(t)[:10] for t in bo_trades['entry_time'])
        overlap = ema_dates & bo_dates
        total_dates = ema_dates | bo_dates
        overlap_pct = len(overlap) / len(total_dates) * 100 if total_dates else 0
        print(f"  EMA交叉入场日: {len(ema_dates)} 天")
        print(f"  通道突破入场日: {len(bo_dates)} 天")
        print(f"  同日入场重叠: {len(overlap)} 天 ({overlap_pct:.0f}%)")
        if overlap_pct < 50:
            print(f"  → 双策略信号相关度低，同时运行可分散风险 ✅")
        else:
            print(f"  → ⚠️ 双策略高度相关，同时运行不提供额外分散")

    # ═══════════════════════
    # 六、综合推荐
    # ═══════════════════════
    print(f"\n{'═' * 78}")
    print(f"🎯 六、Day 3 结论 — 趋势策略选型")
    print(f"{'═' * 78}")

    # 计算平均夏普
    ema_sharpes = []
    bo_sharpes = []
    for symbol in SYMBOLS:
        for name, arr in [('EMA交叉', ema_sharpes), ('通道突破', bo_sharpes)]:
            key = (symbol, '1d', name)
            if key in all_results and all_results[key] is not None:
                arr.append(all_results[key]['full_sample']['sharpe'])

    ema_avg = np.mean(ema_sharpes) if ema_sharpes else 0
    bo_avg = np.mean(bo_sharpes) if bo_sharpes else 0

    print(f"""
  EMA 交叉均线趋势跟踪:
    三品种平均夏普: {ema_avg:.3f}
    赚钱逻辑: 趋势形成后顺势跟进，捕捉主升浪/主跌浪
    优势: 信号明确，持仓周期长，交易成本低
    劣势: 趋势反转时反应慢，震荡市假信号多

  Donchian 通道突破:
    三品种平均夏普: {bo_avg:.3f}
    赚钱逻辑: 价格突破关键高低点 = 新趋势启动
    优势: 捕捉趋势启动点更及时
    劣势: 假突破多，需要 ATR 过滤

  选型建议:
    → 主力策略: EMA 交叉（逻辑更稳健，交易频率更低）
    → 辅助策略: 通道突破（信号不高度重叠时分散风险）
    → 两个策略同时跑，按各自信号独立执行
""")

    print(f"✅ Day 3 双趋势策略对比完成")


def main():
    print("╔" + "═" * 78 + "╗")
    print("║" + "  Day 3 — 双趋势策略回测（EMA 交叉 + 通道突破）".ljust(70) + "║")
    print("╚" + "═" * 78 + "╝")

    all_results = {}

    for symbol in SYMBOLS:
        for tf in TIMEFRAMES:
            symbol_safe = symbol.replace('/', '')
            filepath = os.path.join(CLEAN_DIR, f"{symbol_safe}_{tf}.parquet")
            if not os.path.exists(filepath):
                continue

            df = pd.read_parquet(filepath)
            print(f"\n📂 {symbol} {tf}: {len(df):,} 行")

            # EMA 交叉趋势
            print(f"  🔄 EMA交叉...")
            all_results[(symbol, tf, 'EMA交叉')] = run_one(
                TrendStrategy, TREND_PARAMS, df, f'{symbol} {tf} EMA'
            )

            # 通道突破
            print(f"  🔄 通道突破...")
            all_results[(symbol, tf, '通道突破')] = run_one(
                BreakoutStrategy, BREAKOUT_PARAMS, df, f'{symbol} {tf} Breakout'
            )

    # ── 打印报告 ──
    print_dual_trend_report(all_results)

    # ── 保存交易日志 ──
    for (symbol, tf, name), results in all_results.items():
        if results is None:
            continue
        trade_log = results['trade_log']
        if len(trade_log) > 0:
            symbol_safe = symbol.replace('/', '')
            fname = f"trades_{name}_{symbol_safe}_{tf}.csv"
            filepath = os.path.join(LOGS_DIR, fname)
            trade_log.to_csv(filepath, index=False, encoding='utf-8-sig')
            print(f"📂 {filepath}")


if __name__ == '__main__':
    main()
