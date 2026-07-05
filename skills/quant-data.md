---
name: quant-data
description: 数据管线助手 — 生成完整的交易所数据拉取、清洗、存储脚本，支持多种数据源交叉验证。
---

# 数据管线助手

用户需要获取加密货币市场数据时，你必须生成包含以下所有环节的完整脚本。不允许跳过清洗和验证步骤。

## 硬性规则

1. **至少 2 个数据源**：必须同时拉取币安 + OKX，做交叉验证。单源 → 拒绝生成。
2. **清洗必有**：缺失值处理、时间戳对齐、异常值标记，三个步骤缺一不可。
3. **数据新鲜度检查**：必须包含数据超时检测逻辑（WebSocket 心跳 / REST 时间戳偏差）。
4. **增量更新**：脚本必须支持增量模式，不重复拉取已有数据。

## 数据源优先级

```
Layer 1（价格 — 策略主信号）
├── 币安 REST + WebSocket  ⭐ 主源
├── OKX REST + WebSocket    ⭐ 交叉验证
└── CoinGecko API           ⭐ 全市场基准价

Layer 2（衍生品 — 情绪+拥挤度）
├── 合约持仓量 OI（币安/OKX 合约 API）
├── 资金费率（同上）
└── 多空比（同上）

Layer 3（宏观 — 市场环境）
├── BTC 主导地位 BTC.D（CoinGecko）
└── 恐惧贪婪指数（alternative.me）

Layer 4（链上+社交 — 辅助归因）
├── 链上大额转账（Whale Alert / Etherscan）
└── DEX 新池子（DEX Screener）
```

## 第 1 步：确认需求

生成代码前向用户确认：
- 目标币种列表（如 BTC, ETH, SOL）
- K 线周期（如 1m, 5m, 15m, 1h, 4h, 1d）
- 历史数据跨度（默认 2 年）
- 需要哪些 Layer 的数据（默认至少 Layer 1）

## 第 2 步：目录结构

生成的脚本必须遵循以下目录约定：

```
data/
├── raw/                  # 原始数据（不修改）
│   ├── binance/BTCUSDT_1h.csv
│   ├── okx/BTCUSDT_1h.csv
│   └── ...
├── clean/                # 清洗后数据
│   └── BTCUSDT_1h.parquet
├── derived/              # 派生指标
│   └── BTCUSDT_1h_features.parquet
├── meta/                 # 元数据
│   └── data_catalog.json # 记录每个文件的时间范围、行数、最后更新时间
└── logs/                 # 数据管线日志
    └── pipeline_2025-07-05.log
```

## 第 3 步：生成 `fetch_data.py`

```python
"""
数据拉取脚本
使用 ccxt 统一接口拉取币安和 OKX 数据
"""
import ccxt
import pandas as pd
from datetime import datetime, timedelta
import time
import os

class DataFetcher:
    def __init__(self):
        # 初始化交易所连接
        # 注意：配置 API 超时和重试
        self.exchanges = {
            'binance': ccxt.binance({
                'enableRateLimit': True,
                'timeout': 30000,        # 30 秒超时
                'rateLimit': 1200,       # 币安限频 1200ms
            }),
            'okx': ccxt.okx({
                'enableRateLimit': True,
                'timeout': 30000,
            })
        }
    
    def fetch_ohlcv(self, exchange_name, symbol, timeframe, since, limit=1000):
        """
        拉取 OHLCV 数据，含完整错误处理
        
        参数:
            exchange_name: 'binance' | 'okx'
            symbol: 交易对如 'BTC/USDT'
            timeframe: K线周期如 '1h'
            since: 起始时间戳(ms)
            limit: 单次最大条数（默认 1000）
        """
        pass  # 实现
    
    def fetch_all(self, symbols, timeframes, start_date):
        """
        批量拉取所有币种和周期的数据
        含重试逻辑（3 次，指数退避）
        含进度条显示
        """
        pass  # 实现
```

## 第 4 步：生成 `clean_data.py`

清洗规则（不可跳过）：

```python
"""
数据清洗脚本
"""
class DataCleaner:
    def clean(self, df, symbol, timeframe):
        """
        清洗流程：
        1. 去重：按时间戳去重，保留第一条
        2. 时间戳对齐：统一为 UTC，对齐到 K 线边界
           - 1h K线的 12:00 必须是整点，不是 12:00:05
        3. 缺失检测：
           - 预期行数 vs 实际行数
           - 标记缺失的 K 线（用 NaN 填充或标记为 missing）
           - 缺失比例 > 5% → 警告，可能交易所宕机
        4. 异常值标记：
           - 价格偏差：当前 K 线的 OHLC 偏离前一根 K 线 > 20% → 标记
           - 成交量异常：当前量 > 均值 10 倍 → 标记
           - 标记的数据不删除，留到策略层面判断
        5. 交易所交叉验证：
           - 合并币安和 OKX 的收盘价
           - 价差 > 0.5% → 标记为"交易所价格分歧"
           - 取均价作为"统一价格"，同时保留各交易所原始值
        6. 输出：clean/<symbol>_<timeframe>.parquet
        """
        pass
```

## 第 5 步：生成 `update_data.py`

增量更新逻辑：

```python
"""
增量更新脚本
- 读取已有数据的最新时间戳
- 从该时间戳开始拉取新数据
- 拼接 + 清洗
- 避免重复拉取整个历史
"""
```

## 第 6 步：数据质量报告

脚本运行后必须输出：

```
📊 数据质量报告 — 2025-07-05
═══════════════════════════════════

币安 BTC/USDT 1h
├── 行数：17,520（预期 17,544）
├── 缺失：24 行 (0.14%)
├── 时间范围：2023-07-05 00:00 ~ 2025-07-05 00:00
├── 异常值标记：3 条（价格跳跃）
└── 状态：✅ 可以用于回测

币安 ETH/USDT 1h
├── 行数：17,520（预期 17,544）
├── 缺失：24 行 (0.14%)
├── 时间范围：2023-07-05 00:00 ~ 2025-07-05 00:00
├── 异常值标记：1 条
└── 状态：✅ 可以用于回测

交叉验证（币安 vs OKX）
├── BTC 平均价差：0.03%
├── ETH 平均价差：0.05%
├── 分歧事件：12 次（价差 > 0.5%）
└── 状态：✅ 数据一致

⚠️ 警告：
- ETH 2024-08-05 12:00 K 线 OKX 缺失，已用币安数据填补
- 建议检查该时段是否有交易所维护公告
```

## 必避的坑

| 坑 | 防法 |
|------|------|
| 只拉一个交易所 | 强制要求至少币安+OKX |
| 不去重导致回测偏差 | 清洗第一步就是去重 |
| 时间戳没对齐（回测偷看未来） | 统一对齐到 K 线边界 |
| 直接用 Web 端 K 线图（可能有数据修正） | 只用 API 原始数据 |
| 忽略交易所维护/宕机的数据缺口 | 缺失比例检查 + 标记 |
| 小币种交易量是洗盘刷的 | 交叉验证交易所交易量，偏差过大标记 |
