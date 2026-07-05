"""
实盘交易调度入口 — Day 7

端到端流程：
  数据 → 策略信号 → 风控检查 → 订单执行 → 日志记录

两种模式：
  python run_live.py --mode paper   → 本地模拟盘（当前可用）
  python run_live.py --mode live    → 真实交易（API key 就绪后）

安全边界：
  - 风控有最终否决权
  - 任何自动操作前必须先过风控层
  - 信号生成(策略) → 风控审批 → 订单执行，不可跳过任何一步

用法：
  python run_live.py                          # paper 模式，BTC 1h
  python run_live.py --mode paper --symbol BTC --timeframe 1h
  python run_live.py --mode live --capital 200  # 实盘 200U
  python run_live.py --check-readiness          # 检查部署就绪状态
"""

import pandas as pd
import numpy as np
import sys
import os
import time
import argparse
from datetime import datetime, timezone

if sys.platform == 'win32':
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    sys.stderr.reconfigure(encoding='utf-8', errors='replace')

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from config_manager import ConfigManager, create_env_template
from live_logger import LiveLogger
from risk_manager import RiskManager, RiskAction
from trade_executor import create_exchange, OrderAction
from cost_model import CostModel, OrderType, LiquidityTier
from paper_trader import HistoricalDataFeed

# ═══════════════════════════════
# 配置
# ═══════════════════════════════
DATA_DIR = os.path.dirname(os.path.abspath(__file__))
CLEAN_DIR = os.path.join(DATA_DIR, 'clean')


def print_banner(mode):
    print(f"""
╔══════════════════════════════════════════════════════╗
║         加密量化交易系统 v0.1 — Day 7 实盘壳         ║
║         模式: {mode.upper():<7}                           ║
╚══════════════════════════════════════════════════════╝
""")


def load_data(symbol='BTC/USDT', timeframe='1h'):
    """加载数据"""
    symbol_safe = symbol.replace('/', '')
    filepath = os.path.join(CLEAN_DIR, f"{symbol_safe}_{timeframe}.parquet")
    if not os.path.exists(filepath):
        print(f"❌ 数据文件不存在: {filepath}")
        return None
    df = pd.read_parquet(filepath)
    print(f"📂 加载: {filepath} ({len(df):,} 行)")
    return df


def create_strategy(name, params):
    """根据名称创建策略实例"""
    from backtest.strategies.trend import TrendStrategy
    from backtest.strategies.momentum import MomentumStrategy

    name_lower = name.lower()
    if '趋势' in name or 'trend' in name_lower:
        return TrendStrategy(
            fast_period=params.get('fast_period', 20),
            slow_period=params.get('slow_period', 50),
            atr_stop=params.get('atr_stop', 2.0),
        )
    elif '动量' in name or 'momentum' in name_lower:
        return MomentumStrategy(
            fast_momentum=params.get('fast_momentum', 20),
            slow_momentum=params.get('slow_momentum', 50),
            atr_stop=params.get('atr_stop', 2.5),
        )
    else:
        print(f"  ⚠️ 未知策略: {name}，使用默认趋势跟踪")
        return TrendStrategy()


def print_separator(title=""):
    if title:
        print(f"\n{'='*60}")
        print(f"  {title}")
        print(f"{'='*60}")


# ═══════════════════════════════
# 主循环
# ═══════════════════════════════

def run_paper_mode(cfg, logger):
    """
    Paper 模式主循环

    流程:
      for each bar in data:
        1. 策略生成信号
        2. 风控检查
        3. 模拟成交（PaperExchange）
        4. 日志记录
        5. 更新风控状态
    """
    print_banner('paper')

    # ── 初始化 ──
    initial_capital = int(cfg._get_env('INITIAL_CAPITAL', '10000'))
    symbol = cfg.strategies[0].symbols[0] if cfg.strategies else 'BTC/USDT'
    timeframe = cfg.strategies[0].timeframe if cfg.strategies else '1h'

    # 成本模型
    liquidity_tier = CostModel.classify_liquidity(symbol)
    cm = CostModel(OrderType.MAKER if cfg.risk.default_order_type == 'limit' else OrderType.TAKER,
                   liquidity_tier)

    # 交易所
    exchange = create_exchange('paper', initial_capital, cfg, cm, logger)

    # 风控
    risk_mgr = RiskManager(initial_capital, cfg.risk, logger)

    # 加载数据
    df = load_data(symbol, timeframe)
    if df is None:
        return

    # 创建策略
    strat_cfg = cfg.strategies[0]
    strategy = create_strategy(strat_cfg.name, strat_cfg.params)
    strategy.precompute(df)
    print(f"  ✅ 策略: {strategy.name}")

    # 数据源
    feed = HistoricalDataFeed(df=df.copy(), replay_speed=0)
    feed.initialize()

    # ── 日志: 系统启动 ──
    logger.system_startup(version="0.1.0", config={
        'mode': 'paper', 'symbol': symbol, 'timeframe': timeframe,
        'initial_capital': initial_capital, 'strategy': strategy.name,
        'order_type': cfg.risk.default_order_type,
    })

    # ── 主循环 ──
    print_separator("实盘模拟开始")
    print(f"  标的: {symbol} {timeframe}")
    print(f"  策略: {strategy.name}")
    print(f"  初始资金: {initial_capital:,.0f} USDT")
    print(f"  风控: 仓位上限 {cfg.risk.max_position_pct:.0%} | "
          f"日亏损熔断 {cfg.risk.daily_loss_limit_pct:.0%}")
    print(f"  订单类型: {cfg.risk.default_order_type} (Maker 低成本)")

    bar_count = 0
    signal_count = 0
    rejected_count = 0
    rejection_reasons = {}  # 追踪拒绝原因
    heartbeat_interval = cfg.system.heartbeat_interval_seconds
    last_heartbeat = time.time()

    try:
        while feed.has_data():
            bar = feed.get_next_bar()
            if bar is None:
                break
            bar_count += 1

            # ── 更新持仓市价 ──
            exchange.update_market_prices({symbol: bar['close']})

            # ── 策略信号 ──
            result = strategy.on_bar(
                pd.Series(bar, name=bar['timestamp']), bar_count - 1
            )

            if result is not None:
                signal_count += 1

                # ── 风控检查 ──
                size_pct = cfg.risk.max_position_pct
                can_execute, suggested_size, reason = risk_mgr.quick_check(
                    symbol=symbol,
                    side=result.get('side', 'long'),
                    size_pct=size_pct,
                    signal_price=result['price'],
                    bar=bar,
                    order_type=cfg.risk.default_order_type,
                )

                if not can_execute:
                    rejected_count += 1
                    rejection_reasons[reason] = rejection_reasons.get(reason, 0) + 1
                else:
                    # ── 执行订单 ──
                    if result['action'] == 'entry':
                        existing = exchange.get_positions()
                        if not any(p.symbol == symbol for p in existing):
                            current_eq = risk_mgr.state.current_equity
                            quantity = (current_eq * suggested_size) / result['price']
                            exchange.submit_order(
                                symbol=symbol, side=result['side'], action='entry',
                                order_type=cfg.risk.default_order_type,
                                price=result['price'], quantity=quantity,
                                strategy_name=strategy.name, current_bar=bar,
                            )

                    elif result['action'] == 'exit':
                        existing = exchange.get_positions()
                        if existing:
                            pos = existing[0]
                            order = exchange.submit_order(
                                symbol=symbol, side=result['side'], action='exit',
                                order_type=cfg.risk.default_order_type,
                                price=result['price'], quantity=pos.quantity,
                                strategy_name=strategy.name, current_bar=bar,
                            )
                            if order and order.status.value == 'filled':
                                entry_price = pos.entry_price
                                exit_price = order.fill_price
                                side = pos.side
                                net_return = ((exit_price - entry_price) / entry_price * 100
                                              if side == 'long' else
                                              (entry_price - exit_price) / entry_price * 100)
                                logger.trade_closed(
                                    order_id=order.order_id, entry_price=entry_price,
                                    exit_price=exit_price, side=side,
                                    net_return_pct=net_return,
                                    exit_reason=result.get('reason', 'signal'), bars_held=0,
                                )
                                risk_mgr.update_after_trade(net_return)

            # ── 每根 bar 结束时：撮合挂单 + 同步风控净值 ──
            filled = exchange.check_pending_orders(bar, bar_count)
            for order in filled:
                if order.action.value == 'exit' and hasattr(order, '_entry_price'):
                    entry_price = order._entry_price
                    exit_price = order.fill_price
                    net_return = ((exit_price - entry_price) / entry_price * 100
                                  if order.side == 'long' else
                                  (entry_price - exit_price) / entry_price * 100)
                    logger.trade_closed(
                        order_id=order.order_id, entry_price=entry_price,
                        exit_price=exit_price, side=order.side,
                        net_return_pct=net_return,
                        exit_reason='signal', bars_held=0,
                    )
                    risk_mgr.update_after_trade(net_return)
            balance = exchange.get_balance()
            risk_mgr.update_equity(balance.total_equity)

            # ── 心跳日志 ──
            if time.time() - last_heartbeat > heartbeat_interval:
                balance = exchange.get_balance()
                positions = exchange.get_positions()
                logger.system_heartbeat(
                    uptime_seconds=logger.uptime_seconds(),
                    active_positions=len(positions),
                    equity=balance.total_equity,
                )
                last_heartbeat = time.time()

            # ── 进度 ──
            if bar_count % 1000 == 0:
                balance = exchange.get_balance()
                print(f"   ... {bar_count}/{feed.total_bars()} bars | "
                      f"净值 ${balance.total_equity:,.0f} | "
                      f"{signal_count} 信号 | {rejected_count} 被风控拒绝")

    except KeyboardInterrupt:
        print("\n  ⚠️ 用户中断")
    except Exception as e:
        print(f"\n  ❌ 异常: {e}")
        import traceback
        traceback.print_exc()
        logger.system_error(type(e).__name__, str(e), traceback.format_exc())

    finally:
        # ── 关闭 ──
        uptime = logger.uptime_seconds()
        balance = exchange.get_balance()
        final_equity = balance.total_equity
        total_return = (final_equity / initial_capital - 1) * 100

        logger.pnl_daily_summary(
            date=datetime.now(timezone.utc).strftime('%Y-%m-%d'),
            start_equity=initial_capital,
            end_equity=final_equity,
            num_trades=len(exchange.order_history),
        )
        logger.system_shutdown(reason="complete", uptime_seconds=uptime)

        # ── 打印最终报告 ──
        print_separator("运行结束 — 最终报告")
        print(f"  运行时长: {uptime/3600:.1f} 小时")
        print(f"  处理 K 线: {bar_count:,}")
        print(f"  策略信号: {signal_count}")
        print(f"  风控拒绝: {rejected_count}")
        if rejection_reasons:
            print(f"  拒绝原因分布:")
            for reason, cnt in sorted(rejection_reasons.items(), key=lambda x: -x[1])[:5]:
                print(f"    • {reason}: {cnt} 次")
        print(f"  最终净值: ${final_equity:,.2f} "
              f"({total_return:+.2f}%)")
        print(f"  总订单数: {len(exchange.order_history)}")
        print(f"  挂单剩余: {len(exchange.orders)}")

        # 今日交易摘要
        summary = logger.get_today_summary()
        if summary['num_trades'] > 0:
            print(f"\n  今日交易: {summary['num_trades']} 笔")
            print(f"  胜率: {summary['win_rate']:.1f}%")
            print(f"  累计收益: {summary['total_return_pct']:+.2f}%")

        risk_mgr.print_status()


# ═══════════════════════════════
# 入口
# ═══════════════════════════════

def main():
    parser = argparse.ArgumentParser(description='加密量化交易系统 — 实盘调度入口')
    parser.add_argument('--mode', default='paper', choices=['paper', 'live'],
                        help='运行模式: paper（模拟盘）| live（实盘）')
    parser.add_argument('--symbol', default='BTC', help='交易对')
    parser.add_argument('--timeframe', default='1h', help='K 线周期')
    parser.add_argument('--capital', type=int, default=10000, help='初始资金 (USDT)')
    parser.add_argument('--check-readiness', action='store_true', help='检查部署就绪状态')
    parser.add_argument('--init-env', action='store_true', help='创建 .env.example 模板')
    parser.add_argument('--slow', action='store_true', help='带延迟回放（模拟真实交易节奏）')
    args = parser.parse_args()

    # ── 初始化 .env ──
    if args.init_env:
        create_env_template(DATA_DIR)
        return

    # ── 加载配置 ──
    cfg = ConfigManager(mode=args.mode, project_root=DATA_DIR)

    # ── 就绪检查 ──
    if args.check_readiness:
        cfg.print_readiness()
        return

    # ── 配置展示 ──
    cfg.display()

    # ── 确认 live 模式 ──
    if args.mode == 'live' and not cfg.exchange.api_key:
        print("\n⚠️ LIVE 模式需要币安 API Key，当前未配置。")
        print("   请: cp .env.example .env → 编辑 .env 填入 API Key")
        print("   或使用: python run_live.py --mode paper")
        if input("\n   是否回退到 paper 模式？[Y/n] ").strip().lower() == 'n':
            return
        args.mode = 'paper'
        cfg.mode = 'paper'

    # ── 初始化日志 ──
    logger = LiveLogger(
        log_dir=cfg.log.log_dir,
        mode=cfg.mode,
        keep_days=cfg.log.keep_days,
    )

    # ── 运行 ──
    if args.mode == 'paper':
        run_paper_mode(cfg, logger)
    else:
        # live 模式 — 需要实时数据源 + 真实交易所
        print("🚧 LIVE 模式 — 待 API key 和实时数据源就绪")
        print("   当前请使用: python run_live.py --mode paper")

    print(f"\n✅ Day 7 实盘壳运行完成")
    print(f"   日志目录: {cfg.log.log_dir}/")
    print(f"   查看日志: ls {cfg.log.log_dir}/")


if __name__ == '__main__':
    main()
