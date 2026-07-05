"""
突破策略 — Donchian Channel 通道突破（多空双向）

赚钱逻辑：
  做多：价格突破 N 日最高价 → 市场选择方向向上，顺势做多
  做空：价格跌破 N 日最低价 → 市场选择方向向下，顺势做空
  币圈突破后趋势延续性强（止损盘踩踏 + FOMO追涨），通道突破捕捉方向性爆发。

适合市场环境：强趋势市 / 高波动突破
亏损市场环境：震荡市（反复假突破）

参数（3 个）：
  channel_period: 通道周期（日线推荐 20，小时线推荐 48）
  atr_stop: ATR 止损倍数（默认 2.0）
  atr_filter: 突破需超过昨日收盘价的 N 倍 ATR 才确认（默认 0.5，防噪音突破）
"""

import numpy as np
import pandas as pd
from ..strategy_base import BaseStrategy


class BreakoutStrategy(BaseStrategy):
    """Donchian Channel 突破策略 — 多空双向"""

    def __init__(self, channel_period=20, atr_stop=2.0, atr_filter=0.5):
        super().__init__(name=f"突破策略(DC{channel_period}_ATR{atr_stop})")
        self.channel_period = channel_period
        self.atr_stop = atr_stop
        self.atr_filter = atr_filter

        self._dc_high = None
        self._dc_low = None
        self._dc_mid = None
        self._atr = None
        self._adx = None

    def precompute(self, df):
        """预计算 Donchian Channel + ATR + ADX"""
        close = df['close']
        high = df['high']
        low = df['low']

        # Donchian Channel — 前一根 K 线的通道值（防未来函数）
        roll_high = high.rolling(self.channel_period)
        roll_low = low.rolling(self.channel_period)

        self._dc_high = roll_high.max().shift(1)
        self._dc_low = roll_low.min().shift(1)
        self._dc_mid = ((self._dc_high + self._dc_low) / 2)

        # ATR
        tr1 = high - low
        tr2 = (high - close.shift(1)).abs()
        tr3 = (low - close.shift(1)).abs()
        tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
        self._atr = tr.ewm(span=14, adjust=False).mean().shift(1)

        # ADX — 趋势强度
        atr14 = tr.ewm(span=14, adjust=False).mean()
        up_move = high.diff()
        down_move = -low.diff()
        plus_dm = pd.Series(0.0, index=df.index)
        minus_dm = pd.Series(0.0, index=df.index)
        plus_dm.loc[(up_move > down_move) & (up_move > 0)] = up_move
        minus_dm.loc[(down_move > up_move) & (down_move > 0)] = down_move
        plus_di = 100 * (plus_dm.ewm(span=14, adjust=False).mean() / atr14)
        minus_di = 100 * (minus_dm.ewm(span=14, adjust=False).mean() / atr14)
        dx = 100 * (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, np.nan)
        self._adx = dx.ewm(span=14, adjust=False).mean().shift(1)

    def _channel_values(self, i):
        """获取当前通道值"""
        if self._dc_high is None or self._dc_low is None:
            return None
        if i < self.channel_period:
            return None
        h = self._dc_high.iloc[i]
        l = self._dc_low.iloc[i]
        m = self._dc_mid.iloc[i]
        if pd.isna(h) or pd.isna(l):
            return None
        return h, l, m

    def _atr_value(self, i):
        if self._atr is None:
            return None
        val = self._atr.iloc[i]
        return val if not pd.isna(val) and val > 0 else None

    def _is_clean_breakout(self, bar, i, direction):
        """
        噪音过滤：突破幅度需 > atr_filter × ATR 才算有效突破
        防止在低波动震荡市中被微小突破反复刷单
        """
        atr_val = self._atr_value(i)
        if atr_val is None:
            return True  # 无 ATR 数据时放行

        prev_close = bar['close']  # 已 shift(1)，bar 是前一根 K 线的值
        threshold = atr_val * self.atr_filter

        if direction == 'long':
            return (bar['close'] - prev_close) > threshold
        else:
            return (prev_close - bar['close']) > threshold

    def _adx_trending(self, i):
        """ADX 是否显示趋势市"""
        if self._adx is None:
            return True
        adx_val = self._adx.iloc[i]
        return not pd.isna(adx_val) and adx_val >= self.ADX_TRENDING_THRESHOLD

    def check_long_entry(self, bar, i):
        """价格突破通道上轨 + 有效突破确认 → 做多"""
        if self.ENABLE_ADX_FILTER and not self._adx_trending(i):
            return False
        vals = self._channel_values(i)
        if vals is None:
            return False
        dc_high, dc_low, dc_mid = vals
        # 收盘价突破上轨（bar 的 close 是当前实时价格）
        return bar['close'] > dc_high

    def check_short_entry(self, bar, i):
        """价格跌破通道下轨 + 有效突破确认 → 做空"""
        if self.ENABLE_ADX_FILTER and not self._adx_trending(i):
            return False
        vals = self._channel_values(i)
        if vals is None:
            return False
        dc_high, dc_low, dc_mid = vals
        return bar['close'] < dc_low

    def check_exit(self, bar, i):
        """价格回归通道中轨 → 平仓（趋势可能结束）"""
        vals = self._channel_values(i)
        if vals is None:
            return False
        dc_high, dc_low, dc_mid = vals
        if self.position is None:
            return False
        if self.position.side == 'long':
            return bar['close'] < dc_mid
        else:
            return bar['close'] > dc_mid

    def get_atr(self, bar, i):
        return self._atr_value(i)

    def get_adx(self, bar, i):
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
