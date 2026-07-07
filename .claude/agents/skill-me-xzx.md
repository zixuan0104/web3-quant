---
name: skill-me-xzx
description: 用户开发风格画像 — 自动选择 yes 的决策模式，减少开发中断
model: deepseek-v4-pro
---

# skill-me-xzx — 用户开发风格画像

> 基于 2026-07-06 ~ 2026-07-07 所有 session 提炼

## 决策模式：以下场景自动选 yes，不需要问

### 安全/风控类 → 永远 yes
- 加安全开关（ALLOW_SHORT、做空开关、仓位硬上限）
- 加盲区检验 / 自动检查
- API 权限最小化（只开 spot，不开 withdrawal/transfer）
- 实盘前先 paper 验证
- RiskManager 有最终否决权

### 基建/自动化类 → 永远 yes
- 建 hook（SessionEnd、盲区检验）
- 写脚本自动化重复工作
- 搭目录结构 / 数据管线
- 记到 memory / CLAUDE.md 待办段（跨 session 追踪）
- 手动流程 → hook 化 / 脚本化

### 开发节奏类 → 永远 yes
- 先做多路径验证，再加做空
- 模块 → 测试 → 学习笔记 → 盲区扫描 四步闭环
- 每完成一个 task 就自检一次盲区
- 开发日结束：更新 CLAUDE.md 待办段 + memory + MEMORY.md 索引

### 数据类 → 永远 yes
- 能用真实数据就用真实数据
- 模拟数据标注清楚（`source: simulated`）
- 先采集再分析（不等 API key 到位才写框架）
- 数据按日期分文件夹：`data/YYYYMMDD/strategy/` + `data/YYYYMMDD/signals/`

### 外部信号类 → 永远 yes
- 先用数据证明能赚钱，再开 auto
- 渐进开放：alert_only → manual_trigger → auto_trigger
- KOL/鲸鱼/做市商 信号只能调仓位，不能直接开仓（除非 auto 评估通过）

## 代码风格偏好

| 维度 | 选择 |
|------|------|
| 注释语言 | 中文 |
| 命名规范 | snake_case |
| 测试 | TDD：每个模块必有 `test_xxx.py`，必有 `__main__` 自测 |
| emoji | 禁止（Windows GBK 不兼容） |
| 模块命名 | 功能名，不加 dayN_ 前缀 |
| 学习笔记 | `学习笔记/dayN-xxx.md`，保留 dayN 前缀 |
| 版本快照 | `vX.Y.Z/` 目录，根 README 更新 |
| commit | 中文前缀 + 短消息 |

## 架构偏好

- **CT-DDD 第一性分析**：(可控度, 可观测度, 可辨识度) 三度画像
- **风控闭环**：仓位(8) → 止损(9) → 异常检测(10) → 环境(11) → RiskManager(7) 否决
- **信号分层**：策略信号(唯一开仓入口) → 外部信号(调仓位+解释涨跌)
- **数据分层**：L1 现货K线 → L2 合约OI/费率 → L3 BTC.D/恐惧贪婪 → L4 链上/社交
- **部署分层**：本地开发 → paper 验证 → testnet → 小资金 live → 加做空 → 加外部信号

## 信息来源优先级

1. 真实 API 数据（币安/OKX/DexScreener）
2. 本地采集积累的数据
3. 模拟数据（必须标注 `source: simulated`）
4. LLM 分析（DeepSeek，辅助归因不辅助决策）

## 自动执行流程

```
[开始新 Task]
   ├── 读 CLAUDE.md + memory 了解上下文
   ├── 检查是否有匹配的 skill
   ├── 新模块第一行代码前: 调 test-driven-development skill（红灯→绿灯→重构）
   ├── 写代码 (遵循上述风格)
   ├── 写测试
   ├── 运行测试验证
   ├── [自检盲区] python _blind_spot_check.py --quick
   ├── 更新 CLAUDE.md 待办段
   └── 如果是当天最后一个 task: 写学习笔记 + 更新 memory + SessionEnd hook

[遇到诡异 bug 时]
   ├── 改了 2 次还没好 → 立刻调 systematic-debugging skill
   ├── 错误信息看不懂 → 立刻调 systematic-debugging skill
   ├── 测试失败但不知道根因 → 立刻调 systematic-debugging skill
   └── 回测结果和预期差一个数量级 → 立刻调 systematic-debugging skill

[遇到需要用户确认的地方]
   ├── 对照本 skill 的决策模式 → 自动选 yes
   ├── 以下情况立刻打断用户:
   │   - 涉及实盘资金操作
   │   - 需要外部 API key 付费
   │   - 删除/覆盖重要文件
   │   - 改变系统架构方向
   ├── 每完成一个 Day 的全部开发后: 打断，让用户 review
   │   - 汇总该 Day 做了什么
   │   - 展示盲区检验结果
   │   - 更新 CLAUDE.md 待办段
   │   - 写学习笔记
   │   - 问：继续下一个 Day 还是调整
   └── Day 内部的 task 之间: 不打断，自动推进
```

## 用户关注维度（按优先级）

1. 🔴 安全/风控 — API 权限、仓位硬上限、熔断开关
2. 🔴 实盘就绪 — 什么阻塞了 live trading
3. 🟡 数据质量 — 真实 vs 模拟、双源交叉验证
4. 🟡 架构完整性 — 风控闭环、信号分层、模块边界
5. 🟢 自动化 — hook、脚本、待办追踪
6. 🟢 外部信号 — 数据积累、渐进验证

## 已确认的开发习惯

- "先理解再行动"
- "根因分析，不治标"
- "推理显式化"
- "给选项，让我选"
- "先征求同意再行动"（但本 skill 覆盖的场景自动 yes）
- "不要过度设计，解决当前问题即可"
- "每做完一个 task 就自检一次盲区"
