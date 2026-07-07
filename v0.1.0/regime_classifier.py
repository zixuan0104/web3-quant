"""
市场环境分类器 — Day 11 核心模块

把市场切成 5 种环境，每种环境对应不同的仓位/止损策略。

环境分类：
  TRENDING_UP    — 强上升趋势 (ADX > 25, 价格 > MA50/200)
  TRENDING_DOWN  — 强下降趋势 (ADX > 25, 价格 < MA50/200)
  RANGING        — 震荡 (ADX < 20, 低波动率)
  VOLATILE       — 高波动 (ATR 百分位 > 80)
  TRANSITIONING  — 过渡期 (不符合以上任何一类)

自动动作：
  趋势环境 → 趋势/动量策略满仓, 均值回归减仓
  震荡环境 → 均值回归满仓, 趋势/动量减仓
  高波动   → 所有策略减仓 + 止损收紧
  过渡期   → 不开新仓, 持有观望

新增数据源（框架就绪，数据接入 Day 12+）：
  - 合约持仓量 OI + 资金费率
  - BTC 主导地位 BTC.D
  - 恐惧贪婪指数

用法：
  from regime_classifier import RegimeClassifier, MarketRegime

  rc = RegimeClassifier()
  rc.update(bar)  # 每根 K 线更新

  regime = rc.classify()           # → MarketRegime.TRENDING_UP
  multiplier = rc.get_strategy_multiplier('trend')  # → 1.0
  dashboard = rc.dashboard()       # → dict 用于展示
"""

from dataclasses import dataclass, field
from typing import Optional, Dict, List, Tuple, TYPE_CHECKING
from enum import Enum
from collections import deque
import math

if TYPE_CHECKING:
    import pandas as pd


# ═══════════════════════════════
# 数据类型
# ═══════════════════════════════

class MarketRegime(Enum):
    TRENDING_UP = "trending_up"        # 上升趋势
    TRENDING_DOWN = "trending_down"    # 下降趋势
    RANGING = "ranging"                # 震荡
    VOLATILE = "volatile"              # 高波动
    TRANSITIONING = "transitioning"    # 过渡期


@dataclass
class RegimeConfig:
    """环境分类器配置"""
    # ── ADX 阈值 ──
    adx_trending_threshold: float = 25.0    # ADX > 此值 = 趋势市
    adx_weak_threshold: float = 20.0        # ADX < 此值 = 震荡市基础条件

    # ── 波动率 ──
    atr_percentile_volatile: float = 80.0   # ATR 百分位 > 此值 = 高波动
    atr_lookback: int = 100                 # ATR 百分位计算窗口

    # ── 均线 ──
    ma_short: int = 50                      # 短期趋势方向
    ma_long: int = 200                      # 长期趋势方向

    # ── 布林带 ──
    bb_period: int = 20
    bb_std: float = 2.0
    bb_width_percentile: float = 80.0       # 带宽百分位 > 此值 = 波动扩张

    # ── 趋势一致性 ──
    trend_consensus_window: int = 10         # 连续 N 根 K 线方向一致才确认趋势

    # ── 最小数据要求 ──
    min_bars_for_classify: int = 50

    # ── 宏观流动性评分权重 (Pickle Cat 策略内化) ──
    macro_liquidity_weight: float = 0.25    # 宏观流动性在环境分类中的权重
    macro_score_bullish_threshold: float = 60.0  # > 此值 = 流动性宽松，偏多
    macro_score_bearish_threshold: float = 40.0  # < 此值 = 流动性紧缩，偏空

    # ── 策略调整系数 ──
    # 每种策略类型 × 每种环境 = 仓位倍数
    strategy_multipliers: Dict[str, Dict[str, float]] = field(default_factory=lambda: {
        'trend': {
            'trending_up': 1.0,
            'trending_down': 1.0,
            'ranging': 0.3,
            'volatile': 0.5,
            'transitioning': 0.0,
        },
        'momentum': {
            'trending_up': 1.0,
            'trending_down': 0.7,    # 做空动量在下行趋势中效果不如做多
            'ranging': 0.3,
            'volatile': 0.5,
            'transitioning': 0.0,
        },
        'breakout': {
            'trending_up': 0.8,
            'trending_down': 0.6,
            'ranging': 0.5,           # 假突破风险高
            'volatile': 0.4,          # 突破在波动市容易被扫止损
            'transitioning': 0.0,
        },
        'mean_reversion': {
            'trending_up': 0.3,       # 趋势市均值回归容易被碾压
            'trending_down': 0.3,
            'ranging': 1.0,           # 震荡市是均值回归的主场
            'volatile': 0.5,
            'transitioning': 0.3,
        },
        'funding_arb': {
            'trending_up': 1.0,
            'trending_down': 1.0,
            'ranging': 1.0,
            'volatile': 0.5,          # 高波动时费率不稳定
            'transitioning': 0.5,
        },
    })


@dataclass
class MacroLiquidityData:
    """宏观流动性数据 (Pickle Cat 策略内化)

    数据源（全部免费）：
      - DXY: TradingView / FRED (美元指数，下跌 = 利好风险资产)
      - 实际利率: FRED TIPS yield (下降 = 流动性宽松)
      - 美联储资产负债表: FRED WALCL (扩张 = 放水)
      - USDT 市值: CoinGecko API (增长 = 资金进场)

    更新频率: 日级即可，不需要实时
    """
    dxy: Optional[float] = None                # 美元指数 (e.g. 104.5)
    dxy_change_30d_pct: Optional[float] = None  # 美元指数 30 日变化率
    real_rate_10y: Optional[float] = None       # 10 年期 TIPS 实际利率
    real_rate_change_30d: Optional[float] = None
    fed_balance_sheet_t: Optional[float] = None  # 美联储总资产（万亿美元）
    fed_balance_change_30d_pct: Optional[float] = None
    usdt_market_cap_b: Optional[float] = None    # USDT 市值（十亿美元）
    usdt_mcap_change_30d_pct: Optional[float] = None
    last_updated: Optional[str] = None           # 数据更新时间


# ═══════════════════════════════
# 市场环境分类器
# ═══════════════════════════════

class RegimeClassifier:
    """基于滚动窗口的市场环境实时分类器"""

    def __init__(self, config: Optional[RegimeConfig] = None):
        self.config = config or RegimeConfig()

        # ── 滚动窗口数据 ──
        self._close: deque = deque(maxlen=self.config.ma_long + 100)
        self._high: deque = deque(maxlen=self.config.ma_long + 100)
        self._low: deque = deque(maxlen=self.config.ma_long + 100)
        self._volume: deque = deque(maxlen=self.config.ma_long + 100)
        self._timestamps: deque = deque(maxlen=self.config.ma_long + 100)

        # ── 衍生指标缓存 ──
        self._atr_values: deque = deque(maxlen=self.config.atr_lookback)
        self._bb_widths: deque = deque(maxlen=self.config.atr_lookback)
        self._direction_log: deque = deque(maxlen=self.config.trend_consensus_window)

        # ── 当前状态 ──
        self._current_regime: MarketRegime = MarketRegime.TRANSITIONING
        self._regime_duration: int = 0          # 当前环境持续 K 线数
        self._regime_history: deque = deque(maxlen=100)  # 最近 100 根的环境标签

        # ── 外部数据（Day 12+ 接入）──
        self._oi_data: deque = deque(maxlen=100)       # 合约持仓量
        self._funding_rate: deque = deque(maxlen=100)  # 资金费率
        self._btc_dominance: float = None              # BTC.D
        self._fear_greed: int = None                   # 恐惧贪婪指数 (0-100)

        # ── 宏观流动性数据 (Pickle Cat 策略内化) ──
        self._macro_data = MacroLiquidityData()

        # ── 诊断 ──
        self._scores: Dict[str, float] = {}   # 每种环境的得分
        self._macro_score: float = 50.0       # 宏观流动性评分 (0-100)
        self._n_updates: int = 0

    # ═══════════════════════════════
    # 数据更新
    # ═══════════════════════════════

    def update(self, bar: dict) -> None:
        """
        喂入一根新 K 线（带时间戳的 dict）

        bar 需要包含: close, high, low, volume
        可选: open, timestamp
        """
        self._close.append(bar['close'])
        self._high.append(bar['high'])
        self._low.append(bar['low'])
        self._volume.append(bar.get('volume', 0))
        self._timestamps.append(bar.get('timestamp', self._n_updates))
        self._n_updates += 1

        # 更新衍生指标
        self._update_atr()
        self._update_bollinger()
        self._update_direction()

        # 重新分类
        new_regime = self._classify()
        if new_regime == self._current_regime:
            self._regime_duration += 1
        else:
            self._current_regime = new_regime
            self._regime_duration = 0
        self._regime_history.append(self._current_regime)

    def update_external(self, oi: float = None, funding_rate: float = None,
                        btc_dominance: float = None, fear_greed: int = None):
        """更新外部数据源（OI/资金费率/BTC.D/恐惧贪婪）"""
        if oi is not None:
            self._oi_data.append(oi)
        if funding_rate is not None:
            self._funding_rate.append(funding_rate)
        if btc_dominance is not None:
            self._btc_dominance = btc_dominance
        if fear_greed is not None:
            self._fear_greed = fear_greed

    # ═══════════════════════════════
    # 衍生指标计算
    # ═══════════════════════════════

    def _update_atr(self) -> None:
        """计算最新一根 K 线的 ATR(14)"""
        if len(self._close) < 3:
            return
        high, low = self._high[-1], self._low[-1]
        prev_close = self._close[-2]
        tr = max(high - low, abs(high - prev_close), abs(low - prev_close))
        # 简单平滑
        if not self._atr_values:
            self._atr_values.append(tr)
        else:
            prev_atr = self._atr_values[-1]
            atr = (prev_atr * 13 + tr) / 14
            self._atr_values.append(atr)

    def _update_bollinger(self) -> None:
        """计算最新布林带宽度"""
        if len(self._close) < self.config.bb_period:
            return
        window = list(self._close)[-self.config.bb_period:]
        n = len(window)
        mean = sum(window) / n
        var = sum((x - mean) ** 2 for x in window) / n
        std = var ** 0.5
        upper = mean + self.config.bb_std * std
        lower = mean - self.config.bb_std * std
        width = (upper - lower) / mean if mean > 0 else 0
        self._bb_widths.append(width)

    def _update_direction(self) -> None:
        """记录最近一根 K 线的方向"""
        if len(self._close) < 2:
            return
        change = self._close[-1] - self._close[-2]
        if change > 0:
            self._direction_log.append(1)
        elif change < 0:
            self._direction_log.append(-1)
        else:
            self._direction_log.append(0)

    # ═══════════════════════════════
    # 分类逻辑
    # ═══════════════════════════════

    def _classify(self) -> MarketRegime:
        """综合多维度信号判定当前市场环境"""
        if len(self._close) < self.config.min_bars_for_classify:
            return MarketRegime.TRANSITIONING

        # ── 计算各维度得分 ──
        adx = self._compute_adx()
        atr_pct = self._compute_atr_percentile()
        ma50 = self._compute_sma(self.config.ma_short)
        ma200 = self._compute_sma(self.config.ma_long)
        bb_width_pct = self._compute_bb_width_percentile()
        consensus = self._compute_trend_consensus()
        current_price = self._close[-1]

        # 重置得分
        scores = {
            'trending_up': 0.0,
            'trending_down': 0.0,
            'ranging': 0.0,
            'volatile': 0.0,
        }

        # ── 1. ADX 信号 ──
        if adx is not None:
            if adx >= self.config.adx_trending_threshold:
                # 趋势市：方向由价格 vs MA 决定
                if ma50 is not None and current_price > ma50:
                    scores['trending_up'] += 30
                elif ma50 is not None and current_price < ma50:
                    scores['trending_down'] += 30
                else:
                    scores['trending_up'] += 15
                    scores['trending_down'] += 15
            elif adx < self.config.adx_weak_threshold:
                scores['ranging'] += 25

        # ── 2. 波动率信号 ──
        if atr_pct is not None:
            if atr_pct >= self.config.atr_percentile_volatile:
                scores['volatile'] += 35
            elif atr_pct <= 30:
                scores['ranging'] += 15

        # ── 3. 均线排列 ──
        if ma50 is not None and ma200 is not None and current_price is not None:
            if current_price > ma50 > ma200:
                scores['trending_up'] += 25
            elif current_price < ma50 < ma200:
                scores['trending_down'] += 25
            elif abs(ma50 - ma200) / ma200 < 0.02:
                # MA 缠绕 = 震荡
                scores['ranging'] += 20

        # ── 4. 布林带宽度 ──
        if bb_width_pct is not None:
            if bb_width_pct >= self.config.bb_width_percentile:
                scores['volatile'] += 20
            elif bb_width_pct <= 20:
                scores['ranging'] += 15

        # ── 5. 趋势一致性 ──
        if consensus is not None:
            if consensus >= 0.7:
                scores['trending_up'] += 15
            elif consensus <= -0.7:
                scores['trending_down'] += 15
            elif -0.3 <= consensus <= 0.3:
                scores['ranging'] += 10

        # ── 6. 外部数据调整（如果有）──
        self._apply_external_signals(scores)

        self._scores = scores

        # ── 最高分当选 ──
        best_regime = max(scores, key=scores.get)
        if scores[best_regime] < 20:
            return MarketRegime.TRANSITIONING

        return MarketRegime(best_regime)

    def _apply_external_signals(self, scores: Dict[str, float]) -> None:
        """用外部数据微调得分（O/I、BTC.D、恐惧贪婪）"""
        # 恐惧贪婪极端值
        if self._fear_greed is not None:
            if self._fear_greed <= 25:  # 极度恐惧
                scores['volatile'] += 15
                scores['trending_down'] += 10
            elif self._fear_greed >= 75:  # 极度贪婪
                scores['trending_up'] += 5

        # 资金费率极端值 → 波动增大
        if self._funding_rate:
            recent_fr = list(self._funding_rate)[-5:]
            avg_fr = sum(abs(x) for x in recent_fr) / len(recent_fr)
            if avg_fr > 0.001:  # 0.1%+ = 市场过热
                scores['volatile'] += 10
            if avg_fr > 0.003:  # 0.3%+ = 极度投机
                scores['volatile'] += 10

        # BTC.D 下降 → 山寨季 → 趋势增强
        if self._btc_dominance is not None and len(self._close) >= 30:
            # 简单判断：BTC.D 是否在下降
            pass  # 保留扩展点

        # ── 宏观流动性调整 (Pickle Cat 策略内化) ──
        macro_score = self._macro_liquidity_score()
        if macro_score is not None:
            weight = self.config.macro_liquidity_weight
            if macro_score >= self.config.macro_score_bullish_threshold:
                # 流动性宽松 → 利好风险资产，趋势延续概率高
                boost = (macro_score - 50) * weight
                scores['trending_up'] += boost * 0.6
                scores['trending_down'] -= boost * 0.2  # 宽松环境下行趋势减弱
                scores['volatile'] -= boost * 0.1        # 宽松环境波动降低
            elif macro_score <= self.config.macro_score_bearish_threshold:
                # 流动性紧缩 → 避险，趋势容易被打断
                penalty = (50 - macro_score) * weight
                scores['trending_down'] += penalty * 0.4
                scores['volatile'] += penalty * 0.3       # 紧缩环境波动加大
                scores['trending_up'] -= penalty * 0.2

    # ═══════════════════════════════
    # 宏观流动性评分 (Pickle Cat 策略内化)
    # ═══════════════════════════════

    def update_macro_liquidity(self, dxy: float = None,
                               real_rate_10y: float = None,
                               fed_balance_sheet_t: float = None,
                               usdt_market_cap_b: float = None) -> None:
        """
        更新宏观流动性数据

        数据源（全部免费，日级更新即可）：
          - DXY (美元指数): TradingView 或 FRED DX-Y.NYB
          - 实际利率: FRED DGS10 -  breakeven inflation rate
          - 美联储资产负债表: FRED WALCL (周更)
          - USDT 市值: CoinGecko API /api/v3/coins/tether

        所有参数都是可选的一一可以只更新有数据的维度。
        """
        if dxy is not None:
            old_dxy = self._macro_data.dxy
            self._macro_data.dxy = dxy
            if old_dxy is not None and old_dxy > 0:
                self._macro_data.dxy_change_30d_pct = (dxy - old_dxy) / old_dxy * 100

        if real_rate_10y is not None:
            old_rate = self._macro_data.real_rate_10y
            self._macro_data.real_rate_10y = real_rate_10y
            if old_rate is not None:
                self._macro_data.real_rate_change_30d = real_rate_10y - old_rate

        if fed_balance_sheet_t is not None:
            old_fed = self._macro_data.fed_balance_sheet_t
            self._macro_data.fed_balance_sheet_t = fed_balance_sheet_t
            if old_fed is not None and old_fed > 0:
                self._macro_data.fed_balance_change_30d_pct = (
                    (fed_balance_sheet_t - old_fed) / old_fed * 100)

        if usdt_market_cap_b is not None:
            old_usdt = self._macro_data.usdt_market_cap_b
            self._macro_data.usdt_market_cap_b = usdt_market_cap_b
            if old_usdt is not None and old_usdt > 0:
                self._macro_data.usdt_mcap_change_30d_pct = (
                    (usdt_market_cap_b - old_usdt) / old_usdt * 100)

        from datetime import datetime, timezone
        self._macro_data.last_updated = datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')

        # 重新计算宏观评分
        self._macro_score = self._macro_liquidity_score() or 50.0

    def _macro_liquidity_score(self) -> Optional[float]:
        """
        综合四个维度计算宏观流动性评分 (0-100)

        评分逻辑（每个维度 0-25 分）：
          DXY 下跌 → 利好风险资产 (+分)
          实际利率下降 → 流动性宽松 (+分)
          美联储扩表 → 放水 (+分)
          USDT 市值增长 → 资金进场 (+分)

        返回:
          > 60: 流动性宽松，利好风险资产
          40-60: 中性
          < 40: 流动性紧缩，避险
          None: 数据不足
        """
        data = self._macro_data
        score = 50.0  # 中性起点
        dimensions_used = 0

        # 维度 1: DXY 变化率 (美元跌 → 利好)
        if data.dxy is not None and data.dxy_change_30d_pct is not None:
            # DXY 跌 1% → +3 分, DXY 涨 1% → -3 分, 上限 ±12
            dxy_score = -data.dxy_change_30d_pct * 3
            dxy_score = max(-12, min(12, dxy_score))
            score += dxy_score
            dimensions_used += 1

        # 维度 2: 实际利率变化 (利率降 → 利好)
        if data.real_rate_10y is not None and data.real_rate_change_30d is not None:
            # 实际利率降 0.1% → +3 分, 升 0.1% → -3 分, 上限 ±12
            rate_score = -data.real_rate_change_30d * 30
            rate_score = max(-12, min(12, rate_score))
            score += rate_score
            dimensions_used += 1

        # 维度 3: 美联储资产负债表变化 (扩表 → 利好)
        if data.fed_balance_change_30d_pct is not None:
            # 扩表 1% → +5 分, 缩表 1% → -5 分, 上限 ±10
            fed_score = data.fed_balance_change_30d_pct * 5
            fed_score = max(-10, min(10, fed_score))
            score += fed_score
            dimensions_used += 1

        # 维度 4: USDT 市值变化 (增长 → 资金进场)
        if data.usdt_mcap_change_30d_pct is not None:
            # 市值涨 1% → +3 分, 跌 1% → -3 分, 上限 ±10
            usdt_score = data.usdt_mcap_change_30d_pct * 3
            usdt_score = max(-10, min(10, usdt_score))
            score += usdt_score
            dimensions_used += 1

        if dimensions_used == 0:
            return None  # 数据不足

        # 截断到 0-100
        return max(0.0, min(100.0, score))

    def get_macro_liquidity_score(self) -> Optional[float]:
        """获取当前宏观流动性评分"""
        if self._macro_data.last_updated is None:
            return None
        return round(self._macro_score, 1)

    def get_macro_regime(self) -> str:
        """获取宏观流动性环境描述"""
        score = self._macro_score
        if self._macro_data.last_updated is None:
            return 'no_data'
        if score >= 65:
            return 'liquidity_expanding'     # 流动性扩张 — 利好
        elif score >= 55:
            return 'liquidity_moderate'      # 温和 — 中性偏多
        elif score >= 45:
            return 'neutral'                 # 中性
        elif score >= 35:
            return 'liquidity_contracting'   # 流动性收缩 — 偏空
        else:
            return 'liquidity_crisis'        # 流动性危机 — 避险

    # ═══════════════════════════════
    # 指标计算
    # ═══════════════════════════════

    def _compute_adx(self) -> Optional[float]:
        """计算 ADX(14) — 简化版（只用方向一致性代替完整 DI 计算）"""
        if len(self._close) < 16:
            return None
        closes = list(self._close)
        # 简化 ADX：基于方向连续性的趋势强度
        direction_strength = 0
        for i in range(-14, 0):
            if closes[i] > closes[i - 1]:
                direction_strength += 1
            elif closes[i] < closes[i - 1]:
                direction_strength -= 1
        # 映射到 0-50 范围
        adx = abs(direction_strength) / 14 * 50
        return adx

    def _compute_atr_percentile(self) -> Optional[float]:
        """当前 ATR 在历史窗口中的百分位"""
        if len(self._atr_values) < 20:
            return None
        current_atr = self._atr_values[-1]
        sorted_atr = sorted(self._atr_values)
        rank = sum(1 for x in sorted_atr if x <= current_atr)
        return (rank / len(sorted_atr)) * 100

    def _compute_sma(self, period: int) -> Optional[float]:
        """简单移动平均"""
        if len(self._close) < period:
            return None
        window = list(self._close)[-period:]
        return sum(window) / len(window)

    def _compute_bb_width_percentile(self) -> Optional[float]:
        """布林带宽度在历史中的百分位"""
        if len(self._bb_widths) < 20:
            return None
        current = self._bb_widths[-1]
        sorted_w = sorted(self._bb_widths)
        rank = sum(1 for x in sorted_w if x <= current)
        return (rank / len(sorted_w)) * 100

    def _compute_trend_consensus(self) -> Optional[float]:
        """最近 N 根 K 线的方向一致度 (-1 ~ 1)"""
        if len(self._direction_log) < self.config.trend_consensus_window:
            return None
        recent = list(self._direction_log)[-self.config.trend_consensus_window:]
        return sum(recent) / len(recent)

    # ═══════════════════════════════
    # 对外接口
    # ═══════════════════════════════

    @property
    def regime(self) -> MarketRegime:
        """当前市场环境"""
        return self._current_regime

    @property
    def regime_duration(self) -> int:
        """当前环境已持续 K 线数"""
        return self._regime_duration

    def classify(self) -> MarketRegime:
        """返回当前分类（等同 .regime）"""
        return self._current_regime

    def get_strategy_multiplier(self, strategy_type: str) -> float:
        """
        获取指定策略类型在当前环境下的仓位调整系数

        strategy_type: 'trend' | 'momentum' | 'breakout' | 'mean_reversion' | 'funding_arb'
        返回: 0.0 ~ 1.0 的乘数
        """
        multipliers = self.config.strategy_multipliers.get(strategy_type)
        if multipliers is None:
            return 1.0  # 未知策略类型不动仓位
        regime_key = self._current_regime.value
        return multipliers.get(regime_key, 1.0)

    def get_stop_loss_adjustment(self) -> float:
        """
        根据当前环境返回止损收紧系数

        高波动 → 止损放宽 (1.3x，避免被噪音扫出)
        震荡   → 止损收紧 (0.8x，减少单笔亏损)

        返回: 止损距离的乘数
        """
        adjustments = {
            MarketRegime.TRENDING_UP: 1.0,
            MarketRegime.TRENDING_DOWN: 1.0,
            MarketRegime.RANGING: 0.8,
            MarketRegime.VOLATILE: 1.3,
            MarketRegime.TRANSITIONING: 0.6,  # 过渡期紧止损
        }
        return adjustments.get(self._current_regime, 1.0)

    def get_max_position_adjustment(self) -> float:
        """
        根据环境返回单笔最大仓位限制系数

        高波动 → 降低单笔上限

        返回: 单笔仓位上限的乘数
        """
        adjustments = {
            MarketRegime.TRENDING_UP: 1.0,
            MarketRegime.TRENDING_DOWN: 1.0,
            MarketRegime.RANGING: 0.8,
            MarketRegime.VOLATILE: 0.5,
            MarketRegime.TRANSITIONING: 0.3,
        }
        return adjustments.get(self._current_regime, 1.0)

    def should_open_new_position(self) -> bool:
        """过渡期和高波动极端时不建议开新仓"""
        if self._current_regime == MarketRegime.TRANSITIONING:
            return False
        return True

    def dashboard(self) -> dict:
        """
        生成当前环境诊断快照（用于文字 Dashboard）

        返回: dict 含 regime / scores / multipliers / adjustments
        """
        return {
            'regime': self._current_regime.value,
            'regime_duration': self._regime_duration,
            'scores': dict(self._scores),
            'multipliers': {
                st: self.get_strategy_multiplier(st)
                for st in ['trend', 'momentum', 'breakout', 'mean_reversion', 'funding_arb']
            },
            'stop_loss_adj': self.get_stop_loss_adjustment(),
            'max_position_adj': self.get_max_position_adjustment(),
            'can_open_new': self.should_open_new_position(),
            'external': {
                'btc_dominance': self._btc_dominance,
                'fear_greed': self._fear_greed,
                'funding_rate_avg': (
                    sum(abs(x) for x in list(self._funding_rate)[-5:]) / min(5, len(self._funding_rate))
                    if self._funding_rate else None
                ),
                'macro_liquidity': {
                    'score': self.get_macro_liquidity_score(),
                    'regime': self.get_macro_regime(),
                    'dxy': self._macro_data.dxy,
                    'real_rate_10y': self._macro_data.real_rate_10y,
                    'usdt_mcap_b': self._macro_data.usdt_market_cap_b,
                    'last_updated': self._macro_data.last_updated,
                },
            },
            'indicators': {
                'adx': self._compute_adx(),
                'atr_percentile': self._compute_atr_percentile(),
                'ma50': self._compute_sma(self.config.ma_short),
                'ma200': self._compute_sma(self.config.ma_long),
                'price': self._close[-1] if self._close else None,
                'bb_width_pct': self._compute_bb_width_percentile(),
                'trend_consensus': self._compute_trend_consensus(),
            },
        }

    def recent_regimes(self, n: int = 20) -> List[str]:
        """最近 n 根 K 线的环境序列"""
        history = list(self._regime_history)
        return [r.value for r in history[-n:]]

    def regime_stability(self, n: int = 20) -> float:
        """
        最近 n 根 K 线中环境切换频率
        返回: 0 = 极不稳定(每根都在切换), 1 = 完全稳定
        """
        if len(self._regime_history) < n:
            return 0.0
        recent = list(self._regime_history)[-n:]
        if len(recent) < 2:
            return 1.0
        switches = sum(1 for i in range(1, len(recent)) if recent[i] != recent[i - 1])
        return 1.0 - (switches / (len(recent) - 1))


# ═══════════════════════════════
# 辅助函数
# ═══════════════════════════════

def classify_from_df(df, config: Optional[RegimeConfig] = None) -> "pd.Series":
    """
    从 DataFrame 批量分类历史行情（用于回测）

    参数:
      df: 含 close/high/low/volume 的 DataFrame
      config: RegimeConfig 或 None（用默认值）

    返回: pd.Series，索引同 df，值为 MarketRegime
    """
    import pandas as pd
    rc = RegimeClassifier(config)
    regimes = []
    for i in range(len(df)):
        bar = {
            'close': float(df['close'].iloc[i]),
            'high': float(df['high'].iloc[i]),
            'low': float(df['low'].iloc[i]),
            'volume': float(df['volume'].iloc[i]) if 'volume' in df.columns else 0,
            'timestamp': df.index[i],
        }
        rc.update(bar)
        regimes.append(rc.regime)
    return pd.Series(regimes, index=df.index, name='market_regime')


def regime_distribution(regime_series) -> dict:
    """统计各环境占比"""
    counts = regime_series.value_counts()
    total = len(regime_series)
    return {
        r.value: {
            'count': int(counts.get(r, 0)),
            'pct': round(counts.get(r, 0) / total * 100, 1),
        }
        for r in MarketRegime
    }


# ═══════════════════════════════
# 自测
# ═══════════════════════════════

if __name__ == '__main__':
    import random
    import time

    print("=" * 60)
    print("  市场环境分类器 — 自测")
    print("=" * 60)

    rc = RegimeClassifier()

    # 模拟场景 1: 上升趋势
    print("\n[场景 1] 上升趋势 (价格从 60000 → 70000)")
    price = 60000.0
    for i in range(100):
        change = random.gauss(0.002, 0.015)  # 偏正 drift
        price *= (1 + change)
        bar = {
            'close': price,
            'high': price * (1 + abs(random.gauss(0, 0.005))),
            'low': price * (1 - abs(random.gauss(0, 0.005))),
            'volume': random.uniform(500, 2000),
        }
        rc.update(bar)
    print(f"  最终环境: {rc.regime.value}")
    print(f"  得分: {rc._scores}")
    print(f"  趋势乘数: {rc.get_strategy_multiplier('trend')}")
    print(f"  均值回归乘数: {rc.get_strategy_multiplier('mean_reversion')}")

    # 模拟场景 2: 震荡市
    print("\n[场景 2] 震荡市 (价格围绕 65000 波动)")
    rc2 = RegimeClassifier()
    price = 65000.0
    for i in range(100):
        change = random.gauss(0, 0.01)
        price *= (1 + change)
        price = max(62000, min(68000, price))
        bar = {
            'close': price,
            'high': price * (1 + random.uniform(0, 0.006)),
            'low': price * (1 - random.uniform(0, 0.006)),
            'volume': random.uniform(300, 1000),
        }
        rc2.update(bar)
    print(f"  最终环境: {rc2.regime.value}")
    print(f"  得分: {rc2._scores}")
    print(f"  震荡稳定性: {rc2.regime_stability():.2f}")

    # 模拟场景 3: 高波动 + 外部信号
    print("\n[场景 3] 高波动 + 极度恐惧")
    rc3 = RegimeClassifier()
    price = 65000.0
    for i in range(100):
        change = random.gauss(0, 0.04)  # 高波动
        price *= (1 + change)
        bar = {
            'close': price,
            'high': price * (1 + abs(random.gauss(0, 0.02))),
            'low': price * (1 - abs(random.gauss(0, 0.02))),
            'volume': random.uniform(2000, 10000),
        }
        rc3.update(bar)
    rc3.update_external(fear_greed=18, funding_rate=-0.002)
    # 再推几根让 funding_rate 积累
    for i in range(5):
        bar = {
            'close': price, 'high': price, 'low': price, 'volume': 1000,
        }
        rc3.update(bar)
    print(f"  最终环境: {rc3.regime.value}")
    print(f"  得分: {rc3._scores}")
    print(f"  止损调整: {rc3.get_stop_loss_adjustment()}x")
    print(f"  仓位上限调整: {rc3.get_max_position_adjustment()}x")
    print(f"  可以开新仓: {rc3.should_open_new_position()}")

    # Dashboard 快照
    print("\n[Dashboard] 场景 1:")
    dash = rc.dashboard()
    for k, v in dash.items():
        print(f"  {k}: {v}")

    print("\n" + "=" * 60)
    print("  自测完成")
    print("=" * 60)
