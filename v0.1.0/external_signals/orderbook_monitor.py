"""
订单簿分析模块 — Day 20

做市商行为信号识别：
  1. 假挂单（Spoofing）— 巨量挂单存活 < 5分钟
  2. 冰山订单 — 同一价位反复出现相同大小的挂单
  3. 深度不对称 — 买卖盘深度比 > 3:1
  4. 做市商建仓/出货特征 — 窄幅横盘 + 单侧深度持续增加

硬性规则（来自 quant-orderbook skill）：
  - 不做高频 — 分析分钟级快照，不做 tick 级实时处理
  - 挂单行为比挂单量重要 — 存活时间/撤单模式/补单频率
  - 结果不直接交易 — 纳入综合评分，作为风控因子
"""

import os
import time
from dataclasses import dataclass, field, asdict
from datetime import datetime, timedelta
from enum import Enum
from typing import Optional
from collections import defaultdict
import json


# ═══════════════════════════════
# 数据类型
# ═══════════════════════════════

class MMSignal(str, Enum):
    """做市商信号类型"""
    MM_ACCUMULATING = "MM_ACCUMULATING"      # 建仓中
    MM_DISTRIBUTING = "MM_DISTRIBUTING"      # 出货中
    SPOOFING_DETECTED = "SPOOFING_DETECTED"   # 检测到假挂单
    ICEBERG_DETECTED = "ICEBERG_DETECTED"     # 检测到冰山订单
    DEPTH_IMBALANCE = "DEPTH_IMBALANCE"      # 深度严重不对称
    FAKE_SUPPORT = "FAKE_SUPPORT"            # 假支撑（买墙+价格不涨）
    FAKE_RESISTANCE = "FAKE_RESISTANCE"      # 假压力（卖墙+价格不跌）
    LIQUIDITY_THINNING = "LIQUIDITY_THINNING"  # 流动性正在枯竭


@dataclass
class DepthLevel:
    """一档深度"""
    price: float
    amount: float           # base currency 数量


@dataclass
class DepthSnapshot:
    """订单簿快照"""
    symbol: str
    timestamp: str
    bids: list[DepthLevel]       # 买盘前 20 档
    asks: list[DepthLevel]       # 卖盘前 20 档
    mid_price: float = 0.0

    def __post_init__(self):
        if self.mid_price == 0.0 and self.bids and self.asks:
            self.mid_price = (self.bids[0].price + self.asks[0].price) / 2

    def bid_depth(self, levels: int = 10) -> float:
        """买盘深度（USDT 计）"""
        return sum(l.price * l.amount for l in self.bids[:levels])

    def ask_depth(self, levels: int = 10) -> float:
        """卖盘深度（USDT 计）"""
        return sum(l.price * l.amount for l in self.asks[:levels])

    def spread_pct(self) -> float:
        """买卖价差 %"""
        if not self.bids or not self.asks:
            return 1.0
        return (self.asks[0].price - self.bids[0].price) / self.mid_price * 100

    def depth_imbalance_ratio(self) -> float:
        """买盘/卖盘深度比"""
        ask_d = self.ask_depth()
        if ask_d == 0:
            return float('inf')
        return self.bid_depth() / ask_d


@dataclass
class SpoofingEvent:
    """假挂单事件"""
    timestamp: str
    symbol: str
    side: str                     # bids / asks
    price: float
    amount: float
    lifetime_seconds: float       # 挂单存活时间
    avg_amount: float             # 该价位平均挂单量（对比用）


# ═══════════════════════════════
# 订单簿监控器
# ═══════════════════════════════

class OrderBookMonitor:
    """
    订单簿分析器

    采集策略：
      - 每分钟抓一次深度快照（前 20 档）
      - 极端行情自动提高频率到 10 秒
      - 实时监控只做异常检测，不存全量 tick
    """

    # 异常检测阈值
    SPOOFING_SIZE_MULTIPLIER = 5.0     # 挂单量 > 均值 N 倍
    SPOOFING_MAX_LIFETIME = 300        # 存活 < 5 分钟 = 假单
    ICEBERG_REPEAT_COUNT = 10          # 同价位挂单出现 > N 次/小时
    DEPTH_IMBALANCE_THRESHOLD = 3.0    # 买卖深度比 > N:1
    MM_ACCUMULATION_HOURS = 6          # 连续 N 小时单向深度增加

    def __init__(self, data_dir: Optional[str] = None):
        self.snapshots: dict[str, list[DepthSnapshot]] = defaultdict(list)  # symbol → snapshots
        self.spoofing_events: list[SpoofingEvent] = []
        self.mm_signals: list[dict] = []
        self.data_dir = data_dir or os.path.join(
            os.path.dirname(os.path.abspath(__file__)), "..", "data", "orderbook"
        )
        # 内存中只保留最近 24h
        self._max_snapshots_per_symbol = 1440  # 24h * 60min

    # ═══════════════════════════════
    # 快照采集
    # ═══════════════════════════════

    def collect_snapshot(self, symbol: str, bids: list[list[float]],
                          asks: list[list[float]]) -> DepthSnapshot:
        """
        记录订单簿快照

        bids: [[price, amount], ...] — 从币安 depth API 直接传入
        asks: [[price, amount], ...]
        """
        snapshot = DepthSnapshot(
            symbol=symbol.upper(),
            timestamp=datetime.utcnow().isoformat(),
            bids=[DepthLevel(price=p, amount=a) for p, a in bids[:20]],
            asks=[DepthLevel(price=p, amount=a) for p, a in asks[:20]],
        )
        self.snapshots[symbol].append(snapshot)

        # 限制内存
        if len(self.snapshots[symbol]) > self._max_snapshots_per_symbol:
            self.snapshots[symbol] = self.snapshots[symbol][-self._max_snapshots_per_symbol:]

        return snapshot

    def get_recent_snapshots(self, symbol: str, hours: int = 1) -> list[DepthSnapshot]:
        """获取最近的订单簿快照"""
        symbol = symbol.upper()
        if symbol not in self.snapshots:
            return []
        now = datetime.utcnow()
        cutoff = now - timedelta(hours=hours)
        return [
            s for s in self.snapshots[symbol]
            if datetime.fromisoformat(s.timestamp) >= cutoff
        ]

    # ═══════════════════════════════
    # 信号检测
    # ═══════════════════════════════

    def detect_spoofing(self, symbol: str, hours: int = 1) -> list[SpoofingEvent]:
        """
        检测假挂单

        条件：
          1. 某个价位出现巨量挂单（> 平均深度 5 倍）
          2. 挂单存活时间 < 5 分钟
          3. 撤单后同价位没有重新挂出
        """
        snapshots = self.get_recent_snapshots(symbol, hours)
        if len(snapshots) < 3:
            return []

        events = []

        # 计算每个价位的历史平均挂单量
        price_levels_bids: dict[float, list[float]] = defaultdict(list)
        price_levels_asks: dict[float, list[float]] = defaultdict(list)

        for s in snapshots:
            for bid in s.bids:
                price_levels_bids[bid.price].append(bid.amount)
            for ask in s.asks:
                price_levels_asks[ask.price].append(ask.amount)

        # 检查最新快照
        latest = snapshots[-1]
        for side, levels, price_map in [
            ("bids", latest.bids, price_levels_bids),
            ("asks", latest.asks, price_levels_asks),
        ]:
            for level in levels:
                history = price_map.get(level.price, [])
                if len(history) < 2:
                    continue
                avg_amount = sum(history[:-1]) / (len(history) - 1)
                if avg_amount > 0 and level.amount > avg_amount * self.SPOOFING_SIZE_MULTIPLIER:
                    # 追踪这个挂单的存活时间
                    lifetime = self._track_level_lifetime(snapshots, side, level.price, level.amount)
                    if lifetime < self.SPOOFING_MAX_LIFETIME:
                        event = SpoofingEvent(
                            timestamp=latest.timestamp,
                            symbol=symbol,
                            side=side,
                            price=level.price,
                            amount=level.amount,
                            lifetime_seconds=lifetime,
                            avg_amount=avg_amount,
                        )
                        events.append(event)
                        self.spoofing_events.append(event)

        return events

    def detect_iceberg(self, symbol: str, hours: int = 1) -> list[dict]:
        """
        检测冰山订单

        条件：同一价位反复出现相同或相近大小（±10%）的挂单，出现 >10 次/小时
        """
        snapshots = self.get_recent_snapshots(symbol, hours)
        if len(snapshots) < 10:
            return []

        icebergs = []

        for side_name, side_key in [("bids", "bids"), ("asks", "asks")]:
            # 统计每个价位在每张快照中的出现
            price_appearances: dict[float, list[float]] = defaultdict(list)
            for s in snapshots:
                seen_prices = set()
                for level in getattr(s, side_key):
                    price_appearances[level.price].append(level.amount)
                    seen_prices.add(level.price)

            for price, amounts in price_appearances.items():
                if len(amounts) >= self.ICEBERG_REPEAT_COUNT:
                    # 检查金额是否相近（冰山订单的特征）
                    avg_amount = sum(amounts) / len(amounts)
                    if avg_amount > 0:
                        similar = sum(
                            1 for a in amounts
                            if abs(a - avg_amount) / avg_amount < 0.10
                        )
                        if similar >= self.ICEBERG_REPEAT_COUNT:
                            icebergs.append({
                                "timestamp": snapshots[-1].timestamp,
                                "symbol": symbol,
                                "side": side_name,
                                "price": price,
                                "avg_amount": round(avg_amount, 4),
                                "occurrences": len(amounts),
                                "similar_ratio": round(similar / len(amounts), 2),
                                "interpretation": (
                                    "大规模吸筹（买单冰山）" if side_name == "bids"
                                    else "大规模出货（卖单冰山）"
                                ),
                            })

        return icebergs

    def analyze_depth_imbalance(self, symbol: str) -> dict:
        """
        分析买卖深度不对称

        关键：深度不对称必须和价格行为一起看才有意义
          - 买墙 + 价格不涨 = 假支撑
          - 卖墙 + 价格不跌 = 假压力
        """
        if symbol not in self.snapshots or not self.snapshots[symbol]:
            return {"bid_ask_ratio": 1.0, "status": "balanced", "signal": None}

        latest = self.snapshots[symbol][-1]
        ratio = latest.depth_imbalance_ratio()
        spread = latest.spread_pct()

        signal = None
        if ratio > self.DEPTH_IMBALANCE_THRESHOLD:
            signal = "buy_wall"  # 需要结合价格行为判断真假
        elif ratio < 1.0 / self.DEPTH_IMBALANCE_THRESHOLD:
            signal = "sell_wall"

        return {
            "symbol": symbol,
            "bid_depth_usdt": round(latest.bid_depth(), 2),
            "ask_depth_usdt": round(latest.ask_depth(), 2),
            "bid_ask_ratio": round(ratio, 2),
            "spread_pct": round(spread, 4),
            "status": (
                "balanced" if 1/self.DEPTH_IMBALANCE_THRESHOLD < ratio < self.DEPTH_IMBALANCE_THRESHOLD
                else "buy_heavy" if ratio >= self.DEPTH_IMBALANCE_THRESHOLD
                else "sell_heavy"
            ),
            "raw_signal": signal,
            "needs_price_confirmation": signal is not None,
        }

    def detect_mm_accumulation(self, symbol: str, hours: int = 24) -> dict:
        """
        检测做市商建仓/出货特征

        建仓特征：
          1. 价格窄幅横盘（波动率 < 日均波动率的 50%）
          2. 买盘深度持续增加（做市商在下面接货）
          3. 卖盘深度没有明显变化
          4. 持续时间 > 6 小时

        出货特征：反之
        """
        snapshots = self.get_recent_snapshots(symbol, hours)
        if len(snapshots) < self.MM_ACCUMULATION_HOURS * 2:  # 至少需要足够数据点
            return {"signal": "INSUFFICIENT_DATA", "confidence": "low"}

        # 计算最近 6 小时的深度趋势
        now = datetime.utcnow()
        cutoff_recent = now - timedelta(hours=self.MM_ACCUMULATION_HOURS)
        cutoff_baseline = now - timedelta(hours=hours)

        recent_snaps = [s for s in snapshots
                        if datetime.fromisoformat(s.timestamp) >= cutoff_recent]
        baseline_snaps = [s for s in snapshots
                          if datetime.fromisoformat(s.timestamp) < cutoff_recent
                          and datetime.fromisoformat(s.timestamp) >= cutoff_baseline]

        if not recent_snaps or not baseline_snaps:
            return {"signal": "INSUFFICIENT_DATA", "confidence": "low"}

        # 深度趋势
        recent_bid_avg = sum(s.bid_depth() for s in recent_snaps) / len(recent_snaps)
        recent_ask_avg = sum(s.ask_depth() for s in recent_snaps) / len(recent_snaps)
        baseline_bid_avg = sum(s.bid_depth() for s in baseline_snaps) / len(baseline_snaps)
        baseline_ask_avg = sum(s.ask_depth() for s in baseline_snaps) / len(baseline_snaps)

        bid_change_pct = (recent_bid_avg - baseline_bid_avg) / baseline_bid_avg if baseline_bid_avg > 0 else 0
        ask_change_pct = (recent_ask_avg - baseline_ask_avg) / baseline_ask_avg if baseline_ask_avg > 0 else 0

        CHANGE_THRESHOLD = 0.15  # 15% 深度变化

        signal = "NO_SIGNAL"
        confidence = "low"

        if bid_change_pct > CHANGE_THRESHOLD and ask_change_pct < CHANGE_THRESHOLD * 0.5:
            signal = "MM_ACCUMULATING"
            confidence = "medium"
        elif ask_change_pct > CHANGE_THRESHOLD and bid_change_pct < CHANGE_THRESHOLD * 0.5:
            signal = "MM_DISTRIBUTING"
            confidence = "medium"
        elif bid_change_pct > CHANGE_THRESHOLD and ask_change_pct > CHANGE_THRESHOLD:
            signal = "LIQUIDITY_INCREASING"  # 深度双向增加 = 做市商在双向报价
            confidence = "low"

        return {
            "symbol": symbol,
            "signal": signal,
            "confidence": confidence,
            "bid_depth_change_pct": round(bid_change_pct * 100, 1),
            "ask_depth_change_pct": round(ask_change_pct * 100, 1),
            "recent_bid_depth_avg": round(recent_bid_avg, 2),
            "recent_ask_depth_avg": round(recent_ask_avg, 2),
            "baseline_bid_depth_avg": round(baseline_bid_avg, 2),
            "baseline_ask_depth_avg": round(baseline_ask_avg, 2),
            "hours_analyzed": hours,
        }

    def check_key_levels(self, symbol: str, support: float,
                          resistance: float) -> dict:
        """
        在关键技术位检查订单簿行为

        支撑位附近：
          - 买单深度正常 → 支撑有效
          - 买单深度突然消失 → 支撑即将被破
          - 买单被大量吃穿 → 有人主动砸穿

        阻力位附近：同理
        """
        if symbol not in self.snapshots:
            return {"support_status": "unknown", "resistance_status": "unknown"}

        latest = self.snapshots[symbol][-1]
        mid = latest.mid_price

        # 支撑位检查 — 支撑以下 1% 范围内的买盘深度
        support_bids = [b for b in latest.bids if b.price >= support * 0.99]
        support_depth = sum(b.price * b.amount for b in support_bids)

        # 阻力位检查 — 阻力以上 1% 范围内的卖盘深度
        resistance_asks = [a for a in latest.asks if a.price <= resistance * 1.01]
        resistance_depth = sum(a.price * a.amount for a in resistance_asks)

        return {
            "symbol": symbol,
            "mid_price": mid,
            "support": support,
            "resistance": resistance,
            "support_depth_usdt": round(support_depth, 2),
            "resistance_depth_usdt": round(resistance_depth, 2),
            "support_status": (
                "holding" if support_depth > 50000
                else "weakening" if support_depth > 20000
                else "breaking"
            ),
            "resistance_status": (
                "holding" if resistance_depth > 50000
                else "weakening" if resistance_depth > 20000
                else "breaking"
            ),
        }

    # ═══════════════════════════════
    # 综合评分
    # ═══════════════════════════════

    def get_orderbook_score(self, symbol: str) -> float:
        """
        订单簿综合评分 (0-10)

        用于信号融合 — 权重参考：
          ├── 假挂单检测     权重 15% → 分数越高 = 越诚实
          ├── 冰山订单       权重 20% → 有冰山是加分
          ├── 深度不对称     权重 15% → 和价格行为联动判断
          ├── 做市商行为     权重 35% → 最重要
          └── 价差/流动性    权重 15% → 基本健康度
        """
        score = 5.0  # 基线

        # 1. 假挂单 — 扣分
        spoofing = self.detect_spoofing(symbol, hours=1)
        score -= min(len(spoofing) * 2, 3)

        # 2. 冰山订单 — 加分（有真实大单在运作）
        icebergs = self.detect_iceberg(symbol, hours=1)
        score += min(len(icebergs) * 1.5, 3)

        # 3. 深度不对称
        imbalance = self.analyze_depth_imbalance(symbol)
        if imbalance["raw_signal"] in ("buy_wall", "sell_wall"):
            score -= 1  # 不对称 = 风险（需结合价格验证）

        # 4. 做市商行为 — 最重要
        mm = self.detect_mm_accumulation(symbol, hours=24)
        if mm["signal"] == "MM_ACCUMULATING":
            score += 2.5
        elif mm["signal"] == "MM_DISTRIBUTING":
            score -= 2.5
        elif mm["signal"] == "LIQUIDITY_INCREASING":
            score += 1.5

        # 5. 价差 — 基本健康度
        if imbalance["spread_pct"] < 0.05:
            score += 1.0  # 流动性极好
        elif imbalance["spread_pct"] > 0.5:
            score -= 2.0  # 流动性差

        return max(0, min(10, score))

    # ═══════════════════════════════
    # 风控集成
    # ═══════════════════════════════

    def get_risk_action(self, symbol: str) -> dict:
        """
        根据订单簿评分返回风控动作

        评分 8-10 → 正常交易
        评分 5-7  → 降仓位 30%
        评分 0-4  → 暂停该币种交易
        """
        score = self.get_orderbook_score(symbol)

        if score >= 8:
            return {"action": "normal", "position_multiplier": 1.0, "score": score}
        elif score >= 5:
            return {"action": "reduce", "position_multiplier": 0.7, "score": score,
                    "reason": "订单簿有可疑信号，自动降仓 30%"}
        else:
            return {"action": "pause", "position_multiplier": 0.0, "score": score,
                    "reason": "订单簿严重异常，暂停该币种交易"}

    # ═══════════════════════════════
    # 辅助
    # ═══════════════════════════════

    def _track_level_lifetime(self, snapshots: list[DepthSnapshot],
                               side: str, price: float, amount: float) -> float:
        """追踪某个价位的挂单存活时间（秒）"""
        appearances = []
        for s in snapshots:
            levels = s.bids if side == "bids" else s.asks
            found = False
            for level in levels:
                if abs(level.price - price) / price < 0.001:  # 1bp 内视为同价位
                    if abs(level.amount - amount) / amount < 0.2:  # ±20% 视为同一挂单
                        appearances.append(s.timestamp)
                        found = True
                        break
            if not found and appearances:
                break  # 挂单消失

        if len(appearances) < 2:
            return 0

        t0 = datetime.fromisoformat(appearances[0])
        t1 = datetime.fromisoformat(appearances[-1])
        return (t1 - t0).total_seconds()

    def get_all_signals(self, symbol: str) -> dict:
        """获取所有订单簿信号（用于报告）"""
        return {
            "orderbook_score": round(self.get_orderbook_score(symbol), 1),
            "spoofing_count": len(self.detect_spoofing(symbol)),
            "iceberg_count": len(self.detect_iceberg(symbol)),
            "depth_imbalance": self.analyze_depth_imbalance(symbol),
            "mm_activity": self.detect_mm_accumulation(symbol),
            "risk_action": self.get_risk_action(symbol),
        }

    def get_stats(self) -> dict:
        return {
            "monitored_symbols": len(self.snapshots),
            "total_snapshots": sum(len(v) for v in self.snapshots.values()),
            "total_spoofing_events": len(self.spoofing_events),
        }
