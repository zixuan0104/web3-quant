"""
多策略编排器独立测试 — Day 11

运行: python test_strategy_orchestrator.py
"""

import sys, os, time
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from strategy_orchestrator import (StrategyOrchestrator, OrchestratorConfig,
                                    StrategySlot, SignalAction)

PASS = "[PASS]"
FAIL = "[FAIL]"
passed = 0
failed = 0

def check(name, condition, detail=""):
    global passed, failed
    if condition:
        passed += 1
        print(f"  {PASS} {name}")
    else:
        failed += 1
        print(f"  {FAIL} {name}  -- {detail}")

def section(title):
    print(f"\n{'='*60}")
    print(f"  {title}")
    print(f"{'='*60}")


# ═══════════════════════════════
# 辅助函数：创建 mock 策略
# ═══════════════════════════════

from unittest.mock import MagicMock
import random

def make_mock_strategy(name="test", has_position=False, side=None,
                       entry_price=65000, trade_count=25):
    """创建 mock 策略实例"""
    strat = MagicMock()
    strat.name = name
    strat.position = MagicMock() if has_position else None
    if has_position:
        strat.position.side = side
        strat.position.entry_price = entry_price
    strat.trade_log = [
        {'net_return_pct': random.uniform(-2, 3)} for _ in range(trade_count)
    ]
    strat.on_bar.return_value = None
    return strat


def make_mock_position_sizer(base_size_pct=0.05):
    """创建 mock 仓位计算器"""
    ps = MagicMock()
    ps.calculate.return_value = {
        'size_pct': base_size_pct,
        'size_value': base_size_pct * 10000,
        'method': 'kelly_half',
    }
    return ps


# ═══════════════════════════════
# 1. 初始化 & 策略管理
# ═══════════════════════════════

section("1. 初始化 & 策略管理")

def test_initial_state():
    """空编排器初始状态"""
    orch = StrategyOrchestrator()
    check("活跃策略=0", len(orch.active_strategies) == 0)
    check("信号数=0", orch._total_signals == 0)
    check("自然对冲=False", not orch.is_naturally_hedged())

def test_add_strategy():
    """添加策略"""
    orch = StrategyOrchestrator()
    strat = make_mock_strategy('trend_ema')
    ps = make_mock_position_sizer()
    orch.add_strategy('trend_ema', 'trend', strat, ps)
    check("策略已注册", 'trend_ema' in orch._slots)
    check("策略类型=trend", orch._slots['trend_ema'].strategy_type == 'trend')
    check("活跃策略=1", len(orch.active_strategies) == 1)

def test_disable_enable_strategy():
    """启用/禁用策略"""
    orch = StrategyOrchestrator()
    strat = make_mock_strategy('test')
    ps = make_mock_position_sizer()
    orch.add_strategy('test', 'trend', strat, ps)
    check("初始启用", orch._slots['test'].enabled)
    orch.disable_strategy('test')
    check("禁用后 enabled=False", not orch._slots['test'].enabled)
    check("活跃策略=0", len(orch.active_strategies) == 0)
    orch.enable_strategy('test')
    check("重新启用", orch._slots['test'].enabled)

def test_remove_strategy():
    """移除策略"""
    orch = StrategyOrchestrator()
    strat = make_mock_strategy('test')
    ps = make_mock_position_sizer()
    orch.add_strategy('test', 'trend', strat, ps)
    orch.remove_strategy('test')
    check("策略已移除", 'test' not in orch._slots)

test_initial_state()
test_add_strategy()
test_disable_enable_strategy()
test_remove_strategy()


# ═══════════════════════════════
# 2. on_bar 事件处理
# ═══════════════════════════════

section("2. on_bar 事件处理")

def test_on_bar_no_signals():
    """所有策略无信号 → 空返回"""
    orch = StrategyOrchestrator()
    for name, stype in [('trend', 'trend'), ('momentum', 'momentum')]:
        strat = make_mock_strategy(name, trade_count=30)
        ps = make_mock_position_sizer()
        orch.add_strategy(name, stype, strat, ps)

    bar = {'close': 65000, 'high': 65100, 'low': 64900, 'volume': 1000}
    result = orch.on_bar(bar, idx=0)
    check("无信号列表为空", len(result['signals']) == 0)
    check("无拒绝", len(result['rejected']) == 0)
    check("返回 bar_idx", result['bar_idx'] == 0)

def test_on_bar_with_signal():
    """有策略发出入场信号"""
    orch = StrategyOrchestrator()
    strat = make_mock_strategy('trend_ema', trade_count=30)
    strat.on_bar.return_value = {
        'action': 'entry', 'side': 'long', 'price': 65000,
        'stop_loss': 63000, 'take_profit': 68000,
    }
    ps = make_mock_position_sizer(base_size_pct=0.08)
    orch.add_strategy('trend_ema', 'trend', strat, ps)

    bar = {'close': 65000, 'high': 65100, 'low': 64900, 'volume': 1000}
    result = orch.on_bar(bar, idx=0)
    check("有 1 个信号", len(result['signals']) == 1)
    sig = result['signals'][0]
    check("策略名正确", sig['strategy_name'] == 'trend_ema')
    check("方向正确", sig['side'] == 'long')
    check("含基础仓位", 'base_size_pct' in sig)
    check("含调整后仓位", 'adjusted_size_pct' in sig)
    check("含环境乘数", 'regime_multiplier' in sig)

def test_on_bar_disabled_strategy():
    """禁用策略不产生信号"""
    orch = StrategyOrchestrator()
    strat = make_mock_strategy('disabled_strat')
    strat.on_bar.return_value = {
        'action': 'entry', 'side': 'long', 'price': 65000,
    }
    ps = make_mock_position_sizer()
    orch.add_strategy('disabled_strat', 'trend', strat, ps)
    orch.disable_strategy('disabled_strat')

    bar = {'close': 65000, 'high': 65100, 'low': 64900, 'volume': 1000}
    result = orch.on_bar(bar, idx=0)
    check("禁用策略不产生信号", len(result['signals']) == 0)

def test_signal_counter():
    """信号计数"""
    orch = StrategyOrchestrator()
    strat = make_mock_strategy('test', trade_count=30)
    strat.on_bar.return_value = {
        'action': 'entry', 'side': 'long', 'price': 65000,
    }
    ps = make_mock_position_sizer()
    orch.add_strategy('test', 'trend', strat, ps)

    bar = {'close': 65000, 'high': 65100, 'low': 64900, 'volume': 1000}
    for i in range(3):
        orch.on_bar(bar, idx=i)
    check("累计信号数 = 3", orch._total_signals == 3)

test_on_bar_no_signals()
test_on_bar_with_signal()
test_on_bar_disabled_strategy()
test_signal_counter()


# ═══════════════════════════════
# 3. 相关性计算
# ═══════════════════════════════

section("3. 相关性计算")

def test_pearson_perfect_positive():
    """完全正相关"""
    x = [1.0, 2.0, 3.0, 4.0, 5.0]
    y = [1.0, 2.0, 3.0, 4.0, 5.0]
    corr = StrategyOrchestrator._pearson(x, y)
    check("完全正相关 ≈ 1.0", abs(corr - 1.0) < 0.001)

def test_pearson_perfect_negative():
    """完全负相关"""
    x = [1.0, 2.0, 3.0, 4.0, 5.0]
    y = [5.0, 4.0, 3.0, 2.0, 1.0]
    corr = StrategyOrchestrator._pearson(x, y)
    check("完全负相关 ≈ -1.0", abs(corr + 1.0) < 0.001)

def test_pearson_uncorrelated():
    """不相关"""
    import random
    random.seed(42)
    x = [random.gauss(0, 1) for _ in range(50)]
    random.seed(99)
    y = [random.gauss(0, 1) for _ in range(50)]
    corr = StrategyOrchestrator._pearson(x, y)
    print(f"  corr = {corr:.4f}")
    check("不相关 |corr| < 0.5", abs(corr) < 0.5)

def test_pearson_short_input():
    """输入 < 3 返回 0"""
    corr = StrategyOrchestrator._pearson([1.0], [1.0])
    check("输入不足返回 0", corr == 0.0)

def test_update_correlations_no_trades():
    """无交易数据时不崩溃"""
    orch = StrategyOrchestrator()
    strat = make_mock_strategy('test', trade_count=0)
    strat.trade_log = []
    ps = make_mock_position_sizer()
    orch.add_strategy('test', 'trend', strat, ps)
    orch._update_correlations()
    check("无交易时相关性更新不崩溃", True)

def test_high_correlation_discount():
    """高相关性 → 降权"""
    orch = StrategyOrchestrator()
    config = OrchestratorConfig(
        correlation_lookback=10,
        correlation_warning_threshold=0.5,
        correlation_discount_factor=0.7,
    )
    orch.config = config

    # 两个策略收益率完全一样 → 高相关
    s1 = make_mock_strategy('s1', trade_count=15)
    s2 = make_mock_strategy('s2', trade_count=15)
    same_returns = [1.0, 1.5, 0.8, 1.2, -0.5, 2.0, 0.7, 1.1, 0.9, 1.3,
                    1.4, 0.6, 1.8, 0.4, 1.0]
    s1.trade_log = [{'net_return_pct': r} for r in same_returns]
    s2.trade_log = [{'net_return_pct': r} for r in same_returns]

    ps = make_mock_position_sizer()
    orch.add_strategy('s1', 'trend', s1, ps)
    orch.add_strategy('s2', 'momentum', s2, ps)

    orch._update_correlations()
    check("高相关导致降权",
          orch._slots['s1'].correlation_weight < 1.0 or
          orch._slots['s2'].correlation_weight < 1.0)

test_pearson_perfect_positive()
test_pearson_perfect_negative()
test_pearson_uncorrelated()
test_pearson_short_input()
test_update_correlations_no_trades()
test_high_correlation_discount()


# ═══════════════════════════════
# 4. 敞口计算
# ═══════════════════════════════

section("4. 敞口计算")

def test_exposure_empty():
    """无策略 → 零敞口"""
    orch = StrategyOrchestrator()
    exp = orch._calculate_exposures([])
    check("多头=0", exp['long_pct'] == 0)
    check("空头=0", exp['short_pct'] == 0)
    check("净敞口=0", exp['net_pct'] == 0)
    check("敞口OK", exp['exposure_ok'])

def test_exposure_with_signals():
    """有信号时敞口计算"""
    orch = StrategyOrchestrator()
    signals = [
        {'side': 'long', 'adjusted_size_pct': 0.08, 'risk_rejected': False},
        {'side': 'long', 'adjusted_size_pct': 0.05, 'risk_rejected': False},
        {'side': 'short', 'adjusted_size_pct': 0.03, 'risk_rejected': False},
    ]
    exp = orch._calculate_exposures(signals)
    check("多头=0.13", abs(exp['long_pct'] - 0.13) < 0.01)
    check("空头=0.03", abs(exp['short_pct'] - 0.03) < 0.01)
    check("净敞口=0.10", abs(exp['net_pct'] - 0.10) < 0.01)
    check("总敞口=0.16", abs(exp['gross_pct'] - 0.16) < 0.01)
    check("对冲=True (多空都有)", exp['is_hedged'])

def test_exposure_rejected_ignored():
    """被拒绝的信号不计入敞口"""
    orch = StrategyOrchestrator()
    signals = [
        {'side': 'long', 'adjusted_size_pct': 0.10, 'risk_rejected': False},
        {'side': 'long', 'adjusted_size_pct': 0.20, 'risk_rejected': True},  # 拒绝
    ]
    exp = orch._calculate_exposures(signals)
    check("被拒绝不计入", abs(exp['long_pct'] - 0.10) < 0.01)

def test_exposure_limit_check():
    """敞口超限检测"""
    orch = StrategyOrchestrator()
    signals = [
        {'side': 'long', 'adjusted_size_pct': 0.50, 'risk_rejected': False},
        {'side': 'long', 'adjusted_size_pct': 0.30, 'risk_rejected': False},
    ]
    exp = orch._calculate_exposures(signals)
    check("净敞口超限", not exp['exposure_ok'])

test_exposure_empty()
test_exposure_with_signals()
test_exposure_rejected_ignored()
test_exposure_limit_check()


# ═══════════════════════════════
# 5. 集中度检查
# ═══════════════════════════════

section("5. 集中度检查")

def test_concentration_ok():
    """同一方向策略数未超限"""
    orch = StrategyOrchestrator()
    sig = {'side': 'long'}
    existing = [{'side': 'long'}]  # 1 个同向
    check("2 个同向 ≤ 上限", orch._check_concentration(sig, existing))

def test_concentration_exceeded():
    """同一方向策略数超限"""
    orch = StrategyOrchestrator()
    sig = {'side': 'long'}
    existing = [{'side': 'long'}, {'side': 'long'}]  # 2 个同向

    # 注册已有的 long 持仓
    s = make_mock_strategy('s_long', has_position=True, side='long')
    ps = make_mock_position_sizer()
    orch.add_strategy('s_long', 'trend', s, ps)
    # 已有 1 个持仓 + 2 个 existing 信号 = 3 → 达到上限 2
    check("3 个同向 > 上限", not orch._check_concentration(sig, existing))

test_concentration_ok()
test_concentration_exceeded()


# ═══════════════════════════════
# 6. 对冲检测
# ═══════════════════════════════

section("6. 对冲检测")

def test_is_hedged_true():
    """多空同时持仓"""
    orch = StrategyOrchestrator()
    s_long = make_mock_strategy('long', has_position=True, side='long')
    s_short = make_mock_strategy('short', has_position=True, side='short')
    ps = make_mock_position_sizer()
    orch.add_strategy('long', 'trend', s_long, ps)
    orch.add_strategy('short', 'momentum', s_short, ps)
    check("自然对冲=True", orch.is_naturally_hedged())

def test_is_hedged_false():
    """只有单边持仓"""
    orch = StrategyOrchestrator()
    s1 = make_mock_strategy('s1', has_position=True, side='long')
    s2 = make_mock_strategy('s2', has_position=False)
    ps = make_mock_position_sizer()
    orch.add_strategy('s1', 'trend', s1, ps)
    orch.add_strategy('s2', 'momentum', s2, ps)
    check("自然对冲=False", not orch.is_naturally_hedged())

def test_is_hedged_no_positions():
    """全部空仓"""
    orch = StrategyOrchestrator()
    s1 = make_mock_strategy('s1', has_position=False)
    s2 = make_mock_strategy('s2', has_position=False)
    ps = make_mock_position_sizer()
    orch.add_strategy('s1', 'trend', s1, ps)
    orch.add_strategy('s2', 'momentum', s2, ps)
    check("全部空仓 → 不对冲", not orch.is_naturally_hedged())

test_is_hedged_true()
test_is_hedged_false()
test_is_hedged_no_positions()


# ═══════════════════════════════
# 7. Dashboard 生成
# ═══════════════════════════════

section("7. Dashboard 生成")

def test_dashboard_empty():
    """空编排器 Dashboard"""
    orch = StrategyOrchestrator()
    dash = orch.dashboard()
    check("含 strategies", 'strategies' in dash)
    check("strategies 为空", len(dash['strategies']) == 0)
    check("含 exposures", 'exposures' in dash)
    check("含 correlations", 'correlations' in dash)
    check("含 high_correlation_pairs", 'high_correlation_pairs' in dash)
    check("总信号=0", dash['total_signals'] == 0)
    check("拒绝=0", dash['rejected_signals'] == 0)

def test_dashboard_with_strategies():
    """有策略的 Dashboard"""
    orch = StrategyOrchestrator()
    s1 = make_mock_strategy('trend_ema', has_position=True, side='long')
    s2 = make_mock_strategy('momentum', has_position=False)
    ps = make_mock_position_sizer()
    orch.add_strategy('trend_ema', 'trend', s1, ps)
    orch.add_strategy('momentum', 'momentum', s2, ps)

    dash = orch.dashboard()
    check("2 个策略", len(dash['strategies']) == 2)
    s = dash['strategies'][0]
    check("策略含 name", 'name' in s)
    check("策略含 type", 'type' in s)
    check("策略含 enabled", 'enabled' in s)
    check("策略含 has_position", 'has_position' in s)
    check("策略含 effective_multiplier", 'effective_multiplier' in s)
    check("活跃多头=1", dash['active_long_strategies'] == 1)
    check("活跃空头=0", dash['active_short_strategies'] == 0)

def test_dashboard_text():
    """文字版 Dashboard 生成"""
    orch = StrategyOrchestrator()
    s1 = make_mock_strategy('trend_ema', has_position=True, side='long')
    s2 = make_mock_strategy('momentum', has_position=False)
    ps = make_mock_position_sizer()
    orch.add_strategy('trend_ema', 'trend', s1, ps)
    orch.add_strategy('momentum', 'momentum', s2, ps)

    text = orch.dashboard_text(width=60)
    check("文字版含策略名", 'trend_ema' in text)
    check("文字版含 momentum", 'momentum' in text)
    check("文字版含标题", '多策略 Dashboard' in text)

test_dashboard_empty()
test_dashboard_with_strategies()
test_dashboard_text()


# ═══════════════════════════════
# 8. 调仓建议
# ═══════════════════════════════

section("8. 调仓建议")

def test_suggested_rebalance_hold():
    """正常情况 → hold"""
    orch = StrategyOrchestrator()
    ps = make_mock_position_sizer()
    orch.add_strategy('s1', 'trend', make_mock_strategy('s1'), ps)
    rec = orch.suggested_rebalance()
    check("正常时 action=hold", rec['action'] == 'hold')

def test_suggested_rebalance_structure():
    """建议结构完整"""
    orch = StrategyOrchestrator()
    rec = orch.suggested_rebalance()
    check("含 action", 'action' in rec)
    check("含 reason", 'reason' in rec)
    check("含 suggested_net_exposure", 'suggested_net_exposure' in rec)

test_suggested_rebalance_hold()
test_suggested_rebalance_structure()


# ═══════════════════════════════
# 9. 自定义配置
# ═══════════════════════════════

section("9. 自定义配置")

def test_custom_orchestrator_config():
    """自定义编排器配置"""
    config = OrchestratorConfig(
        max_net_exposure_pct=0.40,
        max_gross_exposure_pct=1.00,
        correlation_warning_threshold=0.50,
        correlation_discount_factor=0.60,
        max_strategies_same_direction=3,
    )
    orch = StrategyOrchestrator(config=config)
    check("自定义净敞口上限", orch.config.max_net_exposure_pct == 0.40)
    check("自定义总敞口上限", orch.config.max_gross_exposure_pct == 1.00)
    check("自定义相关性阈值", orch.config.correlation_warning_threshold == 0.50)
    check("自定义同向策略上限", orch.config.max_strategies_same_direction == 3)

test_custom_orchestrator_config()


# ═══════════════════════════════
# 10. 集成场景
# ═══════════════════════════════

section("10. 集成场景")

def test_multi_bar_simulation():
    """多根 K 线模拟运行"""
    orch = StrategyOrchestrator()

    # 3 个策略：趋势、动量、突破
    trend_s = make_mock_strategy('trend', has_position=False, trade_count=30)
    mom_s = make_mock_strategy('momentum', has_position=False, trade_count=30)
    breakout_s = make_mock_strategy('breakout', has_position=False, trade_count=30)

    # 第 5 根 bar 趋势策略发出做多信号
    call_count = [0]
    def trend_on_bar(bar, idx):
        call_count[0] += 1
        if call_count[0] == 5:
            return {'action': 'entry', 'side': 'long', 'price': bar['close'],
                    'stop_loss': bar['close'] * 0.95, 'take_profit': bar['close'] * 1.10}
        return None
    trend_s.on_bar = trend_on_bar

    ps = make_mock_position_sizer(base_size_pct=0.06)

    orch.add_strategy('trend_ema', 'trend', trend_s, ps)
    orch.add_strategy('momentum', 'momentum', mom_s, ps)
    orch.add_strategy('breakout', 'breakout', breakout_s, ps)

    price = 65000
    for i in range(10):
        price += random.uniform(-100, 150)
        bar = {'close': price, 'high': price + 100, 'low': price - 100, 'volume': 1000}
        result = orch.on_bar(bar, idx=i)

    # 验证最后一根 bar 的结果
    check("10 根 bar 后 bar_count=10", orch._bar_count == 10)
    dash = orch.dashboard()
    check("总信号 >= 1", dash['total_signals'] >= 1)
    check("Dashboard 含 3 个策略", len(dash['strategies']) == 3)

def test_risk_manager_integration():
    """风控集成 — 信号被拒"""
    from risk_manager import RiskManager, RiskCheckResult, RiskAction

    orch = StrategyOrchestrator()
    rm = MagicMock()
    rm.pre_trade_check.return_value = MagicMock(
        passed=False, reason="日内熔断触发",
        suggested_size_pct=0.0,
    )

    orch.risk_manager = rm

    strat = make_mock_strategy('test', trade_count=30)
    strat.on_bar.return_value = {
        'action': 'entry', 'side': 'long', 'price': 65000,
    }
    ps = make_mock_position_sizer(base_size_pct=0.10)
    orch.add_strategy('test', 'trend', strat, ps)

    bar = {'close': 65000, 'high': 65100, 'low': 64900, 'volume': 1000}
    result = orch.on_bar(bar, idx=0)
    check("信号被拒", len(result['signals']) == 0)
    check("拒绝列表含 1 条", len(result['rejected']) == 1)
    check("拒绝原因=日内熔断", result['rejected'][0]['risk_reason'] == '日内熔断触发')
    check("风控拒绝计数+1", orch._rejected_signals == 1)

test_multi_bar_simulation()
test_risk_manager_integration()


# ═══════════════════════════════
# 结果
# ═══════════════════════════════

print(f"\n{'='*60}")
print(f"  测试结果: {passed} 通过, {failed} 失败 (共 {passed + failed})")
print(f"{'='*60}")

if failed > 0:
    print(f"\n  {failed} 个测试失败!")
    sys.exit(1)
else:
    print(f"\n  全部 {passed} 个测试通过!")
