"""
仓位管理方法对比 — Day 8 回测验证

读取已有交易记录，用不同仓位管理方法重放，对比最终资金曲线。

验证的核心问题：
  1. 凯利公式算出的最优仓位 vs 实际策略表现
  2. 半凯利 vs 完整凯利的波动率差异
  3. 固定分数 vs 凯利的长期收益对比
"""

import csv
import os
import sys
from pathlib import Path
from collections import defaultdict

# 确保能导入 position_sizer
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from position_sizer import (PositionSizer, PositionConfig, SizingMethod,
                            PortfolioState, kelly_simulate)


def load_trades(csv_path: str) -> list:
    """加载交易记录 CSV"""
    trades = []
    with open(csv_path, 'r', encoding='utf-8') as f:
        reader = csv.DictReader(f)
        for row in reader:
            trades.append({
                'net_return_pct': float(row.get('net_return_pct', 0)),
                'side': row.get('side', 'long'),
                'exit_reason': row.get('exit_reason', ''),
                'bars_held': int(row.get('bars_held', 0)),
                'entry_time': row.get('entry_time', ''),
                'exit_time': row.get('exit_time', ''),
            })
    return trades


def calc_strategy_stats(trades: list) -> dict:
    """从交易记录计算策略统计"""
    if not trades:
        return {'win_rate': 0.5, 'avg_win_pct': 0.03, 'avg_loss_pct': 0.015,
                'win_loss_ratio': 2.0, 'total_trades': 0}

    wins = [t for t in trades if t['net_return_pct'] > 0]
    losses = [t for t in trades if t['net_return_pct'] <= 0]

    win_rate = len(wins) / len(trades) if trades else 0.5
    avg_win = sum(t['net_return_pct'] for t in wins) / len(wins) / 100 if wins else 0.03
    avg_loss = abs(sum(t['net_return_pct'] for t in losses) / len(losses)) / 100 if losses else 0.015
    win_loss_ratio = avg_win / avg_loss if avg_loss > 0 else 2.0

    return {
        'win_rate': round(win_rate, 3),
        'avg_win_pct': round(avg_win, 3),
        'avg_loss_pct': round(avg_loss, 3),
        'win_loss_ratio': round(win_loss_ratio, 2),
        'total_trades': len(trades),
        'wins': len(wins),
        'losses': len(losses),
    }


def simulate_equity_curve(trades: list, size_pct: float, initial_capital: float = 10000.0) -> list:
    """
    用固定仓位比例模拟资金曲线

    返回: [(trade_index, equity), ...]
    """
    equity = initial_capital
    curve = [(0, equity)]

    for i, t in enumerate(trades):
        ret = t['net_return_pct'] / 100.0
        # 每笔交易用当前资金的 size_pct
        position_value = equity * size_pct
        pnl = position_value * ret
        equity += pnl

        # 防止归零
        if equity < 1.0:
            equity = 1.0

        curve.append((i + 1, equity))

    return curve


def compare_methods(trades: list, stats: dict, initial_capital: float = 10000.0):
    """对比不同仓位管理方法"""
    print(f"\n{'='*70}")
    print(f"  仓位管理方法对比 — 基于实际交易记录")
    print(f"{'='*70}")
    print(f"  策略统计: {stats['total_trades']} 笔交易, "
          f"胜率 {stats['win_rate']:.1%}, "
          f"盈亏比 {stats['win_loss_ratio']:.1f}")
    print(f"  盈利交易: {stats['wins']} 笔, 亏损交易: {stats['losses']} 笔")
    print(f"  平均盈利: {stats['avg_win_pct']:.1%}, "
          f"平均亏损: {stats['avg_loss_pct']:.1%}")

    # 凯利公式
    ps = PositionSizer(initial_capital=initial_capital)
    kelly_full = ps.kelly_position(
        stats['win_rate'], stats['avg_win_pct'], stats['avg_loss_pct'],
        variant=SizingMethod.KELLY_FULL
    )
    kelly_half = ps.kelly_position(
        stats['win_rate'], stats['avg_win_pct'], stats['avg_loss_pct'],
        variant=SizingMethod.KELLY_HALF
    )
    kelly_quarter = ps.kelly_position(
        stats['win_rate'], stats['avg_win_pct'], stats['avg_loss_pct'],
        variant=SizingMethod.KELLY_QUARTER
    )
    fixed = ps.fixed_fraction(0.02)

    # 模拟各方法的资金曲线
    methods = [
        ('完整凯利', kelly_full['size_pct']),
        ('半凯利', kelly_half['size_pct']),
        ('1/4 凯利', kelly_quarter['size_pct']),
        ('固定 2%', fixed['size_pct']),
        ('固定 5%', 0.05),
        ('固定 10%', 0.10),
    ]

    print(f"\n  {'方法':<15} {'仓位%':>8} {'最终资金':>12} {'收益率':>10} "
          f"{'最大回撤':>10} {'终值/初始':>10}")
    print(f"  {'-'*15} {'-'*8} {'-'*12} {'-'*10} {'-'*10} {'-'*10}")

    results = []
    for name, size in methods:
        size = min(size, 0.25)  # 不超过 25%
        if size <= 0:
            continue

        curve = simulate_equity_curve(trades, size, initial_capital)
        final_equity = curve[-1][1]
        return_pct = (final_equity - initial_capital) / initial_capital * 100

        # 计算最大回撤
        peak = initial_capital
        max_dd = 0.0
        for _, eq in curve:
            if eq > peak:
                peak = eq
            dd = (peak - eq) / peak * 100
            if dd > max_dd:
                max_dd = dd

        ratio = final_equity / initial_capital
        print(f"  {name:<15} {size:>7.2%} ${final_equity:>11,.0f} {return_pct:>9.1f}% "
              f"{max_dd:>9.1f}% {ratio:>9.2f}x")

        results.append({
            'name': name, 'size_pct': size, 'final_equity': final_equity,
            'return_pct': return_pct, 'max_drawdown': max_dd, 'ratio': ratio,
        })

    # 蒙特卡洛验证
    print(f"\n  --- 蒙特卡洛模拟 (200笔, 5000路径) ---")
    mc = kelly_simulate(
        win_rate=stats['win_rate'],
        win_loss_ratio=stats['win_loss_ratio'],
        n_trades=200, n_paths=5000, kelly_fraction=0.5
    )
    print(f"  凯利 f* = {mc['kelly_f_star']:.2%}")
    print(f"  半凯利仓位 = {mc['actual_f']:.2%}")
    print(f"  200笔后中位数: {mc['median_final']:.2f}x 初始资金")
    print(f"  5%分位 (最差): {mc['p5_final']:.2f}x")
    print(f"  95%分位 (最好): {mc['p95_final']:.2f}x")
    print(f"  爆仓概率 (<10%剩余): {mc['bust_rate']:.1%}")

    # 结论
    print(f"\n  --- 结论 ---")
    best = max(results, key=lambda r: r['final_equity'])
    best_risk_adj = max(results, key=lambda r: r['return_pct'] / (r['max_drawdown'] + 1))
    print(f"  最高绝对收益: {best['name']} (${best['final_equity']:,.0f}, {best['return_pct']:.1f}%)")
    print(f"  最佳风险调整: {best_risk_adj['name']} "
          f"(收益 {best_risk_adj['return_pct']:.1f}%, 回撤 {best_risk_adj['max_drawdown']:.1f}%)")
    print(f"  推荐: 半凯利 — 在收益和风险之间取得平衡，参数估计误差的保险")

    return results


def main():
    """入口：找所有交易文件，逐个对比"""
    data_root = Path(__file__).parent / 'data' / '20260705' / 'logs'
    if not data_root.exists():
        print(f"数据目录不存在: {data_root}")
        return

    trade_files = sorted(data_root.glob('trades_*.csv'))
    # 去重：只取 *_1d.csv（日线更有代表性）
    daily_files = [f for f in trade_files if '_1d.csv' in f.name]

    if not daily_files:
        print("没有找到交易日志文件")
        return

    all_results = {}
    for tf in daily_files[:6]:  # 前 6 个避免太长
        name = tf.stem.replace('trades_', '')
        trades = load_trades(str(tf))
        if len(trades) < 5:
            print(f"\n  {name}: 交易太少 ({len(trades)} 笔), 跳过")
            continue

        stats = calc_strategy_stats(trades)
        print(f"\n{'='*70}")
        print(f"  [{name}]")
        results = compare_methods(trades, stats)
        all_results[name] = results

    # 汇总
    print(f"\n\n{'='*70}")
    print(f"  汇总: 所有策略仓位方法推荐")
    print(f"{'='*70}")
    print(f"  {'策略':<25} {'推荐方法':<12} {'推荐仓位':>8} {'备注'}")
    print(f"  {'-'*25} {'-'*12} {'-'*8} {'-'*30}")
    for name, results in all_results.items():
        # 找风险调整后最好的
        best = max(results, key=lambda r: r['return_pct'] / (r['max_drawdown'] + 1))
        note = (f"收益{best['return_pct']:.0f}%, 回撤{best['max_drawdown']:.0f}%"
                if best['return_pct'] > 0 else "不建议实盘")
        print(f"  {name:<25} {best['name']:<12} {best['size_pct']:>7.2%}  {note}")


if __name__ == '__main__':
    main()
