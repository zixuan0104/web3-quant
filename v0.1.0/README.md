# v0.1.0 — MVP 雏形

> 2026-07-05 发布 | 回测引擎可用 · 4 策略跑通 · 外部信号骨架就位

## 三层架构

```
┌─────────────────────────────────────┐
│  Layer 3: 外部信号 (external_signals) │  ← 币安Alpha · KOL · 鲸鱼 · 订单簿 · 信号融合
├─────────────────────────────────────┤
│  Layer 2: 回测 + 策略 (backtest)      │  ← 4策略 · 引擎 · 绩效指标 · 参数优化 · 模拟盘
├─────────────────────────────────────┤
│  Layer 1: 数据管线                    │  ← fetch → clean → update · 双源交叉验证
└─────────────────────────────────────┘
```

## 数据管线

```python
# 覆盖 6 标的 × 2 周期，币安 + OKX 双源
fetch_data.py   # 拉取 → raw/
clean_data.py   # 清洗 → clean/*.parquet（双源交叉验证）
update_data.py  # 增量更新
```

## 回测引擎

```python
backtest/
├── engine.py           # 事件驱动核心（滑点、手续费、资金管理）
├── strategy_base.py    # 策略基类，统一接口
├── metrics.py          # 夏普 / 最大回撤 / 胜率 / 盈亏比 / Calmar
└── strategies/
    ├── trend.py        # EMA 双均线 + ATR 止损
    ├── momentum.py     # 多周期动量因子 + 排名筛选
    ├── breakout.py     # Donchian Channel + 成交量确认
    └── funding_arb.py  # 现货多头 + 永续空头，费率收益
```

## 参数优化

```python
parameter_scan.py       # 网格搜索
order_type_comparison.py # 限价单 vs 市价单对比
optimize_btc.py         # BTC 专项优化
compare_p0p1.py         # 调参前后对比
compare_tuned.py        # 多策略调参对比
```

## 模拟盘

```python
paper_trader.py   # 模拟执行，输出 equity 曲线
# 已跑通 4 组对照：Trend/Momentum × Limit/Market
```

## 外部信号

```python
external_signals/
├── binance_alpha.py     # 币安 Alpha 上币公告 + 做市商信号
├── kol_monitor.py       # KOL 推特提及频率 + 情绪打分
├── whale_tracker.py     # 大额转账 + 聪明钱包监控
├── orderbook_monitor.py # 深度快照 + 做市商行为识别
├── hot_token_tracker.py # DexScreener 新币扫描
├── signal_fusion.py     # 多源信号加权融合 + 报告
└── deepseek_client.py   # LLM 辅助市场分析
```

## 风控 + 实盘壳

```python
risk_manager.py   # 仓位限制 · 回撤熔断 · 策略级+组合级
run_live.py       # 实盘入口（paper 模式）
trade_executor.py # 订单执行（限价/市价）
live_logger.py    # 结构化日志 JSONL
config_manager.py # 多环境配置切换
```

## 学习笔记

```
学习笔记/
├── day0-基础设施.md
├── day1-数据管线.md
├── day2-回测框架.md
├── day3-趋势跟踪策略.md
├── day4-动量与费率套利.md
├── day5-模拟盘验证.md
├── day6-交易成本.md
├── day7-实盘上线准备.md
├── day19-KOL监控.md
├── day20-鲸鱼与做市商.md
└── day21-信号融合.md
```

## 数据质量报告

`meta/` 目录包含本次 fetch 的元数据：
- `fetch_log.json` — 24 个任务全部完成，0 失败
- `quality_report.json` — 109,500 行数据，131 个异常事件（成交量峰值 + 价格跳跃），币安/OKX 平均分歧 0.01%～0.09%
