"""
结构化日志系统 — Day 7 核心模块

职责：
  1. 交易日志（每笔订单的完整生命周期：信号→风控→提交→成交/取消）
  2. 系统日志（心跳、启动/停止、错误、重启）
  3. 风控日志（熔断触发、仓位调整、异常标记）
  4. PnL 快照（定时记录净值/持仓/未实现盈亏）

设计原则：
  - JSONL 格式（一行一个 JSON 对象），方便后续 grep/jq/数据分析
  - 每天一个文件（按日期滚动），避免单文件过大
  - 所有时间戳为 UTC ISO8601 格式
  - 敏感信息不入日志（API key 等脱敏）

用法：
  from live_logger import LiveLogger
  logger = LiveLogger(log_dir='logs', mode='paper')

  logger.trade_order_submitted(order_id='abc', symbol='BTC/USDT', ...)
  logger.system_heartbeat(uptime_seconds=3600)
  logger.risk_circuit_breaker(trigger='daily_loss', current_loss_pct=5.2)
  logger.pnl_snapshot(equity=10200, positions=[...])
"""

import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, Dict, List, Any


class LiveLogger:
    """
    结构化日志 — JSONL 格式

    四个日志流：
      1. trades.jsonl    — 交易事件
      2. system.jsonl    — 系统事件
      3. risk.jsonl      — 风控事件
      4. pnl.jsonl       — 净值快照
    """

    def __init__(self, log_dir='logs', mode='paper', keep_days=90):
        self.log_dir = Path(log_dir)
        self.mode = mode
        self.keep_days = keep_days
        self._start_time = time.time()

        # 确保目录存在
        self.log_dir.mkdir(parents=True, exist_ok=True)

        # 今日日期用于文件名
        self._today = self._today_str()

    def _today_str(self) -> str:
        """今天的日期字符串 YYYYMMDD"""
        return datetime.now(timezone.utc).strftime('%Y%m%d')

    def _log_file(self, name: str) -> str:
        """日志文件路径，按日期滚动"""
        if self._today != self._today_str():
            self._today = self._today_str()
        return str(self.log_dir / f"{name}-{self._today}.jsonl")

    def _write(self, file_key: str, event_type: str, data: dict):
        """写入一条 JSONL 日志"""
        record = {
            'ts': datetime.now(timezone.utc).isoformat(),
            'event': event_type,
            'mode': self.mode,
            **data,
        }
        filepath = self._log_file(file_key)
        try:
            with open(filepath, 'a', encoding='utf-8') as f:
                f.write(json.dumps(record, ensure_ascii=False, default=str) + '\n')
        except Exception as e:
            print(f"  ⚠️ 日志写入失败: {filepath} — {e}")

    # ═══════════════════════════════
    # 交易日志
    # ═══════════════════════════════

    def trade_signal(self, strategy: str, symbol: str, side: str, action: str,
                     signal_price: float, bar_idx: int, **kwargs):
        """策略生成信号"""
        self._write('trades', 'signal', {
            'strategy': strategy, 'symbol': symbol, 'side': side,
            'action': action, 'signal_price': signal_price,
            'bar_idx': bar_idx, **kwargs,
        })

    def trade_risk_check(self, order_id: str, passed: bool, reason: str = "", **kwargs):
        """风控检查结果"""
        self._write('trades', 'risk_check', {
            'order_id': order_id, 'passed': passed, 'reason': reason, **kwargs,
        })

    def trade_submitted(self, order_id: str, symbol: str, side: str, action: str,
                        order_type: str, price: float, quantity: float, **kwargs):
        """订单已提交"""
        self._write('trades', 'submitted', {
            'order_id': order_id, 'symbol': symbol, 'side': side,
            'action': action, 'order_type': order_type,
            'price': price, 'quantity': quantity, **kwargs,
        })

    def trade_filled(self, order_id: str, fill_price: float, fill_quantity: float,
                     cost_bps: float = 0, **kwargs):
        """订单成交"""
        self._write('trades', 'filled', {
            'order_id': order_id, 'fill_price': fill_price,
            'fill_quantity': fill_quantity, 'cost_bps': cost_bps, **kwargs,
        })

    def trade_cancelled(self, order_id: str, reason: str = "", **kwargs):
        """订单取消"""
        self._write('trades', 'cancelled', {
            'order_id': order_id, 'reason': reason, **kwargs,
        })

    def trade_closed(self, order_id: str, entry_price: float, exit_price: float,
                     side: str, net_return_pct: float, exit_reason: str,
                     bars_held: int, cost_bps: float = 0, **kwargs):
        """完整交易闭合记录（入场+出场配对）"""
        self._write('trades', 'closed', {
            'order_id': order_id, 'entry_price': entry_price,
            'exit_price': exit_price, 'side': side,
            'net_return_pct': round(net_return_pct, 4),
            'exit_reason': exit_reason, 'bars_held': bars_held,
            'cost_bps': cost_bps, **kwargs,
        })

    # ═══════════════════════════════
    # 系统日志
    # ═══════════════════════════════

    def system_startup(self, version: str = "0.1.0", config: dict = None):
        """系统启动"""
        self._write('system', 'startup', {
            'version': version,
            'mode': self.mode,
            'config': config or {},
        })
        print(f"📝 系统启动 — 日志目录: {self.log_dir}")

    def system_shutdown(self, reason: str = "normal", uptime_seconds: float = 0):
        """系统关闭"""
        self._write('system', 'shutdown', {
            'reason': reason,
            'uptime_seconds': round(uptime_seconds, 1),
        })

    def system_heartbeat(self, uptime_seconds: float = 0, active_positions: int = 0,
                         equity: float = 0, **kwargs):
        """心跳（定期写入，用于监控系统是否存活）"""
        self._write('system', 'heartbeat', {
            'uptime_seconds': round(uptime_seconds, 1),
            'active_positions': active_positions,
            'equity': round(equity, 2),
            **kwargs,
        })

    def system_error(self, error_type: str, message: str, traceback: str = "",
                     **kwargs):
        """错误/异常"""
        self._write('system', 'error', {
            'error_type': error_type,
            'message': message,
            'traceback': traceback[:2000],  # 截断过长 traceback
            **kwargs,
        })

    def system_restart(self, reason: str = "", attempt: int = 0, **kwargs):
        """自动重启"""
        self._write('system', 'restart', {
            'reason': reason,
            'attempt': attempt,
            **kwargs,
        })

    # ═══════════════════════════════
    # 风控日志
    # ═══════════════════════════════

    def risk_circuit_breaker(self, trigger: str, current_value: float,
                             limit: float, action: str = "halt", **kwargs):
        """
        熔断触发

        trigger: 'daily_loss' | 'consecutive_losses' | 'price_spike' | 'api_error'
        """
        self._write('risk', 'circuit_breaker', {
            'trigger': trigger,
            'current_value': round(current_value, 4),
            'limit': limit,
            'action': action,  # 'halt' | 'reduce' | 'warn'
            **kwargs,
        })

    def risk_position_adjust(self, symbol: str, old_size_pct: float,
                             new_size_pct: float, reason: str, **kwargs):
        """仓位调整"""
        self._write('risk', 'position_adjust', {
            'symbol': symbol,
            'old_size_pct': round(old_size_pct, 4),
            'new_size_pct': round(new_size_pct, 4),
            'reason': reason,
            **kwargs,
        })

    def risk_anomaly_detected(self, anomaly_type: str, symbol: str = "",
                              severity: str = "warn", details: str = "", **kwargs):
        """异常检测"""
        self._write('risk', 'anomaly', {
            'anomaly_type': anomaly_type,
            'symbol': symbol,
            'severity': severity,  # 'warn' | 'critical' | 'halt'
            'details': details,
            **kwargs,
        })

    # ═══════════════════════════════
    # PnL 日志
    # ═══════════════════════════════

    def pnl_snapshot(self, equity: float, initial_capital: float,
                     positions: List[Dict] = None, daily_pnl: float = 0,
                     unrealized_pnl: float = 0, **kwargs):
        """净值快照（定时记录）"""
        self._write('pnl', 'snapshot', {
            'equity': round(equity, 2),
            'initial_capital': round(initial_capital, 2),
            'total_return_pct': round((equity / initial_capital - 1) * 100, 4),
            'daily_pnl': round(daily_pnl, 2),
            'unrealized_pnl': round(unrealized_pnl, 2),
            'num_positions': len(positions) if positions else 0,
            'positions': positions or [],
            **kwargs,
        })

    def pnl_daily_summary(self, date: str, start_equity: float, end_equity: float,
                          num_trades: int, total_cost_bps: float = 0, **kwargs):
        """每日盈亏摘要"""
        self._write('pnl', 'daily_summary', {
            'date': date,
            'start_equity': round(start_equity, 2),
            'end_equity': round(end_equity, 2),
            'daily_return_pct': round((end_equity / start_equity - 1) * 100, 4),
            'num_trades': num_trades,
            'total_cost_bps': round(total_cost_bps, 1),
            **kwargs,
        })

    # ═══════════════════════════════
    # 工具方法
    # ═══════════════════════════════

    def uptime_seconds(self) -> float:
        """运行时长（秒）"""
        return time.time() - self._start_time

    def get_recent_trades(self, n=20) -> List[Dict]:
        """获取最近 N 条已完成交易（方便快速查看）"""
        trades = []
        filepath = self._log_file('trades')
        if not os.path.exists(filepath):
            return trades
        try:
            with open(filepath, 'r', encoding='utf-8') as f:
                lines = f.readlines()
                for line in lines[-n*3:]:  # 每笔交易 3 条日志，多读一些
                    try:
                        rec = json.loads(line)
                        if rec.get('event') == 'closed':
                            trades.append(rec)
                    except json.JSONDecodeError:
                        continue
            return trades[-n:]
        except Exception:
            return trades

    def get_today_summary(self) -> Dict:
        """获取今日交易摘要"""
        trades = self.get_recent_trades(999)
        if not trades:
            return {'num_trades': 0, 'total_return_pct': 0, 'win_rate': 0}

        returns = [t['net_return_pct'] for t in trades]
        wins = [r for r in returns if r > 0]
        return {
            'num_trades': len(trades),
            'total_return_pct': round(sum(returns), 4),
            'win_rate': round(len(wins) / len(returns) * 100, 1),
            'avg_return_pct': round(sum(returns) / len(returns), 4),
            'best_trade_pct': round(max(returns), 4),
            'worst_trade_pct': round(min(returns), 4),
        }
