"""
DeepSeek 情绪分析器 — KOL 推文 / 市场新闻 结构化情绪提取

不预测价格，只做三件事:
  1. 情绪打分: -1 (极度看空) ~ +1 (极度看多)
  2. 叙事分类: 利好 / 利空 / 中性 / 喊单嫌疑 / FUD
  3. 可信度评估: 0-1 (基于历史准确率 + 信号一致性)

对接:
  - Day 19 kol_monitor.py: 推文 → 情绪分析 → 融合评分
  - Day 21 signal_fusion.py: 情绪信号 → ExternalNarrative
  - Week 4 Dashboard: 情绪时间线 → 信号 Tab

用法:
  from external_signals.sentiment_analyzer import SentimentAnalyzer
  sa = SentimentAnalyzer()
  result = sa.analyze_tweet(tweet_text, author='@crypto_kol')
  # result: {'sentiment': 0.7, 'category': 'bullish', 'confidence': 0.8, ...}
"""

import os, sys, json, time, logging
from pathlib import Path
from typing import Optional, Dict, List
from dataclasses import dataclass, field, asdict
from enum import Enum

if sys.platform == 'win32':
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

logger = logging.getLogger(__name__)


# ═══════════════════════════════
# 数据类型
# ═══════════════════════════════

class SentimentCategory(Enum):
    BULLISH = "bullish"          # 看多
    BEARISH = "bearish"          # 看空
    NEUTRAL = "neutral"          # 中性 / 信息分享
    SHILLING = "shilling"        # 喊单嫌疑 (过度吹嘘、无实质内容)
    FUD = "fud"                  # 散布恐慌
    NEWS_BULLISH = "news_bullish"    # 利好新闻
    NEWS_BEARISH = "news_bearish"    # 利空新闻


@dataclass
class SentimentResult:
    """单条推文的情绪分析结果"""
    # 核心
    sentiment_score: float          # -1 ~ 1
    category: str                   # SentimentCategory.value
    confidence: float               # 0-1, 分析可信度

    # 详情
    mentioned_assets: List[str] = field(default_factory=list)    # 提到的币种
    key_reasons: List[str] = field(default_factory=list)         # 看多/看空的理由
    is_price_prediction: bool = False   # 是否含具体价格预测
    predicted_price: Optional[float] = None

    # 元信息
    analysis_timestamp: str = ""
    model_used: str = "deepseek-v4-pro"
    raw_response: str = ""

    # 历史统计 (后续填充)
    author_historical_accuracy: Optional[float] = None  # 该作者历史喊单准确率
    signal_consensus: Optional[float] = None             # 多个 KOL 的信号一致性


@dataclass
class BatchSentimentResult:
    """批量推文的情绪聚合"""
    tweets_analyzed: int
    overall_sentiment: float            # 加权平均
    bullish_count: int
    bearish_count: int
    shilling_alert: bool                # 是否检测到集中喊单
    shilling_coins: List[str] = field(default_factory=list)
    narrative_summary: str = ""         # LLM 生成的一句话叙事
    individual_results: List[Dict] = field(default_factory=list)


# ═══════════════════════════════
# 情绪分析器
# ═══════════════════════════════

class SentimentAnalyzer:
    """
    DeepSeek 驱动的 KOL 推文情绪分析器

    不做预测，只做结构化提取。
    """

    # DeepSeek 调用参数
    MODEL = "deepseek-chat"
    TEMPERATURE = 0.1  # 低温度 = 更一致
    MAX_TOKENS = 300

    def __init__(self, api_key: Optional[str] = None):
        # 尝试从多个来源加载 API key
        if api_key:
            self.api_key = api_key
        else:
            # 1. 环境变量
            self.api_key = os.environ.get('DEEPSEEK_API_KEY', '')
            # 2. .env 文件 (如果还没被 load)
            if not self.api_key:
                env_path = Path(__file__).resolve().parent.parent.parent / '.env'
                if env_path.exists():
                    with open(env_path, 'r', encoding='utf-8') as f:
                        for line in f:
                            line = line.strip()
                            if line.startswith('DEEPSEEK_API_KEY='):
                                self.api_key = line.split('=', 1)[1].strip().strip('"').strip("'")
                                break
        self._available = bool(self.api_key)

    @property
    def available(self) -> bool:
        return self._available

    # ═══════════════════════════════
    # 单条推文分析
    # ═══════════════════════════════

    def analyze_tweet(self, text: str, author: str = "",
                      mentioned_coins: List[str] = None) -> SentimentResult:
        """
        分析单条 KOL 推文

        参数:
          text: 推文文本
          author: KOL 用户名
          mentioned_coins: 预先提取的币种提及 (可选，辅助 LLM)

        返回: SentimentResult
        """
        if not self.available:
            return self._fallback_analysis(text, author, mentioned_coins)

        # 构造 prompt
        prompt = self._build_tweet_prompt(text, author, mentioned_coins)

        try:
            raw = self._call_deepseek(prompt)
            return self._parse_tweet_response(raw, text, author)
        except Exception as e:
            logger.warning(f"情绪分析失败 ({author}): {e}")
            return self._fallback_analysis(text, author, mentioned_coins)

    def _build_tweet_prompt(self, text: str, author: str,
                            mentioned_coins: List[str] = None) -> str:
        coins_hint = ""
        if mentioned_coins:
            coins_hint = f"\n已知提到的币种: {', '.join(mentioned_coins)}"

        return f"""分析以下加密货币 KOL 的推文。只返回 JSON，不要解释。

推文作者: {author}
推文内容: {text}{coins_hint}

返回严格 JSON 格式 (不要 markdown 代码块):
{{
  "sentiment_score": <float, -1到1, -1=极度看空, 0=中性, 1=极度看多>,
  "category": "<bullish|bearish|neutral|shilling|fud|news_bullish|news_bearish>",
  "confidence": <float, 0到1, 你对这个判断有多确定>,
  "mentioned_assets": ["<币种代号>"],
  "key_reasons": ["<一句话理由>"],
  "is_price_prediction": <bool>,
  "predicted_price": <float or null>
}}

分类标准:
- shilling: 过度吹嘘、无实质内容、'to the moon'/'100x' 等语言
- fud: 散布恐慌、无数据支撑的极端负面
- news_bullish/news_bearish: 引用具体新闻事件
- bullish/bearish: 有逻辑支撑的方向判断
- neutral: 纯信息分享或无法判断

短文本或信息不足 → confidence < 0.5, category = "neutral" """

    # ═══════════════════════════════
    # 批量推文聚合
    # ═══════════════════════════════

    def analyze_batch(self, tweets: List[Dict]) -> BatchSentimentResult:
        """
        批量分析推文并聚合

        tweets: [{'text': str, 'author': str, 'mentioned_coins': [str]}, ...]

        返回: BatchSentimentResult (含集中喊单检测)
        """
        results = []
        for t in tweets:
            r = self.analyze_tweet(
                t.get('text', ''),
                t.get('author', ''),
                t.get('mentioned_coins', []),
            )
            results.append(asdict(r))

        # 聚合统计
        scores = [r.sentiment_score for r in [
            SentimentResult(**d) for d in results
        ]]
        bullish = sum(1 for s in scores if s > 0.3)
        bearish = sum(1 for s in scores if s < -0.3)

        overall = sum(scores) / len(scores) if scores else 0

        # 集中喊单检测: 同一币种被 3+ 个 KOL 同时提及 = 喊单嫌疑
        coin_mentions = {}
        for r in results:
            for coin in r.get('mentioned_assets', []):
                coin_mentions[coin] = coin_mentions.get(coin, 0) + 1
        shilling_coins = [c for c, n in coin_mentions.items() if n >= 3]
        shilling_alert = len(shilling_coins) > 0

        # 叙事摘要 (取最高置信度的几条)
        narrative_parts = []
        if shilling_alert:
            narrative_parts.append(f"集中喊单: {', '.join(shilling_coins)}")
        if bullish > bearish:
            narrative_parts.append(f"整体偏多 ({bullish}/{len(results)} 条看多)")
        elif bearish > bullish:
            narrative_parts.append(f"整体偏空 ({bearish}/{len(results)} 条看空)")
        else:
            narrative_parts.append("多空分歧")

        return BatchSentimentResult(
            tweets_analyzed=len(results),
            overall_sentiment=round(overall, 3),
            bullish_count=bullish,
            bearish_count=bearish,
            shilling_alert=shilling_alert,
            shilling_coins=shilling_coins,
            narrative_summary='; '.join(narrative_parts),
            individual_results=results,
        )

    # ═══════════════════════════════
    # 内部方法
    # ═══════════════════════════════

    def _call_deepseek(self, prompt: str) -> str:
        """调用 DeepSeek API"""
        import urllib.request, urllib.error

        data = json.dumps({
            'model': self.MODEL,
            'messages': [{'role': 'user', 'content': prompt}],
            'temperature': self.TEMPERATURE,
            'max_tokens': self.MAX_TOKENS,
        }).encode('utf-8')

        for attempt in range(2):
            try:
                req = urllib.request.Request(
                    'https://api.deepseek.com/v1/chat/completions',
                    data=data,
                    headers={
                        'Content-Type': 'application/json',
                        'Authorization': f'Bearer {self.api_key}',
                    },
                )
                with urllib.request.urlopen(req, timeout=30) as resp:
                    result = json.loads(resp.read().decode())
                    return result['choices'][0]['message']['content']
            except Exception as e:
                if attempt == 1:
                    raise
                time.sleep(1)
        return "{}"

    def _parse_tweet_response(self, raw: str, text: str,
                              author: str) -> SentimentResult:
        """解析 DeepSeek 返回的 JSON"""
        try:
            # 清理可能的 markdown 代码块
            raw = raw.strip()
            if raw.startswith('```'):
                raw = raw.split('\n', 1)[1]
                if raw.endswith('```'):
                    raw = raw[:-3]

            data = json.loads(raw)

            return SentimentResult(
                sentiment_score=float(data.get('sentiment_score', 0)),
                category=data.get('category', 'neutral'),
                confidence=float(data.get('confidence', 0.5)),
                mentioned_assets=data.get('mentioned_assets', []),
                key_reasons=data.get('key_reasons', []),
                is_price_prediction=data.get('is_price_prediction', False),
                predicted_price=data.get('predicted_price'),
                analysis_timestamp=time.strftime('%Y-%m-%dT%H:%M:%SZ'),
                model_used=self.MODEL,
                raw_response=raw,
            )
        except (json.JSONDecodeError, KeyError, ValueError) as e:
            logger.warning(f"DeepSeek 返回解析失败: {e}")
            return self._fallback_analysis(text, author)

    def _fallback_analysis(self, text: str, author: str = "",
                           mentioned_coins: List[str] = None) -> SentimentResult:
        """API 不可用时的降级分析 — 简单关键词匹配"""
        text_lower = text.lower()

        # 简单关键词
        bullish_words = ['long', 'buy', 'bullish', 'moon', 'pump', 'breakout',
                         'accumulation', 'undervalued', '看涨', '做多', '买入',
                         '突破', '上涨', '新高', '目标', '利好', '即将上线',
                         'all in', '100x', 'bottom']
        bearish_words = ['short', 'sell', 'bearish', 'dump', 'crash', 'correction',
                         'overvalued', '看跌', '做空', '卖出',
                         '跌破', '下跌', '新低', '失效', '崩盘', '利空']

        bullish_count = sum(1 for w in bullish_words if w in text_lower)
        bearish_count = sum(1 for w in bearish_words if w in text_lower)

        if bullish_count > bearish_count:
            score = min(0.5, bullish_count * 0.15)
            category = 'bullish'
        elif bearish_count > bullish_count:
            score = max(-0.5, -bearish_count * 0.15)
            category = 'bearish'
        else:
            score = 0
            category = 'neutral'

        return SentimentResult(
            sentiment_score=round(score, 2),
            category=category,
            confidence=0.3,  # 降级分析 low confidence
            mentioned_assets=mentioned_coins or [],
            key_reasons=['降级关键词分析'],
            analysis_timestamp=time.strftime('%Y-%m-%dT%H:%M:%SZ'),
            model_used='fallback_keyword',
        )


# ═══════════════════════════════
# 自测
# ═══════════════════════════════

if __name__ == '__main__':
    print("=" * 60)
    print("  DeepSeek 情绪分析器 — 自测")
    print("=" * 60)

    sa = SentimentAnalyzer()
    print(f"  API: {'已配置' if sa.available else '未配置 (使用降级分析)'}")

    test_tweets = [
        ("BTC 刚刚突破了 65000 的关键阻力位，下一目标 70000。链上数据显示交易所余额持续下降。",
         "@crypto_analyst"),
        ("这个新币是下一个 100x!!! 团队匿名，但社区很强!!! 我已经 all in 了!!! #memecoin",
         "@moon_shiller"),
        ("ETH/BTC 汇率创年内新低，L2 叙事似乎已经完全失效了，不知道还有什么能支撑 ETH。",
         "@eth_bear"),
        ("Uniswap v4 即将上线，新特性包括 hooks 和 singleton 合约，Gas 成本预计降低 90%",
         "@defi_news"),
    ]

    for text, author in test_tweets:
        result = sa.analyze_tweet(text, author=author)
        print(f"\n  {author}")
        print(f"    文本: {text[:80]}...")
        print(f"    情绪: {result.sentiment_score:+.2f} ({result.category})")
        print(f"    置信度: {result.confidence:.0%}")
        print(f"    币种: {result.mentioned_assets}")
        print(f"    理由: {result.key_reasons}")

    # 批量测试
    print(f"\n[批量聚合测试]")
    batch = sa.analyze_batch([
        {'text': t, 'author': a} for t, a in test_tweets
    ])
    print(f"  分析 {batch.tweets_analyzed} 条")
    print(f"  整体情绪: {batch.overall_sentiment:+.2f}")
    print(f"  看多: {batch.bullish_count}  看空: {batch.bearish_count}")
    print(f"  喊单警报: {batch.shilling_alert}")
    print(f"  叙事: {batch.narrative_summary}")

    print("\nDONE")
