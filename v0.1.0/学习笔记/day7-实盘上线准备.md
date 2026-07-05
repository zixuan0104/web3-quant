# Day 7 学习笔记 — 实盘上线准备

## 一、实盘壳的设计哲学：没有 API key 也能开发

### 1.1 核心命题

"没有币安 API key"不是开发阻塞——和 Day 5 的模拟盘一样，把所有交易所相关的东西抽象成接口，paper 模式跑通再切换 live。

### 1.2 架构总览

```
run_live.py（调度入口）
    │
    ├── ConfigManager  → .env + 风控参数 + 策略列表
    ├── LiveLogger     → JSONL 结构化日志（交易/系统/风控/PnL）
    ├── RiskManager    → 四层风控（预检 → 单笔 → 日熔断 → 连续亏损）
    └── Exchange       → PaperExchange(当前) | BinanceExchange(将来)
```

### 1.3 关键设计决策

| 决策 | 理由 |
|------|------|
| 风控有最终否决权 | 策略说买、风控说不买 → 不买。无绕过路径 |
| Exchange 抽象接口 | paper/live 模式切换只改一行 `--mode` 参数 |
| JSONL 日志格式 | 一行一个 JSON 事件，grep/jq 可直接分析 |
| .env 不入库 | API key 等敏感信息与环境绑定，不跟随代码 |

---

## 二、四层风控系统

### 2.1 层级结构

```
L0: 预交易门禁（仓位上限 / 最小订单金额 / 可用余额）
L1: 单笔止损（硬止损本金的 1-2%）
L2: 日内熔断（当日累计亏损达本金 5% → 暂停 24h）
L3: 连续亏损检测（连续 N 次亏损 → 暂停 4h）
L4: 时间止损（持仓超时自动平仓）
```

### 2.2 熔断机制

```
触发条件               动作         冷却时间
────────────────────────────────────────────
日亏损 ≥ 5%          暂停所有策略   24 小时
连续亏损 ≥ 5 次       暂停所有策略   4 小时
价格异常波动 >10%     仅拒绝这单     无冷却
```

### 2.3 风控核心代码路径

```python
# 每笔交易前必须走的路径：
result = risk_mgr.pre_trade_check(symbol, side, size_pct, price, bar)
if not result.passed:
    return  # 拒绝，不执行

# 交易后更新：
risk_mgr.update_after_trade(net_return_pct)
```

---

## 三、结构化日志系统

### 3.1 四个日志流

| 日志文件 | 记录内容 | 示例事件 |
|---------|---------|---------|
| `trades-YYYYMMDD.jsonl` | 订单完整生命周期 | signal → submitted → filled → closed |
| `system-YYYYMMDD.jsonl` | 系统运行状态 | startup / heartbeat / error / shutdown |
| `risk-YYYYMMDD.jsonl` | 风控事件 | circuit_breaker / position_adjust / anomaly |
| `pnl-YYYYMMDD.jsonl` | 净值快照 | snapshot / daily_summary |

### 3.2 JSONL 格式的优势

- 每行一个独立 JSON 对象，可追加写入不损坏历史数据
- 支持 `grep "event.*filled" trades.jsonl | jq '.fill_price'` 快速分析
- 按日期滚动，单文件不会过大
- 即使程序崩溃，已写入的日志不丢失（无缓冲写入）

### 3.3 日志保留策略

默认保留 90 天，超期文件需手动清理（或配置自动归档）。

---

## 四、PaperExchange：本地模拟交易所

### 4.1 撮合模型

```
市价单: 立即以 bar close + 成本模型成交
限价单: 挂单簿 → 每根 bar 检查 high/low 是否触达限价
       → 触达 = 成交于限价
       → 超时（默认 24 根 K 线）= 取消
```

### 4.2 账户模型

- `available_balance` — 可用余额
- `locked_balance` — 挂单锁定余额（防超买）
- `positions` — 当前持仓列表
- `order_history` — 历史订单

### 4.3 限价单撮合逻辑

```python
# 买入限价单: bar.low <= limit_price → 成交于 limit_price
# 卖出限价单: bar.high >= limit_price → 成交于 limit_price
# 每根 bar 结束时无条件调用 check_pending_orders()
```

这个逻辑比市价单更真实——不保证立即成交，有漏单风险。

---

## 五、BTC 1h 实测结果（17,520 根 K 线，2024.07-2026.07）

### 5.1 全链路表现

```
策略信号: 536 次（268 入场 + 268 出场）
风控拒绝: 6 次（价格异常波动 >10%）
订单执行: 88 笔（44 笔入场 + 44 笔出场）
完成交易: 43 笔（因净值跌至 $46 后，剩余订单 < 最小金额被拒）
```

### 5.2 关键发现

1. **趋势策略在这段数据上亏损**：这和回测结果一致（回测 -76.52%，模拟盘 -99.54%）。策略本身需要优化，不是系统问题。

2. **净值跌到 $46 后交易被风控阻止**：这是正确行为——$46 的 20% = $9.15 < $10（币安最小下单），不应该继续交易。真实场景中，亏损到这个程度应该停止交易、重新评估策略。

3. **限价单成交率 100%**：BTC 流动性好，每根 bar 的 high/low 都能触达限价。小币种或 Meme 会有更高的漏单率。

---

## 六、API Key 就绪后的切换清单

当 API key 就绪后，只需做以下几步就能从 paper 切换到 live：

1. **`.env` 配置 API key**
   ```
   BINANCE_API_KEY=your_real_key
   BINANCE_SECRET_KEY=your_real_secret
   BINANCE_TESTNET=false
   ```

2. **补全 `BinanceExchange.submit_order()`**
   ```python
   # 当前是 NotImplementedError
   # 补一行：
   result = self._client.create_order(symbol, order_type, side, quantity, price)
   ```

3. **替换实时数据源**
   ```python
   # Day 5 的 HistoricalDataFeed → BinanceTestnetFeed
   # 抽象接口已就绪，只需写一个新的 feed 实现
   ```

4. **运行**
   ```bash
   python run_live.py --mode live --capital 200
   ```

---

## 七、本日复盘

| 学到了什么 | 怎么用 |
|-----------|--------|
| 没有 API key 也能开发实盘系统——抽象接口隔离外部依赖 | 所有外部依赖（交易所/数据源/API）都用接口封装 |
| 风控必须有最终否决权——无绕过路径 | RiskManager.pre_trade_check() 是所有交易的唯一入口 |
| 限价单撮合需要每根 bar 检查，不能用信号触发的即时成交 | check_pending_orders 无条件调用，不依赖信号 |
| JSONL 日志比 CSV/数据库更适合交易系统 | 追加写入不损坏历史、grep 可分析、按日期滚动 |
| 最小订单检查是防止"账户亏完后继续下注"的最后防线 | 不能去掉，只能根据交易所实际下限调整阈值 |
| 纸交易模式的价值不在模拟盈亏数字，在验证全链路流程 | 信号→风控→订单→日志→PnL，每个环节都要验证 |
| 配置管理不只是加载 .env——要做合法性校验和硬上限保护 | 风控参数只能收紧不能放宽，硬上限写在代码里 |
