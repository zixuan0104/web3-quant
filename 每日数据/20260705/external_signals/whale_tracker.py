"""
鲸鱼与聪明钱追踪模块 — Day 20

监控维度：
  1. 大额转账方向分类（交易所流入/流出/内部调仓）
  2. 聪明钱地址追踪（历史胜率 > 60% 的钱包）
  3. 交易所 BTC/稳定币余额趋势
  4. 代币合约事件（增发/销毁/所有权转移）

硬性规则（来自 quant-onchain skill）：
  - 链上数据不作为开仓信号 — 只做风控降权/复盘归因/机会发现
  - 必须标注数据来源和时间戳
  - 区分信号和噪声 — 大额转账 ≠ 鲸鱼在买卖
"""

import json
import os
import time
from dataclasses import dataclass, field, asdict
from datetime import datetime, timedelta
from enum import Enum
from typing import Optional
from collections import defaultdict


# ═══════════════════════════════
# 数据类型
# ═══════════════════════════════

class TransferDirection(str, Enum):
    """转账方向"""
    TO_EXCHANGE = "to_exchange"            # 未知钱包 → 交易所 = 潜在卖压
    FROM_EXCHANGE = "from_exchange"        # 交易所 → 未知钱包 = 潜在积累
    EXCHANGE_TO_EXCHANGE = "exchange_to_exchange"  # 交易所之间 = 做市商调仓
    UNKNOWN_TO_UNKNOWN = "unknown_to_unknown"       # 都未知 = 待分类
    WHALE_TO_WHALE = "whale_to_whale"               # 鲸鱼之间 = OTC
    MM_DEPLOYING = "mm_deploying"                  # 做市商 → 交易所 = 准备做市


@dataclass
class WhaleTransfer:
    """鲸鱼转账记录"""
    tx_hash: str
    from_address: str
    to_address: str
    amount: float                        # 以 USDT 计（BTC 按市价折算）
    asset: str                           # BTC / ETH / USDT / SOL
    direction: TransferDirection
    from_label: str                      # exchange / unknown / whale / market_maker
    to_label: str
    timestamp: str                       # ISO 8601
    block_number: int
    source: str                          # Whale Alert / Etherscan / Solscan
    confidence: str                      # high / medium / low


@dataclass
class SmartWallet:
    """聪明钱钱包"""
    address: str
    label: str                           # 标签（如 "Nansen Smart Money #42"）
    chain: str                           # ethereum / solana / bsc
    historical_win_rate: float           # 历史胜率
    total_trades: int
    tracked_since: str
    last_activity: str = ""
    current_holdings: dict[str, float] = field(default_factory=dict)  # token → usd_value
    recent_buys: list[dict] = field(default_factory=list)             # 最近买入


@dataclass
class ExchangeBalance:
    """交易所余额快照"""
    exchange: str                        # binance / coinbase / all
    asset: str
    balance: float
    change_7d_pct: float                 # 7 日变化率
    change_30d_pct: float                # 30 日变化率
    timestamp: str


@dataclass
class ContractEvent:
    """代币合约事件"""
    token: str
    contract_address: str
    event_type: str                      # Mint / Burn / OwnershipTransferred / Pause
    amount: Optional[float] = None
    tx_hash: str = ""
    timestamp: str = ""
    severity: str = "low"               # critical / high / medium / low


# ═══════════════════════════════
# 已知地址标签 — 模拟 Arkham/Nansen
# ═══════════════════════════════

# 交易所热钱包（部分真实地址）
EXCHANGE_ADDRESSES = {
    # Binance
    "0x28C6c06298d514Db089934071355E5743bf21d60": "binance_hot",
    "0x21a31Ee1afC51d94C2eFcCAa2092aD1028285549": "binance_hot",
    "0xBE0eB53F46cd790Cd13851d5EFf43D12404d33E8": "binance_hot_3",
    # Coinbase
    "0x71660c4005BA85C37ccec55d0C4493E66Fe775d3": "coinbase_hot",
    # OKX
    "0x6cC5F688a315f3dC28A7781717a9A798a59fDA7b": "okx_hot",
}

# 已知做市商地址
MARKET_MAKER_ADDRESSES = {
    "0x0D0707963952f2fBA59dD06f2b425ace40b492Fe": "wintermute",
    "0x6C414Ae5e22F4b37e8c18dC7B1A515Fb15ECB0c4": "gsr",
    "0x0D3aB25DD9565Fed212Df745cD2d0D57baD68fc4": "jump_crypto",
}

# 聪明钱地址样例（Solana 生态 — 模拟数据）
DEFAULT_SMART_WALLETS = [
    {"address": "9xQeWvG816NCcxJ1Pn9wPEREYwMrJ4eNNbL3gUKqCFrt", "label": "GMGN Smart #1",
     "chain": "solana", "win_rate": 0.72, "total_trades": 156},
    {"address": "7VP9s5P19oEjm4M6TzL9B7EW1vDxEB8ERHA7NeSPmKq6", "label": "Ansem Follower",
     "chain": "solana", "win_rate": 0.68, "total_trades": 89},
    {"address": "3vuCfyzoc3LtoqgDAU7u2H1zEFYCEwWFhTjr3jM8qFYp", "label": "Degen Ape #12",
     "chain": "solana", "win_rate": 0.61, "total_trades": 234},
    {"address": "5HMVDynvZYjB2WQPEse1nNvNYPn88G6FJx6FzDk4cvm6", "label": "Pump Fun Early",
     "chain": "solana", "win_rate": 0.79, "total_trades": 67},
    {"address": "2k9MfKxGQN7KxNhV1QaBNBqL6Tn2YMfZn5cRdEjxHkL3", "label": "Raydium sniper",
     "chain": "solana", "win_rate": 0.65, "total_trades": 412},
]


# ═══════════════════════════════
# 鲸鱼追踪器
# ═══════════════════════════════

class WhaleTracker:
    """
    鲸鱼转账监控

    核心逻辑（来自 skill）：
      - 未知钱包 → 交易所 = 潜在卖压（准备卖的）
      - 交易所 → 未知钱包 = 潜在积累（提币到冷钱包 = 长期持有）
      - 两个交易所之间 = 做市商调仓 → 忽略
      - 做市商 → 交易所 = 准备在新的交易所做市
    """

    # 大额转账阈值
    THRESHOLDS = {
        "BTC": 100,        # >100 BTC
        "ETH": 1000,       # >1000 ETH
        "USDT": 1_000_000, # >$1M USDT
        "USDC": 1_000_000,
        "SOL": 10_000,     # >10,000 SOL
    }

    def __init__(self, data_dir: Optional[str] = None):
        self.transfers: list[WhaleTransfer] = []
        self.data_dir = data_dir or os.path.join(
            os.path.dirname(os.path.abspath(__file__)), "..", "data", "onchain"
        )

    def classify_address(self, address: str) -> str:
        """地址分类 — 查已知标签表"""
        if not address:
            return "unknown"
        addr_lower = address.lower()
        if addr_lower in EXCHANGE_ADDRESSES:
            return "exchange"
        if addr_lower in MARKET_MAKER_ADDRESSES:
            return "market_maker"
        # 简单启发式：交易所地址通常有高交易量（实际应用中查数据库）
        return "unknown"

    def classify_transfer(self, tx: dict) -> TransferDirection:
        """
        判断转账方向和含义

        tx: {'from': str, 'to': str, 'amount': float, 'asset': str, 'tx_hash': str}
        """
        from_label = self.classify_address(tx.get("from", ""))
        to_label = self.classify_address(tx.get("to", ""))

        if from_label == "market_maker" and to_label == "exchange":
            return TransferDirection.MM_DEPLOYING
        elif from_label == "unknown" and to_label == "exchange":
            return TransferDirection.TO_EXCHANGE
        elif from_label == "exchange" and to_label == "unknown":
            return TransferDirection.FROM_EXCHANGE
        elif from_label == "exchange" and to_label == "exchange":
            return TransferDirection.EXCHANGE_TO_EXCHANGE
        elif from_label == "unknown" and to_label == "unknown":
            # 检查是否是鲸鱼互转
            if tx.get("amount", 0) > self.THRESHOLDS.get(tx.get("asset", "BTC"), 100) * 2:
                return TransferDirection.WHALE_TO_WHALE
            return TransferDirection.UNKNOWN_TO_UNKNOWN
        return TransferDirection.UNKNOWN_TO_UNKNOWN

    def is_above_threshold(self, asset: str, amount: float) -> bool:
        """检查是否超过大额阈值"""
        threshold = self.THRESHOLDS.get(asset.upper(), 1_000_000)
        return amount >= threshold

    def record_transfer(self, tx: dict) -> Optional[WhaleTransfer]:
        """记录一笔大额转账"""
        asset = tx.get("asset", "BTC").upper()
        amount = float(tx.get("amount", 0))

        if not self.is_above_threshold(asset, amount):
            return None

        direction = self.classify_transfer(tx)
        from_label = self.classify_address(tx.get("from", ""))
        to_label = self.classify_address(tx.get("to", ""))

        # 交易所内部调仓直接忽略
        if direction == TransferDirection.EXCHANGE_TO_EXCHANGE:
            return None

        transfer = WhaleTransfer(
            tx_hash=tx.get("tx_hash", ""),
            from_address=tx.get("from", ""),
            to_address=tx.get("to", ""),
            amount=amount,
            asset=asset,
            direction=direction,
            from_label=from_label,
            to_label=to_label,
            timestamp=tx.get("timestamp", datetime.utcnow().isoformat()),
            block_number=int(tx.get("block_number", 0)),
            source=tx.get("source", "unknown"),
            confidence="high" if from_label != "unknown" and to_label != "unknown" else "medium",
        )

        self.transfers.append(transfer)
        return transfer

    def get_net_flow(self, asset: str = "BTC", hours: int = 24) -> dict:
        """
        计算交易所净流入/流出

        返回:
          {
            'inflow': float,            # 流入交易所总量
            'outflow': float,           # 流出交易所总量
            'net_flow': float,          # 净流入（+ = 卖压，- = 积累）
            'net_flow_signal': str,     # bullish / bearish / neutral
            'transfer_count': int,
          }
        """
        now = datetime.utcnow()
        cutoff = now - timedelta(hours=hours)

        inflow = 0.0
        outflow = 0.0
        count = 0

        for t in self.transfers:
            if t.asset != asset.upper():
                continue
            t_time = datetime.fromisoformat(t.timestamp)
            if t_time < cutoff:
                continue

            count += 1
            if t.direction == TransferDirection.TO_EXCHANGE:
                inflow += t.amount
            elif t.direction == TransferDirection.FROM_EXCHANGE:
                outflow += t.amount

        net = inflow - outflow

        # 信号判断
        if net > 0 and abs(net) > (inflow + outflow) * 0.3:
            signal = "bearish"   # 净流入 = 卖压
        elif net < 0 and abs(net) > (inflow + outflow) * 0.3:
            signal = "bullish"   # 净流出 = 积累
        else:
            signal = "neutral"

        return {
            "inflow": round(inflow, 2),
            "outflow": round(outflow, 2),
            "net_flow": round(net, 2),
            "net_flow_signal": signal,
            "transfer_count": count,
        }

    def get_recent_transfers(self, hours: int = 24, limit: int = 10) -> list[dict]:
        """获取最近的鲸鱼转账"""
        now = datetime.utcnow()
        cutoff = now - timedelta(hours=hours)
        recent = [
            t for t in self.transfers
            if datetime.fromisoformat(t.timestamp) >= cutoff
        ]
        recent.sort(key=lambda t: t.timestamp, reverse=True)
        return [asdict(t) for t in recent[:limit]]

    def get_exchange_balance_trend(self, asset: str = "BTC") -> dict:
        """
        交易所余额趋势分析 — 基于转账流水估算

        实际应用中应使用 CryptoQuant / Glassnode API
        这里用转账流水做近似估算
        """
        now = datetime.utcnow()

        # 7 天净流量
        flow_7d = self.get_net_flow(asset, hours=24 * 7)
        # 30 天净流量
        flow_30d = self.get_net_flow(asset, hours=24 * 30)

        return {
            "asset": asset,
            "net_flow_7d": flow_7d["net_flow"],
            "net_flow_signal_7d": flow_7d["net_flow_signal"],
            "net_flow_30d": flow_30d["net_flow"],
            "net_flow_signal_30d": flow_30d["net_flow_signal"],
            "trend": (
                "accumulation" if flow_30d["net_flow_signal"] == "bullish"
                else "distribution" if flow_30d["net_flow_signal"] == "bearish"
                else "neutral"
            ),
        }

    def get_whale_score(self, asset: str = "BTC") -> float:
        """
        鲸鱼信号评分 (0-10)

        用于信号融合 — 权重 25%
        """
        flow = self.get_net_flow(asset, hours=24)
        balance = self.get_exchange_balance_trend(asset)

        score = 5.0  # 中性基线

        # 净流出 = 积累 = 看涨
        if flow["net_flow_signal"] == "bullish":
            score += 3
        elif flow["net_flow_signal"] == "bearish":
            score -= 3

        # 趋势确认
        if balance["trend"] == "accumulation":
            score += 2
        elif balance["trend"] == "distribution":
            score -= 2

        return max(0, min(10, score))

    def get_stats(self) -> dict:
        return {
            "total_transfers": len(self.transfers),
            "unique_assets": len(set(t.asset for t in self.transfers)),
        }

    def save_state(self):
        os.makedirs(self.data_dir, exist_ok=True)
        filepath = os.path.join(self.data_dir, "transfers.json")
        with open(filepath, "w", encoding="utf-8") as f:
            json.dump([asdict(t) for t in self.transfers], f, ensure_ascii=False, indent=2)


# ═══════════════════════════════
# 聪明钱追踪器
# ═══════════════════════════════

class SmartMoneyTracker:
    """
    聪明钱地址追踪

    追踪历史胜率 > 60% 的钱包地址
    多钱包同向操作（5+ 钱包同时买入同一代币）= 高置信度信号
    """

    def __init__(self):
        self.wallets: dict[str, SmartWallet] = {}
        self._load_defaults()

    def _load_defaults(self):
        """加载默认聪明钱地址"""
        for w in DEFAULT_SMART_WALLETS:
            wallet = SmartWallet(
                address=w["address"],
                label=w["label"],
                chain=w["chain"],
                historical_win_rate=w["win_rate"],
                total_trades=w["total_trades"],
                tracked_since=datetime.utcnow().isoformat(),
            )
            self.wallets[w["address"]] = wallet

    def add_wallet(self, address: str, label: str, chain: str,
                   win_rate: float, total_trades: int, reason: str = ""):
        """添加监控地址 — 必须记录添加原因"""
        wallet = SmartWallet(
            address=address,
            label=label,
            chain=chain,
            historical_win_rate=win_rate,
            total_trades=total_trades,
            tracked_since=datetime.utcnow().isoformat(),
            notes=reason,
        )
        self.wallets[address] = wallet

    def record_activity(self, address: str, action: str, token: str,
                         amount_usd: float, timestamp: Optional[str] = None):
        """记录钱包活动"""
        if address not in self.wallets:
            return

        wallet = self.wallets[address]
        wallet.last_activity = timestamp or datetime.utcnow().isoformat()

        if action == "buy":
            wallet.recent_buys.append({
                "token": token,
                "amount_usd": amount_usd,
                "timestamp": wallet.last_activity,
            })
            # 只保留最近 20 笔
            if len(wallet.recent_buys) > 20:
                wallet.recent_buys = wallet.recent_buys[-20:]

    def scan_buying_activity(self, token: str, hours: int = 24) -> dict:
        """
        扫描聪明钱对特定代币的买入活动

        返回:
          {
            'token': str,
            'buying_wallets': int,          # 近期买入的钱包数
            'total_buy_volume_usd': float,   # 总买入金额
            'high_win_rate_wallets': int,     # 高胜率钱包(>70%)买入数
            'signal_strength': str,          # strong / moderate / weak / none
            'details': [dict],
          }
        """
        now = datetime.utcnow()
        cutoff = now - timedelta(hours=hours)

        buying_wallets = []
        total_volume = 0.0
        high_win_count = 0

        for wallet in self.wallets.values():
            recent_buys_this_token = [
                b for b in wallet.recent_buys
                if b["token"].upper() == token.upper()
                and datetime.fromisoformat(b["timestamp"]) >= cutoff
            ]
            if recent_buys_this_token:
                vol = sum(b["amount_usd"] for b in recent_buys_this_token)
                buying_wallets.append({
                    "address": wallet.address[:8] + "...",
                    "label": wallet.label,
                    "win_rate": wallet.historical_win_rate,
                    "buy_volume_usd": vol,
                })
                total_volume += vol
                if wallet.historical_win_rate > 0.70:
                    high_win_count += 1

        # 信号强度判断
        if len(buying_wallets) >= 5 and high_win_count >= 3:
            strength = "strong"
        elif len(buying_wallets) >= 3:
            strength = "moderate"
        elif len(buying_wallets) >= 1:
            strength = "weak"
        else:
            strength = "none"

        return {
            "token": token,
            "buying_wallets": len(buying_wallets),
            "total_buy_volume_usd": round(total_volume, 2),
            "high_win_rate_wallets": high_win_count,
            "signal_strength": strength,
            "details": buying_wallets,
        }

    def get_smart_money_score(self, token: str) -> float:
        """
        聪明钱评分 (0-10)

        用于信号融合 — 权重 35%（链上最高权重）
        """
        activity = self.scan_buying_activity(token, hours=24)
        strength_scores = {"strong": 9, "moderate": 6, "weak": 3, "none": 0}
        base = strength_scores.get(activity["signal_strength"], 0)
        # 高胜率钱包加成
        bonus = min(activity["high_win_rate_wallets"] * 1.5, 4)
        return min(10, base + bonus)


# ═══════════════════════════════
# 代币合约事件监控
# ═══════════════════════════════

class ContractEventMonitor:
    """代币合约关键事件监控"""

    EVENTS = ["Mint", "Burn", "OwnershipTransferred", "Pause", "ProxyUpgraded"]

    def __init__(self):
        self.events: list[ContractEvent] = []

    def record_event(self, token: str, contract: str, event_type: str,
                      amount: Optional[float] = None, tx_hash: str = "") -> Optional[ContractEvent]:
        """记录合约事件"""
        if event_type not in self.EVENTS:
            return None

        severity_map = {
            "OwnershipTransferred": "critical",  # 可能跑路前奏
            "Pause": "critical",                 # rug pull 信号
            "Mint": "high",                      # 通胀
            "ProxyUpgraded": "medium",           # 合约升级
            "Burn": "low",                       # 通常是好事
        }

        event = ContractEvent(
            token=token,
            contract_address=contract,
            event_type=event_type,
            amount=amount,
            tx_hash=tx_hash,
            timestamp=datetime.utcnow().isoformat(),
            severity=severity_map.get(event_type, "medium"),
        )
        self.events.append(event)
        return event

    def get_recent_critical_events(self, tokens: list[str], hours: int = 24) -> list[dict]:
        """获取指定代币最近的关键事件"""
        now = datetime.utcnow()
        cutoff = now - timedelta(hours=hours)
        critical = [
            e for e in self.events
            if e.token.upper() in [t.upper() for t in tokens]
            and datetime.fromisoformat(e.timestamp) >= cutoff
            and e.severity in ("critical", "high")
        ]
        return [asdict(e) for e in critical]

    def get_rug_risk_score(self, token: str, contract: str) -> float:
        """
        防 rug 风险评分 (0-10, 越高越危险)

        检测项：
          - 合约是否开源（需要实际验证 — 这里用事件反推）
          - 是否有 OwnershipTransferred 事件
          - 是否有 Pause 事件
          - 是否有异常 Mint 事件
        """
        score = 0.0
        for e in self.events:
            if e.contract_address.lower() != contract.lower():
                continue
            if e.event_type == "OwnershipTransferred":
                score += 4
            elif e.event_type == "Pause":
                score += 5
            elif e.event_type == "Mint" and e.amount and e.amount > 0:
                score += 2
        return min(10, score)
