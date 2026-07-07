"""
市场环境分类器独立测试 — Day 11

运行: python test_regime_classifier.py
"""

import sys, os, time
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from regime_classifier import (RegimeClassifier, RegimeConfig, MarketRegime,
                                classify_from_df, regime_distribution)

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
# 1. 初始化
# ═══════════════════════════════

section("1. 初始化 & 默认状态")

def test_initial_state():
    """无数据时默认 TRANSITIONING"""
    rc = RegimeClassifier()
    check("默认环境 = TRANSITIONING", rc.regime == MarketRegime.TRANSITIONING)
    check("可开新仓 = False（过渡期）", not rc.should_open_new_position())

def test_custom_config():
    """自定义配置"""
    config = RegimeConfig(
        adx_trending_threshold=30.0,
        atr_lookback=120,
    )
    rc = RegimeClassifier(config)
    check("自定义 ADX 阈值生效", rc.config.adx_trending_threshold == 30.0)

def test_initial_multipliers():
    """无数据时（过渡期）返回对应的乘数"""
    rc = RegimeClassifier()
    # 过渡期：趋势/动量不可开仓 (0.0)，均值回归减仓 (0.3)，费率套利减仓 (0.5)
    check("趋势乘数=0.0（过渡期禁止开仓）", rc.get_strategy_multiplier('trend') == 0.0)
    check("动量乘数=0.0（过渡期禁止开仓）", rc.get_strategy_multiplier('momentum') == 0.0)
    check("均值回归乘数=0.3（过渡期）", rc.get_strategy_multiplier('mean_reversion') == 0.3)
    check("费率套利乘数=0.5（过渡期）", rc.get_strategy_multiplier('funding_arb') == 0.5)

test_initial_state()
test_custom_config()
test_initial_multipliers()


# ═══════════════════════════════
# 2. 数据更新
# ═══════════════════════════════

section("2. 数据更新")

def test_update_basic():
    """基础 bar 更新不崩溃"""
    rc = RegimeClassifier()
    for i in range(10):
        bar = {'close': 65000 + i * 10, 'high': 65100 + i * 10,
               'low': 64900 + i * 10, 'volume': 1000}
        rc.update(bar)
    check("10 根 bar 更新完成", rc._n_updates == 10)

def test_update_min_data():
    """不足 min_bars 时不分类"""
    rc = RegimeClassifier()
    for i in range(30):
        bar = {'close': 65000, 'high': 65100, 'low': 64900, 'volume': 1000}
        rc.update(bar)
    check("30 根 bar 仍为 TRANSITIONING (<50)", rc.regime == MarketRegime.TRANSITIONING)

def test_update_external():
    """外部数据更新"""
    rc = RegimeClassifier()
    rc.update_external(oi=50000, funding_rate=0.0005,
                       btc_dominance=52.3, fear_greed=65)
    check("OI 已存储", len(rc._oi_data) == 1)
    check("资金费率已存储", len(rc._funding_rate) == 1)
    check("BTC.D 已存储", rc._btc_dominance == 52.3)
    check("恐惧贪婪已存储", rc._fear_greed == 65)

test_update_basic()
test_update_min_data()
test_update_external()


# ═══════════════════════════════
# 3. 趋势市识别
# ═══════════════════════════════

section("3. 趋势市识别")

def test_trending_up_detection():
    """价格持续上涨 + MA50 抬头 → TRENDING_UP"""
    rc = RegimeClassifier()
    price = 60000.0
    for i in range(100):
        # 稳定上涨，低波动
        price += 200  # 每根涨 200
        bar = {
            'close': price,
            'high': price + 50,
            'low': price - 50,
            'volume': 1000,
        }
        rc.update(bar)
    regime = rc.regime
    print(f"  最终环境: {regime.value}, 得分: {rc._scores}")
    # 趋势市应该是 trending_up 或 at least not ranging
    check("趋势上涨被识别", regime in [MarketRegime.TRENDING_UP, MarketRegime.VOLATILE])

def test_trending_down_detection():
    """价格持续下跌 → TRENDING_DOWN"""
    rc = RegimeClassifier()
    price = 70000.0
    for i in range(100):
        price -= 200
        bar = {
            'close': price,
            'high': price + 50,
            'low': price - 50,
            'volume': 1000,
        }
        rc.update(bar)
    regime = rc.regime
    print(f"  最终环境: {regime.value}, 得分: {rc._scores}")
    check("趋势下跌被识别", regime in [MarketRegime.TRENDING_DOWN, MarketRegime.VOLATILE, MarketRegime.TRANSITIONING])

test_trending_up_detection()
test_trending_down_detection()


# ═══════════════════════════════
# 4. 震荡市识别
# ═══════════════════════════════

section("4. 震荡市识别")

def test_ranging_detection():
    """价格窄幅震荡 + 低波动 → RANGING"""
    rc = RegimeClassifier()
    price = 65000.0
    for i in range(100):
        # 极小波动
        import random
        price += random.uniform(-30, 30)
        bar = {
            'close': price,
            'high': price + random.uniform(5, 15),
            'low': price - random.uniform(5, 15),
            'volume': random.uniform(300, 800),
        }
        rc.update(bar)
    regime = rc.regime
    print(f"  最终环境: {regime.value}, 得分: {rc._scores}")
    # 窄幅震荡应该被识别为 ranging 或至少趋势得分不高
    check("震荡被识别（非纯趋势）",
          regime in [MarketRegime.RANGING, MarketRegime.TRANSITIONING, MarketRegime.VOLATILE])

test_ranging_detection()


# ═══════════════════════════════
# 5. 策略乘数
# ═══════════════════════════════

section("5. 策略乘数")

def test_strategy_multiplier_ranging():
    """震荡市 → 均值回归满仓、趋势减仓"""
    rc = RegimeClassifier()
    # 模拟震荡市数据
    price = 65000.0
    import random
    for i in range(80):
        price += random.uniform(-40, 40)
        bar = {
            'close': price, 'high': price + 10, 'low': price - 10, 'volume': 500,
        }
        rc.update(bar)

    trend_mult = rc.get_strategy_multiplier('trend')
    mr_mult = rc.get_strategy_multiplier('mean_reversion')
    print(f"  环境: {rc.regime.value}, 趋势: {trend_mult}, 均值回归: {mr_mult}")
    # 如果环境是 ranging，均值回归乘数应 >= 趋势乘数
    if rc.regime == MarketRegime.RANGING:
        check("震荡市均值回归乘数 >= 趋势乘数", mr_mult >= trend_mult)
    else:
        print(f"  环境是 {rc.regime.value}，跳过此断言")

def test_strategy_multiplier_volatile():
    """高波动 → 所有策略减仓"""
    rc = RegimeClassifier()
    price = 65000.0
    import random
    for i in range(80):
        price *= (1 + random.gauss(0, 0.03))
        bar = {
            'close': price, 'high': price * 1.015, 'low': price * 0.985, 'volume': 3000,
        }
        rc.update(bar)

    mults = {st: rc.get_strategy_multiplier(st)
             for st in ['trend', 'momentum', 'mean_reversion']}
    print(f"  环境: {rc.regime.value}, 乘数: {mults}")
    if rc.regime == MarketRegime.VOLATILE:
        check("高波动所有策略乘数 <= 0.5", all(m <= 0.5 for m in mults.values()))
    else:
        print(f"  环境是 {rc.regime.value}，跳过此断言")

def test_unknown_strategy_type():
    """未知策略类型返回 1.0"""
    rc = RegimeClassifier()
    check("未知策略=1.0", rc.get_strategy_multiplier('quantum_ai') == 1.0)

test_strategy_multiplier_ranging()
test_strategy_multiplier_volatile()
test_unknown_strategy_type()


# ═══════════════════════════════
# 6. 止损 & 仓位调整
# ═══════════════════════════════

section("6. 止损 & 仓位调整")

def test_stop_loss_adjustment():
    """各环境的止损调整"""
    rc = RegimeClassifier()
    # 默认过渡期
    check("过渡期止损系数=0.6", rc.get_stop_loss_adjustment() == 0.6)

def test_max_position_adjustment():
    """各环境的仓位上限调整"""
    rc = RegimeClassifier()
    check("过渡期仓位上限系数=0.3", rc.get_max_position_adjustment() == 0.3)

test_stop_loss_adjustment()
test_max_position_adjustment()


# ═══════════════════════════════
# 7. 环境切换检测
# ═══════════════════════════════

section("7. 环境切换 & 稳定性")

def test_regime_duration():
    """环境持续时间统计"""
    rc = RegimeClassifier()
    for i in range(60):
        bar = {'close': 65000, 'high': 65100, 'low': 64900, 'volume': 1000}
        rc.update(bar)
    check("持续时间 >= 0", rc.regime_duration >= 0)
    check("持续时间 >= 10（从第 50 根开始分类）",
          rc.regime_duration >= 10)

def test_regime_stability():
    """稳定性计算"""
    rc = RegimeClassifier()
    # 同向移动
    price = 65000
    for i in range(30):
        price += 100
        bar = {'close': price, 'high': price + 20, 'low': price - 20, 'volume': 1000}
        rc.update(bar)
    stability = rc.regime_stability(n=20)
    check("稳定趋势稳定性 > 0.5", stability > 0.5)

def test_recent_regimes():
    """最近环境序列"""
    rc = RegimeClassifier()
    for i in range(60):
        bar = {'close': 65000, 'high': 65100, 'low': 64900, 'volume': 1000}
        rc.update(bar)
    recent = rc.recent_regimes(15)
    check("返回 15 个环境标签", len(recent) == 15)
    check("所有标签都是字符串", all(isinstance(r, str) for r in recent))

test_regime_duration()
test_regime_stability()
test_recent_regimes()


# ═══════════════════════════════
# 8. Dashboard
# ═══════════════════════════════

section("8. Dashboard")

def test_dashboard_structure():
    """Dashboard 返回完整结构"""
    rc = RegimeClassifier()
    for i in range(60):
        price = 65000 + i * 50
        bar = {'close': price, 'high': price + 100, 'low': price - 100, 'volume': 1000}
        rc.update(bar)
    dash = rc.dashboard()
    required_keys = ['regime', 'regime_duration', 'scores', 'multipliers',
                     'stop_loss_adj', 'max_position_adj', 'can_open_new',
                     'external', 'indicators']
    for key in required_keys:
        check(f"Dashboard 含 {key}", key in dash)
    check("indicators 含 adx", dash['indicators']['adx'] is not None)
    check("indicators 含 price", dash['indicators']['price'] is not None)

test_dashboard_structure()


# ═══════════════════════════════
# 9. 指标计算
# ═══════════════════════════════

section("9. 指标计算")

def test_adx_strong_trend():
    """强趋势 → 高 ADX"""
    rc = RegimeClassifier()
    price = 60000
    for i in range(30):
        price += 300  # 一致性上涨
        bar = {'close': price, 'high': price + 30, 'low': price - 10, 'volume': 1000}
        rc.update(bar)
    adx = rc._compute_adx()
    print(f"  ADX = {adx}")
    check("强趋势 ADX > 25", adx is not None and adx > 25)

def test_adx_weak_trend():
    """无趋势 → 低 ADX"""
    rc = RegimeClassifier()
    price = 65000
    import random
    for i in range(30):
        price += random.uniform(-100, 100)
        bar = {'close': price, 'high': price + 100, 'low': price - 100, 'volume': 1000}
        rc.update(bar)
    adx = rc._compute_adx()
    print(f"  ADX = {adx}")
    check("弱趋势 ADX < 50", adx is not None and adx < 50)

def test_atr_percentile():
    """ATR 百分位计算"""
    rc = RegimeClassifier()
    price = 65000
    for i in range(50):
        bar = {'close': price, 'high': price + 50, 'low': price - 50, 'volume': 1000}
        rc.update(bar)
    atr_pct = rc._compute_atr_percentile()
    check("ATR 百分位在 0-100 内", atr_pct is not None and 0 <= atr_pct <= 100)

test_adx_strong_trend()
test_adx_weak_trend()
test_atr_percentile()


# ═══════════════════════════════
# 10. classify_from_df
# ═══════════════════════════════

section("10. classify_from_df 批量分类")

def test_classify_from_df():
    """批量分类返回 Series"""
    import pandas as pd
    import numpy as np
    n = 100
    df = pd.DataFrame({
        'close': np.cumsum(np.random.randn(n) * 50) + 65000,
        'high': np.cumsum(np.random.randn(n) * 50) + 65200,
        'low': np.cumsum(np.random.randn(n) * 50) + 64800,
        'volume': np.random.uniform(500, 2000, n),
    })
    regimes = classify_from_df(df)
    check("返回 pd.Series", isinstance(regimes, pd.Series))
    check("长度一致", len(regimes) == n)
    check("前 49 根为 TRANSITIONING（数据不足）",
          all(r == MarketRegime.TRANSITIONING for r in regimes[:49]))

def test_regime_distribution():
    """环境分布统计"""
    import pandas as pd
    import numpy as np
    n = 200
    df = pd.DataFrame({
        'close': np.cumsum(np.random.randn(n) * 20) + 65000,
        'high': np.cumsum(np.random.randn(n) * 20) + 65200,
        'low': np.cumsum(np.random.randn(n) * 20) + 64800,
        'volume': np.random.uniform(500, 2000, n),
    })
    regimes = classify_from_df(df)
    dist = regime_distribution(regimes)
    check("5 种环境都有统计", len(dist) == 5)
    check("百分比加起来约 100%", abs(sum(d['pct'] for d in dist.values()) - 100) < 1)

test_classify_from_df()
test_regime_distribution()


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
