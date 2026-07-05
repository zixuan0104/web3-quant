"""
币安 Alpha + 二线所上币监控 — Day 20

监控维度：
  1. 币安上币四阶段模型（传闻→公告→上线→沉淀）
  2. 二线交易所（MEXC/Bitget/Bybit/KuCoin）上币公告
  3. 做市商质量评分
  4. 暴涨前共性征兆评分

硬性规则（来自 quant-binance-alpha skill）：
  - 不猜上币 — 不在传闻期重仓赌
  - 不做上线首日 — 噪声占主导
  - 四阶段模型 — 机会在沉淀期的二次确认
  - 提醒 ≠ 下单 — 一切分析走 Telegram，不自动下单
"""

import os
import json
import time
from dataclasses import dataclass, field, asdict
from datetime import datetime, timedelta
from enum import Enum
from typing import Optional
from collections import defaultdict


# ═══════════════════════════════
# 数据类型
# ═══════════════════════════════

class ListingPhase(str, Enum):
    """上币四阶段"""
    RUMOR = "rumor"            # 传闻期 — 社区传"要上币安"
    ANNOUNCED = "announced"    # 公告期 — 官方确认
    LAUNCH_DAY = "launch_day"  # 上线首日 — 高波动
    SETTLING = "settling"      # 沉淀期 — 3-14 天，机会区


class Exchange(str, Enum):
    """交易所"""
    BINANCE = "binance"
    MEXC = "mexc"
    BITGET = "bitget"
    BYBIT = "bybit"
    KUCOIN = "kucoin"
    OKX = "okx"


@dataclass
class ListingEvent:
    """上币事件"""
    token: str
    exchange: Exchange
    phase: ListingPhase
    announce_time: str           # 公告时间
    listing_time: Optional[str]  # 实际上线时间
    trading_pairs: list[str] = field(default_factory=list)
    current_stage_days: int = 0  # 当前距上线天数
    mm_quality_score: Optional[float] = None  # 做市商质量评分 (0-10)
    pre_pump_score: Optional[float] = None    # 暴涨前征兆评分 (0-7)
    fake_pump_risk: float = 0.0               # 假暴涨风险 (0-10)
    notes: str = ""


# 交易所上币速度与信号意义
EXCHANGE_PROFILES = {
    Exchange.MEXC: {
        "listing_speed": "小时内",
        "signal_strength": 2,       # 有钱就能上，信号弱
        "review_quality": "low",
        "scam_risk": "high",        # 流水线产品多
        "weight": 5,                # 融合权重（满分 15 中的 5）
    },
    Exchange.BITGET: {
        "listing_speed": "1-3 天",
        "signal_strength": 4,
        "review_quality": "medium",
        "scam_risk": "medium",
        "weight": 5,
    },
    Exchange.BYBIT: {
        "listing_speed": "3-7 天",
        "signal_strength": 6,
        "review_quality": "high",
        "scam_risk": "low",
        "weight": 3,
    },
    Exchange.KUCOIN: {
        "listing_speed": "3-7 天",
        "signal_strength": 5,
        "review_quality": "high",
        "scam_risk": "low",
        "weight": 2,
    },
    Exchange.BINANCE: {
        "listing_speed": "7-30 天",
        "signal_strength": 10,       # 最强信号 — 但主力涨幅已过
        "review_quality": "highest",
        "scam_risk": "very_low",
        "weight": 20,                # 融合权重（满分 20）
    },
}

# 已知做市商信任度
MM_TRUST_SCORES = {
    "jump_crypto": 0.95,
    "wintermute": 0.90,
    "gsr": 0.85,
    "amber_group": 0.80,
    "dwf_labs": 0.65,       # DWF 争议较大
    "unknown": 0.40,        # 未知做市商 = 低信任
}


# ═══════════════════════════════
# 币安 Alpha 监控器
# ═══════════════════════════════

class BinanceAlphaMonitor:
    """
    币安上币流程 + 做市商质量追踪

    四阶段模型：
      传闻期 → 公告期 → 上线首日 → 沉淀期（你的机会区）
    """

    def __init__(self, data_dir: Optional[str] = None):
        self.listings: list[ListingEvent] = []
        self.watchlist: list[ListingEvent] = []  # 沉淀期观察列表
        self.data_dir = data_dir or os.path.join(
            os.path.dirname(os.path.abspath(__file__)), "..", "data", "listings"
        )

    # ═══════════════════════════════
    # 公告追踪
    # ═══════════════════════════════

    def record_announcement(self, token: str, exchange: Exchange,
                             announce_time: str, listing_time: Optional[str] = None,
                             trading_pairs: Optional[list[str]] = None) -> ListingEvent:
        """记录上币公告"""
        event = ListingEvent(
            token=token.upper(),
            exchange=exchange,
            phase=ListingPhase.ANNOUNCED,
            announce_time=announce_time,
            listing_time=listing_time,
            trading_pairs=trading_pairs or [f"{token}USDT"],
        )
        self.listings.append(event)
        return event

    def record_listing(self, token: str, exchange: Exchange,
                        listing_time: str, trading_pairs: Optional[list[str]] = None) -> ListingEvent:
        """记录实际上线"""
        event = ListingEvent(
            token=token.upper(),
            exchange=exchange,
            phase=ListingPhase.LAUNCH_DAY,
            announce_time=listing_time,  # 如果之前有公告，应更新
            listing_time=listing_time,
            trading_pairs=trading_pairs or [f"{token}USDT"],
            current_stage_days=0,
        )
        self.listings.append(event)
        return event

    def update_phases(self):
        """
        更新所有 listing 的阶段

        自动推进：LAUNCH_DAY → SETTLING (3天后)
        """
        now = datetime.utcnow()
        for event in self.listings:
            if event.listing_time and event.phase == ListingPhase.LAUNCH_DAY:
                listing_dt = datetime.fromisoformat(event.listing_time)
                days_since = (now - listing_dt).days
                event.current_stage_days = days_since
                if days_since >= 3:
                    event.phase = ListingPhase.SETTLING

    def get_settling_tokens(self) -> list[ListingEvent]:
        """获取沉淀期代币列表（上线 3-14 天）"""
        self.update_phases()
        return [
            e for e in self.listings
            if e.phase == ListingPhase.SETTLING
            and 3 <= e.current_stage_days <= 14
        ]

    # ═══════════════════════════════
    # 做市商质量评分
    # ═══════════════════════════════

    def score_mm_quality(self, token: str, depth_trend: str = "neutral",
                          known_mm: Optional[str] = None, spread_pct: float = 0.1,
                          price_stability: str = "neutral",
                          net_flow_signal: str = "neutral") -> dict:
        """
        做市商质量评分 (0-10)

        只有上线 ≥3 天后才评估

        五大维度：
          1. 订单簿深度趋势（权重 30%）
          2. 做市商已知地址活动（权重 25%）
          3. 买卖价差（权重 20%）
          4. 价格稳定性（权重 15%）
          5. 交易所净流入/流出（权重 10%）
        """
        scores = {}

        # 1. 深度趋势
        depth_score_map = {"increasing": 9, "neutral": 5, "decreasing": 2}
        scores["depth_trend"] = depth_score_map.get(depth_trend, 5) * 0.30

        # 2. 做市商活动
        mm_trust = MM_TRUST_SCORES.get(known_mm, MM_TRUST_SCORES["unknown"]) if known_mm else 0.40
        scores["mm_activity"] = mm_trust * 10 * 0.25

        # 3. 价差
        if spread_pct < 0.05:
            scores["spread"] = 9 * 0.20
        elif spread_pct < 0.15:
            scores["spread"] = 7 * 0.20
        elif spread_pct < 0.5:
            scores["spread"] = 4 * 0.20
        else:
            scores["spread"] = 1 * 0.20

        # 4. 价格稳定性
        stability_map = {"stable": 8, "neutral": 5, "volatile": 2}
        scores["price_stability"] = stability_map.get(price_stability, 5) * 0.15

        # 5. 净流入
        flow_map = {"bullish": 8, "neutral": 5, "bearish": 2}
        scores["net_flow"] = flow_map.get(net_flow_signal, 5) * 0.10

        total = round(sum(scores.values()), 1)

        return {
            "token": token,
            "total_score": total,
            "breakdown": {k: round(v, 2) for k, v in scores.items()},
            "interpretation": (
                "做市商在认真做事，可信" if total >= 7
                else "做市商存在，但质量一般" if total >= 4
                else "做市商缺位或质量差，谨慎"
            ),
        }

    # ═══════════════════════════════
    # 暴涨前征兆评分（7 信号模型）
    # ═══════════════════════════════

    def score_pre_pump_signals(self, token: str, signals: dict) -> dict:
        """
        暴涨前 7 个共性征兆评分

        每个征兆 0-1 分，总分 0-7
        ≥3 分 → Telegram 提醒
        ≥5 分 → 强提醒

        signals: {
            'oi_buildup': bool,              # OI 低位磨底后放大 + 价格不动
            'funding_rate_normalize': bool,   # 资金费率从极度负值恢复
            'volume_dry_up_then_spike': bool, # 地量后放量
            'smart_money_accumulating': bool, # 多个聪明钱加仓
            'mm_bid_wall_thickening': bool,   # 买单墙持续加厚
            'social_mention_spike': bool,     # 社交飙升 + 价格未动
            'multiple_kol_mention': bool,     # 5+ KOL 同时提及
        }
        """
        weights = {
            # ⭐⭐⭐⭐⭐ 最高可靠性
            "oi_buildup": 1.0,
            # ⭐⭐⭐⭐
            "funding_rate_normalize": 1.0,
            "volume_dry_up_then_spike": 1.0,
            "smart_money_accumulating": 1.0,
            # ⭐⭐⭐
            "mm_bid_wall_thickening": 0.7,
            "social_mention_spike": 0.7,
            # ⭐⭐
            "multiple_kol_mention": 0.5,
        }

        total = 0.0
        breakdown = {}
        for name, triggered in signals.items():
            w = weights.get(name, 0.5)
            score = w if triggered else 0
            breakdown[name] = score
            total += score

        threshold_met = total >= 3.0

        return {
            "token": token,
            "total_score": round(total, 1),
            "max_score": 7.0,
            "threshold_met": threshold_met,
            "alert_level": (
                "strong" if total >= 5.0
                else "moderate" if total >= 3.0
                else "none"
            ),
            "breakdown": breakdown,
        }

    # ═══════════════════════════════
    # 假暴涨检测
    # ═══════════════════════════════

    def detect_fake_pump(self, token: str, conditions: dict) -> dict:
        """
        假暴涨信号检测

        conditions: {
            'price_and_oi_both_spike': bool,    # 价格和OI同时爆拉
            'kol_pumping_at_oi_high': bool,     # KOL喊单+OI高位
            'single_wick_reversal': bool,        # 单根长上影
            'volume_concentrated_1min': bool,    # 成交量集中在1分钟内
        }
        """
        risk_score = 0.0
        reasons = []

        if conditions.get("price_and_oi_both_spike"):
            risk_score += 2.5
            reasons.append("价格和 OI 同时爆拉 — 涨幅已兑现，接力资金在赌继续涨")

        if conditions.get("kol_pumping_at_oi_high"):
            risk_score += 2.5
            reasons.append("KOL 喊单 + OI 历史高位 — 大户在出货找接盘侠")

        if conditions.get("single_wick_reversal"):
            risk_score += 2.5
            reasons.append("单根长上影后立即回落 — 爆仓猎杀，不是建仓")

        if conditions.get("volume_concentrated_1min"):
            risk_score += 2.5
            reasons.append("成交量 >30% 集中在 1 分钟内 — 程序对倒，不是真实买盘")

        return {
            "token": token,
            "risk_score": risk_score,
            "is_suspicious": risk_score >= 5.0,
            "reasons": reasons,
        }

    def get_binance_alpha_score(self, token: str, listing_event: Optional[ListingEvent] = None) -> float:
        """
        币安 Alpha 综合评分 (0-20)

        用于信号融合
        """
        if listing_event is None:
            # 查找此 token 的 listing
            for e in self.listings:
                if e.token == token.upper():
                    listing_event = e
                    break

        if listing_event is None:
            return 0.0

        score = 0.0
        exchange_profile = EXCHANGE_PROFILES.get(listing_event.exchange, {})

        # 交易所信号强度
        signal_strength = exchange_profile.get("signal_strength", 0)
        score += signal_strength * 0.5  # 最多 5 分来自交易所

        # 做市商质量
        if listing_event.mm_quality_score is not None:
            score += listing_event.mm_quality_score * 1.0  # 最多 10 分

        # 暴涨前征兆
        if listing_event.pre_pump_score is not None:
            score += min(listing_event.pre_pump_score, 3) * 1.0  # 最多 3 分

        # 假暴涨风险扣分
        score -= listing_event.fake_pump_risk * 0.5  # 最多扣 5 分

        return max(0, min(20, score))

    def get_stats(self) -> dict:
        return {
            "total_listings": len(self.listings),
            "settling_count": len(self.get_settling_tokens()),
            "by_exchange": {
                ex.value: len([e for e in self.listings if e.exchange == ex])
                for ex in Exchange
            },
        }

    def save_state(self):
        os.makedirs(self.data_dir, exist_ok=True)
        filepath = os.path.join(self.data_dir, "listings.json")
        with open(filepath, "w", encoding="utf-8") as f:
            json.dump([asdict(e) for e in self.listings], f, ensure_ascii=False, indent=2)


# ═══════════════════════════════
# 二线所上币监控
# ═══════════════════════════════

class SecondaryExchangeMonitor:
    """
    二线交易所上币监控

    MEXC / Bitget / Bybit / KuCoin — 通常比币安快 1-7 天
    上二线所本身 = 信号 — 说明项目方有预算做市、有社区基础
    """

    def __init__(self):
        self.listings: list[ListingEvent] = []
        self.last_check: dict[Exchange, str] = {}  # exchange → last check time

    def check_new_listing(self, exchange: Exchange, token: str,
                           listing_time: str, trading_pair: str = "") -> ListingEvent:
        """
        检测到新上币

        注意：MEXC 上的某些币是"一键发币+一键上MEXC"的流水线产品 → 假信号
        区分方式：看社区活跃度 + KOL 讨论 + 链上持币地址增长
        """
        event = ListingEvent(
            token=token.upper(),
            exchange=exchange,
            phase=ListingPhase.LAUNCH_DAY,
            announce_time=listing_time,
            listing_time=listing_time,
            trading_pairs=[trading_pair] if trading_pair else [f"{token}USDT"],
            current_stage_days=0,
        )
        self.listings.append(event)
        self.last_check[exchange] = datetime.utcnow().isoformat()
        return event

    def get_recent_listings(self, days: int = 7) -> list[ListingEvent]:
        """获取最近 N 天的上币"""
        now = datetime.utcnow()
        cutoff = now - timedelta(days=days)
        return [
            e for e in self.listings
            if e.listing_time and datetime.fromisoformat(e.listing_time) >= cutoff
        ]

    def get_multi_exchange_signals(self, token: str) -> dict:
        """
        检查一个币是否同时在多个二线所上线

        MEXC + Bitget 同时上线 → 信号增强
        """
        token = token.upper()
        exchanges = set()
        for e in self.listings:
            if e.token == token:
                exchanges.add(e.exchange.value)

        has_mexc = "mexc" in exchanges
        has_bitget = "bitget" in exchanges
        has_bybit = "bybit" in exchanges
        has_kucoin = "kucoin" in exchanges

        # 信号强度
        signal_boost = 0
        if has_mexc and has_bitget:
            signal_boost += 1.5
        if has_bybit:
            signal_boost += 1.0
        if has_kucoin:
            signal_boost += 0.5

        return {
            "token": token,
            "listed_exchanges": sorted(exchanges),
            "exchange_count": len(exchanges),
            "signal_boost": signal_boost,
            "is_mexc_only": has_mexc and len(exchanges) == 1,
            "warning": (
                "⚠️ 仅 MEXC 上线，无其他二线所验证 — 可能是流水线产品"
                if has_mexc and len(exchanges) == 1 and not has_bitget
                else ""
            ),
        }

    def get_secondary_exchange_score(self, token: str) -> float:
        """
        二线所上币评分 (0-15)

        用于信号融合
        """
        multi = self.get_multi_exchange_signals(token)
        base = 0.0

        if multi["exchange_count"] == 0:
            return 0.0

        # 每个交易所按权重加分
        for ex in multi["listed_exchanges"]:
            ex_enum = Exchange(ex)
            profile = EXCHANGE_PROFILES.get(ex_enum, {})
            base += profile.get("weight", 5) * 0.5

        # 多交易所同时上线 → 信号增强
        base += multi["signal_boost"]

        return min(15, base)

    def get_stats(self) -> dict:
        return {
            "total_listings": len(self.listings),
            "recent_7d": len(self.get_recent_listings(7)),
            "last_check_times": {ex.value: t for ex, t in self.last_check.items()},
        }
