"""
热门代币庄家动向 + 开单点位追踪 — Day 21 增强

实时追踪维度：
  1. 庄家动向 — 吸筹/出货/中性 判断
  2. 合约信号 — OI + 资金费率 + 多空比 + 清算地图
  3. 现货开单点位 — 支撑/阻力（订单簿深度聚类 + 链上成本基础）
  4. 合约开单点位 — 清算密集区 + OI 极值区

输出：
  - 每日热门代币动向日报
  - 开单点位更新（触发条件满足时推送）
  - 庄家行为变化告警

CT-DDD 边界：
  庄家动向属于灰箱域 → 不触发开仓，只提供参考点位 + 仓位调节
  只有策略信号能决定是否入场
"""

import json
import os
from dataclasses import dataclass, field, asdict
from datetime import datetime, timedelta
from enum import Enum
from typing import Optional
from collections import defaultdict


# ═══════════════════════════════
# 数据类型
# ═══════════════════════════════

class MMStance(str, Enum):
    """庄家姿态"""
    ACCUMULATING = "accumulating"        # 吸筹中
    DISTRIBUTING = "distributing"        # 出货中
    NEUTRAL = "neutral"                  # 中性
    ACCUMULATING_AGGRESSIVE = "accumulating_aggressive"  # 激进吸筹
    DISTRIBUTING_AGGRESSIVE = "distributing_aggressive"  # 激进出货


class TradeDirection(str, Enum):
    """交易方向"""
    LONG = "long"
    SHORT = "short"


@dataclass
class EntryZone:
    """开单区间"""
    price: float
    zone_low: float                    # 区间下沿
    zone_high: float                   # 区间上沿
    direction: TradeDirection
    confidence: str                    # high / medium / low
    signal_source: str                 # 信号来源
    stop_loss: float                   # 止损位
    take_profit_1: float               # 第一止盈
    take_profit_2: float               # 第二止盈
    risk_reward: float                 # 盈亏比
    suggested_size_pct: float          # 建议仓位（总资金%）
    notes: str = ""


@dataclass
class MMProfile:
    """庄家画像"""
    token: str
    timestamp: str
    stance: MMStance
    confidence: float                  # 0-1
    signals: dict = field(default_factory=dict)   # 各维度信号
    key_levels: dict = field(default_factory=dict) # 关键技术位
    position_recommendation: str = ""  # 仓位建议


@dataclass
class ContractSignal:
    """合约特有信号"""
    token: str
    timestamp: str
    oi_change_24h_pct: float           # OI 24h 变化率
    oi_price_divergence: str           # OI-价格 背离方向
    funding_rate_pct: float            # 当前资金费率（年化）
    funding_rate_percentile: float     # 资金费率在近30天的分位数
    long_short_ratio: float            # 多空比
    liquidation_clusters: list[dict]   # 清算密集区
    contract_sentiment: str            # bullish / bearish / neutral


# ═══════════════════════════════
# 庄家动向追踪器
# ═══════════════════════════════

class HotTokenTracker:
    """
    热门代币庄家动向追踪

    综合 5 个维度判断庄家正在做什么：
      1. 鲸鱼链上行为（权重 25%）
      2. 订单簿深度变化（权重 25%）
      3. OI + 资金费率（权重 20%，合约专属）
      4. 聪明钱地址活动（权重 20%）
      5. KOL/社交异常（权重 10%）
    """

    # 配置阈值
    ACCUMULATION_OI_DIVERGENCE = -0.05   # OI下跌5%+价格不动 → 吸筹
    DISTRIBUTION_OI_DIVERGENCE = 0.05    # OI上涨5%+价格不动 → 出货
    FUNDING_EXTREME_LONG = 50.0          # 年化 > 50% → 多头拥挤
    FUNDING_EXTREME_SHORT = -20.0        # 年化 < -20% → 空头拥挤
    LIQUIDATION_CLUSTER_RADIUS_PCT = 2.0 # 清算密集区聚合半径（价格%）

    def __init__(self, data_dir: Optional[str] = None):
        self.data_dir = data_dir or os.path.join(
            os.path.dirname(os.path.abspath(__file__)), "..", "data", "hot_tokens"
        )
        self.mm_profiles: dict[str, list[MMProfile]] = defaultdict(list)
        self.contract_signals: dict[str, list[ContractSignal]] = defaultdict(list)

    # ═══════════════════════════════
    # 庄家动向判断
    # ═══════════════════════════════

    def assess_mm_stance(
        self,
        token: str,
        whale_net_flow: Optional[dict] = None,        # 来自 WhaleTracker
        orderbook_signals: Optional[dict] = None,      # 来自 OrderBookMonitor
        smart_money_activity: Optional[dict] = None,   # 来自 SmartMoneyTracker
        oi_data: Optional[dict] = None,                # OI 变化数据
        funding_rate: Optional[float] = None,          # 资金费率（年化%）
        kol_anomalies: Optional[list] = None,          # 来自 KOLMonitor
    ) -> MMProfile:
        """
        综合判断庄家当前姿态

        返回 MMProfile 包含方向 + 置信度 + 各维度信号分解
        """
        now = datetime.utcnow().isoformat()
        scores = {"accumulation": 0.0, "distribution": 0.0}  # 正分 = 吸筹，负的绝对值 = 出货
        details = {}

        # ── 维度 1: 鲸鱼行为（25%）──
        whale_score = 0.0
        if whale_net_flow:
            net_signal = whale_net_flow.get("net_flow_signal", "neutral")
            if net_signal == "bullish":
                whale_score = 0.25
                scores["accumulation"] += 0.25
            elif net_signal == "bearish":
                whale_score = -0.25
                scores["distribution"] += 0.25
            details["whale"] = {
                "score": whale_score,
                "net_flow_signal": net_signal,
                "detail": whale_net_flow,
            }

        # ── 维度 2: 订单簿深度（25%）──
        ob_score = 0.0
        if orderbook_signals:
            mm_activity = orderbook_signals.get("mm_activity", {})
            mm_signal = mm_activity.get("signal", "NO_SIGNAL")
            if mm_signal == "MM_ACCUMULATING":
                ob_score = 0.25
                scores["accumulation"] += 0.25
            elif mm_signal == "MM_DISTRIBUTING":
                ob_score = -0.25
                scores["distribution"] += 0.25

            # 冰山订单加分项
            icebergs = orderbook_signals.get("iceberg_count", 0)
            if icebergs > 0:
                if mm_signal == "MM_ACCUMULATING":
                    scores["accumulation"] += 0.05  # 冰山买单 = 吸筹确认
                elif mm_signal == "MM_DISTRIBUTING":
                    scores["distribution"] += 0.05  # 冰山卖单 = 出货确认

            details["orderbook"] = {
                "score": ob_score,
                "mm_signal": mm_signal,
                "iceberg_count": icebergs,
                "orderbook_score": orderbook_signals.get("orderbook_score", 5.0),
            }

        # ── 维度 3: OI + 资金费率（20%，合约专属）──
        oi_score = 0.0
        if oi_data is not None or funding_rate is not None:
            oi_change = oi_data.get("oi_change_24h_pct", 0) if oi_data else 0
            price_change = oi_data.get("price_change_24h_pct", 0) if oi_data else 0

            # OI-价格 背离分析（最重要）
            if oi_change < self.ACCUMULATION_OI_DIVERGENCE * 100 and price_change > -2:
                # OI 大降 + 价格没跌 → 空头在撤退/多头在吸筹
                oi_score += 0.15
                scores["accumulation"] += 0.15
            elif oi_change > self.DISTRIBUTION_OI_DIVERGENCE * 100 and price_change < 2:
                # OI 大增 + 价格没涨 → 多头拥挤/庄家出货
                oi_score -= 0.15
                scores["distribution"] += 0.15

            # 资金费率极端值
            if funding_rate is not None:
                if funding_rate > self.FUNDING_EXTREME_LONG:
                    # 极高正费率 → 多头拥挤 → 容易被清算
                    oi_score -= 0.05
                    scores["distribution"] += 0.05
                elif funding_rate < self.FUNDING_EXTREME_SHORT:
                    # 极低/负费率 → 空头拥挤 → 容易逼空
                    oi_score += 0.05
                    scores["accumulation"] += 0.05

            details["contract"] = {
                "score": oi_score,
                "oi_change_24h_pct": oi_change,
                "price_change_24h_pct": price_change,
                "funding_rate_annual": funding_rate,
                "oi_price_divergence": (
                    "bullish" if oi_score > 0.05
                    else "bearish" if oi_score < -0.05
                    else "neutral"
                ),
            }

        # ── 维度 4: 聪明钱（20%）──
        sm_score = 0.0
        if smart_money_activity:
            strength = smart_money_activity.get("signal_strength", "none")
            high_win = smart_money_activity.get("high_win_rate_wallets", 0)
            if strength == "strong":
                sm_score = 0.20
                scores["accumulation"] += 0.20
            elif strength == "moderate":
                sm_score = 0.10
                scores["accumulation"] += 0.10
            elif strength == "weak":
                sm_score = 0.05
                scores["accumulation"] += 0.05
            details["smart_money"] = {
                "score": sm_score,
                "signal_strength": strength,
                "high_win_rate_wallets": high_win,
                "buying_wallets": smart_money_activity.get("buying_wallets", 0),
            }

        # ── 维度 5: KOL 异常（10%）──
        kol_score = 0.0
        if kol_anomalies:
            for a in kol_anomalies:
                if a.type == "c_tier_cluster":
                    # C级 KOL 扎堆 = 出货信号
                    kol_score -= 0.10
                    scores["distribution"] += 0.10
                elif a.type == "kol_cluster" and a.severity == "high":
                    kol_score -= 0.05
                    scores["distribution"] += 0.05
            details["kol"] = {
                "score": kol_score,
                "anomaly_count": len(kol_anomalies),
                "anomalies": [asdict(a) for a in kol_anomalies],
            }

        # ── 综合判断 ──
        total_accumulation = scores["accumulation"]
        total_distribution = scores["distribution"]

        if total_accumulation > 0.5:
            stance = MMStance.ACCUMULATING_AGGRESSIVE
            confidence = min(total_accumulation, 1.0)
        elif total_accumulation > 0.25:
            stance = MMStance.ACCUMULATING
            confidence = min(total_accumulation, 1.0)
        elif total_distribution > 0.5:
            stance = MMStance.DISTRIBUTING_AGGRESSIVE
            confidence = min(total_distribution, 1.0)
        elif total_distribution > 0.25:
            stance = MMStance.DISTRIBUTING
            confidence = min(total_distribution, 1.0)
        else:
            stance = MMStance.NEUTRAL
            confidence = 1.0 - abs(total_accumulation - total_distribution)

        profile = MMProfile(
            token=token.upper(),
            timestamp=now,
            stance=stance,
            confidence=round(confidence, 2),
            signals={
                "accumulation_score": round(total_accumulation, 2),
                "distribution_score": round(total_distribution, 2),
                "breakdown": details,
            },
            position_recommendation=self._stance_to_recommendation(stance, confidence),
        )

        self.mm_profiles[token].append(profile)
        return profile

    # ═══════════════════════════════
    # 开单点位计算
    # ═══════════════════════════════

    def calculate_entry_zones(
        self,
        token: str,
        current_price: float,
        orderbook_snapshots: Optional[list] = None,  # DepthSnapshot list
        whale_cost_basis: Optional[float] = None,     # 鲸鱼平均成本
        liquidation_map: Optional[list[dict]] = None, # 清算地图
        atr: Optional[float] = None,                  # ATR(14)
        mm_stance: Optional[MMStance] = None,
    ) -> dict:
        """
        计算现货 + 合约开单点位

        返回:
          {
            'spot': {'long_zones': [...], 'short_zones': [...]},
            'contract': {'long_zones': [...], 'short_zones': [...]},
            'key_support': float,
            'key_resistance': float,
            'summary': str,
          }
        """
        if atr is None:
            atr = current_price * 0.03  # 默认 3% ATR

        spot_long = []
        spot_short = []
        contract_long = []
        contract_short = []

        # ── 现货做多点位 ──
        supports = self._find_support_levels(current_price, orderbook_snapshots, whale_cost_basis, atr)
        for i, support in enumerate(supports[:3]):
            sl = support["price"] - atr * 0.5  # 支撑下方 0.5 ATR
            tp1 = support["price"] + atr * 1.5
            tp2 = support["price"] + atr * 3.0
            rr = (tp1 - support["price"]) / (support["price"] - sl)
            spot_long.append(EntryZone(
                price=support["price"],
                zone_low=support["price"] * 0.995,
                zone_high=support["price"] * 1.005,
                direction=TradeDirection.LONG,
                confidence="high" if support["strength"] == "strong" else "medium",
                signal_source=support["source"],
                stop_loss=round(sl, 4),
                take_profit_1=round(tp1, 4),
                take_profit_2=round(tp2, 4),
                risk_reward=round(rr, 1),
                suggested_size_pct=0.5 if support["strength"] == "strong" else 0.3,
                notes=f"支撑位: {support['source']}, 强度: {support['strength']}",
            ))

        # ── 现货做空点位 ──
        resistances = self._find_resistance_levels(current_price, orderbook_snapshots, atr)
        for i, res in enumerate(resistances[:3]):
            sl = res["price"] + atr * 0.5
            tp1 = res["price"] - atr * 1.5
            tp2 = res["price"] - atr * 3.0
            rr = (res["price"] - tp1) / (sl - res["price"])
            spot_short.append(EntryZone(
                price=res["price"],
                zone_low=res["price"] * 0.995,
                zone_high=res["price"] * 1.005,
                direction=TradeDirection.SHORT,
                confidence="high" if res["strength"] == "strong" else "medium",
                signal_source=res["source"],
                stop_loss=round(sl, 4),
                take_profit_1=round(tp1, 4),
                take_profit_2=round(tp2, 4),
                risk_reward=round(rr, 1),
                suggested_size_pct=0.5 if res["strength"] == "strong" else 0.3,
                notes=f"阻力位: {res['source']}, 强度: {res['strength']}",
            ))

        # ── 合约做多点位（叠加清算地图）──
        if liquidation_map:
            # 下方清算密集区 = 支撑/做多目标
            liq_clusters_long = self._find_liquidation_clusters(
                liquidation_map, current_price, "below"
            )
            for cluster in liq_clusters_long[:2]:
                sl = cluster["price"] - atr * 0.3  # 清算位下方 0.3 ATR
                tp1 = cluster["price"] + atr * 1.0
                rr = (tp1 - cluster["price"]) / (cluster["price"] - sl)
                contract_long.append(EntryZone(
                    price=cluster["price"],
                    zone_low=cluster["price"] * 0.997,
                    zone_high=cluster["price"] * 1.003,
                    direction=TradeDirection.LONG,
                    confidence="medium",
                    signal_source=f"清算密集区 ({cluster['liq_amount']:.0f} USDT)",
                    stop_loss=round(sl, 4),
                    take_profit_1=round(tp1, 4),
                    take_profit_2=round(cluster["price"] + atr * 2.0, 4),
                    risk_reward=round(rr, 1),
                    suggested_size_pct=0.3,
                    notes=f"合约: 下方清算墙, 爆仓量 {cluster['liq_amount']:.0f} USDT",
                ))

            # 上方清算密集区 = 做空目标
            liq_clusters_short = self._find_liquidation_clusters(
                liquidation_map, current_price, "above"
            )
            for cluster in liq_clusters_short[:2]:
                sl = cluster["price"] + atr * 0.3
                tp1 = cluster["price"] - atr * 1.0
                rr = (cluster["price"] - tp1) / (sl - cluster["price"])
                contract_short.append(EntryZone(
                    price=cluster["price"],
                    zone_low=cluster["price"] * 0.997,
                    zone_high=cluster["price"] * 1.003,
                    direction=TradeDirection.SHORT,
                    confidence="medium",
                    signal_source=f"清算密集区 ({cluster['liq_amount']:.0f} USDT)",
                    stop_loss=round(sl, 4),
                    take_profit_1=round(tp1, 4),
                    take_profit_2=round(cluster["price"] - atr * 2.0, 4),
                    risk_reward=round(rr, 1),
                    suggested_size_pct=0.3,
                    notes=f"合约: 上方清算墙, 爆仓量 {cluster['liq_amount']:.0f} USDT",
                ))

        # ── 庄家姿态修正仓位 ──
        if mm_stance:
            self._apply_mm_stance_adjustment(spot_long, spot_short,
                                              contract_long, contract_short, mm_stance)

        # ── 关键位置 ──
        key_support = supports[0]["price"] if supports else current_price * 0.95
        key_resistance = resistances[0]["price"] if resistances else current_price * 1.05

        # ── 综合建议 ──
        summary = self._generate_entry_summary(
            token, current_price, spot_long, spot_short,
            contract_long, contract_short, mm_stance
        )

        return {
            "token": token,
            "current_price": current_price,
            "atr": round(atr, 4),
            "spot": {
                "long_zones": [asdict(z) for z in spot_long],
                "short_zones": [asdict(z) for z in spot_short],
            },
            "contract": {
                "long_zones": [asdict(z) for z in contract_long],
                "short_zones": [asdict(z) for z in contract_short],
            },
            "key_support": round(key_support, 4),
            "key_resistance": round(key_resistance, 4),
            "mm_stance": mm_stance.value if mm_stance else "unknown",
            "summary": summary,
            "timestamp": datetime.utcnow().isoformat(),
        }

    # ═══════════════════════════════
    # 合约信号分析
    # ═══════════════════════════════

    def analyze_contract_signals(
        self,
        token: str,
        oi_change_24h_pct: float,
        price_change_24h_pct: float,
        funding_rate_annual: float,
        funding_rate_30d_history: Optional[list[float]] = None,
        long_short_ratio: Optional[float] = None,
        liquidation_map: Optional[list[dict]] = None,
    ) -> ContractSignal:
        """
        分析合约特有信号

        OI-价格 四象限：
          OI↑ + 价格↑ = 多头建仓（趋势延续）
          OI↑ + 价格↓ = 空头建仓（趋势延续）
          OI↓ + 价格↑ = 空头撤退（可能反转）
          OI↓ + 价格↓ = 多头撤退（可能反转）
        """
        now = datetime.utcnow().isoformat()

        # OI-价格 背离
        if oi_change_24h_pct > 3 and price_change_24h_pct > 0:
            divergence = "long_building"       # 多头正在建仓
        elif oi_change_24h_pct > 3 and price_change_24h_pct < 0:
            divergence = "short_building"      # 空头正在建仓
        elif oi_change_24h_pct < -3 and price_change_24h_pct > 0:
            divergence = "short_covering"      # 空头撤退/逼空
        elif oi_change_24h_pct < -3 and price_change_24h_pct < 0:
            divergence = "long_liquidating"    # 多头撤退/多杀多
        elif abs(oi_change_24h_pct) < 1 and abs(price_change_24h_pct) < 2:
            divergence = "consolidation"       # 横盘
        else:
            divergence = "mixed"

        # 资金费率分位数
        percentile = 0.5
        if funding_rate_30d_history:
            sorted_rates = sorted(funding_rate_30d_history)
            idx = sum(1 for r in sorted_rates if r <= funding_rate_annual)
            percentile = idx / len(sorted_rates)

        # 清算密集区
        clusters = self._find_liquidation_clusters(
            liquidation_map or [], 0, "all"
        )

        # 合约情绪
        sentiment = "neutral"
        if divergence in ("long_building", "short_covering"):
            sentiment = "bullish"
        elif divergence in ("short_building", "long_liquidating"):
            sentiment = "bearish"
        if funding_rate_annual > self.FUNDING_EXTREME_LONG:
            sentiment = "bearish"  # 多头拥挤 = 偏空
        elif funding_rate_annual < self.FUNDING_EXTREME_SHORT:
            sentiment = "bullish"  # 空头拥挤 = 偏多

        signal = ContractSignal(
            token=token.upper(),
            timestamp=now,
            oi_change_24h_pct=oi_change_24h_pct,
            oi_price_divergence=divergence,
            funding_rate_pct=funding_rate_annual,
            funding_rate_percentile=round(percentile, 2),
            long_short_ratio=long_short_ratio or 1.0,
            liquidation_clusters=clusters,
            contract_sentiment=sentiment,
        )

        self.contract_signals[token].append(signal)
        return signal

    # ═══════════════════════════════
    # 内部：支撑/阻力发现
    # ═══════════════════════════════

    def _find_support_levels(
        self, current_price: float, snapshots: Optional[list],
        whale_cost: Optional[float], atr: float
    ) -> list[dict]:
        """发现支撑位"""
        levels = []

        # 1. 订单簿深度聚类
        if snapshots and len(snapshots) > 0:
            latest = snapshots[-1]
            bid_walls = []
            for bid in latest.bids:
                if bid.amount > 2.0:  # 挂单 > 2 BTC/ETH/SOL
                    bid_walls.append({"price": bid.price, "amount": bid.amount})
            bid_walls.sort(key=lambda x: -x["amount"])
            for bw in bid_walls[:2]:
                if bw["price"] < current_price:
                    levels.append({
                        "price": bw["price"],
                        "strength": "strong" if bw["amount"] > 5 else "medium",
                        "source": f"订单簿买墙 ({bw['amount']:.1f} 个)",
                    })

        # 2. 鲸鱼平均成本（链上）
        if whale_cost and whale_cost < current_price:
            levels.append({
                "price": whale_cost,
                "strength": "strong",
                "source": "鲸鱼平均成本",
            })

        # 3. ATR 支撑
        levels.append({
            "price": current_price - atr,
            "strength": "medium",
            "source": f"ATR({1.0:.0f}x) 支撑",
        })
        levels.append({
            "price": current_price - atr * 2,
            "strength": "weak",
            "source": f"ATR({2.0:.0f}x) 支撑",
        })

        # 排序：距当前价最近优先
        levels.sort(key=lambda x: abs(x["price"] - current_price))
        return levels

    def _find_resistance_levels(
        self, current_price: float, snapshots: Optional[list], atr: float
    ) -> list[dict]:
        """发现阻力位"""
        levels = []

        if snapshots and len(snapshots) > 0:
            latest = snapshots[-1]
            ask_walls = []
            for ask in latest.asks:
                if ask.amount > 2.0:
                    ask_walls.append({"price": ask.price, "amount": ask.amount})
            ask_walls.sort(key=lambda x: -x["amount"])
            for aw in ask_walls[:2]:
                if aw["price"] > current_price:
                    levels.append({
                        "price": aw["price"],
                        "strength": "strong" if aw["amount"] > 5 else "medium",
                        "source": f"订单簿卖墙 ({aw['amount']:.1f} 个)",
                    })

        levels.append({
            "price": current_price + atr,
            "strength": "medium",
            "source": f"ATR({1.0:.0f}x) 阻力",
        })
        levels.append({
            "price": current_price + atr * 2,
            "strength": "weak",
            "source": f"ATR({2.0:.0f}x) 阻力",
        })

        levels.sort(key=lambda x: abs(x["price"] - current_price))
        return levels

    def _find_liquidation_clusters(
        self, liquidation_map: list[dict], current_price: float, side: str
    ) -> list[dict]:
        """
        从清算地图找密集区

        side: "above" | "below" | "all"
        """
        if not liquidation_map:
            return []

        clusters = []
        for liq in liquidation_map:
            price = liq.get("price", 0)
            if side == "above" and price <= current_price:
                continue
            if side == "below" and price >= current_price:
                continue
            # 聚合相近价位
            merged = False
            for c in clusters:
                if abs(price - c["price"]) / c["price"] < self.LIQUIDATION_CLUSTER_RADIUS_PCT / 100:
                    c["liq_amount"] += liq.get("amount", 0)
                    merged = True
                    break
            if not merged:
                clusters.append({
                    "price": price,
                    "liq_amount": liq.get("amount", 0),
                })

        clusters.sort(key=lambda x: -x["liq_amount"])
        return clusters[:5]

    def _apply_mm_stance_adjustment(
        self, spot_long: list, spot_short: list,
        contract_long: list, contract_short: list, stance: MMStance
    ):
        """根据庄家姿态调整开单点位建议仓位"""
        if stance in (MMStance.ACCUMULATING, MMStance.ACCUMULATING_AGGRESSIVE):
            # 庄家在吸筹 → 做多点位加仓，做空点位减仓
            for z in spot_long:
                z.suggested_size_pct *= 1.5
                z.confidence = "high" if z.confidence == "medium" else z.confidence
            for z in spot_short:
                z.suggested_size_pct *= 0.3
                z.confidence = "low"
            for z in contract_long:
                z.suggested_size_pct *= 1.5
            for z in contract_short:
                z.suggested_size_pct *= 0.3

        elif stance in (MMStance.DISTRIBUTING, MMStance.DISTRIBUTING_AGGRESSIVE):
            # 庄家在出货 → 做空点位加仓，做多点位减仓
            for z in spot_short:
                z.suggested_size_pct *= 1.5
                z.confidence = "high" if z.confidence == "medium" else z.confidence
            for z in spot_long:
                z.suggested_size_pct *= 0.3
                z.confidence = "low"
            for z in contract_short:
                z.suggested_size_pct *= 1.5
            for z in contract_long:
                z.suggested_size_pct *= 0.3

    def _generate_entry_summary(
        self, token: str, current_price: float,
        spot_long: list, spot_short: list,
        contract_long: list, contract_short: list,
        mm_stance: Optional[MMStance],
    ) -> str:
        """生成开单点位摘要"""
        parts = [f"{token} @ {current_price:.2f}"]

        if mm_stance:
            stance_labels = {
                MMStance.ACCUMULATING: "庄家吸筹中 → 优先做多",
                MMStance.ACCUMULATING_AGGRESSIVE: "庄家激进吸筹 → 只做多",
                MMStance.DISTRIBUTING: "庄家出货中 → 优先做空",
                MMStance.DISTRIBUTING_AGGRESSIVE: "庄家激进出货 → 只做空",
                MMStance.NEUTRAL: "庄家中性 → 多空皆可",
            }
            parts.append(stance_labels.get(mm_stance, ""))

        if spot_long:
            best = spot_long[0]
            parts.append(f"现货做多: {best.zone_low:.2f}-{best.zone_high:.2f} "
                         f"(SL:{best.stop_loss:.2f}, TP1:{best.take_profit_1:.2f}, "
                         f"RR:{best.risk_reward})")

        if spot_short:
            best = spot_short[0]
            parts.append(f"现货做空: {best.zone_low:.2f}-{best.zone_high:.2f} "
                         f"(SL:{best.stop_loss:.2f}, TP1:{best.take_profit_1:.2f}, "
                         f"RR:{best.risk_reward})")

        if contract_long:
            best = contract_long[0]
            parts.append(f"合约做多: {best.zone_low:.2f}-{best.zone_high:.2f} "
                         f"({best.signal_source})")

        if contract_short:
            best = contract_short[0]
            parts.append(f"合约做空: {best.zone_low:.2f}-{best.zone_high:.2f} "
                         f"({best.signal_source})")

        return " | ".join(parts)

    # ═══════════════════════════════
    # 辅助
    # ═══════════════════════════════

    def _stance_to_recommendation(self, stance: MMStance, confidence: float) -> str:
        conf_pct = confidence * 100
        if stance == MMStance.ACCUMULATING_AGGRESSIVE:
            return f"🔥 庄家激进吸筹（置信度 {conf_pct:.0f}%）— 只做多不做空，仓位可适当放大"
        elif stance == MMStance.ACCUMULATING:
            return f"📈 庄家在吸筹（置信度 {conf_pct:.0f}%）— 做多优先，做空谨慎"
        elif stance == MMStance.DISTRIBUTING_AGGRESSIVE:
            return f"🔥 庄家激进出货（置信度 {conf_pct:.0f}%）— 只做空不做多"
        elif stance == MMStance.DISTRIBUTING:
            return f"📉 庄家在出货（置信度 {conf_pct:.0f}%）— 做空优先，做多谨慎"
        return f"➖ 庄家方向不明（置信度 {conf_pct:.0f}%）— 轻仓按策略信号操作"

    def get_latest_mm_stance(self, token: str) -> Optional[MMProfile]:
        """获取最新的庄家姿态"""
        if token in self.mm_profiles and self.mm_profiles[token]:
            return self.mm_profiles[token][-1]
        return None

    def get_stance_history(self, token: str, limit: int = 20) -> list[MMProfile]:
        """获取庄家姿态历史"""
        if token in self.mm_profiles:
            return self.mm_profiles[token][-limit:]
        return []

    def get_stats(self) -> dict:
        return {
            "tracked_tokens": len(self.mm_profiles),
            "total_profiles": sum(len(v) for v in self.mm_profiles.values()),
            "total_contract_signals": sum(len(v) for v in self.contract_signals.values()),
        }
