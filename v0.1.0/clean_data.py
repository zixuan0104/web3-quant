"""
数据清洗脚本 — L1 现货 K 线
读取 raw/ 目录原始数据 → 清洗 → 输出到 clean/ 目录

清洗流程（不可跳过）：
1. 去重：按时间戳去重
2. 时间戳对齐：统一 UTC，对齐到 K 线边界
3. 缺失检测：标记缺失的 K 线
4. 异常值标记：价格跳跃、成交量异常
5. 交易所交叉验证：币安 vs OKX 价差检查
6. 输出 clean/<symbol>_<timeframe>.parquet

用法：
    python clean_data.py
"""

import pandas as pd
import numpy as np
import os
import json
import sys
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

# 异常检测阈值
ANOMALY_CONFIG = {
    'price_jump_threshold': 0.20,      # 单根 K 线涨跌幅 > 20% → 标记
    'volume_spike_multiplier': 10,     # 成交量 > 均值 10 倍 → 标记
    'missing_pct_warn': 0.05,          # 缺失比例 > 5% → 警告
    'cross_exchange_price_divergence': 0.005,  # 价差 > 0.5% → 标记
}

os.makedirs(CLEAN_DIR, exist_ok=True)
os.makedirs(META_DIR, exist_ok=True)
os.makedirs(LOGS_DIR, exist_ok=True)


# ══════════════════════════════════════
# DataCleaner 类
# ══════════════════════════════════════

class DataCleaner:
    """清洗交易所数据，双源交叉验证"""

    def __init__(self):
        self.quality_report = {}
        self.anomaly_log = []

    def load_raw(self, exchange, symbol, timeframe):
        """加载原始 CSV 数据"""
        symbol_safe = symbol.replace('/', '')
        filepath = os.path.join(RAW_DIR, exchange, f"{symbol_safe}_{timeframe}.csv")

        if not os.path.exists(filepath):
            print(f"  ⚠️ 文件不存在: {filepath}")
            return None

        df = pd.read_csv(filepath, index_col='timestamp', parse_dates=True)
        return df

    def deduplicate(self, df):
        """第 1 步：去重 — 按时间戳去重，保留第一条"""
        before = len(df)
        df = df[~df.index.duplicated(keep='first')]
        after = len(df)
        if before != after:
            print(f"    去重: {before} → {after} (删除 {before - after} 条重复)")
        return df

    def align_timestamps(self, df, timeframe):
        """第 2 步：时间戳对齐到 K 线边界"""
        freq_map = {'1h': 'h', '1d': 'D'}

        if timeframe == '1h':
            # 对齐到整点
            df.index = df.index.floor('h')
        elif timeframe == '1d':
            # 对齐到 00:00 UTC
            df.index = df.index.floor('D')
        else:
            return df

        # 合并重复时间戳（对齐后可能产生重复）
        agg_map = {
            'open': 'first',
            'high': 'max',
            'low': 'min',
            'close': 'last',
            'volume': 'sum',
        }
        # symbol/exchange 列可能存在也可能不存在，存在时才聚合
        if 'symbol' in df.columns:
            agg_map['symbol'] = 'first'
        if 'exchange' in df.columns:
            agg_map['exchange'] = 'first'
        df = df.groupby(df.index).agg(agg_map)

        return df

    def detect_missing(self, df, timeframe):
        """第 3 步：缺失检测"""
        freq_map = {'1h': 'h', '1d': 'D'}
        expected_freq = freq_map.get(timeframe, 'h')

        # 生成完整的预期时间索引
        full_index = pd.date_range(
            start=df.index.min(),
            end=df.index.max(),
            freq=expected_freq,
            tz='UTC',
        )

        expected_count = len(full_index)
        actual_count = len(df)
        missing_count = expected_count - actual_count
        missing_ratio = missing_count / expected_count if expected_count > 0 else 0

        # 找出缺失的时间点
        missing_times = full_index.difference(df.index)

        result = {
            'expected_rows': expected_count,
            'actual_rows': actual_count,
            'missing_rows': missing_count,
            'missing_ratio': missing_ratio,
            'missing_times': list(missing_times[:20]),  # 只记录前 20 个
            'warning': missing_ratio > ANOMALY_CONFIG['missing_pct_warn'],
        }

        if result['warning']:
            print(f"    ⚠️ 缺失 {missing_count} 行 ({missing_ratio:.2%}) — 超过 5% 阈值")

        return result

    def mark_anomalies(self, df):
        """第 4 步：异常值标记"""
        anomalies = []

        if len(df) < 2:
            return df, anomalies

        # 4a. 价格跳跃 — 当前收盘价 vs 前一根收盘价
        df['return'] = df['close'].pct_change()
        jump_mask = abs(df['return']) > ANOMALY_CONFIG['price_jump_threshold']

        for idx in df[jump_mask].index:
            anomalies.append({
                'timestamp': str(idx),
                'type': 'price_jump',
                'value': round(df.loc[idx, 'return'] * 100, 2),
                'detail': f"价格跳跃 {df.loc[idx, 'return']:.1%}",
            })

        # 4b. 成交量异常 — 当前量 > 均值 10 倍
        vol_mean = df['volume'].rolling(24).mean()
        vol_spike_mask = df['volume'] > vol_mean * ANOMALY_CONFIG['volume_spike_multiplier']
        vol_spike_mask = vol_spike_mask.fillna(False)

        for idx in df[vol_spike_mask].index:
            anomalies.append({
                'timestamp': str(idx),
                'type': 'volume_spike',
                'value': round(df.loc[idx, 'volume'], 2),
                'detail': f"成交量异常 {df.loc[idx, 'volume']:.0f} (均值 {vol_mean.loc[idx]:.0f})",
            })

        # 添加标记列
        df['anomaly'] = False
        df.loc[jump_mask | vol_spike_mask, 'anomaly'] = True
        df['anomaly_type'] = ''
        df.loc[jump_mask, 'anomaly_type'] = df.loc[jump_mask, 'anomaly_type'] + 'price_jump;'
        df.loc[vol_spike_mask, 'anomaly_type'] = df.loc[vol_spike_mask, 'anomaly_type'] + 'volume_spike;'

        if anomalies:
            print(f"    🚩 标记 {len(anomalies)} 条异常")

        return df, anomalies

    def cross_validate(self, df_binance, df_okx, symbol, timeframe):
        """第 5 步：交易所交叉验证"""
        if df_binance is None or df_okx is None:
            return None, []

        # 取两个交易所共有的时间索引
        common_idx = df_binance.index.intersection(df_okx.index)

        if len(common_idx) == 0:
            print(f"    ⚠️ 两个交易所没有重叠数据，无法交叉验证")
            return None, []

        # 计算收盘价差异
        close_diff = (df_binance.loc[common_idx, 'close'] - df_okx.loc[common_idx, 'close']).abs()
        close_avg = (df_binance.loc[common_idx, 'close'] + df_okx.loc[common_idx, 'close']) / 2
        price_divergence = close_diff / close_avg

        avg_divergence = price_divergence.mean()
        max_divergence = price_divergence.max()

        # 价差异常事件
        divergence_events = []
        threshold = ANOMALY_CONFIG['cross_exchange_price_divergence']
        divergent_mask = price_divergence > threshold
        divergent_times = price_divergence[divergent_mask].index

        for idx in divergent_times[:20]:  # 最多记录 20 个
            divergence_events.append({
                'timestamp': str(idx),
                'binance_close': float(df_binance.loc[idx, 'close']),
                'okx_close': float(df_okx.loc[idx, 'close']),
                'divergence_pct': round(float(price_divergence.loc[idx]) * 100, 3),
            })

        result = {
            'common_rows': len(common_idx),
            'avg_divergence_pct': round(float(avg_divergence) * 100, 4),
            'max_divergence_pct': round(float(max_divergence) * 100, 4),
            'divergence_events_count': len(divergent_times),
            'divergence_events': divergence_events,
            'status': 'OK' if avg_divergence < 0.001 else 'WARN',
        }

        if result['status'] == 'WARN':
            print(f"    ⚠️ 平均价差 {avg_divergence:.4%}，{len(divergent_times)} 次分歧事件")

        # 生成统一价格（取均价）
        unified = pd.DataFrame(index=common_idx)
        unified['open'] = (df_binance.loc[common_idx, 'open'] + df_okx.loc[common_idx, 'open']) / 2
        unified['high'] = df_binance.loc[common_idx, ['high']].join(
            df_okx.loc[common_idx, ['high']], lsuffix='_binance', rsuffix='_okx'
        ).max(axis=1)
        unified['low'] = df_binance.loc[common_idx, ['low']].join(
            df_okx.loc[common_idx, ['low']], lsuffix='_binance', rsuffix='_okx'
        ).min(axis=1)
        unified['close'] = close_avg
        unified['volume'] = (df_binance.loc[common_idx, 'volume'] + df_okx.loc[common_idx, 'volume'])
        unified['symbol'] = symbol
        unified['binance_close'] = df_binance.loc[common_idx, 'close']
        unified['okx_close'] = df_okx.loc[common_idx, 'close']
        unified['binance_volume'] = df_binance.loc[common_idx, 'volume']
        unified['okx_volume'] = df_okx.loc[common_idx, 'volume']
        unified['price_divergence'] = price_divergence

        # 合并异常标记（两个交易所任一标记即视为异常）
        unified['anomaly'] = False
        unified['anomaly_type'] = ''
        if 'anomaly' in df_binance.columns and 'anomaly' in df_okx.columns:
            unified['anomaly'] = (
                df_binance.loc[common_idx, 'anomaly'].fillna(False) |
                df_okx.loc[common_idx, 'anomaly'].fillna(False)
            )
            # 合并异常类型字符串
            b_types = df_binance.loc[common_idx, 'anomaly_type'].fillna('')
            o_types = df_okx.loc[common_idx, 'anomaly_type'].fillna('')
            unified['anomaly_type'] = b_types + o_types

        return unified, result

    def save_clean(self, df, symbol, timeframe):
        """保存清洗后的数据"""
        symbol_safe = symbol.replace('/', '')
        filename = f"{symbol_safe}_{timeframe}.parquet"
        filepath = os.path.join(CLEAN_DIR, filename)

        # 清理辅助列（保留 anomaly / anomaly_type 供回测引擎使用）
        cols_to_drop = ['return', 'exchange']
        for col in cols_to_drop:
            if col in df.columns:
                df = df.drop(columns=[col])

        df.to_parquet(filepath)
        return filepath

    def run(self):
        """主清洗流程"""
        print("\n" + "=" * 60)
        print("🧹 开始数据清洗")
        print("=" * 60)

        all_quality_reports = []
        all_anomalies = []

        for symbol in SYMBOLS:
            for timeframe in TIMEFRAMES:
                symbol_safe = symbol.replace('/', '')
                print(f"\n{'─' * 50}")
                print(f"🔍 {symbol} | {timeframe}")
                print(f"{'─' * 50}")

                # 加载双源数据
                df_binance = self.load_raw('binance', symbol, timeframe)
                df_okx = self.load_raw('okx', symbol, timeframe)

                if df_binance is None and df_okx is None:
                    print(f"  ❌ 两个数据源都没有数据，跳过")
                    all_quality_reports.append({
                        'symbol': symbol, 'timeframe': timeframe, 'status': 'NO_DATA'
                    })
                    continue

                # 分别清洗
                missing_report = {'binance': None, 'okx': None}

                if df_binance is not None:
                    df_binance = self.deduplicate(df_binance)
                    df_binance = self.align_timestamps(df_binance, timeframe)
                    missing_report['binance'] = self.detect_missing(df_binance, timeframe)
                    df_binance, binance_anomalies = self.mark_anomalies(df_binance)
                    all_anomalies.extend(binance_anomalies)
                    print(f"   ✅ 币安: {len(df_binance)} 行")

                if df_okx is not None:
                    df_okx = self.deduplicate(df_okx)
                    df_okx = self.align_timestamps(df_okx, timeframe)
                    missing_report['okx'] = self.detect_missing(df_okx, timeframe)
                    df_okx, okx_anomalies = self.mark_anomalies(df_okx)
                    all_anomalies.extend(okx_anomalies)
                    print(f"   ✅ OKX: {len(df_okx)} 行")

                # 交叉验证 → 生成统一数据集
                cross_report = None
                df_unified = None
                if df_binance is not None and df_okx is not None:
                    df_unified, cross_report = self.cross_validate(
                        df_binance, df_okx, symbol, timeframe
                    )

                # 保存清洗后数据（优先统一数据，其次单源）
                if df_unified is not None and not df_unified.empty:
                    filepath = self.save_clean(df_unified, symbol, timeframe)
                    data_source = 'unified'
                    row_count = len(df_unified)
                elif df_binance is not None:
                    filepath = self.save_clean(df_binance, symbol, timeframe)
                    data_source = 'binance_only'
                    row_count = len(df_binance)
                elif df_okx is not None:
                    filepath = self.save_clean(df_okx, symbol, timeframe)
                    data_source = 'okx_only'
                    row_count = len(df_okx)
                else:
                    filepath = None
                    data_source = 'none'
                    row_count = 0

                quality_entry = {
                    'symbol': symbol,
                    'timeframe': timeframe,
                    'row_count': row_count,
                    'data_source': data_source,
                    'missing_report': missing_report,
                    'cross_validation': cross_report,
                    'anomaly_count': len([
                        a for a in all_anomalies
                        if symbol_safe in str(a.get('timestamp', '')) or symbol in str(a)
                    ]),
                    'filepath': filepath,
                }
                all_quality_reports.append(quality_entry)

        # ── 保存质量报告 ──
        self._save_quality_report(all_quality_reports, all_anomalies)
        self._print_summary(all_quality_reports)

    def _save_quality_report(self, quality_reports, anomalies):
        """保存数据质量报告到 meta/"""
        report = {
            'clean_time': datetime.utcnow().strftime('%Y-%m-%dT%H:%M:%SZ'),
            'symbols_processed': len(SYMBOLS),
            'timeframes_processed': len(TIMEFRAMES),
            'total_rows': sum(q.get('row_count', 0) for q in quality_reports),
            'total_anomalies': len(anomalies),
            'details': quality_reports,
            'anomalies': anomalies[:100],  # 最多存 100 条
        }

        filepath = os.path.join(META_DIR, 'quality_report.json')
        with open(filepath, 'w', encoding='utf-8') as f:
            json.dump(report, f, ensure_ascii=False, indent=2, default=str)

        print(f"\n📂 质量报告: {filepath}")

        # 同时写一份人类可读的文本报告
        txt_path = os.path.join(LOGS_DIR, f"quality_report_{datetime.utcnow().strftime('%Y%m%d_%H%M%S')}.txt")
        with open(txt_path, 'w', encoding='utf-8') as f:
            f.write(self._format_text_report(quality_reports, anomalies))

    def _print_summary(self, reports):
        """打印汇总报告"""
        print("\n" + "=" * 60)
        print("📊 数据质量报告")
        print("=" * 60)

        total_rows = 0
        total_anomalies = 0
        warnings = []

        for q in reports:
            symbol = q['symbol']
            timeframe = q['timeframe']
            rows = q.get('row_count', 0)
            source = q.get('data_source', 'none')
            total_rows += rows

            # 缺失检查
            missing_warn = False
            for ex in ['binance', 'okx']:
                mr = q.get('missing_report', {}).get(ex)
                if mr and mr.get('warning'):
                    warnings.append(f"  ⚠️ {symbol} {timeframe} {ex}: 缺失 {mr['missing_rows']} 行 ({mr['missing_ratio']:.2%})")

            # 价差检查
            cv = q.get('cross_validation')
            cv_status = ''
            if cv:
                if cv.get('status') == 'WARN':
                    cv_status = f" ⚠️ 价差 {cv['avg_divergence_pct']:.2f}%"
                    warnings.append(f"  ⚠️ {symbol} {timeframe} 价差异常: 均值 {cv['avg_divergence_pct']:.2f}%")
                else:
                    cv_status = f" ✅ 价差 {cv['avg_divergence_pct']:.3f}%"

            anomaly_count = q.get('anomaly_count', 0)
            total_anomalies += anomaly_count

            status_icon = '✅' if not any(w in str(warnings[-1:]) for w in [symbol]) else '⚠️'
            print(f"  {status_icon} {symbol:12s} {timeframe:4s} → {rows:6d} 行 | 来源: {source:10s}{cv_status}")

        if warnings:
            print(f"\n⚠️ 警告:")
            for w in warnings:
                print(w)

        print(f"\n✅ 清洗完成: {len(reports)} 数据集, {total_rows:,} 行, {total_anomalies} 条异常标记")

    def _format_text_report(self, reports, anomalies):
        """生成人类可读的文本报告"""
        lines = []
        lines.append("╔══════════════════════════════════════╗")
        lines.append("║     数据质量报告                      ║")
        lines.append(f"║     {datetime.utcnow().strftime('%Y-%m-%d %H:%M')} UTC                   ║")
        lines.append("╚══════════════════════════════════════╝")
        lines.append("")

        for q in reports:
            symbol = q['symbol']
            timeframe = q['timeframe']
            lines.append(f"═══════════════════════════════════")
            lines.append(f"  {symbol} | {timeframe}")
            lines.append(f"  行数: {q.get('row_count', 0):,}")
            lines.append(f"  数据来源: {q.get('data_source', 'N/A')}")
            lines.append(f"  异常标记: {q.get('anomaly_count', 0)} 条")

            # 缺失
            for ex in ['binance', 'okx']:
                mr = q.get('missing_report', {}).get(ex)
                if mr:
                    status = '⚠️' if mr.get('warning') else '✅'
                    lines.append(f"  {ex} 缺失: {mr['missing_rows']} / {mr['expected_rows']} ({mr['missing_ratio']:.2%}) {status}")

            # 价差
            cv = q.get('cross_validation')
            if cv:
                lines.append(f"  价差验证: 均值 {cv['avg_divergence_pct']:.4f}% | 最大 {cv['max_divergence_pct']:.4f}%")
                if cv['divergence_events_count'] > 0:
                    lines.append(f"  分歧事件: {cv['divergence_events_count']} 次")
                    for ev in cv.get('divergence_events', [])[:3]:
                        lines.append(f"    {ev['timestamp']}: B={ev['binance_close']:.4f} O={ev['okx_close']:.4f} ({ev['divergence_pct']:.3f}%)")

            lines.append("")

        return '\n'.join(lines)


# ══════════════════════════════════════
# 入口
# ══════════════════════════════════════

if __name__ == '__main__':
    cleaner = DataCleaner()
    cleaner.run()
