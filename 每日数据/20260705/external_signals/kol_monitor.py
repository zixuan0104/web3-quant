"""
KOL 监控模块 — Day 19

核心能力：
  1. KOL 白名单管理（S/A/B/C 四级分级）
  2. 提及频率追踪 + 首次提及时间戳
  3. DeepSeek 情绪分析聚合
  4. 异常检测（提及暴增 / KOL 集群 / 社交价格背离）

硬性规则（来自 quant-kol skill）：
  - KOL 提及 ≠ 交易信号 — KOL 喊单时可能已建仓
  - 情绪极端才是信号 — 日常讨论是噪声
  - 结果只调节仓位和归因，不触发开仓
"""

import json
import os
import time
from dataclasses import dataclass, field, asdict
from datetime import datetime, timedelta
from enum import Enum
from typing import Optional, Union
from collections import defaultdict

from .deepseek_client import DeepSeekClient


# ═══════════════════════════════
# 数据类型
# ═══════════════════════════════

class KOLTier(str, Enum):
    """KOL 分级 — 信号价值递减"""
    S = "S"  # Solana 生态核心开发者 — 极高信号价值
    A = "A"  # 历史喊单胜率 > 50% 的交易 KOL
    B = "B"  # KOL 集群成员 — 需链上验证
    C = "C"  # 平时不提币、突然喊小币 — 大概率接盘


@dataclass
class KOLEntry:
    """KOL 条目"""
    handle: str                          # Twitter handle (不含@)
    name: str                            # 显示名
    tier: KOLTier                        # 分级
    category: str                        # macro_analyst / onchain_analyst / trader_caller / alpha_caller
    ecosystem: str                       # 主要关注生态 (solana / ethereum / multi)
    accuracy_history: list[float] = field(default_factory=list)  # 历史喊单准确率
    total_calls: int = 0                 # 历史喊单总数
    correct_calls: int = 0               # 正确喊单数
    last_updated: str = ""               # 最后更新时间
    notes: str = ""                      # 备注


@dataclass
class MentionRecord:
    """单次提及记录"""
    kol_handle: str
    kol_tier: KOLTier
    coin: str
    timestamp: str                       # ISO 8601
    text_hash: str                       # 推文内容 hash（去重用）
    sentiment: str = "neutral"           # positive / negative / neutral
    sentiment_confidence: float = 0.0
    is_price_prediction: bool = False
    is_data_driven: bool = False
    narrative_tags: list[str] = field(default_factory=list)
    price_at_mention: Optional[float] = None  # 提及时的币价


@dataclass
class SocialAnomaly:
    """社交异常事件"""
    type: str                            # mention_spike / kol_cluster / social_price_divergence / sentiment_flip
    coin: str
    severity: str                        # low / medium / high / critical
    detail: str
    timestamp: str
    metrics: dict = field(default_factory=dict)
    warning: str = ""                    # 风控建议


# ═══════════════════════════════
# 默认 KOL 白名单 — Solana 生态为主
# ═══════════════════════════════

DEFAULT_KOL_LIST: list[dict] = [
    # ── S 级：Solana 生态核心开发者 ──
    {"handle": "aeyakovenko", "name": "Anatoly Yakovenko", "tier": "S", "category": "macro_analyst", "ecosystem": "solana"},
    {"handle": "rajgokal", "name": "Raj Gokal", "tier": "S", "category": "macro_analyst", "ecosystem": "solana"},
    {"handle": "0xMert_", "name": "Mert", "tier": "S", "category": "onchain_analyst", "ecosystem": "solana"},

    # ── A 级：链上数据 + 高胜率交易 KOL ──
    {"handle": "lookonchain", "name": "Lookonchain", "tier": "A", "category": "onchain_analyst", "ecosystem": "multi"},
    {"handle": "nansen_ai", "name": "Nansen", "tier": "A", "category": "onchain_analyst", "ecosystem": "multi"},
    {"handle": "artemis__xyz", "name": "Artemis", "tier": "A", "category": "onchain_analyst", "ecosystem": "multi"},
    {"handle": "Dynamo_Patrick", "name": "Patrick Scott", "tier": "A", "category": "macro_analyst", "ecosystem": "multi"},

    # ── B 级：Alpha caller + 交易型 KOL ──
    {"handle": "CryptoGodJohn", "name": "Crypto God John", "tier": "B", "category": "trader_caller", "ecosystem": "solana"},
    {"handle": "blknoiz06", "name": "blknoiz06", "tier": "B", "category": "alpha_caller", "ecosystem": "solana"},
    {"handle": "dingalingts", "name": "Dingaling", "tier": "B", "category": "trader_caller", "ecosystem": "multi"},
    {"handle": "cobie", "name": "Cobie", "tier": "B", "category": "alpha_caller", "ecosystem": "multi"},

    # ── C 级：需要验证的喊单型 KOL（用于反向参考）──
    {"handle": "CryptoWendyO", "name": "Wendy O", "tier": "C", "category": "trader_caller", "ecosystem": "multi"},
    {"handle": "Crypto_Banter", "name": "Crypto Banter", "tier": "C", "category": "trader_caller", "ecosystem": "multi"},
]

# 需要监控的关键词（Solana Meme 发现）
DEFAULT_KEYWORDS = [
    # 币种发现
    "$SOL", "solana meme", "new solana coin", "just launched",
    "100x", "1000x", "gem", "next moonshot",
    # 叙事
    "AI agent", "meme coin", "degen", "pump fun",
    # 风险信号
    "rug", "scam", "honeypot", "liquidity pulled",
]


# ═══════════════════════════════
# KOL 监控主类
# ═══════════════════════════════

class KOLMonitor:
    """
    KOL 监控器

    使用方式:
      monitor = KOLMonitor()
      monitor.load_kol_list()                        # 加载默认 KOL 白名单
      monitor.record_mention(kol, coin, text, price)  # 记录提及
      anomalies = monitor.detect_anomalies(coin)      # 检测异常
      report = monitor.get_coin_profile(coin)         # 获取币种 KOL 画像
    """

    def __init__(
        self,
        deepseek_client: Optional[DeepSeekClient] = None,
        data_dir: Optional[str] = None,
    ):
        self.ds = deepseek_client or DeepSeekClient()
        self.kol_list: dict[str, KOLEntry] = {}       # handle → KOLEntry
        self.mentions: list[MentionRecord] = []        # 全部提及记录
        self.data_dir = data_dir or os.path.join(
            os.path.dirname(os.path.abspath(__file__)), "..", "data", "kol"
        )

    # ═══════════════════════════════
    # KOL 列表管理
    # ═══════════════════════════════

    def load_kol_list(self, kol_configs: Optional[list[dict]] = None):
        """加载 KOL 白名单"""
        configs = kol_configs or DEFAULT_KOL_LIST
        for cfg in configs:
            entry = KOLEntry(
                handle=cfg["handle"],
                name=cfg["name"],
                tier=KOLTier(cfg["tier"]),
                category=cfg["category"],
                ecosystem=cfg["ecosystem"],
                last_updated=datetime.utcnow().isoformat(),
            )
            self.kol_list[entry.handle] = entry
        print(f"📋 加载 KOL 白名单: {len(self.kol_list)} 人")
        self._print_tier_breakdown()

    def add_kol(self, handle: str, name: str, tier: KOLTier,
                category: str, ecosystem: str = "multi", notes: str = ""):
        """添加 KOL 到监控列表"""
        entry = KOLEntry(
            handle=handle.lstrip("@"),
            name=name,
            tier=tier,
            category=category,
            ecosystem=ecosystem,
            last_updated=datetime.utcnow().isoformat(),
            notes=notes,
        )
        self.kol_list[entry.handle] = entry

    def remove_kol(self, handle: str):
        """移除 KOL（连续 3 个月准确率 < 随机水平）"""
        handle = handle.lstrip("@")
        if handle in self.kol_list:
            del self.kol_list[handle]

    def downgrade_kol(self, handle: str):
        """降级 KOL"""
        handle = handle.lstrip("@")
        if handle not in self.kol_list:
            return
        current = self.kol_list[handle].tier
        downgrade_map = {KOLTier.S: KOLTier.A, KOLTier.A: KOLTier.B, KOLTier.B: KOLTier.C}
        if current in downgrade_map:
            self.kol_list[handle].tier = downgrade_map[current]

    def upgrade_kol(self, handle: str):
        """升级 KOL"""
        handle = handle.lstrip("@")
        if handle not in self.kol_list:
            return
        current = self.kol_list[handle].tier
        upgrade_map = {KOLTier.C: KOLTier.B, KOLTier.B: KOLTier.A, KOLTier.A: KOLTier.S}
        if current in upgrade_map:
            self.kol_list[handle].tier = upgrade_map[current]

    def update_accuracy(self, handle: str, was_correct: bool):
        """更新 KOL 喊单准确率"""
        handle = handle.lstrip("@")
        if handle not in self.kol_list:
            return
        kol = self.kol_list[handle]
        kol.total_calls += 1
        if was_correct:
            kol.correct_calls += 1
        acc = kol.correct_calls / kol.total_calls
        kol.accuracy_history.append(acc)
        # 只保留最近 20 次
        if len(kol.accuracy_history) > 20:
            kol.accuracy_history = kol.accuracy_history[-20:]

    # ═══════════════════════════════
    # 提及记录
    # ═══════════════════════════════

    def record_mention(
        self,
        kol_handle: str,
        coin: str,
        text: str,
        price: Optional[float] = None,
        analyze_sentiment: bool = True,
    ) -> Optional[MentionRecord]:
        """
        记录一次 KOL 提及

        如果 kol_handle 在白名单中 → 记录 + 情绪分析
        如果在白名单外 → 忽略（减少噪声）
        """
        handle = kol_handle.lstrip("@")
        if handle not in self.kol_list:
            return None

        kol = self.kol_list[handle]
        text_hash = str(hash(text))

        # 去重：同一 KOL 短时间内相同内容不重复记录
        for m in reversed(self.mentions[-50:]):
            if m.kol_handle == handle and m.text_hash == text_hash:
                return None

        record = MentionRecord(
            kol_handle=handle,
            kol_tier=kol.tier,
            coin=coin.upper(),
            timestamp=datetime.utcnow().isoformat(),
            text_hash=text_hash,
            price_at_mention=price,
        )

        # DeepSeek 情绪分析
        if analyze_sentiment:
            result = self.ds.analyze_sentiment(text, coin)
            record.sentiment = result.get("sentiment", "neutral")
            record.sentiment_confidence = result.get("confidence", 0.3)
            record.is_price_prediction = result.get("is_price_prediction", False)
            record.is_data_driven = result.get("is_data_driven", False)
            record.narrative_tags = result.get("narrative_tags", [])

        self.mentions.append(record)
        return record

    def record_mentions_batch(
        self,
        mentions: list[dict],
        analyze_sentiment: bool = True,
    ) -> list[MentionRecord]:
        """
        批量记录提及

        mentions: [{'kol_handle': str, 'coin': str, 'text': str, 'price': float}, ...]
        """
        records = []
        for m in mentions:
            r = self.record_mention(
                kol_handle=m["kol_handle"],
                coin=m["coin"],
                text=m["text"],
                price=m.get("price"),
                analyze_sentiment=analyze_sentiment,
            )
            if r:
                records.append(r)
        return records

    # ═══════════════════════════════
    # 异常检测
    # ═══════════════════════════════

    def detect_anomalies(self, coin: str, hours: int = 24) -> list[SocialAnomaly]:
        """
        检测社交异常事件

        五种异常类型：
          1. mention_spike      — 提及量暴增（> 7日均值 × 3）
          2. kol_cluster        — KOL 集中提及（≥5 个/12h）
          3. social_price_divergence — 热度涨价格不动（可能有人布局）
          4. sentiment_flip     — 情绪极端逆转（24h 内翻转）
          5. c_tier_cluster     — C 级 KOL 扎堆喊单（出货前奏）
        """
        anomalies = []
        now = datetime.utcnow()
        cutoff = now - timedelta(hours=hours)

        # 此币种在时间窗口内的所有提及
        coin_mentions = [
            m for m in self.mentions
            if m.coin == coin.upper()
            and datetime.fromisoformat(m.timestamp) >= cutoff
        ]

        if not coin_mentions:
            return anomalies

        # ── 1. 提及量暴增 ──
        anomaly = self._check_mention_spike(coin, coin_mentions, hours)
        if anomaly:
            anomalies.append(anomaly)

        # ── 2. KOL 集中提及 ──
        anomaly = self._check_kol_cluster(coin, coin_mentions)
        if anomaly:
            anomalies.append(anomaly)

        # ── 3. 情绪极端逆转 ──
        anomaly = self._check_sentiment_flip(coin, coin_mentions)
        if anomaly:
            anomalies.append(anomaly)

        # ── 4. C 级 KOL 扎堆 ──
        anomaly = self._check_c_tier_cluster(coin, coin_mentions)
        if anomaly:
            anomalies.append(anomaly)

        return anomalies

    def _check_mention_spike(self, coin: str, recent: list[MentionRecord],
                              hours: int) -> Optional[SocialAnomaly]:
        """检测提及量暴增"""
        now = datetime.utcnow()
        # 最近 24h 提及数
        recent_24h = [m for m in self.mentions
                      if m.coin == coin.upper()
                      and datetime.fromisoformat(m.timestamp) >= now - timedelta(hours=24)]
        # 前 7 天日均
        older = [m for m in self.mentions
                 if m.coin == coin.upper()
                 and now - timedelta(days=7) <= datetime.fromisoformat(m.timestamp) < now - timedelta(hours=24)]
        avg_7d = len(older) / max((7 - 1), 1)

        if avg_7d > 0 and len(recent_24h) > avg_7d * 3:
            ratio = len(recent_24h) / avg_7d
            return SocialAnomaly(
                type="mention_spike",
                coin=coin,
                severity="medium" if ratio < 10 else "high",
                detail=f"提及量暴增 {ratio:.1f}x（24h: {len(recent_24h)} vs 7d日均: {avg_7d:.1f}）",
                timestamp=now.isoformat(),
                metrics={"ratio": ratio, "mentions_24h": len(recent_24h), "avg_7d": avg_7d},
            )
        return None

    def _check_kol_cluster(self, coin: str, recent: list[MentionRecord]) -> Optional[SocialAnomaly]:
        """检测 KOL 集中提及 — 高优先级"""
        now = datetime.utcnow()
        cutoff_12h = now - timedelta(hours=12)

        unique_kols_12h = set()
        s_a_tier_count = 0
        c_tier_count = 0
        for m in recent:
            t = datetime.fromisoformat(m.timestamp)
            if t >= cutoff_12h:
                unique_kols_12h.add(m.kol_handle)
                if m.kol_tier in (KOLTier.S, KOLTier.A):
                    s_a_tier_count += 1
                elif m.kol_tier == KOLTier.C:
                    c_tier_count += 1

        kol_count = len(unique_kols_12h)
        effective_count = s_a_tier_count * 3 + kol_count  # S/A级 1个顶3个

        if effective_count >= 5:
            return SocialAnomaly(
                type="kol_cluster",
                coin=coin,
                severity="high",
                detail=f"{kol_count} 个 KOL 在 12h 内提及（S/A级: {s_a_tier_count}, C级: {c_tier_count}）",
                timestamp=now.isoformat(),
                metrics={
                    "unique_kols": kol_count,
                    "s_a_tier": s_a_tier_count,
                    "c_tier": c_tier_count,
                    "effective_count": effective_count,
                },
                warning="⚠️ KOL 集中喊单通常不是好事 — 他们在同步出货计划" if c_tier_count >= 3 else "",
            )
        return None

    def _check_sentiment_flip(self, coin: str, recent: list[MentionRecord]) -> Optional[SocialAnomaly]:
        """检测 24h 内情绪极端逆转"""
        now = datetime.utcnow()
        cutoff_12h = now - timedelta(hours=12)

        first_half = []
        second_half = []
        for m in recent:
            t = datetime.fromisoformat(m.timestamp)
            if t >= cutoff_12h:
                second_half.append(m)
            else:
                first_half.append(m)

        if not first_half or not second_half:
            return None

        def avg_score(mentions):
            scores = [1 if m.sentiment == "positive" else -1 if m.sentiment == "negative" else 0
                      for m in mentions]
            return sum(scores) / len(scores) if scores else 0

        early_score = avg_score(first_half)
        late_score = avg_score(second_half)

        # 情绪从极端正向翻转到负向（或反之）
        if abs(early_score - late_score) > 1.2:
            direction = "看多→看空" if early_score > late_score else "看空→看多"
            return SocialAnomaly(
                type="sentiment_flip",
                coin=coin,
                severity="high",
                detail=f"24h 内情绪极端逆转: {direction} (前: {early_score:.2f}, 后: {late_score:.2f})",
                timestamp=now.isoformat(),
                metrics={"early_score": early_score, "late_score": late_score},
                warning="📢 情绪剧烈翻转 — 检查持仓风险" if early_score > late_score else "",
            )
        return None

    def _check_c_tier_cluster(self, coin: str, recent: list[MentionRecord]) -> Optional[SocialAnomaly]:
        """C 级 KOL 扎堆喊单 — 高概率是付费喊单/出货前奏"""
        now = datetime.utcnow()
        cutoff_12h = now - timedelta(hours=12)

        c_tier_kols = set()
        for m in recent:
            t = datetime.fromisoformat(m.timestamp)
            if t >= cutoff_12h and m.kol_tier == KOLTier.C:
                c_tier_kols.add(m.kol_handle)

        if len(c_tier_kols) >= 3:
            return SocialAnomaly(
                type="c_tier_cluster",
                coin=coin,
                severity="critical",
                detail=f"{len(c_tier_kols)} 个 C 级 KOL 在 12h 内扎堆喊单 — 高度疑似付费出货",
                timestamp=now.isoformat(),
                metrics={"c_tier_count": len(c_tier_kols), "kols": list(c_tier_kols)},
                warning="🚨 C级 KOL 扎堆 = 大概率接盘信号。不要跟单。",
            )
        return None

    # ═══════════════════════════════
    # 聚合查询
    # ═══════════════════════════════

    def get_coin_profile(self, coin: str, hours: int = 24) -> dict:
        """
        获取币种的 KOL 画像

        返回:
          {
            'coin': str,
            'total_mentions': int,
            'unique_kols': int,
            'first_mention_time': str | None,
            'kol_tier_distribution': {tier: count},
            'sentiment_distribution': {sentiment: count},
            'avg_sentiment_score': float,
            'narrative_tags': [str],
            'top_kols': [{'handle': str, 'tier': str, 'count': int}],
            'anomalies': [SocialAnomaly],
            'kol_lead_or_lag': 'lead' | 'lag' | 'unknown',
          }
        """
        coin = coin.upper()
        now = datetime.utcnow()
        cutoff = now - timedelta(hours=hours)

        coin_mentions = [
            m for m in self.mentions
            if m.coin == coin and datetime.fromisoformat(m.timestamp) >= cutoff
        ]

        if not coin_mentions:
            return {
                "coin": coin,
                "total_mentions": 0,
                "unique_kols": 0,
                "first_mention_time": None,
                "kol_tier_distribution": {},
                "sentiment_distribution": {},
                "avg_sentiment_score": 0.0,
                "narrative_tags": [],
                "top_kols": [],
                "anomalies": [],
                "kol_lead_or_lag": "unknown",
            }

        # 首次提及时间
        first_mention = min(coin_mentions, key=lambda m: m.timestamp)

        # KOL 分级分布
        tier_dist = defaultdict(int)
        for m in coin_mentions:
            tier_dist[m.kol_tier.value] += 1

        # 情绪分布
        sent_dist = defaultdict(int)
        for m in coin_mentions:
            sent_dist[m.sentiment] += 1

        # 综合情绪分
        scores = [1 if m.sentiment == "positive" else -1 if m.sentiment == "negative" else 0
                  for m in coin_mentions]
        avg_score = sum(scores) / len(scores) if scores else 0

        # 叙事标签聚合
        all_tags = []
        for m in coin_mentions:
            all_tags.extend(m.narrative_tags)
        tag_counts = defaultdict(int)
        for t in all_tags:
            tag_counts[t] += 1
        top_tags = sorted(tag_counts.items(), key=lambda x: -x[1])[:5]

        # Top KOL
        kol_counts = defaultdict(int)
        kol_tier_map = {}
        for m in coin_mentions:
            kol_counts[m.kol_handle] += 1
            kol_tier_map[m.kol_handle] = m.kol_tier.value
        top_kols = sorted(kol_counts.items(), key=lambda x: -x[1])[:5]
        top_kols = [{"handle": h, "tier": kol_tier_map[h], "count": c} for h, c in top_kols]

        # 异常检测
        anomalies = self.detect_anomalies(coin, hours)

        return {
            "coin": coin,
            "total_mentions": len(coin_mentions),
            "unique_kols": len(set(m.kol_handle for m in coin_mentions)),
            "first_mention_time": first_mention.timestamp,
            "kol_tier_distribution": dict(tier_dist),
            "sentiment_distribution": dict(sent_dist),
            "avg_sentiment_score": round(avg_score, 3),
            "narrative_tags": [t for t, _ in top_tags],
            "top_kols": top_kols,
            "anomalies": [asdict(a) for a in anomalies],
            "kol_lead_or_lag": "unknown",  # 需要价格数据才能判断
        }

    def get_aggregate_sentiment(self, coin: str, hours: int = 24) -> dict:
        """
        聚合情绪 — 用于融合评分

        返回（兼容 quant-kol skill 的输出格式）:
          {
            'bullish_ratio': 0.65,
            'bearish_ratio': 0.15,
            'neutral_ratio': 0.20,
            'total_mentions': int,
            'sentiment_score': -1.0 ~ +1.0,
            'mention_change_vs_7d': float,   # 提及量变化率
            'extreme_flag': bool,             # 是否极端（>80%同向）
            'kol_quality_score': 0-25,        # KOL 信号质量分
          }
        """
        coin = coin.upper()
        now = datetime.utcnow()
        cutoff = now - timedelta(hours=hours)

        coin_mentions = [
            m for m in self.mentions
            if m.coin == coin and datetime.fromisoformat(m.timestamp) >= cutoff
        ]

        total = len(coin_mentions)
        if total == 0:
            return {
                "bullish_ratio": 0, "bearish_ratio": 0, "neutral_ratio": 1.0,
                "total_mentions": 0, "sentiment_score": 0.0,
                "mention_change_vs_7d": 0.0, "extreme_flag": False,
                "kol_quality_score": 0,
            }

        bullish = sum(1 for m in coin_mentions if m.sentiment == "positive")
        bearish = sum(1 for m in coin_mentions if m.sentiment == "negative")
        neutral = total - bullish - bearish

        scores = [1 if m.sentiment == "positive" else -1 if m.sentiment == "negative" else 0
                  for m in coin_mentions]
        avg_sentiment = sum(scores) / len(scores)

        # 7 天对比
        older_cutoff = now - timedelta(days=7)
        older = [m for m in self.mentions
                 if m.coin == coin and older_cutoff <= datetime.fromisoformat(m.timestamp) < cutoff]
        older_daily_avg = len(older) / max(7 - hours/24, 1)
        recent_daily_avg = total / max(hours / 24, 0.04)  # 最少 1h
        if older_daily_avg > 0:
            change_vs_7d = (recent_daily_avg - older_daily_avg) / older_daily_avg
        else:
            change_vs_7d = 1.0 if total > 0 else 0.0

        # 极端检测
        max_ratio = max(bullish, bearish) / total if total > 0 else 0
        extreme_flag = max_ratio > 0.8

        # KOL 质量分 (0-25) — S/A/B 级提及加权
        quality_score = 0.0
        for m in coin_mentions:
            tier_weights = {KOLTier.S: 8, KOLTier.A: 5, KOLTier.B: 2, KOLTier.C: 0.5}
            w = tier_weights.get(m.kol_tier, 0)
            if m.is_data_driven:
                w *= 1.5
            if m.is_price_prediction:
                w *= 0.5
            quality_score += w
        quality_score = min(quality_score, 25)

        return {
            "bullish_ratio": round(bullish / total, 3),
            "bearish_ratio": round(bearish / total, 3),
            "neutral_ratio": round(neutral / total, 3),
            "total_mentions": total,
            "sentiment_score": round(avg_sentiment, 3),
            "mention_change_vs_7d": round(change_vs_7d, 2),
            "extreme_flag": extreme_flag,
            "kol_quality_score": round(quality_score, 1),
        }

    # ═══════════════════════════════
    # 辅助
    # ═══════════════════════════════

    def _print_tier_breakdown(self):
        """打印 KOL 分级统计"""
        tier_counts = defaultdict(int)
        for kol in self.kol_list.values():
            tier_counts[kol.tier.value] += 1
        print(f"   S级: {tier_counts['S']} | A级: {tier_counts['A']} | "
              f"B级: {tier_counts['B']} | C级: {tier_counts['C']}")

    def get_stats(self) -> dict:
        """模块运行统计"""
        return {
            "kol_count": len(self.kol_list),
            "total_mentions": len(self.mentions),
            "unique_coins": len(set(m.coin for m in self.mentions)),
            "deepseek_stats": self.ds.stats,
        }

    def save_state(self):
        """持久化提及记录"""
        os.makedirs(self.data_dir, exist_ok=True)
        filepath = os.path.join(self.data_dir, "mentions.json")
        with open(filepath, "w", encoding="utf-8") as f:
            json.dump([asdict(m) for m in self.mentions], f, ensure_ascii=False, indent=2)

    def load_state(self):
        """加载持久化的提及记录"""
        filepath = os.path.join(self.data_dir, "mentions.json")
        if os.path.exists(filepath):
            with open(filepath, "r", encoding="utf-8") as f:
                data = json.load(f)
            self.mentions = []
            for d in data:
                d["kol_tier"] = KOLTier(d["kol_tier"])
                self.mentions.append(MentionRecord(**d))
