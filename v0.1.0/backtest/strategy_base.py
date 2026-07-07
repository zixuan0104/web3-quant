"""
策略基类 — 支持多空双向交易

基类负责：
  1. 持仓状态管理（多头/空头/空仓）
  2. 订单生命周期（创建 → 成交 → 记录）
  3. 成本模型（0.1% 手续费 + 0.05% 滑点，双向收取）
  4. 异常标记感知（anomaly=True 的 K 线默认跳过信号生成）
  5. 资金管理（固定比例仓位）
  6. 交易日志记录（含多空标签）

子类只需实现：
  - check_long_entry(bar, i) → bool    # 做多入场信号
  - check_short_entry(bar, i) → bool   # 做空入场信号（新增）
  - check_exit(bar, i) → bool          # 平仓信号
  - get_stop_loss(bar, i) → float      # 止损价（方向感知，可选）
  - get_take_profit(bar, i) → float    # 止盈价（方向感知，可选）

多空优先级：如果同一根 K 线同时触发多空信号，做多优先（可配置 REVERSE_PRIORITY）
"""

import numpy as np
import pandas as pd


class Position:
    """持仓状态 — 支持多空"""

    def __init__(self, side, entry_price, entry_time, entry_idx, stop_loss=None, take_profit=None):
        self.side = side              # 'long' | 'short'
        self.entry_price = entry_price
        self.entry_time = entry_time
        self.entry_idx = entry_idx
        self.stop_loss = stop_loss
        self.take_profit = take_profit
        # 多空极值含义不同，在 _close_position 中计算 MAE/MFE
        self.lowest = entry_price
        self.highest = entry_price

    def update_extremes(self, bar_low, bar_high):
        """更新持仓期间价格极值"""
        self.lowest = min(self.lowest, bar_low)
        self.highest = max(self.highest, bar_high)


class BaseStrategy:
    """策略基类 — 事件驱动 + 多空双向"""

    # ── 成本参数 ──
    FEE_RATE = 0.001        # 0.1% 单边手续费
    SLIPPAGE = 0.0005       # 0.05% 滑点
    REVERSE_PRIORITY = False

    # ── 移动止盈参数（P0优化）──
    TRAILING_ACTIVATION_ATR = 2.0   # 盈利超过 N 倍 ATR 后激活移动止盈
    TRAILING_DISTANCE_ATR = 1.5     # 移动止盈距离（ATR 倍数）
    ENABLE_TRAILING_STOP = True     # 是否启用移动止盈

    # ── 震荡市过滤（P1优化）──
    ADX_TRENDING_THRESHOLD = 20     # ADX < 此值视为震荡市
    ENABLE_ADX_FILTER = True        # 是否启用震荡市过滤

    # ── 做空开关（Day 11 安全加固）──
    # True  = 回测/全功能模式，多空双向
    # False = 实盘起步模式，只做多不做空（避免保证金借币 + 强平风险）
    # 实盘建议：先验证做多路径跑通，再改为 True
    ALLOW_SHORT = True

    def __init__(self, name="BaseStrategy"):
        self.name = name
        self.position = None
        self.trade_log = []
        self.bar_log = []
        self._current_bar = None
        self._current_idx = -1

    # ═══════════════════════════════
    # 子类必须重写的方法
    # ═══════════════════════════════

    def check_long_entry(self, bar, i):
        """做多入场信号 — 子类重写"""
        return False

    def check_short_entry(self, bar, i):
        """做空入场信号 — 子类重写"""
        return False

    def check_exit(self, bar, i):
        """出场信号 — 子类重写"""
        return False

    def get_stop_loss(self, bar, i, side):
        """止损价 — 子类可选重写"""
        return None

    def get_take_profit(self, bar, i, side):
        """止盈价 — 子类可选重写"""
        return None

    def get_atr(self, bar, i):
        """返回当前 ATR 值 — 子类重写以启用移动止盈"""
        return None

    def get_adx(self, bar, i):
        """返回当前 ADX 值 — 子类重写以启用震荡市过滤"""
        return 100  # 默认返回高值 = 视为趋势市

    # ═══════════════════════════════
    # 事件循环入口
    # ═══════════════════════════════

    def on_bar(self, bar, i):
        """
        每根 K 线回调

        流程：
          1. 更新持仓极值
          2. 检查硬止损/止盈
          3. 有持仓 → check_exit()
          4. 无持仓 → check_long_entry() / check_short_entry()

        返回: dict | None
        """
        self._current_bar = bar
        self._current_idx = i
        is_anomaly = bar.get('anomaly', False)

        # ── 更新持仓极值 ──
        if self.position is not None:
            self.position.update_extremes(bar['low'], bar['high'])

        # ── P0: 移动止盈（在硬止损/止盈之前检查，更新止损价）──
        if self.position is not None and self.ENABLE_TRAILING_STOP:
            self._update_trailing_stop(bar, i)

        # ── 硬止损/止盈（含已被移动止盈更新过的止损）──
        if self.position is not None:
            exit_result = self._check_stop_loss_take_profit(bar, i)
            if exit_result:
                return exit_result

        # ── 有持仓 → 信号出场 ──
        if self.position is not None:
            try:
                if self.check_exit(bar, i):
                    exit_price = self._apply_slippage(bar['close'], 'exit', self.position.side)
                    return self._close_position(exit_price, bar, i, 'signal')
            except Exception as e:
                print(f"  ⚠️ [{self.name}] check_exit 异常 @ {bar.name}: {e}")

        # ── 无持仓 → 检查入场 ──
        else:
            if is_anomaly:
                return None

            try:
                long_signal = self.check_long_entry(bar, i)
                short_signal = self.check_short_entry(bar, i) if self.ALLOW_SHORT else False

                if long_signal and short_signal:
                    # 多空同时触发 → 按优先级选
                    if self.REVERSE_PRIORITY:
                        long_signal = False
                    else:
                        short_signal = False

                if long_signal:
                    entry_price = self._apply_slippage(bar['close'], 'entry', 'long')
                    sl = self.get_stop_loss(bar, i, 'long')
                    tp = self.get_take_profit(bar, i, 'long')
                    return self._open_position('long', entry_price, bar, i, sl, tp)

                if short_signal:
                    entry_price = self._apply_slippage(bar['close'], 'entry', 'short')
                    sl = self.get_stop_loss(bar, i, 'short')
                    tp = self.get_take_profit(bar, i, 'short')
                    return self._open_position('short', entry_price, bar, i, sl, tp)

            except Exception as e:
                print(f"  ⚠️ [{self.name}] check_entry 异常 @ {bar.name}: {e}")

        # ── bar 快照 ──
        self.bar_log.append({
            'timestamp': bar.name if hasattr(bar, 'name') else i,
            'close': bar['close'],
            'has_position': self.position is not None,
            'anomaly': is_anomaly,
        })

        return None

    # ═══════════════════════════════
    # 内部方法
    # ═══════════════════════════════

    def _update_trailing_stop(self, bar, i):
        """
        P0 移动止盈：盈利超过激活阈值后，止损跟随最佳价格移动

        做多：止损 = 持仓期间最高价 × (1 - distance%)
        做空：止损 = 持仓期间最低价 × (1 + distance%)
        止损只能向有利方向移动（做多只能上移，做空只能下移）
        """
        atr = self.get_atr(bar, i)
        if atr is None or (isinstance(atr, float) and pd.isna(atr)) or atr <= 0:
            return

        pos = self.position
        activation_pct = self.TRAILING_ACTIVATION_ATR * atr / bar['close']
        distance_pct = self.TRAILING_DISTANCE_ATR * atr / bar['close']

        if pos.side == 'long':
            best_price = pos.highest
            profit_from_best = (best_price - pos.entry_price) / pos.entry_price
            if profit_from_best >= activation_pct:
                new_stop = best_price * (1 - distance_pct)
                if pos.stop_loss is None or new_stop > pos.stop_loss:
                    pos.stop_loss = new_stop
        else:  # short
            best_price = pos.lowest
            profit_from_best = (pos.entry_price - best_price) / pos.entry_price
            if profit_from_best >= activation_pct:
                new_stop = best_price * (1 + distance_pct)
                if pos.stop_loss is None or new_stop < pos.stop_loss:
                    pos.stop_loss = new_stop

    def _check_stop_loss_take_profit(self, bar, i):
        """检查硬止损/止盈 — 方向感知"""
        pos = self.position
        reason = None
        exit_price = None

        if pos.side == 'long':
            if pos.stop_loss and bar['low'] <= pos.stop_loss:
                reason = 'stop_loss'
                exit_price = pos.stop_loss
            elif pos.take_profit and bar['high'] >= pos.take_profit:
                reason = 'take_profit'
                exit_price = pos.take_profit

        elif pos.side == 'short':
            # 做空：止损在上方（价格上涨 = 亏损），止盈在下方（价格下跌 = 盈利）
            if pos.stop_loss and bar['high'] >= pos.stop_loss:
                reason = 'stop_loss'
                exit_price = pos.stop_loss
            elif pos.take_profit and bar['low'] <= pos.take_profit:
                reason = 'take_profit'
                exit_price = pos.take_profit

        if reason:
            return self._close_position(exit_price, bar, i, reason)
        return None

    def _apply_slippage(self, price, action, side):
        """计算含滑点的成交价 — 方向感知"""
        slip = price * self.SLIPPAGE
        if action == 'entry':
            return price + slip if side == 'long' else price - slip
        else:  # exit
            return price - slip if side == 'long' else price + slip

    def _open_position(self, side, price, bar, i, stop_loss, take_profit):
        """开仓"""
        self.position = Position(
            side=side,
            entry_price=price,
            entry_time=bar.name if hasattr(bar, 'name') else i,
            entry_idx=i,
            stop_loss=stop_loss,
            take_profit=take_profit,
        )
        return {
            'action': 'entry',
            'side': side,
            'price': price,
            'time': self.position.entry_time,
            'idx': i,
            'stop_loss': stop_loss,
            'take_profit': take_profit,
        }

    def _close_position(self, price, bar, i, reason):
        """平仓并记录交易 — 多空 PnL 正确计算"""
        pos = self.position
        entry_price = pos.entry_price
        side = pos.side

        # ── PnL：做多赚涨、做空赚跌 ──
        if side == 'long':
            gross_return = (price - entry_price) / entry_price
            mae = (pos.lowest - entry_price) / entry_price      # 最大不利偏移（负值）
            mfe = (pos.highest - entry_price) / entry_price     # 最大有利偏移（正值）
        else:  # short
            gross_return = (entry_price - price) / entry_price  # 下跌 = 盈利
            mae = (entry_price - pos.highest) / entry_price     # 最大不利偏移（涨 = 亏损）
            mfe = (entry_price - pos.lowest) / entry_price      # 最大有利偏移（跌 = 盈利）

        fee_cost = self.FEE_RATE * 2  # 开仓 + 平仓
        net_return = gross_return - fee_cost

        trade = {
            'entry_time': pos.entry_time,
            'exit_time': bar.name if hasattr(bar, 'name') else i,
            'entry_idx': pos.entry_idx,
            'exit_idx': i,
            'side': side,
            'entry_price': round(entry_price, 8),
            'exit_price': round(price, 8),
            'gross_return_pct': round(gross_return * 100, 4),
            'fee_pct': round(fee_cost * 100, 4),
            'net_return_pct': round(net_return * 100, 4),
            'mae_pct': round(mae * 100, 4),
            'mfe_pct': round(mfe * 100, 4),
            'exit_reason': reason,
            'bars_held': i - pos.entry_idx,
        }

        self.trade_log.append(trade)
        self.position = None

        return {
            'action': 'exit',
            'side': side,
            'price': price,
            'time': trade['exit_time'],
            'idx': i,
            'reason': reason,
            'net_return_pct': trade['net_return_pct'],
        }

    def has_position(self):
        return self.position is not None

    def get_trade_log_df(self):
        if not self.trade_log:
            return pd.DataFrame()
        return pd.DataFrame(self.trade_log)
