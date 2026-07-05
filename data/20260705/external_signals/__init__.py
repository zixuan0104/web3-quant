"""
外部信号模块 — CT-DDD harness 控制壳

职责：
  外部信号（KOL/链上/订单簿/交易所公告）属于灰箱/黑箱域，
  不可直接触发交易开仓。只做三件事：
    1. 调节仓位大小（风控降权）
    2. 解释复盘归因（三维复盘的事件驱动层）
    3. 发现潜在机会（提醒，需人工确认）

五路信号源：
  信号1 — 链上实时扫描（0-40分）
  信号2 — KOL 推特监控（0-25分）
  信号3 — 二线所上币监控（0-15分）
  信号4 — 币安 Alpha 监控（0-20分）
  信号5 — 社区情绪 + 叙事热度（0-10分）

只有策略信号（白箱域）能触发开仓。
"""

from .deepseek_client import DeepSeekClient
from .kol_monitor import KOLMonitor, KOLEntry, KOLTier, SocialAnomaly
from .whale_tracker import WhaleTracker, TransferDirection, SmartMoneyTracker, ContractEventMonitor
from .orderbook_monitor import OrderBookMonitor, MMSignal, DepthSnapshot
from .binance_alpha import (
    BinanceAlphaMonitor, ListingPhase, ListingEvent,
    SecondaryExchangeMonitor, Exchange
)
from .signal_fusion import SignalFusion, FusionScore, AlertLevel
from .fusion_report import FusionReport
from .hot_token_tracker import (
    HotTokenTracker, MMStance, MMProfile,
    EntryZone, ContractSignal, TradeDirection,
)
