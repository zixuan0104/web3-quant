"""
数据拉取脚本 — L1 现货 K 线
使用 ccxt 统一接口拉取币安和 OKX 数据
双源交叉验证，自动重试，进度显示

用法：
    python fetch_data.py

配置在脚本顶部的 CONFIG 字典中修改。
"""

import ccxt
import pandas as pd
from datetime import datetime, timedelta
import time
import os
import sys
import json

# Windows GBK 编码兼容：强制 UTF-8 输出
if sys.platform == 'win32':
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    sys.stderr.reconfigure(encoding='utf-8', errors='replace')

# ══════════════════════════════════════
# 配置
# ══════════════════════════════════════
CONFIG = {
    # 目标币种
    'symbols': ['BTC/USDT', 'ETH/USDT', 'SOL/USDT', 'DOGE/USDT', 'PEPE/USDT', 'WIF/USDT'],

    # K 线周期
    'timeframes': ['1h', '1d'],

    # 历史数据跨度（年）
    'history_years': 2,

    # 数据源
    'exchanges': ['binance', 'okx'],

    # 存储路径（脚本所在目录）
    'data_dir': os.path.dirname(os.path.abspath(__file__)),

    # API 超时（毫秒）
    'timeout': 30000,

    # 代理配置（国内直连币安/OKX 超时，需要通过 VPN 代理）
    # 常见 VPN 代理地址（根据你的 VPN 软件填写）：
    #   Clash: http://127.0.0.1:7890
    #   V2Ray: http://127.0.0.1:10809
    #   Shadowsocks: socks5://127.0.0.1:1080
    'proxy': {
        'http': 'http://127.0.0.1:7890',
        'https': 'http://127.0.0.1:7890',
    },

    # 重试配置
    'max_retries': 3,
    'retry_delay': 5,  # 秒

    # ccxt 交易所配置
    'exchange_config': {
        'binance': {
            'enableRateLimit': True,
            'timeout': 30000,
            'proxies': {  # 通过代理连接
                'http': 'http://127.0.0.1:7890',
                'https': 'http://127.0.0.1:7890',
            },
        },
        'okx': {
            'enableRateLimit': True,
            'timeout': 30000,
            'proxies': {
                'http': 'http://127.0.0.1:7890',
                'https': 'http://127.0.0.1:7890',
            },
        },
    },
}

# ══════════════════════════════════════
# DataFetcher 类
# ══════════════════════════════════════

class DataFetcher:
    """拉取交易所 OHLCV 数据"""

    def __init__(self, config=CONFIG):
        self.config = config
        self.data_dir = config['data_dir']
        self.exchanges = {}
        self._init_exchanges()

    def _init_exchanges(self):
        """初始化交易所连接"""
        for name in self.config['exchanges']:
            try:
                exchange_class = getattr(ccxt, name)
                cfg = self.config['exchange_config'].get(name, {})
                self.exchanges[name] = exchange_class(cfg)
                print(f"✅ {name} 连接成功")
            except Exception as e:
                print(f"❌ {name} 连接失败: {e}")
                self.exchanges[name] = None

    def fetch_ohlcv(self, exchange_name, symbol, timeframe, since, limit=1000):
        """
        拉取 OHLCV 数据，含完整错误处理和重试逻辑

        参数:
            exchange_name: 'binance' | 'okx'
            symbol: 交易对如 'BTC/USDT'
            timeframe: K线周期如 '1h' | '1d'
            since: 起始时间戳(ms)
            limit: 单次最大条数（默认 1000）

        返回:
            pd.DataFrame 或 None
        """
        exchange = self.exchanges.get(exchange_name)
        if exchange is None:
            print(f"  ⚠️ {exchange_name} 未初始化，跳过")
            return None

        for attempt in range(1, self.config['max_retries'] + 1):
            try:
                raw = exchange.fetch_ohlcv(
                    symbol, timeframe,
                    since=since,
                    limit=limit
                )

                if not raw:
                    print(f"  ⚠️ {exchange_name} {symbol} {timeframe} 返回空数据")
                    return None

                df = pd.DataFrame(
                    raw,
                    columns=['timestamp', 'open', 'high', 'low', 'close', 'volume']
                )
                df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms', utc=True)
                df.set_index('timestamp', inplace=True)
                df['symbol'] = symbol
                df['exchange'] = exchange_name

                return df

            except ccxt.NetworkError as e:
                if attempt < self.config['max_retries']:
                    delay = self.config['retry_delay'] * attempt  # 指数退避
                    print(f"  🔄 网络错误，{delay}s 后重试 ({attempt}/{self.config['max_retries']}): {e}")
                    time.sleep(delay)
                else:
                    print(f"  ❌ 网络错误，已达最大重试: {e}")
                    return None

            except ccxt.RateLimitExceeded as e:
                delay = self.config['retry_delay'] * 5
                print(f"  ⏳ 限频，{delay}s 后重试: {e}")
                time.sleep(delay)

            except Exception as e:
                print(f"  ❌ 未知错误: {e}")
                return None

        return None

    def fetch_all_ohlcv(self, exchange_name, symbol, timeframe, start_date):
        """
        分段拉取全部历史数据

        参数:
            exchange_name: 交易所名称
            symbol: 交易对
            timeframe: K 线周期
            start_date: 起始日期 (datetime)

        返回:
            pd.DataFrame（所有分段拼接）
        """
        exchange = self.exchanges.get(exchange_name)
        if exchange is None:
            print(f"  ❌ {exchange_name} 未连接")
            return None

        since = exchange.parse8601(start_date.strftime('%Y-%m-%dT%H:%M:%SZ'))
        all_dfs = []

        print(f"\n{'='*60}")
        print(f"📥 {exchange_name.upper()} | {symbol} | {timeframe}")
        print(f"   起始: {start_date.strftime('%Y-%m-%d')} → 至今")
        print(f"{'='*60}")

        while True:
            df = self.fetch_ohlcv(exchange_name, symbol, timeframe, since=since)

            if df is None or df.empty:
                break

            all_dfs.append(df)

            # 最后一条的时间 + 1 个周期作为下一次拉取的起点
            last_ts = df.index[-1]
            since = exchange.parse8601(
                (last_ts + timedelta(hours=1)).strftime('%Y-%m-%dT%H:%M:%SZ')
            )

            # 拉到了最新数据
            if last_ts >= datetime.now(tz=last_ts.tzinfo) - timedelta(hours=1):
                break

            # 进度显示
            if len(all_dfs) % 10 == 0:
                print(f"   已拉取 {len(all_dfs)} 批 → 最新: {last_ts.strftime('%Y-%m-%d %H:%M')}")

            time.sleep(0.5)  # 礼貌等待

        if not all_dfs:
            print(f"  ❌ 没有拉到任何数据")
            return None

        result = pd.concat(all_dfs)
        result = result[~result.index.duplicated(keep='first')]  # 去重
        result.sort_index(inplace=True)

        print(f"   ✅ 完成: {len(result)} 行 → {result.index[0]} ~ {result.index[-1]}")

        return result

    def save_raw(self, df, exchange_name, symbol, timeframe):
        """保存原始数据到 raw/ 目录"""
        symbol_safe = symbol.replace('/', '')
        filename = f"{symbol_safe}_{timeframe}.csv"

        raw_dir = os.path.join(self.data_dir, 'raw', exchange_name)
        os.makedirs(raw_dir, exist_ok=True)

        filepath = os.path.join(raw_dir, filename)
        df.to_csv(filepath)
        return filepath

    def run(self):
        """主流程：拉取所有配置的币种和周期"""
        start_date = datetime.utcnow() - timedelta(days=365 * self.config['history_years'])

        total_tasks = (
            len(self.config['exchanges']) *
            len(self.config['symbols']) *
            len(self.config['timeframes'])
        )
        completed = 0
        failed = []

        log_entries = []

        print("\n" + "=" * 60)
        print("🚀 开始拉取数据")
        print(f"   交易所: {self.config['exchanges']}")
        print(f"   币种: {self.config['symbols']}")
        print(f"   周期: {self.config['timeframes']}")
        print(f"   历史: {self.config['history_years']} 年")
        print(f"   任务数: {total_tasks}")
        print("=" * 60)

        for exchange_name in self.config['exchanges']:
            for symbol in self.config['symbols']:
                for timeframe in self.config['timeframes']:
                    completed += 1
                    print(f"\n[{completed}/{total_tasks}]", end=" ")

                    df = self.fetch_all_ohlcv(exchange_name, symbol, timeframe, start_date)

                    if df is not None and not df.empty:
                        filepath = self.save_raw(df, exchange_name, symbol, timeframe)
                        log_entries.append({
                            'exchange': exchange_name,
                            'symbol': symbol,
                            'timeframe': timeframe,
                            'rows': len(df),
                            'start': str(df.index[0]),
                            'end': str(df.index[-1]),
                            'filepath': filepath,
                            'status': 'OK',
                        })
                    else:
                        failed.append(f"{exchange_name}/{symbol}/{timeframe}")
                        log_entries.append({
                            'exchange': exchange_name,
                            'symbol': symbol,
                            'timeframe': timeframe,
                            'status': 'FAILED',
                        })

        # ── 拉取日志 ──
        log_path = os.path.join(self.data_dir, 'meta', 'fetch_log.json')
        os.makedirs(os.path.dirname(log_path), exist_ok=True)
        with open(log_path, 'w', encoding='utf-8') as f:
            json.dump({
                'fetch_time': datetime.utcnow().strftime('%Y-%m-%dT%H:%M:%SZ'),
                'config': {
                    'symbols': self.config['symbols'],
                    'timeframes': self.config['timeframes'],
                    'exchanges': self.config['exchanges'],
                    'history_years': self.config['history_years'],
                },
                'total_tasks': total_tasks,
                'completed': total_tasks - len(failed),
                'failed': len(failed),
                'entries': log_entries,
            }, f, ensure_ascii=False, indent=2)

        # ── 汇总报告 ──
        print("\n" + "=" * 60)
        print("📊 拉取报告")
        print("=" * 60)
        for entry in log_entries:
            if entry['status'] == 'OK':
                print(f"  ✅ {entry['exchange']:8s} {entry['symbol']:12s} {entry['timeframe']:4s} → {entry['rows']:6d} 行 | {entry['start'][:10]} ~ {entry['end'][:10]}")
            else:
                print(f"  ❌ {entry['exchange']:8s} {entry['symbol']:12s} {entry['timeframe']:4s} → 失败")

        if failed:
            print(f"\n⚠️ {len(failed)} 个任务失败:")
            for f in failed:
                print(f"   - {f}")

        print(f"\n📂 原始数据: {os.path.join(self.data_dir, 'raw')}")
        print(f"📂 拉取日志: {log_path}")
        print(f"✅ 完成 {total_tasks - len(failed)}/{total_tasks}")

        return log_entries


# ══════════════════════════════════════
# 入口
# ══════════════════════════════════════

if __name__ == '__main__':
    # 检查 ccxt 是否安装
    try:
        import ccxt
    except ImportError:
        print("❌ 请先安装 ccxt: pip install ccxt")
        sys.exit(1)

    fetcher = DataFetcher()
    fetcher.run()
