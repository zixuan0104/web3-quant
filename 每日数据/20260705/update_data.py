"""
增量更新脚本 — L1 现货 K 线
读取已有数据的最新时间戳 → 从该时间戳拉取新数据 → 拼接 + 清洗 → 更新存档

用途：
- 每日运行一次（或每小时），保持数据最新
- 只拉增量，不重复拉整个历史
- 自动验证新旧数据连续性

用法：
    python update_data.py
"""

import pandas as pd
import numpy as np
import os
import json
import sys
import time
from datetime import datetime, timedelta

# Windows GBK 编码兼容
if sys.platform == 'win32':
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    sys.stderr.reconfigure(encoding='utf-8', errors='replace')

# ══════════════════════════════════════
# 配置
# ══════════════════════════════════════
DATA_DIR = os.path.dirname(os.path.abspath(__file__))
RAW_DIR = os.path.join(DATA_DIR, 'raw')
CLEAN_DIR = os.path.join(DATA_DIR, 'clean')
META_DIR = os.path.join(DATA_DIR, 'meta')
LOGS_DIR = os.path.join(DATA_DIR, 'logs')

SYMBOLS = ['BTC/USDT', 'ETH/USDT', 'SOL/USDT', 'DOGE/USDT', 'PEPE/USDT', 'WIF/USDT']
TIMEFRAMES = ['1h', '1d']
EXCHANGES = ['binance', 'okx']

# 最大拉取缺口（天）：如果最新数据超过这个天数，建议跑完整 fetch 而非 update
MAX_GAP_DAYS = 7

os.makedirs(RAW_DIR, exist_ok=True)
os.makedirs(CLEAN_DIR, exist_ok=True)
os.makedirs(META_DIR, exist_ok=True)
os.makedirs(LOGS_DIR, exist_ok=True)


class DataUpdater:
    """增量更新管理器"""

    def __init__(self):
        self.update_log = []
        self.warnings = []

    def get_latest_timestamp(self, exchange, symbol, timeframe):
        """
        读取已有数据的最新时间戳

        返回:
            (latest_timestamp_utc, row_count) 或 (None, 0)
        """
        symbol_safe = symbol.replace('/', '')
        raw_path = os.path.join(RAW_DIR, exchange, f"{symbol_safe}_{timeframe}.csv")

        if not os.path.exists(raw_path):
            print(f"    📄 {exchange}/{symbol_safe}_{timeframe}.csv 不存在，需要全量拉取")
            return None, 0

        try:
            df = pd.read_csv(raw_path, index_col='timestamp', parse_dates=True)
            if df.empty:
                return None, 0
            latest = df.index.max()
            return latest, len(df)
        except Exception as e:
            print(f"    ❌ 读取失败: {e}")
            return None, 0

    def fetch_incremental(self, exchange_name, symbol, timeframe, since_timestamp):
        """
        拉取增量数据

        参数:
            exchange_name: 'binance' | 'okx'
            symbol: 交易对
            timeframe: K 线周期
            since_timestamp: 起始时间戳 (datetime with tz)

        返回:
            pd.DataFrame 或 None
        """
        import ccxt

        try:
            exchange_class = getattr(ccxt, exchange_name)
            exchange = exchange_class({
                'enableRateLimit': True,
                'timeout': 30000,
            })
        except Exception as e:
            print(f"    ❌ 初始化失败: {e}")
            return None

        # ccxt 需要毫秒时间戳
        since_ms = exchange.parse8601(since_timestamp.strftime('%Y-%m-%dT%H:%M:%SZ'))

        all_dfs = []
        max_retries = 3

        # 逐批拉取
        batch = 0
        while True:
            for attempt in range(max_retries):
                try:
                    raw = exchange.fetch_ohlcv(symbol, timeframe, since=since_ms, limit=1000)
                    break
                except Exception as e:
                    if attempt < max_retries - 1:
                        time.sleep(5 * (attempt + 1))
                    else:
                        print(f"    ❌ {exchange_name} {symbol} {timeframe}: 拉取失败: {e}")
                        return None if batch == 0 else pd.concat(all_dfs)

            if not raw:
                break

            df = pd.DataFrame(raw, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
            df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms', utc=True)
            df.set_index('timestamp', inplace=True)
            df['symbol'] = symbol
            df['exchange'] = exchange_name

            all_dfs.append(df)

            last_ts = df.index[-1]
            since_ms = exchange.parse8601(
                (last_ts + timedelta(hours=1)).strftime('%Y-%m-%dT%H:%M:%SZ')
            )

            # 到达当前时间
            if last_ts >= datetime.now(tz=last_ts.tzinfo) - timedelta(hours=1):
                break

            batch += 1
            time.sleep(0.3)

        if not all_dfs:
            return None

        result = pd.concat(all_dfs)
        result = result[~result.index.duplicated(keep='first')]
        result.sort_index(inplace=True)
        return result

    def merge_and_save_raw(self, existing_df, new_df, exchange, symbol, timeframe):
        """合并已有数据和新数据，保存原始 CSV"""
        symbol_safe = symbol.replace('/', '')

        if existing_df is not None and not existing_df.empty:
            merged = pd.concat([existing_df, new_df])
        else:
            merged = new_df

        merged = merged[~merged.index.duplicated(keep='last')]  # 重叠部分用新数据覆盖
        merged.sort_index(inplace=True)

        raw_path = os.path.join(RAW_DIR, exchange, f"{symbol_safe}_{timeframe}.csv")
        merged.to_csv(raw_path)

        return merged, raw_path

    def check_continuity(self, old_latest, new_earliest, symbol, timeframe):
        """
        检查数据连续性

        如果新老数据之间有较大缺口 → 可能是数据源问题
        """
        if old_latest is None:
            return

        gap = new_earliest - old_latest

        freq_map = {'1h': timedelta(hours=1), '1d': timedelta(days=1)}
        expected_gap = freq_map.get(timeframe, timedelta(hours=1))

        if gap > expected_gap * 3:  # 超过 3 个周期的缺口
            hours_gap = gap.total_seconds() / 3600
            self.warnings.append({
                'symbol': symbol,
                'timeframe': timeframe,
                'gap_hours': round(hours_gap, 1),
                'old_latest': str(old_latest),
                'new_earliest': str(new_earliest),
            })
            print(f"    ⚠️ 数据缺口: {hours_gap:.1f}h ({old_latest} → {new_earliest})")

    def run_clean_update(self):
        """更新完原始数据后重新跑清洗"""
        from clean_data import DataCleaner
        print(f"\n{'─' * 50}")
        print(f"🧹 重新清洗所有数据（增量更新后）")
        print(f"{'─' * 50}")
        cleaner = DataCleaner()
        cleaner.run()

    def run(self):
        """主增量更新流程"""
        print("\n" + "=" * 60)
        print("🔄 增量数据更新")
        print(f"   时间: {datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')} UTC")
        print("=" * 60)

        total_updated = 0
        total_skipped = 0

        for exchange_name in EXCHANGES:
            for symbol in SYMBOLS:
                for timeframe in TIMEFRAMES:
                    print(f"\n📡 {exchange_name:8s} {symbol:12s} {timeframe}")

                    # 1. 查已有数据的最新时间戳
                    latest_ts, row_count = self.get_latest_timestamp(
                        exchange_name, symbol, timeframe
                    )

                    # 2. 判断是否需要全量拉取
                    if latest_ts is None:
                        print(f"    ⚠️ 无已有数据，跳过增量更新（请先运行 fetch_data.py）")
                        total_skipped += 1
                        continue

                    # 3. 检查数据是否太旧
                    now = datetime.now(tz=latest_ts.tzinfo)
                    gap_days = (now - latest_ts).total_seconds() / 86400

                    if gap_days > MAX_GAP_DAYS:
                        print(f"    ⚠️ 最新数据距今 {gap_days:.0f} 天 > {MAX_GAP_DAYS} 天")
                        print(f"    建议重新运行 fetch_data.py 做全量拉取")
                        # 仍然尝试增量更新
                    elif gap_days < 0.05:  # < 1.2 小时
                        print(f"    ✅ 数据已是最新 ({latest_ts.strftime('%Y-%m-%d %H:%M')} UTC) 跳过")
                        total_skipped += 1
                        continue

                    # 4. 读取已有数据
                    existing_df = None
                    symbol_safe = symbol.replace('/', '')
                    raw_path = os.path.join(RAW_DIR, exchange_name, f"{symbol_safe}_{timeframe}.csv")
                    if os.path.exists(raw_path):
                        existing_df = pd.read_csv(raw_path, index_col='timestamp', parse_dates=True)

                    # 5. 拉取增量
                    since = latest_ts - timedelta(hours=1)  # 多拉 1 小时避免边界缺口
                    new_df = self.fetch_incremental(exchange_name, symbol, timeframe, since)

                    if new_df is None or new_df.empty:
                        print(f"    ℹ️ 无新数据")
                        total_skipped += 1
                        continue

                    # 只保留真正新的数据（比已有最新时间戳更晚的）
                    new_df = new_df[new_df.index > latest_ts]
                    if new_df.empty:
                        print(f"    ✅ 无新增数据")
                        total_skipped += 1
                        continue

                    # 6. 连续性检查
                    self.check_continuity(latest_ts, new_df.index.min(), symbol, timeframe)

                    # 7. 合并 + 保存
                    merged, saved_path = self.merge_and_save_raw(
                        existing_df, new_df, exchange_name, symbol, timeframe
                    )

                    new_rows = len(new_df)
                    print(f"    ✅ +{new_rows} 行 → {saved_path}")
                    print(f"       时间范围: {new_df.index[0]} → {new_df.index[-1]}")

                    self.update_log.append({
                        'exchange': exchange_name,
                        'symbol': symbol,
                        'timeframe': timeframe,
                        'new_rows': new_rows,
                        'total_rows': len(merged),
                        'latest': str(merged.index[-1]),
                    })
                    total_updated += 1

        # ── 保存更新日志 ──
        self._save_log()

        # ── 如果有更新，重新跑清洗 ──
        if total_updated > 0:
            self.run_clean_update()

        # ── 汇总 ──
        print("\n" + "=" * 60)
        print("📊 更新报告")
        print("=" * 60)
        for entry in self.update_log:
            print(f"  ✅ {entry['exchange']:8s} {entry['symbol']:12s} {entry['timeframe']:4s} → +{entry['new_rows']:5d} 行 (总计 {entry['total_rows']:,})")

        if total_skipped > 0:
            print(f"  ⏭️ {total_skipped} 个任务无需更新（已是最新）")

        if self.warnings:
            print(f"\n⚠️ {len(self.warnings)} 个数据连续性警告:")
            for w in self.warnings:
                print(f"   - {w['symbol']} {w['timeframe']}: {w['gap_hours']:.1f}h 缺口")

        print(f"\n✅ 更新 {total_updated} 个数据集, 跳过 {total_skipped} 个")

    def _save_log(self):
        """保存更新日志"""
        log_entry = {
            'update_time': datetime.utcnow().strftime('%Y-%m-%dT%H:%M:%SZ'),
            'datasets_updated': len(self.update_log),
            'warnings': len(self.warnings),
            'details': self.update_log,
            'warnings': self.warnings,
        }

        # 追加到日志文件
        log_path = os.path.join(META_DIR, 'update_log.jsonl')
        with open(log_path, 'a', encoding='utf-8') as f:
            f.write(json.dumps(log_entry, ensure_ascii=False, default=str) + '\n')

        print(f"\n📂 更新日志: {log_path}")


# ══════════════════════════════════════
# 入口
# ══════════════════════════════════════

if __name__ == '__main__':
    updater = DataUpdater()
    updater.run()
