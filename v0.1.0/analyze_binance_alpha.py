"""
币安 Alpha 公告回溯分析 — 唯一有历史数据可做统计的外部信号

分析逻辑:
  1. 从币安公告页面 / 公开数据源 获取历史 Listing 事件
  2. 对每个 Listing 事件，取公告时刻前后 N 天的价格数据
  3. 计算: 1h/24h/7d/30d 的价格变化、胜率、最大回撤
  4. 统计: 按季度/按板块分组，找出规律

数据来源:
  - Binance API: 公告端点 (免费公开)
  - 价格数据: 我们有 clean/ 的 OHLCV (目前只有 BTC/ETH/SOL)
  - 对于新上币种: 需要补拉该币种的上线后数据 (ccxt 公开 API 免费)

输出:
  data/external_signals/binance_alpha/listing_analysis_YYYYMMDD.json

用法:
  python analyze_binance_alpha.py              # 完整分析
  python analyze_binance_alpha.py --mock       # 用模拟公告数据测试框架
  python analyze_binance_alpha.py --symbols BTC,ETH,SOL  # 只分析指定币种
"""

import os, sys, json, time
from datetime import datetime, timedelta, timezone
from dataclasses import dataclass, field, asdict
from typing import Optional, Dict, List, Any
from pathlib import Path

if sys.platform == 'win32':
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# ── 路径 ──
V01 = Path(__file__).resolve().parent
WEB3_ROOT = V01.parent
CLEAN_DIR = V01 / 'clean'
OUTPUT_DIR = WEB3_ROOT / 'data' / 'external_signals' / 'binance_alpha'
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

import pandas as pd
import numpy as np


# ═══════════════════════════════
# 数据结构
# ═══════════════════════════════

@dataclass
class ListingEvent:
    """币安上币事件"""
    symbol: str              # 'BTC/USDT'
    base_asset: str          # 'BTC'
    listing_time: datetime   # UTC
    announcement_time: datetime  # UTC (通常比 listing_time 早几小时到几天)
    category: str = ""       # 'DeFi', 'Layer2', 'Meme', 'AI', 'Infra', 'Other'
    source: str = "binance_announcement"

@dataclass
class ListingAnalysis:
    """单个上币事件的价格表现"""
    event: ListingEvent
    # 上线后各时间窗口表现
    return_1h: Optional[float] = None     # %
    return_24h: Optional[float] = None    # %
    return_7d: Optional[float] = None     # %
    return_30d: Optional[float] = None    # %
    max_drawdown_7d: Optional[float] = None
    max_pump_7d: Optional[float] = None   # 最大涨幅
    volume_spike_ratio: Optional[float] = None
    # 数据质量
    data_available: bool = False
    notes: str = ""


# ═══════════════════════════════
# 公告数据获取
# ═══════════════════════════════

def fetch_binance_listings_mock() -> List[ListingEvent]:
    """
    模拟币安上币公告（2025-2026 部分真实事件）

    后续可替换为真实 API 爬取:
      https://api.binance.com/api/v3/exchangeInfo (当前交易对)
      https://www.binance.com/en/support/announcement/c-48 (公告页面)
    """
    mock_events = [
        # 用我们有数据的币种做测试 (BTC/ETH/SOL)
        ListingEvent('BTC/USDT', 'BTC',
                     datetime(2025, 9, 1, 8, 0, tzinfo=timezone.utc),
                     datetime(2025, 8, 30, 10, 0, tzinfo=timezone.utc),
                     'Layer1'),
        ListingEvent('ETH/USDT', 'ETH',
                     datetime(2025, 10, 1, 8, 0, tzinfo=timezone.utc),
                     datetime(2025, 9, 28, 14, 0, tzinfo=timezone.utc),
                     'Layer1'),
        ListingEvent('SOL/USDT', 'SOL',
                     datetime(2025, 11, 1, 9, 0, tzinfo=timezone.utc),
                     datetime(2025, 10, 29, 8, 0, tzinfo=timezone.utc),
                     'Layer1'),
        # 2025 Q3
        ListingEvent('DOGE/USDT', 'DOGE',
                     datetime(2025, 8, 15, 8, 0, tzinfo=timezone.utc),
                     datetime(2025, 8, 13, 10, 0, tzinfo=timezone.utc),
                     'Meme'),
        ListingEvent('PEPE/USDT', 'PEPE',
                     datetime(2025, 9, 1, 10, 0, tzinfo=timezone.utc),
                     datetime(2025, 8, 28, 14, 0, tzinfo=timezone.utc),
                     'Meme'),
        ListingEvent('WIF/USDT', 'WIF',
                     datetime(2025, 9, 20, 9, 0, tzinfo=timezone.utc),
                     datetime(2025, 9, 18, 8, 0, tzinfo=timezone.utc),
                     'Meme'),
        # 2025 Q4
        ListingEvent('ARB/USDT', 'ARB',
                     datetime(2025, 10, 10, 8, 0, tzinfo=timezone.utc),
                     datetime(2025, 10, 8, 12, 0, tzinfo=timezone.utc),
                     'Layer2'),
        ListingEvent('OP/USDT', 'OP',
                     datetime(2025, 11, 5, 9, 0, tzinfo=timezone.utc),
                     datetime(2025, 11, 3, 15, 0, tzinfo=timezone.utc),
                     'Layer2'),
        ListingEvent('APT/USDT', 'APT',
                     datetime(2025, 12, 12, 10, 0, tzinfo=timezone.utc),
                     datetime(2025, 12, 10, 8, 0, tzinfo=timezone.utc),
                     'Layer1'),
        # 2026 Q1
        ListingEvent('TIA/USDT', 'TIA',
                     datetime(2026, 1, 20, 8, 0, tzinfo=timezone.utc),
                     datetime(2026, 1, 18, 11, 0, tzinfo=timezone.utc),
                     'Infra'),
        ListingEvent('STRK/USDT', 'STRK',
                     datetime(2026, 2, 14, 9, 0, tzinfo=timezone.utc),
                     datetime(2026, 2, 12, 14, 0, tzinfo=timezone.utc),
                     'Layer2'),
        ListingEvent('JUP/USDT', 'JUP',
                     datetime(2026, 3, 8, 10, 0, tzinfo=timezone.utc),
                     datetime(2026, 3, 6, 16, 0, tzinfo=timezone.utc),
                     'DeFi'),
        # 2026 Q2
        ListingEvent('W/USDT', 'W',
                     datetime(2026, 4, 18, 8, 0, tzinfo=timezone.utc),
                     datetime(2026, 4, 16, 9, 0, tzinfo=timezone.utc),
                     'Infra'),
        ListingEvent('PYTH/USDT', 'PYTH',
                     datetime(2026, 5, 22, 10, 0, tzinfo=timezone.utc),
                     datetime(2026, 5, 20, 11, 0, tzinfo=timezone.utc),
                     'Infra'),
        ListingEvent('SEI/USDT', 'SEI',
                     datetime(2026, 6, 15, 8, 0, tzinfo=timezone.utc),
                     datetime(2026, 6, 13, 10, 0, tzinfo=timezone.utc),
                     'Layer1'),
    ]
    return mock_events


def fetch_binance_listings_real() -> List[ListingEvent]:
    """
    从币安 API 获取真实 Listing 事件

    思路:
      1. 用 ccxt 加载所有 USDT 交易对
      2. 对每个交易对，拉最早可用的 K 线时间 => 推断上线时间
      3. 从公告页面补全公告时间

    注意: 这种方法只能找到"当前还在交易的"币种，已下架的找不到。
    """
    events = []
    try:
        import ccxt
        ex = ccxt.binance({'enableRateLimit': True, 'timeout': 30000})
        if hasattr(ex, 'session'):
            ex.session.proxies.update({
                'http': 'http://127.0.0.1:7890',
                'https': 'http://127.0.0.1:7890',
            })
        markets = ex.load_markets()
        usdt_pairs = {k: v for k, v in markets.items() if k.endswith('/USDT')}

        for symbol, info in usdt_pairs.items():
            if info.get('spot', False) and info.get('active', False):
                try:
                    # 拉最早的数据来推断上线时间
                    since = ex.parse8601('2025-01-01T00:00:00Z')
                    ohlcv = ex.fetch_ohlcv(symbol, '1d', since=since, limit=1)
                    if ohlcv and len(ohlcv) > 0:
                        first_ts = datetime.fromtimestamp(ohlcv[0][0] / 1000, tz=timezone.utc)
                        base = symbol.split('/')[0]
                        events.append(ListingEvent(
                            symbol=symbol, base_asset=base,
                            listing_time=first_ts,
                            announcement_time=first_ts - timedelta(days=2),
                            source='binance_api_inferred',
                        ))
                except Exception:
                    pass
                time.sleep(0.1)
    except ImportError:
        pass
    except Exception as e:
        print(f"  [WARN] 真实 API 拉取失败: {e}")

    return events


# ═══════════════════════════════
# 价格数据分析
# ═══════════════════════════════

def load_price_data(symbol: str, timeframe: str = '1h') -> Optional[pd.DataFrame]:
    """加载清洗后的价格数据"""
    symbol_safe = symbol.replace('/', '')
    filepath = CLEAN_DIR / f'{symbol_safe}_{timeframe}.parquet'
    if filepath.exists():
        return pd.read_parquet(filepath)
    return None


def analyze_listing(event: ListingEvent, df: pd.DataFrame) -> ListingAnalysis:
    """
    对单个 Listing 事件做价格表现分析

    df: 该币种的 K 线数据 (1h)
    """
    analysis = ListingAnalysis(event=event)

    listing_ts = pd.Timestamp(event.listing_time)
    if listing_ts.tz is None:
        listing_ts = listing_ts.tz_localize('UTC')

    # 上线价: 上线后第一根 K 线的收盘价
    after_listing = df[df.index >= listing_ts]
    if len(after_listing) == 0:
        analysis.notes = '无上线后数据'
        return analysis

    analysis.data_available = True
    entry_price = after_listing.iloc[0]['close']

    # 各时间窗口收益
    windows = {'1h': 1, '24h': 24, '7d': 168, '30d': 720}
    for label, bars in windows.items():
        if len(after_listing) >= bars:
            exit_price = after_listing.iloc[bars - 1]['close']
            ret = (exit_price - entry_price) / entry_price * 100
            setattr(analysis, f'return_{label}', round(ret, 2))

    # 7 日最大回撤 & 最大涨幅
    if len(after_listing) >= 168:
        window = after_listing.iloc[:168]
        peak = window['high'].cummax()
        drawdown = (window['close'] - peak) / peak * 100
        analysis.max_drawdown_7d = round(drawdown.min(), 2)

        pump = (window['high'] - entry_price) / entry_price * 100
        analysis.max_pump_7d = round(pump.max(), 2)

    # 成交量突增
    if len(after_listing) >= 169:  # 上线前 1h + 上线后 7d
        pre_volume = df[df.index < listing_ts].tail(1)['volume'].values
        post_volume = after_listing.iloc[:168]['volume'].mean()
        if len(pre_volume) > 0 and pre_volume[0] > 0:
            analysis.volume_spike_ratio = round(post_volume / pre_volume[0], 1)

    return analysis


# ═══════════════════════════════
# 统计汇总
# ═══════════════════════════════

def summarize(analyses: List[ListingAnalysis]) -> dict:
    """汇总所有 Listing 的分析结果"""
    valid = [a for a in analyses if a.data_available]

    def stats(values):
        if not values:
            return None
        arr = np.array(values)
        return {
            'count': len(arr),
            'mean': round(arr.mean(), 2),
            'median': round(np.median(arr), 2),
            'std': round(arr.std(), 2),
            'min': round(arr.min(), 2),
            'max': round(arr.max(), 2),
            'win_rate': round((arr > 0).sum() / len(arr) * 100, 1),
        }

    # 按类别分组
    by_category = {}
    for a in valid:
        cat = a.event.category or 'Other'
        if cat not in by_category:
            by_category[cat] = []
        by_category[cat].append(a)

    category_stats = {}
    for cat, cat_analyses in by_category.items():
        rets = [a.return_7d for a in cat_analyses if a.return_7d is not None]
        category_stats[cat] = {
            'count': len(cat_analyses),
            'return_7d': stats(rets),
        }

    all_1h = [a.return_1h for a in valid if a.return_1h is not None]
    all_24h = [a.return_24h for a in valid if a.return_24h is not None]
    all_7d = [a.return_7d for a in valid if a.return_7d is not None]
    all_30d = [a.return_30d for a in valid if a.return_30d is not None]

    return {
        'analysis_time': datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ'),
        'total_events': len(analyses),
        'with_data': len(valid),
        'without_data': len(analyses) - len(valid),
        'returns': {
            '1h': stats(all_1h),
            '24h': stats(all_24h),
            '7d': stats(all_7d),
            '30d': stats(all_30d),
        },
        'by_category': category_stats,
        'individual': [asdict(a) for a in analyses],
    }


# ═══════════════════════════════
# 公告后趋势分类 (Wallet 0xb319 策略内化)
# ═══════════════════════════════

class AnnouncementTrendType:
    """公告后趋势分类"""
    SUSTAINED_PUMP = "sustained_pump"        # 持续上涨 — 趋势策略可交易
    SPIKE_AND_FADE = "spike_and_fade"        # 脉冲回落 — 不适合趋势跟踪
    DUMP = "dump"                             # 持续下跌 — 做空信号
    NO_REACTION = "no_reaction"               # 无明显反应


def classify_announcement_trend(analysis: ListingAnalysis) -> dict:
    """
    将单个 Listing 分析结果分类为趋势类型

    判断逻辑:
      1. 如果 return_1h > 5% 且 return_24h < return_1h*0.3 → 脉冲回落
      2. 如果 return_1h > 3% 且 return_24h > return_1h*0.6 → 持续上涨
      3. 如果 return_24h < -5% → 持续下跌
      4. 否则 → 无明显反应

    来源: Wallet 0xb319 — 79% 胜率的公告套利策略。
          我们不学他的秒级狙击，但可以从公告后 1h-24h 的趋势持续性中提取信号。

    返回:
      dict with trend_type, confidence, tradeable, note
    """
    ret_1h = analysis.return_1h
    ret_24h = analysis.return_24h
    ret_7d = analysis.return_7d

    if ret_1h is None:
        return {
            'trend_type': AnnouncementTrendType.NO_REACTION,
            'confidence': 0.0,
            'tradeable': False,
            'note': '数据不足，无法分类',
        }

    # 判断 1: 持续上涨
    if ret_1h > 3.0:
        if ret_24h is not None and ret_24h > ret_1h * 0.6:
            # 24h 保持了一小时涨幅的 60% 以上 → 趋势持续
            confidence = min(90, 50 + ret_24h * 2)
            return {
                'trend_type': AnnouncementTrendType.SUSTAINED_PUMP,
                'confidence': round(confidence, 1),
                'tradeable': True,
                'note': f'公告后持续上涨: 1h={ret_1h:+.1f}%, 24h={ret_24h:+.1f}%',
                'suggested_action': '趋势策略可追多，止损设在公告前低点',
            }
        elif ret_24h is not None and ret_24h < ret_1h * 0.3:
            # 24h 涨幅不到 1h 的 30% → 脉冲回落
            return {
                'trend_type': AnnouncementTrendType.SPIKE_AND_FADE,
                'confidence': round(min(80, 40 + abs(ret_1h - (ret_24h or 0)) * 3), 1),
                'tradeable': False,
                'note': f'脉冲后回落: 1h={ret_1h:+.1f}%, 24h={ret_24h:+.1f}% → 不适合追',
                'suggested_action': '不追。等回调到公告前水平再看',
            }

    # 判断 2: 持续下跌
    if ret_24h is not None and ret_24h < -5.0:
        confidence = min(85, 40 + abs(ret_24h) * 2)
        return {
            'trend_type': AnnouncementTrendType.DUMP,
            'confidence': round(confidence, 1),
            'tradeable': True,
            'note': f'公告后持续下跌: 24h={ret_24h:+.1f}%',
            'suggested_action': '可考虑做空（如果 ALLOW_SHORT=True），或回避',
        }

    # 判断 3: 一周趋势补充判断（当 1h/24h 都不够明显时）
    if ret_7d is not None:
        if ret_7d > 15:
            return {
                'trend_type': AnnouncementTrendType.SUSTAINED_PUMP,
                'confidence': 60.0,
                'tradeable': True,
                'note': f'公告后周线走强: 7d={ret_7d:+.1f}%',
                'suggested_action': '中期趋势确认，可分批建仓',
            }
        elif ret_7d < -15:
            return {
                'trend_type': AnnouncementTrendType.DUMP,
                'confidence': 60.0,
                'tradeable': True,
                'note': f'公告后周线走弱: 7d={ret_7d:+.1f}%',
                'suggested_action': '回避或做空',
            }

    # 默认: 无明显反应
    return {
        'trend_type': AnnouncementTrendType.NO_REACTION,
        'confidence': 30.0,
        'tradeable': False,
        'note': f'公告后无明显趋势: 1h={ret_1h:+.1f}%',
    }


def generate_trend_signals(analyses: List[ListingAnalysis]) -> List[dict]:
    """
    将公告后趋势分类转化为策略信号

    输出格式与 strategy_orchestrator.on_bar() 的信号格式兼容:
      {
        'symbol': 'BTC/USDT',
        'source': 'binance_announcement',
        'trend_type': 'sustained_pump',
        'confidence': 75.0,
        'tradeable': True,
        'suggested_action': '...',
        'return_1h': +8.5,
        'return_24h': +12.3,
        'category': 'Layer1',
      }
    """
    signals = []
    for analysis in analyses:
        if not analysis.data_available:
            continue
        classification = classify_announcement_trend(analysis)
        if classification['tradeable']:
            signals.append({
                'symbol': analysis.event.symbol,
                'source': 'binance_announcement',
                'trend_type': classification['trend_type'],
                'confidence': classification['confidence'],
                'tradeable': classification['tradeable'],
                'suggested_action': classification.get('suggested_action', ''),
                'return_1h': analysis.return_1h,
                'return_24h': analysis.return_24h,
                'return_7d': analysis.return_7d,
                'category': analysis.event.category,
                'listing_time': analysis.event.listing_time.strftime('%Y-%m-%d'),
            })

    # 按 confidence 降序排列
    signals.sort(key=lambda s: s['confidence'], reverse=True)
    return signals


# ═══════════════════════════════
# 主入口
# ═══════════════════════════════

def run(mock=False):
    print("=" * 60)
    print("  币安 Alpha 公告回溯分析")
    print("=" * 60)

    # 获取 Listing 事件
    if mock:
        print("\n[模式] 模拟公告数据")
        events = fetch_binance_listings_mock()
    else:
        print("\n[模式] 真实 API")
        events = fetch_binance_listings_real()

    print(f"  公告事件: {len(events)} 个")

    if not events:
        # 回退到模拟数据
        print("  [INFO] 真实 API 无数据，回退到模拟公告")
        events = fetch_binance_listings_mock()

    # 逐事件分析
    analyses = []
    for event in events:
        df = load_price_data(event.symbol, '1h')
        if df is not None:
            analysis = analyze_listing(event, df)
            ret_7d = analysis.return_7d
            ret_str = f'{ret_7d:+.1f}%' if ret_7d is not None else 'N/A'
            print(f"  {event.symbol:<14} {event.listing_time.strftime('%Y-%m-%d'):<12} "
                  f"{event.category:<8} 7d={ret_str}")
        else:
            analysis = ListingAnalysis(event=event, notes='本地无该币种 K 线数据')
            print(f"  {event.symbol:<14} {event.listing_time.strftime('%Y-%m-%d'):<12} "
                  f"[SKIP] 无本地数据")
        analyses.append(analysis)

    # 汇总
    summary = summarize(analyses)

    # 输出
    output_path = OUTPUT_DIR / f'listing_analysis_{datetime.now().strftime("%Y%m%d")}.json'
    with open(output_path, 'w', encoding='utf-8') as f:
        json.dump(summary, f, ensure_ascii=False, indent=2, default=str)

    print(f"\n[汇总]")
    returns = summary['returns']
    for window in ['1h', '24h', '7d', '30d']:
        s = returns.get(window)
        if s and s['count'] > 0:
            print(f"  {window:>4s}: avg={s['mean']:+.1f}%  median={s['median']:+.1f}%  "
                  f"win={s['win_rate']:.0f}%  n={s['count']}")

    if summary['by_category']:
        print(f"\n[按类别]")
        for cat, stats in summary['by_category'].items():
            s = stats.get('return_7d')
            if s:
                print(f"  {cat:<10}: 7d avg={s['mean']:+.1f}%  win={s['win_rate']:.0f}%  n={s['count']}")

    print(f"\n[输出] {output_path}")
    return summary


if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument('--mock', action='store_true')
    args = parser.parse_args()
    run(mock=args.mock)
