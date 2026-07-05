"""
外部信号模块 — 端到端演示

运行流程：
  1. 初始化各监控模块
  2. 加载模拟数据（实际应接入 API）
  3. 执行五路信号融合评估
  4. 生成日报 + Meme 发现报告

用法：
  python run_external_signals.py
  python run_external_signals.py --meme  # 包含 Meme 币发现报告
"""

import io
import sys
import os

# 确保 GBK 兼容
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')

# 添加到 Python path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from external_signals import (
    DeepSeekClient, KOLMonitor, KOLTier,
    WhaleTracker, SmartMoneyTracker, ContractEventMonitor,
    OrderBookMonitor,
    BinanceAlphaMonitor, SecondaryExchangeMonitor, Exchange, ListingPhase,
    SignalFusion, FusionReport,
)


def init_modules():
    """初始化所有外部信号模块"""
    print("🚀 初始化外部信号模块...\n")

    # API 客户端
    ds = DeepSeekClient()  # 如果没有 DEEPSEEK_API_KEY，使用降级模式

    # Day 19: KOL 监控
    kol = KOLMonitor(deepseek_client=ds)
    kol.load_kol_list()
    print(f"   KOL 监控: {len(kol.kol_list)} 人在白名单, "
          f"API {'已配置' if ds.api_key else '降级模式（模拟数据）'}")

    # Day 20: 鲸鱼追踪
    whale = WhaleTracker()
    smart_money = SmartMoneyTracker()
    contracts = ContractEventMonitor()
    print(f"   鲸鱼追踪: {len(smart_money.wallets)} 个聪明钱地址")

    # Day 20: 订单簿
    orderbook = OrderBookMonitor()
    print(f"   订单簿: 就绪")

    # Day 20: 交易所监控
    binance_alpha = BinanceAlphaMonitor()
    secondary_ex = SecondaryExchangeMonitor()
    print(f"   交易所监控: 就绪")

    print()
    return ds, kol, whale, smart_money, contracts, orderbook, binance_alpha, secondary_ex


def load_simulated_data(kol, whale, smart_money, binance_alpha, secondary_ex, orderbook):
    """
    加载模拟数据 — 演示各模块工作流程

    实际运行时应替换为 API 拉取：
      - KOL: Twitter API / Nitter RSS
      - 鲸鱼: Whale Alert API / Etherscan
      - 订单簿: 币安 depth WebSocket
      - 交易所: 公告页 RSS 爬取
    """
    print("📥 加载模拟数据...\n")

    # ── 模拟 KOL 提及 ──
    simulated_mentions = [
        # BTC — 正常讨论
        {"kol": "lookonchain", "coin": "BTC", "text": "BTC exchange reserves hit 5-year low.  Accumulation continues.", "price": 65000},
        {"kol": "nansen_ai", "coin": "BTC", "text": "Smart money is buying the dip on BTC. Interesting on-chain patterns.", "price": 64800},
        {"kol": "aeyakovenko", "coin": "BTC", "text": "BTC dominance rising, alts bleeding. Macro looks uncertain.", "price": 65200},
        {"kol": "Dynamo_Patrick", "coin": "BTC", "text": "BTC ETF flows positive for 5th consecutive day.", "price": 65100},

        # ETH — 中等讨论
        {"kol": "lookonchain", "coin": "ETH", "text": "ETH staking rate hits new ATH. Supply shock incoming?", "price": 3400},
        {"kol": "artemis__xyz", "coin": "ETH", "text": "ETH L2 activity growing — Base and Arbitrum leading.", "price": 3420},

        # SOL — KOL 集群（异常信号）
        {"kol": "0xMert_", "coin": "SOL", "text": "Solana ecosystem is on fire. New projects launching daily.", "price": 145},
        {"kol": "blknoiz06", "coin": "SOL", "text": "SOL looking primed for a breakout. DeFi TVL surging.", "price": 146},
        {"kol": "dingalingts", "coin": "SOL", "text": "Bought more SOL. The ecosystem is undervalued relative to ETH.", "price": 147},
        {"kol": "CryptoGodJohn", "coin": "SOL", "text": "SOL to $200 soon. Don't fade the strength.", "price": 148},
        {"kol": "Crypto_Banter", "coin": "SOL", "text": "SOL is the trade of 2026. Loading up.", "price": 147},

        # ANSEM — Meme 币发现
        {"kol": "0xMert_", "coin": "ANSEM", "text": "Interesting new AI agent project on Solana. Worth watching.", "price": 0.0001},
        {"kol": "blknoiz06", "coin": "ANSEM", "text": "ANSEM might be the next big AI play. Early stages.", "price": 0.00012},
    ]

    for m in simulated_mentions:
        kol.record_mention(
            kol_handle=m["kol"], coin=m["coin"],
            text=m["text"], price=m["price"],
            analyze_sentiment=False,  # 模拟模式跳过 API 调用
        )
    print(f"   加载 {len(simulated_mentions)} 条模拟 KOL 提及")

    # ── 模拟鲸鱼转账 ──
    simulated_transfers = [
        {"tx_hash": "0xabc1", "from": "0xUnknown1", "to": "0x28C6c06298d514Db089934071355E5743bf21d60",
         "amount": 500, "asset": "BTC", "block_number": 800000, "source": "whale_alert"},
        {"tx_hash": "0xabc2", "from": "0x21a31Ee1afC51d94C2eFcCAa2092aD1028285549",
         "to": "0xUnknown2", "amount": 200, "asset": "BTC", "block_number": 800001, "source": "whale_alert"},
        {"tx_hash": "0xabc3", "from": "0xUnknown3",
         "to": "0x6cC5F688a315f3dC28A7781717a9A798a59fDA7b",
         "amount": 10000000, "asset": "USDT", "block_number": 800002, "source": "etherscan"},
    ]
    for tx in simulated_transfers:
        whale.record_transfer(tx)
    print(f"   加载 {len(simulated_transfers)} 条模拟鲸鱼转账")

    # ── 模拟聪明钱活动 ──
    for i, addr in enumerate(list(smart_money.wallets.keys())[:3]):
        smart_money.record_activity(addr, "buy", "SOL", 5000 + i * 2000)
    print(f"   模拟 3 个聪明钱钱包买入 SOL")

    # ── 模拟二线所上币 ──
    secondary_ex.check_new_listing(Exchange.MEXC, "ANSEM", "2026-07-05T08:00:00Z")
    print(f"   模拟 MEXC 上线 ANSEM")

    # ── 模拟币安 Alpha ──
    binance_alpha.record_announcement("NEWCOIN", Exchange.BINANCE, "2026-07-04T10:00:00Z")
    print(f"   模拟币安公告 NEWCOIN")

    # ── 模拟订单簿 ──
    # BTC 模拟快照
    orderbook.collect_snapshot(
        "BTC",
        bids=[[64900, 1.5], [64850, 2.3], [64800, 5.0], [64700, 8.0], [64600, 12.0]],
        asks=[[65100, 1.2], [65150, 2.0], [65200, 4.5], [65300, 7.0], [65400, 10.0]],
    )
    print(f"   模拟 BTC 订单簿快照")

    print()


def main():
    # ── 初始化 ──
    ds, kol, whale, smart_money, contracts, orderbook, binance_alpha, secondary_ex = init_modules()

    # ── 加载模拟数据 ──
    load_simulated_data(kol, whale, smart_money, binance_alpha, secondary_ex, orderbook)

    # ── 融合引擎 ──
    fusion = SignalFusion(
        kol_monitor=kol,
        whale_tracker=whale,
        smart_money=smart_money,
        contract_monitor=contracts,
        orderbook_monitor=orderbook,
        binance_alpha=binance_alpha,
        secondary_exchange=secondary_ex,
    )

    # ── 主流币评估 ──
    print("=" * 60)
    print("🔬 主流币五路信号融合评估")
    print("=" * 60)

    for token in ["BTC", "ETH", "SOL"]:
        result = fusion.evaluate(token)
        symbol = {"red": "🔴", "yellow": "🟡", "green": "🟢", "gray": "⚪"}[result.alert_level.value]
        print(f"\n{symbol} {token} — {result.total_score:.1f}/110 分 ({result.alert_level.value})")
        for s in result.sources:
            bar = "█" * int(s.normalized_score * 10) + "░" * (10 - int(s.normalized_score * 10))
            print(f"   {s.name:12s} [{bar}] {s.raw_score:.1f}/{s.max_score:.0f}")
        print(f"   仓位系数: ×{result.position_multiplier}")
        if result.risk_flags:
            for flag in result.risk_flags:
                print(f"   ⚠️ {flag}")
        print(f"   📋 {result.recommendation}")

    # ── 仓位调整建议 ──
    print(f"\n{'='*60}")
    print("📐 仓位调整 (基于外部信号)")
    print("=" * 60)
    for token in ["BTC", "ETH", "SOL"]:
        adj = fusion.get_position_adjustment(token, base_position_pct=1.0)
        print(f"   {token}: 1.0 → ×{adj['multiplier']} = {adj['adjusted_position_pct']} "
              f"({adj['alert_level']})")

    # ── 生成报告 ──
    print(f"\n{'='*60}")
    print("📄 生成信号融合报告")
    print("=" * 60)

    report = FusionReport(fusion, kol=kol, whale=whale, orderbook=orderbook)

    # 日报
    daily = report.generate_daily_brief(["BTC", "ETH", "SOL"])
    print(daily)

    # Meme 发现报告（如果传入 --meme 参数）
    if "--meme" in sys.argv:
        print("\n")
        meme_candidates = [
            {
                "token": "ANSEM", "created_hours_ago": 6, "volume_24h": 1_200_000,
                "smart_money_buying": 3, "kol_mentions": 2,
                "liquidity_locked": True, "on_mexc": True, "on_bitget": False,
            },
            {
                "token": "MOONDOG", "created_hours_ago": 2, "volume_24h": 300_000,
                "smart_money_buying": 1, "kol_mentions": 0,
                "liquidity_locked": False, "on_mexc": False, "on_bitget": False,
            },
            {
                "token": "SOLPEPE", "created_hours_ago": 12, "volume_24h": 800_000,
                "smart_money_buying": 5, "kol_mentions": 4,
                "liquidity_locked": True, "on_mexc": True, "on_bitget": True,
            },
        ]
        meme_report = report.generate_meme_discovery_report(meme_candidates)
        print(meme_report)

    # ── 统计 ──
    print(f"\n📊 模块运行统计:")
    print(f"   KOL: {kol.get_stats()}")
    print(f"   鲸鱼: {whale.get_stats()}")
    print(f"   订单簿: {orderbook.get_stats()}")
    print(f"   融合: {fusion.get_stats()}")

    print(f"\n✅ 外部信号模块运行完成")
    print(f"   ⚠️ 当前使用模拟数据。接入真实 API 后替换 load_simulated_data() 中的数据源。")


if __name__ == "__main__":
    main()
