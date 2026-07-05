"""
五路信号融合引擎 — Day 21

CT-DDD 视角：
  外部信号（灰箱/黑箱域）→ 融合评分 → 仓位调节 + 风控降权 + 复盘归因
  只有策略信号（白箱域）能触发开仓 — harness 控制壳模式

五路信号源与权重：
  信号1 — 链上实时扫描（0-40分）
  信号2 — KOL 推特监控（0-25分）
  信号3 — 二线所上币监控（0-15分）
  信号4 — 币安 Alpha 监控（0-20分）
  信号5 — 社区情绪 + 叙事热度（0-10分）

  综合满分 = 110 分
  > 70 分 → 🔴 强力信号 → Telegram 推送 + 可考虑入场
  50-70 分 → 🟡 关注信号 → Dashboard 中优先级 + 继续观察
  30-50 分 → 🟢 观察信号 → 仅记录
  < 30 分 → 噪音 → 忽略

信号分层用途（路线图 Day 21 表）：
  | 信号来源     | 控制仓位 | 解释涨跌 | 发现机会 | 触发开仓 |
  | 策略信号     | —       | —       | —       | ✅      |
  | KOL 情绪     | ✅      | ✅      | ✅      | ❌      |
  | 链上鲸鱼     | ✅      | ✅      | ✅      | ❌      |
  | 做市商行为   | ✅      | ✅      | ✅      | ❌      |
  | 市场环境     | ✅      | ✅      | —       | ❌      |
"""

import json
import os
from dataclasses import dataclass, field, asdict
from datetime import datetime, timedelta
from enum import Enum
from typing import Optional

from .kol_monitor import KOLMonitor
from .whale_tracker import WhaleTracker, SmartMoneyTracker, ContractEventMonitor
from .orderbook_monitor import OrderBookMonitor
from .binance_alpha import (
    BinanceAlphaMonitor, SecondaryExchangeMonitor,
    ListingPhase, Exchange,
)


# ═══════════════════════════════
# 数据类型
# ═══════════════════════════════

class AlertLevel(str, Enum):
    """警报级别"""
    RED = "red"        # > 70 分 → 强力信号
    YELLOW = "yellow"  # 50-70 分 → 关注
    GREEN = "green"    # 30-50 分 → 观察
    GRAY = "gray"      # < 30 分 → 噪音


@dataclass
class SignalSource:
    """单个信号源得分"""
    name: str
    raw_score: float
    max_score: float
    normalized_score: float       # 0-1 归一化
    confidence: str               # high / medium / low
    details: dict = field(default_factory=dict)
    alerts: list[str] = field(default_factory=list)


@dataclass
class FusionScore:
    """融合评分结果"""
    token: str
    timestamp: str
    total_score: float             # 0-110
    normalized_score: float        # 0-100
    alert_level: AlertLevel
    sources: list[SignalSource]
    position_multiplier: float     # 仓位调整系数（0.0-1.0）
    risk_flags: list[str]          # 风控标记
    attribution_notes: list[str]   # 归因备注
    recommendation: str            # 综合建议


# ═══════════════════════════════
# 信号融合引擎
# ═══════════════════════════════

class SignalFusion:
    """
    五路信号融合引擎

    使用方式:
      fusion = SignalFusion(kol=kol_monitor, whale=whale_tracker, ...)
      result = fusion.evaluate("BTC")  # 主流币评估
      result = fusion.evaluate_meme("ANSEM")  # Meme 币评估（侧重链上+KOL）
    """

    def __init__(
        self,
        kol_monitor: Optional[KOLMonitor] = None,
        whale_tracker: Optional[WhaleTracker] = None,
        smart_money: Optional[SmartMoneyTracker] = None,
        contract_monitor: Optional[ContractEventMonitor] = None,
        orderbook_monitor: Optional[OrderBookMonitor] = None,
        binance_alpha: Optional[BinanceAlphaMonitor] = None,
        secondary_exchange: Optional[SecondaryExchangeMonitor] = None,
    ):
        self.kol = kol_monitor or KOLMonitor()
        self.whale = whale_tracker or WhaleTracker()
        self.smart_money = smart_money or SmartMoneyTracker()
        self.contracts = contract_monitor or ContractEventMonitor()
        self.orderbook = orderbook_monitor or OrderBookMonitor()
        self.binance_alpha = binance_alpha or BinanceAlphaMonitor()
        self.secondary_ex = secondary_exchange or SecondaryExchangeMonitor()

        self.fusion_history: list[FusionScore] = []

    # ═══════════════════════════════
    # 主流币评估（BTC/ETH/SOL）
    # ═══════════════════════════════

    def evaluate(self, token: str) -> FusionScore:
        """
        主流币五路信号融合评估

        用于 BTC/ETH/SOL — 侧重链上鲸鱼 + 订单簿 + KOL 情绪
        """
        token = token.upper()
        now = datetime.utcnow().isoformat()
        sources: list[SignalSource] = []
        risk_flags: list[str] = []
        attribution_notes: list[str] = []

        # ── 信号 1: 链上扫描 (0-40) ──
        whale_score = self.whale.get_whale_score(token)          # 0-10
        smart_score = self.smart_money.get_smart_money_score(token)  # 0-10
        # 鲸鱼 + 聪明钱 + 交易所余额趋势 → 映射到 0-40
        balance = self.whale.get_exchange_balance_trend(token if token in ("BTC", "ETH") else "BTC")
        onchain_raw = whale_score * 2.0 + smart_score * 1.5 + (
            10 if balance["trend"] == "accumulation" else 0
        )
        onchain_raw = min(40, onchain_raw)

        sources.append(SignalSource(
            name="链上扫描",
            raw_score=round(onchain_raw, 1),
            max_score=40,
            normalized_score=round(onchain_raw / 40, 2),
            confidence="medium" if self.whale.transfers else "low",
            details={
                "whale_score": whale_score,
                "smart_money_score": smart_score,
                "exchange_balance_trend": balance["trend"],
                "whale_net_flow": self.whale.get_net_flow(token, hours=24),
            },
        ))

        # ── 信号 2: KOL 监控 (0-25) ──
        sentiment = self.kol.get_aggregate_sentiment(token, hours=24)
        kol_score = sentiment.get("kol_quality_score", 0)
        sources.append(SignalSource(
            name="KOL 情绪",
            raw_score=round(kol_score, 1),
            max_score=25,
            normalized_score=round(kol_score / 25, 2),
            confidence="medium" if sentiment.get("total_mentions", 0) > 5 else "low",
            details=sentiment,
        ))

        # KOL 异常检测
        kol_anomalies = self.kol.detect_anomalies(token, hours=24)
        if kol_anomalies:
            for a in kol_anomalies:
                if a.severity in ("high", "critical"):
                    risk_flags.append(f"KOL异常: {a.type} — {a.detail}")
                    if a.warning:
                        attribution_notes.append(a.warning)

        # ── 信号 3: 二线所上币 (0-15) ──
        sec_ex_score = self.secondary_ex.get_secondary_exchange_score(token)
        sources.append(SignalSource(
            name="二线所上币",
            raw_score=round(sec_ex_score, 1),
            max_score=15,
            normalized_score=round(sec_ex_score / 15, 2),
            confidence="high" if sec_ex_score > 5 else "medium",
            details=self.secondary_ex.get_multi_exchange_signals(token),
        ))

        # ── 信号 4: 币安 Alpha (0-20) ──
        binance_score = self.binance_alpha.get_binance_alpha_score(token)
        sources.append(SignalSource(
            name="币安 Alpha",
            raw_score=round(binance_score, 1),
            max_score=20,
            normalized_score=round(binance_score / 20, 2),
            confidence="medium",
            details={},
        ))

        # ── 信号 5: 社区情绪 (0-10) ──
        community_score = 0.0
        if sentiment.get("extreme_flag"):
            community_score += 3  # 极端情绪有价值
        mention_change = sentiment.get("mention_change_vs_7d", 0)
        if mention_change > 3:
            community_score += 4  # 提及暴增
        elif mention_change > 1:
            community_score += 2
        # 叙事一致性加分
        if sentiment.get("total_mentions", 0) > 10:
            community_score += 3
        community_score = min(10, community_score)

        sources.append(SignalSource(
            name="社区情绪",
            raw_score=round(community_score, 1),
            max_score=10,
            normalized_score=round(community_score / 10, 2),
            confidence="low",
            details={
                "mention_change_vs_7d": mention_change,
                "extreme_flag": sentiment.get("extreme_flag"),
                "total_mentions": sentiment.get("total_mentions", 0),
            },
        ))

        # ── 综合评分 ──
        total = sum(s.raw_score for s in sources)
        normalized = round(total / 110 * 100, 1)
        alert_level = self._classify_alert(total)

        # ── 仓位调整系数 ──
        position_mult = self._calc_position_multiplier(total, risk_flags)

        # ── 综合建议 ──
        recommendation = self._generate_recommendation(alert_level, sources, risk_flags)

        result = FusionScore(
            token=token,
            timestamp=now,
            total_score=round(total, 1),
            normalized_score=normalized,
            alert_level=alert_level,
            sources=sources,
            position_multiplier=position_mult,
            risk_flags=risk_flags,
            attribution_notes=attribution_notes,
            recommendation=recommendation,
        )

        self.fusion_history.append(result)
        return result

    # ═══════════════════════════════
    # Meme 币评估（10-100x 框架）
    # ═══════════════════════════════

    def evaluate_meme(self, token: str, created_hours_ago: float = 24,
                       volume_24h_usd: float = 0, liquidity_locked: bool = True,
                       smart_money_buying: int = 0, kol_mentions: int = 0,
                       on_mexc: bool = False, on_bitget: bool = False) -> FusionScore:
        """
        小币种/Meme 币五路信号融合评估

        与主流币侧重点不同：
          - 链上权重大（新代币，链上数据最重要）
          - KOL 权重中（早期发现靠 KOL）
          - 交易所权重低（还没上大所）
          - 订单簿不可用（没有深度数据）
        """
        token = token.upper()
        now = datetime.utcnow().isoformat()
        sources: list[SignalSource] = []
        risk_flags: list[str] = []
        attribution_notes: list[str] = []

        # ── 信号 1: 链上扫描 (0-40) ──
        onchain_raw = 0.0

        # 代币新鲜度
        if created_hours_ago < 1:
            onchain_raw += 8   # 创建 < 1h = 极早期
        elif created_hours_ago < 6:
            onchain_raw += 6
        elif created_hours_ago < 24:
            onchain_raw += 4

        # 成交量
        if volume_24h_usd > 1_000_000:
            onchain_raw += 12
        elif volume_24h_usd > 500_000:
            onchain_raw += 8
        elif volume_24h_usd > 100_000:
            onchain_raw += 4

        # 流动性锁定
        if liquidity_locked:
            onchain_raw += 10
        else:
            risk_flags.append("🚨 流动性未锁定 — rug pull 风险极高")
            onchain_raw -= 20

        # 聪明钱建仓
        onchain_raw += min(smart_money_buying * 5, 15)

        onchain_raw = max(0, min(40, onchain_raw))

        sources.append(SignalSource(
            name="链上扫描",
            raw_score=round(onchain_raw, 1),
            max_score=40,
            normalized_score=round(onchain_raw / 40, 2),
            confidence="medium" if volume_24h_usd > 100_000 else "low",
            details={
                "created_hours_ago": created_hours_ago,
                "volume_24h_usd": volume_24h_usd,
                "liquidity_locked": liquidity_locked,
                "smart_money_buying": smart_money_buying,
            },
        ))

        # ── 信号 2: KOL 监控 (0-25) ──
        sentiment = self.kol.get_aggregate_sentiment(token, hours=24)
        kol_score = sentiment.get("kol_quality_score", 0)

        # Meme 币的 KOL 信号更看重 S/A 级
        s_a_mentions = sum(
            1 for m in self.kol.mentions
            if m.coin == token and m.kol_tier.value in ("S", "A")
        )
        kol_score += s_a_mentions * 3
        kol_score = min(25, kol_score)

        sources.append(SignalSource(
            name="KOL 情绪",
            raw_score=round(kol_score, 1),
            max_score=25,
            normalized_score=round(kol_score / 25, 2),
            confidence="medium",
            details={**sentiment, "s_a_tier_mentions": s_a_mentions},
        ))

        # ── 信号 3: 二线所上币 (0-15) ──
        sec_ex_score = 0.0
        if on_mexc:
            sec_ex_score += 3  # MEXC = 弱信号
        if on_bitget:
            sec_ex_score += 5  # Bitget = 中信号
        if on_mexc and on_bitget:
            sec_ex_score += 3  # 双所同时上线 = 信号增强
        sec_ex_score = min(15, sec_ex_score)

        sources.append(SignalSource(
            name="二线所上币",
            raw_score=round(sec_ex_score, 1),
            max_score=15,
            normalized_score=round(sec_ex_score / 15, 2),
            confidence="high" if on_bitget else "low",
            details={"on_mexc": on_mexc, "on_bitget": on_bitget},
        ))

        # ── 信号 4: 币安 Alpha (0-20) ──
        # Meme 币大概率没上币安，此信号通常为 0
        sources.append(SignalSource(
            name="币安 Alpha",
            raw_score=0,
            max_score=20,
            normalized_score=0,
            confidence="low",
            details={"note": "Meme 币通常未上币安"},
        ))

        # ── 信号 5: 社区情绪 (0-10) ──
        community_score = 0.0
        mention_change = sentiment.get("mention_change_vs_7d", 0)
        if mention_change > 5:
            community_score += 5
        elif mention_change > 2:
            community_score += 3

        total_mentions = sentiment.get("total_mentions", 0)
        if total_mentions > 20:
            community_score += 3
        elif total_mentions > 5:
            community_score += 2

        if sentiment.get("extreme_flag"):
            community_score += 2

        community_score = min(10, community_score)

        sources.append(SignalSource(
            name="社区情绪",
            raw_score=round(community_score, 1),
            max_score=10,
            normalized_score=round(community_score / 10, 2),
            confidence="medium" if total_mentions > 10 else "low",
            details=sentiment,
        ))

        # 防 rug 检测（额外扣分项）
        rug_risk = self.contracts.get_rug_risk_score(token, "")
        if rug_risk > 5:
            risk_flags.append(f"🚨 Rug 风险评分 {rug_risk}/10")

        # 假 pump 检测
        fake_pump_conditions = {
            "kol_pumping_at_oi_high": kol_mentions >= 3 and sentiment.get("extreme_flag"),
        }
        fake_pump = self.binance_alpha.detect_fake_pump(token, fake_pump_conditions)
        if fake_pump["is_suspicious"]:
            risk_flags.extend(fake_pump["reasons"])

        # ── 综合评分 ──
        total = sum(s.raw_score for s in sources) - rug_risk  # 扣 rug 风险分
        total = max(0, total)
        normalized = round(total / 110 * 100, 1)
        alert_level = self._classify_alert(total)

        # Meme 币仓位系数更保守
        position_mult = 1.0
        if risk_flags:
            position_mult = 0.0  # 有风险标记 = 不入场
        elif alert_level == AlertLevel.RED:
            position_mult = 0.005  # 0.5% 仓位（Meme 币单笔上限）
        elif alert_level == AlertLevel.YELLOW:
            position_mult = 0.002  # 0.2% 仓位

        recommendation = self._generate_meme_recommendation(alert_level, sources, risk_flags)

        result = FusionScore(
            token=token,
            timestamp=now,
            total_score=round(total, 1),
            normalized_score=normalized,
            alert_level=alert_level,
            sources=sources,
            position_multiplier=position_mult,
            risk_flags=risk_flags,
            attribution_notes=attribution_notes,
            recommendation=recommendation,
        )

        self.fusion_history.append(result)
        return result

    # ═══════════════════════════════
    # 风控集成
    # ═══════════════════════════════

    def get_position_adjustment(self, token: str, base_position_pct: float) -> dict:
        """
        根据信号融合结果调整仓位

        返回：
          {
            'original_position_pct': float,
            'adjusted_position_pct': float,
            'multiplier': float,
            'reason': str,
          }
        """
        result = self.evaluate(token)
        multiplier = result.position_multiplier
        adjusted = base_position_pct * multiplier

        return {
            "token": token,
            "original_position_pct": base_position_pct,
            "adjusted_position_pct": round(adjusted, 4),
            "multiplier": multiplier,
            "alert_level": result.alert_level.value,
            "reason": (
                f"信号融合评分 {result.total_score}/110 ({result.alert_level.value})，"
                f"仓位系数 ×{multiplier}"
            ),
            "risk_flags": result.risk_flags,
        }

    def should_pause_strategy(self, token: str) -> bool:
        """
        是否应该暂停该币种的策略

        条件：融合评分 < 20 或存在 critical 级别风险标记
        """
        result = self.evaluate(token)
        if result.total_score < 20:
            return True
        critical_flags = [f for f in result.risk_flags if "🚨" in f]
        return len(critical_flags) > 0

    # ═══════════════════════════════
    # 复盘归因
    # ═══════════════════════════════

    def attribute_price_move(self, token: str, price_change_pct: float,
                              event_time: str) -> dict:
        """
        解释一次价格变动 — 用于三维复盘的事件驱动层

        返回：
          {
            'price_change_pct': float,
            'attribution': {
              'onchain_contribution': str,
              'kol_contribution': str,
              'exchange_contribution': str,
              'community_contribution': str,
            },
            'narrative': str,
            'anomalies_during_move': list,
          }
        """
        # 查事件时间附近的信号
        event_dt = datetime.fromisoformat(event_time)
        nearby_kol = [
            m for m in self.kol.mentions
            if m.coin == token.upper()
            and abs((datetime.fromisoformat(m.timestamp) - event_dt).total_seconds()) < 3600
        ]

        nearby_whale = [
            t for t in self.whale.transfers
            if t.asset == token.upper()
            and abs((datetime.fromisoformat(t.timestamp) - event_dt).total_seconds()) < 7200
        ]

        attribution = {
            "price_change_pct": price_change_pct,
            "attribution": {
                "onchain_contribution": (
                    f"{len(nearby_whale)} 笔鲸鱼转账在事件附近"
                    if nearby_whale else "无显著链上活动"
                ),
                "kol_contribution": (
                    f"{len(nearby_kol)} 次 KOL 提及在事件附近"
                    if nearby_kol else "无显著 KOL 活动"
                ),
                "exchange_contribution": "待接入交易所数据",
                "community_contribution": "待接入社交数据",
            },
            "narrative": (
                "KOL 驱动" if len(nearby_kol) > 3
                else "鲸鱼驱动" if len(nearby_whale) > 2
                else "未知驱动"
            ),
            "anomalies_during_move": [
                asdict(a) for a in self.kol.detect_anomalies(token, hours=2)
            ],
        }
        return attribution

    # ═══════════════════════════════
    # 内部
    # ═══════════════════════════════

    def _classify_alert(self, total_score: float) -> AlertLevel:
        if total_score >= 70:
            return AlertLevel.RED
        elif total_score >= 50:
            return AlertLevel.YELLOW
        elif total_score >= 30:
            return AlertLevel.GREEN
        return AlertLevel.GRAY

    def _calc_position_multiplier(self, total_score: float, risk_flags: list[str]) -> float:
        """计算仓位调整系数"""
        # 有 🚨 标记 → 暂停
        if any("🚨" in f for f in risk_flags):
            return 0.0

        # 有 ⚠️ 标记 → 减仓
        if any("⚠️" in f for f in risk_flags):
            return 0.5

        # 按评分调仓
        if total_score >= 70:
            return 1.0
        elif total_score >= 50:
            return 1.0  # 关注但不减仓
        elif total_score >= 30:
            return 0.7  # 轻度减仓
        else:
            return 0.3  # 噪音，大幅减仓

    def _generate_recommendation(self, alert_level: AlertLevel,
                                   sources: list[SignalSource],
                                   risk_flags: list[str]) -> str:
        """生成主流币综合建议"""
        if alert_level == AlertLevel.RED:
            base = "✅ 外部信号积极 — 可维持正常仓位，关注 KOL 异常事件"
        elif alert_level == AlertLevel.YELLOW:
            base = "🟡 外部信号中性偏积极 — 保持仓位，密切关注变化"
        elif alert_level == AlertLevel.GREEN:
            base = "🟢 外部信号偏弱 — 建议减仓至 70%，等待信号改善"
        else:
            base = "⚪ 外部信号噪声 — 建议大幅减仓至 30%，依赖策略自身信号"

        if risk_flags:
            base += f" | ⚠️ 风控标记: {'; '.join(risk_flags[:3])}"
        return base

    def _generate_meme_recommendation(self, alert_level: AlertLevel,
                                        sources: list[SignalSource],
                                        risk_flags: list[str]) -> str:
        """生成 Meme 币综合建议"""
        if risk_flags:
            return "🚨 存在风控标记 — 不入场。等待信号清理后重新评估"

        if alert_level == AlertLevel.RED:
            return "🔴 Meme 强力信号 — 可考虑 0.2-0.5% 小仓位入场，翻倍出本金"
        elif alert_level == AlertLevel.YELLOW:
            return "🟡 Meme 关注信号 — 继续观察，等待更多信号确认"
        elif alert_level == AlertLevel.GREEN:
            return "🟢 Meme 弱信号 — 仅记录，不入场"
        else:
            return "⚪ 噪音 — 忽略"

    def get_stats(self) -> dict:
        return {
            "total_evaluations": len(self.fusion_history),
            "recent_alerts": [
                {"token": f.token, "score": f.total_score, "level": f.alert_level.value}
                for f in self.fusion_history[-10:]
            ],
        }
