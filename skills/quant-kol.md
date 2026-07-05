---
name: quant-kol
description: KOL 与社交情绪聚合 — 监控指定 KOL 推特、关键词提及频率变化、情绪打分，结果纳入复盘归因和风控降权，不作为开仓信号。
---

# KOL 与社交情绪聚合

监控社交媒体上的加密话题，量化"市场在聊什么"和"谁在聊"。核心价值不是跟单，而是解释价格波动和提前感知情绪极端点。

## 硬性规则

1. **KOL 提及 ≠ 交易信号**：KOL 说一个币的时候，他可能已经建仓了。这条最多做辅助因子，不能做主信号。
2. **情绪极端才是信号**：日常讨论是噪声。极度恐惧和极度贪婪才有信息量。
3. **量化情绪，不量化观点**：不判断 KOL 说的是对是错，只统计提及频率、情绪极性和时间关系。
4. **结果只在复盘中出现**：融入日报/周报的归因分析，不直接触发交易。

## 第 1 步：KOL 列表管理

```python
class KOLTracker:
    """
    维护一个 KOL 监控列表
    
    选择标准：
    - 不要只看喊单人 → 也看分析师和链上数据博主
    - 同一赛道至少监控 2 个 → 交叉验证
    - 定期淘汰 → 连续 3 个月喊单准确率 < 随机水平就移除
    """
    
    def __init__(self):
        # KOL 分类
        self.kol_list = {
            'macro_analyst': [   # 宏观分析型（理解叙事用）
                # '@macro_analyst_1',
                # '@macro_analyst_2',
            ],
            'onchain_analyst': [  # 链上数据型（验证链上信号用）
                # '@lookonchain',
                # '@onchain_analyst_2',
            ],
            'trader_caller': [    # 喊单型（谨慎对待，用来反着看）
                # '@trader_1',
                # '@trader_2',
            ],
            'alpha_caller': [     # 早期发现型
                # '@alpha_1',
                # '@alpha_2',
            ],
        }
    
    def track_accuracy(self, kol_handle):
        """
        追踪 KOL 喊单准确率：
        - 每次提及一个币，记录时间和价格
        - 7 天后 / 30 天后回溯
        - 喊完涨了 = 正向，喊完跌了 = 反向
        - 连续 3 个月准确率 < 50% → 标记为"反指"，逆向参考
        """
        pass
```

## 第 2 步：数据采集

```python
class SocialDataCollector:
    """
    社交媒体数据采集
    
    采集源优先级：
    1. Twitter/X（加密讨论主战场）
    2. CryptoPanic 新闻聚合（免费 API）
    3. LunarCrush（社交情绪 API，部分免费）
    
    采集内容：
    - 指定 KOL 的推文（定时拉取）
    - 关键词搜索（币名 + $符号 + 合约地址）
    - 提及量时间序列（每分钟聚合）
    """
    
    def fetch_kol_tweets(self, kol_handle, since_timestamp):
        """
        拉取 KOL 推文
        使用 Twitter API v2 或第三方替代（Nitter RSS）
        """
        pass
    
    def search_keywords(self, keywords, hours=24):
        """
        搜索币种相关关键词
        返回：提及次数、时间分布、情绪极性（正/负/中性）
        """
        pass
    
    def get_social_volume(self, coin):
        """
        获取某个币的社交提及量
        对比过去 7 天均值 → 输出异常级别
        """
        pass
```

## 第 3 步：情绪分析

```python
class SentimentAnalyzer:
    """
    情绪分析
    
    策略：
    - 用 DeepSeek API 做情绪分类（便宜，¥1/百万 tokens）
    - 每条推文 → positive/negative/neutral + confidence
    - 聚合统计：某币在最近 N 小时的情绪倾向
    """
    
    def analyze_tweet(self, tweet_text, coin):
        """
        单条推文情绪分析
        
        返回：
        {
            'sentiment': 'positive' | 'negative' | 'neutral',
            'confidence': 0-1,
            'coin_mentioned': bool,
            'is_price_prediction': bool,  # 是否在预测价格（通常是噪声）
            'is_data_driven': bool,        # 是否有数据支撑（更有参考价值）
        }
        """
        prompt = f"""
        分析以下推文对 {coin} 的情绪：
        "{tweet_text}"
        
        返回 JSON：sentiment（positive/negative/neutral）、
        confidence（0-1）、
        is_price_prediction（是否在预测价格）、
        is_data_driven（是否有数据分析支撑）
        """
        # 调用 DeepSeek API（便宜）
        pass
    
    def aggregate_sentiment(self, coin, hours=24):
        """
        聚合最近 N 小时的情绪
        
        输出：
        {
            'bullish_ratio': 0.65,        # 看多占比
            'bearish_ratio': 0.15,        # 看空占比
            'neutral_ratio': 0.20,        # 中性占比
            'total_mentions': 234,        # 总提及数
            'sentiment_score': 0.50,      # 综合情绪 (-1 ~ +1)
            'mention_change_vs_7d': +340%,# 提及量变化
            'extreme_flag': False,        # 是否极端
        }
        """
        pass
```

## 第 4 步：异常检测

```python
def detect_social_anomalies(coin):
    """
    检测社交异常事件
    
    异常类型：
    1. 提及量暴增: 24h 提及量 > 7 日均值 * 3
    2. 情绪极端: 90%+ 的提及都是 positive 或 negative
    3. KOL 集中提及: 5+ 个监控 KOL 在 12h 内提及同一币种
    4. 价格背离: 社交热度暴增但价格没动（可能是 KOL 在铺货/吸筹）
    5. 情绪反转: 24h 内从极度 positive 翻到 negative（黑天鹅）
    """
    
    anomalies = []
    
    # 检查 1: 提及量暴增
    mentions_24h = get_mentions(coin, 24)
    mentions_7d_avg = get_avg_mentions(coin, 7)
    if mentions_24h > mentions_7d_avg * 3:
        anomalies.append({
            'type': 'mention_spike',
            'severity': 'medium',
            'detail': f'提及量暴增 {mentions_24h/mentions_7d_avg:.0f}x'
        })
    
    # 检查 3: KOL 集中提及
    kol_count = count_kol_mentions(coin, hours=12)
    if kol_count >= 5:
        anomalies.append({
            'type': 'kol_cluster',
            'severity': 'high',  # 可能是出货前奏
            'detail': f'{kol_count} 个 KOL 在 12h 内提及',
            'warning': '⚠️ KOL 集中喊单通常不是好事——他们在同步出货计划'
        })
    
    # 检查 4: 社交热度 vs 价格背离
    if mentions_24h > mentions_7d_avg * 5 and price_change(coin, 24) < 0.02:
        anomalies.append({
            'type': 'social_price_divergence',
            'severity': 'high',  # 可能是吸筹/出货
            'detail': '热度暴增 5x 但价格没动——有人在引导舆论',
        })
    
    return anomalies
```

## 第 5 步：KOL 提及时间线分析

```python
def build_kol_timeline(coin, event_time):
    """
    当某个币发生重大价格变动时，回溯 KOL 提及的时间线
    
    关键问题：
    - 价格先动还是 KOL 先喊？（KOL 先喊 = 可能是信号；价格先动 = KOL 在追）
    - 喊之前价格涨了多少？（已涨很多 → KOL 在帮忙找接盘侠）
    - 喊之后价格走势？（持续涨 = 可能有料；涨了就跌 = 标准出货）
    
    这个分析嵌入复盘报告的事件驱动层。
    """
    
    mentions_before = get_mentions_before(coin, event_time, hours=24)
    mentions_after = get_mentions_after(coin, event_time, hours=24)
    price_before_first_mention = get_price_before(coin, mentions_before[0].time)
    
    return {
        'first_mention_time': mentions_before[0].time if mentions_before else None,
        'price_before_first_mention': price_before_first_mention,
        'price_action_after': classify_price_action(mentions_after),
        'kol_lead_or_lag': 'lead' if mentions_before else 'lag',
    }
```

## 第 6 步：融入系统的信号输出

```
KOL 情绪分析 → 日报/周报/月报（嵌入三维复盘）
├── 因果叙事层：本周市场在讲什么故事？谁在讲？
├── 事件驱动层：每笔大盈亏对应的 KOL 提及时间线
└── 情绪极端标记：恐惧贪婪指数 + KOL 集中度

KOL 异常事件 → Telegram 提醒
├── 5+ KOL 同时喊单一个币 → ⚠️ 提醒 + "别急着跟"
├── 情绪极端逆转 → 📢 提醒 + "检查持仓风险"
└── 社交热度 vs 价格背离 → 🔍 提醒 + "可能是大户在布局"

KOL 准确率追踪 → 月报
├── 本月喊单准确率 Top 3 KOL
└── 本月喊单准确率 Bottom 3（标记为反指参考）
```

## 必避的坑

| 坑 | 现实 |
|------|------|
| KOL 喊了就跟 | KOL 的工作是建立影响力，不是帮你赚钱。他喊之前就建仓了 |
| 只关注喊单 KOL | 最好的 alpha 来源往往是数据型博主（Lookonchain 模式），不是喊单博主 |
| 情绪分析替代策略信号 | 情绪是滞后指标——极度贪婪时可能还继续涨，极度恐惧时可能还继续跌 |
| 用 LLM 直接预测价格 | LLM 的情绪分析在金融场景下的准确率很低，不要让它判断"该不该买" |
| 中英文 KOL 只盯一边 | 加密是全球化市场，重要信息可能先在英文/韩文/日文圈发酵 |
