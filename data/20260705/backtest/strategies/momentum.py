"""
动量策略 — 双层时间框架动量确认（多空双向）

赚钱逻辑：
  做多：快速动量 > 0 AND 慢速动量 > 0 → 上涨趋势确立，顺势做多
  做空：快速动量 < 0 AND 慢速动量 < 0 → 下跌趋势确立，顺势做空
  双层过滤避免单一时间框架的假信号。

适合市场环境：趋势市
亏损市场环境：震荡市（动量频繁反转）


参数（3 个）：
  fast_momentum: 快速动量窗口（日线推荐 10，小时线推荐 20）
  slow_momentum: 慢速动量窗口（日线推荐 30，小时线推荐 50）
  atr_stop: ATR 止损倍数（默认 2.5）
"""

import numpy as np
import pandas as pd
from ..strategy_base import BaseStrategy


class MomentumStrategy(BaseStrategy):
    """双层时间框架动量 — 多空双向"""

    def __init__(self, fast_momentum=10, slow_momentum=30, atr_stop=3.0):
        super().__init__(name=f"动量策略(MOM_{fast_momentum}/{slow_momentum})")
        self.fast_momentum = fast_momentum
        self.slow_momentum = slow_momentum
        self.atr_stop = atr_stop

        self._fast_mom = None
        self._slow_mom = None
        self._atr = None
        self._adx = None

    def precompute(self, df):
        """预计算动量指标 + ADX"""
        close = df['close']
        high, low = df['high'], df['low']

        self._fast_mom = (close / close.shift(self.fast_momentum) - 1).shift(1)
        self._slow_mom = (close / close.shift(self.slow_momentum) - 1).shift(1)

        # ATR
        tr1 = high - low
        tr2 = (high - close.shift(1)).abs()
        tr3 = (low - close.shift(1)).abs()
        tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
        self._atr = tr.ewm(span=14, adjust=False).mean().shift(1)

        # ADX
        atr14 = tr.ewm(span=14, adjust=False).mean()
        up_move = high.diff()
        down_move = -low.diff()
        plus_dm = pd.Series(0.0, index=df.index)
        minus_dm = pd.Series(0.0, index=df.index)
        plus_dm[(up_move > down_move) & (up_move > 0)] = up_move
        minus_dm[(down_move > up_move) & (down_move > 0)] = down_move
        plus_di = 100 * (plus_dm.ewm(span=14, adjust=False).mean() / atr14)
        minus_di = 100 * (minus_dm.ewm(span=14, adjust=False).mean() / atr14)
        dx = 100 * (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, np.nan)
        self._adx = dx.ewm(span=14, adjust=False).mean().shift(1)

    def _momentum_values(self, i):
        """获取当前 K 线的动量值"""
        if self._fast_mom is None or self._slow_mom is None:
            return None
        if i < self.slow_momentum:
            return None
        fast = self._fast_mom.iloc[i]
        slow = self._slow_mom.iloc[i]
        if pd.isna(fast) or pd.isna(slow):
            return None
        return fast, slow

    def check_long_entry(self, bar, i):
        """双层动量均为正 + ADX 过滤 → 做多"""
        if self.ENABLE_ADX_FILTER and not self._adx_trending(i):
            return False
        vals = self._momentum_values(i)
        if vals is None:
            return False
        fast, slow = vals
        return fast > 0 and slow > 0 and fast > slow

    def check_short_entry(self, bar, i):
        """双层动量均为负 + ADX 过滤 → 做空"""
        if self.ENABLE_ADX_FILTER and not self._adx_trending(i):
            return False
        vals = self._momentum_values(i)
        if vals is None:
            return False
        fast, slow = vals
        return fast < 0 and slow < 0 and fast < slow

    def _adx_trending(self, i):
        """ADX 是否显示趋势市"""
        if self._adx is None:
            return True
        adx_val = self._adx.iloc[i]
        return not pd.isna(adx_val) and adx_val >= self.ADX_TRENDING_THRESHOLD

    def check_exit(self, bar, i):
        """动量方向逆转 → 平仓"""
        vals = self._momentum_values(i)
        if vals is None:
            return False
        fast, slow = vals
        if self.position is None:
            return False
        if self.position.side == 'long':
            return fast < 0 or slow < 0
        else:
            return fast > 0 or slow > 0

    def _atr_value(self, i):
        if self._atr is None:
            return None
        val = self._atr.iloc[i]
        return val if not pd.isna(val) and val > 0 else None

    def get_atr(self, bar, i):
        """返回当前 ATR（供基类移动止盈使用）"""
        return self._atr_value(i)

    def get_adx(self, bar, i):
        """返回当前 ADX（供基类震荡市过滤使用）"""
        if self._adx is None:
            return 100
        val = self._adx.iloc[i]
        return val if not pd.isna(val) else 100

    def get_stop_loss(self, bar, i, side):
        """ATR 动态止损"""
        atr_val = self._atr_value(i)
        if atr_val is None:
            return None
        if side == 'long':
            return bar['close'] - atr_val * self.atr_stop
        else:
            return bar['close'] + atr_val * self.atr_stop
