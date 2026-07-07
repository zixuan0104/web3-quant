"""
多策略并行编排器 + 市场环境自动适配 — Day 11 核心模块

核心命题：3 个策略同时跑 ≠ 3 倍收益。策略之间可能同涨同跌（相关性风险），
多策略的真正价值在于对冲和互补，不是简单叠加。

编排器职责：
  1. 对每根 K 线，同时推送给所有活跃策略
  2. 收集各策略信号，按策略类型查 RegimeClassifier → 仓位乘数
  3. 计算净敞口（long 总仓位 - short 总仓位），检测过度集中
  4. 计算策略间交易相关性，相关性过高时降权
  5. 统一通过 RiskManager 最终审批
  6. 生成文字版多策略 Dashboard

仓位调整链：
  PositionSizer(基础仓位) × RegimeClassifier(环境乘数) × 相关性降权 → RiskManager(最终否决)

用法：
  from strategy_orchestrator import StrategyOrchestrator

  orch = StrategyOrchestrator(risk_manager=rm, regime_classifier=rc)
  orch.add_strategy('trend', trend_strategy, position_sizer)
  orch.add_strategy('momentum', momentum_strategy, position_sizer)

  for bar in bars:
      results = orch.on_bar(bar)
      # results: {'signals': [...], 'exposures': {...}, 'dashboard': {...}}
"""

from dataclasses import dataclass, field
from typing import Optional, Dict, List, Any, Tuple
from enum import Enum
from collections import deque
import math


# ═══════════════════════════════
# 数据类型
# ═══════════════════════════════

class SignalAction(Enum):
    ENTRY_LONG = "entry_long"
    ENTRY_SHORT = "entry_short"
    EXIT = "exit"
    HOLD = "hold"


@dataclass
class StrategySlot:
    """编排器中的一个策略槽位"""
    name: str                          # 策略显示名
    strategy_type: str                 # 'trend' | 'momentum' | 'breakout' | 'mean_reversion' | 'funding_arb'
    strategy_instance: Any             # BaseStrategy 子类实例
    position_sizer: Any                # PositionSizer 实例
    enabled: bool = True               # 是否启用
    correlation_weight: float = 1.0    # 相关性降权（1.0 = 无降权）
    trade_returns: deque = field(default_factory=lambda: deque(maxlen=30))  # 最近 30 笔收益率


@dataclass
class OrchestratorConfig:
    """编排器配置"""
    # ── 净敞口限制 ──
    max_net_exposure_pct: float = 0.60      # 净多头敞口上限 (long - short)
    max_gross_exposure_pct: float = 1.20    # 总敞口上限 (long + short)

    # ── 相关性 ──
    correlation_lookback: int = 20          # 相关性计算最少需要 N 笔共同交易
    correlation_warning_threshold: float = 0.60  # 相关性 > 此值 → 降权
    correlation_discount_factor: float = 0.70    # 高相关性时仓位折扣

    # ── 集中度 ──
    max_strategies_same_direction: int = 2  # 同一方向最多 N 个策略同时持仓

    # ── 数据输出 ──
    dashboard_width: int = 60               # 文字 Dashboard 宽度


# ═══════════════════════════════
# 多策略编排器
# ═══════════════════════════════

class StrategyOrchestrator:
    """多策略并行 + 市场环境适配"""

    def __init__(self, risk_manager=None, regime_classifier=None,
                 config: Optional[OrchestratorConfig] = None):
        """
        risk_manager: RiskManager 实例（最终审批）
        regime_classifier: RegimeClassifier 实例（环境适配）
        config: 编排器配置
        """
        self.risk_manager = risk_manager
        self.regime_classifier = regime_classifier
        self.config = config or OrchestratorConfig()

        # ── 策略槽 ──
        self._slots: Dict[str, StrategySlot] = {}

        # ── 运行时状态 ──
        self._current_bar: Optional[dict] = None
        self._current_idx: int = -1
        self._bar_count: int = 0
        self._signals: List[dict] = []       # 当前 bar 产生的所有信号

        # ── 统计 ──
        self._total_signals: int = 0
        self._rejected_signals: int = 0      # 被风控拒绝的信号数

    # ═══════════════════════════════
    # 策略管理
    # ═══════════════════════════════

    def add_strategy(self, name: str, strategy_type: str,
                     strategy_instance, position_sizer) -> None:
        """
        注册一个策略到编排器

        name: 策略显示名
        strategy_type: 'trend' | 'momentum' | 'breakout' | 'mean_reversion' | 'funding_arb'
        strategy_instance: BaseStrategy 子类实例
        position_sizer: PositionSizer 实例
        """
        self._slots[name] = StrategySlot(
            name=name,
            strategy_type=strategy_type,
            strategy_instance=strategy_instance,
            position_sizer=position_sizer,
        )

    def remove_strategy(self, name: str) -> None:
        """移除策略"""
        self._slots.pop(name, None)

    def enable_strategy(self, name: str) -> None:
        """启用策略"""
        if name in self._slots:
            self._slots[name].enabled = True

    def disable_strategy(self, name: str) -> None:
        """禁用策略（不改变现有持仓，只不再发新信号）"""
        if name in self._slots:
            self._slots[name].enabled = False

    @property
    def active_strategies(self) -> List[str]:
        """当前启用的策略名列表"""
        return [name for name, slot in self._slots.items() if slot.enabled]

    # ═══════════════════════════════
    # 事件循环
    # ═══════════════════════════════

    def on_bar(self, bar: dict, idx: int = None) -> dict:
        """
        每根 K 线回调 — 多策略并行入口

        流程：
          1. 更新市场环境（如果有 RegimeClassifier）
          2. 各策略独立运行 on_bar
          3. 收集所有入场信号
          4. 应用环境乘数 + 相关性降权
          5. RiskManager 审批
          6. 生成 Dashboard 快照

        返回:
          {
            'signals': [{strategy, action, side, price, size_pct, adjusted_size_pct, ...}],
            'exposures': {long_pct, short_pct, net_pct, gross_pct},
            'dashboard': {...},
            'rejected': [...],
          }
        """
        if idx is not None:
            self._current_idx = idx
        else:
            self._current_idx = self._bar_count
        self._current_bar = bar
        self._bar_count += 1
        self._signals = []

        # ── Step 0: 更新环境分类 ──
        if self.regime_classifier is not None:
            self.regime_classifier.update(bar)

        # ── Step 1: 各策略独立运行 ──
        raw_signals = []
        for name, slot in self._slots.items():
            if not slot.enabled:
                continue
            strat = slot.strategy_instance
            result = strat.on_bar(bar, self._current_idx)
            if result is not None and result.get('action') == 'entry':
                raw_signals.append({
                    'strategy_name': name,
                    'strategy_type': slot.strategy_type,
                    'side': result['side'],
                    'price': result['price'],
                    'stop_loss': result.get('stop_loss'),
                    'take_profit': result.get('take_profit'),
                })

        # ── Step 2: 相关性更新（利用最近交易记录）──
        self._update_correlations()

        # ── Step 3: 对每个入场信号应用调整 ──
        processed_signals = []
        rejected_signals = []
        for sig in raw_signals:
            slot = self._slots[sig['strategy_name']]

            # 3a. 基础仓位计算
            ps = slot.position_sizer
            sizing_result = ps.calculate(
                symbol='BTC/USDT',  # 默认，实际使用时可从 bar 中获取
                side=sig['side'],
                signal_price=sig['price'],
            )
            base_size_pct = sizing_result['size_pct']

            # 3b. 环境乘数
            regime_mult = 1.0
            if self.regime_classifier is not None:
                # 先检查是否允许开新仓
                if not self.regime_classifier.should_open_new_position():
                    rejected_signals.append({
                        **sig,
                        'reject_reason': f"环境 {self.regime_classifier.regime.value} 禁止开新仓",
                    })
                    continue
                regime_mult = self.regime_classifier.get_strategy_multiplier(slot.strategy_type)
                if regime_mult <= 0:
                    rejected_signals.append({
                        **sig,
                        'reject_reason': f"环境乘数为 0（{slot.strategy_type} 不适合当前 {self.regime_classifier.regime.value}）",
                    })
                    continue

            # 3c. 相关性降权
            corr_weight = slot.correlation_weight

            # 3d. 集中度检查
            concentration_ok = self._check_concentration(sig, processed_signals)
            if not concentration_ok:
                rejected_signals.append({
                    **sig,
                    'reject_reason': '同一方向已有足够策略持仓（集中度限制）',
                })
                continue

            # 3e. 计算最终仓位
            adjusted_size_pct = base_size_pct * regime_mult * corr_weight
            adjusted_size_pct = min(adjusted_size_pct, 0.20)  # 单策略硬上限 20%

            sig['base_size_pct'] = round(base_size_pct, 4)
            sig['regime_multiplier'] = round(regime_mult, 3)
            sig['correlation_weight'] = round(corr_weight, 3)
            sig['adjusted_size_pct'] = round(adjusted_size_pct, 4)
            sig['action'] = f"entry_{sig['side']}"

            # 3f. RiskManager 审批
            if self.risk_manager is not None:
                risk_result = self.risk_manager.pre_trade_check(
                    symbol='BTC/USDT',
                    side=sig['side'],
                    size_pct=adjusted_size_pct,
                    signal_price=sig['price'],
                    bar=bar,
                )
                if not risk_result.passed:
                    sig['risk_rejected'] = True
                    sig['risk_reason'] = risk_result.reason
                    rejected_signals.append(sig)
                    self._rejected_signals += 1
                    continue
                # 风控可能调整仓位
                if risk_result.suggested_size_pct > 0:
                    sig['adjusted_size_pct'] = round(
                        min(adjusted_size_pct, risk_result.suggested_size_pct), 4
                    )

            sig['risk_rejected'] = False
            processed_signals.append(sig)
            self._total_signals += 1

        self._signals = processed_signals + rejected_signals

        # ── Step 4: 计算敞口 ──
        exposures = self._calculate_exposures(processed_signals)

        # ── Step 5: 生成 Dashboard（每 N 根或头寸变化时）──
        dashboard = self.dashboard()

        return {
            'signals': processed_signals,
            'rejected': rejected_signals,
            'exposures': exposures,
            'dashboard': dashboard,
            'bar_idx': self._current_idx,
        }

    # ═══════════════════════════════
    # 相关性 & 集中度
    # ═══════════════════════════════

    def _update_correlations(self) -> None:
        """从各策略的交易记录更新收益率序列并计算相关性"""
        # 收集所有启用策略的收益率序列
        returns_map = {}
        for name, slot in self._slots.items():
            if not slot.enabled:
                continue
            trades = slot.strategy_instance.trade_log
            if len(trades) < self.config.correlation_lookback:
                continue
            recent = trades[-self.config.correlation_lookback:]
            rets = [t['net_return_pct'] for t in recent]
            # 用 0 填充到等长
            if len(rets) < self.config.correlation_lookback:
                rets = [0.0] * (self.config.correlation_lookback - len(rets)) + rets
            returns_map[name] = rets

        if len(returns_map) < 2:
            return

        # 计算两两相关性
        corr_matrix = {}
        names = list(returns_map.keys())
        for i in range(len(names)):
            for j in range(i + 1, len(names)):
                n1, n2 = names[i], names[j]
                r1, r2 = returns_map[n1], returns_map[n2]
                corr = self._pearson(r1, r2)
                corr_matrix[(n1, n2)] = corr

        # 找出高相关性策略对 → 降权
        # 降权逻辑：两个策略相关性 > 阈值时，两者都打折
        for (n1, n2), corr in corr_matrix.items():
            if abs(corr) >= self.config.correlation_warning_threshold:
                # 相关性越高折扣越大
                discount = self.config.correlation_discount_factor
                # min(corr, 1.0) 防止 corr>1 时过度折扣
                adjusted = discount * min(abs(corr), 1.0)
                # 现有权重取平均（两个策略都降）
                w1 = self._slots[n1].correlation_weight
                w2 = self._slots[n2].correlation_weight
                self._slots[n1].correlation_weight = min(w1, adjusted)
                self._slots[n2].correlation_weight = min(w2, adjusted)

    @staticmethod
    def _pearson(x: List[float], y: List[float]) -> float:
        """计算皮尔逊相关系数"""
        n = len(x)
        if n < 3:
            return 0.0
        mean_x = sum(x) / n
        mean_y = sum(y) / n
        cov = sum((xi - mean_x) * (yi - mean_y) for xi, yi in zip(x, y))
        std_x = (sum((xi - mean_x) ** 2 for xi in x)) ** 0.5
        std_y = (sum((yi - mean_y) ** 2 for yi in y)) ** 0.5
        if std_x == 0 or std_y == 0:
            return 0.0
        return cov / (std_x * std_y)

    def _check_concentration(self, new_signal: dict, existing: List[dict]) -> bool:
        """
        检查同一方向是否已有过多策略持仓

        new_signal: 待审批的新入场信号
        existing: 本轮已通过的信号列表
        """
        # 统计同一方向的策略数
        same_direction = 0
        target_side = new_signal['side']

        # 已通过的信号
        for sig in existing:
            if sig['side'] == target_side:
                same_direction += 1

        # 已有持仓的策略
        for name, slot in self._slots.items():
            if not slot.enabled:
                continue
            pos = slot.strategy_instance.position
            if pos is not None and pos.side == target_side:
                same_direction += 1

        return same_direction < self.config.max_strategies_same_direction

    def get_pairwise_correlations(self) -> Dict[Tuple[str, str], float]:
        """获取所有策略对的相关性（用于 Dashboard）"""
        returns_map = {}
        for name, slot in self._slots.items():
            if not slot.enabled:
                continue
            trades = slot.strategy_instance.trade_log
            if len(trades) < self.config.correlation_lookback:
                continue
            recent = trades[-self.config.correlation_lookback:]
            returns_map[name] = [t['net_return_pct'] for t in recent]

        result = {}
        names = list(returns_map.keys())
        for i in range(len(names)):
            for j in range(i + 1, len(names)):
                n1, n2 = names[i], names[j]
                if len(returns_map[n1]) >= 3 and len(returns_map[n2]) >= 3:
                    # 对齐长度
                    min_len = min(len(returns_map[n1]), len(returns_map[n2]))
                    r1 = returns_map[n1][-min_len:]
                    r2 = returns_map[n2][-min_len:]
                    result[(n1, n2)] = round(self._pearson(r1, r2), 3)
        return result

    # ═══════════════════════════════
    # 敞口计算
    # ═══════════════════════════════

    def _calculate_exposures(self, signals: List[dict]) -> dict:
        """
        计算当前总敞口

        已有持仓 + 本轮新增信号 = 瞬时敞口

        返回:
          {
            'long_pct': 多头总仓位%,
            'short_pct': 空头总仓位%,
            'net_pct': 净多头% (long - short),
            'gross_pct': 总敞口% (long + short),
            'is_hedged': 是否对冲 (long>0 and short>0),
          }
        """
        long_pct = 0.0
        short_pct = 0.0

        # 已有持仓
        for name, slot in self._slots.items():
            if not slot.enabled:
                continue
            pos = slot.strategy_instance.position
            if pos is None:
                continue
            # 用 entry_price 和 current_price 估算仓位%
            # 简化：直接用最近交易记录里的仓位信息
            if pos.side == 'long':
                long_pct += 0.05  # 默认 5%，实际应查询 position_sizer
            else:
                short_pct += 0.05

        # 本轮信号（使用调整后的仓位%）
        for sig in signals:
            if sig.get('risk_rejected', False):
                continue
            size = sig.get('adjusted_size_pct', 0.02)
            if sig['side'] == 'long':
                long_pct += size
            else:
                short_pct += size

        return {
            'long_pct': round(long_pct, 4),
            'short_pct': round(short_pct, 4),
            'net_pct': round(long_pct - short_pct, 4),
            'gross_pct': round(long_pct + short_pct, 4),
            'is_hedged': long_pct > 0 and short_pct > 0,
            'exposure_ok': (abs(long_pct - short_pct) <= self.config.max_net_exposure_pct
                            and (long_pct + short_pct) <= self.config.max_gross_exposure_pct),
        }

    # ═══════════════════════════════
    # Dashboard
    # ═══════════════════════════════

    def dashboard(self) -> dict:
        """
        生成当前多策略状态快照

        用于文字 Dashboard、Telegram 推送、日志记录
        """
        # ── 各策略状态 ──
        strategy_states = []
        for name, slot in self._slots.items():
            strat = slot.strategy_instance
            pos = strat.position
            regime_mult = 1.0
            if self.regime_classifier is not None:
                regime_mult = self.regime_classifier.get_strategy_multiplier(slot.strategy_type)

            state = {
                'name': name,
                'type': slot.strategy_type,
                'enabled': slot.enabled,
                'has_position': pos is not None,
                'position_side': pos.side if pos else None,
                'position_entry': round(pos.entry_price, 2) if pos else None,
                'regime_multiplier': round(regime_mult, 3),
                'correlation_weight': round(slot.correlation_weight, 3),
                'recent_trades': len(strat.trade_log),
                'effective_multiplier': round(regime_mult * slot.correlation_weight, 3),
            }
            strategy_states.append(state)

        # ── 相关性 ──
        correlations = self.get_pairwise_correlations()
        high_corr_pairs = [
            f"{n1}×{n2}" for (n1, n2), c in correlations.items()
            if abs(c) >= self.config.correlation_warning_threshold
        ]

        # ── 环境信息 ──
        regime_info = None
        if self.regime_classifier is not None:
            regime_info = self.regime_classifier.dashboard()

        # ── 敞口 ──
        exposures = self._calculate_exposures(self._signals)

        current_ts = self._bar_count
        if self._current_bar is not None:
            current_ts = self._current_bar.get('timestamp', self._bar_count)
        return {
            'timestamp': current_ts,
            'bar_idx': self._current_idx,
            'bar_count': self._bar_count,
            'regime': regime_info,
            'strategies': strategy_states,
            'exposures': exposures,
            'correlations': {f"{n1}×{n2}": c for (n1, n2), c in correlations.items()},
            'high_correlation_pairs': high_corr_pairs,
            'active_long_strategies': sum(1 for s in strategy_states
                                          if s['has_position'] and s['position_side'] == 'long'),
            'active_short_strategies': sum(1 for s in strategy_states
                                           if s['has_position'] and s['position_side'] == 'short'),
            'total_signals': self._total_signals,
            'rejected_signals': self._rejected_signals,
        }

    def dashboard_text(self, width: int = None) -> str:
        """
        生成文字版 Dashboard（适合 Telegram 推送或终端打印）

        返回多行字符串
        """
        w = width or self.config.dashboard_width
        dash = self.dashboard()

        lines = []
        lines.append("=" * w)
        lines.append("  多策略 Dashboard".center(w - 6))
        lines.append("=" * w)

        # ── 环境 ──
        if dash['regime']:
            r = dash['regime']
            regime_emoji = {
                'trending_up': '[上]', 'trending_down': '[下]',
                'ranging': '[横]', 'volatile': '[波]', 'transitioning': '[过]',
            }
            em = regime_emoji.get(r['regime'], '[?]')
            lines.append(f"  市场环境: {em} {r['regime']} (持续 {r['regime_duration']} 根)")
            lines.append(f"  环境得分: {r['scores']}")
            lines.append(f"  止损调整: {r['stop_loss_adj']}x | 仓位上限: {r['max_position_adj']}x | 可开新仓: {r['can_open_new']}")
            lines.append("-" * w)

        # ── 敞口 ──
        e = dash['exposures']
        lines.append(f"  净敞口: {e['net_pct']:.2%} | 多头: {e['long_pct']:.2%} | 空头: {e['short_pct']:.2%} | 总敞口: {e['gross_pct']:.2%}")
        hedge_status = "[对冲]" if e['is_hedged'] else "[单边]"
        exposure_status = "[OK]" if e['exposure_ok'] else "[!超限]"
        lines.append(f"  对冲状态: {hedge_status} | 敞口检查: {exposure_status}")
        lines.append("-" * w)

        # ── 策略状态 ──
        lines.append(f"  {'策略':<20} {'类型':<12} {'状态':<8} {'持仓':<8} {'乘数':<8}")
        lines.append("  " + "-" * (w - 4))
        for s in dash['strategies']:
            status = "[ON] " if s['enabled'] else "[OFF]"
            pos_str = f"{s['position_side']}" if s['has_position'] else "空仓"
            lines.append(
                f"  {s['name']:<20} {s['type']:<12} {status:<8} {pos_str:<8} {s['effective_multiplier']:.2f}"
            )
        lines.append("-" * w)

        # ── 相关性告警 ──
        if dash['high_correlation_pairs']:
            lines.append(f"  [!] 高相关性策略对: {', '.join(dash['high_correlation_pairs'])}")
            lines.append(f"       同涨同跌 → 仓位已自动降权")
        else:
            lines.append(f"  相关性: 无告警 (所有策略对相关性 < {self.config.correlation_warning_threshold})")
        lines.append("-" * w)

        # ── 统计 ──
        lines.append(f"  活跃策略: {len([s for s in dash['strategies'] if s['enabled']])} | "
                     f"多头策略: {dash['active_long_strategies']} | 空头策略: {dash['active_short_strategies']}")
        lines.append(f"  累计信号: {dash['total_signals']} | 风控拒绝: {dash['rejected_signals']}")
        lines.append("=" * w)

        return "\n".join(lines)

    # ═══════════════════════════════
    # 对冲检查
    # ═══════════════════════════════

    def is_naturally_hedged(self) -> bool:
        """
        检查是否存在自然对冲：
        至少一个策略做多 + 至少一个策略做空 → 市场中性倾向
        """
        has_long = False
        has_short = False
        for name, slot in self._slots.items():
            if not slot.enabled:
                continue
            pos = slot.strategy_instance.position
            if pos is None:
                continue
            if pos.side == 'long':
                has_long = True
            elif pos.side == 'short':
                has_short = True
        return has_long and has_short

    def suggested_rebalance(self) -> dict:
        """
        根据当前环境给出手动调仓建议

        返回:
          {'action': 'reduce_long'|'reduce_short'|'hold'|'close_all',
           'reason': str,
           'suggested_net_exposure': float}
        """
        dash = self.dashboard()
        exposures = dash['exposures']

        # 净敞口超限
        if not exposures['exposure_ok']:
            if exposures['net_pct'] > self.config.max_net_exposure_pct:
                return {
                    'action': 'reduce_long',
                    'reason': f"净多头 {exposures['net_pct']:.1%} 超限 {self.config.max_net_exposure_pct:.0%}",
                    'suggested_net_exposure': self.config.max_net_exposure_pct * 0.8,
                }
            elif exposures['net_pct'] < -self.config.max_net_exposure_pct:
                return {
                    'action': 'reduce_short',
                    'reason': f"净空头 {abs(exposures['net_pct']):.1%} 超限 {self.config.max_net_exposure_pct:.0%}",
                    'suggested_net_exposure': -self.config.max_net_exposure_pct * 0.8,
                }

        # 过渡期 → 减仓
        if dash['regime'] and dash['regime']['regime'] == 'transitioning':
            return {
                'action': 'close_all',
                'reason': '市场处于过渡期，建议平仓观望',
                'suggested_net_exposure': 0.0,
            }

        # 高波动 → 减仓
        if dash['regime'] and dash['regime']['regime'] == 'volatile':
            return {
                'action': 'reduce_long',
                'reason': '高波动环境，建议降低净敞口',
                'suggested_net_exposure': self.config.max_net_exposure_pct * 0.5,
            }

        return {
            'action': 'hold',
            'reason': '当前配置合理',
            'suggested_net_exposure': exposures['net_pct'],
        }


# ═══════════════════════════════
# 自测
# ═══════════════════════════════

if __name__ == '__main__':
    import random

    print("=" * 60)
    print("  多策略编排器 — 自测")
    print("=" * 60)

    # 需要模拟策略和仓位计算器来测试编排器
    from unittest.mock import MagicMock

    # 创建 mock 策略
    def create_mock_strategy(name, has_position=False, side=None):
        strat = MagicMock()
        strat.name = name
        strat.position = MagicMock() if has_position else None
        if has_position:
            strat.position.side = side
            strat.position.entry_price = 65000
        strat.trade_log = [
            {'net_return_pct': random.uniform(-2, 3)} for _ in range(25)
        ]
        strat.on_bar.return_value = None  # 默认不产生信号
        return strat

    orch = StrategyOrchestrator()

    # 注册 3 个策略
    from position_sizer import PositionSizer, PositionConfig
    ps = PositionSizer(initial_capital=10000)

    trend = create_mock_strategy('趋势跟踪(EMA_5/20)', has_position=False)
    momentum = create_mock_strategy('动量策略(MOM_10/30)', has_position=True, side='long')
    breakout = create_mock_strategy('突破策略(DC_20)', has_position=False)

    orch.add_strategy('trend_ema', 'trend', trend, ps)
    orch.add_strategy('momentum', 'momentum', momentum, ps)
    orch.add_strategy('breakout_dc', 'breakout', breakout, ps)

    # 模拟几根 K 线
    price = 65000
    for i in range(10):
        price *= (1 + random.gauss(0.0005, 0.01))
        bar = {
            'close': price,
            'high': price * 1.005,
            'low': price * 0.995,
            'volume': random.uniform(500, 2000),
            'timestamp': i,
        }
        result = orch.on_bar(bar, idx=i)

    print("\n[Dashboard 文字版]")
    print(orch.dashboard_text())

    print("\n[敞口]")
    print(orch._calculate_exposures([]))

    print("\n[调仓建议]")
    print(orch.suggested_rebalance())

    print("\n" + "=" * 60)
    print("  自测完成")
    print("=" * 60)
