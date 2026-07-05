"""
趋势跟踪策略 — EMA 双均线交叉（多空双向）

赚钱逻辑：
  做多：快 EMA > 慢 EMA → 上升趋势确立，顺势做多
  做空：快 EMA < 慢 EMA → 下降趋势确立，顺势做空
  用 ATR 动态止损限制趋势反转时的亏损。

适合市场环境：强趋势市
亏损市场环境：震荡市（反复假突破）


参数（3 个）：
  fast_period: 快线周期（日线推荐 5，小时线推荐 20）
  slow_period: 慢线周期（日线推荐 20，小时线推荐 50）
  atr_stop: ATR 止损倍数（默认 2.0）
"""

import numpy as np
import pandas as pd
from ..strategy_base import BaseStrategy


class TrendStrategy(BaseStrategy):
    """EMA 双均线趋势跟踪 — 多空双向"""

    def __init__(self, fast_period=5, slow_period=20, atr_stop=3.0):
        super().__init__(name=f"趋势跟踪(EMA_{fast_period}/{slow_period})")
        self.fast_period = fast_period
        self.slow_period = slow_period
        self.atr_stop = atr_stop

        self._ema_fast = None
        self._ema_slow = None
        self._atr = None
        self._adx = None

    def precompute(self, df):
        """预计算指标 + ADX 震荡市过滤器"""
        close = df['close']
        high, low = df['high'], df['low']

        self._ema_fast = close.ewm(span=self.fast_period, adjust=False).mean().shift(1)
        self._ema_slow = close.ewm(span=self.slow_period, adjust=False).mean().shift(1)

        # ATR
        tr1 = high - low
        tr2 = (high - close.shift(1)).abs()
        tr3 = (low - close.shift(1)).abs()
        tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
        self._atr = tr.ewm(span=14, adjust=False).mean().shift(1)

        # ADX — 趋势强度指标（< 20 = 震荡市，> 25 = 趋势市）
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

    def _ema_values(self, i):
        """获取当前和前一根 K 线的 EMA 值"""
        if self._ema_fast is None or self._ema_slow is None:
            return None
        if i < self.slow_period:
            return None
        fast = self._ema_fast.iloc[i]
        slow = self._ema_slow.iloc[i]
        prev_fast = self._ema_fast.iloc[i - 1] if i > 0 else fast
        prev_slow = self._ema_slow.iloc[i - 1] if i > 0 else slow
        if pd.isna(fast) or pd.isna(slow):
            return None
        return fast, slow, prev_fast, prev_slow

    def check_long_entry(self, bar, i):
        """快线上穿慢线 + ADX 过滤 → 做多"""
        if self.ENABLE_ADX_FILTER and not self._adx_trending(i):
            return False
        vals = self._ema_values(i)
        if vals is None:
            return False
        fast, slow, prev_fast, prev_slow = vals
        return fast > slow and prev_fast <= prev_slow

    def check_short_entry(self, bar, i):
        """快线下穿慢线 + ADX 过滤 → 做空"""
        if self.ENABLE_ADX_FILTER and not self._adx_trending(i):
            return False
        vals = self._ema_values(i)
        if vals is None:
            return False
        fast, slow, prev_fast, prev_slow = vals
        return fast < slow and prev_fast >= prev_slow

    def _adx_trending(self, i):
        """ADX 是否显示趋势市"""
        if self._adx is None:
            return True  # 无 ADX 数据时默认允许交易
        adx_val = self._adx.iloc[i]
        return not pd.isna(adx_val) and adx_val >= self.ADX_TRENDING_THRESHOLD

    def check_exit(self, bar, i):
        """EMA 方向反转 → 平仓"""
        vals = self._ema_values(i)
        if vals is None:
            return False
        fast, slow, prev_fast, prev_slow = vals
        if self.position is None:
            return False
        if self.position.side == 'long':
            return fast < slow
        else:
            return fast > slow

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
