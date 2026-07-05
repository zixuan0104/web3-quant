"""
本地模拟盘引擎 — Day 5 核心模块

职责：
  1. 用历史数据逐 K 线回放，模拟真实交易的时间流逝和信号生成时机
  2. 提供抽象接口 MarketDataFeed / ExecutionSim，后续可切换到币安 testnet
  3. 模拟真实成交条件（限价单需价格触达、市价单有滑点/价差）
  4. 输出模拟盘 vs 回测偏差分析报告

架构：
  ┌──────────────────────────────────────────────┐
  │              PaperTrader（调度核心）            │
  │  策略信号 → 风控检查 → 订单管理 → 日志 → 偏差分析  │
  └──────────┬────────────────┬─────────────────┘
             │                │
      ┌──────▼────┐    ┌──────▼──────┐
      │MarketDataFeed│  │ExecutionSim  │  ← 抽象接口（可插拔）
      │ (行情数据源)   │  │(成交模拟)     │
      └──────┬────┘    └──────┬──────┘
             │                │
    ┌────────▼───┐    ┌───────▼────────┐
    │历史 Parquet  │    │LocalExecutionSim│  ← 当前实现
    │逐行 yield   │    │(CostModel 驱动)  │
    └────────────┘    └────────────────┘
             │                │
    ┌────────▼───┐    ┌───────▼────────┐
    │币安 testnet │    │币安 testnet 订单 │  ← 将来实现
    │WebSocket   │    │提交 + 成交回报    │
    └────────────┘    └────────────────┘

用法：
  from paper_trader import PaperTrader, HistoricalDataFeed, LocalExecutionSim
  from cost_model import CostModel, OrderType, LiquidityTier

  feed = HistoricalDataFeed("clean/BTCUSDT_1h.parquet", replay_speed=0)
  exec_sim = LocalExecutionSim(CostModel(OrderType.TAKER, LiquidityTier.HIGH))
  trader = PaperTrader(strategy, feed, exec_sim, initial_capital=10000)

  results = trader.run()
  trader.print_deviation_report()
"""

import time
import numpy as np
import pandas as pd
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Optional, Dict, List, Any
from enum import Enum


# ═══════════════════════════════
# 抽象接口
# ═══════════════════════════════

class MarketDataFeed(ABC):
    """
    行情数据源抽象

    子类实现：
      - HistoricalDataFeed: 本地 parquet 文件逐行回放
      - BinanceTestnetFeed: 币安 testnet WebSocket（将来）
    """

    @abstractmethod
    def initialize(self, symbol: str, timeframe: str) -> None:
        """初始化数据源"""
        ...

    @abstractmethod
    def get_next_bar(self) -> Optional[dict]:
        """
        返回下一根 K 线，格式:
          {timestamp, open, high, low, close, volume, anomaly, atr}
        数据耗尽返回 None
        """
        ...

    @abstractmethod
    def has_data(self) -> bool:
        """是否还有未消费的数据"""
        ...

    @abstractmethod
    def current_index(self) -> int:
        """当前 K 线序号"""
        ...

    @abstractmethod
    def total_bars(self) -> int:
        """总 K 线数"""
        ...


class ExecutionSim(ABC):
    """
    成交模拟器抽象

    子类实现：
      - LocalExecutionSim: 本地成本模型模拟成交
      - BinanceTestnetExecution: 币安 testnet 真实订单（将来）
    """

    @abstractmethod
    def simulate_entry(self, side: str, signal_price: float, bar: dict,
                       order_type: str = 'market', limit_price: float = None
                       ) -> Optional[dict]:
        """
        模拟入场成交

        返回: {filled, fill_price, slippage_pct, cost_bps, order_type, reason}
        未成交返回 None
        """
        ...

    @abstractmethod
    def simulate_exit(self, side: str, signal_price: float, bar: dict,
                      order_type: str = 'market', limit_price: float = None
                      ) -> Optional[dict]:
        """模拟出场成交"""
        ...


# ═══════════════════════════════
# 订单记录
# ═══════════════════════════════

class OrderStatus(Enum):
    PENDING = "pending"
    FILLED = "filled"
    CANCELLED = "cancelled"
    EXPIRED = "expired"


@dataclass
class SimOrder:
    """模拟订单"""
    order_id: int
    symbol: str
    side: str                 # 'long' | 'short'
    action: str               # 'entry' | 'exit'
    order_type: str           # 'market' | 'limit'
    signal_price: float
    limit_price: Optional[float] = None
    fill_price: Optional[float] = None
    status: OrderStatus = OrderStatus.PENDING
    created_at_idx: int = 0
    filled_at_idx: Optional[int] = None
    cost_bps: float = 0.0
    reject_reason: str = ""


@dataclass
class SimTrade:
    """模拟交易记录"""
    entry_time: Any
    exit_time: Any
    entry_idx: int
    exit_idx: int
    side: str
    entry_price: float
    exit_price: float
    entry_cost_bps: float
    exit_cost_bps: float
    gross_return_pct: float
    net_return_pct: float
    exit_reason: str
    bars_held: int
    order_types: str  # 'market/market', 'limit/limit', 'market/limit' 等


# ═══════════════════════════════
# 本地历史数据源
# ═══════════════════════════════

class HistoricalDataFeed(MarketDataFeed):
    """
    从 parquet 文件逐行回放历史数据

    replay_speed:
      - None / 0: 立即（无延迟，只验证信号逻辑）
      - >0: 每根 K 线间隔 N 秒（模拟真实时间流逝）
      - 'real': 根据 K 线时间戳的真实间隔 sleep
    """

    def __init__(self, data_path: str = None, df: pd.DataFrame = None,
                 replay_speed: Any = 0):
        """
        data_path: parquet 文件路径
        df: 或直接传入 DataFrame（优先级更高）
        replay_speed: 回放速度控制
        """
        self._data_path = data_path
        self._df = df
        self._replay_speed = replay_speed
        self._idx = 0
        self._n = 0
        self._prev_timestamp = None

    def initialize(self, symbol: str = "", timeframe: str = ""):
        """加载数据"""
        if self._df is not None:
            pass  # 已经传入了
        elif self._data_path:
            self._df = pd.read_parquet(self._data_path)
        else:
            raise ValueError("必须提供 data_path 或 df")

        self._n = len(self._df)
        self._idx = 0
        self._prev_timestamp = None
        print(f"📂 模拟盘数据源: {self._n:,} 根 K 线已加载")

    def get_next_bar(self) -> Optional[dict]:
        """返回下一根 K 线，带可选延迟"""
        if self._idx >= self._n:
            return None

        row = self._df.iloc[self._idx]

        # ── 模拟真实时间延迟 ──
        if self._replay_speed and self._replay_speed != 0:
            if isinstance(self._replay_speed, (int, float)) and self._replay_speed > 0:
                time.sleep(self._replay_speed)
            elif self._replay_speed == 'real' and self._prev_timestamp is not None:
                actual_gap = (row.name - self._prev_timestamp).total_seconds()
                if 0 < actual_gap < 3600:  # 最多等 1 小时
                    time.sleep(min(actual_gap, 1.0))  # 缩放到最多 1s

        self._prev_timestamp = row.name if hasattr(row, 'name') else self._idx
        self._idx += 1

        return {
            'timestamp': self._prev_timestamp,
            'open': float(row['open']),
            'high': float(row['high']),
            'low': float(row['low']),
            'close': float(row['close']),
            'volume': float(row['volume']),
            'anomaly': bool(row.get('anomaly', False)),
            'atr': float(row.get('atr', 0)) if 'atr' in row.index else None,
            'adx': float(row.get('adx', 0)) if 'adx' in row.index else None,
        }

    def has_data(self) -> bool:
        return self._idx < self._n

    def current_index(self) -> int:
        return self._idx

    def total_bars(self) -> int:
        return self._n


# ═══════════════════════════════
# 本地成交模拟器
# ═══════════════════════════════

class LocalExecutionSim(ExecutionSim):
    """
    基于 CostModel 的本地成交模拟

    支持两种订单类型：
      - market: 以当前 bar close + 成本模型计算的有效价成交（几乎必成交）
      - limit: 限价单需 bar 的 low/high 触达限价才成交（可能不成交）

    模拟的行为更接近真实交易所：
      - long entry limit buy: 当 bar.low <= limit_price 时成交
      - short entry limit sell: 当 bar.high >= limit_price 时成交
      - long exit limit sell: 当 bar.high >= limit_price 时成交
      - short exit limit buy: 当 bar.low <= limit_price 时成交
    """

    def __init__(self, cost_model=None, min_fill_delay_bars=0):
        """
        cost_model: CostModel 实例
        min_fill_delay_bars: 最小成交延迟（K 线数），0 = 当前 bar 可成交
        """
        from cost_model import CostModel, OrderType, LiquidityTier
        self.cost_model = cost_model or CostModel(OrderType.TAKER, LiquidityTier.HIGH)
        self.min_fill_delay = min_fill_delay_bars
        self.fill_log = []  # 所有成交记录

    def simulate_entry(self, side, signal_price, bar, order_type='market',
                       limit_price=None) -> Optional[dict]:
        """模拟入场成交"""
        return self._simulate(side, 'entry', signal_price, bar, order_type, limit_price)

    def simulate_exit(self, side, signal_price, bar, order_type='market',
                      limit_price=None) -> Optional[dict]:
        """模拟出场成交"""
        return self._simulate(side, 'exit', signal_price, bar, order_type, limit_price)

    def _simulate(self, side, action, signal_price, bar, order_type,
                  limit_price) -> Optional[dict]:
        """
        核心成交逻辑

        市价单: 以信号价 + 成本模型的有效价立即成交
        限价单: 检查 bar 价格范围是否触达限价，触达才成交
        """
        atr = bar.get('atr')
        volume_24h = bar.get('volume_24h')

        if order_type == 'market':
            # ── 市价单：几乎必然成交，但有滑点+价差 ──
            from cost_model import OrderType as OT
            self.cost_model.order_type = OT.TAKER
            fill_price, cost = self.cost_model.effective_fill_price(
                signal_price, side, atr, volume_24h
            )
            result = {
                'filled': True,
                'fill_price': fill_price,
                'signal_price': signal_price,
                'slippage_pct': (fill_price - signal_price) / signal_price
                                if side == 'long' else
                                (signal_price - fill_price) / signal_price,
                'cost_bps': cost.total_bps,
                'cost_breakdown': cost,
                'order_type': 'market',
                'reason': 'market_fill',
            }

        elif order_type == 'limit':
            # ── 限价单：需要价格触达限价 ──
            lp = limit_price or signal_price
            from cost_model import OrderType as OT
            self.cost_model.order_type = OT.MAKER

            filled = False
            fill_price = lp

            if action == 'entry':
                if side == 'long' and bar['low'] <= lp:
                    filled = True  # 买限价单：价格跌到限价以下 = 成交
                elif side == 'short' and bar['high'] >= lp:
                    filled = True  # 卖限价单：价格涨到限价以上 = 成交
            else:  # exit
                if side == 'long' and bar['high'] >= lp:
                    filled = True  # 卖限价单：价格涨到限价以上 = 成交
                elif side == 'short' and bar['low'] <= lp:
                    filled = True  # 买限价单：价格跌到限价以下 = 成交

            if filled:
                # 限价单成交价 = 限价（Maker 无价差成本）
                _, cost = self.cost_model.effective_fill_price(lp, side, atr, volume_24h)
                result = {
                    'filled': True,
                    'fill_price': fill_price,
                    'signal_price': signal_price,
                    'slippage_pct': abs(fill_price - signal_price) / signal_price,
                    'cost_bps': cost.total_bps,
                    'cost_breakdown': cost,
                    'order_type': 'limit',
                    'reason': 'limit_fill',
                }
            else:
                result = {
                    'filled': False,
                    'fill_price': None,
                    'signal_price': signal_price,
                    'slippage_pct': 0,
                    'cost_bps': 0,
                    'cost_breakdown': None,
                    'order_type': 'limit',
                    'reason': 'limit_not_triggered',
                }
        else:
            result = None

        if result:
            self.fill_log.append({**result, 'side': side, 'action': action,
                                  'bar_idx': bar.get('timestamp', 0)})
        return result

    def get_fill_stats(self) -> dict:
        """成交统计"""
        if not self.fill_log:
            return {'total_attempts': 0, 'filled': 0, 'fill_rate': 0}

        filled = [f for f in self.fill_log if f['filled']]
        market = [f for f in self.fill_log if f.get('order_type') == 'market']
        limit = [f for f in self.fill_log if f.get('order_type') == 'limit']
        limit_filled = [f for f in limit if f['filled']]

        return {
            'total_attempts': len(self.fill_log),
            'filled': len(filled),
            'fill_rate': len(filled) / len(self.fill_log) if self.fill_log else 0,
            'market_fills': len(market),
            'limit_attempts': len(limit),
            'limit_fills': len(limit_filled),
            'limit_fill_rate': len(limit_filled) / len(limit) if limit else 0,
            'avg_cost_bps': np.mean([f['cost_bps'] for f in filled]) if filled else 0,
        }


# ═══════════════════════════════
# 模拟盘引擎
# ═══════════════════════════════

class PaperTrader:
    """
    本地模拟盘引擎

    流程：
      1. 从 MarketDataFeed 逐 K 线读取
      2. 每根 K 线调用策略的 on_bar() → 获取信号
      3. 通过 ExecutionSim 模拟成交
      4. 记录成交/持仓/净值
      5. 结束后与回测结果对比，输出偏差分析
    """

    def __init__(self, strategy, data_feed: MarketDataFeed, execution_sim: ExecutionSim,
                 initial_capital=10000, order_type='limit'):
        """
        strategy: BaseStrategy 子类实例（和回测用同一个策略）
        data_feed: MarketDataFeed 实现
        execution_sim: ExecutionSim 实现
        initial_capital: 初始资金
        order_type: 默认订单类型 'market' | 'limit'
        """
        self.strategy = strategy
        self.feed = data_feed
        self.exec_sim = execution_sim
        self.initial_capital = initial_capital
        self.default_order_type = order_type

        # ── 运行状态 ──
        self.equity = initial_capital
        self.equity_curve = []
        self.trades: List[SimTrade] = []
        self.position = None  # 模拟盘的持仓（独立于策略的 position）
        self.pending_orders: List[SimOrder] = []
        self.order_counter = 0
        self.backtest_results = None  # 外部注入，用于偏差对比

    # ═══════════════════════════════
    # 主循环
    # ═══════════════════════════════

    def run(self, symbol="UNKNOWN", verbose=True) -> dict:
        """
        执行模拟盘

        返回: dict {
          equity_curve, trades, fill_stats, deviation_report, ...
        }
        """
        if verbose:
            print(f"\n📊 模拟盘启动: {self.strategy.name}")
            print(f"   初始资金: {self.initial_capital:,.0f} USDT")
            print(f"   订单类型: {self.default_order_type}")
            print(f"   总 K 线: {self.feed.total_bars():,}")

        # ── 重置策略状态（与回测独立）──
        self.strategy.position = None
        self.strategy.trade_log = []
        self.strategy.bar_log = []

        bar_count = 0
        signal_count = 0
        missed_signals = 0

        while self.feed.has_data():
            bar = self.feed.get_next_bar()
            if bar is None:
                break
            bar_count += 1

            # ── 更新模拟盘持仓的浮动净值 ──
            self._update_equity(bar, bar_count)

            # ── 检查挂单是否成交 ──
            self._process_pending_orders(bar, bar_count)

            # ── 策略信号生成（和回测完全一样的逻辑）──
            result = self.strategy.on_bar(
                pd.Series(bar, name=bar['timestamp']), bar_count - 1
            )

            if result is None:
                continue

            signal_count += 1

            # ── 模拟成交 ──
            if result['action'] == 'entry':
                fill = self.exec_sim.simulate_entry(
                    side=result['side'],
                    signal_price=result['price'],
                    bar=bar,
                    order_type=self.default_order_type,
                )
                if fill and fill['filled']:
                    self._open_sim_position(result['side'], fill['fill_price'],
                                            bar, bar_count, fill)
                else:
                    missed_signals += 1

            elif result['action'] == 'exit':
                if self.position:
                    fill = self.exec_sim.simulate_exit(
                        side=result['side'],
                        signal_price=result['price'],
                        bar=bar,
                        order_type=self.default_order_type,
                    )
                    if fill and fill['filled']:
                        self._close_sim_position(fill['fill_price'], bar, bar_count,
                                                 result.get('reason', 'signal'), fill)
                    else:
                        missed_signals += 1

            # ── 进度打印 ──
            if verbose and bar_count % 500 == 0:
                print(f"   ... {bar_count}/{self.feed.total_bars()} 根 K 线, "
                      f"{len(self.trades)} 笔交易, 净值 {self.equity:,.0f}")

        if verbose:
            print(f"\n✅ 模拟盘完成: {bar_count} 根 K 线, {signal_count} 次信号, "
                  f"{len(self.trades)} 笔交易")
            print(f"   错失信号: {missed_signals} (限价单未触达)")
            print(f"   最终净值: {self.equity:,.2f} USDT "
                  f"({(self.equity/self.initial_capital - 1)*100:+.2f}%)")

        return self._build_results(symbol)

    # ═══════════════════════════════
    # 模拟持仓管理
    # ═══════════════════════════════

    def _open_sim_position(self, side, fill_price, bar, idx, fill_result):
        """开仓"""
        self.position = {
            'side': side,
            'entry_price': fill_price,
            'entry_time': bar['timestamp'],
            'entry_idx': idx,
            'entry_cost_bps': fill_result['cost_bps'],
            'entry_fill': fill_result,
        }

    def _close_sim_position(self, fill_price, bar, idx, reason, fill_result):
        """平仓并记录交易"""
        pos = self.position
        entry_price = pos['entry_price']
        side = pos['side']

        # ── 计算收益 ──
        if side == 'long':
            gross_return = (fill_price - entry_price) / entry_price
        else:
            gross_return = (entry_price - fill_price) / entry_price

        total_cost_pct = (pos['entry_cost_bps'] + fill_result['cost_bps']) / 10000
        net_return = gross_return - total_cost_pct

        trade = SimTrade(
            entry_time=pos['entry_time'],
            exit_time=bar['timestamp'],
            entry_idx=pos['entry_idx'],
            exit_idx=idx,
            side=side,
            entry_price=entry_price,
            exit_price=fill_price,
            entry_cost_bps=pos['entry_cost_bps'],
            exit_cost_bps=fill_result['cost_bps'],
            gross_return_pct=gross_return * 100,
            net_return_pct=net_return * 100,
            exit_reason=reason,
            bars_held=idx - pos['entry_idx'],
            order_types=f"{pos['entry_fill']['order_type']}/{fill_result['order_type']}",
        )
        self.trades.append(trade)

        # ── 更新净值 ──
        self.equity *= (1 + net_return)

        self.position = None

    def _process_pending_orders(self, bar, idx):
        """处理挂单队列（限价单可能跨 bar 成交）"""
        # 当前实现：限价单在同一根 bar 内成交或不成交
        # 将来可扩展为挂单簿模式
        pass

    def _update_equity(self, bar, idx):
        """记录净值曲线（含浮动盈亏）"""
        current_equity = self.equity
        if self.position:
            pos = self.position
            if pos['side'] == 'long':
                unrealized = (bar['close'] - pos['entry_price']) / pos['entry_price']
            else:
                unrealized = (pos['entry_price'] - bar['close']) / pos['entry_price']
            current_equity = self.equity * (1 + unrealized)

        self.equity_curve.append({
            'timestamp': bar['timestamp'],
            'equity': current_equity,
            'return': current_equity / self.initial_capital - 1,
        })

    # ═══════════════════════════════
    # 结果 + 偏差分析
    # ═══════════════════════════════

    def _build_results(self, symbol) -> dict:
        equity_df = pd.DataFrame(self.equity_curve).set_index('timestamp')
        trades_df = pd.DataFrame([t.__dict__ for t in self.trades]) if self.trades else pd.DataFrame()
        fill_stats = self.exec_sim.get_fill_stats()

        results = {
            'symbol': symbol,
            'strategy': self.strategy.name,
            'initial_capital': self.initial_capital,
            'final_equity': self.equity,
            'total_return_pct': (self.equity / self.initial_capital - 1) * 100,
            'equity_curve': equity_df,
            'trades': trades_df,
            'num_trades': len(self.trades),
            'fill_stats': fill_stats,
            'order_type': self.default_order_type,
        }

        # ── 如果有回测结果，做偏差分析 ──
        if self.backtest_results is not None:
            results['deviation'] = self._analyze_deviation()

        return results

    def set_backtest_results(self, bt_results: dict):
        """注入回测结果用于偏差对比"""
        self.backtest_results = bt_results

    def _analyze_deviation(self) -> dict:
        """
        偏差分析：模拟盘 vs 回测

        对比维度：
          1. 信号一致性 — 同一 bar 是否产生相同信号
          2. 成交价偏差 — 模拟成交价 vs 回测理想成交价
          3. 滑点分布 — 实际滑点 vs 回测假设的 0.05%
          4. 交易笔数差异 — 限价单漏单 vs 全部成交
          5. 净值偏差 — 最终净值差异
        """
        bt = self.backtest_results
        pt_trades = self.trades
        bt_trades_df = bt.get('trade_log', pd.DataFrame())

        # ── 信号一致性 ──
        # 比较策略 bar_log 和回测的 entry/exit 时序
        signal_match_rate = self._compare_signals()

        # ── 成交价偏差 ──
        price_deviation = self._compare_fill_prices(bt_trades_df)

        # ── 滑点分布 ──
        slippage_stats = self._slippage_distribution()

        # ── 净值偏差 ──
        bt_return = bt.get('full_sample', {}).get('total_return_pct', 0)
        pt_return = (self.equity / self.initial_capital - 1) * 100
        equity_deviation = pt_return - bt_return

        deviation = {
            'signal_match_rate': signal_match_rate,
            'price_deviation': price_deviation,
            'slippage_stats': slippage_stats,
            'backtest_return_pct': bt_return,
            'paper_return_pct': pt_return,
            'equity_deviation_pct': equity_deviation,
            'verdict': self._deviation_verdict(equity_deviation, signal_match_rate),
        }
        return deviation

    def _compare_signals(self) -> float:
        """对比模拟盘和回测的信号一致性"""
        pt_entries = set(t.entry_idx for t in self.trades)
        if self.backtest_results and 'trade_log' in self.backtest_results:
            bt_df = self.backtest_results['trade_log']
            if not bt_df.empty:
                bt_entries = set(bt_df['entry_idx'].values)
                if bt_entries:
                    overlap = len(pt_entries & bt_entries)
                    return overlap / len(bt_entries | pt_entries) if (bt_entries | pt_entries) else 1.0
        return 1.0  # 无回测数据时默认一致

    def _compare_fill_prices(self, bt_trades_df) -> dict:
        """对比成交价偏差"""
        if bt_trades_df.empty or not self.trades:
            return {'mean_deviation_pct': 0, 'max_deviation_pct': 0}

        deviations = []
        for pt in self.trades:
            bt_match = bt_trades_df[bt_trades_df['entry_idx'] == pt.entry_idx]
            if not bt_match.empty:
                bt_entry = bt_match.iloc[0]['entry_price']
                deviation = abs(pt.entry_price - bt_entry) / bt_entry * 100
                deviations.append(deviation)

        return {
            'mean_deviation_pct': np.mean(deviations) if deviations else 0,
            'max_deviation_pct': np.max(deviations) if deviations else 0,
            'sample_count': len(deviations),
        }

    def _slippage_distribution(self) -> dict:
        """滑点分布统计"""
        if not self.trades:
            return {}

        entry_slippages = []
        exit_slippages = []
        for t in self.trades:
            entry_slippages.append(t.entry_cost_bps)
            exit_slippages.append(t.exit_cost_bps)

        return {
            'entry_avg_bps': np.mean(entry_slippages) if entry_slippages else 0,
            'entry_max_bps': np.max(entry_slippages) if entry_slippages else 0,
            'exit_avg_bps': np.mean(exit_slippages) if exit_slippages else 0,
            'exit_max_bps': np.max(exit_slippages) if exit_slippages else 0,
            'total_avg_bps': np.mean(entry_slippages + exit_slippages) if entry_slippages else 0,
        }

    def _deviation_verdict(self, equity_deviation, signal_match) -> str:
        """偏差判定"""
        if abs(equity_deviation) < 2 and signal_match > 0.95:
            return "✅ 回测可信 — 模拟盘与回测高度一致"
        elif abs(equity_deviation) < 5 and signal_match > 0.85:
            return "⚠️ 轻微偏差 — 可接受范围，关注滑点模型"
        elif abs(equity_deviation) < 10:
            return "🔶 中度偏差 — 建议检查成本模型参数和限价单漏单"
        else:
            return "🔴 严重偏差 — 回测假设与实盘差距大，需调成本模型或检查未来函数"

    def print_deviation_report(self):
        """打印偏差分析报告"""
        if self.backtest_results is None:
            print("\n⚠️ 未注入回测结果，无法做偏差分析。"
                  "请调用 paper_trader.set_backtest_results(bt_results)")
            return

        dev = self._analyze_deviation()

        print(f"\n{'='*60}")
        print(f"  模拟盘 vs 回测 — 偏差分析报告")
        print(f"{'='*60}")
        print(f"\n  📡 信号一致性: {dev['signal_match_rate']:.1%}")
        print(f"  💰 回测收益:   {dev['backtest_return_pct']:+.2f}%")
        print(f"  💰 模拟盘收益: {dev['paper_return_pct']:+.2f}%")
        print(f"  📉 净值偏差:   {dev['equity_deviation_pct']:+.2f}%")

        pd = dev['price_deviation']
        if pd:
            print(f"\n  🎯 成交价偏差 (vs 信号价):")
            print(f"     平均: {pd.get('mean_deviation_pct', 0):.3f}%")
            print(f"     最大: {pd.get('max_deviation_pct', 0):.3f}%")
            print(f"     样本: {pd.get('sample_count', 0)} 笔")

        ss = dev['slippage_stats']
        if ss:
            print(f"\n  📏 滑点分布 (bps):")
            print(f"     入场平均: {ss.get('entry_avg_bps', 0):.1f} bps")
            print(f"     出场平均: {ss.get('exit_avg_bps', 0):.1f} bps")
            print(f"     综合平均: {ss.get('total_avg_bps', 0):.1f} bps")
            print(f"     回测假设: 5.0 bps (固定 0.05%)")

        print(f"\n  📋 判定: {dev['verdict']}")

    def get_trade_summary(self) -> dict:
        """交易摘要统计"""
        if not self.trades:
            return {'total_trades': 0}

        wins = [t for t in self.trades if t.net_return_pct > 0]
        losses = [t for t in self.trades if t.net_return_pct <= 0]

        return {
            'total_trades': len(self.trades),
            'win_count': len(wins),
            'loss_count': len(losses),
            'win_rate': len(wins) / len(self.trades) * 100 if self.trades else 0,
            'avg_win_pct': np.mean([t.net_return_pct for t in wins]) if wins else 0,
            'avg_loss_pct': np.mean([t.net_return_pct for t in losses]) if losses else 0,
            'avg_bars_held': np.mean([t.bars_held for t in self.trades]) if self.trades else 0,
            'best_trade_pct': max(t.net_return_pct for t in self.trades) if self.trades else 0,
            'worst_trade_pct': min(t.net_return_pct for t in self.trades) if self.trades else 0,
        }
