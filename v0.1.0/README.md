# v0.1.0

> 2026-07-05 发布 | 回测引擎 · 4 策略 · 外部信号骨架 · 模拟盘

## 更新内容

MVP 雏形发布。回测引擎跑通，4 条策略（趋势/动量/突破/费率套利）完成参数优化与模拟盘对照，外部信号 7 模块骨架就位，11 天学习笔记归档。

---

## 文件功能说明

### 数据管线

| 文件 | 功能 |
|------|------|
| `fetch_data.py` | 从币安 + OKX 双源拉取现货 K 线，覆盖 BTC/ETH/SOL/DOGE/PEPE/WIF 六个标的，支持 1h/1d 多周期，自动补全缺失时段 |
| `clean_data.py` | 数据清洗：去重、时间对齐、缺失值插值；双源交叉验证，计算币安/OKX 逐行价格分歧率，标记分歧事件；异常检测（成交量峰值 + 价格跳跃），产出清洗报告 |
| `update_data.py` | 增量更新脚本，对比本地最新时间戳，只拉取增量数据并合并，避免全量重拉 |

### 回测引擎

| 文件 | 功能 |
|------|------|
| `backtest/engine.py` | 事件驱动回测核心：逐 K 线遍历、滑点模拟、手续费扣除、资金管理（固定比例/凯利公式），输出逐笔交易记录 |
| `backtest/strategy_base.py` | 策略基类：定义 `generate_signals()` + `calculate_position_size()` 统一接口，内置参数校验、信号去重、持仓状态管理 |
| `backtest/metrics.py` | 绩效指标：年化收益率、夏普比率、最大回撤（含回撤区间）、胜率、盈亏比、Calmar 比率、滚动窗口稳定性 |
| `cost_model.py` | 交易成本建模：Maker/Taker 费率 + 线性/平方根滑点模型，支持按交易所和交易量动态调整 |
| `diagnose.py` | 策略诊断：逐笔交易归因分析，区分 alpha 收益 vs 市场 beta 收益，标记异常交易 |

### 策略库

| 文件 | 功能 |
|------|------|
| `backtest/strategies/trend.py` | EMA 双均线趋势跟踪：快线/慢线金叉做多、死叉平仓，ATR 动态止损止盈，支持多时间周期信号过滤 |
| `backtest/strategies/momentum.py` | 多周期动量因子策略：计算 7d/14d/30d 动量得分，跨标的排名筛选 Top-K，等权/动量加权两种分配方式 |
| `backtest/strategies/breakout.py` | Donchian Channel 突破策略：N 日最高/最低价通道，突破入场 + 成交量确认过滤器，通道中线动态止盈 |
| `backtest/strategies/funding_arb.py` | 资金费率套利：现货多头 + 永续合约空头对冲，监控 8h 费率结算周期，费率阈值触发开仓，自动展期 |

### 参数优化

| 文件 | 功能 |
|------|------|
| `parameter_scan.py` | 网格搜索参数扫描：对参数组合遍历回测，输出参数-绩效矩阵，支持多进程并行加速 |
| `order_type_comparison.py` | 限价单 vs 市价单对比：同一策略下两种订单类型分别回测，对比成交率、滑点损耗、最终收益差异 |
| `optimize_btc.py` | BTC 专属参数优化：针对 BTC 高流动性特征调整滑点模型和仓位参数，避免小币种参数外推到大市值标的 |
| `compare_p0p1.py` | 参数组对比（P0 vs P1）：两组参数同策略回测结果并排对比，输出收益率曲线叠加图 + 指标差异表 |
| `compare_tuned.py` | 多策略调参前后对比：所有策略调参前 vs 调参后的横向对比报告 |
| `dual_trend.py` | 双均线参数敏感性分析：快慢线周期组合的二维热力图，寻找稳定区域 |
| `trend_report.py` | 趋势策略专项报告：含参数敏感性、不同市况表现分解、最大回撤区间分析 |
| `strategy_comparison.py` | 四策略横向对比：全部策略按统一基准（相同时间段、相同标的池）横向排名 |

### 模拟盘

| 文件 | 功能 |
|------|------|
| `paper_trader.py` | 模拟交易执行引擎：逐 K 线模拟下单（市价/限价），记录持仓变化和权益曲线，输出 CSV 格式交易日志 |
| `run_paper_trade.py` | 模拟盘批量运行入口：指定策略+订单类型组合，自动运行并汇总 equity 曲线到 `logs/` 目录 |

### 外部信号

| 文件 | 功能 |
|------|------|
| `external_signals/__init__.py` | 外部信号模块入口，统一导出所有信号采集器 |
| `external_signals/binance_alpha.py` | 币安 Alpha 监控：抓取即将上币公告、新币上线后的做市商行为（挂单密度、买卖价差），综合评分预警 |
| `external_signals/kol_monitor.py` | KOL 推特监控：按配置的 KOL 列表拉取近期推文，关键词匹配 + NLP 情绪打分，输出每日 KOL 情绪摘要 |
| `external_signals/whale_tracker.py` | 鲸鱼追踪：监控链上大额转账（>100 ETH / >1000 SOL），标记已知聪明钱包地址，增量推送异常转账事件 |
| `external_signals/orderbook_monitor.py` | 订单簿深度分析：定时采集 depth 20 档快照，检测假单/冰山单/撤单模式，计算买卖压力比和失衡信号 |
| `external_signals/hot_token_tracker.py` | 热门代币扫描：对接 DexScreener API，扫描 Solana 新交易对、24h 成交量突增代币、流动性骤变池子 |
| `external_signals/signal_fusion.py` | 多源信号融合引擎：对 5 路信号（Alpha/KOL/鲸鱼/订单簿/热门代币）加权融合，输出统一评分和方向建议 |
| `external_signals/fusion_report.py` | 融合报告生成：将融合信号格式化为 Markdown 报告，含信号来源追溯、权重分解、历史准确率回测 |
| `external_signals/deepseek_client.py` | DeepSeek API 客户端：封装 LLM 调用接口，用于市场分析辅助、信号解读、自然语言报告生成 |
| `run_external_signals.py` | 外部信号全量运行入口：依次执行所有信号采集模块，汇总输出 |
| `run_hot_tokens.py` | 热门代币独立扫描入口：仅运行 DexScreener 新币扫描 |
| `run_scan_1d_smallcoins.py` | 小币种日线批量扫描：对所有非主流币种运行策略回测，筛选潜在机会 |

### 实盘壳

| 文件 | 功能 |
|------|------|
| `run_live.py` | 实盘入口（当前 paper 模式）：加载策略配置 → 获取行情 → 生成信号 → 风控检查 → 下单/记录，完整交易循环 |
| `trade_executor.py` | 订单执行器：对接交易所 API（限价单/市价单），含重试逻辑、部分成交处理、订单状态追踪 |
| `live_logger.py` | 结构化日志：JSONL 格式记录每笔信号、订单、成交、持仓变化，用于复盘和审计 |
| `config_manager.py` | 多环境配置切换：`paper` / `live` 双模式，自动加载对应 `.env` 变量和参数文件，切换时不改代码 |
| `risk_manager.py` | 风控模块：单笔仓位上限（%）、总敞口限制、连续回撤熔断（次数/幅度）、策略级 + 组合级双层校验 |

### 回测运行入口

| 文件 | 功能 |
|------|------|
| `run_backtest.py` | 回测主入口：指定策略 + 标的 + 周期，运行完整回测并打印指标报告 |
| `run_backtest_1d.py` | 日线回测入口：针对日线级别长周期策略的批量回测 |

### 数据与元信息

| 文件 | 功能 |
|------|------|
| `meta/fetch_log.json` | 最近一次数据拉取的完整日志：24 任务全部完成，含每标的行数、时间范围、文件路径 |
| `meta/quality_report.json` | 数据质量报告：109,500 行数据中 131 个异常事件（成交量峰值 + 价格跳跃），双源分歧概率 0.01%～0.09% |
| `学习笔记/` | 11 天量化学习笔记（day0～day7 + day19～day21），覆盖基础设施、数据管线、回测框架、策略设计、模拟盘验证、交易成本、实盘准备、KOL 监控、鲸鱼追踪、信号融合 |
