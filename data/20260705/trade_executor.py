"""
交易执行模块 — Day 7 核心模块

三层抽象：
  1. Exchange（抽象基类）— 定义 submit_order / cancel_order / get_balance / get_positions
  2. PaperExchange — 本地模拟撮合（当前使用，不需要 API key）
  3. BinanceExchange — 真实币安 API（骨架就绪，API key 到了补 submit_order 实现）

订单生命周期：
  信号 → 风控检查 → create_order → submit_order → (open → filled / cancelled / expired)
                                                    └→ 定时检查成交状态

设计：所有订单操作通过 Exchange 接口，上层代码不感知 paper 还是 live。

用法：
  from trade_executor import PaperExchange, create_exchange

  exchange = create_exchange(mode='paper', initial_capital=10000, logger=logger)
  order = exchange.submit_order(symbol='BTC/USDT', side='long', action='entry',
                                 order_type='limit', price=65000, quantity=0.01)
"""

import time
import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Optional, Dict, List, Any
from enum import Enum


# ═══════════════════════════════
# 数据类型
# ═══════════════════════════════

class OrderStatus(Enum):
    CREATED = "created"        # 已创建，等待提交
    SUBMITTED = "submitted"    # 已提交到交易所
    PARTIALLY_FILLED = "partially_filled"
    FILLED = "filled"
    CANCELLED = "cancelled"
    EXPIRED = "expired"
    REJECTED = "rejected"


class OrderAction(Enum):
    ENTRY = "entry"
    EXIT = "exit"


@dataclass
class Order:
    """订单"""
    order_id: str
    symbol: str
    side: str                 # 'long' | 'short'
    action: OrderAction       # entry | exit
    order_type: str           # 'market' | 'limit'
    price: float
    quantity: float
    status: OrderStatus = OrderStatus.CREATED
    fill_price: Optional[float] = None
    fill_quantity: Optional[float] = None
    created_at: float = field(default_factory=time.time)
    filled_at: Optional[float] = None
    cost_bps: float = 0.0
    strategy_name: str = ""
    exchange_order_id: str = ""  # 交易所返回的订单 ID
    notes: str = ""


@dataclass
class PositionInfo:
    """持仓信息"""
    symbol: str
    side: str                 # 'long' | 'short'
    quantity: float
    entry_price: float
    current_price: float = 0.0
    unrealized_pnl: float = 0.0
    unrealized_pnl_pct: float = 0.0


@dataclass
class BalanceInfo:
    """账户余额"""
    total_equity: float = 0.0
    available_balance: float = 0.0
    locked_balance: float = 0.0    # 挂单锁定的余额
    unrealized_pnl: float = 0.0


# ═══════════════════════════════
# 抽象 Exchange 接口
# ═══════════════════════════════

class Exchange(ABC):
    """交易所抽象接口"""

    @abstractmethod
    def submit_order(self, symbol: str, side: str, action: str, order_type: str,
                     price: float, quantity: float, strategy_name: str = "",
                     **kwargs) -> Optional[Order]:
        """
        提交订单到交易所

        返回: Order 对象（含 order_id 和状态），失败返回 None
        """
        ...

    @abstractmethod
    def cancel_order(self, order_id: str) -> bool:
        """取消订单"""
        ...

    @abstractmethod
    def get_order_status(self, order_id: str) -> OrderStatus:
        """查询订单状态"""
        ...

    @abstractmethod
    def get_balance(self) -> BalanceInfo:
        """查询账户余额"""
        ...

    @abstractmethod
    def get_positions(self) -> List[PositionInfo]:
        """查询当前持仓"""
        ...

    @abstractmethod
    def get_order(self, order_id: str) -> Optional[Order]:
        """查询订单详情"""
        ...

    @abstractmethod
    def is_connected(self) -> bool:
        """交易所连接是否正常"""
        ...


# ═══════════════════════════════
# PaperExchange — 本地模拟撮合
# ═══════════════════════════════

class PaperExchange(Exchange):
    """
    本地模拟交易所

    不连接真实交易所，用本地 CostModel 模拟订单撮合：
      - 市价单: 立即以 bar close + 成本模型成交
      - 限价单: 根据 bar 的 high/low 判断是否触达

    模拟余额、持仓、PnL。用于 paper 模式开发和验证。
    """

    def __init__(self, initial_capital=10000, cost_model=None, logger=None,
                 order_expiry_bars=24):
        self.initial_capital = initial_capital
        self.cost_model = cost_model
        self.logger = logger
        self.order_expiry_bars = order_expiry_bars

        # ── 账户状态 ──
        self.available_balance = initial_capital
        self.locked_balance = 0.0

        # ── 订单簿 ──
        self.orders: Dict[str, Order] = {}
        self.order_history: List[Order] = []

        # ── 持仓 ──
        self.positions: Dict[str, PositionInfo] = {}  # symbol → position

        self._current_bar_idx = 0
        self._connected = True

    @property
    def total_equity(self) -> float:
        """总净值 = 可用余额 + 持仓市值 + 锁定的余额"""
        pos_value = 0.0
        for p in self.positions.values():
            pos_value += p.quantity * p.current_price
        return self.available_balance + self.locked_balance + pos_value

    # ═══════════════════════════════
    # 订单提交
    # ═══════════════════════════════

    def submit_order(self, symbol: str, side: str, action: str, order_type: str,
                     price: float, quantity: float, strategy_name: str = "",
                     current_bar: dict = None, **kwargs) -> Optional[Order]:
        """
        提交模拟订单

        市价单: 立即以 current_bar 的 close 成交
        限价单: 创建挂单，等待价格触达后成交
        """
        order_id = f"paper-{uuid.uuid4().hex[:12]}"

        order = Order(
            order_id=order_id,
            symbol=symbol,
            side=side,
            action=OrderAction.ENTRY if action == 'entry' else OrderAction.EXIT,
            order_type=order_type,
            price=price,
            quantity=quantity,
            status=OrderStatus.CREATED,
            strategy_name=strategy_name,
        )

        if self.logger:
            self.logger.trade_submitted(
                order_id=order_id, symbol=symbol, side=side,
                action=action, order_type=order_type,
                price=price, quantity=quantity,
            )

        # ── 市价单: 立即成交 ──
        if order_type == 'market' and current_bar:
            fill_price = self._compute_fill_price(side, price, current_bar)
            order.status = OrderStatus.FILLED
            order.fill_price = fill_price
            order.fill_quantity = quantity
            order.filled_at = time.time()
            self._apply_fill(order, fill_price)
            self.order_history.append(order)

            if self.logger:
                self.logger.trade_filled(
                    order_id=order_id, fill_price=fill_price,
                    fill_quantity=quantity,
                )
            return order

        # ── 限价单: 加入挂单簿 ──
        order.status = OrderStatus.SUBMITTED
        order.created_at_bar = current_bar.get('_idx', 0) if current_bar else 0  # 记录创建时的 bar 序号
        self.orders[order_id] = order

        # 锁定余额（以防超买）
        if action == 'entry':
            lock_amount = price * quantity
            if lock_amount <= self.available_balance:
                self.available_balance -= lock_amount
                self.locked_balance += lock_amount
            else:
                order.status = OrderStatus.REJECTED
                order.notes = f"余额不足: 需要 {lock_amount:.0f} USDT, 可用 {self.available_balance:.0f}"
                self.orders.pop(order_id, None)
                return None

        return order

    def cancel_order(self, order_id: str) -> bool:
        """取消挂单"""
        if order_id not in self.orders:
            return False

        order = self.orders[order_id]
        order.status = OrderStatus.CANCELLED

        # 释放锁定的余额
        if order.action == OrderAction.ENTRY:
            unlock = order.price * order.quantity
            self.locked_balance -= unlock
            self.available_balance += unlock

        self.order_history.append(order)
        del self.orders[order_id]

        if self.logger:
            self.logger.trade_cancelled(order_id=order_id, reason='manual')

        return True

    def get_order_status(self, order_id: str) -> OrderStatus:
        if order_id in self.orders:
            return self.orders[order_id].status
        for o in self.order_history:
            if o.order_id == order_id:
                return o.status
        return OrderStatus.CANCELLED

    def get_order(self, order_id: str) -> Optional[Order]:
        if order_id in self.orders:
            return self.orders[order_id]
        for o in self.order_history:
            if o.order_id == order_id:
                return o
        return None

    # ═══════════════════════════════
    # 限价单撮合引擎
    # ═══════════════════════════════

    def check_pending_orders(self, bar: dict, bar_idx: int) -> List[Order]:
        """
        每根 K 线调用一次：检查挂单簿中的限价单是否被触达

        触发条件：
          - 买入限价单: bar.low <= limit_price → 成交于 limit_price
          - 卖出限价单: bar.high >= limit_price → 成交于 limit_price

        返回: 本 bar 新成交的订单列表（供上层做交易闭合记录）
        """
        bar_low = bar['low']
        bar_high = bar['high']

        filled_ids = []
        filled_orders = []
        for order_id, order in list(self.orders.items()):
            filled = False
            fill_price = order.price

            if order.side == 'long' and order.action == OrderAction.ENTRY:
                # 做多入场 = 买入限价单: 价格跌到限价以下 = 成交
                if bar_low <= order.price:
                    filled = True
                    fill_price = order.price  # 限价单成交于限价

            elif order.side == 'short' and order.action == OrderAction.ENTRY:
                # 做空入场 = 卖出限价单: 价格涨到限价以上 = 成交
                if bar_high >= order.price:
                    filled = True
                    fill_price = order.price

            elif order.side == 'long' and order.action == OrderAction.EXIT:
                # 做多出场 = 卖出限价单: 价格涨到限价以上 = 成交
                if bar_high >= order.price:
                    filled = True
                    fill_price = order.price

            elif order.side == 'short' and order.action == OrderAction.EXIT:
                # 做空出场 = 买入限价单: 价格跌到限价以下 = 成交
                if bar_low <= order.price:
                    filled = True
                    fill_price = order.price

            # ── 超时取消（K 线数超限）──
            if not filled:
                bars_since_created = bar_idx - getattr(order, 'created_at_bar', bar_idx)
                if bars_since_created > self.order_expiry_bars:
                    self.cancel_order(order_id)
                    continue

            if filled:
                order.status = OrderStatus.FILLED
                order.fill_price = fill_price
                order.fill_quantity = order.quantity
                order.filled_at = time.time()
                self._apply_fill(order, fill_price)
                self.order_history.append(order)
                filled_ids.append(order_id)
                filled_orders.append(order)

                if self.logger:
                    self.logger.trade_filled(
                        order_id=order_id, fill_price=fill_price,
                        fill_quantity=order.quantity,
                    )

        # 清理已成交的订单
        for oid in filled_ids:
            self.orders.pop(oid, None)

        return filled_orders

    # ═══════════════════════════════
    # 内部方法
    # ═══════════════════════════════

    def _compute_fill_price(self, side, signal_price, bar):
        """计算模拟成交价"""
        if self.cost_model:
            fill_price, _ = self.cost_model.effective_fill_price(
                signal_price, side, bar.get('atr')
            )
            return fill_price
        return signal_price

    def _apply_fill(self, order: Order, fill_price: float):
        """应用成交到账户余额和持仓"""
        if order.action == OrderAction.ENTRY:
            # 入场: 扣除余额，增加持仓
            cost = fill_price * order.quantity

            if order.side == 'long':
                self.locked_balance -= cost  # 解除锁定
            else:
                self.locked_balance -= cost

            # 更新持仓
            pos_key = order.symbol
            if pos_key in self.positions:
                pos = self.positions[pos_key]
                total_qty = pos.quantity + order.quantity
                pos.entry_price = ((pos.entry_price * pos.quantity) +
                                   (fill_price * order.quantity)) / total_qty
                pos.quantity = total_qty
            else:
                self.positions[pos_key] = PositionInfo(
                    symbol=order.symbol,
                    side=order.side,
                    quantity=order.quantity,
                    entry_price=fill_price,
                )

        elif order.action == OrderAction.EXIT:
            # 出场: 释放持仓，增加余额
            pos_key = order.symbol
            if pos_key in self.positions:
                pos = self.positions[pos_key]
                # 保存入场价到订单（供上层做交易闭合记录）
                order._entry_price = pos.entry_price
                if order.quantity >= pos.quantity:
                    # 完全平仓
                    if pos.side == 'long':
                        self.available_balance += fill_price * pos.quantity
                    else:
                        pnl = (pos.entry_price - fill_price) * pos.quantity
                        self.available_balance += pnl
                    del self.positions[pos_key]
                else:
                    # 部分平仓
                    pos.quantity -= order.quantity
                    self.available_balance += fill_price * order.quantity

    def update_market_prices(self, prices: Dict[str, float]):
        """更新持仓的当前市价（用于计算未实现盈亏）"""
        for symbol, pos in self.positions.items():
            if symbol in prices:
                pos.current_price = prices[symbol]
                if pos.side == 'long':
                    pos.unrealized_pnl = (pos.current_price - pos.entry_price) * pos.quantity
                else:
                    pos.unrealized_pnl = (pos.entry_price - pos.current_price) * pos.quantity
                pos.unrealized_pnl_pct = (pos.unrealized_pnl /
                                          (pos.entry_price * pos.quantity)) * 100

    # ═══════════════════════════════
    # 查询接口
    # ═══════════════════════════════

    def get_balance(self) -> BalanceInfo:
        unrealized = sum(p.unrealized_pnl for p in self.positions.values())
        return BalanceInfo(
            total_equity=self.total_equity + unrealized,
            available_balance=self.available_balance,
            locked_balance=self.locked_balance,
            unrealized_pnl=unrealized,
        )

    def get_positions(self) -> List[PositionInfo]:
        return list(self.positions.values())

    def is_connected(self) -> bool:
        return self._connected

    def get_stats(self) -> dict:
        """获取交易所统计"""
        return {
            'total_equity': self.total_equity,
            'available_balance': self.available_balance,
            'num_positions': len(self.positions),
            'num_pending_orders': len(self.orders),
            'total_orders': len(self.order_history),
        }


# ═══════════════════════════════
# BinanceExchange 骨架
# ═══════════════════════════════

class BinanceExchange(Exchange):
    """
    币安交易所 — 骨架已就绪，API key 到了补 submit_order 实现

    当前状态: 所有方法抛出 NotImplementedError（paper 模式用 PaperExchange）
    就绪后: 补 submit_order / cancel_order / get_order_status 三个核心方法即可
    """

    def __init__(self, api_key="", secret_key="", testnet=True, logger=None):
        self.api_key = api_key
        self.secret_key = secret_key
        self.testnet = testnet
        self.logger = logger
        self._client = None  # ccxt.binance() 实例（API key 就绪后初始化）
        self._connected = False

        if api_key and secret_key:
            self._init_client()

    def _init_client(self):
        """初始化 ccxt 客户端（需要 API key）"""
        try:
            import ccxt
            self._client = ccxt.binance({
                'apiKey': self.api_key,
                'secret': self.secret_key,
                'enableRateLimit': True,
                'options': {'defaultType': 'spot'},
                'urls': {
                    'api': 'https://testnet.binance.vision' if self.testnet
                           else 'https://api.binance.com',
                },
            })
            self._connected = True
            print(f"🔗 币安 {'testnet' if self.testnet else '主网'} 已连接")
        except ImportError:
            print("⚠️ ccxt 未安装: pip install ccxt")
        except Exception as e:
            print(f"⚠️ 币安连接失败: {e}")

    def submit_order(self, symbol: str, side: str, action: str, order_type: str,
                     price: float, quantity: float, strategy_name: str = "",
                     **kwargs) -> Optional[Order]:
        """提交订单到币安 — TODO: API key 就绪后实现"""
        if not self._client:
            raise NotImplementedError(
                "BinanceExchange 需要 API key。"
                "当前请使用 PaperExchange (--mode paper)。"
            )
        # TODO: 实现真实订单提交
        # ccxt_side = 'buy' if side == 'long' else 'sell'
        # if action == 'exit':
        #     ccxt_side = 'sell' if side == 'long' else 'buy'
        # result = self._client.create_order(symbol, order_type, ccxt_side, quantity, price)
        raise NotImplementedError("待 API key 就绪后实现")

    def cancel_order(self, order_id: str) -> bool:
        if not self._client:
            return False
        # TODO: self._client.cancel_order(order_id, symbol)
        raise NotImplementedError("待 API key 就绪后实现")

    def get_order_status(self, order_id: str) -> OrderStatus:
        if not self._client:
            return OrderStatus.CANCELLED
        # TODO: self._client.fetch_order(order_id, symbol)
        raise NotImplementedError("待 API key 就绪后实现")

    def get_balance(self) -> BalanceInfo:
        if not self._client:
            return BalanceInfo()
        # TODO: self._client.fetch_balance()
        raise NotImplementedError("待 API key 就绪后实现")

    def get_positions(self) -> List[PositionInfo]:
        if not self._client:
            return []
        # TODO: self._client.fetch_positions()
        raise NotImplementedError("待 API key 就绪后实现")

    def get_order(self, order_id: str) -> Optional[Order]:
        if not self._client:
            return None
        raise NotImplementedError("待 API key 就绪后实现")

    def is_connected(self) -> bool:
        return self._connected


# ═══════════════════════════════
# 工厂函数
# ═══════════════════════════════

def create_exchange(mode='paper', initial_capital=10000, config=None,
                    cost_model=None, logger=None) -> Exchange:
    """
    创建交易所实例

    mode='paper' → PaperExchange（本地模拟撮合）
    mode='live'  → BinanceExchange（真实 API，需要 .env 中的 API key）
    """
    if mode == 'live':
        if config and config.exchange.api_key:
            return BinanceExchange(
                api_key=config.exchange.api_key,
                secret_key=config.exchange.secret_key,
                testnet=config.exchange.testnet,
                logger=logger,
            )
        else:
            print("⚠️ LIVE 模式缺少 API key，回退到 PaperExchange")
            return PaperExchange(
                initial_capital=initial_capital,
                cost_model=cost_model,
                logger=logger,
            )

    # 默认 paper 模式
    return PaperExchange(
        initial_capital=initial_capital,
        cost_model=cost_model,
        logger=logger,
    )
