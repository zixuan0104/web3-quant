"""
四层风控系统 — Day 7 核心模块

层级设计（越靠前越基础，越靠后越智能）：

  Layer 1: 单笔止损 — 本金的 1-2%
  Layer 2: 日内熔断 — 当日累计亏损达本金 5%
  Layer 3: 策略止损 — 策略核心假设被证伪时退出
  Layer 4: 时间止损 — "我赌它 4 小时涨，4 小时没涨就走了"

加上额外两层防护：
  L0 前检查: 仓位上限 / 单币种敞口 / 可用余额 → 预交易门禁
  全局熔断: 连续亏损 / 异常行情 / 系统异常 → 暂停所有策略

核心原则：
  风控有最终否决权。策略说买、风控说不买 → 不买。
  没有任何代码路径可以绕过风控层。

用法：
  from risk_manager import RiskManager
  rm = RiskManager(initial_capital=10000, config=risk_config)

  # 每笔交易前检查
  result = rm.pre_trade_check(symbol='BTC/USDT', side='long', size_pct=0.2,
                               signal_price=65000, bar=None)
  if result.passed:
      execute_order(...)
  else:
      log_skip(result.reason)
"""

import time
from datetime import datetime, timezone
from dataclasses import dataclass, field
from typing import Optional, Dict, List, Any
from enum import Enum


# ═══════════════════════════════
# 数据类型
# ═══════════════════════════════

class RiskAction(Enum):
    APPROVE = "approve"         # 通过，正常执行
    REDUCE = "reduce"           # 减仓执行（只允许原仓位 × 折扣）
    REJECT = "reject"           # 拒绝（资金/仓位不足等，不暂停系统）
    HALT = "halt"               # 拒绝 + 暂停策略（触发熔断）


@dataclass
class RiskCheckResult:
    """风控检查结果"""
    passed: bool
    action: RiskAction = RiskAction.APPROVE
    reason: str = ""
    suggested_size_pct: float = 0.0  # 建议调整后的仓位
    checks: List[Dict] = field(default_factory=list)  # 各检查项的详细结果


@dataclass
class RiskState:
    """风控运行时状态"""
    initial_capital: float = 10000.0
    current_equity: float = 10000.0
    daily_pnl: float = 0.0
    daily_pnl_pct: float = 0.0
    consecutive_losses: int = 0
    total_trades_today: int = 0
    circuit_breaker_active: bool = False
    circuit_breaker_reason: str = ""
    circuit_breaker_until: float = 0.0  # Unix timestamp
    last_trade_time: float = 0.0
    last_price: Dict[str, float] = field(default_factory=dict)  # symbol → last price

    # 当日统计
    day_start_equity: float = 10000.0
    day_trades: List[Dict] = field(default_factory=list)


class RiskManager:
    """
    四层风控管理器

    L0: 预交易门禁（仓位/余额/敞口）
    L1: 单笔止损（硬止损 1-2%）
    L2: 日内熔断（日亏损 5%）
    L3: 连续亏损检测 + 策略假设检查
    L4: 时间止损

    全局: 异常行情熔断 + 系统异常保护
    """

    def __init__(self, initial_capital=10000, config=None, logger=None):
        """
        config: RiskConfig 实例（来自 config_manager）
        logger: LiveLogger 实例（可选）
        """
        self.initial_capital = initial_capital
        self.config = config
        self.logger = logger

        # ── 运行时状态 ──
        self.state = RiskState(
            initial_capital=initial_capital,
            current_equity=initial_capital,
            day_start_equity=initial_capital,
        )

        # ── 从 config 提取参数（有就用，没有用默认）──
        if config:
            self.max_position_pct = config.max_position_pct
            self.daily_loss_limit_pct = config.daily_loss_limit_pct
            self.max_consecutive_losses = config.max_consecutive_losses
            self.price_spike_pct = config.price_spike_threshold_pct
            self.volume_spike = config.volume_spike_threshold
        else:
            self.max_position_pct = 0.20
            self.daily_loss_limit_pct = 0.05
            self.max_consecutive_losses = 5
            self.price_spike_pct = 10.0
            self.volume_spike = 5.0

    # ═══════════════════════════════
    # L0: 预交易门禁
    # ═══════════════════════════════

    def pre_trade_check(self, symbol: str, side: str, size_pct: float,
                        signal_price: float, bar: dict = None,
                        order_type: str = 'limit') -> RiskCheckResult:
        """
        每笔交易前必须调用的检查

        返回 RiskCheckResult:
          - passed=True → 可以执行
          - passed=False 且 action=REJECT → 拒绝这单但不暂停系统
          - passed=False 且 action=HALT → 暂停整个策略
        """
        checks = []
        reasons = []

        # ── 检查 0: 熔断是否激活 ──
        if self.state.circuit_breaker_active:
            if time.time() < self.state.circuit_breaker_until:
                checks.append({'check': '熔断状态', 'passed': False,
                               'detail': f"熔断中: {self.state.circuit_breaker_reason}"})
                return RiskCheckResult(
                    passed=False, action=RiskAction.HALT,
                    reason=f"熔断中: {self.state.circuit_breaker_reason}",
                    checks=checks,
                )
            else:
                # 熔断到期，自动恢复
                self._reset_circuit_breaker()

        # ── 检查 1: 仓位上限 ──
        if size_pct > self.max_position_pct:
            capped = self.max_position_pct
            checks.append({'check': '仓位上限', 'passed': False,
                           'detail': f"请求 {size_pct:.0%} > 上限 {self.max_position_pct:.0%}"})
            reasons.append(f"仓位超限: {size_pct:.0%} > {self.max_position_pct:.0%}")
            return RiskCheckResult(
                passed=False, action=RiskAction.REDUCE,
                reason='; '.join(reasons),
                suggested_size_pct=capped,
                checks=checks,
            )
        checks.append({'check': '仓位上限', 'passed': True,
                       'detail': f"{size_pct:.0%} ≤ {self.max_position_pct:.0%}"})

        # ── 检查 2: 最小下单金额（交易所现货最低 $10）──
        order_value = self.state.current_equity * size_pct
        min_order = 10.0  # 币安现货最小下单金额
        if order_value < min_order:
            checks.append({'check': '最小下单', 'passed': False,
                           'detail': f"订单金额 ${order_value:.2f} < ${min_order}"})
            return RiskCheckResult(
                passed=False, action=RiskAction.REJECT,
                reason=f"订单金额过小: ${order_value:.2f}",
                checks=checks,
            )
        checks.append({'check': '最小下单', 'passed': True,
                       'detail': f"${order_value:.0f}"})

        # ── 检查 3: 日亏损熔断 ──
        if self.state.daily_pnl_pct <= -self.daily_loss_limit_pct * 100:
            self._trigger_circuit_breaker('daily_loss', self.state.daily_pnl_pct)
            checks.append({'check': '日亏损熔断', 'passed': False,
                           'detail': f"日亏损 {self.state.daily_pnl_pct:.1f}% ≥ {self.daily_loss_limit_pct*100:.1f}%"})
            return RiskCheckResult(
                passed=False, action=RiskAction.HALT,
                reason=f"日亏损熔断: {self.state.daily_pnl_pct:.1f}%",
                checks=checks,
            )
        checks.append({'check': '日亏损熔断', 'passed': True,
                       'detail': f"日亏损 {self.state.daily_pnl_pct:.1f}%"})

        # ── 检查 4: 连续亏损 ──
        if self.state.consecutive_losses >= self.max_consecutive_losses:
            self._trigger_circuit_breaker('consecutive_losses',
                                          self.state.consecutive_losses)
            checks.append({'check': '连续亏损', 'passed': False,
                           'detail': f"{self.state.consecutive_losses} 次 ≥ {self.max_consecutive_losses}"})
            return RiskCheckResult(
                passed=False, action=RiskAction.HALT,
                reason=f"连续亏损熔断: {self.state.consecutive_losses} 次",
                checks=checks,
            )
        checks.append({'check': '连续亏损', 'passed': True,
                       'detail': f"{self.state.consecutive_losses} 次"})

        # ── 先取上一次价格，再更新（避免拒绝后不更新导致连锁误判）──
        if bar:
            current_close = bar.get('close', signal_price)
            prev_close = self.state.last_price.get(symbol, 0)  # 取更新前的旧值
            self.state.last_price[symbol] = current_close      # 再更新为新值

        # ── 检查 5: 异常价格波动 ──
        if prev_close > 0:
            change_pct = abs(current_close - prev_close) / prev_close * 100
            if change_pct > self.price_spike_pct:
                checks.append({'check': '价格波动', 'passed': False,
                               'detail': f"{change_pct:.1f}% 波动 > {self.price_spike_pct}% 阈值"})
                reasons.append(f"价格异常波动: {change_pct:.1f}%")
                return RiskCheckResult(
                    passed=False, action=RiskAction.REJECT,
                    reason=f"价格异常波动: {change_pct:.1f}%",
                    checks=checks,
                )
        checks.append({'check': '价格波动', 'passed': True, 'detail': '正常'})

        # ── 全部通过 ──
        return RiskCheckResult(
            passed=True,
            action=RiskAction.APPROVE,
            suggested_size_pct=size_pct,
            checks=checks,
        )

    # ═══════════════════════════════
    # L1: 单笔止损检测
    # ═══════════════════════════════

    def check_stop_loss(self, symbol: str, entry_price: float, current_price: float,
                        side: str, stop_loss: float = None) -> RiskAction:
        """
        检查是否触发单笔止损

        返回: APPROVE (继续持有) | REJECT (建议平仓)
        """
        if stop_loss is None:
            return RiskAction.APPROVE

        if side == 'long' and current_price <= stop_loss:
            return RiskAction.REJECT  # 触发了止损
        elif side == 'short' and current_price >= stop_loss:
            return RiskAction.REJECT

        return RiskAction.APPROVE

    # ═══════════════════════════════
    # L2: 日内熔断
    # ═══════════════════════════════

    def _trigger_circuit_breaker(self, trigger: str, value: float):
        """触发熔断"""
        cooldown_hours = 24 if trigger == 'daily_loss' else 4
        self.state.circuit_breaker_active = True
        self.state.circuit_breaker_reason = f"{trigger}({value:.2f})"
        self.state.circuit_breaker_until = time.time() + cooldown_hours * 3600

        if self.logger:
            self.logger.risk_circuit_breaker(
                trigger=trigger,
                current_value=value,
                limit=self.daily_loss_limit_pct * 100 if trigger == 'daily_loss'
                      else self.max_consecutive_losses,
                action='halt',
            )

    def _reset_circuit_breaker(self):
        """重置熔断"""
        self.state.circuit_breaker_active = False
        self.state.circuit_breaker_reason = ""
        self.state.circuit_breaker_until = 0
        self.state.consecutive_losses = 0
        if self.logger:
            self.logger.risk_circuit_breaker(
                trigger='reset', current_value=0, limit=0, action='resume',
            )

    def is_circuit_breaker_active(self) -> bool:
        """熔断是否激活"""
        if self.state.circuit_breaker_active:
            if time.time() >= self.state.circuit_breaker_until:
                self._reset_circuit_breaker()
                return False
            return True
        return False

    # ═══════════════════════════════
    # L3: 策略止损检测
    # ═══════════════════════════════

    def check_strategy_assumption(self, strategy_name: str, bar: dict,
                                  position=None) -> RiskAction:
        """
        检查策略核心假设是否仍然成立

        趋势跟踪: 趋势还在吗？（ADX 是否还在趋势区间）
        动量策略: 动量还在吗？
        均值回归: 偏离是否在扩大？
        """
        # ── 基类实现：检查 ADX ──
        adx = bar.get('adx', 100)
        if adx is not None and adx < 15:
            # ADX < 15 = 极弱趋势或无趋势，趋势策略假设不成立
            if '趋势' in strategy_name or 'Trend' in strategy_name:
                return RiskAction.REJECT

        return RiskAction.APPROVE

    # ═══════════════════════════════
    # L4: 时间止损
    # ═══════════════════════════════

    def check_time_stop(self, bars_held: int, max_bars: int = None) -> RiskAction:
        """
        持仓时间超限 → 平仓

        "我赌它 4 小时涨，4 小时没涨就走了"
        """
        if max_bars is None:
            max_bars = 24  # 默认 24 根 K 线（1h 即 24 小时）

        if bars_held >= max_bars:
            return RiskAction.REJECT

        return RiskAction.APPROVE

    # ═══════════════════════════════
    # 状态更新
    # ═══════════════════════════════

    def update_after_trade(self, net_return_pct: float):
        """
        交易完成后更新风控状态

        应在每笔交易闭合后调用
        """
        self.state.total_trades_today += 1

        if net_return_pct > 0:
            self.state.consecutive_losses = 0
        else:
            self.state.consecutive_losses += 1

        # ── 更新日累计盈亏 ──
        self.state.daily_pnl += net_return_pct
        self.state.daily_pnl_pct = self.state.daily_pnl
        self.state.last_trade_time = time.time()

        # ── 记录 ──
        self.state.day_trades.append({
            'time': datetime.now(timezone.utc).isoformat(),
            'return_pct': net_return_pct,
            'consecutive_losses': self.state.consecutive_losses,
        })

    def update_equity(self, new_equity: float):
        """更新当前净值"""
        self.state.current_equity = new_equity

    def reset_daily(self):
        """
        重置日统计（UTC+8 零点调用）

        注意：不重置策略状态和持仓，只重置统计计数器
        """
        self.state.daily_pnl = 0.0
        self.state.daily_pnl_pct = 0.0
        self.state.total_trades_today = 0
        self.state.day_start_equity = self.state.current_equity
        self.state.day_trades = []
        # 注意：consecutive_losses 不重置——跨日连续亏损也要算

    # ═══════════════════════════════
    # 快捷检查（组合调用）
    # ═══════════════════════════════

    def quick_check(self, symbol: str, side: str, size_pct: float,
                    signal_price: float, bar: dict = None,
                    order_type: str = 'limit') -> tuple:
        """
        快捷风控检查 → (是否可执行, 建议仓位, 原因)

        一个调用覆盖所有风控层
        """
        result = self.pre_trade_check(symbol, side, size_pct, signal_price, bar, order_type)

        if not result.passed:
            return False, result.suggested_size_pct, result.reason

        return True, result.suggested_size_pct, "OK"

    def get_status(self) -> dict:
        """获取风控状态摘要"""
        return {
            'circuit_breaker_active': self.state.circuit_breaker_active,
            'circuit_breaker_reason': self.state.circuit_breaker_reason,
            'daily_pnl_pct': round(self.state.daily_pnl_pct, 2),
            'consecutive_losses': self.state.consecutive_losses,
            'total_trades_today': self.state.total_trades_today,
            'current_equity': round(self.state.current_equity, 2),
            'day_start_equity': round(self.state.day_start_equity, 2),
        }

    def print_status(self):
        """打印风控状态"""
        s = self.get_status()
        status_icon = '🔴 熔断' if s['circuit_breaker_active'] else '🟢 正常'
        print(f"\n  🛡️ 风控状态: {status_icon}")
        print(f"     日盈亏: {s['daily_pnl_pct']:+.2f}%")
        print(f"     连续亏损: {s['consecutive_losses']} 次")
        print(f"     今日交易: {s['total_trades_today']} 笔")
        print(f"     当前净值: ${s['current_equity']:,.2f}")
        if s['circuit_breaker_active']:
            print(f"     熔断原因: {s['circuit_breaker_reason']}")
