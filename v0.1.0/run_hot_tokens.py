"""
热门代币庄家动向日报 -- Day 21 增强

实时追踪每日热门代币的：
 1. 庄家姿态（吸筹/出货/中性）
 2. 合约信号（OI + 资金费率 + 多空比）
 3. 现货/合约开单点位（支撑/阻力 + 止损/止盈 + 盈亏比）

用法：
 python run_hot_tokens.py
 python run_hot_tokens.py --token BTC --token SOL
"""

import io
import sys
import os

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from external_signals import (
 DeepSeekClient, KOLMonitor, KOLTier,
 WhaleTracker, SmartMoneyTracker,
 OrderBookMonitor,
 BinanceAlphaMonitor, SecondaryExchangeMonitor,
 SignalFusion, FusionReport,
 HotTokenTracker, MMStance, TradeDirection,
)


def print_separator(title: str = ""):
 if title:
 print(f"\n{'='*60}")
 print(f" {title}")
 print(f"{'='*60}")
 else:
 print("-" * 60)


def print_entry_zone(zone, label: str):
 """格式化打印开单区间"""
 if not zone:
 return
 dir_symbol = " LONG" if zone["direction"] == "long" else "[!OVF] SHORT"
 conf_symbol = {"high": " ", "medium": " ", "low": " "}[zone["confidence"]]
 print(f" {label} {dir_symbol} {conf_symbol}")
 print(f" 入场区间: {zone['zone_low']:.4f} ~ {zone['zone_high']:.4f}")
 print(f" 止损: {zone['stop_loss']:.4f} | "
 f"止盈1: {zone['take_profit_1']:.4f} | 止盈2: {zone['take_profit_2']:.4f}")
 print(f" 盈亏比: {zone['risk_reward']} | 建议仓位: {zone['suggested_size_pct']:.1%}")
 print(f" 信号来源: {zone['signal_source']}")
 if zone['notes']:
 print(f" 备注: {zone['notes']}")


def print_mm_stance(profile):
 """格式化打印庄家姿态"""
 stance_symbols = {
 "accumulating_aggressive": " 激进吸筹",
 "accumulating": "[UP] 吸筹中",
 "distributing_aggressive": " 激进出货",
 "distributing": "[DOWN] 出货中",
 "neutral": " 中性",
 }
 symbol = stance_symbols.get(profile.stance.value, " ")
 print(f"\n 庄家姿态: {symbol} (置信度 {profile.confidence:.0%})")
 print(f" {profile.position_recommendation}")

 if profile.signals.get("breakdown"):
 breakdown = profile.signals["breakdown"]
 for source, detail in breakdown.items():
 if isinstance(detail, dict):
 score = detail.get("score", 0)
 bar = " " if score > 0.05 else "[!OVF]" if score < -0.05 else " "
 print(f" {bar} {source}: {score:+.2f}")


def print_contract_signal(signal):
 """格式化打印合约信号"""
 if not signal:
 return
 print(f"\n [STAT] 合约信号:")
 print(f" OI 24h变化: {signal.oi_change_24h_pct:+.1f}%")
 print(f" OI-价格背离: {signal.oi_price_divergence}")
 print(f" 资金费率(年化): {signal.funding_rate_pct:+.2f}% "
 f"(分位数: {signal.funding_rate_percentile:.0%})")
 print(f" 合约情绪: {signal.contract_sentiment}")

 if signal.liquidation_clusters:
 print(f" 清算密集区:")
 for c in signal.liquidation_clusters[:3]:
 print(f" ${c['price']:.4f}: {c['liq_amount']:.0f} USDT")


def main():
 # 初始化 
 print("[GO] 热门代币庄家动向追踪\n")
 ds = DeepSeekClient()
 kol = KOLMonitor(deepseek_client=ds)
 kol.load_kol_list()
 whale = WhaleTracker()
 smart_money = SmartMoneyTracker()
 orderbook = OrderBookMonitor()
 hot_tokens = HotTokenTracker()

 # 加载模拟数据 
 print("[FETCH] 加载模拟数据...")

 # 模拟 KOL
 simulated_mentions = [
 {"kol": "lookonchain", "coin": "BTC", "text": "BTC exchange reserves at new low.", "price": 65000},
 {"kol": "nansen_ai", "coin": "BTC", "text": "Smart money accumulating BTC.", "price": 64800},
 {"kol": "0xMert_", "coin": "SOL", "text": "Solana ecosystem growing fast.", "price": 145},
 {"kol": "blknoiz06", "coin": "SOL", "text": "SOL breakout loading.", "price": 146},
 {"kol": "CryptoGodJohn", "coin": "SOL", "text": "SOL to $200 soon!", "price": 147},
 {"kol": "Crypto_Banter", "coin": "SOL", "text": "SOL is the trade. Loading up.", "price": 147},
 ]
 for m in simulated_mentions:
 kol.record_mention(m["kol"], m["coin"], m["text"], m["price"], analyze_sentiment=False)

 # 模拟鲸鱼转账 -- BTC 净流出（积累信号）
 whale.record_transfer({
 "tx_hash": "0xabc1", "from": "0x21a31Ee1afC51d94C2eFcCAa2092aD1028285549",
 "to": "0xUnknown1", "amount": 300, "asset": "BTC",
 "block_number": 800000, "source": "whale_alert",
 })
 whale.record_transfer({
 "tx_hash": "0xabc2", "from": "0xUnknown2",
 "to": "0x28C6c06298d514Db089934071355E5743bf21d60",
 "amount": 800, "asset": "USDT",
 "block_number": 800001, "source": "etherscan",
 })

 # 模拟聪明钱
 for i, addr in enumerate(list(smart_money.wallets.keys())[:4]):
 smart_money.record_activity(addr, "buy", "SOL", 3000 + i * 1500)

 # 模拟订单簿
 orderbook.collect_snapshot("BTC",
 bids=[[64900, 1.5], [64800, 3.0], [64700, 5.5], [64500, 8.0], [64300, 12.0]],
 asks=[[65100, 1.2], [65200, 2.5], [65400, 4.0], [65600, 6.0], [65800, 8.0]],
 )
 orderbook.collect_snapshot("SOL",
 bids=[[143, 5000], [142, 8000], [140, 15000], [138, 20000]],
 asks=[[148, 4000], [150, 6000], [152, 10000], [155, 12000]],
 )

 print(f" 加载 {len(simulated_mentions)} 条 KOL 提及")
 print(f" 加载 2 条鲸鱼转账")
 print(f" 模拟 4 个聪明钱买入 SOL")
 print(f" 加载 BTC/SOL 订单簿快照")
 print()

 # ===============================
 # BTC 庄家动向
 # ===============================
 print_separator("BTC 庄家动向与开单点位")

 # 庄家评估
 whale_flow = whale.get_net_flow("BTC", hours=24)
 ob_signals = orderbook.get_all_signals("BTC")
 kol_anomalies = kol.detect_anomalies("BTC", hours=24)

 btc_profile = hot_tokens.assess_mm_stance(
 token="BTC",
 whale_net_flow=whale_flow,
 orderbook_signals=ob_signals,
 kol_anomalies=kol_anomalies,
 funding_rate=15.0, # 年化 15%
 )
 print_mm_stance(btc_profile)

 # 合约信号
 btc_contract = hot_tokens.analyze_contract_signals(
 token="BTC",
 oi_change_24h_pct=-4.5,
 price_change_24h_pct=0.8,
 funding_rate_annual=15.0,
 long_short_ratio=1.2,
 liquidation_map=[
 {"price": 63200, "amount": 5000000},
 {"price": 63800, "amount": 3500000},
 {"price": 67200, "amount": 4200000},
 {"price": 68000, "amount": 6500000},
 ],
 )
 print_contract_signal(btc_contract)

 # 开单点位
 btc_entry = hot_tokens.calculate_entry_zones(
 token="BTC",
 current_price=65000,
 orderbook_snapshots=orderbook.get_recent_snapshots("BTC"),
 atr=1200,
 liquidation_map=[
 {"price": 63200, "amount": 5000000},
 {"price": 63800, "amount": 3500000},
 {"price": 67200, "amount": 4200000},
 {"price": 68000, "amount": 6500000},
 ],
 mm_stance=btc_profile.stance,
 )

 print(f"\n 现价: ${btc_entry['current_price']:,.2f} | ATR: {btc_entry['atr']:.0f}")
 print(f" 关键支撑: ${btc_entry['key_support']:,.2f} | 关键阻力: ${btc_entry['key_resistance']:,.2f}")

 print(f"\n === 现货开单点位 ===")
 for z in btc_entry["spot"]["long_zones"]:
 print_entry_zone(z, "做多")
 for z in btc_entry["spot"]["short_zones"]:
 print_entry_zone(z, "做空")

 print(f"\n === 合约开单点位 ===")
 for z in btc_entry["contract"]["long_zones"]:
 print_entry_zone(z, "做多")
 for z in btc_entry["contract"]["short_zones"]:
 print_entry_zone(z, "做空")

 print(f"\n 综合建议: {btc_entry['summary']}")

 # ===============================
 # SOL 庄家动向
 # ===============================
 print_separator("SOL 庄家动向与开单点位")

 smart_money_activity = smart_money.scan_buying_activity("SOL", hours=24)
 ob_signals_sol = orderbook.get_all_signals("SOL")
 kol_anomalies_sol = kol.detect_anomalies("SOL", hours=24)

 sol_profile = hot_tokens.assess_mm_stance(
 token="SOL",
 orderbook_signals=ob_signals_sol,
 smart_money_activity=smart_money_activity,
 kol_anomalies=kol_anomalies_sol,
 oi_data={"oi_change_24h_pct": 8.2, "price_change_24h_pct": 1.5},
 funding_rate=35.0,
 )
 print_mm_stance(sol_profile)

 # SOL 合约信号
 sol_contract = hot_tokens.analyze_contract_signals(
 token="SOL",
 oi_change_24h_pct=8.2,
 price_change_24h_pct=1.5,
 funding_rate_annual=35.0,
 long_short_ratio=2.1,
 liquidation_map=[
 {"price": 138, "amount": 1200000},
 {"price": 155, "amount": 2100000},
 ],
 )
 print_contract_signal(sol_contract)

 # SOL 开单点位
 sol_entry = hot_tokens.calculate_entry_zones(
 token="SOL",
 current_price=146,
 orderbook_snapshots=orderbook.get_recent_snapshots("SOL"),
 atr=5.5,
 liquidation_map=[
 {"price": 138, "amount": 1200000},
 {"price": 155, "amount": 2100000},
 ],
 mm_stance=sol_profile.stance,
 )

 print(f"\n 现价: ${sol_entry['current_price']:.2f} | ATR: {sol_entry['atr']:.2f}")
 print(f" 关键支撑: ${sol_entry['key_support']:.2f} | 关键阻力: ${sol_entry['key_resistance']:.2f}")

 print(f"\n === 现货开单点位 ===")
 for z in sol_entry["spot"]["long_zones"]:
 print_entry_zone(z, "做多")
 for z in sol_entry["spot"]["short_zones"]:
 print_entry_zone(z, "做空")

 print(f"\n === 合约开单点位 ===")
 for z in sol_entry["contract"]["long_zones"]:
 print_entry_zone(z, "做多")
 for z in sol_entry["contract"]["short_zones"]:
 print_entry_zone(z, "做空")

 print(f"\n 综合建议: {sol_entry['summary']}")

 # ===============================
 # 汇总
 # ===============================
 print_separator("每日热门代币汇总")

 for token in ["BTC", "SOL"]:
 latest = hot_tokens.get_latest_mm_stance(token)
 if latest:
 stance_labels = {
 "accumulating_aggressive": " 激进吸筹",
 "accumulating": "[UP] 吸筹",
 "distributing_aggressive": " 激进出货",
 "distributing": "[DOWN] 出货",
 "neutral": " 中性",
 }
 print(f" {token:6s} | {stance_labels.get(latest.stance.value, '?'):12s} | "
 f"置信度 {latest.confidence:.0%}")

 print(f"\n[STAT] 追踪统计: {hot_tokens.get_stats()}")
 print(f"\n[OK] 庄家动向追踪完成")
 print(f" [WARN] 合约数据（OI/资金费率/清算地图）当前使用模拟数据。")
 print(f" 接入币安合约 API 后替换为实时数据。")


if __name__ == "__main__":
 main()
