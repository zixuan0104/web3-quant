---
name: quant-onchain
description: 链上数据追踪 — 监控链上大额转账、聪明钱包地址、交易所余额、稳定币供应量、代币合约事件，生成异常信号并融入风控和复盘。
---

# 链上数据追踪

监控链上数据，识别鲸鱼行为、做市商活动和市场资金流向。链上数据不作为开仓信号，用于风控降权、复盘归因和机会发现。

## 硬性规则

1. **不作为开仓信号**：链上数据只用于三类场景——风控降权、复盘归因、机会发现。不直接触发交易。
2. **必须标注数据来源和时间戳**：每条链上信号记录来源（API/平台/地址）和区块确认时间。
3. **区分信号和噪声**：大额转账不等于鲸鱼在买卖。很多是交易所内部调仓。必须上下文判断。
4. **不追踪隐私链**：Monero/Zcash 等隐私币不在追踪范围。只监控公开可查的公链数据。

## 监控维度

### 第一层：大额转账监控

| 监控对象 | 阈值 | 数据源 |
|------|------|------|
| BTC 大额转账 | >500 BTC | Whale Alert API, Blockchain.com |
| ETH/USDT 大额转账 | >1M USDT | Etherscan API, Whale Alert |
| 交易所钱包互转 | 任意金额 | Arkham Intelligence 标签 |
| 未知钱包 → 交易所 | >100 BTC / >500K USDT | 同上 |

```python
class WhaleTracker:
    """
    鲸鱼转账监控
    
    核心逻辑：
    - 不是所有大额转账都是信号
    - 未知钱包 → 交易所 = 潜在卖压（币转到交易所通常是准备卖的）
    - 交易所 → 未知钱包 = 潜在积累（提币到冷钱包 = 长期持有）
    - 两个交易所之间互转 = 做市商调仓，忽略
    """
    
    def classify_transfer(self, tx):
        """判断转账方向和含义"""
        from_label = self.get_label(tx.from_address)  # 交易所/未知/鲸鱼/做市商
        to_label = self.get_label(tx.to_address)
        
        if from_label == 'unknown' and to_label == 'exchange':
            return {'signal': 'potential_sell_pressure', 'confidence': 'medium'}
        elif from_label == 'exchange' and to_label == 'unknown':
            return {'signal': 'potential_accumulation', 'confidence': 'medium'}
        elif from_label == 'exchange' and to_label == 'exchange':
            return {'signal': 'internal_transfer', 'confidence': 'ignore'}
        elif from_label == 'market_maker' and to_label == 'exchange':
            return {'signal': 'mm_deploying', 'confidence': 'high'}
```

### 第二层：聪明钱包追踪

| 钱包来源 | 怎么找 | 追踪什么 |
|------|------|------|
| Nansen Smart Money 标签 | Nansen 公开数据 / Dune 社区版 | 地址余额变化、最近交易 |
| Arkham 公开标签 | Arkham Intelligence | Wintermute/GSR/Jump 等做市商地址 |
| 历史盈利地址 | Dune 分析：过去 1 年胜率 > 60% 的地址 | 当前持仓、最近买卖 |
| 早期项目参与地址 | 在代币上线 24h 内买入且盈利的地址 | 是否在买新的东西 |

```python
class SmartMoneyTracker:
    def __init__(self):
        # 维护一个监控地址列表（初始 20-50 个地址）
        self.watchlist = self.load_watchlist()
    
    def scan_activity(self):
        """扫描监控地址的最近活动"""
        # 返回：哪些聪明钱包在买/卖什么
        # 多钱包同向操作（5+ 钱包同时买入同一代币）= 高置信度信号
        pass
    
    def add_to_watchlist(self, address, reason):
        """添加到监控列表，必须记录添加原因"""
        pass
```

### 第三层：交易所 BTC 余额

| 指标 | 数据源 | 信号含义 |
|------|------|------|
| BTC 交易所余额 | CryptoQuant, Glassnode | 持续下降 = 积累 = 看涨；持续上升 = 抛压 = 看跌 |
| 稳定币交易所余额 | CryptoQuant | 上升 = 增量资金准备入场 |
| ETH 质押量 | Beaconcha.in | 上升 = ETH 流通减少 = 潜在通缩 |

### 第四层：代币合约事件

```python
class TokenContractMonitor:
    """监控 ERC-20/BEP-20 合约的关键事件"""
    
    EVENTS_TO_TRACK = [
        'Mint',      # 增发 → 可能通胀
        'Burn',      # 销毁 → 可能通缩
        'Transfer',  # 大额转账 → 归入 WhaleTracker
        'OwnershipTransferred',  # 合约所有权转移 → 可能跑路前奏
        'Pause',     # 暂停交易 → rug pull 信号
    ]
```

## 信号聚合与评分

```python
def aggregate_onchain_signals(coin):
    """
    聚合所有链上信号，输出综合评分
    
    返回值：
    {
        'score': 0-10,          # 综合链上评分
        'whale_activity': {...}, # 鲸鱼活动详情
        'smart_money': {...},   # 聪明钱包动向
        'exchange_balance': {...}, # 交易所余额趋势
        'alerts': [...]         # 异常告警列表
    }
    """
    
    signals = {
        'whale_net_inflow': get_whale_net_inflow(coin),  # 鲸鱼净流入/流出
        'smart_money_buying': count_smart_money_buying(coin),  # 聪明钱买入数
        'exchange_btc_trend': get_exchange_btc_trend(),  # 交易所 BTC 余额 7 日趋势
        'stablecoin_supply_change': get_stablecoin_change(),  # 稳定币供应变化
    }
    
    # 权重配置
    weights = {
        'whale_net_inflow': 0.25,
        'smart_money_buying': 0.35,  # 最高权重
        'exchange_btc_trend': 0.25,
        'stablecoin_supply_change': 0.15,
    }
    
    score = sum(signals[k] * weights[k] for k in weights)
    return {'score': score, 'signals': signals}
```

## 融入风控和报告

| 信号 | 动作 | 去向 |
|------|------|------|
| 多个聪明钱包同时卖出 → | 该币种策略仓位降 50% | 风控（RiskManager） |
| 交易所 BTC 余额 7 日增幅 >5% → | 全市场谨慎，整体仓位降 30% | 风控 |
| 未知钱包大量 BTC 转入交易所 → | 标记关注，不自动操作 | 日报 + Telegram 提醒 |
| 代币合约增发事件 → | 标记，检查是否影响持仓代币 | 周报归因 |
| 稳定币供应量持续增长 → | 市场资金面健康，正常仓位 | 月报宏观分析 |

## 必避的坑

| 坑 | 说明 | 防法 |
|------|------|------|
| 交易所内部调仓误判为鲸鱼买卖 | 交易所 hot wallet → cold wallet 是正常操作 | 用 Arkham 标签区分交易所内部地址 |
| 假代币合约 | 同名代币遍地，监控了假合约 | 用 CoinGecko/CMC 验证合约地址 |
| 清洗交易 | DEX 上的量可能是刷的 | 交易量+独立地址数交叉验证 |
| 数据延迟 | 链上数据有区块确认延迟 | 记录数据时间戳，和价格数据对齐时标注延迟 |
| 过度解读 | "鲸鱼买了 = 一定涨" 是错的 | 只做风控降权，不做交易信号 |
