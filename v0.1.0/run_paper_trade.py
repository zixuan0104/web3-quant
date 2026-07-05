"""
Day 5+6 模拟盘 + 成本模型验证入口

流程：
  1. 加载清洗后的 1h 数据
  2. 跑回测（作为对比基准）
  3. 跑模拟盘（市价单 + 限价单两种模式）
  4. 跑偏差分析（模拟盘 vs 回测）
  5. 跑订单类型对比（限价 vs 市价）
  6. 跑成本效率分析
  7. 输出完整报告

用法：
  python run_paper_trade.py
  python run_paper_trade.py --symbol BTC --timeframe 1h
"""

import pandas as pd
import numpy as np
import sys
import os
import argparse

if sys.platform == 'win32':
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    sys.stderr.reconfigure(encoding='utf-8', errors='replace')

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from backtest.engine import BacktestEngine
from backtest.strategies.trend import TrendStrategy
from backtest.strategies.momentum import MomentumStrategy
from cost_model import (
    CostModel, OrderType, LiquidityTier, CostBreakdown,
    BacktestCostAdapter,
)
from paper_trader import (
    PaperTrader, HistoricalDataFeed, LocalExecutionSim,
    SimTrade, SimOrder, MarketDataFeed, ExecutionSim,
)
from order_type_comparison import OrderTypeComparison, analyze_cost_efficiency

# ═══════════════════════════════
# 配置
# ═══════════════════════════════
DATA_DIR = os.path.dirname(os.path.abspath(__file__))
CLEAN_DIR = os.path.join(DATA_DIR, 'clean')
INITIAL_CAPITAL = 10000
SPLIT_RATIO = 0.7


def print_separator(title=""):
    if title:
        print(f"\n{'='*70}")
        print(f"  {title}")
        print(f"{'='*70}")
    else:
        print("-" * 70)


def load_data(symbol='BTC/USDT', timeframe='1h'):
    """加载清洗后的数据"""
    symbol_safe = symbol.replace('/', '')
    filepath = os.path.join(CLEAN_DIR, f"{symbol_safe}_{timeframe}.parquet")
    if not os.path.exists(filepath):
        print(f"❌ 数据文件不存在: {filepath}")
        sys.exit(1)
    df = pd.read_parquet(filepath)
    print(f"📂 加载数据: {filepath} ({len(df):,} 行)")
    print(f"   时间范围: {df.index[0]} → {df.index[-1]}")
    return df


def run_backtest(strategy, df, label=""):
    """运行回测，返回结果"""
    print(f"\n🔄 回测: {strategy.name}")
    engine = BacktestEngine(strategy, initial_capital=INITIAL_CAPITAL, split_ratio=SPLIT_RATIO)
    results = engine.run(df)
    return results


def run_paper_trade(strategy, df, order_type='market', symbol='BTC',
                    bt_results=None, verbose=True):
    """
    运行模拟盘

    strategy: 已 precompute 的 BaseStrategy 实例
    order_type: 'market' | 'limit'
    """
    from copy import deepcopy
    import pickle

    # 深拷贝策略（避免修改原实例）— 用重建方式规避 pickle 限制
    strategy_cls = strategy.__class__
    strat_params = {}
    for attr in ['fast_period', 'slow_period', 'atr_stop',
                 'fast_momentum', 'slow_momentum']:
        if hasattr(strategy, attr):
            strat_params[attr] = getattr(strategy, attr)

    paper_strat = strategy_cls(**strat_params)
    paper_strat.precompute(df)

    # 数据源
    feed = HistoricalDataFeed(df=df.copy(), replay_speed=0)
    feed.initialize()

    # 成交模拟
    liquidity_tier = CostModel.classify_liquidity(symbol)
    if order_type == 'market':
        cm = CostModel(OrderType.TAKER, liquidity_tier)
    else:
        cm = CostModel(OrderType.MAKER, liquidity_tier)

    exec_sim = LocalExecutionSim(cm)
    trader = PaperTrader(paper_strat, feed, exec_sim, INITIAL_CAPITAL, order_type)

    if bt_results:
        trader.set_backtest_results(bt_results)

    results = trader.run(symbol, verbose=verbose)
    results['trader'] = trader  # 保留 trader 引用用于偏差报告
    return results


def print_cost_breakdown(cost_model, symbol, price, atr=None, quantity=500):
    """打印单笔交易成本分解"""
    tier = cost_model.liquidity_tier
    round_trip = cost_model.round_trip_cost(price, atr, quantity_usdt=quantity)

    print(f"\n  💸 成本分解 — {symbol} @ ${price:,.2f} (流动性: {tier.value})")
    print(f"  {'─'*50}")
    print(f"  手续费 (双向):  {round_trip.fee_bps:>8.1f} bps")
    print(f"  滑点 (双向):    {round_trip.slippage_bps:>8.1f} bps")
    print(f"  价差 (双向):    {round_trip.spread_bps:>8.1f} bps")
    print(f"  市场冲击 (双向): {round_trip.impact_bps:>8.1f} bps")
    print(f"  {'─'*50}")
    print(f"  总成本 (双向):  {round_trip.total_bps:>8.1f} bps = {round_trip.total_pct()*100:.3f}%")
    print(f"  隐含盈亏要求:   策略每笔交易至少赚 {round_trip.total_pct()*100:.3f}% 才不亏")


def print_bt_vs_paper(bt_results, paper_results, label=""):
    """打印回测 vs 模拟盘对比"""
    if bt_results is None or paper_results is None:
        return

    bt_return = bt_results['full_sample']['total_return_pct']
    pt_return = paper_results['total_return_pct']
    bt_trades = bt_results['full_sample']['total_trades']
    pt_trades = paper_results['num_trades']

    print(f"\n  📊 回测 vs 模拟盘 — {label}")
    print(f"  {'─'*50}")
    print(f"  {'指标':<20} {'回测':>12} {'模拟盘':>12} {'差异':>10}")
    print(f"  {'─'*50}")
    print(f"  {'总收益率':<20} {bt_return:>11.2f}% {pt_return:>11.2f}% {pt_return - bt_return:>9.2f}%")
    print(f"  {'交易笔数':<20} {bt_trades:>12} {pt_trades:>12} {pt_trades - bt_trades:>+10}")


def main():
    parser = argparse.ArgumentParser(description='Day 5+6 模拟盘 + 成本模型验证')
    parser.add_argument('--symbol', default='BTC', help='交易对 (default: BTC)')
    parser.add_argument('--timeframe', default='1h', help='K 线周期 (default: 1h)')
    parser.add_argument('--capital', type=int, default=10000, help='初始资金')
    parser.add_argument('--slow', action='store_true', help='带延迟回放（体验真实时间流逝）')
    args = parser.parse_args()

    global INITIAL_CAPITAL
    INITIAL_CAPITAL = args.capital
    symbol = args.symbol
    symbol_pair = f"{symbol}/USDT"

    print_separator(f"Day 5+6 模拟盘 + 成本模型验证 — {symbol_pair}")
    print(f"  初始资金: {INITIAL_CAPITAL:,} USDT")
    print(f"  回放速度: {'实时模拟' if args.slow else '立即（无延迟）'}")

    # ═══════════════════════════════
    # 1. 加载数据
    # ═══════════════════════════════
    df = load_data(symbol_pair, args.timeframe)

    # ═══════════════════════════════
    # 2. 成本模型演示
    # ═══════════════════════════════
    print_separator("成本模型 — 不同流动性等级对比")

    current_price = df['close'].iloc[-1]
    atr_val = df['atr'].iloc[-1] if 'atr' in df.columns else None

    for tier in [LiquidityTier.HIGH, LiquidityTier.MEDIUM, LiquidityTier.LOW, LiquidityTier.MICRO]:
        cm = CostModel(OrderType.TAKER, tier)
        tier_names = {LiquidityTier.HIGH: "BTC/ETH 主流", LiquidityTier.MEDIUM: "中型币",
                      LiquidityTier.LOW: "小币种", LiquidityTier.MICRO: "Meme/土狗"}
        rt = cm.round_trip_cost(current_price if tier == LiquidityTier.HIGH else 10.0, atr_val)
        print(f"  {tier_names[tier]:<16} | 双向总成本: {rt.total_bps:>8.1f} bps = {rt.total_pct()*100:.4f}%")

    print(f"\n  📝 注意: 小币种/Meme 的成本可以达到交易金额的 5-20%，必须纳入考量！")

    # ═══════════════════════════════
    # 3. 成本分解详情
    # ═══════════════════════════════
    cm_btc = CostModel(OrderType.TAKER, LiquidityTier.HIGH)
    print_cost_breakdown(cm_btc, symbol, current_price, atr_val)

    # ═══════════════════════════════
    # 4. Maker vs Taker 成本对比
    # ═══════════════════════════════
    print_separator("Maker vs Taker 成本对比")
    comparison = CostModel.compare_order_types(current_price, atr_val)
    print(f"\n  市价单 (Taker) 总成本: {comparison['taker'].total_bps:.1f} bps")
    print(f"  限价单 (Maker) 总成本: {comparison['maker'].total_bps:.1f} bps")
    print(f"  节省: {comparison['savings_bps']:.1f} bps = {comparison['savings_pct']*100:.4f}%")
    print(f"  判定: {comparison['verdict']}")

    # ═══════════════════════════════
    # 5. 创建策略
    # ═══════════════════════════════
    print_separator("策略准备")

    trend_strategy = TrendStrategy(fast_period=20, slow_period=50, atr_stop=2.0)
    trend_strategy.precompute(df)
    print(f"  ✅ {trend_strategy.name} — 指标预计算完成")

    momentum_strategy = MomentumStrategy(fast_momentum=20, slow_momentum=50, atr_stop=2.5)
    momentum_strategy.precompute(df)
    print(f"  ✅ {momentum_strategy.name} — 指标预计算完成")

    # ═══════════════════════════════
    # 6. 回测（基准）
    # ═══════════════════════════════
    print_separator("回测基准（用于偏差对比）")

    bt_trend = run_backtest(trend_strategy, df, "趋势跟踪")
    bt_momentum = run_backtest(momentum_strategy, df, "动量策略")

    # ═══════════════════════════════
    # 7. 模拟盘 — 市价单
    # ═══════════════════════════════
    print_separator("模拟盘 — 市价单模式 (TAKER)")

    pt_trend_market = run_paper_trade(trend_strategy, df, 'market', symbol, bt_trend)
    pt_momentum_market = run_paper_trade(momentum_strategy, df, 'market', symbol, bt_momentum)

    # ═══════════════════════════════
    # 8. 模拟盘 — 限价单
    # ═══════════════════════════════
    print_separator("模拟盘 — 限价单模式 (MAKER)")

    pt_trend_limit = run_paper_trade(trend_strategy, df, 'limit', symbol, bt_trend)
    pt_momentum_limit = run_paper_trade(momentum_strategy, df, 'limit', symbol, bt_momentum)

    # ═══════════════════════════════
    # 9. 偏差分析
    # ═══════════════════════════════
    print_separator("偏差分析 — 模拟盘 vs 回测")

    for name, pt, bt in [("趋势跟踪-市价单", pt_trend_market, bt_trend),
                          ("动量策略-市价单", pt_momentum_market, bt_momentum)]:
        print_bt_vs_paper(bt, pt, name)
        if 'trader' in pt:
            pt['trader'].print_deviation_report()

    # ═══════════════════════════════
    # 10. 订单类型对比
    # ═══════════════════════════════
    print_separator("订单类型对比 — 限价单 vs 市价单")

    # 用趋势跟踪策略做对比
    comp_trend = OrderTypeComparison(trend_strategy, df, initial_capital=INITIAL_CAPITAL)
    comp_trend_results = comp_trend.run(symbol)
    comp_trend.print_report()

    # ═══════════════════════════════
    # 11. 成本效率分析
    # ═══════════════════════════════
    print_separator("成本效率分析")

    for name, pt in [("趋势跟踪-市价单", pt_trend_market),
                      ("动量策略-市价单", pt_momentum_market)]:
        if pt['trades'] is not None and len(pt['trades']) > 0:
            eff = analyze_cost_efficiency(pt['trades'])
            if eff:
                print(f"\n  📊 {name}:")
                print(f"     毛利润: {eff['gross_return_sum_pct']:+.2f}%")
                print(f"     净利润: {eff['net_return_sum_pct']:+.2f}%")
                print(f"     总成本: {eff['total_cost_pct']:.2f}%")
                print(f"     成本/毛利: {eff['cost_to_profit_ratio']:.1%}")
                print(f"     被成本'杀死'的交易: {eff['trades_killed_by_cost']} 笔 "
                      f"({eff.get('pct_trades_killed',0):.0f}%)")
                print(f"     判定: {eff['verdict']}")

    # ═══════════════════════════════
    # 12. 最终汇总
    # ═══════════════════════════════
    print_separator("最终汇总")

    print(f"""
  ┌─────────────────────────────────────────────────────────────┐
  │  Day 5+6 验证结论                                           │
  ├─────────────────────────────────────────────────────────────┤
  │  1. 成本模型: 四层成本（手续费+滑点+价差+冲击）嵌入回测/模拟盘 │
  │  2. 模拟盘: 抽象接口设计，将来可切换币安 testnet               │
  │  3. 偏差分析: 量化回测与模拟盘的差距，评估回测可信度           │
  │  4. 订单对比: 限价单省成本但有漏单风险，按策略特征选择         │
  └─────────────────────────────────────────────────────────────┘
""")

    print(f"\n  数据源接口:")
    print(f"    ✅ HistoricalDataFeed (当前) — 本地 parquet 文件回放")
    print(f"    ⬜ BinanceTestnetFeed (将来) — 币安 testnet WebSocket")

    print(f"\n  成交模拟接口:")
    print(f"    ✅ LocalExecutionSim (当前) — CostModel 驱动模拟")
    print(f"    ⬜ BinanceTestnetExecution (将来) — 币安 testnet 订单撮合")

    print(f"\n✅ Day 5+6 验证完成")

    # 保存日志
    logs_dir = os.path.join(DATA_DIR, 'logs')
    os.makedirs(logs_dir, exist_ok=True)

    for name, pt in [("Trend_Market", pt_trend_market), ("Trend_Limit", pt_trend_limit),
                      ("Momentum_Market", pt_momentum_market), ("Momentum_Limit", pt_momentum_limit)]:
        if 'equity_curve' in pt:
            filepath = os.path.join(logs_dir, f"paper_{name}_equity.csv")
            pt['equity_curve'].to_csv(filepath, encoding='utf-8-sig')

    print(f"\n📂 净值曲线已保存到 logs/paper_*_equity.csv")


if __name__ == '__main__':
    main()
