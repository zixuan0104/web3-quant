"""
配置管理器 — 集成仓位管理 (Day 8 更新)

新增：
  PositionConfig — 仓位管理参数（凯利/固定分数/波动率调整/盈利提现）
  ConfigManager.position — 仓位配置入口
"""

import os
import sys
from pathlib import Path
from dataclasses import dataclass, field
from typing import Optional, Dict, List
from enum import Enum


# ═══════════════════════════════
# 配置数据结构
# ═══════════════════════════════

@dataclass
class ExchangeConfig:
    """交易所配置"""
    name: str = "binance"
    api_key: str = ""
    secret_key: str = ""
    testnet: bool = True
    base_url: str = ""
    ws_url: str = ""


@dataclass
class RiskConfig:
    """风控参数配置"""
    max_position_pct: float = 0.20
    max_total_exposure_pct: float = 1.0
    min_order_usdt: float = 50.0
    daily_loss_limit_pct: float = 0.05
    daily_loss_hard_limit_pct: float = 0.08
    max_consecutive_losses: int = 5
    consecutive_loss_cooldown_hours: int = 24
    price_spike_threshold_pct: float = 10.0
    volume_spike_threshold: float = 5.0
    default_order_type: str = "limit"
    max_slippage_pct: float = 0.005
    order_timeout_seconds: int = 60


class SizingMethod(Enum):
    """仓位计算方法（从 position_sizer 同步）"""
    KELLY_FULL = "kelly_full"
    KELLY_HALF = "kelly_half"
    KELLY_QUARTER = "kelly_quarter"
    FIXED_FRACTION = "fixed_fraction"
    VOLATILITY_ADJUSTED = "volatility_adjusted"


@dataclass
class PositionConfig:
    """
    仓位管理配置 — Day 8 新增

    与 RiskConfig 的区别:
      RiskConfig → 风控门禁（能不能交易）
      PositionConfig → 仓位计算（该下多少）
    """
    # ── 基础 ──
    initial_capital: float = 10000.0
    default_method: SizingMethod = SizingMethod.KELLY_HALF  # 默认半凯利

    # ── 凯利参数（无历史数据时的默认值）──
    default_win_rate: float = 0.50
    default_avg_win_pct: float = 0.03
    default_avg_loss_pct: float = 0.015

    # ── 固定分数 ──
    fixed_fraction_pct: float = 0.02  # 每笔 2%

    # ── 波动率调整 ──
    vol_target_risk_pct: float = 0.01    # 每笔目标风险 = 本金 1%
    atr_lookback: int = 14
    vol_multiplier: float = 2.0

    # ── 硬约束 ──
    max_position_pct: float = 0.20       # 单笔最大 20%
    min_position_pct: float = 0.005      # 单笔最小 0.5%
    max_single_asset_exposure: float = 0.30  # 单币种敞口 30%
    max_total_exposure: float = 0.80     # 总敞口 80%

    # ── 盈利提现 ──
    profit_take_interval_days: int = 30
    profit_take_pct: float = 0.30
    profit_take_min_profit: float = 500.0


@dataclass
class StrategyConfig:
    """策略运行配置"""
    name: str = "trend"
    symbols: List[str] = field(default_factory=lambda: ["BTC/USDT"])
    timeframe: str = "1h"
    enabled: bool = True
    params: Dict = field(default_factory=dict)


@dataclass
class LogConfig:
    """日志配置"""
    log_dir: str = "logs"
    trade_log_file: str = "trades.jsonl"
    system_log_file: str = "system.jsonl"
    risk_log_file: str = "risk.jsonl"
    keep_days: int = 90


@dataclass
class SystemConfig:
    """系统级配置"""
    mode: str = "paper"
    project_root: str = ""
    data_dir: str = "clean"
    heartbeat_interval_seconds: int = 60
    restart_on_error: bool = True
    max_restarts_per_hour: int = 3


# ═══════════════════════════════
# 配置管理器
# ═══════════════════════════════

class ConfigManager:
    """统一配置入口 — 集成仓位管理"""

    REQUIRED_ENV_VARS = ["BINANCE_API_KEY", "BINANCE_SECRET_KEY"]
    OPTIONAL_ENV_VARS = [
        "OKX_API_KEY", "OKX_SECRET_KEY",
        "DEEPSEEK_API_KEY", "ANTHROPIC_API_KEY",
        "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID",
        "ETHERSCAN_API_KEY", "WHALE_ALERT_API_KEY",
    ]

    HARD_LIMITS = {
        'max_position_pct': 0.25,
        'daily_loss_limit_pct': 0.08,
        'max_consecutive_losses': 7,
        'max_total_exposure_pct': 1.0,
    }

    def __init__(self, mode='paper', project_root=None):
        self.mode = mode
        if project_root is None:
            project_root = os.path.dirname(os.path.abspath(__file__))
        self.project_root = Path(project_root)

        self._load_env()

        self.exchange = self._init_exchange()
        self.risk = self._init_risk()
        self.position = self._init_position()  # Day 8 新增
        self.strategies = self._init_strategies()
        self.log = self._init_log()
        self.system = SystemConfig(
            mode=mode,
            project_root=str(self.project_root),
            data_dir=str(self.project_root / 'clean'),
        )
        self._validate()

    def _load_env(self):
        env_path = self.project_root / '.env'
        if env_path.exists():
            with open(env_path, 'r', encoding='utf-8') as f:
                for line in f:
                    line = line.strip()
                    if line and not line.startswith('#') and '=' in line:
                        key, _, val = line.partition('=')
                        key, val = key.strip(), val.strip().strip('"').strip("'")
                        if key not in os.environ:
                            os.environ[key] = val

    def _get_env(self, key, default=""):
        return os.environ.get(key, default)

    # ── 各模块初始化 ──

    def _init_exchange(self) -> ExchangeConfig:
        testnet = self.mode == 'paper' or self._get_env('BINANCE_TESTNET', 'true').lower() == 'true'
        return ExchangeConfig(
            name=self._get_env('EXCHANGE', 'binance'),
            api_key=self._get_env('BINANCE_API_KEY', ''),
            secret_key=self._get_env('BINANCE_SECRET_KEY', ''),
            testnet=testnet,
            base_url=self._get_env('BINANCE_BASE_URL',
                                   'https://testnet.binance.vision' if testnet else 'https://api.binance.com'),
            ws_url=self._get_env('BINANCE_WS_URL',
                                 'wss://testnet.binance.vision/ws' if testnet else 'wss://stream.binance.com:9443/ws'),
        )

    def _init_risk(self) -> RiskConfig:
        rc = RiskConfig()
        for param, hard_limit in self.HARD_LIMITS.items():
            env_val = self._get_env(f"RISK_{param.upper()}", '')
            if env_val:
                try:
                    val = float(env_val)
                    setattr(rc, param, min(val, hard_limit))
                except ValueError:
                    pass
        rc.default_order_type = self._get_env('DEFAULT_ORDER_TYPE', 'limit')
        return rc

    def _init_position(self) -> PositionConfig:
        """初始化仓位管理配置 — Day 8 新增"""
        pc = PositionConfig()

        # 从 env 读取（可选覆盖）
        for field_name in ['initial_capital', 'fixed_fraction_pct',
                           'vol_target_risk_pct', 'max_position_pct',
                           'max_single_asset_exposure', 'max_total_exposure',
                           'profit_take_interval_days', 'profit_take_pct',
                           'profit_take_min_profit']:
            env_val = self._get_env(f"POS_{field_name.upper()}", '')
            if env_val:
                try:
                    setattr(pc, field_name, float(env_val))
                except ValueError:
                    pass

        # 仓位计算方法
        method_env = self._get_env('POS_DEFAULT_METHOD', 'kelly_half')
        method_map = {
            'kelly_full': SizingMethod.KELLY_FULL,
            'kelly_half': SizingMethod.KELLY_HALF,
            'kelly_quarter': SizingMethod.KELLY_QUARTER,
            'fixed_fraction': SizingMethod.FIXED_FRACTION,
            'volatility_adjusted': SizingMethod.VOLATILITY_ADJUSTED,
        }
        pc.default_method = method_map.get(method_env, SizingMethod.KELLY_HALF)

        return pc

    def _init_strategies(self) -> List[StrategyConfig]:
        strategies = []
        enabled_strats = self._get_env('ENABLED_STRATEGIES', 'trend,momentum')
        enabled_names = [s.strip() for s in enabled_strats.split(',') if s.strip()]

        strategy_presets = {
            'trend': {'name': '趋势跟踪', 'class': 'TrendStrategy',
                      'params': {'fast_period': 20, 'slow_period': 50, 'atr_stop': 2.0}},
            'momentum': {'name': '动量策略', 'class': 'MomentumStrategy',
                         'params': {'fast_momentum': 20, 'slow_momentum': 50, 'atr_stop': 2.5}},
            'breakout': {'name': '突破策略', 'class': 'BreakoutStrategy',
                         'params': {'breakout_period': 20, 'breakout_threshold': 0.02, 'atr_stop': 2.0}},
            'funding_arb': {'name': '资金费率套利', 'class': 'FundingArbStrategy', 'params': {}},
        }

        for name in enabled_names:
            if name in strategy_presets:
                preset = strategy_presets[name]
                symbols_str = self._get_env(f'STRATEGY_{name.upper()}_SYMBOLS', 'BTC/USDT')
                symbols = [s.strip() for s in symbols_str.split(',')]
                strategies.append(StrategyConfig(
                    name=preset['name'], symbols=symbols, params=preset['params'],
                ))

        if not strategies:
            strategies.append(StrategyConfig(
                name='趋势跟踪', symbols=['BTC/USDT'],
                params={'fast_period': 20, 'slow_period': 50, 'atr_stop': 2.0},
            ))
        return strategies

    def _init_log(self) -> LogConfig:
        log_dir = self._get_env('LOG_DIR', str(self.project_root / 'logs'))
        return LogConfig(
            log_dir=log_dir,
            trade_log_file=self._get_env('TRADE_LOG_FILE', 'trades.jsonl'),
            system_log_file=self._get_env('SYSTEM_LOG_FILE', 'system.jsonl'),
            risk_log_file=self._get_env('RISK_LOG_FILE', 'risk.jsonl'),
            keep_days=int(self._get_env('LOG_KEEP_DAYS', '90')),
        )

    def _validate(self):
        warnings, errors = [], []

        if self.mode == 'live':
            if not self.exchange.api_key:
                errors.append("LIVE 模式缺少 BINANCE_API_KEY")
            if not self.exchange.secret_key:
                errors.append("LIVE 模式缺少 BINANCE_SECRET_KEY")

        if self.risk.max_position_pct <= 0 or self.risk.max_position_pct > 0.5:
            errors.append(f"max_position_pct ({self.risk.max_position_pct}) 不合理")
        if not self.strategies:
            errors.append("没有启用任何策略")

        # Day 8: 仓位配置验证
        if self.position.max_position_pct > 0.25:
            warnings.append(f"仓位上限 {self.position.max_position_pct:.0%} > 25%, 重仓风险较高")
        if self.position.default_method == SizingMethod.KELLY_FULL:
            warnings.append("使用完整凯利仓位 — 实际胜率和盈亏比有估计误差，建议半凯利")

        for w in warnings:
            print(f"  [!] {w}")
        for e in errors:
            print(f"  [X] {e}")

        if errors and self.mode == 'live':
            raise ValueError(f"配置校验失败 ({len(errors)} 个错误)，Live 模式拒绝启动")

    # ── 便捷方法 ──

    def check_readiness(self) -> dict:
        checks = []
        has_api = bool(self.exchange.api_key and self.exchange.secret_key)
        checks.append({'name': '交易所 API Key', 'passed': has_api,
                       'message': '已配置' if has_api else '未配置（paper 模式可用）'})
        risk_ok = 0 < self.risk.max_position_pct <= 0.25
        checks.append({'name': '风控参数', 'passed': risk_ok,
                       'message': f'单币种上限 {self.risk.max_position_pct:.0%}' if risk_ok else '风控参数异常'})
        checks.append({'name': '仓位管理 (Day 8)', 'passed': True,
                       'message': f'{self.position.default_method.value}, 单笔上限 {self.position.max_position_pct:.0%}'})
        checks.append({'name': '策略配置', 'passed': len(self.strategies) > 0,
                       'message': f'{len(self.strategies)} 个策略已启用'})
        data_dir = self.system.data_dir
        has_data = os.path.isdir(data_dir) and len(os.listdir(data_dir)) > 0
        checks.append({'name': '历史数据', 'passed': has_data,
                       'message': f'数据目录: {data_dir}' if has_data else f'数据目录不存在或为空: {data_dir}'})
        all_passed = all(c['passed'] for c in checks if c['name'] != '交易所 API Key')
        return {
            'ready': all_passed, 'mode': self.mode,
            'can_go_live': has_api and all_passed, 'checks': checks,
        }

    def print_readiness(self):
        status = self.check_readiness()
        print(f"\n{'='*60}")
        print(f"  实盘就绪检查 — 模式: {self.mode.upper()}")
        print(f"{'='*60}")
        for c in status['checks']:
            icon = '[OK]' if c['passed'] else '[--]'
            print(f"  {icon} {c['name']:<16}: {c['message']}")
        print(f"\n  Paper 模式可用: {'[OK]' if status['ready'] else '[X]'}")
        print(f"  Live 模式可用:  {'[OK]' if status['can_go_live'] else '[X] (缺少 API Key)'}")

    def display(self):
        print(f"\n{'='*60}")
        print(f"  当前配置 — {self.mode.upper()} 模式")
        print(f"{'='*60}")
        print(f"\n  交易所: {self.exchange.name} "
              f"({'testnet' if self.exchange.testnet else 'mainnet'})")
        print(f"  API Key: {'已配置' if self.exchange.api_key else '未配置'}")
        print(f"\n  [风控参数]")
        print(f"     单币种最大仓位: {self.risk.max_position_pct:.0%}")
        print(f"     日亏损熔断: {self.risk.daily_loss_limit_pct:.0%}")
        print(f"     连续亏损上限: {self.risk.max_consecutive_losses} 次")
        print(f"\n  [仓位管理 - Day 8]")
        print(f"     计算方法: {self.position.default_method.value}")
        print(f"     单笔上限: {self.position.max_position_pct:.0%}")
        print(f"     单币种敞口上限: {self.position.max_single_asset_exposure:.0%}")
        print(f"     总敞口上限: {self.position.max_total_exposure:.0%}")
        print(f"     盈利提现: 每{self.position.profit_take_interval_days}天提{self.position.profit_take_pct:.0%}")
        print(f"\n  [启用策略]")
        for s in self.strategies:
            print(f"     - {s.name} — {', '.join(s.symbols)}")
        print(f"\n  日志: {self.log.log_dir}/ (保留 {self.log.keep_days} 天)")


# ═══════════════════════════════
# .env 模板（含仓位配置）
# ═══════════════════════════════

ENV_TEMPLATE = """# ═══════════════════════════════════════
# 量化交易系统 .env 配置
# 此文件不入 Git 仓库
# ═══════════════════════════════════════

# ── 交易所 API ──
BINANCE_API_KEY=your_binance_api_key_here
BINANCE_SECRET_KEY=your_binance_secret_key_here
BINANCE_TESTNET=true

# OKX 备用交易所（可选）
OKX_API_KEY=
OKX_SECRET_KEY=

# ── AI 模型 API ──
DEEPSEEK_API_KEY=
ANTHROPIC_API_KEY=

# ── Telegram Bot ──
TELEGRAM_BOT_TOKEN=
TELEGRAM_CHAT_ID=

# ── 链上数据 API（可选）──
ETHERSCAN_API_KEY=
WHALE_ALERT_API_KEY=

# ── 策略配置 ──
ENABLED_STRATEGIES=trend,momentum
DEFAULT_ORDER_TYPE=limit

# ── 风控参数（只能收紧，不能放宽硬上限）──
RISK_MAX_POSITION_PCT=0.20
RISK_DAILY_LOSS_LIMIT_PCT=0.05
RISK_MAX_CONSECUTIVE_LOSSES=5

# ── 仓位管理 (Day 8) ──
POS_DEFAULT_METHOD=kelly_half
POS_MAX_POSITION_PCT=0.20
POS_MAX_SINGLE_ASSET_EXPOSURE=0.30
POS_MAX_TOTAL_EXPOSURE=0.80
POS_PROFIT_TAKE_PCT=0.30
POS_PROFIT_TAKE_MIN_PROFIT=500

# ── 日志 ──
LOG_DIR=logs
LOG_KEEP_DAYS=90
"""


def create_env_template(project_root=None):
    if project_root is None:
        project_root = os.path.dirname(os.path.abspath(__file__))
    example_path = os.path.join(project_root, '.env.example')
    with open(example_path, 'w', encoding='utf-8') as f:
        f.write(ENV_TEMPLATE)
    print(f"[OK] 已更新 .env.example: {example_path}")
    return True
