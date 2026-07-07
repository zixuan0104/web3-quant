"""
回测引擎 — 事件驱动循环

职责：
  1. 加载清理后的 parquet 数据
  2. 样本内/样本外分割（默认 70/30）
  3. 逐 K 线推送给策略实例
  4. 维护净值曲线
  5. 返回结构化回测结果
"""

import pandas as pd
import numpy as np
import os


class BacktestEngine:
    """事件驱动回测引擎"""

    def __init__(self, strategy, initial_capital=10000, split_ratio=0.7):
        """
        strategy: BaseStrategy 子类实例
        initial_capital: 初始资金（USDT）
        split_ratio: 样本内比例（0.7 = 70% 样本内, 30% 样本外）
        """
        self.strategy = strategy
        self.initial_capital = initial_capital
        self.split_ratio = split_ratio
        self.results = None

    def load_data(self, symbol, timeframe='1h', clean_dir=None):
        """
        加载清洗后的 parquet 数据

        如果 clean_dir 未指定，自动在脚本所在目录找 clean/ 文件夹
        """
        if clean_dir is None:
            clean_dir = os.path.join(
                os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                'clean'
            )

        symbol_safe = symbol.replace('/', '')
        filepath = os.path.join(clean_dir, f"{symbol_safe}_{timeframe}.parquet")

        if not os.path.exists(filepath):
            raise FileNotFoundError(f"数据文件不存在: {filepath}")

        df = pd.read_parquet(filepath)
        print(f"[加载] {filepath} ({len(df):,} 行)")

        # 确认 anomaly 列存在
        if 'anomaly' not in df.columns:
            print("  [WARN] 数据中无 anomaly 列，所有 K 线视为正常")

        return df

    def run(self, df):
        """
        执行回测

        返回:
          dict {
            'equity_curve': pd.Series,
            'trade_log': pd.DataFrame,
            'split_idx': int (样本内/样本外分割点),
            'in_sample': dict (指标),
            'out_of_sample': dict (指标),
            'full_sample': dict (指标),
          }
        """
        n = len(df)
        split_idx = int(n * self.split_ratio)

        # ── 重置策略状态 ──
        self.strategy.position = None
        self.strategy.trade_log = []
        self.strategy.bar_log = []

        # ── 初始化 ──
        equity = self.initial_capital
        equity_curve = []
        peak_equity = self.initial_capital

        entry_count = 0
        anomaly_skipped = 0

        print(f"\n[开始回测] {self.strategy.name}")
        print(f"   数据: {df.index[0]} → {df.index[-1]} ({n:,} 根 K 线)")
        print(f"   样本内: 0 → {split_idx-1} ({split_idx:,} 根)")
        print(f"   样本外: {split_idx} → {n-1} ({n - split_idx:,} 根)")
        print(f"   初始资金: {self.initial_capital:,.0f} USDT")

        # ── 事件循环 ──
        for i in range(n):
            bar = df.iloc[i]
            is_anomaly = bar.get('anomaly', False)

            result = self.strategy.on_bar(bar, i)

            # ── 统计异常跳过次数 ──
            if result is None and is_anomaly and not self.strategy.has_position():
                anomaly_skipped += 1

            # ── 平仓时更新实际净值 ──
            if result is not None and result['action'] == 'exit':
                net_return = result['net_return_pct'] / 100
                equity = equity * (1 + net_return)
                entry_count += 1

            # ── 计算当前净值（含浮动盈亏，多空方向感知）──
            if self.strategy.has_position():
                pos = self.strategy.position
                if pos.side == 'long':
                    unrealized = (bar['close'] - pos.entry_price) / pos.entry_price
                else:  # short
                    unrealized = (pos.entry_price - bar['close']) / pos.entry_price
                current_equity = equity * (1 + unrealized)
            else:
                current_equity = equity

            equity_curve.append({
                'timestamp': df.index[i],
                'equity': current_equity,
                'return': (current_equity / self.initial_capital - 1),
            })

        equity_df = pd.DataFrame(equity_curve).set_index('timestamp')

        # ── 最终净值（取最后一根 K 线的值）──
        final_equity = equity_df['equity'].iloc[-1]
        trade_log_df = self.strategy.get_trade_log_df()

        # ── 计算指标 ──
        from .metrics import compute_metrics
        is_df = equity_df.iloc[:split_idx]
        oos_df = equity_df.iloc[split_idx:]

        self.results = {
            'equity_curve': equity_df,
            'trade_log': trade_log_df,
            'split_idx': split_idx,
            'in_sample': compute_metrics(is_df, trade_log_df, self.initial_capital, label='样本内'),
            'out_of_sample': compute_metrics(oos_df, trade_log_df, self.initial_capital, label='样本外'),
            'full_sample': compute_metrics(equity_df, trade_log_df, self.initial_capital, label='全样本'),
            'anomaly_skipped': anomaly_skipped,
            'total_bars': n,
        }

        # ── 快速打印 ──
        fs = self.results['full_sample']
        print(f"\n[回测完成]")
        print(f"   总交易: {fs['total_trades']} 笔")
        print(f"   胜率: {fs['win_rate']:.1f}%")
        print(f"   总收益: {fs['total_return_pct']:.2f}%")
        print(f"   夏普: {fs['sharpe']:.2f}")
        print(f"   最大回撤: {fs['max_drawdown_pct']:.2f}%")
        print(f"   异常 K 线跳过: {anomaly_skipped} 次")

        return self.results
