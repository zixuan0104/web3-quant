# web3-quant

Web3 加密量化交易系统 — 从数据管线、回测引擎、策略库到外部信号聚合的一站式量化框架。

**当前版本：v0.1.0** — MVP 雏形：回测引擎可用，4 条策略跑通，外部信号模块骨架就位。

---

## 项目结构

```
web3量化0705/
├── skills/                  # AI Agent 技能定义（12 个）
├── v0.1.0/                  # ← 当前版本快照（静态代码包）
│   ├── README.md            #   版本功能说明（逐文件）
│   ├── backtest/            #   回测引擎 + 4 条策略
│   ├── external_signals/    #   外部信号聚合（7 个模块）
│   ├── meta/                #   数据质量报告
│   ├── 学习笔记/             #   每日学习笔记（11 天）
│   ├── fetch_data.py        #   数据拉取
│   ├── clean_data.py        #   数据清洗
│   ├── run_backtest.py      #   回测入口
│   ├── run_live.py          #   实盘入口（paper 模式）
│   ├── risk_manager.py      #   风控模块
│   ├── paper_trader.py      #   模拟交易
│   └── config_manager.py    #   多环境配置
├── .env.example             # 环境变量模板
└── .gitignore               # 安全配置（密钥/数据/日志不入库）
```

---

## v0.1.0 功能清单

### 数据管线
- `fetch_data.py` — 从币安 / OKX 拉取现货 K 线数据
- `clean_data.py` — 数据清洗（去重、对齐、缺失值处理）
- `update_data.py` — 增量更新，支持 1d / 1h 多时间周期
- 覆盖标的：BTC、ETH、SOL、DOGE、PEPE、WIF

### 回测引擎 (`backtest/`)
- `engine.py` — 事件驱动回测核心（滑点、手续费、资金管理）
- `strategy_base.py` — 策略基类（统一接口，参数化配置）
- `metrics.py` — 绩效指标（夏普、最大回撤、胜率、盈亏比、Calmar）
- `cost_model.py` — 交易成本建模（手续费 + 滑点）

### 策略库 (`backtest/strategies/`)
| 策略 | 文件 | 逻辑 |
|------|------|------|
| 趋势跟踪 | `trend.py` | EMA 双均线 + ATR 止损 |
| 动量 | `momentum.py` | 多时间周期动量因子 + 排名筛选 |
| 突破 | `breakout.py` | Donchian Channel + 成交量确认 |
| 资金费率套利 | `funding_arb.py` | 现货多头 + 永续空头，费率收益 |

### 参数优化
- `parameter_scan.py` — 网格搜索参数扫描
- `order_type_comparison.py` — 限价单 vs 市价单对比分析
- `optimize_btc.py` — BTC 专项参数优化
- `compare_p0p1.py` / `compare_tuned.py` — 调参前后对比

### 模拟盘 (`paper_trader.py`)
- 模拟交易执行，输出 equity 曲线 CSV
- 已跑通：Trend + Momentum × Limit + Market 四组对照

### 外部信号 (`external_signals/`)
| 模块 | 文件 | 功能 |
|------|------|------|
| 币安 Alpha | `binance_alpha.py` | 监控即将上币公告 + 做市商信号 |
| KOL 监控 | `kol_monitor.py` | 推特 KOL 提及频率 + 情绪打分 |
| 鲸鱼追踪 | `whale_tracker.py` | 大额转账 + 聪明钱包监控 |
| 订单簿 | `orderbook_monitor.py` | 深度快照 + 做市商行为识别 |
| 热门代币 | `hot_token_tracker.py` | DexScreener 新币扫描 |
| 信号融合 | `signal_fusion.py` | 多源信号加权融合 + 报告生成 |
| DeepSeek 客户端 | `deepseek_client.py` | LLM 辅助市场分析 |

### 风控 (`risk_manager.py`)
- 单笔仓位上限、总敞口限制、连续回撤熔断
- 策略级 + 组合级两层风控

### 实盘壳（纸交易模式）
- `run_live.py` — 实盘入口（目前跑 paper 模式）
- `trade_executor.py` — 订单执行（支持限价/市价）
- `live_logger.py` — 结构化日志（JSONL）
- `config_manager.py` — 多环境配置切换

---

## Skills（AI Agent 技能包）

项目内置 12 个 Claude Code Agent Skill，覆盖量化全流程：

| Skill | 用途 |
|-------|------|
| `quant-onboarding` | 量化系统搭建引导，从零到一的新手路线 |
| `quant-data` | 数据管线助手 — 拉取、清洗、存储脚本生成 |
| `quant-strategy` | 策略设计助手 — 想法 → 回测代码 + 过拟合检查 |
| `quant-backtest` | 回测报告生成器 — 指标、样本内外对比、市况分解 |
| `quant-risk` | 风控检查清单 — 上线前逐项审核 + 运行中风控代码 |
| `quant-strategy-health` | 策略健康度诊断 — 每日评分 + 退役/唤醒机制 |
| `quant-market-regime` | 市场环境分类 — 趋势/震荡/高波/低波 + 仓位建议 |
| `quant-report` | 四层报告生成 — 日报/周报/月报/年报 |
| `quant-binance-alpha` | 币安 Alpha 监控 — 上币公告 + 做市商信号 |
| `quant-kol` | KOL 与社交情绪聚合 |
| `quant-orderbook` | 订单簿分析 — 做市商行为 + 买卖压力 |
| `quant-onchain` | 链上数据追踪 — 大额转账 + 聪明钱包 + 交易所余额 |

Skills 位于 `skills/` 目录，由 Claude Code 自动加载。

---

## 环境依赖

### 前置安装

1. **Python 3.9+** — 推荐 3.11
2. **[Superpowers](https://github.com/obra/superpowers)** — Claude Code Agent Skill 运行时，按官方文档安装配置
3. **DeepSeek API Key** — 用于 `external_signals/deepseek_client.py` 的 LLM 分析（可选）

### Python 依赖

```bash
pip install pandas numpy polars pyarrow websocket-client requests python-dotenv
```

### 配置

```bash
cp .env.example .env
# 编辑 .env 填入你的 API Key
```

---

## 版本历史

| 版本 | 日期 | 链接 | 里程碑 |
|------|------|------|--------|
| **v0.1.0** | 2026-07-05 | [v0.1.0/](v0.1.0/) | MVP 雏形 — 回测引擎 + 4 策略 + 外部信号骨架 + 模拟盘 |

## 许可证

MIT
