---
name: quant-orderbook
description: 订单簿分析 — 深度快照采集、做市商行为识别（假单/冰山单/撤单模式）、买卖压力判断、异常信号标记。
---

# 订单簿信号分析

分析交易所订单簿（Order Book / Depth），识别做市商行为模式、买卖压力失衡和潜在操纵信号。

## 硬性规则

1. **不做高频**：本 skill 分析分钟级快照，不做 tick 级实时处理。那个是 HFT 的领域。
2. **不只盯一个价位**：必须看深度曲线整体形态，不能只看买一卖一。
3. **挂单行为比挂单量重要**：挂单的存活时间、撤单模式、补单频率比挂单量本身更有信息量。
4. **结果不直接交易**：订单簿信号纳入综合评分，作为风控因子，不单独触发开仓。

## 第 1 步：数据采集

```python
class OrderBookCollector:
    """
    订单簿快照采集
    
    采集策略：
    - 每分钟抓一次深度快照（前 20 档）
    - 极端行情自动提高频率到 10 秒一次
    - 实时监控只做异常检测，不存全量 tick
    """
    
    def collect_snapshot(self, symbol):
        """获取订单簿快照"""
        # 币安 depth API: /api/v3/depth?symbol=BTCUSDT&limit=20
        snapshot = {
            'timestamp': now_utc(),
            'symbol': symbol,
            'bids': [[price, amount], ...],  # 买盘前 20 档
            'asks': [[price, amount], ...],  # 卖盘前 20 档
        }
        return snapshot
    
    def save_snapshot(self, snapshot):
        """存储到本地数据库（每分钟一次）"""
        # 建议用 SQLite，一张表只存当天
        # 历史超过 7 天的存档到 Parquet
        pass
```

## 第 2 步：做市商行为信号

### 信号 1：假挂单（Spoofing）

```python
def detect_spoofing(snapshots_1h):
    """
    检测假挂单：
    - 某个价位出现巨量挂单（> 平均深度 5 倍）
    - 挂单存活时间 < 5 分钟
    - 撤单后同价位没有重新挂出
    → 大概率是假单，用来误导散户
    
    返回值：最近 1 小时的假挂单事件列表
    """
    events = []
    for snap in snapshots_1h:
        for side in ['bids', 'asks']:
            for level in snap[side]:
                if level['amount'] > avg_depth * 5:
                    lifetime = track_lifetime(level)
                    if lifetime < 5 * 60:  # 5 分钟
                        events.append({
                            'time': snap['timestamp'],
                            'side': side,
                            'price': level['price'],
                            'amount': level['amount'],
                            'lifetime_seconds': lifetime,
                            'type': 'potential_spoofing'
                        })
    return events
```

### 信号 2：冰山订单

```python
def detect_iceberg(snapshots_1h):
    """
    检测冰山订单：
    - 某个价位反复出现相同或相近大小的挂单
    - 每成交一部分就自动补上
    - 同一个价位挂单出现 >10 次/小时
    → 大概率是冰山订单（大单拆小单隐藏真实意图）
    
    含义：
    - 买单冰山 = 有人在大规模吸筹，不想让人知道
    - 卖单冰山 = 有人在大规模出货
    """
    pass
```

### 信号 3：深度不对称

```python
def analyze_depth_imbalance(snapshot):
    """
    计算买卖深度不平衡度
    
    买盘深度 / 卖盘深度 > 3:1 → 买单墙
    卖盘深度 / 买盘深度 > 3:1 → 卖单墙
    
    但！关键判断：
    - 买单墙 + 价格不涨 = 假支撑（有人挂假买单出货）
    - 卖单墙 + 价格不跌 = 假压力（有人压价吸筹）
    - 买单墙 + 价格涨 = 真支撑（真实买盘）
    - 卖单墙 + 价格跌 = 真压力（真实卖盘）
    
    所以深度不对称必须和价格行为一起看才有意义。
    """
    bid_depth = sum(bid[1] for bid in snapshot['bids'][:10])
    ask_depth = sum(ask[1] for ask in snapshot['asks'][:10])
    ratio = bid_depth / ask_depth if ask_depth > 0 else float('inf')
    return {
        'bid_ask_ratio': ratio,
        'bid_depth_usdt': bid_depth,
        'ask_depth_usdt': ask_depth,
    }
```

### 信号 4：做市商建仓/出货特征

```python
def detect_mm_accumulation(snapshots_24h, price_data_24h):
    """
    做市商建仓特征：
    1. 价格窄幅横盘（波动率 < 日均波动率的 50%）
    2. 买盘深度持续增加（做市商在下面接货）
    3. 卖盘深度没有明显变化
    4. 持续时间 > 6 小时
    
    做市商出货特征：
    1. 价格窄幅横盘
    2. 卖盘深度持续增加（做市商在上面出货）
    3. 买盘深度不变或下降
    4. 持续时间 > 6 小时
    """
    volatility = calc_volatility(price_data_24h)
    avg_volatility_30d = get_historical_avg_volatility(30)
    
    if volatility < avg_volatility_30d * 0.5:
        bid_trend = calc_depth_trend(snapshots_24h, 'bids')
        ask_trend = calc_depth_trend(snapshots_24h, 'asks')
        
        if bid_trend > 0 and ask_trend <= 0:
            return {'signal': 'MM_ACCUMULATING', 'confidence': 'medium'}
        elif ask_trend > 0 and bid_trend <= 0:
            return {'signal': 'MM_DISTRIBUTING', 'confidence': 'medium'}
    
    return {'signal': 'NO_SIGNAL'}
```

## 第 3 步：综合订单簿评分

```python
def score_orderbook(symbol):
    """
    订单簿综合评分（0-10）
    
    计算各信号加权：
    ├── 假挂单检测     权重 15%  → 分数越高 = 越诚实
    ├── 冰山订单       权重 20%  → 有冰山是加分（有人在真买/真卖）
    ├── 深度不对称     权重 15%  → 和价格行为联动判断
    ├── 做市商行为     权重 35%  → 最重要
    └── 价差/流动性    权重 15%  → 基本健康度
    
    输出：
    - 分数 8-10：订单簿健康，无异常 → 正常交易
    - 分数 5-7：有可疑信号 → 降仓位 30%，密切观察
    - 分数 0-4：严重异常 → 停止该币种交易，等信号恢复
    """
    pass
```

## 第 4 步：关键价位的订单簿异常

```python
def check_key_levels(snapshot, support_levels, resistance_levels):
    """
    在关键技术位（支撑/阻力）检查订单簿行为
    
    在支撑位附近：
    - 买单深度正常 → 支撑有效
    - 买单深度突然消失（撤单）→ 支撑即将被破
    - 买单被大量吃穿 → 有人在主动砸穿支撑
    
    在阻力位附近：
    - 卖单深度正常 → 阻力有效
    - 卖单深度消失 → 即将突破
    - 卖单被大量吃穿 → 有人在主动推高
    """
    pass
```

## 订单簿数据存储策略

```
数据量太大，不能全存原始的。
存储策略：
├── 实时：只保留最近 24 小时全量快照（内存 + SQLite）
├── 归档：7 天以前 → 存每小时均值 + 关键统计量（Parquet）
├── 异常事件：永久保存（标记为做市商异常事件，复盘用）
└── 长期趋势：每月存一个"深度画像"（bid/ask 曲线形状特征）
```

## 融入系统的方式

```
订单簿评分 → RiskManager ↓
├── 评分 8-10 → 正常
├── 评分 5-7 → auto_reduce_position(0.7)  # 自动降仓位至 70%
└── 评分 0-4 → auto_pause_symbol(symbol)  # 暂停该币种

异常事件 → 日报/周报 ↓
├── 假挂单事件 → "今日该币种检测到 N 次可疑假单"
├── 做市商建仓 → "该币种连续 6h 买盘深度增加，疑似做市商在接货"
└── 深度异常 → "阻力位 68,000 卖单墙已被吃掉 80%，关注突破"
```

## 必避的坑

| 坑 | 实际含义 | 防法 |
|------|------|------|
| 看到墙就以为有支撑 | 假单随时可以撤 | 必须看挂单存活时间 |
| 深度数据有 200ms 延迟 | 你看到的时候已经变了 | 只做分钟级判断，不做 tick 级 |
| 小币种深度更假 | 流动性低，一个人就能画墙 | 只对 Top 50 市值币种做深度分析 |
| 只有 bid/ask 没有 trade 数据 | 挂单量 ≠ 成交量 | 结合成交数据验证 |
