"""
DeepSeek API 客户端 — 轻量级 LLM 调用

用途：KOL 推文情绪分析、新闻摘要、叙事分类
选择 DeepSeek 的原因：¥1/百万 tokens，便宜且中文能力强

安全设计：
  - API key 从环境变量 DEEPSEEK_API_KEY 读取
  - 请求超时 30s，失败不阻塞主流程
  - 返回结构化 JSON，不直接返回自然语言
"""

import os
import json
import time
import logging
from typing import Optional

logger = logging.getLogger(__name__)


class DeepSeekClient:
    """DeepSeek API 轻量客户端 — 只做分类和摘要，不做预测"""

    BASE_URL = "https://api.deepseek.com/v1/chat/completions"
    MODEL = "deepseek-chat"
    TIMEOUT = 30  # 秒
    MAX_RETRIES = 2

    def __init__(self, api_key: Optional[str] = None):
        self.api_key = api_key or os.environ.get("DEEPSEEK_API_KEY", "")
        self._call_count = 0
        self._error_count = 0
        self._total_latency_ms = 0

    # ═══════════════════════════════
    # 公开接口
    # ═══════════════════════════════

    def analyze_sentiment(self, tweet_text: str, coin: str) -> dict:
        """
        单条推文情绪分析

        返回:
          {
            'sentiment': 'positive' | 'negative' | 'neutral',
            'confidence': 0.0-1.0,
            'coin_mentioned': bool,
            'is_price_prediction': bool,   # 是否在预测价格（通常是噪声）
            'is_data_driven': bool,         # 是否有数据支撑
            'narrative_tags': [str],        # 叙事标签
          }
        """
        prompt = f"""分析以下推文对 {coin} 的情绪。返回严格 JSON，不要任何其他文字。

推文: "{tweet_text}"

JSON schema:
{{
  "sentiment": "positive" | "negative" | "neutral",
  "confidence": 0.0-1.0,
  "coin_mentioned": true/false,
  "is_price_prediction": true/false,
  "is_data_driven": true/false,
  "narrative_tags": ["标签1", "标签2"]
}}"""
        return self._call(prompt, expect_json=True)

    def classify_narrative(self, tweets_batch: list[str], coin: str) -> dict:
        """
        批量推文叙事分类 — 这个币在讲什么故事？

        返回:
          {
            'primary_narrative': str,       # 主导叙事
            'narrative_strength': 0.0-1.0,  # 叙事一致性（越高越统一）
            'narrative_evolution': str,     # 叙事演变方向
            'sentiment_score': -1.0-1.0,    # 综合情绪
            'extreme_flag': bool,           # 是否极端（>80%同向）
          }
        """
        combined = "\n---\n".join(tweets_batch[:20])  # 最多 20 条
        prompt = f"""分析以下关于 {coin} 的推文集合。返回严格 JSON。

推文集合:
{combined}

JSON schema:
{{
  "primary_narrative": "AI Agent" | "Meme" | "基础设施" | "游戏/元宇宙" | "DeFi" | "其他",
  "narrative_strength": 0.0-1.0,
  "narrative_evolution": "形成中" | "扩散中" | "顶峰" | "衰退中",
  "sentiment_score": -1.0到1.0,
  "extreme_flag": true/false
}}"""
        return self._call(prompt, expect_json=True)

    def classify_kol_quality(self, kol_handle: str, recent_tweets: list[str]) -> dict:
        """
        评估 KOL 内容质量 — 用于 KOL 分级

        返回:
          {
            'content_type': 'macro_analyst' | 'onchain_analyst' | 'trader_caller' | 'alpha_caller' | 'noise',
            'data_driven_ratio': 0.0-1.0,     # 有数据支撑的比例
            'prediction_ratio': 0.0-1.0,       # 预测价格的比例（越高越像喊单）
            'avg_sentiment_bias': -1.0-1.0,    # 情绪偏向（极端值 = 不客观）
            'quality_tier': 'S' | 'A' | 'B' | 'C',
          }
        """
        sample = "\n---\n".join(recent_tweets[:10])
        prompt = f"""分析 KOL @{kol_handle} 的推文质量。返回严格 JSON。

推文样本:
{sample}

JSON schema:
{{
  "content_type": "macro_analyst" | "onchain_analyst" | "trader_caller" | "alpha_caller" | "noise",
  "data_driven_ratio": 0.0-1.0,
  "prediction_ratio": 0.0-1.0,
  "avg_sentiment_bias": -1.0到1.0,
  "quality_tier": "S" | "A" | "B" | "C"
}}"""
        return self._call(prompt, expect_json=True)

    def detect_fake_pump_pattern(self, kol_mentions: list[dict]) -> dict:
        """
        检测是否疑似付费喊单/假 pump

        kol_mentions: [{'kol': str, 'tier': str, 'time': str, 'text': str}, ...]

        返回:
          {
            'is_suspicious': bool,
            'risk_score': 0-10,
            'reasons': [str],
          }
        """
        mentions_str = json.dumps(kol_mentions, ensure_ascii=False, indent=2)
        prompt = f"""分析以下 KOL 对同一币种的提及模式，判断是否疑似付费喊单。

KOL 提及记录:
{mentions_str}

判断标准:
- 多个平时准确率低的 C 级 KOL 同时喊同一个币 → 可疑
- 喊单时间高度集中（1h 内 5+ 个）→ 可疑
- 喊单内容高度相似（复制粘贴话术）→ 可疑
- KOL 集群包含 S 级开发者 → 降低可疑度

返回严格 JSON:
{{
  "is_suspicious": true/false,
  "risk_score": 0-10,
  "reasons": ["原因1", "原因2"]
}}"""
        return self._call(prompt, expect_json=True)

    # ═══════════════════════════════
    # 内部
    # ═══════════════════════════════

    def _call(self, prompt: str, expect_json: bool = True) -> dict:
        """
        调用 DeepSeek API

        失败时返回降级结果，不抛异常 — 外部信号不能阻塞主流程
        """
        if not self.api_key:
            return self._fallback_response()

        for attempt in range(self.MAX_RETRIES + 1):
            try:
                import urllib.request
                import urllib.error

                t0 = time.time()
                data = json.dumps({
                    "model": self.MODEL,
                    "messages": [
                        {"role": "system", "content": "你是一个加密货币情绪分析助手。只返回 JSON，不返回其他内容。"},
                        {"role": "user", "content": prompt},
                    ],
                    "temperature": 0.1,
                    "max_tokens": 500,
                    "response_format": {"type": "json_object"} if expect_json else None,
                }).encode("utf-8")

                req = urllib.request.Request(
                    self.BASE_URL,
                    data=data,
                    headers={
                        "Authorization": f"Bearer {self.api_key}",
                        "Content-Type": "application/json",
                    },
                )

                with urllib.request.urlopen(req, timeout=self.TIMEOUT) as resp:
                    elapsed_ms = (time.time() - t0) * 1000
                    self._call_count += 1
                    self._total_latency_ms += elapsed_ms

                    body = json.loads(resp.read().decode("utf-8"))
                    content = body["choices"][0]["message"]["content"]

                    if expect_json:
                        # 清理可能的 markdown 包裹
                        content = content.strip()
                        if content.startswith("```"):
                            content = content.split("\n", 1)[1]
                            if content.endswith("```"):
                                content = content[:-3]
                        return json.loads(content)
                    return {"content": content}

            except Exception as e:
                self._error_count += 1
                if attempt < self.MAX_RETRIES:
                    time.sleep(1 * (attempt + 1))
                    continue
                logger.warning(f"DeepSeek API 调用失败 (已重试{self.MAX_RETRIES}次): {e}")
                return self._fallback_response()

        return self._fallback_response()

    def _fallback_response(self) -> dict:
        """API 不可用时的降级响应 — 保守中性"""
        return {
            "sentiment": "neutral",
            "confidence": 0.3,
            "coin_mentioned": True,
            "is_price_prediction": False,
            "is_data_driven": False,
            "narrative_tags": [],
            "primary_narrative": "未知",
            "narrative_strength": 0.0,
            "narrative_evolution": "未知",
            "sentiment_score": 0.0,
            "extreme_flag": False,
            "is_suspicious": False,
            "risk_score": 5.0,
            "reasons": ["API 不可用，返回降级中性结果"],
            "content_type": "noise",
            "data_driven_ratio": 0.0,
            "prediction_ratio": 0.0,
            "avg_sentiment_bias": 0.0,
            "quality_tier": "C",
        }

    @property
    def stats(self) -> dict:
        return {
            "call_count": self._call_count,
            "error_count": self._error_count,
            "avg_latency_ms": self._total_latency_ms / max(self._call_count, 1),
            "api_configured": bool(self.api_key),
        }
