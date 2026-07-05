"""
资金费率套利策略 — 现货 + 永续合约对冲（基准策略）

赚钱逻辑：
  现货买入 + 永续合约做空 = 锁定价差，收取资金费率。
  多头支付空头 → 永续价格 > 现货价格时，多头给空头付钱。
  作为基准策略——其他策略必须先跑赢它才有存在的意义。

风险收益特征：
  - 市场中性（方向风险接近零）
  - 年化 10-30%，取决于资金费率水平
  - 风险：资金费率反转（从正变负）、交易所爆雷、手续费侵蚀

参数（3 个）：
  min_funding_rate: 最低年化费率才开仓（默认 10%，低于不参与）
  exit_funding_rate: 费率跌到多少退出（默认 5%，费率不行了就撤）
  max_holding_days: 最大持仓天数（默认 30，超时强制平仓）

注意：
  本策略使用模拟资金费率数据（基于 BTC 历史价格波动率推算）。
  实盘需接入 Binance/OKX 永续合约 API 获取实时 funding rate。
"""

import numpy as np
import pandas as pd
from ..strategy_base import BaseStrategy


class FundingArbitrageStrategy(BaseStrategy):
    """资金费率套利 — 现货多头 + 永续空头（模拟版）"""

    # 套利策略的交易成本更高：现货 + 合约双边
    FEE_RATE = 0.001        # 现货 0.1%
    SLIPPAGE = 0.0005       # 滑点 0.05%
    # 合约端额外成本（开仓 + 平仓）
    CONTRACT_FEE = 0.0004   # 0.04% maker + taker 平均

    # 套利策略不适用移动止盈和 ADX 过滤
    ENABLE_TRAILING_STOP = False
    ENABLE_ADX_FILTER = False

    def __init__(self, min_funding_rate=10.0, exit_funding_rate=5.0, max_holding_days=30):
        super().__init__(name=f"资金费率套利(>{min_funding_rate}%)")
        self.min_funding_rate = min_funding_rate        # 年化 %
        self.exit_funding_rate = exit_funding_rate       # 年化 %
        self.max_holding_days = max_holding_days

        self._funding_rate = None     # 年化资金费率序列
        self._daily_return = None     # 日收益率序列（用于模拟波动率）

    def precompute(self, df):
        """
        模拟资金费率序列

        实盘中替换为：
        - Binance: GET /fapi/v1/fundingRate?symbol=BTCUSDT
        - OKX: GET /api/v5/public/funding-rate?instId=BTC-USDT-SWAP

        模拟逻辑：
        - 资金费率与价格波动率和趋势方向相关
        - 强趋势 + 高波动 → 资金费率高（多头拥挤）
        - 震荡 + 低波动 → 资金费率低
        """
        close = df['close']

        # 波动率 proxy：用 30 日波动率
        returns = close.pct_change()
        vol_30d = returns.rolling(30).std() * np.sqrt(365)

        # 趋势 proxy：用 30 日动量
        momentum_30d = close.pct_change(30)

        # 资金费率模拟公式：
        # 费率 ≈ 基础费率 + 趋势加成 + 波动率调节
        # 历史平均 BTC 资金费率约 0.01%/8h ≈ 10.95% 年化
        base_rate = 8.0   # 年化基础费率 (%)
        trend_bonus = momentum_30d * 100 * 0.5   # 上升趋势 = 正费率（多头付钱）
        vol_boost = (vol_30d - vol_30d.median()) * 100 * 2

        raw_rate = base_rate + trend_bonus + vol_boost
        raw_rate = raw_rate.clip(lower=-30, upper=80)  # 合理范围

        # 滚动平滑
        self._funding_rate = raw_rate.rolling(7, min_periods=1).mean().shift(1)
        self._daily_return = returns.shift(1)

    def _get_funding_rate(self, i):
        """获取年化资金费率 (%)"""
        if self._funding_rate is None:
            return None
        val = self._funding_rate.iloc[i]
        return val if not pd.isna(val) else None

    def check_long_entry(self, bar, i):
        """
        套利入场条件：
        1. 资金费率 > 最低阈值（有肉吃）
        2. 当前无持仓
        """
        rate = self._get_funding_rate(i)
        if rate is None:
            return False
        return rate >= self.min_funding_rate

    def check_short_entry(self, bar, i):
        """套利策略不做单向做空"""
        return False

    def check_exit(self, bar, i):
        """
        套利平仓条件（满足任一即退出）：
        1. 资金费率跌到退出阈值以下（肉不够吃了）
        2. 持仓达到最大天数
        3. 资金费率变负（需要付钱，逻辑反转）
        """
        rate = self._get_funding_rate(i)
        if rate is None:
            return True  # 数据缺失，安全退出

        # 条件 1+3: 费率太低或变负
        if rate < self.exit_funding_rate:
            return True

        # 条件 2: 持仓超时
        if self.position is not None:
            bars_held = i - self.position.entry_idx
            # 日线：max_holding_days 直接是天数；小时线：*24 换算
            max_bars = self.max_holding_days
            if bars_held >= max_bars:
                return True

        return False

    def get_stop_loss(self, bar, i, side):
        """套利策略不用价格止损——方向风险已被对冲"""
        return None

    def get_take_profit(self, bar, i, side):
        """套利策略不用止盈——利润来自资金费率累积，不是价格波动"""
        return None

    # ═══════════════════════════════
    # 重写 _close_position，加入资金费率收益计算
    # ═══════════════════════════════

    def on_bar(self, bar, i):
        """
        套利策略自定义事件循环（覆盖基类 on_bar）

        和普通策略的区别：
        - 开仓时同时模拟现货买入 + 合约做空
        - 平仓时计算累计资金费率收益
        - 资金费率按每 8 小时结算一次
        """
        self._current_bar = bar
        self._current_idx = i

        # ── 有持仓：每日累积资金费率收益 ──
        if self.position is not None:
            # 资金费率按日累计（简化：年化费率 / 365 为日费率）
            rate = self._get_funding_rate(i)
            if rate is not None:
                daily_rate = rate / 100 / 365  # 年化% → 日小数
                self.position.funding_accrued = getattr(
                    self.position, 'funding_accrued', 0.0
                ) + daily_rate

            # 检查出场
            if self.check_exit(bar, i):
                exit_price = self._apply_slippage(bar['close'], 'exit', 'long')
                return self._close_position(exit_price, bar, i, 'signal')

        # ── 无持仓：检查入场 ──
        else:
            if self.check_long_entry(bar, i):
                entry_price = self._apply_slippage(bar['close'], 'entry', 'long')
                return self._open_position('long', entry_price, bar, i, None, None)

        # ── bar 快照 ──
        self.bar_log.append({
            'timestamp': bar.name if hasattr(bar, 'name') else i,
            'close': bar['close'],
            'has_position': self.position is not None,
            'anomaly': bar.get('anomaly', False),
        })

        return None

    def _open_position(self, side, price, bar, i, stop_loss, take_profit):
        """开仓 — 加入套利特有的成本（现货+合约双边）"""
        pos = super()._open_position(side, price, bar, i, stop_loss, take_profit)
        if pos and self.position:
            self.position.funding_accrued = 0.0
            self.position.entry_funding_rate = self._get_funding_rate(i)
        return pos

    def _close_position(self, price, bar, i, reason):
        """平仓 — PnL = 资金费率累积 - 双边交易成本"""
        pos = self.position
        entry_price = pos.entry_price

        # ── 价格方向 PnL 理论上接近零（现货+1，合约-1）──
        # 实际有小幅偏差（基差变动），这里按对冲完美计算
        gross_return = 0.0  # 对冲后方向风险被抵消

        # ── 资金费率收益 ──
        funding_return = getattr(pos, 'funding_accrued', 0.0)

        # ── 双边成本 ──
        # 现货：开仓 0.1% + 平仓 0.1% = 0.2%
        # 合约：开仓 0.04% + 平仓 0.04% = 0.08%
        # 总计约 0.28% 往返（比单向策略高一倍）
        spot_fee = self.FEE_RATE * 2      # 0.2%
        contract_fee = self.CONTRACT_FEE * 2  # 0.08%
        total_fee = spot_fee + contract_fee + self.SLIPPAGE * 2  # + 双边滑点

        net_return = gross_return + funding_return - total_fee

        # ── MAE/MFE 对套利策略意义不同 ──
        mae = 0.0
        mfe = funding_return

        trade = {
            'entry_time': pos.entry_time,
            'exit_time': bar.name if hasattr(bar, 'name') else i,
            'entry_idx': pos.entry_idx,
            'exit_idx': i,
            'side': 'arb',
            'entry_price': round(entry_price, 8),
            'exit_price': round(price, 8),
            'gross_return_pct': round(gross_return * 100, 4),
            'fee_pct': round(total_fee * 100, 4),
            'net_return_pct': round(net_return * 100, 4),
            'mae_pct': round(mae * 100, 4),
            'mfe_pct': round(mfe * 100, 4),
            'exit_reason': reason,
            'bars_held': i - pos.entry_idx,
            'funding_accrued_pct': round(funding_return * 100, 4),
            'entry_funding_rate': getattr(pos, 'entry_funding_rate', None),
        }

        self.trade_log.append(trade)
        self.position = None

        return {
            'action': 'exit',
            'side': 'arb',
            'price': price,
            'time': trade['exit_time'],
            'idx': i,
            'reason': reason,
            'net_return_pct': trade['net_return_pct'],
            'funding_return_pct': trade['funding_accrued_pct'],
        }
