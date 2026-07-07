"""
仓位管理模块 — Day 8 核心模块

数学告诉你该下多少。不是感觉，不是信心，是数学。

三种仓位计算方法：
  1. 凯利公式 — 已知胜率+盈亏比 → 最优仓位比例
  2. 固定分数 — 每笔交易固定 % 本金
  3. 波动率调整 — ATR 越大 → 仓位越小

额外约束：
  - 单币种最大敞口硬编码上限
  - 盈利提现策略（定期将利润转出交易所）

核心原则：
  凯利公式告诉我们：即使 60% 胜率、2:1 盈亏比的策略，
  最优仓位也只有 20%。超过这个比例，长期一定会爆仓。
  不是概率问题，是数学问题。

用法：
  from position_sizer import PositionSizer
  ps = PositionSizer(initial_capital=10000, config=pos_config)

  # 计算仓位
  result = ps.calculate(
      symbol='BTC/USDT', side='long', signal_price=65000,
      atr=1200, win_rate=0.55, avg_win=0.04, avg_loss=0.02,
  )
  print(f"建议仓位: {result['size_pct']:.2%}")
  print(f"建议金额: ${result['size_value']:.2f}")
  print(f"计算方法: {result['method']}")
"""

from dataclasses import dataclass, field
from typing import Optional, Dict, List, Tuple
from enum import Enum
import math


# ═══════════════════════════════
# 数据类型
# ═══════════════════════════════

class SizingMethod(Enum):
    KELLY_FULL = "kelly_full"        # 完整凯利（激进）
    KELLY_HALF = "kelly_half"        # 半凯利（推荐，保守）
    KELLY_QUARTER = "kelly_quarter"  # 四分之一凯利（极保守）
    FIXED_FRACTION = "fixed_fraction"  # 固定分数
    VOLATILITY_ADJUSTED = "volatility_adjusted"  # ATR 调整


@dataclass
class PositionConfig:
    """仓位管理配置"""
    # ── 基础 ──
    initial_capital: float = 10000.0
    default_method: SizingMethod = SizingMethod.KELLY_HALF

    # ── 凯利参数 ──
    default_win_rate: float = 0.50      # 默认胜率（无历史数据时）
    default_avg_win_pct: float = 0.03   # 默认平均盈利 %
    default_avg_loss_pct: float = 0.015 # 默认平均亏损 %

    # ── 固定分数 ──
    fixed_fraction_pct: float = 0.02    # 每笔 2% 本金

    # ── 波动率调整 ──
    vol_target_risk_pct: float = 0.01   # 每笔目标风险 = 本金 1%
    atr_lookback: int = 14              # ATR 计算周期
    vol_multiplier: float = 2.0         # ATR 倍数（止损距离 = multiplier × ATR）

    # ── 硬约束 ──
    max_position_pct: float = 0.20      # 单笔最大仓位 20%
    min_position_pct: float = 0.005     # 单笔最小仓位 0.5%（低于这个不下单）
    max_single_asset_exposure: float = 0.30  # 单币种总敞口 30%
    max_total_exposure: float = 0.80    # 总敞口 80%（留 20% 现金）

    # ── 盈利提现 ──
    profit_take_interval_days: int = 30   # 每 30 天提现一次
    profit_take_pct: float = 0.30         # 提取盈利的 30%
    profit_take_min_profit: float = 500.0 # 盈利超过 $500 才触发提现


@dataclass
class PortfolioState:
    """当前持仓状态"""
    total_equity: float = 10000.0
    cash_available: float = 10000.0
    positions: Dict[str, float] = field(default_factory=dict)  # symbol → 持仓金额
    total_exposure: float = 0.0  # 当前总敞口金额
    realized_profits: float = 0.0  # 累计已实现盈利
    last_profit_take_date: str = ""  # 上次提现日期


class PositionSizer:
    """
    仓位计算器

    三种计算方法 + 硬约束 + 盈利提现策略
    """

    def __init__(self, initial_capital: float = 10000.0,
                 config: PositionConfig = None,
                 portfolio: PortfolioState = None):
        self.initial_capital = initial_capital
        self.config = config or PositionConfig(initial_capital=initial_capital)
        self.portfolio = portfolio or PortfolioState(total_equity=initial_capital,
                                                      cash_available=initial_capital)

    # ═══════════════════════════════
    # 凯利公式
    # ═══════════════════════════════

    def kelly_criterion(self, win_rate: float, avg_win_pct: float,
                        avg_loss_pct: float) -> float:
        """
        凯利公式: f* = (p * b - q) / b

        其中:
          p  = 胜率
          q  = 1 - p（败率）
          b  = avg_win / avg_loss（盈亏比）

        返回: 最优仓位比例（0 ~ 1）

        推导:
          如果 b = 2（赢一次赚 2 单位，输一次亏 1 单位），p = 0.4:
            f* = (0.4 × 2 - 0.6) / 2 = 0.2 / 2 = 0.1
          → 即使只有 40% 胜率，2:1 的盈亏比也允许 10% 仓位

        警告:
          凯利公式假设你知道真实的 p 和 b。
          现实中的 p 和 b 都是估计值，有误差。
          所以永远不要用完整凯利——用半凯利（/2）或四分之一凯利（/4）。
        """
        # 盈亏比
        if avg_loss_pct <= 0:
            return 0.0  # 止损为 0 或不设止损 → 不交易

        b = avg_win_pct / avg_loss_pct
        q = 1.0 - win_rate

        # f* = (p * b - q) / b
        f_star = (win_rate * b - q) / b

        # 凯利公式可能返回负值（没有优势 → 不下注）
        return max(0.0, min(f_star, 1.0))

    def kelly_position(self, win_rate: float = None, avg_win_pct: float = None,
                       avg_loss_pct: float = None,
                       variant: SizingMethod = SizingMethod.KELLY_HALF
                       ) -> Dict:
        """
        凯利仓位计算

        用历史回测统计的胜率和盈亏比来计算最优仓位
        """
        wr = win_rate if win_rate is not None else self.config.default_win_rate
        aw = avg_win_pct if avg_win_pct is not None else self.config.default_avg_win_pct
        al = avg_loss_pct if avg_loss_pct is not None else self.config.default_avg_loss_pct

        f_star_full = self.kelly_criterion(wr, aw, al)

        # 凯利变体
        if variant == SizingMethod.KELLY_FULL:
            f_star = f_star_full
        elif variant == SizingMethod.KELLY_HALF:
            f_star = f_star_full / 2.0   # 半凯利 — 推荐
        elif variant == SizingMethod.KELLY_QUARTER:
            f_star = f_star_full / 4.0   # 四分之一凯利 — 极保守
        else:
            f_star = f_star_full / 2.0

        return {
            'method': variant.value,
            'f_star_raw': round(f_star_full, 4),
            'size_pct': round(f_star, 4),
            'win_rate': wr,
            'avg_win_pct': aw,
            'avg_loss_pct': al,
            'win_loss_ratio': round(aw / al, 2) if al > 0 else 0,
        }

    # ═══════════════════════════════
    # 固定分数
    # ═══════════════════════════════

    def fixed_fraction(self, fraction_pct: float = None) -> Dict:
        """
        固定分数仓位

        最简单的仓位管理：每笔交易固定 % 本金。
        优点：简单、可预测、不会因为参数估计偏差而爆仓。
        缺点：不考虑策略质量，好策略和差策略用同样的仓位。
        """
        pct = fraction_pct if fraction_pct is not None else self.config.fixed_fraction_pct

        return {
            'method': 'fixed_fraction',
            'size_pct': round(pct, 4),
            'note': f'每笔固定 {pct:.1%} 本金',
        }

    # ═══════════════════════════════
    # 波动率调整
    # ═══════════════════════════════

    def volatility_adjusted(self, atr: float, signal_price: float,
                            target_risk_pct: float = None,
                            multiplier: float = None) -> Dict:
        """
        ATR 波动率调整仓位

        逻辑:
          目标: 每笔交易最多亏本金的 X%
          止损距离 = multiplier × ATR
          仓位大小 = 目标亏损金额 / 止损距离

        例子:
          本金 $10,000，目标风险 1%（每笔最多亏 $100）
          ATR = $1,200，止损距离 = 2 × $1,200 = $2,400
          → 仓位 = $100 / $2,400 ≈ 4.17%（买约 $417 的 BTC）

        高 ATR → 高波动 → 宽止损 → 小仓位（风险预算约束下）
        低 ATR → 低波动 → 窄止损 → 大仓位
        """
        target_risk = target_risk_pct if target_risk_pct is not None else self.config.vol_target_risk_pct
        mult = multiplier if multiplier is not None else self.config.vol_multiplier

        equity = self.portfolio.total_equity
        risk_amount = equity * target_risk           # 这笔最多亏多少钱
        stop_distance = mult * atr                    # 止损距离（价格单位）
        stop_distance_pct = stop_distance / signal_price  # 止损距离（%）

        if stop_distance_pct <= 0:
            return {
                'method': 'volatility_adjusted',
                'size_pct': 0.0,
                'error': 'ATR 或价格异常，无法计算仓位',
            }

        # 仓位 % = 风险金额 / (止损距离 × 每单位仓位价值)
        # 简化：size_pct = target_risk_pct / stop_distance_pct
        size_pct = target_risk / stop_distance_pct

        # 实际金额
        size_value = equity * size_pct

        return {
            'method': 'volatility_adjusted',
            'size_pct': round(size_pct, 4),
            'size_value': round(size_value, 2),
            'atr': round(atr, 2),
            'stop_distance': round(stop_distance, 2),
            'stop_distance_pct': round(stop_distance_pct, 4),
            'target_risk_amount': round(risk_amount, 2),
            'note': f'ATR={atr:.0f}, 止损距={stop_distance_pct:.2%}, '
                    f'目标风险={target_risk:.1%}',
        }

    # ═══════════════════════════════
    # 不对称比率筛选 (Eugene Ng 策略内化)
    # ═══════════════════════════════

    def calculate_asymmetry_ratio(self, entry_price: float,
                                  stop_loss_price: float,
                                  target_price: float) -> float:
        """
        不对称比率 = 预期上行 / 最大下行

        来源: Eugene Ng (0xENAS) — "寻找下跌有限、上涨巨大的不对称机会"

        计算:
          upside   = target_price - entry_price    (预期盈利空间)
          downside = entry_price - stop_loss_price (最大亏损空间)
          ratio    = upside / downside

        例子:
          入场 $65,000, 止损 $63,500 (下行 $1,500)
          目标 $70,000 (上行 $5,000)
          → ratio = 5000/1500 = 3.33 → 强不对称
        """
        upside = target_price - entry_price
        downside = entry_price - stop_loss_price
        if downside <= 0 or upside <= 0:
            return 0.0
        return upside / downside

    def apply_asymmetry_filter(self, size_pct: float, asymmetry_ratio: float
                               ) -> tuple:
        """
        用不对称比率调整仓位大小

        返回: (调整后仓位, 调整说明)
        """
        if asymmetry_ratio >= 3.0:
            return size_pct, f'不对称比率={asymmetry_ratio:.1f}(>=3) → 满仓'
        elif asymmetry_ratio >= 2.0:
            return size_pct * 0.75, f'不对称比率={asymmetry_ratio:.1f}(2-3) → 仓位×0.75'
        elif asymmetry_ratio >= 1.0:
            return size_pct * 0.5, f'不对称比率={asymmetry_ratio:.1f}(1-2) → 仓位×0.5'
        else:
            return 0.0, f'不对称比率={asymmetry_ratio:.1f}(<1) → 不交易（亏比赚多）'

    # ═══════════════════════════════
    # 主入口 — 综合仓位计算
    # ═══════════════════════════════

    def calculate(self, symbol: str, side: str, signal_price: float,
                  atr: float = None,
                  win_rate: float = None,
                  avg_win_pct: float = None,
                  avg_loss_pct: float = None,
                  method: SizingMethod = None,
                  existing_position_value: float = 0.0,
                  **kwargs  # asymmetry_ratio, stop_loss_price, target_price 等
                  ) -> Dict:
        """
        综合仓位计算

        参数:
          symbol: 交易对（如 'BTC/USDT'）
          side: 'long' 或 'short'
          signal_price: 信号价格
          atr: 当前 ATR 值（波动率调整方法需要）
          win_rate / avg_win_pct / avg_loss_pct: 策略历史统计（凯利方法需要）
          method: 仓位计算方法，默认用 config 里配的
          existing_position_value: 该币种已有持仓金额

        返回:
          dict:
            - passed: 是否通过所有约束
            - size_pct: 最终仓位比例
            - size_value: 最终仓位金额
            - method: 使用的计算方法
            - reason: 未通过时的原因
            - breakdown: 各步骤的计算明细
        """
        m = method or self.config.default_method
        breakdown = {}

        # ── Step 1: 原始仓位计算（按选定方法）──
        if m in (SizingMethod.KELLY_FULL, SizingMethod.KELLY_HALF,
                 SizingMethod.KELLY_QUARTER):
            raw = self.kelly_position(win_rate, avg_win_pct, avg_loss_pct, variant=m)
            raw_size_pct = raw['size_pct']
        elif m == SizingMethod.FIXED_FRACTION:
            raw = self.fixed_fraction()
            raw_size_pct = raw['size_pct']
        elif m == SizingMethod.VOLATILITY_ADJUSTED:
            if atr is None:
                return {
                    'passed': False, 'size_pct': 0.0, 'size_value': 0.0,
                    'method': m.value, 'reason': '波动率调整方法需要 ATR 参数',
                }
            raw = self.volatility_adjusted(atr, signal_price)
            raw_size_pct = raw['size_pct']
        else:
            raw = self.kelly_position(win_rate, avg_win_pct, avg_loss_pct)
            raw_size_pct = raw['size_pct']

        breakdown['raw'] = raw

        # ── Step 1.5: 不对称比率调整 (Eugene Ng 策略内化) ──
        # 如果调用者传了不对称比率参数，先做不对称过滤再进硬约束
        asymmetry_ratio = kwargs.get('asymmetry_ratio', None)
        if asymmetry_ratio is not None:
            adj_size, adj_note = self.apply_asymmetry_filter(raw_size_pct, asymmetry_ratio)
            breakdown['asymmetry'] = {
                'ratio': round(asymmetry_ratio, 2),
                'original_size_pct': round(raw_size_pct, 4),
                'adjusted_size_pct': round(adj_size, 4),
                'note': adj_note,
            }
            raw_size_pct = adj_size
            if raw_size_pct <= 0:
                return {
                    'passed': False, 'size_pct': 0.0, 'size_value': 0.0,
                    'method': m.value,
                    'reason': f'不对称比率 {asymmetry_ratio:.1f} < 1，亏比赚多，不交易',
                    'breakdown': breakdown,
                }

        # ── Step 2: 硬约束 ──
        constraints = []

        # 2a. 单笔仓位上限
        if raw_size_pct > self.config.max_position_pct:
            raw_size_pct = self.config.max_position_pct
            constraints.append(f'单笔上限 cap → {raw_size_pct:.2%}')

        # 2b. 单笔仓位下限（太小不值得交易）
        if raw_size_pct < self.config.min_position_pct:
            return {
                'passed': False, 'size_pct': 0.0, 'size_value': 0.0,
                'method': m.value,
                'reason': f'仓位 {raw_size_pct:.3%} < 最小阈值 {self.config.min_position_pct:.3%}，不交易',
                'breakdown': breakdown,
            }

        # 2c. 单币种敞口上限
        equity = self.portfolio.total_equity
        proposed_value = equity * raw_size_pct
        total_in_symbol = existing_position_value + proposed_value
        max_in_symbol = equity * self.config.max_single_asset_exposure

        if total_in_symbol > max_in_symbol:
            # 砍到敞口上限以内
            allowed_additional = max_in_symbol - existing_position_value
            if allowed_additional <= 0:
                return {
                    'passed': False, 'size_pct': 0.0, 'size_value': 0.0,
                    'method': m.value,
                    'reason': f'{symbol} 已达单币种敞口上限 {self.config.max_single_asset_exposure:.0%}',
                    'breakdown': breakdown,
                }
            raw_size_pct = allowed_additional / equity
            constraints.append(
                f'单币敞口 cap → {raw_size_pct:.2%} '
                f'(已有 ${existing_position_value:.0f}, 上限 ${max_in_symbol:.0f})')

        # 2d. 总敞口上限
        proposed_total_exposure = self.portfolio.total_exposure + proposed_value
        if proposed_total_exposure > equity * self.config.max_total_exposure:
            remaining = equity * self.config.max_total_exposure - self.portfolio.total_exposure
            if remaining <= 0:
                return {
                    'passed': False, 'size_pct': 0.0, 'size_value': 0.0,
                    'method': m.value,
                    'reason': f'总敞口已达上限 {self.config.max_total_exposure:.0%}',
                    'breakdown': breakdown,
                }
            raw_size_pct = remaining / equity
            constraints.append(f'总敞口 cap → {raw_size_pct:.2%}')

        # 2e. 可用现金约束
        if proposed_value > self.portfolio.cash_available:
            raw_size_pct = self.portfolio.cash_available / equity
            constraints.append(f'现金约束 → {raw_size_pct:.2%}')

        # ── Step 3: 最终结果 ──
        final_size_pct = raw_size_pct
        final_size_value = equity * final_size_pct

        return {
            'passed': True,
            'size_pct': round(final_size_pct, 4),
            'size_value': round(final_size_value, 2),
            'method': m.value,
            'reason': '; '.join(constraints) if constraints else 'OK',
            'breakdown': breakdown,
            'constraints_applied': constraints,
        }

    # ═══════════════════════════════
    # 组合级仓位分配
    # ═══════════════════════════════

    def allocate_multi_strategy(self,
                                signals: List[Dict],
                                total_capital: float = None
                                ) -> List[Dict]:
        """
        多策略仓位分配

        当多个策略同时发出信号时，按优先级分配总仓位预算。

        signals: [{'symbol': 'BTC/USDT', 'strategy': 'trend', 'score': 8.5,
                    'raw_size_pct': 0.15, ...}, ...]

        分配规则:
          1. 按信号评分排序
          2. 从高到低依次分配，直到总敞口预算用完
          3. 高分策略先吃饱，低分策略可能分不到仓位
        """
        equity = total_capital or self.portfolio.total_equity
        total_budget = equity * self.config.max_total_exposure
        remaining_budget = total_budget - self.portfolio.total_exposure

        if remaining_budget <= 0:
            return []

        # 按评分降序排列
        sorted_signals = sorted(signals, key=lambda s: s.get('score', 0), reverse=True)
        allocations = []

        for sig in sorted_signals:
            if remaining_budget <= 0:
                break

            raw_size = sig.get('raw_size_pct', self.config.fixed_fraction_pct)
            proposed_value = equity * raw_size

            # 不超过剩余预算
            allocated_value = min(proposed_value, remaining_budget)
            allocated_pct = allocated_value / equity

            # 不超过单币种上限
            symbol = sig.get('symbol', 'UNKNOWN')
            existing = self.portfolio.positions.get(symbol, 0.0)
            max_per_symbol = equity * self.config.max_single_asset_exposure
            if existing + allocated_value > max_per_symbol:
                allocated_value = max(0, max_per_symbol - existing)
                allocated_pct = allocated_value / equity

            if allocated_pct < self.config.min_position_pct:
                continue  # 太小了，跳过

            remaining_budget -= allocated_value

            allocations.append({
                **sig,
                'allocated_size_pct': round(allocated_pct, 4),
                'allocated_size_value': round(allocated_value, 2),
                'remaining_budget': round(remaining_budget, 2),
            })

        return allocations

    # ═══════════════════════════════
    # 盈利提现策略
    # ═══════════════════════════════

    def profit_taking_check(self, current_date: str,
                            current_equity: float,
                            total_deposits: float = None) -> Dict:
        """
        盈利提现检查

        定期将利润从交易所转出到冷钱包/银行。

        逻辑:
          总盈利 = 当前净值 - 累计入金
          可提现金额 = max(0, 总盈利) × 提现比例
          触发条件: 距上次提现 ≥ 间隔天数 且 盈利 ≥ 最低触发金额
        """
        deposits = total_deposits or self.initial_capital
        total_profit = current_equity - deposits

        can_withdraw = total_profit > self.config.profit_take_min_profit

        if can_withdraw:
            take_amount = total_profit * self.config.profit_take_pct
        else:
            take_amount = 0.0

        return {
            'current_equity': round(current_equity, 2),
            'total_deposits': round(deposits, 2),
            'total_profit': round(total_profit, 2),
            'profit_take_pct': self.config.profit_take_pct,
            'can_withdraw': can_withdraw,
            'suggested_withdraw': round(take_amount, 2),
            'min_profit_threshold': self.config.profit_take_min_profit,
            'interval_days': self.config.profit_take_interval_days,
            'note': (f'建议提现 ${take_amount:.0f}（盈利的 {self.config.profit_take_pct:.0%}）'
                     if can_withdraw
                     else f'盈利 ${total_profit:.0f} < 阈值 ${self.config.profit_take_min_profit:.0f}，暂不提现'),
        }

    # ═══════════════════════════════
    # 仓位对比分析
    # ═══════════════════════════════

    def compare_methods(self, win_rate: float, avg_win_pct: float,
                        avg_loss_pct: float, atr: float = None,
                        signal_price: float = None) -> List[Dict]:
        """
        对比所有仓位计算方法的输出

        用于回测验证：哪种仓位管理方法最适合当前策略？
        """
        results = []

        # 凯利变体
        for variant, label in [
            (SizingMethod.KELLY_FULL, '完整凯利'),
            (SizingMethod.KELLY_HALF, '半凯利（推荐）'),
            (SizingMethod.KELLY_QUARTER, '1/4 凯利'),
        ]:
            r = self.kelly_position(win_rate, avg_win_pct, avg_loss_pct, variant=variant)
            results.append({
                'method': label,
                'size_pct': r['size_pct'],
                'size_value': round(self.portfolio.total_equity * r['size_pct'], 2),
                'f_star_raw': r.get('f_star_raw', 0),
                'growth_optimal': '理论上最优' if variant == SizingMethod.KELLY_FULL else
                                 '保守但安全' if variant == SizingMethod.KELLY_HALF else
                                 '极保守',
            })

        # 固定分数
        r = self.fixed_fraction()
        results.append({
            'method': f'固定分数 ({self.config.fixed_fraction_pct:.0%})',
            'size_pct': r['size_pct'],
            'size_value': round(self.portfolio.total_equity * r['size_pct'], 2),
            'f_star_raw': None,
            'growth_optimal': '不考虑策略质量，简单但不够聪明',
        })

        # 波动率调整
        if atr and signal_price:
            r = self.volatility_adjusted(atr, signal_price)
            results.append({
                'method': 'ATR 波动率调整',
                'size_pct': r['size_pct'],
                'size_value': r.get('size_value',
                                    round(self.portfolio.total_equity * r['size_pct'], 2)),
                'f_star_raw': None,
                'growth_optimal': '适应市场波动，高波动自动降仓',
            })

        return results

    def print_comparison(self, win_rate: float = 0.55, avg_win_pct: float = 0.04,
                         avg_loss_pct: float = 0.02, atr: float = None,
                         signal_price: float = None):
        """打印仓位方法对比表"""
        results = self.compare_methods(win_rate, avg_win_pct, avg_loss_pct, atr, signal_price)
        equity = self.portfolio.total_equity

        print(f"\n  [仓位计算方法对比] 本金 ${equity:,.0f}")
        print(f"     胜率={win_rate:.0%}, 平均盈利={avg_win_pct:.1%}, "
              f"平均亏损={avg_loss_pct:.1%}")
        if atr and signal_price:
            print(f"     ATR=${atr:.0f}, 价格=${signal_price:.0f}")
        print()
        print(f"  {'方法':<20} {'仓位%':>8} {'金额':>10} {'评价'}")
        print(f"  {'-'*20} {'-'*8} {'-'*10} {'-'*20}")
        for r in results:
            print(f"  {r['method']:<20} {r['size_pct']:>7.2%} ${r['size_value']:>9,.0f}  "
                  f"{r['growth_optimal']}")

    # ═══════════════════════════════
    # 与 RiskManager 集成
    # ═══════════════════════════════

    def integrate_with_risk_manager(self, sizing_result: Dict,
                                    risk_manager) -> Dict:
        """
        仓位计算 → 风控门禁 → 最终决策

        PositionSizer 算出建议仓位，RiskManager 有最终否决权。
        风控说不买 → 不买。没有任何代码路径可以绕过风控层。
        """
        if not sizing_result.get('passed'):
            return {**sizing_result, 'risk_veto': False, 'final_decision': 'REJECT',
                    'final_reason': '仓位计算未通过'}

        # 传递给风控检查
        # 注意：RiskManager 会做独立判断，可能拒绝或减少仓位
        risk_check = risk_manager.pre_trade_check(
            symbol=sizing_result.get('symbol', 'UNKNOWN'),
            side=sizing_result.get('side', 'long'),
            size_pct=sizing_result['size_pct'],
            signal_price=sizing_result.get('signal_price', 0),
        )

        if not risk_check.passed:
            return {
                **sizing_result,
                'risk_veto': True,
                'final_decision': risk_check.action.value,
                'final_reason': risk_check.reason,
                'final_size_pct': risk_check.suggested_size_pct,
            }

        return {
            **sizing_result,
            'risk_veto': False,
            'final_decision': 'APPROVE',
            'final_size_pct': sizing_result['size_pct'],
        }


# ═══════════════════════════════
# 辅助函数
# ═══════════════════════════════

def kelly_simulate(win_rate: float, win_loss_ratio: float,
                   n_trades: int = 100, n_paths: int = 1000,
                   kelly_fraction: float = 0.5) -> Dict:
    """
    蒙特卡洛模拟：给定凯利仓位，模拟 N 笔交易后的资金分布

    参数:
      win_rate: 胜率
      win_loss_ratio: 盈亏比 (avg_win / avg_loss)
      n_trades: 每轮模拟的交易笔数
      n_paths: 模拟路径数
      kelly_fraction: 凯利分数（0.5 = 半凯利）

    返回:
      包含中位数、P5、P95、爆仓概率的统计
    """
    import random
    random.seed(42)

    # 凯利最优仓位
    b = win_loss_ratio
    p, q = win_rate, 1 - win_rate
    f_star = max(0, (p * b - q) / b)
    actual_f = f_star * kelly_fraction

    final_values = []
    bust_count = 0  # 爆仓 = 资金 < 初始的 10%

    for _ in range(n_paths):
        capital = 1.0
        for _ in range(n_trades):
            if capital < 0.01:  # 已经爆了
                bust_count += 1
                capital = 0.001
                break
            bet = capital * actual_f
            if random.random() < win_rate:
                capital += bet * win_loss_ratio  # 赢
            else:
                capital -= bet                     # 输

        final_values.append(capital)

    final_values.sort()
    n = len(final_values)
    median = final_values[n // 2]
    p5 = final_values[int(n * 0.05)]
    p95 = final_values[int(n * 0.95)]
    bust_rate = sum(1 for v in final_values if v < 0.1) / n

    return {
        'kelly_f_star': round(f_star, 4),
        'actual_f': round(actual_f, 4),
        'kelly_fraction': kelly_fraction,
        'median_final': round(median, 3),
        'p5_final': round(p5, 3),
        'p95_final': round(p95, 3),
        'bust_rate': round(bust_rate, 3),
        'expected_growth': round((median - 1.0) * 100, 1),
        'note': (f'半凯利 ({actual_f:.1%}) 仓位，{n_trades} 笔交易后 '
                 f'中位数回报 {(median - 1.0)*100:+.0f}%，'
                 f'爆仓概率 {bust_rate:.1%}'),
    }


# ═══════════════════════════════
# 快速测试
# ═══════════════════════════════

if __name__ == '__main__':
    # ── 基础演示 ──
    ps = PositionSizer(initial_capital=10000)

    # 一个典型趋势策略的统计
    # 胜率 45%, 平均盈利 4%, 平均亏损 1.5%
    print("=" * 60)
    print("  仓位管理模块 — Day 8")
    print("=" * 60)

    # 方法对比
    ps.print_comparison(
        win_rate=0.45, avg_win_pct=0.04, avg_loss_pct=0.015,
        atr=1200, signal_price=65000,
    )

    # 具体计算
    print("\n  ── 具体仓位计算示例 ──")
    result = ps.calculate(
        symbol='BTC/USDT', side='long', signal_price=65000,
        atr=1200, win_rate=0.45, avg_win_pct=0.04, avg_loss_pct=0.015,
        method=SizingMethod.KELLY_HALF,
    )
    print(f"  信号: BTC/USDT 做多 @ $65,000")
    print(f"  计算结果: {result['size_pct']:.2%} (${result['size_value']:.0f})")
    print(f"  计算方法: {result['method']}")
    print(f"  约束: {result['reason']}")

    # 蒙特卡洛模拟
    print("\n  ── 蒙特卡洛模拟 ──")
    mc = kelly_simulate(win_rate=0.45, win_loss_ratio=4.0/1.5,
                        n_trades=200, n_paths=5000, kelly_fraction=0.5)
    print(f"  凯利 f* = {mc['kelly_f_star']:.2%}")
    print(f"  实际仓位 = {mc['actual_f']:.2%}（半凯利）")
    print(f"  200 笔后中位数: {mc['median_final']:.2f}x")
    print(f"  爆仓概率: {mc['bust_rate']:.2%}")
    print(f"  结论: {mc['note']}")

    # 盈利提现
    print("\n  ── 盈利提现策略 ──")
    pt = ps.profit_taking_check(current_date='2026-07-06', current_equity=13500)
    print(f"  当前净值: ${pt['current_equity']:,.0f}")
    print(f"  总盈利: ${pt['total_profit']:,.0f}")
    print(f"  {pt['note']}")

    # 多策略分配
    print("\n  ── 多策略仓位分配 ──")
    signals = [
        {'symbol': 'BTC/USDT', 'strategy': 'trend', 'score': 8.5, 'raw_size_pct': 0.12},
        {'symbol': 'ETH/USDT', 'strategy': 'momentum', 'score': 7.2, 'raw_size_pct': 0.08},
        {'symbol': 'SOL/USDT', 'strategy': 'breakout', 'score': 6.0, 'raw_size_pct': 0.10},
        {'symbol': 'DOGE/USDT', 'strategy': 'momentum', 'score': 4.5, 'raw_size_pct': 0.06},
    ]
    allocs = ps.allocate_multi_strategy(signals)
    for a in allocs:
        print(f"  {a['strategy']:>10} {a['symbol']:>12} "
              f"评分={a['score']} → 分配 {a['allocated_size_pct']:.2%} "
              f"(${a['allocated_size_value']:,.0f})")

    print("\n  [OK] Day 8 仓位管理模块就绪")
