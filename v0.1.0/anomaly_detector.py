"""
异常检测模块 — Day 10 核心模块

币圈的「黑天鹅」其实是日常：
  - 5 分钟跌 30%（价格断崖）
  - API 突然返回 503（交易所挂了）
  - WebSocket 断了但你不知道（数据已经过期 3 分钟了）
  - 成交量突然放大 10 倍（有人砸盘）

这个模块做的事：在这些异常发生时，第一时间检测到并触发保护动作。

三大检测维度：
  1. 价格异常 — 断崖/插针/成交量突增
  2. 连接异常 — API 超时/WebSocket 断连/心跳丢失
  3. 数据异常 — 数据断流/时间戳倒退/价源分歧

与 RiskManager 的关系：
  AnomalyDetector 检测异常 → 触发 RiskManager 熔断
  风控闭环的最后一块拼图：仓位管理 → 止损 → 异常检测 → 熔断

用法：
  from anomaly_detector import AnomalyDetector
  ad = AnomalyDetector()

  # 每根 K 线/每次 tick 调用
  result = ad.check_price_anomaly('BTC/USDT', current_price=65000, bar=bar)
  if result.triggered:
      print(f"异常: {result.reason}")
"""

import time
from datetime import datetime, timezone
from dataclasses import dataclass, field
from typing import Optional, Dict, List, Tuple
from enum import Enum
from collections import deque


# ═══════════════════════════════
# 数据类型
# ═══════════════════════════════

class AnomalyType(Enum):
    PRICE_SPIKE = "price_spike"            # 价格断崖/插针
    VOLUME_SPIKE = "volume_spike"          # 成交量突增
    PRICE_STALE = "price_stale"            # 价格长时间不变（数据断了）
    SPREAD_WIDENING = "spread_widening"    # 买卖价差异常扩大
    API_TIMEOUT = "api_timeout"            # API 超时
    API_ERROR_RATE = "api_error_rate"       # API 错误率过高
    WS_DISCONNECT = "ws_disconnect"        # WebSocket 断连
    WS_HEARTBEAT_LOST = "ws_heartbeat_lost"  # 心跳丢失
    DATA_GAP = "data_gap"                  # 数据断流
    TIMESTAMP_REGRESSION = "timestamp_regression"  # 时间戳倒退
    SOURCE_DIVERGENCE = "source_divergence"  # 多数据源分歧


class AnomalySeverity(Enum):
    INFO = "info"         # 记录但不暂停
    WARNING = "warning"    # 警告，收紧风控
    CRITICAL = "critical"  # 立即熔断


@dataclass
class AnomalyResult:
    """异常检测结果"""
    triggered: bool = False
    anomaly_type: Optional[AnomalyType] = None
    severity: AnomalySeverity = AnomalySeverity.INFO
    reason: str = ""
    detail: Dict = field(default_factory=dict)
    timestamp: float = 0.0

    def __post_init__(self):
        if self.timestamp == 0.0:
            self.timestamp = time.time()


@dataclass
class AnomalyConfig:
    """异常检测配置"""

    # ── 价格异常 ──
    price_spike_pct_5min: float = 10.0      # 5 分钟内涨跌 >10% → 暂停
    price_spike_pct_1min: float = 5.0       # 1 分钟内涨跌 >5% → 警告
    price_stale_seconds: float = 300.0       # 价格 5 分钟不变 → 数据可能断了
    max_spread_pct: float = 5.0             # 买卖价差 >5% → 流动性异常

    # ── 成交量异常 ──
    volume_spike_multiplier: float = 5.0     # 成交量 > 近期均值的 5 倍 → 异常
    volume_lookback_bars: int = 20           # 成交量均值计算窗口

    # ── API 异常 ──
    api_timeout_seconds: float = 10.0        # API 单次超时阈值
    api_max_retries: int = 3                # 最大重试次数
    api_error_rate_threshold: float = 0.3    # 最近 10 次请求错误率 >30% → 熔断
    api_error_window: int = 10              # 错误率计算窗口

    # ── WebSocket 异常 ──
    ws_heartbeat_interval: float = 30.0      # 预期心跳间隔（秒）
    ws_heartbeat_timeout: float = 90.0       # 3 个心跳周期无响应 → 断连
    ws_reconnect_max_attempts: int = 5       # 最大重连次数

    # ── 数据异常 ──
    data_gap_max_seconds: float = 120.0      # 数据间隔 >2 分钟 → 断流
    max_source_divergence_pct: float = 1.0   # 双源价差 >1% → 分歧

    # ── 自动恢复 ──
    auto_resume_after_seconds: float = 600.0  # 10 分钟后自动解除熔断


@dataclass
class SystemHealth:
    """系统健康状态"""
    overall: str = "healthy"  # healthy | degraded | critical
    price_ok: bool = True
    api_ok: bool = True
    ws_ok: bool = True
    data_ok: bool = True
    active_anomalies: List[str] = field(default_factory=list)
    last_check_time: float = 0.0


class AnomalyDetector:
    """
    异常检测器 — 系统级健康监控

    三大检测维度 + 自动恢复逻辑
    """

    def __init__(self, config: AnomalyConfig = None):
        self.config = config or AnomalyConfig()

        # ── 历史缓冲区 ──
        self._price_history: Dict[str, deque] = {}     # symbol → [(timestamp, price), ...]
        self._volume_history: Dict[str, deque] = {}    # symbol → [volume, ...]
        self._last_price_time: Dict[str, float] = {}   # symbol → last update timestamp

        # ── API 状态 ──
        self._api_errors: deque = deque(maxlen=self.config.api_error_window)
        self._api_last_success: float = time.time()

        # ── WebSocket 状态 ──
        self._ws_last_heartbeat: Dict[str, float] = {}  # channel → last heartbeat
        self._ws_reconnect_count: Dict[str, int] = {}   # channel → reconnect attempts

        # ── 熔断状态 ──
        self._circuit_breaker_active: bool = False
        self._circuit_breaker_reason: str = ""
        self._circuit_breaker_until: float = 0.0

        # ── 双数据源价格缓存（交叉验证用）──
        self._source_prices: Dict[str, Dict[str, float]] = {}  # symbol → {source: price}

    # ═══════════════════════════════
    # 1. 价格异常检测
    # ═══════════════════════════════

    def check_price_spike(self, symbol: str, current_price: float,
                          timestamp: float = None) -> AnomalyResult:
        """
        价格断崖/插针检测

        逻辑:
          维护最近 5 分钟的价格序列
          计算 max(high) - min(low) 的变化幅度
          超过阈值 → 可能是插针或断崖
        """
        ts = timestamp or time.time()

        if symbol not in self._price_history:
            self._price_history[symbol] = deque(maxlen=300)  # 最多 300 个 tick

        history = self._price_history[symbol]
        history.append((ts, current_price))
        self._last_price_time[symbol] = ts

        # 需要至少 2 个数据点
        if len(history) < 2:
            return AnomalyResult()

        # ── 5 分钟窗口内的极值 ──
        cutoff_5min = ts - 300
        recent = [p for t, p in history if t >= cutoff_5min]
        if len(recent) < 2:
            recent = [p for _, p in history]

        high = max(recent)
        low = min(recent)
        change_pct = abs(high - low) / low * 100

        # ── 1 分钟窗口 ──
        cutoff_1min = ts - 60
        recent_1min = [p for t, p in history if t >= cutoff_1min]
        if len(recent_1min) >= 2:
            change_1min = abs(recent_1min[-1] - recent_1min[0]) / recent_1min[0] * 100
        else:
            change_1min = 0

        # ── 判断 ──
        if change_pct >= self.config.price_spike_pct_5min:
            return AnomalyResult(
                triggered=True,
                anomaly_type=AnomalyType.PRICE_SPIKE,
                severity=AnomalySeverity.CRITICAL,
                reason=f'{symbol} 5 分钟内波动 {change_pct:.1f}% ≥ {self.config.price_spike_pct_5min}%',
                detail={
                    'symbol': symbol, 'change_5min_pct': round(change_pct, 2),
                    'high': round(high, 2), 'low': round(low, 2),
                    'current': current_price,
                },
            )

        if change_1min >= self.config.price_spike_pct_1min:
            return AnomalyResult(
                triggered=True,
                anomaly_type=AnomalyType.PRICE_SPIKE,
                severity=AnomalySeverity.WARNING,
                reason=f'{symbol} 1 分钟内波动 {change_1min:.1f}% ≥ {self.config.price_spike_pct_1min}%',
                detail={
                    'symbol': symbol, 'change_1min_pct': round(change_1min, 2),
                    'current': current_price,
                },
            )

        return AnomalyResult()

    def check_volume_spike(self, symbol: str, current_volume: float) -> AnomalyResult:
        """
        成交量突增检测

        逻辑:
          维护最近 N 根 K 线的成交量
          当前量 > N 日均值的 X 倍 → 有人砸盘/抢筹
        """
        if symbol not in self._volume_history:
            self._volume_history[symbol] = deque(
                maxlen=self.config.volume_lookback_bars
            )

        history = self._volume_history[symbol]

        # 刚启动时数据不足，先积累
        if len(history) < 5:
            history.append(current_volume)
            return AnomalyResult()

        avg_volume = sum(history) / len(history)
        history.append(current_volume)

        if avg_volume > 0 and current_volume > avg_volume * self.config.volume_spike_multiplier:
            ratio = current_volume / avg_volume
            return AnomalyResult(
                triggered=True,
                anomaly_type=AnomalyType.VOLUME_SPIKE,
                severity=AnomalySeverity.WARNING,
                reason=f'{symbol} 成交量 {ratio:.1f}x 突增（均值 {avg_volume:.0f}）',
                detail={
                    'symbol': symbol, 'current_volume': current_volume,
                    'avg_volume': round(avg_volume, 0), 'ratio': round(ratio, 1),
                },
            )

        return AnomalyResult()

    def check_price_stale(self, symbol: str, timestamp: float = None) -> AnomalyResult:
        """
        价格停滞检测 — 数据流是否断了

        如果某个标的的价格超过 N 秒没更新，可能是:
          - WebSocket 断了但没通知
          - 交易所限制了该交易对
          - 网络问题
        """
        ts = timestamp or time.time()
        last_ts = self._last_price_time.get(symbol, ts)

        gap = ts - last_ts
        if gap > self.config.price_stale_seconds:
            return AnomalyResult(
                triggered=True,
                anomaly_type=AnomalyType.PRICE_STALE,
                severity=AnomalySeverity.WARNING,
                reason=f'{symbol} 价格 {gap:.0f} 秒未更新 (> {self.config.price_stale_seconds:.0f}s)',
                detail={'symbol': symbol, 'seconds_stale': round(gap, 1)},
            )

        return AnomalyResult()

    def check_spread_anomaly(self, symbol: str, bid: float, ask: float) -> AnomalyResult:
        """
        买卖价差异常 — 流动性枯竭信号

        价差突然扩大 → 做市商撤退 / 流动性枯竭
        此时下单容易滑点巨大
        """
        if bid <= 0 or ask <= 0:
            return AnomalyResult()

        spread_pct = (ask - bid) / bid * 100

        if spread_pct > self.config.max_spread_pct:
            return AnomalyResult(
                triggered=True,
                anomaly_type=AnomalyType.SPREAD_WIDENING,
                severity=AnomalySeverity.WARNING,
                reason=f'{symbol} 买卖价差 {spread_pct:.2f}% > {self.config.max_spread_pct}%',
                detail={'symbol': symbol, 'bid': bid, 'ask': ask,
                        'spread_pct': round(spread_pct, 2)},
            )

        return AnomalyResult()

    # ═══════════════════════════════
    # 2. 连接异常检测
    # ═══════════════════════════════

    def record_api_success(self):
        """记录 API 调用成功"""
        self._api_errors.append(False)
        self._api_last_success = time.time()

    def record_api_error(self, error_msg: str = "") -> AnomalyResult:
        """
        记录 API 调用失败 → 检查是否需要熔断

        返回值: 如果错误率过高，返回 CRITICAL 级别的异常
        """
        self._api_errors.append(True)

        error_count = sum(self._api_errors)
        total = len(self._api_errors)
        error_rate = error_count / total if total > 0 else 0

        if total >= 5 and error_rate >= self.config.api_error_rate_threshold:
            return AnomalyResult(
                triggered=True,
                anomaly_type=AnomalyType.API_ERROR_RATE,
                severity=AnomalySeverity.CRITICAL,
                reason=f'API 错误率 {error_rate:.0%} (最近 {total} 次中 {error_count} 次失败)',
                detail={'error_rate': round(error_rate, 2), 'total': total,
                        'errors': error_count, 'last_error': error_msg},
            )

        return AnomalyResult()

    def check_api_timeout(self, request_start: float,
                          timeout: float = None) -> AnomalyResult:
        """
        API 超时检测

        单次请求超时 → 重试
        连续超时 → 切换备用端点 → 熔断
        """
        threshold = timeout or self.config.api_timeout_seconds
        elapsed = time.time() - request_start

        if elapsed > threshold:
            return AnomalyResult(
                triggered=True,
                anomaly_type=AnomalyType.API_TIMEOUT,
                severity=AnomalySeverity.WARNING,
                reason=f'API 超时: {elapsed:.1f}s > {threshold}s',
                detail={'elapsed': round(elapsed, 1), 'threshold': threshold},
            )

        return AnomalyResult()

    # ── WebSocket ──

    def record_ws_heartbeat(self, channel: str):
        """记录 WebSocket 心跳"""
        self._ws_last_heartbeat[channel] = time.time()
        # 心跳到了 → 重连计数器重置
        self._ws_reconnect_count[channel] = 0

    def check_ws_health(self, channel: str = "default") -> AnomalyResult:
        """
        WebSocket 健康检查

        超过 heartbeat_timeout 没有心跳 → 断连
        """
        last_hb = self._ws_last_heartbeat.get(channel, 0)
        if last_hb == 0:
            return AnomalyResult()  # 还没开始，不算异常

        gap = time.time() - last_hb
        if gap > self.config.ws_heartbeat_timeout:
            return AnomalyResult(
                triggered=True,
                anomaly_type=AnomalyType.WS_HEARTBEAT_LOST,
                severity=AnomalySeverity.CRITICAL,
                reason=f'WebSocket [{channel}] 心跳丢失 {gap:.0f}s (> {self.config.ws_heartbeat_timeout}s)',
                detail={'channel': channel, 'seconds_since_hb': round(gap, 1)},
            )

        return AnomalyResult()

    def record_ws_reconnect(self, channel: str) -> AnomalyResult:
        """
        记录 WebSocket 重连 → 检查是否超过最大重连次数
        """
        count = self._ws_reconnect_count.get(channel, 0) + 1
        self._ws_reconnect_count[channel] = count

        if count >= self.config.ws_reconnect_max_attempts:
            return AnomalyResult(
                triggered=True,
                anomaly_type=AnomalyType.WS_DISCONNECT,
                severity=AnomalySeverity.CRITICAL,
                reason=f'WebSocket [{channel}] 重连 {count} 次失败，已达上限',
                detail={'channel': channel, 'attempts': count,
                        'max_attempts': self.config.ws_reconnect_max_attempts},
            )

        return AnomalyResult(
            triggered=True,
            anomaly_type=AnomalyType.WS_DISCONNECT,
            severity=AnomalySeverity.WARNING,
            reason=f'WebSocket [{channel}] 重连第 {count} 次',
            detail={'channel': channel, 'attempts': count},
        )

    # ═══════════════════════════════
    # 3. 数据异常检测
    # ═══════════════════════════════

    def check_data_gap(self, symbol: str, current_ts: float,
                       prev_ts: float = None) -> AnomalyResult:
        """
        数据断流检测

        K 线间隔突然变大 → 数据源可能断了
        """
        if prev_ts is None:
            return AnomalyResult()

        gap = current_ts - prev_ts
        if gap > self.config.data_gap_max_seconds:
            return AnomalyResult(
                triggered=True,
                anomaly_type=AnomalyType.DATA_GAP,
                severity=AnomalySeverity.WARNING,
                reason=f'{symbol} 数据间隔 {gap:.0f}s (> {self.config.data_gap_max_seconds:.0f}s)',
                detail={'symbol': symbol, 'gap_seconds': round(gap, 1),
                        'prev_ts': prev_ts, 'current_ts': current_ts},
            )

        return AnomalyResult()

    def check_source_divergence(self, symbol: str, price_binance: float,
                                price_okx: float) -> AnomalyResult:
        """
        多数据源交叉验证 — 价差过大

        币安和 OKX 的价格差了 2% → 其中一个交易所可能有问题
        或者市场正在剧烈波动（此时也应该暂停交易）
        """
        if price_binance <= 0 or price_okx <= 0:
            return AnomalyResult()

        divergence = abs(price_binance - price_okx) / min(price_binance, price_okx) * 100

        if divergence > self.config.max_source_divergence_pct:
            return AnomalyResult(
                triggered=True,
                anomaly_type=AnomalyType.SOURCE_DIVERGENCE,
                severity=AnomalySeverity.WARNING,
                reason=f'{symbol} 双源价差 {divergence:.2f}% (币安={price_binance}, OKX={price_okx})',
                detail={'symbol': symbol, 'binance': price_binance,
                        'okx': price_okx, 'divergence_pct': round(divergence, 2)},
            )

        return AnomalyResult()

    def check_timestamp_regression(self, symbol: str,
                                   current_ts: float) -> AnomalyResult:
        """
        时间戳倒退检测

        新数据的时间戳比旧数据还早 → 数据错乱
        """
        last_ts = self._last_price_time.get(f"{symbol}_ts", 0)
        self._last_price_time[f"{symbol}_ts"] = current_ts

        if last_ts > 0 and current_ts < last_ts:
            return AnomalyResult(
                triggered=True,
                anomaly_type=AnomalyType.TIMESTAMP_REGRESSION,
                severity=AnomalySeverity.WARNING,
                reason=f'{symbol} 时间戳倒退: {current_ts} < {last_ts}',
                detail={'symbol': symbol, 'current_ts': current_ts,
                        'prev_ts': last_ts},
            )

        return AnomalyResult()

    # ═══════════════════════════════
    # 综合健康检查
    # ═══════════════════════════════

    def system_health_check(self, symbols: List[str] = None) -> SystemHealth:
        """
        综合系统健康检查

        返回整体健康状态: healthy | degraded | critical
        """
        health = SystemHealth(last_check_time=time.time())

        # ── 检查价格数据新鲜度 ──
        if symbols:
            stale_count = 0
            for sym in symbols:
                last_ts = self._last_price_time.get(sym, 0)
                if time.time() - last_ts > self.config.price_stale_seconds:
                    stale_count += 1
            if stale_count > len(symbols) // 2:
                health.price_ok = False
                health.active_anomalies.append(f'{stale_count}/{len(symbols)} 标的价格停滞')

        # ── 检查 API 健康 ──
        api_errors = sum(self._api_errors)
        if len(self._api_errors) >= 5 and api_errors / len(self._api_errors) >= 0.5:
            health.api_ok = False
            health.active_anomalies.append(f'API 错误率 {api_errors}/{len(self._api_errors)}')

        if time.time() - self._api_last_success > 300:
            health.api_ok = False
            health.active_anomalies.append('API 超过 5 分钟无成功响应')

        # ── 检查 WebSocket ──
        for channel, last_hb in self._ws_last_heartbeat.items():
            if time.time() - last_hb > self.config.ws_heartbeat_timeout:
                health.ws_ok = False
                health.active_anomalies.append(f'WebSocket [{channel}] 心跳丢失')

        # ── 综合判定 ──
        critical_flags = sum([not health.price_ok, not health.api_ok, not health.ws_ok])
        if critical_flags == 0:
            health.overall = "healthy"
        elif critical_flags == 1:
            health.overall = "degraded"
        else:
            health.overall = "critical"

        return health

    # ═══════════════════════════════
    # 全量检查（所有维度一次跑完）
    # ═══════════════════════════════

    def check_all(self, symbol: str, bar: dict = None,
                  ws_channel: str = "default") -> List[AnomalyResult]:
        """
        对单个标的全量异常检查

        返回: 所有触发的异常列表（可能多个同时触发）
        """
        results = []
        bar = bar or {}

        # ── 价格 ──
        if 'close' in bar:
            r = self.check_price_spike(symbol, bar['close'])
            if r.triggered:
                results.append(r)

            r = self.check_price_stale(symbol)
            if r.triggered:
                results.append(r)

        # ── 成交量 ──
        if 'volume' in bar:
            r = self.check_volume_spike(symbol, bar['volume'])
            if r.triggered:
                results.append(r)

        # ── 买卖价差 ──
        if 'bid' in bar and 'ask' in bar:
            r = self.check_spread_anomaly(symbol, bar['bid'], bar['ask'])
            if r.triggered:
                results.append(r)

        # ── 双源分歧 ──
        if 'close_binance' in bar and 'close_okx' in bar:
            r = self.check_source_divergence(symbol, bar['close_binance'],
                                             bar['close_okx'])
            if r.triggered:
                results.append(r)

        # ── WebSocket ──
        r = self.check_ws_health(ws_channel)
        if r.triggered:
            results.append(r)

        return results

    # ═══════════════════════════════
    # 熔断 & 恢复
    # ═══════════════════════════════

    def trigger_circuit_breaker(self, reason: str, duration_seconds: float = None):
        """触发系统熔断"""
        duration = duration_seconds or self.config.auto_resume_after_seconds
        self._circuit_breaker_active = True
        self._circuit_breaker_reason = reason
        self._circuit_breaker_until = time.time() + duration

    def is_circuit_breaker_active(self) -> bool:
        """检查熔断是否激活"""
        if self._circuit_breaker_active:
            if time.time() >= self._circuit_breaker_until:
                self._circuit_breaker_active = False
                self._circuit_breaker_reason = ""
                return False
            return True
        return False

    def get_circuit_breaker_status(self) -> Dict:
        """获取熔断状态"""
        return {
            'active': self._circuit_breaker_active,
            'reason': self._circuit_breaker_reason,
            'remaining_seconds': max(0, self._circuit_breaker_until - time.time()),
        }

    # ═══════════════════════════════
    # 状态查询
    # ═══════════════════════════════

    def get_status(self) -> Dict:
        """获取异常检测器完整状态"""
        health = self.system_health_check()
        return {
            'health': health.overall,
            'circuit_breaker': self.get_circuit_breaker_status(),
            'api_error_rate': (sum(self._api_errors) / len(self._api_errors)
                               if self._api_errors else 0),
            'ws_channels': {ch: f'{time.time() - ts:.0f}s ago'
                            for ch, ts in self._ws_last_heartbeat.items()},
            'active_anomalies': health.active_anomalies,
        }

    def print_status(self):
        """打印异常检测状态"""
        s = self.get_status()
        health_icon = {'healthy': '[OK]', 'degraded': '[!!]', 'critical': '[XX]'}
        cb_icon = '[STOP]' if s['circuit_breaker']['active'] else '[GO]'
        print(f"\n  [异常检测状态]")
        print(f"  系统健康: {health_icon.get(s['health'], '?')} {s['health']}")
        print(f"  熔断状态: {cb_icon}")
        if s['circuit_breaker']['active']:
            print(f"    原因: {s['circuit_breaker']['reason']}")
            print(f"    剩余: {s['circuit_breaker']['remaining_seconds']:.0f}s")
        print(f"  API 错误率: {s['api_error_rate']:.0%}")
        if s['active_anomalies']:
            for a in s['active_anomalies']:
                print(f"  [!] {a}")


# ═══════════════════════════════
# API 重试装饰器（用于封装交易所 API 调用）
# ═══════════════════════════════

class APIRetryHandler:
    """
    API 调用重试处理器

    用法:
      handler = APIRetryHandler(detector=ad)
      result = handler.call_with_retry(
          lambda: exchange.fetch_ticker('BTC/USDT'),
          'fetch_ticker',
      )
    """

    def __init__(self, detector: AnomalyDetector, max_retries: int = 3,
                 base_delay: float = 1.0):
        self.detector = detector
        self.max_retries = max_retries
        self.base_delay = base_delay

    def call_with_retry(self, fn, operation_name: str = "api_call"):
        """
        带重试和异常上报的 API 调用

        返回: (result, error)
          - 成功: (data, None)
          - 失败: (None, error_message)
        """
        last_error = None

        for attempt in range(self.max_retries):
            start = time.time()

            try:
                result = fn()
                self.detector.record_api_success()
                return result, None
            except Exception as e:
                last_error = str(e)
                anomaly = self.detector.record_api_error(last_error)

                # 检查超时
                self.detector.check_api_timeout(start)

                # 如果错误率过高，不再重试
                if anomaly.severity == AnomalySeverity.CRITICAL:
                    return None, f"API 熔断: {anomaly.reason}"

                # 指数退避
                if attempt < self.max_retries - 1:
                    delay = self.base_delay * (2 ** attempt)
                    time.sleep(delay)

        return None, f"重试 {self.max_retries} 次后仍失败: {last_error}"


# ═══════════════════════════════
# 快速测试
# ═══════════════════════════════

if __name__ == '__main__':
    print("=" * 60)
    print("  异常检测模块 — Day 10")
    print("=" * 60)

    ad = AnomalyDetector()

    # ── 场景 1: 价格断崖 ──
    print("\n  [场景 1] 价格断崖 — BTC 5 分钟跌 15%")
    # 模拟价格序列
    prices = [65000, 64800, 64200, 63000, 61000, 58000, 55250]
    for i, p in enumerate(prices):
        ts = time.time() - (len(prices) - i) * 30  # 每 30 秒一个点
        r = ad.check_price_spike('BTC/USDT', p, ts)
        if r.triggered:
            print(f"  触发! {r.reason} (severity={r.severity.value})")

    # ── 场景 2: 成交量突增 ──
    print("\n  [场景 2] 成交量突增")
    normal_volumes = [1000, 1200, 900, 1100, 1050, 950, 1150, 1000, 1080, 1020]
    for v in normal_volumes:
        ad.check_volume_spike('BTC/USDT', v)
    r = ad.check_volume_spike('BTC/USDT', 8000)  # 8x 突增
    print(f"  触发: {r.triggered}, {r.reason}")

    # ── 场景 3: API 错误率 ──
    print("\n  [场景 3] API 连续失败 → 熔断")
    for i in range(6):
        ad.record_api_success()
    for i in range(5):
        r = ad.record_api_error(f"Connection reset #{i+1}")
        if r.triggered:
            print(f"  触发! {r.reason}")
    print(f"  API 错误率: {sum(ad._api_errors)}/{len(ad._api_errors)}")

    # ── 场景 4: WebSocket 心跳丢失 ──
    print("\n  [场景 4] WebSocket 心跳丢失")
    ad.record_ws_heartbeat('ticker')
    # 模拟心跳在 100 秒前（超过 90s 超时）
    ad._ws_last_heartbeat['ticker'] = time.time() - 100
    r = ad.check_ws_health('ticker')
    print(f"  触发: {r.triggered}, {r.reason}")

    # ── 场景 5: 双源分歧 ──
    print("\n  [场景 5] 双数据源分歧")
    r = ad.check_source_divergence('BTC/USDT', 65000, 67000)
    print(f"  触发: {r.triggered}, {r.reason}")

    # ── API 重试 ──
    print("\n  [场景 6] API 重试处理器")
    handler = APIRetryHandler(ad, max_retries=3, base_delay=0.1)
    fail_count = [0]

    def mock_api():
        fail_count[0] += 1
        if fail_count[0] < 4:
            raise ConnectionError("模拟网络错误")
        return {"price": 65000}

    result, error = handler.call_with_retry(mock_api, 'fetch_ticker')
    print(f"  重试 3 次后: result={result}, error={error}")

    # ── 综合健康 ──
    print("\n  [系统健康]")
    ad._last_price_time['BTC/USDT'] = time.time()
    health = ad.system_health_check(['BTC/USDT'])
    print(f"  整体: {health.overall}")
    print(f"  活跃异常: {health.active_anomalies}")

    print("\n  [OK] Day 10 异常检测模块就绪")
