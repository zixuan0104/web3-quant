"""
精细化交易成本模型 — Day 6 核心模块

四层成本叠加：
  1. 手续费（Maker 0.075% / Taker 0.1%，BNB 抵扣可配）
  2. 滑点（基于波动率 + 成交量的动态滑点，非固定值）
  3. 价差成本（市价单隐含的 bid-ask spread）
  4. 市场冲击（大单对订单簿的冲击，量越大越贵）

设计原则：
  - 成本模型必须在回测/模拟盘中内嵌计算，不能事后补算
  - 小币种滑点远大于主流币，需要根据流动性分级
  - 与策略基类的旧成本模型兼容（渐进替换）

用法：
  cm = CostModel(order_type='taker', liquidity_tier='medium')
  entry_cost = cm.entry_cost(price, quantity_usdt=500)
  exit_cost = cm.exit_cost(price, quantity_usdt=500)
  total_cost_bps = entry_cost + exit_cost  # 基点为单位的双向总成本
"""

import numpy as np
from dataclasses import dataclass, field
from enum import Enum


class OrderType(Enum):
    MAKER = "maker"      # 限价单，吃挂单返佣（通常更便宜）
    TAKER = "taker"      # 市价单，吃流动性（含价差成本）


class LiquidityTier(Enum):
    """流动性分级 — 决定滑点乘数"""
    HIGH = "high"          # BTC/ETH/SOL 等主流（日均成交量 > $1B）
    MEDIUM = "medium"      # 中型币（日均 $100M-$1B）
    LOW = "low"            # 小币种（日均 $10M-$100M）
    MICRO = "micro"        # Meme/土狗（日均 < $10M，滑点极大）


@dataclass
class CostBreakdown:
    """单次交易成本明细（单位：基点 bps，1bp = 0.01%）"""
    fee_bps: float = 0.0           # 手续费
    slippage_bps: float = 0.0      # 滑点
    spread_bps: float = 0.0        # 价差（仅 taker 有）
    impact_bps: float = 0.0        # 市场冲击（大单有）
    total_bps: float = 0.0         # 合计

    def total_pct(self) -> float:
        """转为百分比"""
        return self.total_bps / 10000


class CostModel:
    """
    四层交易成本模型

    参考币安现货费率（2024）：
      - 普通用户 Maker 0.1%, Taker 0.1%
      - BNB 抵扣 25% 折扣 → Maker 0.075%, Taker 0.075%
    """

    # ── 基础费率（可配置）──
    MAKER_FEE = 0.00075    # 0.075% — 限价单（吃挂单返佣）
    TAKER_FEE = 0.00075    # 0.075% — 市价单（吃流动性，BNB 抵扣后）

    # ── 滑点乘数（不同流动性等级）──
    # Taker: 吃流动性，滑点大（跨价差 + 市场冲击）
    # Maker: 挂单等成交，滑点趋近于零（你的限价 = 你能接受的最差价格）
    SLIPPAGE_MULTIPLIER_TAKER = {
        LiquidityTier.HIGH:   1.0,    # BTC/ETH: 基础滑点 ×1
        LiquidityTier.MEDIUM: 2.5,    # 中型币: 基础滑点 ×2.5
        LiquidityTier.LOW:    6.0,    # 小币种: 基础滑点 ×6
        LiquidityTier.MICRO:  20.0,   # Meme: 基础滑点 ×20（极度危险）
    }
    SLIPPAGE_MULTIPLIER_MAKER = {
        LiquidityTier.HIGH:   0.05,   # BTC/ETH 限价单几乎零滑点
        LiquidityTier.MEDIUM: 0.15,   # 中型币限价: 极低滑点
        LiquidityTier.LOW:    0.5,    # 小币种限价: 低滑点
        LiquidityTier.MICRO:  2.0,    # Meme 限价: 仍有滑点，但远好于市价
    }

    # ── 典型价差（不同流动性等级，单位 bps）──
    TYPICAL_SPREAD = {
        LiquidityTier.HIGH:   1.0,    # BTC ~0.01%
        LiquidityTier.MEDIUM: 3.0,    # 中型币 ~0.03%
        LiquidityTier.LOW:    10.0,   # 小币种 ~0.1%
        LiquidityTier.MICRO:  50.0,   # Meme ~0.5%（甚至更高）
    }

    def __init__(self, order_type=OrderType.TAKER, liquidity_tier=LiquidityTier.HIGH,
                 maker_fee=None, taker_fee=None):
        self.order_type = order_type
        self.liquidity_tier = liquidity_tier
        self._maker_fee = maker_fee if maker_fee is not None else self.MAKER_FEE
        self._taker_fee = taker_fee if taker_fee is not None else self.TAKER_FEE

    # ═══════════════════════════════
    # 单次成本计算
    # ═══════════════════════════════

    def entry_cost(self, price, atr=None, volume_24h=None, quantity_usdt=500) -> CostBreakdown:
        """开仓成本（单向）"""
        return self._compute(price, atr, volume_24h, quantity_usdt)

    def exit_cost(self, price, atr=None, volume_24h=None, quantity_usdt=500) -> CostBreakdown:
        """平仓成本（单向，与入场对称）"""
        return self._compute(price, atr, volume_24h, quantity_usdt)

    def round_trip_cost(self, price, atr=None, volume_24h=None, quantity_usdt=500) -> CostBreakdown:
        """双向总成本（开仓 + 平仓）"""
        one_way = self._compute(price, atr, volume_24h, quantity_usdt)
        return CostBreakdown(
            fee_bps=one_way.fee_bps * 2,
            slippage_bps=one_way.slippage_bps * 2,
            spread_bps=one_way.spread_bps * 2,
            impact_bps=one_way.impact_bps * 2,
            total_bps=one_way.total_bps * 2,
        )

    def _compute(self, price, atr, volume_24h, quantity_usdt) -> CostBreakdown:
        """内部四层成本计算"""
        breakdown = CostBreakdown()

        # ── 层 1: 手续费 ──
        fee_rate = self._maker_fee if self.order_type == OrderType.MAKER else self._taker_fee
        breakdown.fee_bps = fee_rate * 10000  # 百分比 → 基点

        # ── 层 2: 滑点（基于 ATR 的动态滑点）──
        slippage_bps = self._compute_slippage(price, atr)
        breakdown.slippage_bps = slippage_bps

        # ── 层 3: 价差成本（仅 taker 有）──
        if self.order_type == OrderType.TAKER:
            breakdown.spread_bps = self.TYPICAL_SPREAD[self.liquidity_tier]
        # Maker 没有价差成本（挂单等成交，不跨价差）

        # ── 层 4: 市场冲击（大单才有）──
        breakdown.impact_bps = self._compute_impact(price, quantity_usdt, volume_24h)

        breakdown.total_bps = (breakdown.fee_bps + breakdown.slippage_bps +
                               breakdown.spread_bps + breakdown.impact_bps)
        return breakdown

    def _compute_slippage(self, price, atr) -> float:
        """
        基于 ATR 的动态滑点计算

        逻辑：
          - 有 ATR 数据 → 滑点 = ATR / price × 流动性乘数
          - ATR 越大波动越大，滑点越大（合理：波动大时做市商收窄深度）
          - 流动性越差，乘数越大
          - 无 ATR → 回退到固定滑点（price 的固定比例）

        对于 BTC:  ATR(1h) ~ $500 → 500/65000 = 0.77% × 1.0 = 0.77% ≈ 77 bps
        对于 SOL:  ATR(1h) ~ $3   → 3/146 = 2.05% × 2.5 = 5.1% ≈ 510 bps
        对于 Meme: ATR(1h) ~ 很大  → ×20 → 滑点吃掉利润
        """
        if atr is not None and atr > 0 and price > 0:
            base_slippage_bps = (atr / price) * 10000  # 转为基点
        else:
            # 回退：基于价格水平的固定比例
            base_slippage_bps = 5.0  # 0.05% 默认

        if self.order_type == OrderType.MAKER:
            multiplier = self.SLIPPAGE_MULTIPLIER_MAKER[self.liquidity_tier]
        else:
            multiplier = self.SLIPPAGE_MULTIPLIER_TAKER[self.liquidity_tier]
        return base_slippage_bps * multiplier

    def _compute_impact(self, price, quantity_usdt, volume_24h) -> float:
        """
        市场冲击模型

        逻辑：单笔交易金额 / 日成交量 的比例越大，冲击越大
        公式：impact_bps = (qty / vol) × 10000 × 乘数

        例：$500 交易 / $1B 日成交量 → 几乎 0
            $5000 交易 / $10M 日成交量 → 0.05% × 10000 = 500 bps（严重）
        """
        if volume_24h is None or volume_24h <= 0:
            return 0.0

        volume_ratio = quantity_usdt / volume_24h
        # 冲击乘数：流动性越差，同样比例冲击越大
        impact_multiplier = {
            LiquidityTier.HIGH:   1.0,
            LiquidityTier.MEDIUM: 3.0,
            LiquidityTier.LOW:    10.0,
            LiquidityTier.MICRO:  50.0,
        }[self.liquidity_tier]

        impact_bps = volume_ratio * 10000 * impact_multiplier
        return impact_bps

    # ═══════════════════════════════
    # 便捷方法
    # ═══════════════════════════════

    def effective_fill_price(self, signal_price, side, atr=None, volume_24h=None,
                             quantity_usdt=500):
        """
        计算考虑所有成本后的有效成交价

        side: 'long' 或 'short'
        返回: (实际成交价, CostBreakdown)

        long  entry → 买价上浮（成本在成交价上方）
        long  exit  → 卖价下浮（成本在成交价下方）
        short entry → 卖价下浮
        short exit → 买价上浮
        """
        cost = self._compute(signal_price, atr, volume_24h, quantity_usdt)
        cost_pct = cost.total_pct()

        if side == 'long':
            effective = signal_price * (1 + cost_pct)  # 买入更贵
        else:
            effective = signal_price * (1 - cost_pct)  # 卖出更便宜

        return effective, cost

    def slippage_only(self, price, atr=None) -> float:
        """仅滑点成本（百分比，用于旧接口兼容）"""
        return self._compute_slippage(price, atr) / 10000

    def total_one_way_pct(self, price, atr=None, volume_24h=None, quantity_usdt=500) -> float:
        """单向总成本（百分比）"""
        return self._compute(price, atr, volume_24h, quantity_usdt).total_pct()

    def total_round_trip_pct(self, price, atr=None, volume_24h=None, quantity_usdt=500) -> float:
        """双向总成本（百分比）"""
        return self.round_trip_cost(price, atr, volume_24h, quantity_usdt).total_pct()

    # ═══════════════════════════════
    # 静态工具
    # ═══════════════════════════════

    @staticmethod
    def classify_liquidity(symbol, avg_daily_volume_usdt=None) -> LiquidityTier:
        """
        根据代币/成交量自动分类流动性等级

        已知主流币直接匹配：
        """
        high_tier = {'BTC', 'ETH', 'BNB', 'SOL', 'XRP', 'DOGE', 'ADA', 'AVAX'}
        medium_tier = {'DOT', 'LINK', 'UNI', 'ATOM', 'MATIC', 'LTC', 'ETC', 'APT', 'ARB',
                       'OP', 'NEAR', 'FIL', 'INJ', 'TIA', 'SEI', 'SUI'}

        s = symbol.upper().replace('USDT', '').replace('BUSD', '').replace('USDC', '')

        if s in high_tier:
            return LiquidityTier.HIGH
        if s in medium_tier:
            return LiquidityTier.MEDIUM

        if avg_daily_volume_usdt:
            if avg_daily_volume_usdt > 1_000_000_000:
                return LiquidityTier.HIGH
            if avg_daily_volume_usdt > 100_000_000:
                return LiquidityTier.MEDIUM
            if avg_daily_volume_usdt > 10_000_000:
                return LiquidityTier.LOW
        return LiquidityTier.MICRO

    @staticmethod
    def compare_order_types(price, atr=None, volume_24h=None, quantity_usdt=500):
        """
        对比限价单 vs 市价单的成本差异

        返回: dict {maker: CostBreakdown, taker: CostBreakdown, savings_bps, savings_pct}
        """
        maker_cm = CostModel(OrderType.MAKER)
        taker_cm = CostModel(OrderType.TAKER)

        # 确保用相同的流动性等级
        tier = CostModel.classify_liquidity("", volume_24h)
        maker_cm.liquidity_tier = tier
        taker_cm.liquidity_tier = tier

        maker_cost = maker_cm._compute(price, atr, volume_24h, quantity_usdt)
        taker_cost = taker_cm._compute(price, atr, volume_24h, quantity_usdt)

        savings_bps = taker_cost.total_bps - maker_cost.total_bps
        return {
            'maker': maker_cost,
            'taker': taker_cost,
            'savings_bps': savings_bps,
            'savings_pct': savings_bps / 10000,
            'verdict': 'maker 更优' if savings_bps > 0 else 'taker 更优' if savings_bps < 0 else '无差异',
        }


# ═══════════════════════════════
# 兼容旧接口的包装器
# ═══════════════════════════════

def legacy_cost(price, fee_rate=0.001, slippage=0.0005):
    """
    旧版固定成本计算（兼容现有回测引擎）

    返回: (gross_return_pct, net_return_pct 需要的扣除量)
    """
    total_cost_pct = fee_rate * 2 + slippage * 2  # 双向
    return total_cost_pct


class BacktestCostAdapter:
    """
    将 CostModel 适配到回测引擎的旧接口

    用法:
      adapter = BacktestCostAdapter(CostModel(OrderType.TAKER, LiquidityTier.HIGH))
      # 在回测策略中:
      fee_cost = adapter.round_trip_fee(price, atr)
      slippage_entry = adapter.entry_slippage(price, atr)
    """

    def __init__(self, cost_model=None):
        self.cm = cost_model or CostModel(OrderType.TAKER, LiquidityTier.HIGH)

    def round_trip_fee(self, price, atr=None, volume_24h=None):
        """双向手续费（百分比），兼容旧 FEE_RATE * 2"""
        cost = self.cm.round_trip_cost(price, atr, volume_24h)
        return cost.fee_bps / 10000

    def entry_slippage(self, price, atr=None):
        """入场滑点（百分比），兼容旧 SLIPPAGE"""
        return self.cm.slippage_only(price, atr)

    def exit_slippage(self, price, atr=None):
        """出场滑点（百分比）"""
        return self.cm.slippage_only(price, atr)

    def total_round_trip_pct(self, price, atr=None, volume_24h=None):
        """双向总成本"""
        return self.cm.total_round_trip_pct(price, atr, volume_24h)
