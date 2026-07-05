"""
信号融合报告生成器 — Day 21

输出：
  1. 每日信号融合快报（日报嵌入）
  2. 外部信号仪表盘摘要（文字版）
  3. 三维复盘 — 事件驱动层数据

融入路线图 Day 15-18 的复盘系统：
  - 日报核心问题：今天外部信号有没有触发风控？
  - 周报核心问题：本周外部信号对策略绩效的净影响？
  - 月报核心问题：信号源的可靠性有没有变化？
"""

import json
import os
from dataclasses import asdict
from datetime import datetime, timedelta
from typing import Optional

from .signal_fusion import SignalFusion, FusionScore, AlertLevel
from .kol_monitor import KOLMonitor
from .whale_tracker import WhaleTracker
from .orderbook_monitor import OrderBookMonitor


class FusionReport:
    """
    信号融合报告生成器
    """

    def __init__(
        self,
        fusion: SignalFusion,
        kol: Optional[KOLMonitor] = None,
        whale: Optional[WhaleTracker] = None,
        orderbook: Optional[OrderBookMonitor] = None,
    ):
        self.fusion = fusion
        self.kol = kol
        self.whale = whale
        self.orderbook = orderbook

    # ═══════════════════════════════
    # 日报：信号融合快报
    # ═══════════════════════════════

    def generate_daily_brief(self, tokens: list[str]) -> str:
        """
        每日信号融合快报

        回答：今天外部信号有没有触发风控？有没有值得关注的新信号？
        """
        results = [self.fusion.evaluate(t) for t in tokens]

        lines = []
        lines.append("=" * 60)
        lines.append(f"📡 外部信号融合日报 — {datetime.utcnow().strftime('%Y-%m-%d')}")
        lines.append("=" * 60)

        # ── 总览 ──
        reds = [r for r in results if r.alert_level == AlertLevel.RED]
        yellows = [r for r in results if r.alert_level == AlertLevel.YELLOW]
        greens = [r for r in results if r.alert_level == AlertLevel.GREEN]
        grays = [r for r in results if r.alert_level == AlertLevel.GRAY]

        lines.append(f"\n📊 信号总览: 🔴{len(reds)} 🟡{len(yellows)} 🟢{len(greens)} ⚪{len(grays)}")
        lines.append("")

        # ── 各币种详情 ──
        for r in results:
            self._append_token_section(lines, r)

        # ── 风控汇总 ──
        all_risk_flags = []
        for r in results:
            all_risk_flags.extend(r.risk_flags)
        if all_risk_flags:
            lines.append("-" * 60)
            lines.append("🛡️ 风控标记汇总:")
            for flag in all_risk_flags:
                lines.append(f"   • {flag}")

        # ── 仓位调整建议 ──
        lines.append("")
        lines.append("-" * 60)
        lines.append("📐 仓位调整建议:")
        for r in results:
            adj = self.fusion.get_position_adjustment(r.token, 1.0)
            lines.append(f"   {r.token}: 原始 1.0 → 调整后 ×{adj['multiplier']} "
                         f"({adj['reason'][:60]}...)")

        # ── KOL 异常 ──
        if self.kol:
            lines.append("")
            lines.append("-" * 60)
            lines.append("🐦 KOL 异常检测:")
            for token in tokens:
                anomalies = self.kol.detect_anomalies(token, hours=24)
                if anomalies:
                    for a in anomalies:
                        lines.append(f"   [{a.severity.upper()}] {token}: {a.type} — {a.detail}")
                else:
                    lines.append(f"   {token}: 无异常")
            if not any(self.kol.detect_anomalies(t) for t in tokens):
                lines.append("   全部币种无 KOL 异常事件")

        # ── 鲸鱼活动 ──
        if self.whale:
            lines.append("")
            lines.append("-" * 60)
            lines.append("🐋 鲸鱼活动 (24h):")
            for asset in ["BTC", "ETH"]:
                flow = self.whale.get_net_flow(asset, hours=24)
                if flow["transfer_count"] > 0:
                    lines.append(
                        f"   {asset}: 净{'流入' if flow['net_flow'] > 0 else '流出'} "
                        f"({flow['net_flow_signal']}) — {flow['transfer_count']} 笔大额转账"
                    )

        lines.append("")
        lines.append("=" * 60)
        return "\n".join(lines)

    # ═══════════════════════════════
    # 周报：外部信号绩效分析
    # ═══════════════════════════════

    def generate_weekly_summary(self, tokens: list[str]) -> str:
        """
        周度外部信号总结

        回答：本周外部信号对策略绩效的净影响？
        """
        lines = []
        lines.append("=" * 60)
        lines.append(f"📡 外部信号融合周报 — {datetime.utcnow().strftime('%Y-W%W')}")
        lines.append("=" * 60)

        # 收集本周所有融合评估
        week_results = self.fusion.fusion_history[-50:]  # 假设最近 50 条覆盖本周

        lines.append(f"\n📊 本周评估次数: {len(week_results)}")

        # 信号分布
        level_counts = {level: 0 for level in AlertLevel}
        for r in week_results:
            level_counts[r.alert_level] += 1
        lines.append(f"   信号分布: 🔴{level_counts[AlertLevel.RED]} "
                     f"🟡{level_counts[AlertLevel.YELLOW]} "
                     f"🟢{level_counts[AlertLevel.GREEN]} "
                     f"⚪{level_counts[AlertLevel.GRAY]}")

        # 各币种均值
        lines.append("\n📈 各币种融合评分均值:")
        token_scores = {}
        for r in week_results:
            if r.token not in token_scores:
                token_scores[r.token] = []
            token_scores[r.token].append(r.total_score)

        for token, scores in sorted(token_scores.items()):
            avg = sum(scores) / len(scores)
            trend = "↑" if len(scores) > 1 and scores[-1] > scores[0] else "↓" if len(scores) > 1 else "→"
            lines.append(f"   {token}: {avg:.1f}/110 {trend} (n={len(scores)})")

        # 风险事件汇总
        all_flags = []
        for r in week_results:
            all_flags.extend(r.risk_flags)
        if all_flags:
            lines.append(f"\n🛡️ 本周风控触发: {len(all_flags)} 次")
            # 去重
            unique_flags = list(set(all_flags))
            for flag in unique_flags[:10]:
                count = all_flags.count(flag)
                lines.append(f"   • [{count}×] {flag}")

        # 各信号源平均贡献
        lines.append("\n🔬 各信号源平均贡献:")
        source_contributions = {}
        for r in week_results:
            for s in r.sources:
                if s.name not in source_contributions:
                    source_contributions[s.name] = []
                source_contributions[s.name].append(s.normalized_score)

        for name, scores in source_contributions.items():
            avg = sum(scores) / len(scores)
            bar = "█" * int(avg * 20)
            lines.append(f"   {name}: {bar} {avg:.2f}")

        lines.append("")
        lines.append("=" * 60)
        return "\n".join(lines)

    # ═══════════════════════════════
    # 三维复盘：事件驱动层
    # ═══════════════════════════════

    def generate_3d_event_layer(self, token: str, big_moves: list[dict]) -> str:
        """
        三维复盘 — 事件驱动层

        big_moves: [{'time': str, 'pct': float, 'direction': 'up'|'down'}, ...]

        回答：每笔大盈亏对应的外部事件是什么？
        """
        lines = []
        lines.append(f"\n{'='*60}")
        lines.append(f"🔍 三维复盘 — 事件驱动层: {token}")
        lines.append(f"{'='*60}")

        for i, move in enumerate(big_moves, 1):
            lines.append(f"\n📌 事件 #{i}: "
                         f"{'📈' if move['direction'] == 'up' else '📉'} "
                         f"{move['pct']:+.2f}% @ {move['time']}")

            # 归因分析
            attribution = self.fusion.attribute_price_move(
                token, move['pct'], move['time']
            )

            lines.append(f"   叙事驱动: {attribution['narrative']}")
            for source, contrib in attribution['attribution'].items():
                lines.append(f"   {source}: {contrib}")

            # 附近异常
            if attribution['anomalies_during_move']:
                lines.append(f"   ⚠️ 同期异常: {len(attribution['anomalies_during_move'])} 个")
                for a in attribution['anomalies_during_move'][:3]:
                    lines.append(f"      - {a.get('type', 'unknown')}: {a.get('detail', '')}")

        lines.append(f"\n{'='*60}")
        return "\n".join(lines)

    # ═══════════════════════════════
    # Meme 币发现日报
    # ═══════════════════════════════

    def generate_meme_discovery_report(self, candidates: list[dict]) -> str:
        """
        Meme 币发现日报 — 10-100x 候选列表

        candidates: [{'token': str, 'created_hours_ago': float, 'volume_24h': float,
                       'smart_money_buying': int, 'kol_mentions': int,
                       'liquidity_locked': bool, 'on_mexc': bool, 'on_bitget': bool}, ...]
        """
        lines = []
        lines.append("=" * 60)
        lines.append(f"🎯 10-100x Meme 币发现日报 — {datetime.utcnow().strftime('%Y-%m-%d')}")
        lines.append("=" * 60)

        results = []
        for c in candidates:
            r = self.fusion.evaluate_meme(
                token=c["token"],
                created_hours_ago=c.get("created_hours_ago", 24),
                volume_24h_usd=c.get("volume_24h", 0),
                liquidity_locked=c.get("liquidity_locked", True),
                smart_money_buying=c.get("smart_money_buying", 0),
                kol_mentions=c.get("kol_mentions", 0),
                on_mexc=c.get("on_mexc", False),
                on_bitget=c.get("on_bitget", False),
            )
            results.append((c, r))

        # 按评分排序
        results.sort(key=lambda x: -x[1].total_score)

        lines.append(f"\n📊 候选币种: {len(results)} 个")
        lines.append("")

        for cand, result in results:
            symbol = "🔴" if result.alert_level == AlertLevel.RED else \
                     "🟡" if result.alert_level == AlertLevel.YELLOW else \
                     "🟢" if result.alert_level == AlertLevel.GREEN else "⚪"
            lines.append(f"{symbol} {cand['token']} — {result.total_score:.1f}/110 分")
            lines.append(f"   创建 {cand.get('created_hours_ago', '?')}h 前 | "
                         f"24h成交量 ${cand.get('volume_24h', 0):,.0f} | "
                         f"聪明钱 {cand.get('smart_money_buying', 0)} 个 | "
                         f"KOL {cand.get('kol_mentions', 0)} 次")
            if cand.get("on_mexc"):
                lines.append(f"   🏦 MEXC 已上线" + (" | Bitget 已上线" if cand.get("on_bitget") else ""))
            if result.risk_flags:
                for flag in result.risk_flags[:2]:
                    lines.append(f"   ⚠️ {flag}")
            lines.append(f"   📋 {result.recommendation}")
            lines.append("")

        # ── 汇总统计 ──
        reds = sum(1 for _, r in results if r.alert_level == AlertLevel.RED)
        yellows = sum(1 for _, r in results if r.alert_level == AlertLevel.YELLOW)
        lines.append("-" * 60)
        lines.append(f"📊 总结: {reds} 强力信号 | {yellows} 关注信号 | "
                     f"{len(results) - reds - yellows} 弱/噪音")
        lines.append("")
        lines.append("⚠️ 提醒：以上为外部信号融合评分，不构成交易建议。")
        lines.append("   只有策略信号能触发开仓。Meme 币单笔仓位严格限制在 0.1-0.5%。")
        lines.append("=" * 60)

        return "\n".join(lines)

    # ═══════════════════════════════
    # 辅助
    # ═══════════════════════════════

    def _append_token_section(self, lines: list[str], r: FusionScore):
        """追加单个币种的日报部分"""
        symbol = "🔴" if r.alert_level == AlertLevel.RED else \
                 "🟡" if r.alert_level == AlertLevel.YELLOW else \
                 "🟢" if r.alert_level == AlertLevel.GREEN else "⚪"
        lines.append(f"{symbol} {r.token} — 融合评分 {r.total_score:.1f}/110 ({r.alert_level.value})")

        # 各信号源
        for s in r.sources:
            bar = "█" * int(s.normalized_score * 10) + "░" * (10 - int(s.normalized_score * 10))
            lines.append(f"   {s.name:12s} [{bar}] {s.raw_score:.1f}/{s.max_score:.0f}")

        # 仓位建议
        lines.append(f"   仓位系数: ×{r.position_multiplier}")

        # 风险标记
        if r.risk_flags:
            for flag in r.risk_flags:
                lines.append(f"   ⚠️ {flag}")

        lines.append("")

    def save_report(self, report: str, report_type: str, data_dir: Optional[str] = None):
        """保存报告到文件"""
        data_dir = data_dir or os.path.join(
            os.path.dirname(os.path.abspath(__file__)), "..", "reports"
        )
        os.makedirs(data_dir, exist_ok=True)
        date_str = datetime.utcnow().strftime("%Y%m%d")
        filename = f"{report_type}_{date_str}.txt"
        filepath = os.path.join(data_dir, filename)
        with open(filepath, "w", encoding="utf-8") as f:
            f.write(report)
        print(f"📄 报告已保存: {filepath}")
        return filepath
