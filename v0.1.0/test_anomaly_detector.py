"""
异常检测独立测试 — Day 10

运行: python test_anomaly_detector.py
"""

import sys, os, time
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from anomaly_detector import (AnomalyDetector, AnomalyConfig, AnomalyType,
                              AnomalySeverity, AnomalyResult, APIRetryHandler,
                              SystemHealth)

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
# 1. 价格异常
# ═══════════════════════════════

section("1. 价格断崖检测")

def test_price_spike_critical():
    """5 分钟 15% 跌幅 → CRITICAL"""
    ad = AnomalyDetector()
    now = time.time()
    prices = [(now - 240, 65000), (now - 180, 64000), (now - 120, 61000),
              (now - 60, 57000), (now, 55250)]
    result = None
    for ts, p in prices:
        result = ad.check_price_spike('BTC/USDT', p, ts)
    check("5 分钟跌 15% 触发", result.triggered)
    check("严重级别=CRITICAL", result.severity == AnomalySeverity.CRITICAL)

def test_price_spike_warning():
    """1 分钟 6% 跌幅 → WARNING"""
    ad = AnomalyDetector()
    now = time.time()
    prices = [(now - 40, 65000), (now - 20, 63000), (now, 61100)]
    result = None
    for ts, p in prices:
        result = ad.check_price_spike('BTC/USDT', p, ts)
    check("1 分钟跌 6% 触发 WARNING", result.triggered)
    check("严重级别=WARNING", result.severity == AnomalySeverity.WARNING)

def test_price_normal():
    """正常波动 → 不触发"""
    ad = AnomalyDetector()
    now = time.time()
    prices = [(now - 200, 65000), (now - 100, 64800), (now, 65200)]
    result = None
    for ts, p in prices:
        result = ad.check_price_spike('BTC/USDT', p, ts)
    check("正常波动不触发", not result.triggered)

def test_price_single_point():
    """只有 1 个数据点 → 不触发"""
    ad = AnomalyDetector()
    r = ad.check_price_spike('ETH/USDT', 3000)
    check("单数据点不触发", not r.triggered)

test_price_spike_critical()
test_price_spike_warning()
test_price_normal()
test_price_single_point()


# ═══════════════════════════════
# 2. 成交量异常
# ═══════════════════════════════

section("2. 成交量突增检测")

def test_volume_spike():
    """成交量 8x 均值 → 触发"""
    ad = AnomalyDetector()
    for _ in range(10):
        ad.check_volume_spike('BTC/USDT', 1000)
    r = ad.check_volume_spike('BTC/USDT', 8000)
    check("8x 成交量触发", r.triggered)
    check("类型=VOLUME_SPIKE", r.anomaly_type == AnomalyType.VOLUME_SPIKE)

def test_volume_normal():
    """成交量正常 → 不触发"""
    ad = AnomalyDetector()
    for _ in range(10):
        ad.check_volume_spike('BTC/USDT', 1000)
    r = ad.check_volume_spike('BTC/USDT', 1200)
    check("1.2x 成交量不触发", not r.triggered)

def test_volume_insufficient_data():
    """数据不足时不误报"""
    ad = AnomalyDetector()
    r = ad.check_volume_spike('NEW/USDT', 99999)
    check("数据不足时不触发", not r.triggered)

test_volume_spike()
test_volume_normal()
test_volume_insufficient_data()


# ═══════════════════════════════
# 3. 价格停滞检测
# ═══════════════════════════════

section("3. 价格停滞检测")

def test_price_stale():
    """价格超过 300 秒未更新 → 触发"""
    ad = AnomalyDetector()
    ad._last_price_time['BTC/USDT'] = time.time() - 350
    r = ad.check_price_stale('BTC/USDT')
    check("350 秒未更新触发", r.triggered)

def test_price_fresh():
    """价格刚更新 → 不触发"""
    ad = AnomalyDetector()
    ad._last_price_time['BTC/USDT'] = time.time()
    r = ad.check_price_stale('BTC/USDT')
    check("刚更新不触发", not r.triggered)

test_price_stale()
test_price_fresh()


# ═══════════════════════════════
# 4. 买卖价差异常
# ═══════════════════════════════

section("4. 买卖价差检测")

def test_spread_wide():
    """价差 6% → 触发"""
    ad = AnomalyDetector()
    r = ad.check_spread_anomaly('BTC/USDT', bid=64000, ask=68000)
    check("6.25% 价差触发", r.triggered)

def test_spread_normal():
    """价差 0.1% → 不触发"""
    ad = AnomalyDetector()
    r = ad.check_spread_anomaly('BTC/USDT', bid=64950, ask=65050)
    check("0.15% 价差不触发", not r.triggered)

def test_spread_zero_bid():
    """bid=0 防御"""
    ad = AnomalyDetector()
    r = ad.check_spread_anomaly('BTC/USDT', bid=0, ask=65000)
    check("bid=0 不崩溃", not r.triggered)

test_spread_wide()
test_spread_normal()
test_spread_zero_bid()


# ═══════════════════════════════
# 5. API 异常
# ═══════════════════════════════

section("5. API 异常检测")

def test_api_error_rate_escalation():
    """连续错误 → 错误率上升 → 触发临界"""
    ad = AnomalyDetector()
    for _ in range(6):
        ad.record_api_success()
    # 前 3 次失败不触发（错误率 < 30%）
    for i in range(3):
        r = ad.record_api_error(f"err {i}")
    check("3/9 错误 (33%) 触发", r.triggered)

def test_api_success_rate():
    """全部成功 → API 健康"""
    ad = AnomalyDetector()
    for _ in range(10):
        ad.record_api_success()
    check("10/10 成功, 健康", sum(ad._api_errors) == 0)

test_api_error_rate_escalation()
test_api_success_rate()


# ═══════════════════════════════
# 6. WebSocket
# ═══════════════════════════════

section("6. WebSocket 异常检测")

def test_ws_heartbeat_lost():
    """心跳丢失 100s → 触发"""
    ad = AnomalyDetector()
    ad._ws_last_heartbeat['ticker'] = time.time() - 100
    r = ad.check_ws_health('ticker')
    check("100s 无心跳触发", r.triggered)
    check("类型=WS_HEARTBEAT_LOST", r.anomaly_type == AnomalyType.WS_HEARTBEAT_LOST)

def test_ws_healthy():
    """刚有心跳 → 不触发"""
    ad = AnomalyDetector()
    ad.record_ws_heartbeat('ticker')
    r = ad.check_ws_health('ticker')
    check("刚有心跳不触发", not r.triggered)

def test_ws_max_reconnect():
    """重连超过上限 → CRITICAL"""
    ad = AnomalyDetector()
    for i in range(4):
        r = ad.record_ws_reconnect('ticker')
    # 第 5 次达到上限
    r = ad.record_ws_reconnect('ticker')
    check("第 5 次重连触发 CRITICAL", r.severity == AnomalySeverity.CRITICAL)

test_ws_heartbeat_lost()
test_ws_healthy()
test_ws_max_reconnect()


# ═══════════════════════════════
# 7. 数据异常
# ═══════════════════════════════

section("7. 数据异常检测")

def test_data_gap():
    """数据间隔 150s → 触发"""
    ad = AnomalyDetector()
    r = ad.check_data_gap('BTC/USDT', time.time(), time.time() - 150)
    check("150s 间隔触发", r.triggered)

def test_data_gap_normal():
    """数据间隔 60s → 不触发"""
    ad = AnomalyDetector()
    r = ad.check_data_gap('BTC/USDT', time.time(), time.time() - 60)
    check("60s 间隔不触发", not r.triggered)

def test_source_divergence():
    """双源价差 2% → 触发"""
    ad = AnomalyDetector()
    r = ad.check_source_divergence('BTC/USDT', 65000, 66300)
    check("2% 价差触发", r.triggered)

test_data_gap()
test_data_gap_normal()
test_source_divergence()


# ═══════════════════════════════
# 8. 综合健康检查
# ═══════════════════════════════

section("8. 系统健康检查")

def test_health_healthy():
    """全部正常 → healthy"""
    ad = AnomalyDetector()
    ad._last_price_time['BTC/USDT'] = time.time()
    for _ in range(10):
        ad.record_api_success()
    health = ad.system_health_check(['BTC/USDT'])
    check("全正常 → healthy", health.overall == "healthy")

def test_health_degraded():
    """价格停滞 → degraded"""
    ad = AnomalyDetector()
    ad._last_price_time['BTC/USDT'] = time.time() - 400
    for _ in range(10):
        ad.record_api_success()
    health = ad.system_health_check(['BTC/USDT'])
    check("价格停滞 → degraded", health.overall != "healthy")

test_health_healthy()
test_health_degraded()


# ═══════════════════════════════
# 9. 熔断 & 恢复
# ═══════════════════════════════

section("9. 熔断 & 恢复")

def test_circuit_breaker():
    """熔断激活 → 到期自动恢复"""
    ad = AnomalyDetector()
    ad.trigger_circuit_breaker("测试熔断", duration_seconds=0.1)
    check("熔断激活", ad.is_circuit_breaker_active())
    time.sleep(0.2)
    check("熔断到期自动恢复", not ad.is_circuit_breaker_active())

def test_circuit_breaker_status():
    """熔断状态查询"""
    ad = AnomalyDetector()
    ad.trigger_circuit_breaker("测试", duration_seconds=60)
    status = ad.get_circuit_breaker_status()
    check("熔断状态 active=True", status['active'])
    check("熔断原因正确", status['reason'] == '测试')

test_circuit_breaker()
test_circuit_breaker_status()


# ═══════════════════════════════
# 10. check_all 全量检查
# ═══════════════════════════════

section("10. check_all 全量检查")

def test_check_all_normal():
    """正常 bar → 无异常"""
    ad = AnomalyDetector()
    results = ad.check_all('BTC/USDT', {
        'close': 65000, 'volume': 1000, 'bid': 64950, 'ask': 65050,
        'close_binance': 65000, 'close_okx': 65010,
    })
    check("正常 bar 无异常", len(results) == 0)

def test_check_all_spike():
    """异常 bar → 捕获"""
    ad = AnomalyDetector()
    # 预热
    for _ in range(15):
        ad.check_volume_spike('BTC/USDT', 1000)
    results = ad.check_all('BTC/USDT', {
        'close': 55000, 'volume': 10000,
        'close_binance': 55000, 'close_okx': 57000,
    })
    check("异常 bar 捕获到异常", len(results) > 0)

test_check_all_normal()
test_check_all_spike()


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
