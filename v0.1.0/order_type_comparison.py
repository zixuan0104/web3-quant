"""
订单类型对比引擎 — Day 6

用同一策略、同一数据，分别以市价单和限价单跑模拟盘，
对比成交率、成交价、滑点损失、错失信号率，输出结构化的对比报告。

核心输出：对当前策略，限价单能省多少成本，但代价是多大的不成交风险？

用法：
  from order_type_comparison import OrderTypeComparison
  comp = OrderTypeComparison(strategy, df, CostModel)
  report = comp.run()
  comp.print_report()
"""

import numpy as np
import pandas as pd
from dataclasses import dataclass


@dataclass
class ComparisonResult:
    """单次对比结果"""
    order_type: str
    num_trades: int
    fill_rate: float
    total_return_pct: float
    avg_cost_per_trade_bps: float
    avg_slippage_bps: float
    avg_fill_price_deviation_pct: float
    missed_signals: int
    equity_curve: pd.DataFrame


class OrderTypeComparison:
    """
    限价单 vs 市价单对比

    方法：
      1. 跑两遍模拟盘（market 和 limit 模式）
      2. 对齐交易对，逐一对比成交价和收益
      3. 输出对比报告 + 推荐
    """

    def __init__(self, strategy, data_df: pd.DataFrame, cost_model=None,
                 initial_capital=10000):
        """
        strategy: BaseStrategy 实例（会被重置两次）
        data_df: 历史 K 线 DataFrame（已包含 ATR 等指标列）
        cost_model: CostModel 实例（可选，默认做对比时各自构造）
        initial_capital: 初始资金
        """
        self.strategy_class = strategy.__class__
        self.strategy_kwargs = self._capture_strategy_params(strategy)
        self.strategy_name = strategy.name
        self.data_df = data_df
        self.initial_capital = initial_capital

        from cost_model import CostModel, OrderType, LiquidityTier
        self.CostModel = CostModel
        self.OrderType = OrderType
        self.LiquidityTier = LiquidityTier

        self.market_result = None
        self.limit_result = None
        self.comparison = {}

    def _capture_strategy_params(self, strategy) -> dict:
        """捕获策略的可配置参数（仅 __init__ 接受的参数）"""
        # 策略构造函数接受的参数（按策略类型）
        known_params = {
            'TrendStrategy': ['fast_period', 'slow_period', 'atr_stop'],
            'MomentumStrategy': ['fast_momentum', 'slow_momentum', 'atr_stop'],
            'BreakoutStrategy': ['breakout_period', 'breakout_threshold', 'atr_stop'],
            'FundingArbStrategy': [],
        }
        cls_name = strategy.__class__.__name__
        param_names = known_params.get(cls_name, [])

        params = {}
        for attr in param_names:
            if hasattr(strategy, attr):
                val = getattr(strategy, attr)
                if not callable(val):
                    params[attr] = val
        return params

    def _create_strategy(self):
        """创建策略新实例（避免状态污染）"""
        return self.strategy_class(**self.strategy_kwargs)

    def _get_liquidity_tier(self, symbol: str):
        """根据代币确定流动性等级"""
        from cost_model import CostModel
        return CostModel.classify_liquidity(symbol)

    def run(self, symbol="BTC", atr_column='atr'):
        """
        跑两遍模拟盘，输出对比报告

        返回: dict {market: ComparisonResult, limit: ComparisonResult, comparison: dict}
        """
        from paper_trader import PaperTrader, HistoricalDataFeed, LocalExecutionSim
        from cost_model import CostModel, OrderType
        from copy import deepcopy

        liquidity_tier = self._get_liquidity_tier(symbol)
        df = self.data_df.copy()

        print(f"\n{'='*60}")
        print(f"  订单类型对比: {symbol}")
        print(f"  流动性等级: {liquidity_tier.value}")
        print(f"  策略: {self.strategy_name}")
        print(f"{'='*60}")

        # ── 跑市价单 ──
        print(f"\n── 市价单 (TAKER) ──")
        market_feed = HistoricalDataFeed(df=df.copy(), replay_speed=0)
        market_feed.initialize()
        market_cm = CostModel(OrderType.TAKER, liquidity_tier)
        market_exec = LocalExecutionSim(market_cm)
        market_strategy = self._create_strategy()
        market_strategy.precompute(df)
        market_trader = PaperTrader(market_strategy, market_feed, market_exec,
                                     self.initial_capital, order_type='market')
        market_results = market_trader.run(symbol, verbose=True)

        self.market_result = ComparisonResult(
            order_type='market',
            num_trades=market_results['num_trades'],
            fill_rate=market_results['fill_stats'].get('fill_rate', 1.0),
            total_return_pct=market_results['total_return_pct'],
            avg_cost_per_trade_bps=self._avg_cost(market_results['trades']),
            avg_slippage_bps=market_results['fill_stats'].get('avg_cost_bps', 0),
            avg_fill_price_deviation_pct=0,  # 市价单以信号价成交，偏差为成本
            missed_signals=market_results['fill_stats'].get('total_attempts', 0) -
                           market_results['fill_stats'].get('filled', 0),
            equity_curve=market_results['equity_curve'],
        )

        # ── 跑限价单 ──
        print(f"\n── 限价单 (MAKER) ──")
        limit_feed = HistoricalDataFeed(df=df.copy(), replay_speed=0)
        limit_feed.initialize()
        limit_cm = CostModel(OrderType.MAKER, liquidity_tier)
        limit_exec = LocalExecutionSim(limit_cm)
        limit_strategy = self._create_strategy()
        limit_strategy.precompute(df)
        limit_trader = PaperTrader(limit_strategy, limit_feed, limit_exec,
                                    self.initial_capital, order_type='limit')
        limit_results = limit_trader.run(symbol, verbose=True)

        self.limit_result = ComparisonResult(
            order_type='limit',
            num_trades=limit_results['num_trades'],
            fill_rate=limit_results['fill_stats'].get('limit_fill_rate', 0),
            total_return_pct=limit_results['total_return_pct'],
            avg_cost_per_trade_bps=self._avg_cost(limit_results['trades']),
            avg_slippage_bps=limit_results['fill_stats'].get('avg_cost_bps', 0),
            avg_fill_price_deviation_pct=0,
            missed_signals=limit_results['fill_stats'].get('limit_attempts', 0) -
                           limit_results['fill_stats'].get('limit_fills', 0),
            equity_curve=limit_results['equity_curve'],
        )

        # ── 构建对比 ──
        self._build_comparison()

        return {
            'market': self.market_result,
            'limit': self.limit_result,
            'comparison': self.comparison,
        }

    def _avg_cost(self, trades) -> float:
        """计算每笔交易的平均成本（bps）"""
        if trades is None:
            return 0
        # DataFrame from paper trader results
        if isinstance(trades, pd.DataFrame):
            if trades.empty:
                return 0
            if 'entry_cost_bps' in trades.columns:
                return (trades['entry_cost_bps'] + trades['exit_cost_bps']).mean()
            return 0
        # list of SimTrade objects
        if isinstance(trades, list):
            return np.mean([t.entry_cost_bps + t.exit_cost_bps for t in trades]) if trades else 0
        return 0

    def _build_comparison(self):
        """构建对比数据"""
        m = self.market_result
        l = self.limit_result

        cost_savings_bps = m.avg_cost_per_trade_bps - l.avg_cost_per_trade_bps
        # 限价单省了成本，但可能错失交易 → 比较最终收益
        return_diff = l.total_return_pct - m.total_return_pct

        # 判断哪种更优
        if l.fill_rate >= 0.9 and return_diff > 0:
            verdict = "限价单更优 ✅ — 成本节省超过错失交易的损失"
            recommendation = "建议使用限价单，可额外设置 0.1% 容忍价差提高成交率"
        elif l.fill_rate < 0.7:
            verdict = "市价单更优 ✅ — 限价单成交率过低，错失太多信号"
            recommendation = "建议使用市价单，当前策略对成交时机敏感"
        elif abs(return_diff) < 1:
            verdict = "差异不显著 ➖ — 两者差距在噪声范围内"
            recommendation = "优先选限价单（更低成本），如果成交率持续 <80% 切换为市价单"
        else:
            verdict = f"限价单{'更优' if return_diff > 0 else '更差'} — 收益差 {return_diff:+.2f}%"
            recommendation = "根据回测期间行情特征决定"

        self.comparison = {
            'cost_savings_bps': cost_savings_bps,
            'return_diff_pct': return_diff,
            'fill_rate_diff': l.fill_rate - m.fill_rate,
            'trade_count_diff': l.num_trades - m.num_trades,
            'verdict': verdict,
            'recommendation': recommendation,
        }

    def print_report(self):
        """打印对比报告"""
        if self.market_result is None or self.limit_result is None:
            print("⚠️ 请先调用 run() 执行对比")
            return

        m = self.market_result
        l = self.limit_result
        c = self.comparison

        print(f"\n{'='*60}")
        print(f"  订单类型对比报告")
        print(f"{'='*60}")

        print(f"\n{'指标':<24} {'市价单 (Taker)':>15} {'限价单 (Maker)':>15}")
        print(f"{'-'*54}")
        print(f"{'交易笔数':<24} {m.num_trades:>15} {l.num_trades:>15}")
        print(f"{'成交率':<24} {m.fill_rate:>14.1%} {l.fill_rate:>14.1%}")
        print(f"{'总收益率':<24} {m.total_return_pct:>14.2f}% {l.total_return_pct:>14.2f}%")
        print(f"{'平均成本/笔':<24} {m.avg_cost_per_trade_bps:>13.1f} bps {l.avg_cost_per_trade_bps:>13.1f} bps")
        print(f"{'平均滑点':<24} {m.avg_slippage_bps:>13.1f} bps {l.avg_slippage_bps:>13.1f} bps")
        print(f"{'错失信号':<24} {m.missed_signals:>15} {l.missed_signals:>15}")

        print(f"\n{'─'*54}")
        print(f"  💰 成本节省: {c['cost_savings_bps']:.1f} bps/笔 (限价单 vs 市价单)")
        print(f"  📈 收益差: {c['return_diff_pct']:+.2f}% (限价单 - 市价单)")
        print(f"  🎯 成交率差: {c['fill_rate_diff']:+.1%}")
        print(f"  📊 交易数差: {c['trade_count_diff']:+d} 笔")
        print(f"\n  📋 判定: {c['verdict']}")
        print(f"  💡 建议: {c['recommendation']}")

    def get_summary_table(self) -> str:
        """返回 Markdown 格式的对比表"""
        if self.market_result is None:
            return ""

        m = self.market_result
        l = self.limit_result
        c = self.comparison

        return f"""
### 订单类型对比总结

| 指标 | 市价单 (Taker) | 限价单 (Maker) | 差异 |
|------|:---:|:---:|:---:|
| 交易笔数 | {m.num_trades} | {l.num_trades} | {l.num_trades - m.num_trades:+d} |
| 成交率 | {m.fill_rate:.0%} | {l.fill_rate:.0%} | {l.fill_rate - m.fill_rate:+.0%} |
| 总收益率 | {m.total_return_pct:+.2f}% | {l.total_return_pct:+.2f}% | {c['return_diff_pct']:+.2f}% |
| 平均成本/笔 | {m.avg_cost_per_trade_bps:.0f} bps | {l.avg_cost_per_trade_bps:.0f} bps | {c['cost_savings_bps']:.0f} bps |
| 错失信号 | {m.missed_signals} | {l.missed_signals} | {l.missed_signals - m.missed_signals:+d} |

**判定**: {c['verdict']}

**建议**: {c['recommendation']}
"""


# ═══════════════════════════════
# 成本效率分析（独立工具）
# ═══════════════════════════════

def analyze_cost_efficiency(trades_df, cost_model=None):
    """
    分析交易的成本效率

    核心问题：交易成本吃掉了多少利润？

    返回: dict {
      gross_profit, total_cost, net_profit, cost_to_profit_ratio,
      would_be_profitable_without_cost, ...
    }
    """
    if trades_df is None or trades_df.empty:
        return {}

    if isinstance(trades_df, list):
        trades = trades_df
        gross_returns = [t.gross_return_pct for t in trades]
        net_returns = [t.net_return_pct for t in trades]
        total_cost = sum(t.entry_cost_bps + t.exit_cost_bps for t in trades)
    else:
        gross_returns = trades_df['gross_return_pct'].values
        net_returns = trades_df['net_return_pct'].values
        total_cost = 0

    gross_sum = sum(gross_returns)
    net_sum = sum(net_returns)
    cost_total = gross_sum - net_sum

    # 被成本"杀死"的盈利交易
    killed_trades = sum(1 for g, n in zip(gross_returns, net_returns)
                        if g > 0 and n <= 0)

    n_trades = len(list(gross_returns)) if hasattr(gross_returns, '__iter__') else 0

    return {
        'gross_return_sum_pct': gross_sum,
        'net_return_sum_pct': net_sum,
        'total_cost_pct': cost_total,
        'cost_to_profit_ratio': cost_total / gross_sum if gross_sum > 0 else float('inf'),
        'trades_killed_by_cost': killed_trades,
        'pct_trades_killed': killed_trades / n_trades * 100 if n_trades > 0 else 0,
        'verdict': (f"成本吃掉 {cost_total/gross_sum*100:.0f}% 毛利"
                    if gross_sum > 0 else "无毛利可比较"),
    }
