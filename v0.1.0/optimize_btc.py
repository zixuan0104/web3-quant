"""
BTC 主流币趋势跟踪 + 动量策略深度优化

优化维度：
  1. MAE/MFE 分布 → 最优止损/止盈区间
  2. 市场环境分解 → 什么市况赚钱/亏钱
  3. 跨品种验证 → BTC / ETH / SOL
  4. 做多 vs 做空拆解 → 哪一侧是利润来源
  5. 交易级诊断 → 最大亏损交易的根因

用法：
    python optimize_btc.py
"""

import pandas as pd
import numpy as np
import sys
import os
import warnings

warnings.filterwarnings('ignore')

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


def run_full_analysis(strategy, df, symbol, label):
    """运行策略并输出完整诊断"""
    print(f"\n{'═' * 70}")
    print(f"  🔬 {symbol} — {label}")
    print(f"{'═' * 70}")

    strat = strategy()
    strat.precompute(df)
    engine = BacktestEngine(strat, initial_capital=INITIAL_CAPITAL, split_ratio=SPLIT_RATIO)
    results = engine.run(df)

    trade_df = results['trade_log']
    if trade_df is None or len(trade_df) == 0:
        print("  ❌ 无交易")
        return results

    # ── MAE/MFE 分布 ──
    print(f"\n  📐 MAE/MFE 分析 (n={len(trade_df)})")

    # 盈利交易
    winners = trade_df[trade_df['net_return_pct'] > 0]
    losers = trade_df[trade_df['net_return_pct'] <= 0]

    if len(winners) > 0:
        w_mae = winners['mae_pct'].abs()
        w_mfe = winners['mfe_pct']
        print(f"    盈利交易 ({len(winners)}笔):")
        print(f"      MAE (最大浮亏): P50={w_mae.median():.2f}%  P75={w_mae.quantile(0.75):.2f}%  P95={w_mae.quantile(0.95):.2f}%")
        print(f"      MFE (最大浮盈): P50={w_mfe.median():.2f}%  P75={w_mfe.quantile(0.75):.2f}%  P95={w_mfe.quantile(0.95):.2f}%")
        print(f"      → 95%的盈利交易 MAE 不超过 {w_mae.quantile(0.95):.2f}%，止损设在此水平以下可保留大部分盈利交易")
        print(f"      → 中位 MFE {w_mfe.median():.2f}%，中位净收益 {winners['net_return_pct'].median():.2f}%——止盈太早会错过 {(w_mfe.median() - winners['net_return_pct'].median()):.2f}%")

    if len(losers) > 0:
        l_mae = losers['mae_pct'].abs()
        l_mfe = losers['mfe_pct']
        # 有多少亏损交易曾经盈利过
        was_profitable = (losers['mfe_pct'] > 0).sum()
        print(f"\n    亏损交易 ({len(losers)}笔):")
        print(f"      MAE (最大浮亏): P50={l_mae.median():.2f}%  P75={l_mae.quantile(0.75):.2f}%  P95={l_mae.quantile(0.95):.2f}%")
        print(f"      曾经盈利过的: {was_profitable}/{len(losers)} ({was_profitable/len(losers)*100:.0f}%) — 如果能及时止盈可挽回")
        stop_out = (losers['exit_reason'] == 'stop_loss').sum()
        print(f"      止损出场: {stop_out}/{len(losers)} ({stop_out/len(losers)*100:.0f}%)")

    # ── 做多 vs 做空 ──
    print(f"\n  📊 多空拆解")
    for side in ['long', 'short']:
        side_df = trade_df[trade_df['side'] == side]
        if len(side_df) == 0:
            print(f"    {side}: 无交易")
            continue
        win_rate = (side_df['net_return_pct'] > 0).sum() / len(side_df) * 100
        total = side_df['net_return_pct'].sum()
        avg = side_df['net_return_pct'].mean()
        best = side_df['net_return_pct'].max()
        worst = side_df['net_return_pct'].min()
        avg_bars = side_df['bars_held'].mean()
        print(f"    {side}: {len(side_df)}笔 | 胜率 {win_rate:.0f}% | 累计 {total:+.1f}% | 均值 {avg:+.2f}% | 最佳 {best:+.1f}% | 最差 {worst:+.1f}% | 均持 {avg_bars:.0f}天")

    # ── 出场原因 ──
    print(f"\n  🚪 出场原因分布")
    for reason in trade_df['exit_reason'].unique():
        reason_df = trade_df[trade_df['exit_reason'] == reason]
        total = reason_df['net_return_pct'].sum()
        win_rate = (reason_df['net_return_pct'] > 0).sum() / len(reason_df) * 100 if len(reason_df) > 0 else 0
        print(f"    {reason:<12}: {len(reason_df):>3}笔 | 累计 {total:>+8.1f}% | 胜率 {win_rate:>5.0f}%")

    # ── 市场环境分解（仅在 BTC 上做）──
    if symbol == 'BTC/USDT':
        print(f"\n  🌍 市场环境分解（入场时的市况）")
        # 重新计算市况（用 200 日滚动窗口）
        df_regime = df.copy()
        df_regime['ret_200'] = df_regime['close'].pct_change(200)
        df_regime['vol_200'] = df_regime['close'].pct_change().rolling(200).std() * np.sqrt(365)
        median_vol = df_regime['vol_200'].median()

        for regime_name, regime_cond in [
            ('趋势市', lambda r, v: abs(r) > 0.15),
            ('震荡市', lambda r, v: abs(r) <= 0.15),
            ('高波动', lambda r, v: v > median_vol),
            ('低波动', lambda r, v: v <= median_vol),
        ]:
            regime_trades = []
            for _, trade in trade_df.iterrows():
                entry_idx = int(trade['entry_idx'])
                if entry_idx < 200:
                    continue
                r = df_regime['ret_200'].iloc[entry_idx]
                v = df_regime['vol_200'].iloc[entry_idx]
                if pd.isna(r) or pd.isna(v):
                    continue
                if regime_cond(r, v):
                    regime_trades.append(trade['net_return_pct'])

            if regime_trades:
                total_ret = sum(regime_trades)
                wr = sum(1 for x in regime_trades if x > 0) / len(regime_trades) * 100
                print(f"    {regime_name:<10}: {len(regime_trades):>3}笔 | 累计 {total_ret:>+8.1f}% | 胜率 {wr:>5.0f}% | 均值 {np.mean(regime_trades):>+6.2f}%")

    # ── 最大亏损诊断 ──
    print(f"\n  🔴 最大亏损交易 Top-3")
    worst_trades = trade_df.nsmallest(3, 'net_return_pct')
    for rank, (_, trade) in enumerate(worst_trades.iterrows(), 1):
        entry_t = str(trade['entry_time'])[:10]
        exit_t = str(trade['exit_time'])[:10]
        print(f"    {rank}. {trade['side']} | {entry_t} → {exit_t} | "
              f"收益 {trade['net_return_pct']:.1f}% | "
              f"MAE {trade['mae_pct']:.1f}% | MFE {trade['mfe_pct']:.1f}% | "
              f"出场: {trade['exit_reason']} | 持 {int(trade['bars_held'])}天")

    return results


def print_cross_asset_table(all_symbols_results):
    """跨品种对比表"""
    print(f"\n\n{'═' * 80}")
    print(f"  📊 跨品种验证 — BTC / ETH / SOL")
    print(f"{'═' * 80}")

    for strategy_name in ['趋势跟踪(10/40/2.0)', '动量策略(10/20/2.0)']:
        print(f"\n  ── {strategy_name} ──")
        print(f"  {'币种':<10} {'总收益%':>10} {'夏普':>8} {'最大回撤%':>10} {'胜率%':>8} {'交易笔数':>8} {'做多收益':>10} {'做空收益':>10}")
        print(f"  {'─' * 80}")

        for symbol in ['BTC/USDT', 'ETH/USDT', 'SOL/USDT']:
            if symbol not in all_symbols_results or strategy_name not in all_symbols_results[symbol]:
                continue
            r = all_symbols_results[symbol][strategy_name]
            if r is None:
                continue
            full = r['full_sample']
            trade_df = r['trade_log']

            long_ret = trade_df[trade_df['side']=='long']['net_return_pct'].sum() if len(trade_df) > 0 else 0
            short_ret = trade_df[trade_df['side']=='short']['net_return_pct'].sum() if len(trade_df) > 0 else 0

            print(f"  {symbol:<10} {full['total_return_pct']:>+9.1f}% {full['sharpe']:>8.3f} {full['max_drawdown_pct']:>+9.1f}% {full['win_rate']:>7.1f}% {full['total_trades']:>8.0f} {long_ret:>+9.1f}% {short_ret:>+9.1f}%")


def main():
    # ── 最优参数（来自参数扫描结果）──
    best_trend_params = {'fast_period': 10, 'slow_period': 40, 'atr_stop': 2.0}
    best_mom_params = {'fast_momentum': 10, 'slow_momentum': 20, 'atr_stop': 2.0}

    print("╔" + "═" * 78 + "╗")
    print("║  主流币量化深度优化 — 趋势跟踪 + 动量策略".ljust(70) + "║")
    print("╠" + "═" * 78 + "╣")
    print(f"║  趋势: EMA({best_trend_params['fast_period']}/{best_trend_params['slow_period']}) ATR={best_trend_params['atr_stop']}".ljust(72) + "║")
    print(f"║  动量: MOM({best_mom_params['fast_momentum']}/{best_mom_params['slow_momentum']}) ATR={best_mom_params['atr_stop']}".ljust(72) + "║")
    print("╚" + "═" * 78 + "╝")

    all_results = {}

    for symbol in ['BTC/USDT', 'ETH/USDT', 'SOL/USDT']:
        symbol_safe = symbol.replace('/', '')
        filepath = os.path.join(CLEAN_DIR, f'{symbol_safe}_1d.parquet')
        if not os.path.exists(filepath):
            print(f"\n⚠️ {symbol} 数据不存在，跳过")
            continue

        df = pd.read_parquet(filepath)
        print(f"\n📂 {symbol}: {len(df):,} 行 | {df.index[0].strftime('%Y-%m-%d')} → {df.index[-1].strftime('%Y-%m-%d')}")

        all_results[symbol] = {}

        # 趋势跟踪
        trend_results = run_full_analysis(
            lambda: TrendStrategy(**best_trend_params),
            df, symbol, f'趋势跟踪 EMA({best_trend_params["fast_period"]}/{best_trend_params["slow_period"]})'
        )
        all_results[symbol]['趋势跟踪(10/40/2.0)'] = trend_results

        # 动量策略
        mom_results = run_full_analysis(
            lambda: MomentumStrategy(**best_mom_params),
            df, symbol, f'动量策略 MOM({best_mom_params["fast_momentum"]}/{best_mom_params["slow_momentum"]})'
        )
        all_results[symbol]['动量策略(10/20/2.0)'] = mom_results

    # ── 跨品种对比 ──
    print_cross_asset_table(all_results)

    # ═══════════════════════
    # 优化建议
    # ═══════════════════════
    print(f"\n\n{'═' * 80}")
    print(f"  🎯 优化建议")
    print(f"{'═' * 80}")
    print(f"""
  1. 止损优化
  ───────────
  基于 MAE 分位数，将止损从固定 ATR 倍数改为动态分位数止损。
  95% 的盈利交易 MAE 不超过某个阈值 → 止损设在此阈值 + 缓冲。

  2. 止盈优化
  ───────────
  相当比例的亏损交易"曾经盈利过"。引入移动止盈（trailing stop）
  可大幅减少这类"浮盈→亏损"的交易。

  3. 震荡市过滤
  ───────────
  加入市场环境过滤器：在震荡市中降低仓位或暂停交易。
  识别方式：ADX < 20 或 布林带宽度收窄。

  4. 做空权重调整
  ───────────
  如果做多方向持续盈利而做空亏损（取决于BTC大趋势方向），
  可以给做空信号加更严格的确认条件。

  5. 跨品种资金分配
  ───────────
  BTC/ETH/SOL 上策略表现可能不同。按各品种的滚动夏普动态分配资金。
""")

    print("✅ 主流币优化分析完成")


if __name__ == '__main__':
    main()
