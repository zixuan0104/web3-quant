---
name: quant-binance-alpha
description: 币安 Alpha 监控 — 追踪即将上币公告、新币上线后做市商信号、暴涨前共性征兆综合评分。不跟单，只做监控提醒。
---

# 币安 Alpha 监控

追踪币安上币流程中的 alpha 机会。核心不是"猜哪个币会上币安"，而是在上币公告后判断这个币有没有持续做市的迹象。有做市商托底的币有二次机会，没有托底的币上线即巅峰。

## 硬性规则

1. **不猜上币**：不在传闻期重仓赌上币。那是信息不对称游戏，你天然在劣势方。
2. **不做上线首日**：上线首日高波动，做市商在建立初始流动性，散户在 FOMO 和恐慌。远离。
3. **四阶段模型**：传闻期→公告期→上线首日→沉淀期。你的机会在沉淀期的二次确认。
4. **提醒 ≠ 下单**：一切分析结果走 Telegram 提醒，不自动下单。

## 四阶段模型与策略

```
传闻期（暗暗涨）
├── 特征：链上监控到异常、内幕盘在吸筹
├── 信号：聪明钱地址持续买入、交易所净流出
├── 风险：不确定性最高，可能是假消息
└── 你的动作：标记关注，不上仓位

公告期（爆拉）
├── 特征：官方公告币安将上线，价格瞬间拉升 30-100%
├── 信号：公告时间戳 → 第一时间抢跑的量化机器人买入
├── 风险：提前埋伏的人在出货，追进去大概率接盘
└── 你的动作：记录公告时间和价格，等上线后沉淀期判断

上线首日（高波动）
├── 特征：做市商建初始流动性，价格剧烈波动
├── 信号：无可靠信号，噪声占主导
├── 风险：极高。价格可以涨 200% 也可以跌 80%
└── 你的动作：远离。不交易。

沉淀期（回归价值 ← 你的机会区）
├── 特征：上线 3-14 天后，投机资金退出，真实买盘和做市商行为浮现
├── 入场条件（必须同时满足）：
│   ├── 价格在 7 日均线上方企稳
│   ├── 订单簿深度持续 3 天增长（做市商在托底）
│   ├── 成交量从上线峰值萎缩 70%+ 后企稳
│   └── 项目叙事仍然成立（不是一波流题材）
└── 你的动作：策略正常评估，和小币种同等对待（风控更严格）
```

## 第 1 步：公告监控

```python
class BinanceListingMonitor:
    """
    监控币安上币公告
    
    数据源：
    1. 币安官方 API / 公告页面
    2. 币安 Twitter (@binance)
    3. 第三方聚合（CoinGecko 新上架列表）
    """
    
    def __init__(self):
        self.announced_listings = []   # 已公告但未上线
        self.recent_listings = []      # 过去 30 天上线的币
        self.watching = []             # 沉淀期观察列表
    
    def check_new_announcement(self):
        """
        检查币安是否发布了新上币公告
        触发条件：检测到新公告 → 立即 Telegram 推送
        """
        pass
    
    def track_listing_phases(self, coin):
        """
        追踪一个币从上币公告到沉淀期的完整过程
        - 公告日：D-day
        - 上线日：D+0
        - 沉淀期开始：D+3 ~ D+14
        """
        pass
```

## 第 2 步：沉淀期做市商信号评分

对于上线 3-14 天的新币，每日计算做市商质量评分：

```python
def score_new_listing_mm_quality(coin, days_since_listing):
    """
    做市商质量评分（0-10）
    只有在新币上线 3 天后才开始评估
    """
    if days_since_listing < 3:
        return {'score': None, 'reason': 'too_early'}
    
    signals = {
        # 1. 订单簿深度趋势（权重 30%）
        'depth_trend': check_depth_trend(coin),  # 最近 3 天深度在增长还是萎缩
        
        # 2. 做市商已知地址活动（权重 25%）
        'mm_address_activity': check_known_mm_activity(coin),
        # Wintermute / GSR / Jump 等是否在操作这个币
        
        # 3. 买卖价差（权重 20%）
        'spread': check_spread(coin),  # 价差在缩小 = 流动性在改善
        
        # 4. 价格稳定性（权重 15%）
        'price_stability': check_price_stability(coin),
        # 不是说不波动，而是波动有逻辑（有支撑有压力），不是无序乱跳
        
        # 5. 交易所净流入/流出（权重 10%）
        'net_flow': check_exchange_net_flow(coin),
    }
    
    weighted_score = sum(s[k] * w[k] for k, (s, w) in signals.items())
    return {'score': weighted_score, 'signals': signals}
```

## 第 3 步：暴涨前共性征兆评分

基于我们讨论过的 7 个高可靠性征兆（需同时出现 ≥3 个才关注）：

```python
def score_pre_pump_signals(coin):
    """
    暴涨前征兆评分
    
    每个征兆 0-1 分（1 = 完全满足），总分 0-7
    总分 ≥ 3 分 → Telegram 提醒
    总分 ≥ 5 分 → 强提醒（仍需人工判断）
    """
    signals = {
        # ⭐⭐⭐⭐⭐ 最高可靠性
        'oi_buildup': {  # OI 低位磨底后突然放大 + 价格不动
            'check': lambda: (oi_30d_low(coin) and oi_spike(coin) and not price_moved(coin)),
            'weight': 1.0,
        },
        
        # ⭐⭐⭐⭐
        'funding_rate_normalize': {  # 资金费率从极度负值恢复中性
            'check': lambda: (funding_rate_was_extreme_negative(coin) and funding_rate_normalizing(coin)),
            'weight': 1.0,
        },
        'volume_dry_up_then_spike': {  # 地量后放量
            'check': lambda: (volume_at_30d_low(coin) and volume_spiking(coin)),
            'weight': 1.0,
        },
        'smart_money_accumulating': {  # 多个聪明钱加仓
            'check': lambda: count_smart_money_buying(coin) >= 5,
            'weight': 1.0,
        },
        
        # ⭐⭐⭐
        'mm_bid_wall_thickening': {  # 做市商买单墙在低位持续加厚
            'check': lambda: bid_wall_trend(coin) == 'increasing' and duration > 6 * 3600,
            'weight': 0.7,
        },
        'social_mention_spike': {  # 社交提及飙升 + 价格未动
            'check': lambda: (social_mentions(coin) > avg * 3 and not price_moved(coin)),
            'weight': 0.7,
        },
        'multiple_kol_mention': {  # 5+ KOL 同时提及
            'check': lambda: count_kol_mentions(coin, hours=12) >= 5,
            'weight': 0.5,  # 最低权重——可能是出货前奏
        },
    }
    
    total_score = sum(
        s['check']() * s['weight']
        for s in signals.values()
    )
    
    return {
        'total_score': total_score,
        'threshold_met': total_score >= 3.0,
        'breakdown': {name: s['check']() for name, s in signals.items()},
    }
```

## 第 4 步：假暴涨信号辨别（反向信号）

```python
def detect_fake_pump_signals(coin):
    """
    这些信号出现 = 可能是假暴涨 = 不跟
    
    返回值：risk_score: 0-10（越高越像假暴涨）
    """
    risks = {
        'price_and_oi_both_spike': check_both_spike(coin),
        # 价格和 OI 同时爆拉 = 涨幅已经兑现，接力资金在赌继续涨
        # 信号：TRUE = 危险
        
        'kol_pumping_at_oi_high': check_kol_pump_oi_high(coin),
        # KOL 喊单 + OI 历史高位 = 大户在出货找接盘侠
        # 信号：TRUE = 危险
        
        'single_wick_reversal': check_single_wick(coin),
        # 只拉一根长上影就跌回来 = 爆仓猎杀，不是建仓
        # 信号：TRUE = 危险
        
        'volume_concentrated_1min': check_volume_concentration(coin),
        # 当日成交量 > 30% 集中在 1 分钟内 = 程序对倒，不是真实买盘
        # 信号：TRUE = 危险
    }
    
    risk_score = sum(2.5 for r in risks.values() if r)  # 每项 2.5 分
    return risk_score
```

## 第 5 步：综合决策输出

```
每日对监控列表中的每个新币输出：

═══════════════════════════════════
[COIN] Alpha 评估 — 2025-07-05
═══════════════════════════════════
上线日期：2025-07-01（上线第 5 天）
当前阶段：沉淀期 ✅

📊 做市商质量：7.5/10
├── 深度趋势：✅ 持续增长
├── 做市商活动：✅ Wintermute 地址活跃
├── 买卖价差：⚠️ 0.15%（偏大，但收窄中）
├── 价格稳定性：✅ 窄幅横盘，有支撑
└── 净流入/流出：✅ 交易所净流出（积累）

🔮 暴涨前征兆：3.7/7（≥3 触发关注）
├── ✅ OI 低位放量：+1.0
├── ✅ 聪明钱加仓：+1.0（6/10 监控地址近期买入）
├── ✅ 做市商买单墙加厚：+0.7
├── ❌ 资金费率正常化：未触发
├── ❌ 地量放量：未触发（量还不够低）
├── ❌ 社交飙升：未触发
└── ❌ KOL 提及：未触发

🛡️ 假暴涨风险：2.5/10（低）
└── 无明显假暴涨信号

📋 结论：
├── 做市商在认真做事，不是一波流
├── 暴涨前 7 征兆中 3 个触发（刚好过线）
├── 假暴涨风险低
└── 下次策略评估时该币可以作为正常标的（仓位上限减半）

⚠️ 提醒级别：🟡 关注（综合分 25/40）
═══════════════════════════════════
```

## 融入冲刺月

- **Day 1**：数据管线接入币安公告 API
- **Day 19-20**：KOL/链上数据接入后，Alpha 监控模块上线
- **Week 3 日报**：新币观察列表纳入每日报告
- **Week 4 驾驶舱**：Alpha 监控面板独立 Tab

## 必避的坑

| 坑 | 现实 |
|------|------|
| "这个币肯定上币安" | 你听到这个消息的时候，比你早知道的人已经建仓了 |
| 上线就冲进去 | 上线首日是出货日，早期投资者把币转到币安卖给你 |
| 看到 KOL 喊就追 | KOL 喊的时候他可能正在出货 |
| 以为做市商在"拉盘" | 做市商的职责是提供流动性，不是帮你拉盘。他们在赚价差和返佣 |
