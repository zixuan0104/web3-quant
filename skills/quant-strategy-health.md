---
name: quant-strategy-health
description: 策略健康度诊断 — 每日自动评分（绿灯/黄灯/橙灯/红灯），含参数滚动优化防过拟合、策略退役机制、休眠策略唤醒条件。
---

# 策略健康度诊断

策略不是一次设计完就万事大吉。市场在变，策略在衰减。这套机制帮你判断策略还活着没。

## 硬性规则

1. **每天自动评分**：UTC 00:05 日报生成前跑一次。
2. **灯号有实际后果**：绿灯正常、黄灯关注、橙灯减仓、红灯暂停。不是装饰品。
3. **区分失效和不适**：策略亏钱 ≠ 策略失效。可能是环境切换（用 market-regime 判断）。
4. **退役不删代码**：退役=进休眠库，环境恢复了可以唤醒。

---

## 第 1 步：每日健康度评分

```python
class StrategyHealthMonitor:
    """
    策略健康度每日评估
    
    输出：
    - 综合评分 (0-100)
    - 灯号（绿/黄/橙/红）
    - 诊断建议
    """
    
    def evaluate(self, strategy, lookback_days=30):
        """
        六维度加权评分
        """
        scores = {
            # 1. 夏普比率（滚动 30 天）—— 权重 25%
            'sharpe_30d': self.score_sharpe(strategy, lookback_days) * 0.25,
            
            # 2. 回撤状态 —— 权重 25%
            'drawdown_status': self.score_drawdown(strategy) * 0.25,
            
            # 3. 胜率偏离 —— 权重 15%
            'winrate_deviation': self.score_winrate(strategy, lookback_days) * 0.15,
            
            # 4. 连续亏损天数 —— 权重 15%
            'consecutive_losses': self.score_consecutive_losses(strategy) * 0.15,
            
            # 5. 样本外表现比 —— 权重 20%
            'out_of_sample_ratio': self.score_oos_ratio(strategy) * 0.20,
        }
        
        total_score = sum(scores.values())
        light = self.determine_light(total_score)
        
        return {
            'score': total_score,
            'light': light,
            'breakdown': scores,
            'recommendation': self.get_recommendation(light, scores),
        }
    
    def score_sharpe(self, strategy, days):
        """
        夏普评分（满分 25）
        
        滚动 30 天夏普：
        > 1.5  → 满分 25
        1.0-1.5 → 20
        0.5-1.0 → 15
        0.0-0.5 → 10
        < 0    → 5
        """
        sharpe = strategy.rolling_sharpe(days)
        if sharpe > 1.5: return 25
        elif sharpe > 1.0: return 20
        elif sharpe > 0.5: return 15
        elif sharpe > 0.0: return 10
        else: return 5
    
    def score_drawdown(self, strategy):
        """
        回撤评分（满分 25）
        
        当前回撤 / 历史最大回撤：
        < 50%  → 满分 25（正常回撤范围内）
        50-80% → 20
        80-100% → 10（接近历史极值，警戒）
        > 100% → 0（创新高回撤！红色警报）
        """
        current_dd = strategy.current_drawdown()
        historical_max_dd = strategy.historical_max_drawdown()
        
        if historical_max_dd == 0:
            return 25
        
        ratio = current_dd / historical_max_dd
        
        if ratio < 0.5: return 25
        elif ratio < 0.8: return 20
        elif ratio < 1.0: return 10
        else: return 0  # 创新高回撤
    
    def score_winrate(self, strategy, days):
        """
        胜率偏离评分（满分 15）
        
        近 30 天胜率 vs 历史胜率：
        偏离 < 10% → 满分 15
        偏离 10-20% → 10
        偏离 20-40% → 5
        偏离 > 40% → 0
        """
        recent_wr = strategy.winrate(days=30)
        historical_wr = strategy.historical_winrate()
        
        if historical_wr == 0:
            return 15
        
        deviation = abs(recent_wr - historical_wr) / historical_wr
        
        if deviation < 0.1: return 15
        elif deviation < 0.2: return 10
        elif deviation < 0.4: return 5
        else: return 0
    
    def score_consecutive_losses(self, strategy):
        """
        连续亏损评分（满分 15）
        
        当前连续亏损：
        0-3 天 → 15
        4-5 天 → 10（关注）
        6-10 天 → 5（警戒）
        > 10 天 → 0（红色）
        """
        consecutive = strategy.consecutive_losses()
        
        if consecutive <= 3: return 15
        elif consecutive <= 5: return 10
        elif consecutive <= 10: return 5
        else: return 0
    
    def score_oos_ratio(self, strategy):
        """
        样本外表现比（满分 20）
        
        策略上线后实盘收益 / 历史回测同期收益：
        > 80%  → 满分 20
        50-80% → 15
        20-50% → 10（过拟合嫌疑）
        0-20%  → 5
        < 0    → 0（实盘亏，回测赚——典型过拟合）
        """
        live_return = strategy.live_return_since_deploy()
        backtest_return = strategy.backtest_return_same_period()
        
        if backtest_return <= 0:
            return 20  # 回测也不赚钱的情况看其他指标
        
        ratio = live_return / backtest_return
        
        if ratio > 0.8: return 20
        elif ratio > 0.5: return 15
        elif ratio > 0.2: return 10
        elif ratio > 0: return 5
        else: return 0
    
    def determine_light(self, score):
        """
        灯号判定
        🟢 绿灯 80-100：正常运行
        🟡 黄灯 60-79：关注（提醒但不操作）
        🟠 橙灯 40-59：减仓 50%（自动执行）
        🔴 红灯  <40：暂停策略（自动执行）
        """
        if score >= 80: return 'GREEN'
        elif score >= 60: return 'YELLOW'
        elif score >= 40: return 'ORANGE'
        else: return 'RED'
```

---

## 第 2 步：灯号对应的系统动作

```python
# 灯号 → 自动操作映射
LIGHT_ACTIONS = {
    'GREEN': {
        'position_multiplier': 1.0,
        'entry_allowed': True,
        'telegram_alert': False,
        'action': '正常运行',
    },
    'YELLOW': {
        'position_multiplier': 1.0,
        'entry_allowed': True,
        'telegram_alert': True,  # 提醒用户关注
        'action': '正常交易，但发提醒关注',
        'alert_message': '🟡 策略 [{name}] 健康度降至 {score} 分，建议关注',
    },
    'ORANGE': {
        'position_multiplier': 0.5,     # 仓位减半
        'entry_allowed': True,
        'new_position_size': 0.5,       # 新开仓也只能半仓
        'telegram_alert': True,
        'action': '自动减仓 50%，需人工分析原因',
        'alert_message': '🟠 策略 [{name}] 健康度降至 {score} 分，已自动减仓 50%，请尽快分析原因',
    },
    'RED': {
        'position_multiplier': 0.0,     # 只平不平
        'entry_allowed': False,         # 禁止新开仓
        'telegram_alert': True,
        'require_manual_review': True,  # 必须人工介入才能重启
        'action': '自动暂停，只允许平仓，需人工审查后解除',
        'alert_message': '🔴 策略 [{name}] 健康度降至 {score} 分，已自动暂停！请立即审查',
    },
}
```

---

## 第 3 步：参数滚动优化（防过拟合）

```python
class RollingOptimizer:
    """
    参数滚动优化
    
    每周自动执行一次（Week 3 Day 17 后）
    
    核心安全机制：
    - 样本内优化 → 样本外验证 → 验证失败 = 不更新
    - 连续 3 次验证失败 → 怀疑策略逻辑本身出问题
    """
    
    def optimize(self, strategy, price_data):
        """
        滚动窗口验证法：
        
        数据分区：
        |---- 样本内（最近 60 天）----|---- 样本外（前 60 天）----|
                                       ↑ 用于验证的区间
        
        1. 在样本内优化参数
        2. 在样本外验证
        3. 样本外表现 ≥ 样本内的 70% → 参数可以更新
        4. < 70% → 过拟合嫌疑，参数不更新
        """
        
        # 1. 分区
        in_sample = price_data[-60:]
        out_of_sample = price_data[-120:-60]
        
        # 2. 样本内优化
        best_params, in_sample_performance = self.grid_search(
            strategy, in_sample
        )
        
        # 3. 样本外验证
        out_of_sample_performance = self.backtest_with_params(
            strategy, best_params, out_of_sample
        )
        
        # 4. 验证比率
        validation_ratio = out_of_sample_performance.sharpe / in_sample_performance.sharpe
        
        if validation_ratio >= 0.7:
            return {
                'approved': True,
                'new_params': best_params,
                'in_sample_sharpe': in_sample_performance.sharpe,
                'out_of_sample_sharpe': out_of_sample_performance.sharpe,
                'validation_ratio': validation_ratio,
            }
        else:
            # 参数不更新，记录这次失败
            self.failed_attempts += 1
            
            if self.failed_attempts >= 3:
                return {
                    'approved': False,
                    'warning': '⚠️ 连续 3 次参数更新验证失败，建议人工审查策略逻辑',
                    'failed_attempts': self.failed_attempts,
                }
            
            return {
                'approved': False,
                'reason': f'样本外验证未通过 ({validation_ratio:.0%} < 70%)',
                'failed_attempts': self.failed_attempts,
            }
```

---

## 第 4 步：策略退役机制

```python
class StrategyRetirement:
    """
    策略退役管理
    
    退役 ≠ 删除。退役 = 进休眠库，环境变化可以唤醒。
    """
    
    RETIREMENT_CONDITIONS = [
        {
            'condition': '连续 2 个月夏普 < 0.5',
            'action': '移入休眠库，保留代码和数据，每月自动检查一次环境是否恢复',
        },
        {
            'condition': '最大回撤创历史新高 + 健康度 < 50',
            'action': '立即暂停，标记退役审查',
        },
        {
            'condition': '样本外连续 3 个月跑输回测基准 50%+',
            'action': '退役，分析失效原因，写入策略日志（策略墓志铭）',
        },
    ]
    
    def monthly_retirement_check(self, strategy):
        """
        每月检查退役条件（月报的一部分）
        
        输出：
        - 是否触发退役条件
        - 建议：继续运行 / 减少资金 / 退役休眠
        """
        triggered = []
        for condition in self.RETIREMENT_CONDITIONS:
            if self.evaluate_condition(strategy, condition['condition']):
                triggered.append(condition)
        
        return triggered
    
    def write_strategy_epitaph(self, strategy):
        """
        策略墓志铭：
        
        「[策略名] 生于 [上线日期]，死于 [退役日期]，
        存续 [N] 天，总盈利 [X] USDT。
        死于 [失效原因]。
        教训：[...]」
        
        每个退役的策略都有墓志铭，这是你的知识库。
        """
        pass
```

---

## 第 5 步：休眠策略唤醒机制

```python
def check_wakeup_conditions(dormant_strategies):
    """
    每月检查休眠策略是否可以唤醒
    
    唤醒条件（必须全部满足）：
    1. 市场环境切回策略的适宜区
    2. 用休眠期间的数据回测验证通过
    3. 样本外表现 > 回测基准的 60%
    
    如果满足 → Telegram 提醒用户，建议小资金试跑 1 周
    """
    candidates = []
    for strategy in dormant_strategies:
        current_regime = get_current_regime()
        suitable_regimes = STRATEGY_REGIME_FIT[strategy.type]
        
        if suitable_regimes.get(current_regime, 0) >= 0.7:
            # 环境适宜了，验证一下
            dormant_period_data = get_data_since_retirement(strategy)
            test_result = backtest(strategy, dormant_period_data)
            
            if test_result.sharpe > 0.5:
                candidates.append({
                    'strategy': strategy.name,
                    'regime': current_regime,
                    'test_sharpe': test_result.sharpe,
                    'suggestion': '建议小资金（原规模的 25%）试跑 1 周',
                })
    
    return candidates
```

---

## 第 6 步：健康度日报输出

```
🩺 策略健康度 — 2025-07-05
═══════════════════════════════════

趋势跟踪 BTC
├── 综合评分：82/100 🟢
├── 夏普(30d)：1.45 (25/25)
├── 回撤状态：-8.2% / 历史最大 -15.2% (20/25) ⚠️ 接近历史 54%
├── 胜率偏离：42% vs 历史 43% (15/15) ✅
├── 连续亏损：2 天 (15/15) ✅
├── 样本外比：实盘 85% vs 回测 (17/20)
└── 行动：正常运行

均值回归 ETH
├── 综合评分：55/100 🟠
├── 夏普(30d)：0.32 (-10/25) ❌
├── 回撤状态：-18.5% / 历史最大 -20% (10/25) ⚠️ 接近历史极值
├── 胜率偏离：35% vs 历史 58% (-5/15) ❌
├── 连续亏损：7 天 (-5/15) ❌
├── 样本外比：实盘 22% vs 回测 (-5/20) ❌ 过拟合嫌疑
└── 🔴 行动：自动减仓 50%，请尽快分析：
    → 当前市场是强趋势市（环境分类器显示 TRENDING）
    → 均值回归策略在趋势市天然亏损
    → 这不是策略失效，是环境不适
    → 建议：保持减仓状态，等待环境切换回震荡市

参数优化状态：
├── 上次优化：2025-06-28（7 天前）
├── 验证结果：✅ 通过（样本外 78%）
├── 下次优化：2025-07-05
└── 连续失败：0 次

退役评估：
├── 无策略触发退役条件
└── 休眠策略：0 个（无待唤醒）

═══════════════════════════════════
```

---

## 融入冲刺月

| 模块 | 阶段 |
|------|------|
| 每日健康度评分 | Week 3 Day 17（月报内嵌） + Week 4 Day 22（驾驶舱） |
| 灯号自动执行 | Week 2 Day 10（风控系统的一部分） |
| 参数滚动优化 | Week 3 Day 17（月报后的迭代流程） |
| 策略退役/休眠 | Week 4 Day 28（月度总结固化） |

## 必避的坑

| 坑 | 正确做法 |
|------|------|
| 看到红灯就慌 | 先看环境分类器——可能只是环境不适，不是策略失效 |
| 参数一不好就优化 | 每次优化有验证门禁。门禁不通过就保持旧参数 |
| 退役=删除策略 | 保留代码和数据。市场是周期性切换的 |
| 健康度好就不管了 | 绿灯是最危险的麻痹区。保持每周复盘 |
