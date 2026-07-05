"""
配置管理器 — Day 7 核心模块

职责：
  1. 加载 .env 环境变量，缺失关键变量时报警
  2. 集中管理策略参数、风控参数、交易参数
  3. 配置校验（参数合法性检查）
  4. 支持 paper/live 两套配置切换

原则：
  - 敏感信息只在 .env 中，不入库
  - 所有参数有明确默认值，缺失不崩溃但报警
  - 风控参数硬编码上限，.env 只能收紧不能放宽

用法：
  from config_manager import ConfigManager
  cfg = ConfigManager(mode='paper')
  print(cfg.risk.max_position_pct)
"""

import os
import sys
from pathlib import Path
from dataclasses import dataclass, field
from typing import Optional, Dict, List


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
    """
    风控参数配置

    硬上限：写在代码里的物理上限，不能通过 .env 突破
    软上限：可通过 .env 配置，但不超过硬上限
    """
    # ── 仓位限制 ──
    max_position_pct: float = 0.2          # 单币种最大仓位（20%）
    max_total_exposure_pct: float = 1.0    # 总敞口上限（100%，即无杠杆）
    min_order_usdt: float = 50.0           # 最小下单金额

    # ── 日亏损熔断 ──
    daily_loss_limit_pct: float = 0.05     # 日亏损 5% 熔断
    daily_loss_hard_limit_pct: float = 0.08  # 硬上限，不可放宽

    # ── 连续亏损 ──
    max_consecutive_losses: int = 5        # 连续亏损 N 次 → 暂停
    consecutive_loss_cooldown_hours: int = 24  # 暂停冷却时间

    # ── 异常行情 ──
    price_spike_threshold_pct: float = 10.0  # 5 分钟内涨跌 >10% → 暂停
    volume_spike_threshold: float = 5.0      # 成交量突增 >5x → 警告

    # ── 执行参数 ──
    default_order_type: str = "limit"        # 默认限价单（省成本）
    max_slippage_pct: float = 0.005         # 最大可接受滑点 0.5%
    order_timeout_seconds: int = 60          # 限价单超时取消


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
    keep_days: int = 90  # 日志保留天数


@dataclass
class SystemConfig:
    """系统级配置"""
    mode: str = "paper"           # 'paper' | 'live'
    project_root: str = ""
    data_dir: str = "clean"
    heartbeat_interval_seconds: int = 60
    restart_on_error: bool = True
    max_restarts_per_hour: int = 3


# ═══════════════════════════════
# 配置管理器
# ═══════════════════════════════

class ConfigManager:
    """统一配置入口"""

    # ── 必填环境变量（paper 模式下可选）──
    REQUIRED_ENV_VARS = [
        "BINANCE_API_KEY",
        "BINANCE_SECRET_KEY",
    ]
    OPTIONAL_ENV_VARS = [
        "OKX_API_KEY", "OKX_SECRET_KEY",
        "DEEPSEEK_API_KEY", "ANTHROPIC_API_KEY",
        "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID",
        "ETHERSCAN_API_KEY", "WHALE_ALERT_API_KEY",
    ]

    # ── 风控硬上限（不可通过 .env 突破）──
    HARD_LIMITS = {
        'max_position_pct': 0.25,           # 单币种最多 25%
        'daily_loss_limit_pct': 0.08,       # 日亏损最多 8%
        'max_consecutive_losses': 7,        # 连续亏损最多 7 次
        'max_total_exposure_pct': 1.0,      # 不能加杠杆
    }

    def __init__(self, mode='paper', project_root=None):
        """
        mode: 'paper' | 'live'
        project_root: 项目根目录（用于定位 .env 和 data 目录）
        """
        self.mode = mode

        if project_root is None:
            project_root = os.path.dirname(os.path.abspath(__file__))
        self.project_root = Path(project_root)

        # ── 加载 .env ──
        self._load_env()

        # ── 初始化各配置模块 ──
        self.exchange = self._init_exchange()
        self.risk = self._init_risk()
        self.strategies = self._init_strategies()
        self.log = self._init_log()
        self.system = SystemConfig(
            mode=mode,
            project_root=str(self.project_root),
            data_dir=str(self.project_root / 'clean'),
        )

        # ── 校验 ──
        self._validate()

    def _load_env(self):
        """加载 .env 文件到 os.environ"""
        env_path = self.project_root / '.env'

        if env_path.exists():
            with open(env_path, 'r', encoding='utf-8') as f:
                for line in f:
                    line = line.strip()
                    if line and not line.startswith('#') and '=' in line:
                        key, _, val = line.partition('=')
                        key = key.strip()
                        val = val.strip().strip('"').strip("'")
                        # 只设置未定义的环境变量（已定义的不覆盖）
                        if key not in os.environ:
                            os.environ[key] = val
            print(f"📄 加载配置: {env_path}")
        else:
            print(f"⚠️ 未找到 .env 文件 ({env_path})，使用默认配置")

    def _get_env(self, key, default=""):
        """安全获取环境变量"""
        return os.environ.get(key, default)

    # ═══════════════════════════════
    # 各模块初始化
    # ═══════════════════════════════

    def _init_exchange(self) -> ExchangeConfig:
        """初始化交易所配置"""
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
        """初始化风控配置（env 只能收紧不能放宽）"""
        rc = RiskConfig()

        # ── 从 env 读取（并确保不超过硬上限）──
        for param, hard_limit in self.HARD_LIMITS.items():
            env_key = f"RISK_{param.upper()}"
            env_val = self._get_env(env_key, '')
            if env_val:
                try:
                    val = float(env_val)
                    # 确保不超过硬上限
                    capped = min(val, hard_limit) if hard_limit > 0 else val
                    if capped != val:
                        print(f"  ⚠️ RISK_{param.upper()}={val} 超过硬上限 {hard_limit}，已限制为 {capped}")
                    setattr(rc, param, capped)
                except ValueError:
                    pass

        rc.default_order_type = self._get_env('DEFAULT_ORDER_TYPE', 'limit')
        return rc

    def _init_strategies(self) -> List[StrategyConfig]:
        """初始化策略列表"""
        strategies = []

        # 默认: 趋势跟踪 + 动量策略
        enabled_strats = self._get_env('ENABLED_STRATEGIES', 'trend,momentum')
        enabled_names = [s.strip() for s in enabled_strats.split(',') if s.strip()]

        strategy_presets = {
            'trend': {
                'name': '趋势跟踪',
                'class': 'TrendStrategy',
                'params': {'fast_period': 20, 'slow_period': 50, 'atr_stop': 2.0},
            },
            'momentum': {
                'name': '动量策略',
                'class': 'MomentumStrategy',
                'params': {'fast_momentum': 20, 'slow_momentum': 50, 'atr_stop': 2.5},
            },
            'breakout': {
                'name': '突破策略',
                'class': 'BreakoutStrategy',
                'params': {'breakout_period': 20, 'breakout_threshold': 0.02, 'atr_stop': 2.0},
            },
            'funding_arb': {
                'name': '资金费率套利',
                'class': 'FundingArbStrategy',
                'params': {},
            },
        }

        for name in enabled_names:
            if name in strategy_presets:
                preset = strategy_presets[name]
                symbols_str = self._get_env(f'STRATEGY_{name.upper()}_SYMBOLS', 'BTC/USDT')
                symbols = [s.strip() for s in symbols_str.split(',')]
                strategies.append(StrategyConfig(
                    name=preset['name'],
                    symbols=symbols,
                    params=preset['params'],
                ))

        if not strategies:
            print("⚠️ 没有启用的策略，使用默认趋势跟踪")
            strategies.append(StrategyConfig(
                name='趋势跟踪',
                symbols=['BTC/USDT'],
                params={'fast_period': 20, 'slow_period': 50, 'atr_stop': 2.0},
            ))

        return strategies

    def _init_log(self) -> LogConfig:
        """初始化日志配置"""
        log_dir = self._get_env('LOG_DIR', str(self.project_root / 'logs'))
        return LogConfig(
            log_dir=log_dir,
            trade_log_file=self._get_env('TRADE_LOG_FILE', 'trades.jsonl'),
            system_log_file=self._get_env('SYSTEM_LOG_FILE', 'system.jsonl'),
            risk_log_file=self._get_env('RISK_LOG_FILE', 'risk.jsonl'),
            keep_days=int(self._get_env('LOG_KEEP_DAYS', '90')),
        )

    def _validate(self):
        """校验配置合法性"""
        warnings = []
        errors = []

        # ── live 模式必须检查 API key ──
        if self.mode == 'live':
            if not self.exchange.api_key:
                errors.append("LIVE 模式缺少 BINANCE_API_KEY — 请在 .env 中设置")
            if not self.exchange.secret_key:
                errors.append("LIVE 模式缺少 BINANCE_SECRET_KEY — 请在 .env 中设置")

        # ── 风控参数检查 ──
        if self.risk.max_position_pct <= 0 or self.risk.max_position_pct > 0.5:
            errors.append(f"max_position_pct ({self.risk.max_position_pct}) 不合理，应在 0.01-0.50 之间")
        if self.risk.daily_loss_limit_pct <= 0 or self.risk.daily_loss_limit_pct > 0.10:
            warnings.append(f"daily_loss_limit_pct ({self.risk.daily_loss_limit_pct}) 偏极端，建议 0.03-0.08")

        # ── 策略检查 ──
        if not self.strategies:
            errors.append("没有启用任何策略")
        for s in self.strategies:
            if not s.symbols:
                errors.append(f"策略 {s.name} 没有指定交易对")

        # ── 输出 ──
        for w in warnings:
            print(f"  ⚠️ {w}")
        for e in errors:
            print(f"  ❌ {e}")

        if errors and self.mode == 'live':
            raise ValueError(f"配置校验失败 ({len(errors)} 个错误)，Live 模式拒绝启动")

    # ═══════════════════════════════
    # 便捷方法
    # ═══════════════════════════════

    def check_readiness(self) -> dict:
        """
        检查实盘就绪状态

        返回: {ready: bool, checks: [{name, passed, message}]}
        """
        checks = []

        # API key
        has_api = bool(self.exchange.api_key and self.exchange.secret_key)
        checks.append({
            'name': '交易所 API Key',
            'passed': has_api,
            'message': '已配置' if has_api else '未配置（paper 模式可用）',
        })

        # 风控参数
        risk_ok = 0 < self.risk.max_position_pct <= 0.25
        checks.append({
            'name': '风控参数',
            'passed': risk_ok,
            'message': f'单币种上限 {self.risk.max_position_pct:.0%}' if risk_ok else '风控参数异常',
        })

        # 策略
        checks.append({
            'name': '策略配置',
            'passed': len(self.strategies) > 0,
            'message': f'{len(self.strategies)} 个策略已启用',
        })

        # 数据
        data_dir = self.system.data_dir
        has_data = os.path.isdir(data_dir) and len(os.listdir(data_dir)) > 0
        checks.append({
            'name': '历史数据',
            'passed': has_data,
            'message': f'数据目录: {data_dir}' if has_data else f'数据目录不存在或为空: {data_dir}',
        })

        # 日志目录
        log_dir = self.log.log_dir
        log_writable = os.path.isdir(log_dir) or os.access(os.path.dirname(log_dir) or '.', os.W_OK)
        checks.append({
            'name': '日志目录',
            'passed': log_writable,
            'message': f'日志目录: {log_dir}',
        })

        all_passed = all(c['passed'] for c in checks if c['name'] != '交易所 API Key')  # API key 非必须

        return {
            'ready': all_passed,
            'mode': self.mode,
            'can_go_live': has_api and all_passed,
            'checks': checks,
        }

    def print_readiness(self):
        """打印就绪状态"""
        status = self.check_readiness()
        print(f"\n{'='*60}")
        print(f"  实盘就绪检查 — 模式: {self.mode.upper()}")
        print(f"{'='*60}")
        for c in status['checks']:
            icon = '✅' if c['passed'] else '❌' if c['name'] == '交易所 API Key' and self.mode == 'paper' else '⬜'
            print(f"  {icon} {c['name']:<16}: {c['message']}")

        print(f"\n  📋 综合判定:")
        print(f"     Paper 模式可用: {'✅' if status['ready'] else '❌'}")
        print(f"     Live 模式可用:  {'✅' if status['can_go_live'] else '❌ (缺少 API Key)'}")

    def display(self):
        """打印完整配置摘要"""
        print(f"\n{'='*60}")
        print(f"  当前配置 — {self.mode.upper()} 模式")
        print(f"{'='*60}")
        print(f"\n  🏦 交易所: {self.exchange.name} "
              f"({'testnet' if self.exchange.testnet else 'mainnet'})")
        print(f"     API Key: {'已配置' if self.exchange.api_key else '未配置'}")

        print(f"\n  🛡️ 风控参数:")
        print(f"     单币种最大仓位: {self.risk.max_position_pct:.0%}")
        print(f"     日亏损熔断: {self.risk.daily_loss_limit_pct:.0%}")
        print(f"     连续亏损上限: {self.risk.max_consecutive_losses} 次")
        print(f"     默认订单类型: {self.risk.default_order_type}")
        print(f"     最大可接受滑点: {self.risk.max_slippage_pct:.1%}")

        print(f"\n  📈 启用策略:")
        for s in self.strategies:
            print(f"     • {s.name} — 交易对: {', '.join(s.symbols)}")

        print(f"\n  📝 日志: {self.log.log_dir}/")
        print(f"     保留 {self.log.keep_days} 天")


# ═══════════════════════════════
# 快速创建 .env 模板
# ═══════════════════════════════

ENV_TEMPLATE = """# ═══════════════════════════════════════
# 量化交易系统 .env 配置
# ⚠️ 此文件不入 Git 仓库
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

# ── 日志 ──
LOG_DIR=logs
LOG_KEEP_DAYS=90
"""


def create_env_template(project_root=None):
    """创建 .env 模板文件（如果不存在）"""
    if project_root is None:
        project_root = os.path.dirname(os.path.abspath(__file__))
    env_path = os.path.join(project_root, '.env')

    if os.path.exists(env_path):
        print(f"⚠️ .env 已存在: {env_path}")
        return False

    # 创建 .env.example 供参考
    example_path = os.path.join(project_root, '.env.example')
    with open(example_path, 'w', encoding='utf-8') as f:
        f.write(ENV_TEMPLATE)
    print(f"📄 已创建 .env.example: {example_path}")
    print(f"   请复制为 .env 并填入你的 API Key: cp .env.example .env")
    return True
