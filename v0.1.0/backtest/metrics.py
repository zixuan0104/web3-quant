"""
指标计算 — 从净值曲线和交易日志提取全套量化指标

计算内容：
  - 收益率：总收益、年化收益、月收益序列
  - 风险：年化波动率、最大回撤、最大回撤持续时间
  - 风险调整：夏普比率、Calmar 比率、Sortino 比率
  - 交易统计：胜率、盈亏比、利润率因子、平均持仓时间
  - MAE/MFE 分布

默认无风险利率 3%。
"""

import numpy as np
import pandas as pd

RISK_FREE_RATE = 0.03  # 3% 无风险利率


def compute_metrics(equity_df, trade_df, initial_capital, label=''):
    """
    从净值曲线和交易日志计算全套指标

    参数:
      equity_df: pd.DataFrame, index=timestamp, columns=['equity', 'return']
      trade_df: pd.DataFrame, 交易日志（可能为空）
      initial_capital: 初始资金
      label: 标签（'样本内' / '样本外' / '全样本'）

    返回: dict
    """
    if len(equity_df) < 2:
        return _empty_metrics(label)

    equity = equity_df['equity'].values
    returns_arr = equity_df['return'].values

    n_days = max((equity_df.index[-1] - equity_df.index[0]).days, 1)

    # ── 收益率 ──
    total_return = (equity[-1] / initial_capital) - 1
    annual_return = (1 + total_return) ** (365 / n_days) - 1 if n_days > 0 else 0

    # ── 日收益率序列（从 equity 推算）──
    daily_returns = pd.Series(equity).pct_change().dropna()

    # ── 波动率 ──
    annual_vol = daily_returns.std() * np.sqrt(365) if len(daily_returns) > 1 else 0

    # ── 最大回撤 ──
    peak = np.maximum.accumulate(equity)
    drawdown = (equity - peak) / peak
    max_dd = drawdown.min()
    max_dd_idx = drawdown.argmin()

    # 最大回撤持续时间
    dd_start = None
    dd_duration = 0
    max_dd_duration = 0
    in_dd = False
    for i in range(len(equity)):
        if equity[i] < peak[i]:
            if not in_dd:
                dd_start = i
                in_dd = True
            dd_duration = i - dd_start
            max_dd_duration = max(max_dd_duration, dd_duration)
        else:
            in_dd = False
            dd_duration = 0

    # ── 夏普比率 ──
    excess = daily_returns - RISK_FREE_RATE / 365
    sharpe = (excess.mean() / daily_returns.std()) * np.sqrt(365) if daily_returns.std() > 0 else 0

    # ── Calmar 比率 ──
    calmar = annual_return / abs(max_dd) if max_dd != 0 else 0

    # ── Sortino 比率 ──
    downside = daily_returns[daily_returns < 0]
    downside_vol = downside.std() * np.sqrt(365) if len(downside) > 1 else 0
    sortino = (annual_return - RISK_FREE_RATE) / downside_vol if downside_vol > 0 else 0

    # ── 交易统计 ──
    if trade_df is not None and len(trade_df) > 0:
        total_trades = len(trade_df)
        winners = trade_df[trade_df['net_return_pct'] > 0]
        losers = trade_df[trade_df['net_return_pct'] <= 0]
        win_count = len(winners)
        lose_count = len(losers)
        win_rate = win_count / total_trades * 100 if total_trades > 0 else 0

        avg_win = winners['net_return_pct'].mean() if win_count > 0 else 0
        avg_loss = abs(losers['net_return_pct'].mean()) if lose_count > 0 else 0
        profit_loss_ratio = avg_win / avg_loss if avg_loss > 0 else 0

        total_profit = winners['net_return_pct'].sum() if win_count > 0 else 0
        total_loss = abs(losers['net_return_pct'].sum()) if lose_count > 0 else 0
        profit_factor = total_profit / total_loss if total_loss > 0 else float('inf')

        avg_bars_held = trade_df['bars_held'].mean()
        max_bars_held = trade_df['bars_held'].max()

        # ── MAE / MFE 统计 ──
        avg_mae = trade_df['mae_pct'].mean()
        avg_mfe = trade_df['mfe_pct'].mean()
        mae_95 = trade_df['mae_pct'].abs().quantile(0.95)  # 95% 分位 MAE

        # ── 最大连续亏损笔数 ──
        max_consecutive_losses = _max_consecutive(trade_df['net_return_pct'] <= 0)

        # ── 按出场原因分组 ──
        exit_reason_counts = trade_df['exit_reason'].value_counts().to_dict()
    else:
        total_trades = 0
        win_rate = 0
        profit_loss_ratio = 0
        profit_factor = 0
        avg_bars_held = 0
        max_bars_held = 0
        avg_mae = 0
        avg_mfe = 0
        mae_95 = 0
        max_consecutive_losses = 0
        exit_reason_counts = {}

    return {
        'label': label,
        'initial_capital': initial_capital,
        'final_equity': round(float(equity[-1]), 2),
        'total_return_pct': round(total_return * 100, 2),
        'annual_return_pct': round(annual_return * 100, 2),
        'annual_vol_pct': round(annual_vol * 100, 2),
        'max_drawdown_pct': round(max_dd * 100, 2),
        'max_dd_duration_bars': max_dd_duration,
        'sharpe': round(sharpe, 3),
        'calmar': round(calmar, 3),
        'sortino': round(sortino, 3),
        'total_trades': total_trades,
        'win_rate': round(win_rate, 1),
        'profit_loss_ratio': round(profit_loss_ratio, 2),
        'profit_factor': round(profit_factor, 2) if profit_factor != float('inf') else '∞',
        'avg_bars_held': round(avg_bars_held, 1),
        'max_bars_held': int(max_bars_held),
        'avg_mae_pct': round(avg_mae, 4),
        'avg_mfe_pct': round(avg_mfe, 4),
        'mae_95_pct': round(mae_95, 4),
        'max_consecutive_losses': max_consecutive_losses,
        'exit_reason_counts': exit_reason_counts,
    }


def _max_consecutive(mask):
    """计算最大连续 True 的次数"""
    max_streak = 0
    current = 0
    for v in mask:
        if v:
            current += 1
            max_streak = max(max_streak, current)
        else:
            current = 0
    return max_streak


def _empty_metrics(label):
    """空指标（无交易时返回）"""
    return {
        'label': label,
        'total_return_pct': 0,
        'annual_return_pct': 0,
        'annual_vol_pct': 0,
        'max_drawdown_pct': 0,
        'max_dd_duration_bars': 0,
        'sharpe': 0,
        'calmar': 0,
        'sortino': 0,
        'total_trades': 0,
        'win_rate': 0,
        'profit_loss_ratio': 0,
        'profit_factor': 0,
    }
