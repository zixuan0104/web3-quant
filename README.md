# web3-quant

> 加密量化交易系统 — 从数据管线到实盘执行的完整工具链。
> 代码是武器，配置是弹药。武器可以分享，弹药必须私藏。

## 架构

```
data/                   ← 工作目录（当前版本代码）
├── backtest/           ← 回测引擎 + 策略库
├── external_signals/   ← 链上+社交+订单簿信号聚合
├── fetch_data.py       ← 币安+OKX 双源数据管线
├── run_live.py         ← 实盘入口
└── 学习笔记/           ← 每日知识积累

skills/                 ← 12 个 Claude Code Agent Skill 定义
v0.1.0/                 ← 版本快照（静态代码包）
```

## 三层管线

```python
# L1: 数据 — 6 标的 × 2 周期，双源交叉验证
fetch_data.py → clean_data.py → update_data.py

# L2: 回测 — 事件驱动引擎，成本模型，4 策略
backtest/engine.py + strategy_base.py + metrics.py
  strategies/
    trend.py       # EMA 双均线 + ATR 止损
    momentum.py    # 多周期动量因子 + 排名筛选
    breakout.py    # Donchian Channel + 成交量确认
    funding_arb.py # 现货多头 + 永续空头，费率收益

# L3: 外部信号 — 7 模块聚合
binance_alpha.py → kol_monitor.py → whale_tracker.py
  → orderbook_monitor.py → hot_token_tracker.py
  → signal_fusion.py → deepseek_client.py
```

## 风控边界

```python
risk_manager.py
  ├── 单笔仓位上限
  ├── 总敞口限制
  ├── 连续回撤熔断
  └── 策略级 + 组合级双层
```

## Skills 覆盖链

```
入门引导 → 数据管线 → 策略设计 → 回测 → 风控 → 模拟盘 → 实盘
                                    ↓
              外部信号 ← 币安Alpha · KOL · 订单簿 · 链上
                                    ↓
              信号融合 → 健康度诊断 → 市场环境 → 四层报告
```

## 快速开始

```bash
# 环境
pip install pandas numpy polars pyarrow websocket-client requests python-dotenv

# 配置
cp .env.example .env
# 编辑 .env 填入 API Key

# 拉数据
python data/20260705/fetch_data.py

# 跑回测
python data/20260705/run_backtest.py
```

## 安全

`.env` · `logs/` · `*.csv` · `*.jsonl` · `*.parquet` · `*.key` · `__pycache__/` — 全部 `.gitignore` 拦截，永不上传。

## 版本历史

| 版本 | 日期 | 链接 | 里程碑 |
|------|------|------|--------|
| v0.1.0 | 2026-07-05 | [v0.1.0/](v0.1.0/) | MVP — 回测引擎 + 4策略 + 外部信号骨架 + 模拟盘 |

## 许可

MIT
