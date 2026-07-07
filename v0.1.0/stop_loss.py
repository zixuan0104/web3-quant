"""
四层止损系统 — Day 9 核心模块

止损不是"亏了就跑"那么简单。每一层回答不同的问题：

  Layer 1: 技术止损 — "价格跌破了关键位置吗？"
  Layer 2: 波动率追踪止损 — "市场波动变了吗？"
  Layer 3: 时间止损 — "我等了够久但它不动，还等吗？"
  Layer 4: 策略逻辑止损 — "当初开仓的理由还成立吗？"

设计原则：
  - 每层独立计算自己的止损价格
  - 最紧迫的那个（最近当前价格的）触发
  - 止损只能上移（多头）/下移（空头），不能放松
  - 与 RiskManager 集成：StopLossManager 计算止损价，RiskManager 做执行决策

用法：
  from stop_loss import StopLossManager, StopLossConfig

  sl = StopLossManager(config=StopLossConfig())
  result = sl.evaluate(
      position={'entry_price': 65000, 'side': 'long', 'bars_held': 12},
      bar={'close': 64000, 'high': 65500, 'low': 63800, 'atr': 1200, 'adx': 18},
      strategy_name='趋势跟踪',
  )
  if result.triggered:
      print(f"止损触发: {result.reason}, 止损价: {result.stop_price}")
"""

from dataclasses import dataclass, field
from typing import Optional, Dict, List, Tuple
from enum import Enum


# ═══════════════════════════════
# 数据类型
# ═══════════════════════════════

class StopLossLayer(Enum):
    """止损层级"""
    TECHNICAL = "L1_technical"           # 技术止损
    VOLATILITY_TRAILING = "L2_trailing"  # 波动率追踪止损
    TIME = "L3_time"                     # 时间止损
    STRATEGY_LOGIC = "L4_strategy"       # 策略逻辑止损


@dataclass
class StopLossResult:
    """止损评估结果"""
    triggered: bool = False
    layer: Optional[StopLossLayer] = None  # 触发的层级
    stop_price: float = 0.0                # 止损触发价格
    reason: str = ""                       # 触发原因
    urgency: str = "normal"                # 'normal' | 'urgent' | 'critical'
    details: Dict = field(default_factory=dict)  # 各层详细信息


@dataclass
class StopLossConfig:
    """止损系统配置"""

    # ── L1: 技术止损（硬止损）──
    # 止损距离 = entry ± (ATR × multiplier)
    # 不能超过 max_loss_pct（绝对上限）
    technical_atr_multiplier: float = 2.0
    technical_max_loss_pct: float = 0.02   # 单笔最多亏 2%

    # ── L2: 波动率追踪止损 ──
    # 盈利超过 activation_pct 后激活追踪止损
    # 止损价 = 最高价（多头）/ 最低价（空头） - ATR × multiplier
    trailing_atr_multiplier: float = 3.0
    trailing_activation_pct: float = 0.01   # 盈利 1% 后开始追踪

    # ── L3: 时间止损 ──
    # 持仓超过 max_bars 根 K 线且盈利不足 min_profit_pct → 退出
    max_bars: int = 24
    time_stop_min_profit_pct: float = 0.005  # 0.5%
    # 特别：如果持仓超过 max_bars * 1.5 根 K 线，无论盈亏都退出
    time_stop_force_exit_multiplier: float = 1.5

    # ── L4: 策略逻辑止损 ──
    # 趋势策略：ADX 跌到趋势区间以下
    trend_adx_min: int = 15
    # 动量策略：价格变化率低于阈值
    momentum_roc_min: float = 0.01  # 1%
    momentum_roc_lookback: int = 5  # 5 根 K 线的 ROC
    # 突破策略：价格回落到通道中线以下
    breakout_channel_mid_pct: float = 0.50  # 回落到通道 50% 以下


class StopLossManager:
    """
    四层止损管理器

    每一层独立计算自己的止损价格。
    最终取最接近当前价格的那个作为有效止损。
    """

    def __init__(self, config: StopLossConfig = None):
        self.config = config or StopLossConfig()
        # 追踪止损状态（跨 bar 持久化）
        self._trailing_stop_price: Dict[str, float] = {}  # position_id → stop_price
        self._highest_since_entry: Dict[str, float] = {}  # position_id → highest price

    # ═══════════════════════════════
    # L1: 技术止损
    # ═══════════════════════════════

    def calculate_technical_stop(self, entry_price: float, side: str,
                                 atr: float) -> Tuple[float, str]:
        """
        技术止损：基于 ATR 的固定止损

        逻辑:
          多头: stop = entry - ATR × multiplier
          空头: stop = entry + ATR × multiplier
          但亏损不能超过本金的 max_loss_pct

        返回: (止损价格, 说明)
        """
        distance = atr * self.config.technical_atr_multiplier

        if side == 'long':
            stop_price = entry_price - distance
            # 绝对上限：不能亏超过 max_loss_pct
            max_loss_stop = entry_price * (1 - self.config.technical_max_loss_pct)
            stop_price = max(stop_price, max_loss_stop)  # 取更紧的那个
        else:
            stop_price = entry_price + distance
            max_loss_stop = entry_price * (1 + self.config.technical_max_loss_pct)
            stop_price = min(stop_price, max_loss_stop)

        note = (f'ATR={atr:.0f}, 距离={distance:.0f}, '
                f'{"取ATR止损" if stop_price != max_loss_stop else "取硬上限"}')
        return stop_price, note

    def check_technical_stop(self, entry_price: float, current_price: float,
                             side: str, atr: float) -> Dict:
        """
        检查是否触发技术止损

        返回: {triggered, stop_price, loss_pct_if_hit, note}
        """
        stop_price, note = self.calculate_technical_stop(entry_price, side, atr)

        if side == 'long':
            triggered = current_price <= stop_price
        else:
            triggered = current_price >= stop_price

        loss_pct = abs(current_price - entry_price) / entry_price

        return {
            'triggered': triggered,
            'stop_price': round(stop_price, 2),
            'current_loss_pct': round(loss_pct, 4),
            'max_loss_pct': self.config.technical_max_loss_pct,
            'atr_multiplier': self.config.technical_atr_multiplier,
            'note': note,
        }

    # ═══════════════════════════════
    # L2: 波动率追踪止损
    # ═══════════════════════════════

    def calculate_trailing_stop(self, entry_price: float, current_price: float,
                                highest_since_entry: float, lowest_since_entry: float,
                                side: str, atr: float,
                                position_id: str = None) -> Dict:
        """
        波动率追踪止损

        逻辑:
          激活条件: 盈利 > activation_pct（如 1%）
          激活后:   止损价 = 最高价 - ATR × 3（多头）
                    止损价 = 最低价 + ATR × 3（空头）
          止损只能朝有利方向移动（只紧不松）

        「让利润奔跑」的数学实现：
          你买入后涨了 10%，然后回落 3 个 ATR 的幅度
          → 你仍然锁定 7% 的利润，而不是亏回原点
        """
        trailing_distance = atr * self.config.trailing_atr_multiplier
        activation = entry_price * (1 + self.config.trailing_activation_pct)

        # ── 检查激活 ──
        if side == 'long':
            profit_pct = (current_price - entry_price) / entry_price
            activated = profit_pct >= self.config.trailing_activation_pct
            new_stop = highest_since_entry - trailing_distance
            # 止损只能上移
            old_stop = self._trailing_stop_price.get(position_id, 0)
            if new_stop > old_stop:
                stop_price = new_stop
            else:
                stop_price = old_stop if old_stop > 0 else entry_price - trailing_distance
        else:
            profit_pct = (entry_price - current_price) / entry_price
            activated = profit_pct >= self.config.trailing_activation_pct
            new_stop = lowest_since_entry + trailing_distance
            # 止损只能下移
            old_stop = self._trailing_stop_price.get(position_id, float('inf'))
            if new_stop < old_stop:
                stop_price = new_stop
            else:
                stop_price = old_stop if old_stop < float('inf') else entry_price + trailing_distance

        # ── 持久化 ──
        if position_id:
            self._trailing_stop_price[position_id] = stop_price

        # ── 当前是否触发 ──
        if side == 'long':
            triggered = current_price <= stop_price if activated else False
        else:
            triggered = current_price >= stop_price if activated else False

        return {
            'triggered': triggered,
            'activated': activated,
            'stop_price': round(stop_price, 2),
            'profit_pct': round(profit_pct, 4),
            'trailing_distance': round(trailing_distance, 2),
            'highest_since_entry': round(highest_since_entry, 2),
            'activation_threshold': self.config.trailing_activation_pct,
            'note': (f'追踪止损已激活, 止损={stop_price:.0f}'
                     if activated else f'未激活 (盈利 {profit_pct:.2%} < {self.config.trailing_activation_pct:.1%})'),
        }

    # ═══════════════════════════════
    # L3: 时间止损
    # ═══════════════════════════════

    def check_time_stop(self, bars_held: int, entry_price: float,
                        current_price: float, side: str) -> Dict:
        """
        时间止损

        「我赌它 4 小时涨，4 小时没涨就走了」

        两级触发:
          1. 软触发: bars_held >= max_bars 且盈利 < min_profit_pct
             → "等了够久，没赚够，不耗了"
          2. 硬触发: bars_held >= max_bars × 1.5
             → "不管盈亏都得走了，时间成本太高"
        """
        if side == 'long':
            profit_pct = (current_price - entry_price) / entry_price
        else:
            profit_pct = (entry_price - current_price) / entry_price

        max_bars = self.config.max_bars
        force_exit_bars = int(max_bars * self.config.time_stop_force_exit_multiplier)
        min_profit = self.config.time_stop_min_profit_pct

        # 硬触发
        if bars_held >= force_exit_bars:
            return {
                'triggered': True,
                'trigger_type': '硬触发',
                'bars_held': bars_held,
                'force_exit_bars': force_exit_bars,
                'profit_pct': round(profit_pct, 4),
                'note': f'持仓 {bars_held} 根 K 线 ≥ {force_exit_bars}（硬上限），强制退出',
            }

        # 软触发
        if bars_held >= max_bars and profit_pct < min_profit:
            return {
                'triggered': True,
                'trigger_type': '软触发',
                'bars_held': bars_held,
                'max_bars': max_bars,
                'profit_pct': round(profit_pct, 4),
                'min_profit_pct': min_profit,
                'note': f'持仓 {bars_held} 根 K 线，盈利仅 {profit_pct:.2%} < {min_profit:.1%}，时间止损退出',
            }

        return {
            'triggered': False,
            'bars_held': bars_held,
            'max_bars': max_bars,
            'profit_pct': round(profit_pct, 4),
            'note': f'正常 (已持 {bars_held}/{max_bars})',
        }

    # ═══════════════════════════════
    # L4: 策略逻辑止损
    # ═══════════════════════════════

    def check_strategy_stop(self, strategy_name: str, bar: dict) -> Dict:
        """
        策略逻辑止损

        每个策略的"存在理由"不同，失效条件也不同:

          趋势跟踪 → ADX < 15（没有趋势，你在跟踪什么？）
          动量策略 → ROC 变小（动量在消失）
          突破策略 → 价格回到通道下沿以下（不是真突破）
          均值回归 → 偏离继续扩大（没在回归）
          费率套利 → 资金费率降到阈值以下（无利可套）
        """
        triggered = False
        reason = ""

        # ── 趋势跟踪 ──
        if '趋势' in strategy_name or 'trend' in str(strategy_name).lower():
            adx = bar.get('adx', 100)
            if adx is not None and adx < self.config.trend_adx_min:
                triggered = True
                reason = f'ADX={adx} < {self.config.trend_adx_min}，趋势消失'

        # ── 动量策略 ──
        elif '动量' in strategy_name or 'momentum' in str(strategy_name).lower():
            roc = bar.get('roc', 999)
            if roc is not None and abs(roc) < self.config.momentum_roc_min:
                triggered = True
                reason = f'ROC={roc:.3f} < {self.config.momentum_roc_min}，动量消失'

        # ── 突破策略 ──
        elif '突破' in strategy_name or 'breakout' in str(strategy_name).lower():
            close = bar.get('close', 0)
            upper = bar.get('donchian_upper', 0)
            lower = bar.get('donchian_lower', 0)
            if upper > 0 and lower > 0:
                channel_height = upper - lower
                mid = lower + channel_height * self.config.breakout_channel_mid_pct
                if close < mid:
                    triggered = True
                    reason = f'价格 {close:.0f} < 通道中线 {mid:.0f}，假突破'

        # ── 均值回归 ──
        elif '均值' in strategy_name or 'mean_reversion' in str(strategy_name).lower():
            # 偏离继续扩大 → 可能在形成新趋势
            zscore = bar.get('zscore', 0)
            prev_zscore = bar.get('prev_zscore', 0)
            if abs(zscore) > abs(prev_zscore) and abs(zscore) > 2.5:
                triggered = True
                reason = f'Z-score 继续扩大到 {zscore:.1f}，可能形成新趋势'

        # ── 资金费率套利 ──
        elif '费率' in strategy_name or 'funding' in str(strategy_name).lower():
            funding_rate = bar.get('funding_rate', 0)
            if abs(funding_rate) < 0.0001:  # 0.01% → 年化才 0.3%，不值得
                triggered = True
                reason = f'资金费率 {funding_rate:.4%} 太低，不值得套利'

        return {
            'triggered': triggered,
            'strategy_name': strategy_name,
            'reason': reason if triggered else '策略逻辑仍然成立',
            'bar_snapshot': {k: bar.get(k) for k in ['adx', 'roc', 'close', 'funding_rate']
                             if k in bar},
        }

    # ═══════════════════════════════
    # 综合评估
    # ═══════════════════════════════

    def evaluate(self, position: dict, bar: dict, strategy_name: str,
                 position_id: str = None) -> StopLossResult:
        """
        四层止损综合评估

        参数:
          position: {
              'entry_price': float,   # 开仓价
              'side': 'long'|'short', # 方向
              'bars_held': int,       # 持仓 K 线数
          }
          bar: {
              'open', 'high', 'low', 'close',  # OHLC
              'atr': float,                      # ATR
              'adx': float,                      # ADX (optional)
              'roc': float,                      # Rate of Change (optional)
              ...
          }
          strategy_name: 策略名称（用于 L4 策略逻辑止损）
          position_id: 持仓标识（用于 L2 追踪止損状态持久化 — 跨 bar 跟踪）

        返回: StopLossResult — 是否触发、哪层触发、止损价、原因
        """
        entry_price = position['entry_price']
        side = position['side']
        bars_held = position.get('bars_held', 0)
        current_price = bar['close']
        atr = bar.get('atr', 0)
        pid = position_id or f"{strategy_name}_{entry_price}"

        # ── 更新追踪止損的极值 ──
        if pid not in self._highest_since_entry:
            self._highest_since_entry[pid] = current_price
            self._lowest_since_entry = getattr(self, '_lowest_since_entry', {})
            self._lowest_since_entry[pid] = current_price

        if side == 'long':
            self._highest_since_entry[pid] = max(
                self._highest_since_entry.get(pid, entry_price),
                bar.get('high', current_price)
            )
        else:
            if not hasattr(self, '_lowest_since_entry'):
                self._lowest_since_entry = {}
            self._lowest_since_entry[pid] = min(
                self._lowest_since_entry.get(pid, entry_price),
                bar.get('low', current_price)
            )

        highest = self._highest_since_entry.get(pid, current_price)
        lowest = self._lowest_since_entry.get(pid, current_price) if hasattr(self, '_lowest_since_entry') else current_price

        # ── 逐层检查（短路：L4 → L3 → L2 → L1）──
        # 短路顺序：策略逻辑最优先（根本性原因），时间其次，
        # 然后追踪止损（锁利润），最后技术止损（硬底线）

        details = {}

        # L4: 策略逻辑止损
        l4 = self.check_strategy_stop(strategy_name, bar)
        details['L4_strategy'] = l4
        if l4['triggered']:
            return StopLossResult(
                triggered=True,
                layer=StopLossLayer.STRATEGY_LOGIC,
                stop_price=current_price,  # 市价退出
                reason=l4['reason'],
                urgency='critical',
                details=details,
            )

        # L3: 时间止损
        l3 = self.check_time_stop(bars_held, entry_price, current_price, side)
        details['L3_time'] = l3
        if l3['triggered']:
            return StopLossResult(
                triggered=True,
                layer=StopLossLayer.TIME,
                stop_price=current_price,
                reason=l3['note'],
                urgency='urgent' if l3.get('trigger_type') == '硬触发' else 'normal',
                details=details,
            )

        # L2: 波动率追踪止损
        if atr > 0:
            l2 = self.calculate_trailing_stop(
                entry_price, current_price, highest, lowest,
                side, atr, position_id=pid,
            )
            details['L2_trailing'] = l2
            if l2['triggered']:
                return StopLossResult(
                    triggered=True,
                    layer=StopLossLayer.VOLATILITY_TRAILING,
                    stop_price=l2['stop_price'],
                    reason=f"追踪止损触发: 已盈利 {l2['profit_pct']:.2%}, "
                           f"止损价 {l2['stop_price']:.0f}",
                    urgency='normal',
                    details=details,
                )
        else:
            details['L2_trailing'] = {'triggered': False, 'note': 'ATR 不可用，跳过'}

        # L1: 技术止损
        if atr > 0:
            l1 = self.check_technical_stop(entry_price, current_price, side, atr)
            details['L1_technical'] = l1
            if l1['triggered']:
                return StopLossResult(
                    triggered=True,
                    layer=StopLossLayer.TECHNICAL,
                    stop_price=l1['stop_price'],
                    reason=f"技术止损触发: 亏损 {l1['current_loss_pct']:.2%}, "
                           f"止损价 {l1['stop_price']:.0f}",
                    urgency='normal',
                    details=details,
                )
        else:
            details['L1_technical'] = {'triggered': False, 'note': 'ATR 不可用，跳过'}

        # ── 全部通过 ──
        return StopLossResult(
            triggered=False,
            details=details,
        )

    # ═══════════════════════════════
    # 重置追踪状态
    # ═══════════════════════════════

    def reset_position(self, position_id: str):
        """平仓后清理该仓位的追踪止损状态"""
        self._trailing_stop_price.pop(position_id, None)
        self._highest_since_entry.pop(position_id, None)
        if hasattr(self, '_lowest_since_entry'):
            self._lowest_since_entry.pop(position_id, None)

    def reset_all(self):
        """重置全部状态"""
        self._trailing_stop_price.clear()
        self._highest_since_entry.clear()
        if hasattr(self, '_lowest_since_entry'):
            self._lowest_since_entry.clear()

    # ═══════════════════════════════
    # 可插拔接口：自定义层
    # ═══════════════════════════════

    def add_custom_layer(self, name: str, check_fn):
        """
        添加自定义止损层

        check_fn 签名: (position: dict, bar: dict) → Dict
        返回格式: {triggered: bool, stop_price: float, reason: str}
        """
        if not hasattr(self, '_custom_layers'):
            self._custom_layers = {}
        self._custom_layers[name] = check_fn

    def remove_custom_layer(self, name: str):
        """移除自定义止损层"""
        if hasattr(self, '_custom_layers'):
            self._custom_layers.pop(name, None)

    # ═══════════════════════════════
    # 止损价格可视化
    # ═══════════════════════════════

    def get_stop_prices(self, entry_price: float, atr: float, side: str = 'long',
                        highest: float = None, lowest: float = None) -> Dict:
        """
        获取所有止损层的价格（不判断触发，只是展示）

        用于图表标注：在 K 线图上画出各层止损线
        """
        prices = {}

        # L1 技术止损
        tech_stop, _ = self.calculate_technical_stop(entry_price, side, atr)
        prices['L1_technical'] = round(tech_stop, 2)

        # L2 追踪止损（如果激活了）
        if highest is not None and lowest is not None:
            trailing_dist = atr * self.config.trailing_atr_multiplier
            if side == 'long':
                prices['L2_trailing'] = round(highest - trailing_dist, 2)
            else:
                prices['L2_trailing'] = round(lowest + trailing_dist, 2)

        # 当前价格（参考线）
        prices['entry'] = round(entry_price, 2)

        return prices

    def print_stop_levels(self, entry_price: float, atr: float, side: str = 'long'):
        """打印各层止损价格"""
        prices = self.get_stop_prices(entry_price, atr, side)
        print(f"\n  [止损价格一览] 入场={prices['entry']:.0f}, ATR={atr:.0f}")
        print(f"  {'层':<12} {'价格':>10} {'距入场':>10}")
        print(f"  {'-'*12} {'-'*10} {'-'*10}")
        for layer, price in prices.items():
            if layer != 'entry':
                dist = abs(price - entry_price)
                dist_pct = dist / entry_price * 100
                print(f"  {layer:<12} {price:>10.0f} {dist_pct:>9.2f}%")


# ═══════════════════════════════
# 与 RiskManager 的集成适配器
# ═══════════════════════════════

class StopLossRiskAdapter:
    """
    连接 StopLossManager 和 RiskManager

    RiskManager 有最终否决权。
    StopLossManager 计算止损价，RiskManager 决定是否执行。

    流程:
      1. StopLossManager.evaluate() → 是否触发止损
      2. 如果触发 → RiskManager.pre_trade_check() → 是否允许平仓
      3. RiskManager 说不平 → 不平（风控最终否决权）
    """

    def __init__(self, stop_loss_manager: StopLossManager, risk_manager=None):
        self.sl = stop_loss_manager
        self.rm = risk_manager

    def evaluate_with_risk_check(self, position: dict, bar: dict,
                                 strategy_name: str, position_id: str = None) -> Dict:
        """
        止损评估 → 风控门禁 → 最终决策
        """
        # Step 1: 止损评估
        sl_result = self.sl.evaluate(position, bar, strategy_name, position_id)

        if not sl_result.triggered:
            return {'action': 'hold', 'sl_result': sl_result}

        # Step 2: 风控门禁（如果有 RiskManager）
        if self.rm:
            # 平仓方向与原仓位相反
            close_side = 'sell' if position['side'] == 'long' else 'buy'
            risk_check = self.rm.pre_trade_check(
                symbol=position.get('symbol', 'UNKNOWN'),
                side=close_side,
                size_pct=position.get('size_pct', 0.1),
                signal_price=bar['close'],
            )
            if not risk_check.passed:
                return {
                    'action': 'hold',
                    'sl_result': sl_result,
                    'risk_veto': True,
                    'risk_reason': risk_check.reason,
                }

        # 全部通过 → 执行止损
        return {
            'action': 'stop_loss',
            'stop_price': sl_result.stop_price,
            'layer': sl_result.layer.value if sl_result.layer else 'unknown',
            'reason': sl_result.reason,
            'sl_result': sl_result,
        }


# ═══════════════════════════════
# 快速测试（内联 demo）
# ═══════════════════════════════

if __name__ == '__main__':
    print("=" * 60)
    print("  四层止损系统 — Day 9")
    print("=" * 60)

    sl = StopLossManager()

    # ── 场景 1: 正常持仓（不触发止损）──
    print("\n  [场景 1] 正常持仓 — 全部通过")
    result = sl.evaluate(
        position={'entry_price': 65000, 'side': 'long', 'bars_held': 5},
        bar={'open': 64800, 'high': 65500, 'low': 64700, 'close': 65200,
             'atr': 1200, 'adx': 25},
        strategy_name='趋势跟踪',
        position_id='test_1',
    )
    print(f"  触发: {result.triggered}")
    for layer, detail in result.details.items():
        print(f"    {layer}: {detail.get('note', detail.get('reason', ''))}")

    # ── 场景 2: 技术止损触发 ──
    print("\n  [场景 2] 技术止损 — 价格跌破 ATR 止损线")
    result = sl.evaluate(
        position={'entry_price': 65000, 'side': 'long', 'bars_held': 3},
        bar={'open': 63200, 'high': 65000, 'low': 62200, 'close': 62400,
             'atr': 1200, 'adx': 22},
        strategy_name='趋势跟踪',
        position_id='test_2',
    )
    print(f"  触发: {result.triggered}, 层: {result.layer}")
    print(f"  止损价: {result.stop_price}, 原因: {result.reason}")

    # ── 场景 3: 时间止损触发 ──
    print("\n  [场景 3] 时间止损 — 持仓太久利润不足")
    result = sl.evaluate(
        position={'entry_price': 65000, 'side': 'long', 'bars_held': 30},
        bar={'open': 65200, 'high': 65300, 'low': 64900, 'close': 65100,
             'atr': 800, 'adx': 18},
        strategy_name='趋势跟踪',
        position_id='test_3',
    )
    print(f"  触发: {result.triggered}, 层: {result.layer}")
    print(f"  原因: {result.reason}")
    l3 = result.details.get('L3_time', {})
    print(f"  持仓: {l3.get('bars_held')} K线, 盈利: {l3.get('profit_pct', 0):.3%}")

    # ── 场景 4: 策略逻辑止损 ──
    print("\n  [场景 4] 策略逻辑止损 — 趋势消失 (ADX < 15)")
    result = sl.evaluate(
        position={'entry_price': 65000, 'side': 'long', 'bars_held': 8},
        bar={'open': 64800, 'high': 65100, 'low': 64600, 'close': 64900,
             'atr': 600, 'adx': 12},
        strategy_name='趋势跟踪',
        position_id='test_4',
    )
    print(f"  触发: {result.triggered}, 层: {result.layer}")
    print(f"  原因: {result.reason}")

    # ── 场景 5: 追踪止损 ──
    print("\n  [场景 5] 追踪止损 — 盈利后回撤")
    pid = 'test_5'
    # 模拟: 开仓后一路上涨，激活追踪止损
    bars = [
        {'open': 65000, 'high': 65500, 'low': 64800, 'close': 65400, 'atr': 1200, 'adx': 30},
        {'open': 65400, 'high': 66200, 'low': 65300, 'close': 66000, 'atr': 1200, 'adx': 32},
        {'open': 66000, 'high': 67000, 'low': 65800, 'close': 66800, 'atr': 1200, 'adx': 33},
        {'open': 66800, 'high': 67500, 'low': 66000, 'close': 66200, 'atr': 1200, 'adx': 28},  # 回撤!
    ]
    for i, bar in enumerate(bars):
        result = sl.evaluate(
            position={'entry_price': 65000, 'side': 'long', 'bars_held': i + 1},
            bar=bar, strategy_name='趋势跟踪', position_id=pid,
        )
        l2 = result.details.get('L2_trailing', {})
        print(f"  Bar {i+1}: close={bar['close']:.0f}, "
              f"触发={result.triggered}, "
              f"追踪止损={l2.get('stop_price', 'N/A')}, "
              f"盈利={l2.get('profit_pct', 0):.2%}, "
              f"{l2.get('note', '')}")
        if result.triggered:
            print(f"    >>> 止损触发! 层: {result.layer}, 原因: {result.reason}")

    # ── 止损价格一览 ──
    sl2 = StopLossManager()
    sl2.print_stop_levels(entry_price=65000, atr=1200, side='long')

    print("\n  [OK] Day 9 四层止损系统就绪")
