"""
止损系统独立测试 — Day 9

TDD: 每层止损独立验证，用已知输入 → 已知输出。

运行: python test_stop_loss.py
"""

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from stop_loss import (StopLossManager, StopLossConfig, StopLossLayer,
                       StopLossResult, StopLossRiskAdapter)


# ═══════════════════════════════
# 测试工具
# ═══════════════════════════════

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
# L1: 技术止损
# ═══════════════════════════════

section("L1: 技术止损")

def test_technical_stop_long_triggered():
    """多头价格跌破止损线 → 触发（硬上限 2% 比 ATR 止损更紧）"""
    sl = StopLossManager()
    l1 = sl.check_technical_stop(
        entry_price=65000, current_price=62000, side='long', atr=1200,
    )
    check("价格跌破止损线应触发", l1['triggered'])
    # ATR止损 = 65000-2400=62600, 硬上限 = 65000*0.98=63700, 取更紧的=63700
    check("止损价取硬上限(63700)", abs(l1['stop_price'] - 63700) < 10)

def test_technical_stop_long_not_triggered():
    """多头价格在止损线上方 → 不触发"""
    sl = StopLossManager()
    l1 = sl.check_technical_stop(
        entry_price=65000, current_price=64000, side='long', atr=1200,
    )
    check("价格在止损线上方不触发", not l1['triggered'])

def test_technical_stop_short_triggered():
    """空头价格涨破止损线 → 触发（硬上限 2% 比 ATR 止损更紧）"""
    sl = StopLossManager()
    l1 = sl.check_technical_stop(
        entry_price=65000, current_price=68000, side='short', atr=1200,
    )
    check("空头涨破止损线应触发", l1['triggered'])
    # ATR止损 = 65000+2400=67400, 硬上限 = 65000*1.02=66300, 取更紧的=66300
    check("止损价取硬上限(66300)", abs(l1['stop_price'] - 66300) < 10)

def test_technical_stop_max_loss_cap():
    """硬上限：亏损不能超过 max_loss_pct (2%)"""
    sl = StopLossManager()
    # 如果 ATR 特别大 (如 5000)，ATR止损会很远，硬上限会接管
    l1 = sl.check_technical_stop(
        entry_price=65000, current_price=64000, side='long', atr=5000,
    )
    # 硬上限: 65000 * (1 - 0.02) = 63700
    check("硬上限止损价 = 63700", abs(l1['stop_price'] - 63700) < 10)
    check("硬上限应取更紧的那个", '取硬上限' in l1['note'])

def test_technical_stop_atr_zero():
    """ATR=0 时不应崩溃"""
    sl = StopLossManager()
    l1 = sl.check_technical_stop(
        entry_price=65000, current_price=64000, side='long', atr=0,
    )
    check("ATR=0 应回退到硬上限", l1['stop_price'] > 0)

test_technical_stop_long_triggered()
test_technical_stop_long_not_triggered()
test_technical_stop_short_triggered()
test_technical_stop_max_loss_cap()
test_technical_stop_atr_zero()


# ═══════════════════════════════
# L2: 波动率追踪止损
# ═══════════════════════════════

section("L2: 波动率追踪止损")

def test_trailing_not_activated():
    """盈利不足 1% 时不激活"""
    sl = StopLossManager()
    l2 = sl.calculate_trailing_stop(
        entry_price=65000, current_price=65400,  # 仅 0.6% 盈利
        highest_since_entry=65500, lowest_since_entry=64800,
        side='long', atr=1200, position_id='t1',
    )
    check("盈利 0.6% 不激活", not l2['activated'])
    check("不激活则不触发", not l2['triggered'])

def test_trailing_activated_and_triggered():
    """盈利后回撤到追踪止损线以下 → 触发（止损线已在入场价上方）"""
    sl = StopLossManager()
    # 一路上涨: 最高价推高追踪止损到入场价上方
    # Bar 2: highest=70000, trailing_stop=70000-3600=66400 (>65000 entry!)
    sl.calculate_trailing_stop(
        entry_price=65000, current_price=69000,
        highest_since_entry=70000, lowest_since_entry=64800,
        side='long', atr=1200, position_id='t2',
    )
    # Bar 3: 回撤但仍高于入场价: close=66000 < 66400 止损线
    l2 = sl.calculate_trailing_stop(
        entry_price=65000, current_price=66000,
        highest_since_entry=70000, lowest_since_entry=64800,
        side='long', atr=1200, position_id='t2',
    )
    check("回撤触发追踪止损（止损线在入场价上方）", l2['triggered'])
    check("止损价随最高价上移", l2['stop_price'] >= 66000)

def test_trailing_stop_only_tightens():
    """追踪止损只能收紧不能放松（多头只上移）"""
    sl = StopLossManager()
    # 第一根 bar: 最高 66000，止损 66000 - 3600 = 62400
    l2_1 = sl.calculate_trailing_stop(
        entry_price=65000, current_price=66000,
        highest_since_entry=66000, lowest_since_entry=64800,
        side='long', atr=1200, position_id='t3',
    )
    # 第二根 bar: 最高 65500（回落了），止损不应下移
    l2_2 = sl.calculate_trailing_stop(
        entry_price=65000, current_price=65500,
        highest_since_entry=66000, lowest_since_entry=64800,
        side='long', atr=1200, position_id='t3',
    )
    check("止损价只紧不松", l2_2['stop_price'] >= l2_1['stop_price'])

def test_trailing_short():
    """空头追踪止损"""
    sl = StopLossManager()
    l2 = sl.calculate_trailing_stop(
        entry_price=65000, current_price=64000,
        highest_since_entry=65000, lowest_since_entry=63800,
        side='short', atr=1200, position_id='t4',
    )
    check("空头止损应下移", l2['stop_price'] < 65000 + 3600)

test_trailing_not_activated()
test_trailing_activated_and_triggered()
test_trailing_stop_only_tightens()
test_trailing_short()


# ═══════════════════════════════
# L3: 时间止损
# ═══════════════════════════════

section("L3: 时间止损")

def test_time_stop_soft_trigger():
    """持仓超限 + 盈利不足 → 软触发"""
    sl = StopLossManager()
    l3 = sl.check_time_stop(
        bars_held=25, entry_price=65000, current_price=65100, side='long',
    )
    check("25 根 K 线盈利 0.15% → 软触发", l3['triggered'])
    check("触发类型为软触发", l3.get('trigger_type') == '软触发')

def test_time_stop_hard_trigger():
    """持仓超过强制退出线 → 无论盈亏都触发"""
    sl = StopLossManager()
    l3 = sl.check_time_stop(
        bars_held=40, entry_price=65000, current_price=70000, side='long',
    )
    check("40 根 K 线 → 硬触发 (36=24×1.5)", l3['triggered'])
    check("触发类型为硬触发", l3.get('trigger_type') == '硬触发')

def test_time_stop_not_triggered():
    """持仓时间短 → 不触发"""
    sl = StopLossManager()
    l3 = sl.check_time_stop(
        bars_held=10, entry_price=65000, current_price=65100, side='long',
    )
    check("10 根 K 线 → 不触发", not l3['triggered'])

def test_time_stop_profitable_no_trigger():
    """持仓超限但盈利充足 → 不触发软触发"""
    sl = StopLossManager()
    l3 = sl.check_time_stop(
        bars_held=25, entry_price=65000, current_price=70000, side='long',
    )
    # 盈利 7.7% > 0.5%, 所以软触发不应触发
    check("盈利 7.7% > 0.5% → 软触发不触发", not l3['triggered'])

test_time_stop_soft_trigger()
test_time_stop_hard_trigger()
test_time_stop_not_triggered()
test_time_stop_profitable_no_trigger()


# ═══════════════════════════════
# L4: 策略逻辑止损
# ═══════════════════════════════

section("L4: 策略逻辑止损")

def test_trend_adx_low():
    """趋势策略 ADX < 15 → 触发"""
    sl = StopLossManager()
    l4 = sl.check_strategy_stop('趋势跟踪', {'adx': 10})
    check("ADX=10 < 15 → 趋势策略触发", l4['triggered'])

def test_trend_adx_normal():
    """趋势策略 ADX > 15 → 不触发"""
    sl = StopLossManager()
    l4 = sl.check_strategy_stop('趋势跟踪', {'adx': 25})
    check("ADX=25 → 趋势策略不触发", not l4['triggered'])

def test_momentum_roc_low():
    """动量策略 ROC 太低 → 触发"""
    sl = StopLossManager()
    l4 = sl.check_strategy_stop('动量策略', {'roc': 0.005})
    check("ROC=0.005 < 0.01 → 动量策略触发", l4['triggered'])

def test_momentum_roc_normal():
    """动量策略 ROC 正常 → 不触发"""
    sl = StopLossManager()
    l4 = sl.check_strategy_stop('动量策略', {'roc': 0.05})
    check("ROC=0.05 → 动量策略不触发", not l4['triggered'])

def test_breakout_false():
    """价格回落到通道中线以下 → 假突破 → 触发"""
    sl = StopLossManager()
    l4 = sl.check_strategy_stop('突破策略', {
        'close': 64500, 'donchian_upper': 68000, 'donchian_lower': 62000,
    })
    # 通道中线 = 62000 + (68000-62000)*0.5 = 65000
    # close 64500 < 65000 → 假突破
    check("价格跌破通道中线 → 假突破", l4['triggered'])

def test_funding_arb_low_rate():
    """资金费率太低 → 不值得套利"""
    sl = StopLossManager()
    l4 = sl.check_strategy_stop('资金费率套利', {'funding_rate': 0.00005})
    check("费率 0.005% < 0.01% → 不值得套利", l4['triggered'])

test_trend_adx_low()
test_trend_adx_normal()
test_momentum_roc_low()
test_momentum_roc_normal()
test_breakout_false()
test_funding_arb_low_rate()


# ═══════════════════════════════
# 综合评估
# ═══════════════════════════════

section("综合评估: evaluate()")

def test_evaluate_pass_all():
    """正常行情 → 全部通过"""
    sl = StopLossManager()
    result = sl.evaluate(
        position={'entry_price': 65000, 'side': 'long', 'bars_held': 5},
        bar={'open': 64800, 'high': 65500, 'low': 64700, 'close': 65200,
             'atr': 1200, 'adx': 25},
        strategy_name='趋势跟踪',
    )
    check("正常行情不触发", not result.triggered)
    check("四层都返回了详情", len(result.details) == 4)

def test_evaluate_l4_first():
    """L4 策略逻辑止损优先级最高"""
    sl = StopLossManager()
    result = sl.evaluate(
        position={'entry_price': 65000, 'side': 'long', 'bars_held': 30},
        bar={'open': 64800, 'high': 65100, 'low': 64600, 'close': 64900,
             'atr': 600, 'adx': 10},  # ADX 低 + 持仓时间长
        strategy_name='趋势跟踪',
    )
    check("ADX < 15 → L4 先于 L3 触发", result.layer == StopLossLayer.STRATEGY_LOGIC)

def test_evaluate_l3_before_l2_l1():
    """时间止损优先于技术止损"""
    sl = StopLossManager()
    result = sl.evaluate(
        position={'entry_price': 65000, 'side': 'long', 'bars_held': 40},
        bar={'open': 64800, 'high': 65100, 'low': 64600, 'close': 70000,  # 价格涨了但时间太久
             'atr': 600, 'adx': 30},
        strategy_name='趋势跟踪',
    )
    check("持仓过久 → L3 触发", result.layer == StopLossLayer.TIME)

def test_evaluate_reset_position():
    """reset_position 清理追踪止损状态"""
    sl = StopLossManager()
    sl.calculate_trailing_stop(
        entry_price=65000, current_price=67000,
        highest_since_entry=67500, lowest_since_entry=64800,
        side='long', atr=1200, position_id='r1',
    )
    sl.reset_position('r1')
    # 重置后再查询不应有旧状态
    l2 = sl.calculate_trailing_stop(
        entry_price=65000, current_price=65500,
        highest_since_entry=65500, lowest_since_entry=65000,
        side='long', atr=1200, position_id='r1',
    )
    # 重置后 highest_since_entry 应该重置为当前价
    check("重置后追踪止损重新开始", not l2['activated'])  # 盈利不足 1%

test_evaluate_pass_all()
test_evaluate_l4_first()
test_evaluate_l3_before_l2_l1()
test_evaluate_reset_position()


# ═══════════════════════════════
# 集成适配器
# ═══════════════════════════════

section("集成: StopLossRiskAdapter")

def test_adapter_without_rm():
    """没有 RiskManager 时自动放行"""
    sl = StopLossManager()
    adapter = StopLossRiskAdapter(sl)
    result = adapter.evaluate_with_risk_check(
        position={'entry_price': 65000, 'side': 'long', 'bars_held': 40,
                  'symbol': 'BTC/USDT', 'size_pct': 0.1},
        bar={'open': 64800, 'high': 65100, 'low': 64600, 'close': 64900,
             'atr': 600, 'adx': 12},
        strategy_name='趋势跟踪',
    )
    check("L4 触发 → action=stop_loss", result['action'] == 'stop_loss')

def test_adapter_hold_when_no_trigger():
    """没有触发时返回 hold"""
    sl = StopLossManager()
    adapter = StopLossRiskAdapter(sl)
    result = adapter.evaluate_with_risk_check(
        position={'entry_price': 65000, 'side': 'long', 'bars_held': 5,
                  'symbol': 'BTC/USDT', 'size_pct': 0.1},
        bar={'open': 64800, 'high': 65500, 'low': 64700, 'close': 65200,
             'atr': 1200, 'adx': 25},
        strategy_name='趋势跟踪',
    )
    check("无触发 → action=hold", result['action'] == 'hold')

test_adapter_without_rm()
test_adapter_hold_when_no_trigger()


# ═══════════════════════════════
# 边界条件
# ═══════════════════════════════

section("边界条件")

def test_zero_atr_graceful():
    """ATR=0 不崩溃"""
    sl = StopLossManager()
    result = sl.evaluate(
        position={'entry_price': 65000, 'side': 'long', 'bars_held': 5},
        bar={'open': 65000, 'high': 65000, 'low': 65000, 'close': 65000,
             'atr': 0, 'adx': 20},
        strategy_name='趋势跟踪',
    )
    check("ATR=0 不崩溃", not result.triggered)  # L1/L2 跳过，L3/L4 不触发

def test_missing_bar_fields():
    """bar 缺少可选字段不崩溃"""
    sl = StopLossManager()
    result = sl.evaluate(
        position={'entry_price': 65000, 'side': 'long', 'bars_held': 5},
        bar={'close': 65200},  # 最简 bar
        strategy_name='趋势跟踪',
    )
    check("最简 bar 不崩溃", not result.triggered)

def test_unknown_strategy():
    """未知策略名不崩溃，L4 不触发"""
    sl = StopLossManager()
    result = sl.evaluate(
        position={'entry_price': 65000, 'side': 'long', 'bars_held': 5},
        bar={'open': 64800, 'high': 65500, 'low': 64700, 'close': 65200,
             'atr': 1200, 'adx': 25},
        strategy_name='火星套利策略',
    )
    check("未知策略不崩溃", not result.triggered)

test_zero_atr_graceful()
test_missing_bar_fields()
test_unknown_strategy()


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
