# Skills

Claude Code Agent Skill 定义文件，位于 `skills/` 目录，由 Claude Code 自动加载。

## 用途

每个 `.md` 文件是一个 skill，定义了 agent 在特定场景下的行为规范、参考框架和工作流程。当你对 Claude Code 提出量化相关需求时，匹配的 skill 会自动激活，确保输出质量和一致性。

## 设计原则

**引用框架而不复制框架** — skills 里存放的是对外部方法论的适配说明和使用指引，不是原文搬运。

## 技能列表（12 个）

```
skills/
├── quant-onboarding.md        # 量化入门引导 — 从零搭建系统的路线设计
├── quant-data.md              # 数据管线助手 — 拉取/清洗/存储脚本生成
├── quant-strategy.md          # 策略设计 — 想法→回测代码+过拟合检查
├── quant-backtest.md          # 回测报告生成 — 指标+样本内外+市况分解
├── quant-risk.md              # 风控检查 — 上线前逐项审核+运行中风控代码
├── quant-strategy-health.md   # 策略健康度 — 每日评分+退役/唤醒机制
├── quant-market-regime.md     # 市场环境分类 — 趋势/震荡/高波/低波+仓位建议
├── quant-report.md            # 四层报告 — 日报/周报/月报/年报
├── quant-binance-alpha.md     # 币安 Alpha 监控 — 上币公告+做市商信号
├── quant-kol.md               # KOL 与社交情绪聚合
├── quant-orderbook.md         # 订单簿分析 — 做市商行为+买卖压力
└── quant-onchain.md           # 链上数据追踪 — 大额转账+聪明钱包+交易所余额
```

## 覆盖链路

```
入门引导 → 数据管线 → 策略设计 → 回测验证 → 风控审核 → 模拟盘 → 实盘
                ↓                                    ↓
          外部信号聚合 ←── 币安Alpha / KOL / 订单簿 / 链上
                ↓
           信号融合 → 策略健康度 → 市场环境 → 四层报告
```
