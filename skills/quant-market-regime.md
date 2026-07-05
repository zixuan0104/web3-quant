---
name: quant-market-regime
description: 市场环境分类器 — 每日自动判断当前市场处于什么环境（趋势/震荡/高波/低波），输出各策略适宜度分数，自动调整仓位。
---

# 市场环境分类器

策略的表现是市场环境的函数。趋势策略死于震荡市，均值回归死于趋势市。区分"策略失效"和"环境不对"，是你能不能坚持到策略重新赚钱的关键。

## 硬性规则

1. **每天跑一次**：在 UTC 00:05（日报前）自动计算。不实时，因为环境切换是以天为单位的。
2. **输出给风控**：分类结果直接输入 RiskManager，自动调整仓位倍数。
3. **不预测切换**：只判断"现在是什么"，不预测"接下来会变成什么"。环境切换是不可预测的。
4. **不把环境分类当作交易信号**：它是仓位调节器，不是开仓触发器。

## 第 1 步：四象限分类

```python
class MarketRegimeClassifier:
    """
    市场环境分类器
    
    两个维度：
    X 轴：波动率（低 ← → 高）
    Y 轴：趋势强度（弱/震荡 ← → 强/趋势）
    
    四个象限：
    ┌──────────────┬──────────────┐
    │  高波 + 震荡  │  高波 + 趋势  │
    │  (极端市)    │  (趋势市)    │
    ├──────────────┼──────────────┤
    │  低波 + 震荡  │  低波 + 趋势  │
    │  (垃圾时间)  │  (温和趋势)  │
    └──────────────┴──────────────┘
    
    简化输出为四个标签：
    - TRENDING_UP / TRENDING_DOWN：强趋势市
    - RANGING：震荡市
    - HIGH_VOL：高波动市（叠加在趋势或震荡之上）
    - LOW_VOL：低波动市（垃圾时间）
    """
    
    def classify(self, price_data_30d):
        """
        输入：过去 30 天的 OHLCV 数据
        输出：当前市场环境标签 + 每个策略的适宜度
        """
        volatility = self.calc_volatility(price_data_30d)
        trend_strength = self.calc_trend_strength(price_data_30d)
        
        # 波动率分档（基于历史百分位）
        vol_percentile = self.volatility_percentile(volatility)
        high_vol = vol_percentile > 75
        low_vol = vol_percentile < 25
        
        # 趋势强度分档
        strong_trend = trend_strength > 0.70
        weak_trend = trend_strength < 0.30
        
        if strong_trend and high_vol:
            regime = 'HIGH_VOL_TRENDING'
        elif strong_trend and not high_vol:
            regime = 'TRENDING'
        elif weak_trend and high_vol:
            regime = 'HIGH_VOL_CHOPPY'
        elif weak_trend and low_vol:
            regime = 'LOW_VOL_DEAD'
        else:
            regime = 'RANGING'
        
        return regime
```

## 第 2 步：核心指标计算

```python
def calc_trend_strength(price_data_30d, lookback=20):
    """
    趋势强度评分（0-1）
    
    构成：
    1. 均线斜率（40%）：20 日均线斜率 / 历史波动率 → 归一化
    2. 价格与均线关系（30%）：价格在均线上方的时间占比
    3. 动量持续性（30%）：正收益率的天数占比
    
    > 0.70 = 强趋势
    0.30-0.70 = 弱趋势/震荡
    < 0.30 = 纯震荡
    """
    
    # 1. 均线斜率
    ma20 = price_data_30d['close'].rolling(20).mean()
    slope = (ma20.iloc[-1] - ma20.iloc[-5]) / ma20.iloc[-5]
    hist_vol = price_data_30d['close'].pct_change().std()
    slope_normalized = min(abs(slope) / hist_vol, 1.0) if hist_vol > 0 else 0
    
    # 2. 价格与均线关系
    above_ma = (price_data_30d['close'] > ma20).sum() / len(price_data_30d)
    above_ma_score = abs(above_ma - 0.5) * 2  # 偏离 50% 越远 = 趋势越强
    
    # 3. 动量持续性
    returns = price_data_30d['close'].pct_change().dropna()
    positive_streak = (returns > 0).rolling(5).sum().max() / 5  # 最长连续正收益
    negative_streak = (returns < 0).rolling(5).sum().max() / 5
    persistence = max(positive_streak, negative_streak)
    
    # 加权
    score = slope_normalized * 0.4 + above_ma_score * 0.3 + persistence * 0.3
    return score


def calc_volatility(price_data_30d):
    """
    波动率评估
    
    返回：
    - current_vol：当前 7 日年化波动率
    - percentile：在历史 1 年中的百分位
    - label：normal / high / low
    """
    returns = price_data_30d['close'].pct_change().dropna()
    current_vol = returns.tail(7).std() * np.sqrt(365)
    return current_vol
```

## 第 3 步：策略适宜度矩阵

```python
# 策略适宜度矩阵（固定配置）
STRATEGY_REGIME_FIT = {
    # 趋势跟踪：在强趋势和高波动时最赚钱
    'trend_following': {
        'TRENDING':          1.0,   # 全仓
        'HIGH_VOL_TRENDING': 1.0,   # 全仓（止损放宽）
        'HIGH_VOL_CHOPPY':   0.5,   # 减半（高波但不一定有方向）
        'RANGING':           0.3,   # 减仓至 30%
        'LOW_VOL_DEAD':      0.0,   # 不交易
    },
    
    # 均值回归：在震荡市最赚钱，趋势市危险
    'mean_reversion': {
        'RANGING':           1.0,   # 全仓
        'LOW_VOL_DEAD':      0.7,   # 低波震荡可以跑
        'HIGH_VOL_CHOPPY':   0.5,   # 减半（波动大，回归不确定）
        'TRENDING':          0.2,   # 极低仓位（趋势市回归策略容易爆亏）
        'HIGH_VOL_TRENDING': 0.0,   # 不交易
    },
    
    # 动量策略：和趋势跟踪类似
    'momentum': {
        'TRENDING':          1.0,
        'HIGH_VOL_TRENDING': 0.8,
        'HIGH_VOL_CHOPPY':   0.4,
        'RANGING':           0.3,
        'LOW_VOL_DEAD':      0.0,
    },
    
    # 做市：低波+窄幅最适宜
    'market_making': {
        'RANGING':           1.0,
        'LOW_VOL_DEAD':      0.8,
        'TRENDING':          0.3,
        'HIGH_VOL_TRENDING': 0.0,
        'HIGH_VOL_CHOPPY':   0.0,
    },
}

def get_strategy_fitness(strategy_type, regime):
    """返回某个策略在当前环境的适宜度（0-1）→ 仓位倍数"""
    return STRATEGY_REGIME_FIT.get(strategy_type, {}).get(regime, 0.5)
```

## 第 4 步：每日输出

```python
def daily_regime_report(price_data_30d, active_strategies):
    """
    每日市场环境报告（嵌入日报顶部）
    """
    classifier = MarketRegimeClassifier()
    regime = classifier.classify(price_data_30d)
    
    print("═══════════════════════════════════")
    print("📊 今日市场环境")
    print(f"环境标签：{regime}")
    print(f"波动率：{classifier.current_vol:.1%}（{classifier.vol_percentile}分位）")
    print(f"趋势强度：{classifier.trend_strength:.2f}")
    print()
    print("策略适宜度：")
    for strategy in active_strategies:
        fitness = get_strategy_fitness(strategy.type, regime)
        bar = '█' * int(fitness * 10) + '░' * (10 - int(fitness * 10))
        status = '✅ 全仓' if fitness >= 0.8 else '⚠️ 减仓' if fitness >= 0.3 else '🔴 暂停'
        print(f"  {strategy.name:20s} [{bar}] {fitness:.0%} {status}")
    print("═══════════════════════════════════")
    
    return {
        'regime': regime,
        'strategy_fitness': {
            s.type: get_strategy_fitness(s.type, regime)
            for s in active_strategies
        }
    }
```

## 第 5 步：环境切换检测

```python
def detect_regime_change(regime_history_30d):
    """
    检测环境切换
    
    切换类型：
    - 渐进切换：连续 5 天分类不同，方向一致 → 可能是周期切换
    - 突然切换：单天从震荡翻到强趋势 → 可能是重大事件驱动
    - 持久不切换：同一环境 > 21 天 → 注意策略惯性（趋势策略可能过度自信）
    """
    
    current_regime = regime_history_30d[-1]
    streak = count_consecutive_same_regime(regime_history_30d)
    
    if streak > 21:
        return {
            'alert': 'regime_persistence',
            'message': f'同一环境已持续 {streak} 天，注意策略惯性，检查是否过度自信',
            'action': 'review_position_sizes'
        }
    
    # 最近 5 天的环境标签变化
    recent_change = check_regime_trend(regime_history_30d[-5:])
    if recent_change == 'shifting':
        return {
            'alert': 'regime_transition',
            'message': '最近 5 天环境在切换中，建议降仓观察',
            'action': 'reduce_all_positions_30pct'
        }
    
    return {'alert': None}
```

## 第 6 步：融入风控

```python
# RiskManager 中的环境适配逻辑
class RiskManager:
    def adjust_for_regime(self, regime_report):
        """
        根据市场环境自动收紧或放宽风控参数
        
        不适宜环境下的调整：
        ├── 趋势跟踪在震荡市 → 仓位上限 * 0.3
        ├── 均值回归在趋势市 → 仓位上限 * 0.2
        ├── 止损收紧：震荡市趋势策略的假突破多 → 止损设更紧
        └── 低波动市 → 所有策略仓位上限 * 0.5
        
        适宜环境下的调整：
        ├── 趋势市趋势策略 → 正常仓位
        ├── 可以适当放宽持仓时间
        └── 但永不放宽单笔止损上限！
        """
        for strategy in self.active_strategies:
            fitness = regime_report['strategy_fitness'][strategy.type]
            strategy.position_multiplier = fitness
            
            if fitness < 0.3:
                # 严重不适宜 → 只允许平仓，不开仓
                strategy.entry_allowed = False
            elif fitness < 0.6:
                # 不太适宜 → 减仓 + 收紧止损
                strategy.entry_allowed = True
                strategy.stop_loss_multiplier = 0.7  # 止损收紧到 70%
            else:
                strategy.entry_allowed = True
                strategy.stop_loss_multiplier = 1.0  # 正常止损
```

## 融入冲刺月

- **Week 2 Day 11**（多策略并行）：市场环境分类器上线，自动调仓
- **Week 3 日报**：每日报告顶部显示当前环境和各策略适宜度
- **Week 4 驾驶舱**：环境状态作为仪表盘常驻元素

## 必避的坑

| 坑 | 正确认知 |
|------|------|
| 试图预测环境切换 | 环境切换和价格一样不可预测。只判断现在是啥 |
| 在不适环境里调策略参数 | 不是策略不对，是环境不对。换环境不是换策略 |
| 环境分类太细太复杂 | 四象限足够。分类多于 6 种时你自己都不知道在干啥 |
| 不适环境超过 14 天还在硬扛 | 不是策略失效，但一直不适=你得暂停。保护资金比证明策略正确重要 |
