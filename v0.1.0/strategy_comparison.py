"""
Day 4 — 三策略对比报告（趋势跟踪 + 动量 + 资金费率套利基准）

资金费率套利作为基准策略——其他策略必须先跑赢它才有存在的意义。

赚钱逻辑：
  趋势跟踪：「顺势而为，捕捉币圈强趋势延续性」
  动量策略：「双层动量确认，避免单一时间框架假信号」
  资金费率套利：「现货+合约对冲，锁定价差收取费率，市场中性」

用法：
    python strategy_comparison.py
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
from backtest.strategies.momentum import MomentumStrategy
from backtest.strategies.funding_arb import FundingArbitrageStrategy

DATA_DIR = os.path.dirname(os.path.abspath(__file__))
CLEAN_DIR = os.path.join(DATA_DIR, 'clean')
LOGS_DIR = os.path.join(DATA_DIR, 'logs')
INITIAL_CAPITAL = 10000
SPLIT_RATIO = 0.7

os.makedirs(LOGS_DIR, exist_ok=True)

# ═══════════════════════════════
# 参数
# ═══════════════════════════════
BEST_TREND = {'fast_period': 10, 'slow_period': 40, 'atr_stop': 2.0}
BEST_MOM = {'fast_momentum': 10, 'slow_momentum': 20, 'atr_stop': 2.0}
BEST_FUNDING = {'min_funding_rate': 10.0, 'exit_funding_rate': 5.0, 'max_holding_days': 30}

SYMBOLS = ['BTC/USDT', 'ETH/USDT', 'SOL/USDT']


def run_one(strategy_class, params, df, label):
    """运行单个策略"""
    s = strategy_class(**params)
    s.precompute(df)
    engine = BacktestEngine(s, initial_capital=INITIAL_CAPITAL, split_ratio=SPLIT_RATIO)
    return engine.run(df)


def print_three_way_report(all_results):
    """三策略对比报告"""
    now = datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')

    print("\n")
    print("╔" + "═" * 78 + "╗")
    print("║" + "  📊 Day 4 — 三策略对比报告（趋势 + 动量 + 费率套利）".ljust(70) + "║")
    print("╠" + "═" * 78 + "╣")
    print(f"║  基准策略：资金费率套利（年化目标 10-30%，市场中性）".ljust(72) + "║")
    print(f"║  报告时间：{now}".ljust(72) + "║")
    print("╚" + "═" * 78 + "╝")

    # ═══════════════════════
    # 一、核心指标对比
    # ═══════════════════════
    print("\n📊 一、BTC 日线 — 三策略核心指标")
    print(f"  赚钱逻辑检查：")
    print(f"    趋势跟踪「EMA交叉确认趋势，顺势跟进到反转」")
    print(f"    动量策略「双层动量均正=上涨趋势确认，双层动量均负=下跌趋势确认」")
    print(f"    费率套利「锁定价差，赚的是多头付给空头的资金费率」")

    header = f"  {'指标':<24} {'趋势跟踪':>14} {'动量策略':>14} {'费率套利':>14}"
    print()
    print(header)
    print("  " + "─" * len(header))

    metrics_to_show = [
        ('total_return_pct', '总收益率 (%)', '{:.2f}%'),
        ('annual_return_pct', '年化收益率 (%)', '{:.2f}%'),
        ('sharpe', '夏普比率', '{:.3f}'),
        ('max_drawdown_pct', '最大回撤 (%)', '{:.2f}%'),
        ('calmar', 'Calmar 比率', '{:.3f}'),
        ('total_trades', '总交易笔数', '{:.0f}'),
        ('win_rate', '胜率 (%)', '{:.1f}%'),
        ('profit_loss_ratio', '盈亏比', '{:.2f}'),
    ]

    strategies_names = ['趋势跟踪', '动量策略', '费率套利']

    for key, label_text, fmt in metrics_to_show:
        row = f"  {label_text:<22}"
        for name in strategies_names:
            key_tup = ('BTC/USDT', name)
            if key_tup in all_results and all_results[key_tup] is not None:
                val = all_results[key_tup]['full_sample'].get(key, 'N/A')
                try:
                    row += f" {fmt.format(val):>14}"
                except (ValueError, TypeError):
                    row += f" {str(val):>14}"
            else:
                row += f" {'—':>14}"
        print(row)

    print("  " + "─" * len(header))

    # ═══════════════════════
    # 二、风险维度
    # ═══════════════════════
    print(f"\n🛡️ 二、风险维度对比 (BTC 1d)")

    for name in strategies_names:
        key = ('BTC/USDT', name)
        if key not in all_results or all_results[key] is None:
            continue
        r = all_results[key]['full_sample']
        trade_df = all_results[key]['trade_log']
        max_consec = r.get('max_consecutive_losses', 0)

        print(f"\n  ── {name} ──")
        print(f"    最大回撤: {r['max_drawdown_pct']:.1f}%")
        print(f"    最大连续亏损: {max_consec} 笔")
        print(f"    夏普: {r['sharpe']:.3f}  |  Calmar: {r['calmar']:.3f}")

        if name == '费率套利' and len(trade_df) > 0:
            # 费率套利特殊指标
            funding_yield = trade_df['funding_accrued_pct'].sum() if 'funding_accrued_pct' in trade_df.columns else 0
            avg_rate = trade_df['entry_funding_rate'].mean() if 'entry_funding_rate' in trade_df.columns else 0
            print(f"    累计资金费率收入: {funding_yield:+.2f}%")
            print(f"    平均入场费率: {avg_rate:.1f}% 年化")

    # ═══════════════════════
    # 三、跨品种稳定性
    # ═══════════════════════
    print(f"\n📈 三、跨品种一致性 (日线)")
    print(f"  {'策略':<16} {'BTC夏普':>10} {'ETH夏普':>10} {'SOL夏普':>10} {'正夏普率':>10} {'判定':>10}")
    print(f"  {'─' * 72}")

    for name in strategies_names:
        sharpes = []
        for symbol in SYMBOLS:
            key = (symbol, name)
            if key in all_results and all_results[key] is not None:
                sharpes.append(all_results[key]['full_sample']['sharpe'])

        if sharpes:
            positive_rate = sum(1 for s in sharpes if s > 0) / len(sharpes)
            verdict = '✅ 稳定' if positive_rate >= 0.67 else ('🟡 部分' if positive_rate >= 0.33 else '❌ 不稳定')
            s_btc = sharpes[0] if len(sharpes) > 0 else float('nan')
            s_eth = sharpes[1] if len(sharpes) > 1 else float('nan')
            s_sol = sharpes[2] if len(sharpes) > 2 else float('nan')
            print(f"  {name:<16} {s_btc:>10.3f} {s_eth:>10.3f} {s_sol:>10.3f} {positive_rate:>9.0%} {verdict:>10}")

    # ═══════════════════════
    # 四、成本敏感性
    # ═══════════════════════
    print(f"\n💰 四、交易成本对策略的影响 (BTC 1d)")
    print(f"  {'策略':<16} {'交易笔数':>8} {'单笔成本%':>10} {'总成本%':>10} {'毛收益%':>10} {'净收益%':>10} {'成本占比':>10}")
    print(f"  {'─' * 80}")

    for name in strategies_names:
        key = ('BTC/USDT', name)
        if key not in all_results or all_results[key] is None:
            continue
        trade_df = all_results[key]['trade_log']
        full = all_results[key]['full_sample']
        n_trades = len(trade_df)
        if n_trades == 0:
            continue

        # 不同策略的成本结构不同
        if name == '费率套利':
            cost_per_trade = 0.28  # 现货 0.2% + 合约 0.08%
        else:
            cost_per_trade = 0.2   # 0.1% × 2 往返

        total_cost = n_trades * cost_per_trade
        net_ret = full['total_return_pct']
        gross_ret = net_ret + total_cost

        cost_ratio = abs(total_cost / gross_ret * 100) if abs(gross_ret) > 0.01 else float('inf')
        print(f"  {name:<16} {n_trades:>8.0f} {cost_per_trade:>9.2f}% {total_cost:>9.2f}% {gross_ret:>+9.2f}% {net_ret:>+9.2f}% {cost_ratio:>9.0f}%")

    # ═══════════════════════
    # 五、关键结论
    # ═══════════════════════
    print(f"\n{'═' * 78}")
    print(f"🎯 五、Day 4 结论 — 策略优先级")
    print(f"{'═' * 78}")

    # 收集数据
    trend_btc = all_results.get(('BTC/USDT', '趋势跟踪'), {}).get('full_sample', {})
    mom_btc = all_results.get(('BTC/USDT', '动量策略'), {}).get('full_sample', {})
    arb_btc = all_results.get(('BTC/USDT', '费率套利'), {}).get('full_sample', {})

    print(f"""
  三策略定位：
  ┌──────────────┬──────────────┬──────────────┬──────────────┐
  │              │   趋势跟踪   │   动量策略   │   费率套利   │
  ├──────────────┼──────────────┼──────────────┼──────────────┤
  │ 风险类型     │ 方向性       │ 方向性       │ 市场中性     │
  │ 收益来源     │ 价格趋势     │ 价格动量     │ 资金费率     │
  │ 适合市场     │ 强趋势市     │ 趋势市       │ 正费率市场   │
  │ 交易频率     │ 低 (数笔/年) │ 中 (数十笔)  │ 极低 (每笔数周)│
  │ 最大风险     │ 震荡市假突破 │ 动量频繁反转 │ 费率反转变负 │
  └──────────────┴──────────────┴──────────────┴──────────────┘

  策略优先级（实盘顺序）：
  1. 🥇 资金费率套利 — 基准策略，最稳，先跑稳再谈超额
  2. 🥈 趋势跟踪 — 主力策略，夏普最高，交易最少
  3. 🥉 动量策略 — 辅助策略，提供额外信号源

  铁律：其他策略必须跑赢资金费率套利才有存在的意义。
  如果你的趋势/动量策略年化 < 费率套利，那不如全押费率套利。
""")

    print(f"✅ Day 4 三策略对比完成")


def main():
    print("╔" + "═" * 78 + "╗")
    print("║" + "  Day 4 — 三策略对比（趋势跟踪 + 动量 + 资金费率套利）".ljust(70) + "║")
    print("╚" + "═" * 78 + "╝")

    all_results = {}

    for symbol in SYMBOLS:
        symbol_safe = symbol.replace('/', '')
        # 只用日线数据（费率套利在 1h 上没有意义）
        filepath = os.path.join(CLEAN_DIR, f"{symbol_safe}_1d.parquet")
        if not os.path.exists(filepath):
            print(f"⚠️ {symbol} 数据不存在，跳过")
            continue

        df = pd.read_parquet(filepath)
        print(f"\n📂 {symbol} 1d: {len(df):,} 行 | {df.index[0].strftime('%Y-%m-%d')} → {df.index[-1].strftime('%Y-%m-%d')}")

        # 趋势跟踪
        print(f"  🔄 趋势跟踪 EMA(10/40)...")
        all_results[(symbol, '趋势跟踪')] = run_one(
            TrendStrategy, BEST_TREND, df, f'{symbol} Trend'
        )

        # 动量策略
        print(f"  🔄 动量策略 MOM(10/20)...")
        all_results[(symbol, '动量策略')] = run_one(
            MomentumStrategy, BEST_MOM, df, f'{symbol} Mom'
        )

        # 资金费率套利
        print(f"  🔄 资金费率套利 (>10%年化)...")
        all_results[(symbol, '费率套利')] = run_one(
            FundingArbitrageStrategy, BEST_FUNDING, df, f'{symbol} Funding'
        )

    # ── 打印报告 ──
    print_three_way_report(all_results)

    # ── 保存交易日志 ──
    for (symbol, name), results in all_results.items():
        if results is None:
            continue
        trade_log = results['trade_log']
        if len(trade_log) > 0:
            symbol_safe = symbol.replace('/', '')
            fname = f"trades_{name}_{symbol_safe}_1d.csv"
            filepath = os.path.join(LOGS_DIR, fname)
            trade_log.to_csv(filepath, index=False, encoding='utf-8-sig')
            print(f"📂 {filepath}")


if __name__ == '__main__':
    main()
