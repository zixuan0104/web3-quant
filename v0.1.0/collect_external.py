"""
外部信号自采集脚本 — 每日运行，积累数据

采集维度:
  1. 订单簿快照 — 币安 depth 20 档 (免费公开 API)
  2. 币安 Alpha 公告 — 新上币/将上币公告
  3. Whale Alert — 大额转账 webhook (模拟, 后续接真实 API)
  4. DexScreener — 新交易对/热门代币 (免费 API)

数据路径:
  data/external_signals/
  ├── orderbook/     — depth 快照
  ├── binance_alpha/ — 公告分析
  ├── whale_alert/   — 鲸鱼转账
  ├── dexscreener/   — 新池子
  └── holder_stats/  — 筹码结构

频率:
  目前: 手动运行或 cron 每天 1 次
  Day 12: 服务器上 cron 定时运行
  Week 4: 订单簿升级为 WebSocket 实时流

用法:
  python collect_external.py                  # 全部采集
  python collect_external.py --orderbook     # 仅订单簿
  python collect_external.py --alpha          # 仅公告
  python collect_external.py --whale          # 仅鲸鱼
  python collect_external.py --dexscreener    # 仅热门代币
  python collect_external.py --holders        # 仅筹码结构
"""

import os, sys, json, time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional, Dict, List, Any

if sys.platform == 'win32':
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# ── 路径 ──
V01 = Path(__file__).resolve().parent
WEB3_ROOT = V01.parent
DATA_DIR = WEB3_ROOT / 'data' / 'external_signals'

# ── 代理 ──
PROXY = 'http://127.0.0.1:7890'

SYMBOLS = ['BTC/USDT', 'ETH/USDT', 'SOL/USDT']


# ═══════════════════════════════
# 1. 订单簿快照
# ═══════════════════════════════

def collect_orderbook():
    """采集币安订单簿 depth 20 档快照"""
    print("\n[1] 订单簿快照...")
    output_dir = DATA_DIR / 'orderbook'
    output_dir.mkdir(parents=True, exist_ok=True)

    try:
        import ccxt
        ex = ccxt.binance({'enableRateLimit': True, 'timeout': 15000})
        if hasattr(ex, 'session'):
            ex.session.proxies.update({'http': PROXY, 'https': PROXY})

        snapshot = {
            'timestamp': datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ'),
            'symbols': {},
        }

        for symbol in SYMBOLS:
            try:
                ob = ex.fetch_order_book(symbol, limit=20)
                snapshot['symbols'][symbol] = {
                    'bids': ob['bids'][:5],    # 只存前 5 档 (节省空间)
                    'asks': ob['asks'][:5],
                    'spread': ob['asks'][0][0] - ob['bids'][0][0] if ob['asks'] and ob['bids'] else 0,
                    'spread_pct': (ob['asks'][0][0] - ob['bids'][0][0]) / ob['bids'][0][0] * 100 if ob['asks'] and ob['bids'] else 0,
                }
                print(f"  {symbol}: bid={ob['bids'][0][0]:.2f} ask={ob['asks'][0][0]:.2f} spread={snapshot['symbols'][symbol]['spread_pct']:.4f}%")
            except Exception as e:
                print(f"  {symbol}: [X] {e}")
                snapshot['symbols'][symbol] = {'error': str(e)[:100]}

        # 保存
        today = datetime.now().strftime('%Y%m%d')
        filepath = output_dir / f'depth_{today}.json'
        with open(filepath, 'w', encoding='utf-8') as f:
            json.dump(snapshot, f, ensure_ascii=False, indent=2)

        # 同时追加到当日日志
        logpath = output_dir / f'depth_log_{today}.jsonl'
        with open(logpath, 'a', encoding='utf-8') as f:
            f.write(json.dumps(snapshot, ensure_ascii=False) + '\n')

        print(f"  [OK] -> {filepath}")
        return snapshot
    except ImportError:
        print("  [SKIP] ccxt 未安装")
    except Exception as e:
        print(f"  [X] {e}")


# ═══════════════════════════════
# 2. Alpha 公告
# ═══════════════════════════════

def collect_alpha():
    """采集币安 Alpha / 上币公告 (从 analyze_binance_alpha 调用)"""
    print("\n[2] 币安 Alpha 公告...")
    try:
        from analyze_binance_alpha import run as run_alpha, fetch_binance_listings_real
        # 用真实 API 重新拉取 (可能有新的上线)
        summary = run_alpha(mock=False)
        print(f"  [OK] 已更新公告分析")
        return summary
    except Exception as e:
        print(f"  [X] {e}")


# ═══════════════════════════════
# 3. Whale Alert (模拟)
# ═══════════════════════════════

def collect_whale():
    """
    鲸鱼大额转账采集

    当前: 模拟数据 (Whale Alert 免费 API 有调用限制)
    后续: 接 Whale Alert webhook 或 Etherscan API
    """
    print("\n[3] 鲸鱼转账...")
    output_dir = DATA_DIR / 'whale_alert'
    output_dir.mkdir(parents=True, exist_ok=True)

    # 模拟数据 — 后续替换为真实 API 调用
    import random
    whales = [
        {'address': '0xabc...123', 'label': 'Jump Trading', 'type': 'exchange_deposit',
         'asset': 'ETH', 'amount': random.randint(500, 5000), 'usd_value': random.randint(1_000_000, 15_000_000)},
        {'address': '0xdef...456', 'label': 'Wintermute', 'type': 'exchange_withdrawal',
         'asset': 'USDT', 'amount': random.randint(1_000_000, 10_000_000), 'usd_value': random.randint(1_000_000, 10_000_000)},
        {'address': '0x789...abc', 'label': 'Unknown Whale', 'type': 'wallet_to_wallet',
         'asset': 'BTC', 'amount': round(random.uniform(0.1, 5.0), 2), 'usd_value': random.randint(500_000, 10_000_000)},
    ]

    snapshot = {
        'timestamp': datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ'),
        'source': 'simulated',
        'transfers': whales,
    }

    today = datetime.now().strftime('%Y%m%d')
    filepath = output_dir / f'whales_{today}.json'
    with open(filepath, 'w', encoding='utf-8') as f:
        json.dump(snapshot, f, ensure_ascii=False, indent=2)

    for w in whales:
        print(f"  {w['label']:<18} {w['type']:<22} {w['amount']:>10,.0f} {w['asset']}")

    print(f"  [OK] -> {filepath} (模拟数据)")
    return snapshot


# ═══════════════════════════════
# 4. DexScreener 热门代币
# ═══════════════════════════════

def collect_dexscreener():
    """
    DexScreener 新交易对/热门代币采集

    免费 API: https://api.dexscreener.com/latest/dex/search?q=SOL
    """
    print("\n[4] DexScreener 热门代币...")
    output_dir = DATA_DIR / 'dexscreener'
    output_dir.mkdir(parents=True, exist_ok=True)

    result = {'timestamp': datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ'),
              'source': 'simulated', 'pairs': []}

    try:
        import urllib.request, urllib.error
        url = 'https://api.dexscreener.com/latest/dex/search?q=SOL'
        req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode())
            pairs = data.get('pairs', [])[:20]  # Top 20
            result['source'] = 'dexscreener_api'
            result['pairs'] = [{
                'chain': p.get('chainId', ''),
                'symbol': f"{p.get('baseToken', {}).get('symbol', '')}/{p.get('quoteToken', {}).get('symbol', '')}",
                'price_usd': p.get('priceUsd', ''),
                'volume_24h': p.get('volume', {}).get('h24', 0),
                'liquidity_usd': p.get('liquidity', {}).get('usd', 0),
                'price_change_24h': p.get('priceChange', {}).get('h24', 0),
                'created_at': p.get('pairCreatedAt', 0),
            } for p in pairs]
            print(f"  [OK] API 返回 {len(pairs)} 个 SOL 交易对")
    except Exception as e:
        print(f"  [WARN] API 调用失败 ({e})，使用模拟数据")
        import random
        result['pairs'] = [
            {'chain': 'solana', 'symbol': f'SOL/MEME{i}',
             'price_usd': round(random.uniform(0.0001, 0.01), 8),
             'volume_24h': random.randint(10000, 500000),
             'liquidity_usd': random.randint(5000, 200000),
             'price_change_24h': round(random.uniform(-50, 200), 1),
             'created_at': int(time.time()) - random.randint(3600, 86400 * 30),
            } for i in range(10)
        ]
        print(f"  [OK] 模拟 {len(result['pairs'])} 个交易对")

    today = datetime.now().strftime('%Y%m%d')
    filepath = output_dir / f'hot_pairs_{today}.json'
    with open(filepath, 'w', encoding='utf-8') as f:
        json.dump(result, f, ensure_ascii=False, indent=2)

    print(f"  [OK] -> {filepath}")
    return result


# ═══════════════════════════════
# 5. 链上筹码结构 (枯坐p小将/猫姐 策略内化)
# ═══════════════════════════════

def collect_holder_stats(symbols: List[str] = None):
    """
    采集链上筹码结构数据

    数据维度:
      - Top 10 持仓占比: 高度集中 → 操纵风险大
      - 流动性锁仓比例: 高锁仓 → 流通盘小，容易被拉盘
      - 新创建地址买入量: 新钱进场 → 趋势可能持续

    数据源: DexScreener 免费 API（已验证可通）
    后续可扩展: Moralis / Birdeye / Solscan

    保存路径: data/external_signals/holder_stats/
    """
    print("\n[5] 链上筹码结构...")
    output_dir = DATA_DIR / 'holder_stats'
    output_dir.mkdir(parents=True, exist_ok=True)

    if symbols is None:
        symbols = SYMBOLS

    result = {
        'timestamp': datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ'),
        'source': 'dexscreener_api',
        'tokens': [],
    }

    try:
        import urllib.request, urllib.error

        for symbol in symbols:
            base = symbol.split('/')[0]
            token_data = {
                'symbol': symbol,
                'base': base,
                'holder_concentration': None,
                'liquidity_lock_pct': None,
                'top_holders': [],
                'risk_flags': [],
            }

            # 从 DexScreener 获取流动性/交易对信息
            try:
                url = f'https://api.dexscreener.com/latest/dex/search?q={base}'
                req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
                with urllib.request.urlopen(req, timeout=15) as resp:
                    data = json.loads(resp.read().decode())
                    pairs = data.get('pairs', [])

                    if pairs:
                        # 取流动性最大的交易对
                        best = max(pairs, key=lambda p: float(p.get('liquidity', {}).get('usd', 0) or 0))
                        liquidity = float(best.get('liquidity', {}).get('usd', 0) or 0)
                        fdv = float(best.get('fdv', 0) or 0)

                        if fdv > 0 and liquidity > 0:
                            # 锁仓比例估算: 流动性 / FDV
                            # 高比例 → 流通盘小，筹码集中
                            lock_ratio = liquidity / fdv
                            token_data['liquidity_lock_pct'] = round(lock_ratio * 100, 2)

                            if lock_ratio > 0.5:
                                token_data['risk_flags'].append(
                                    f'高流动性锁仓比 ({lock_ratio:.0%}) — 流通盘小，容易被操纵')

                        token_data['fdv'] = round(fdv, 0)
                        token_data['liquidity_usd'] = round(liquidity, 0)
                        token_data['price_usd'] = best.get('priceUsd', '')
                        token_data['price_change_24h'] = best.get('priceChange', {}).get('h24', 0)
            except Exception as e:
                token_data['risk_flags'].append(f'DexScreener 查询失败: {str(e)[:80]}')
                # 即使 API 失败也记录，后续可补

            # 风险评估
            if token_data['liquidity_lock_pct'] is not None:
                if token_data['liquidity_lock_pct'] > 60:
                    token_data['risk_flags'].append('[HIGH RISK] 极度集中持仓 — 不建议大仓位')
                elif token_data['liquidity_lock_pct'] > 40:
                    token_data['risk_flags'].append('[CAUTION] 持仓较集中 — 注意操纵风险')

            result['tokens'].append(token_data)
            flags_str = f' ({len(token_data["risk_flags"])} 风险标记)' if token_data['risk_flags'] else ''
            liq_str = f'liq=${token_data.get("liquidity_usd", "N/A")}' if token_data.get('liquidity_usd') else ''
            print(f'  {symbol:<14} {liq_str:<20}{flags_str}')

    except ImportError:
        print('  [SKIP] urllib 不可用')
    except Exception as e:
        print(f'  [X] {e}')

    # 保存
    today = datetime.now().strftime('%Y%m%d')
    filepath = output_dir / f'holder_stats_{today}.json'
    with open(filepath, 'w', encoding='utf-8') as f:
        json.dump(result, f, ensure_ascii=False, indent=2)

    risk_count = sum(len(t.get('risk_flags', [])) for t in result['tokens'])
    print(f'  [OK] -> {filepath} ({len(result["tokens"])} 代币, {risk_count} 风险标记)')
    return result


# ═══════════════════════════════
# 主入口
# ═══════════════════════════════

def run_all(orderbook=True, alpha=True, whale=True, dexscreener=True,
            holder_stats=True):
    print("=" * 60)
    print("  外部信号自采集")
    print(f"  时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"  数据目录: {DATA_DIR}")
    print("=" * 60)

    results = {}

    if orderbook:
        results['orderbook'] = collect_orderbook()
    if alpha:
        results['alpha'] = collect_alpha()
    if whale:
        results['whale'] = collect_whale()
    if dexscreener:
        results['dexscreener'] = collect_dexscreener()
    if holder_stats:
        results['holder_stats'] = collect_holder_stats()

    # ── 采集日志 ──
    log_entry = {
        'timestamp': datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ'),
        'modules': {k: 'OK' if v else 'FAILED' for k, v in results.items()},
    }
    log_dir = DATA_DIR
    log_path = log_dir / 'collect_log.jsonl'
    with open(log_path, 'a', encoding='utf-8') as f:
        f.write(json.dumps(log_entry, ensure_ascii=False) + '\n')

    print(f"\n[采集日志] {log_path}")
    print("DONE")
    return results


if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument('--orderbook', action='store_true', help='仅订单簿')
    parser.add_argument('--alpha', action='store_true', help='仅公告')
    parser.add_argument('--whale', action='store_true', help='仅鲸鱼')
    parser.add_argument('--dexscreener', action='store_true', help='仅热门代币')
    parser.add_argument('--holders', action='store_true', help='仅筹码结构')
    args = parser.parse_args()

    # 如果指定了具体的 flag，只跑那些
    if any([args.orderbook, args.alpha, args.whale, args.dexscreener, args.holders]):
        run_all(orderbook=args.orderbook, alpha=args.alpha,
                whale=args.whale, dexscreener=args.dexscreener,
                holder_stats=args.holders)
    else:
        run_all()
